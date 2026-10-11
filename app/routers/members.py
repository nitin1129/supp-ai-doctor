"""Member side: Consult (find and book) and Track → Consults."""

import re
from datetime import date

from fastapi import APIRouter, HTTPException, Query
from pymongo import DESCENDING
from pymongo.errors import DuplicateKeyError

from app.config import get_settings
from app.db import now, oid
from app.deps import DB, Member
from app.models import AppointmentStatus, DoctorStatus
from app.schemas.booking import (
    AppointmentOut,
    BookIn,
    BookingOut,
    CancelIn,
    DaySlotsOut,
    DoctorCard,
    RescheduleIn,
    ReviewIn,
    SlotOut,
    ist_label,
)
from app.serializers import appointment_out, doctor_card
from app.services import appointments as appt_svc
from app.services.slots import day_slots, leave_on, next_available

router = APIRouter(tags=["Member: consult"])

PUBLIC_DOCTOR_FIELDS = {"pan_encrypted": 0, "bank_account_encrypted": 0, "aadhaar_last4": 0,
                        "bank_account_last4": 0, "bank_ifsc": 0}


def _active_doctor(db: DB, doctor_id: str) -> dict:
    d = db.doctors.find_one({"_id": oid(doctor_id, "Doctor"), "status": DoctorStatus.ACTIVE.value},
                            PUBLIC_DOCTOR_FIELDS)
    if d is None:
        raise HTTPException(404, "Doctor not found")
    return d


# --- Find a doctor ---------------------------------------------------------------

@router.get("/doctors", response_model=list[DoctorCard])
def find_doctors(
    db: DB,
    specialty: str | None = None,
    pincode: str | None = Query(default=None, pattern=r"^[1-9]\d{5}$"),
    mode: str | None = Query(default=None, pattern="^(video|clinic)$"),
    q: str | None = None,
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
) -> list[DoctorCard]:
    """Active doctors. With a pincode, in-clinic doctors must be at that pincode;
    video doctors are shown wherever the member is."""
    query: dict = {"status": DoctorStatus.ACTIVE.value}
    if specialty:
        query["specialties"] = {"$regex": re.escape(specialty), "$options": "i"}
    if q:
        query["full_name"] = {"$regex": re.escape(q), "$options": "i"}
    if pincode and mode == "clinic":
        query["modes"] = "clinic"
        query["clinic_pincode"] = pincode
    elif pincode:
        if mode:
            query["modes"] = mode
        query["$or"] = [{"modes": "video"}, {"clinic_pincode": pincode}]
    elif mode:
        query["modes"] = mode
    doctors = (db.doctors.find(query, PUBLIC_DOCTOR_FIELDS)
               .sort([("rating_count", DESCENDING), ("experience_years", DESCENDING), ("_id", 1)])
               .skip(offset).limit(limit))
    t = now()
    cards = [doctor_card(d, next_available(db, d, t)) for d in doctors]
    # Doctors with an opening soonest first; those without hours go last.
    cards.sort(key=lambda c: (c.next_available_at is None, c.next_available_at or t))
    return cards


@router.get("/doctors/{doctor_id}", response_model=DoctorCard)
def doctor_profile(doctor_id: str, db: DB) -> DoctorCard:
    d = _active_doctor(db, doctor_id)
    return doctor_card(d, next_available(db, d))


@router.get("/doctors/{doctor_id}/slots", response_model=DaySlotsOut)
def doctor_slots(doctor_id: str, db: DB, date_: date = Query(alias="date", description="IST date")) -> DaySlotsOut:
    d = _active_doctor(db, doctor_id)
    leave = leave_on(db, d["_id"], date_)
    return DaySlotsOut(
        doctor_id=str(d["_id"]),
        date=date_,
        day_off=leave is not None,
        day_off_note=leave.get("note") if leave else None,
        slots=[SlotOut(start_at=s.start_at, end_at=s.end_at, start_ist=ist_label(s.start_at), available=s.available)
               for s in day_slots(db, d, date_)],
    )


# --- Book and track ---------------------------------------------------------------

def _mine(db: DB, appointment_id: str, member: Member) -> dict:
    a = db.appointments.find_one({"_id": oid(appointment_id, "Appointment"), "member_id": member.sub})
    if a is None:
        raise HTTPException(404, "Appointment not found")
    return a


@router.post("/appointments", response_model=BookingOut, status_code=201)
def book(body: BookIn, db: DB, member: Member) -> BookingOut:
    """Holds the slot for a few minutes and returns a Razorpay order to pay.
    The booking is confirmed when Razorpay's webhook reports the payment."""
    a = appt_svc.create_hold(
        db, doctor_id=oid(body.doctor_id, "Doctor"), member_id=member.sub, start_at=body.start_at,
        mode=body.mode, patient=body.patient.model_dump(),
    )
    out = appointment_out(a)
    return BookingOut(**out.model_dump(), razorpay_key_id=get_settings().razorpay_key_id, amount_paise=a["fee_paise"])


@router.get("/appointments", response_model=list[AppointmentOut])
def my_appointments(
    db: DB, member: Member, scope: str = Query(default="all", pattern="^(upcoming|past|all)$")
) -> list[AppointmentOut]:
    q: dict = {"member_id": member.sub, "status": {"$ne": AppointmentStatus.EXPIRED.value}}
    t = now()
    if scope == "upcoming":
        q["end_at"] = {"$gte": t}
        sort = [("start_at", 1)]
    elif scope == "past":
        q["end_at"] = {"$lt": t}
        sort = [("start_at", DESCENDING)]
    else:
        sort = [("start_at", DESCENDING)]
    return [appointment_out(a, t) for a in db.appointments.find(q).sort(sort).limit(200)]


@router.get("/appointments/{appointment_id}", response_model=AppointmentOut)
def get_appointment(appointment_id: str, db: DB, member: Member) -> AppointmentOut:
    return appointment_out(_mine(db, appointment_id, member))


@router.patch("/appointments/{appointment_id}/reschedule", response_model=AppointmentOut)
def reschedule(appointment_id: str, body: RescheduleIn, db: DB, member: Member) -> AppointmentOut:
    a = _mine(db, appointment_id, member)
    return appointment_out(appt_svc.reschedule(db, a, body.start_at, member.sub))


@router.post("/appointments/{appointment_id}/cancel", response_model=AppointmentOut)
def cancel(appointment_id: str, body: CancelIn, db: DB, member: Member) -> AppointmentOut:
    """Full refund up to the cut-off before the consult; no refund after it."""
    a = _mine(db, appointment_id, member)
    if a["status"] == AppointmentStatus.BOOKED and a["start_at"] <= now():
        raise HTTPException(409, "The consult has already started")
    pct = appt_svc.member_cancel_refund_percent(a)
    a = appt_svc.cancel(db, a, by="member", actor_id=member.sub, reason=body.reason, refund_percent=pct)
    return appointment_out(a)


@router.post("/appointments/{appointment_id}/review", status_code=201)
def review(appointment_id: str, body: ReviewIn, db: DB, member: Member) -> dict:
    a = _mine(db, appointment_id, member)
    if a["status"] != AppointmentStatus.COMPLETED:
        raise HTTPException(409, "You can review a consult once it's completed")
    try:
        db.reviews.insert_one({"appointment_id": a["_id"], "doctor_id": a["doctor_id"], "member_id": member.sub,
                               "rating": body.rating, "comment": body.comment, "created_at": now()})
    except DuplicateKeyError:
        raise HTTPException(409, "You've already reviewed this consult")
    db.doctors.update_one({"_id": a["doctor_id"]}, {"$inc": {"rating_sum": body.rating, "rating_count": 1}})
    return {"ok": True}
