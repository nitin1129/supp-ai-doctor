"""Doctor dashboard at /doctor: Today, Calendar and Settings.

The doctor is always taken from the token (CurrentDoctor); no endpoint here
accepts a doctor_id from the request.
"""

from datetime import date, time, timedelta

from fastapi import APIRouter, HTTPException, Query
from pymongo.errors import DuplicateKeyError

from app.db import now, oid
from app.deps import DB, CurrentDoctor
from app.models import AppointmentStatus
from app.schemas.booking import (
    AppointmentOut,
    HoursIn,
    HoursOut,
    HoursWindow,
    LeaveIn,
    LeaveOut,
    OutcomeIn,
    ProfileIn,
    ProfileOut,
)
from app.serializers import appointment_out, rating
from app.services import appointments as appt_svc
from app.services.audit import audit
from app.services.locks import doctor_lock
from app.services.slots import ist_day_bounds, today_ist

router = APIRouter(prefix="/doctor", tags=["Doctor dashboard"])

VISIBLE = [s.value for s in (AppointmentStatus.BOOKED, AppointmentStatus.COMPLETED, AppointmentStatus.NO_SHOW,
                             AppointmentStatus.CANCELLED, AppointmentStatus.NEEDS_REVIEW)]


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def _time(m: int) -> time:
    return time(m // 60, m % 60) if m < 24 * 60 else time(23, 59)


# --- Profile -------------------------------------------------------------------

def _profile(db: DB, d: dict) -> ProfileOut:
    pending = db.profile_change_requests.find({"doctor_id": d["_id"], "status": "pending"})
    return ProfileOut(
        id=str(d["_id"]), full_name=d["full_name"], email=d["email"], mobile=d.get("mobile"),
        specialties=d["specialties"], qualifications=d["qualifications"], languages=d["languages"], modes=d["modes"],
        bio=d.get("bio"), fee_inr=d["fee_paise"] / 100, experience_years=d["experience_years"],
        clinic_address=d.get("clinic_address"), clinic_pincode=d.get("clinic_pincode"),
        registration_number=d.get("registration_number"), registration_council=d.get("registration_council"),
        rating=rating(d), rating_count=d.get("rating_count", 0),
        pending_changes=[{"id": str(p["_id"]), "changes": p["changes"], "created_at": p["created_at"].isoformat()}
                         for p in pending],
    )


@router.get("/me", response_model=ProfileOut)
def get_profile(db: DB, doctor: CurrentDoctor) -> ProfileOut:
    return _profile(db, doctor)


@router.put("/me", response_model=ProfileOut)
def update_profile(body: ProfileIn, db: DB, doctor: CurrentDoctor) -> ProfileOut:
    data = body.model_dump(exclude_unset=True)
    needs_approval = {k: data.pop(k) for k in ("fee_inr", "specialties") if k in data}
    modes = data.get("modes", doctor["modes"])
    if "clinic" in modes and not (data.get("clinic_address", doctor.get("clinic_address"))
                                  and data.get("clinic_pincode", doctor.get("clinic_pincode"))):
        raise HTTPException(422, "In-clinic consults need a clinic address and pincode")
    if data:
        db.doctors.update_one({"_id": doctor["_id"]}, {"$set": data})
    if needs_approval:
        db.profile_change_requests.insert_one({"doctor_id": doctor["_id"], "changes": needs_approval,
                                               "status": "pending", "note": None, "decided_by": None,
                                               "decided_at": None, "created_at": now()})
    audit(db, actor_type="doctor", actor_id=str(doctor["_id"]), action="doctor.profile_updated",
          entity_type="doctor", entity_id=doctor["_id"], data={"applied": data, "for_approval": needs_approval})
    return _profile(db, db.doctors.find_one({"_id": doctor["_id"]}))


# --- Hours and days off -----------------------------------------------------------

def _hours_out(d: dict) -> HoursOut:
    return HoursOut(
        windows=[HoursWindow(weekday=h["weekday"], start=_time(h["start_min"]), end=_time(h["end_min"]))
                 for h in sorted(d.get("hours", []), key=lambda h: (h["weekday"], h["start_min"]))],
        slot_minutes=d["slot_minutes"],
        notice_minutes=d["notice_minutes"],
    )


@router.get("/hours", response_model=HoursOut)
def get_hours(doctor: CurrentDoctor) -> HoursOut:
    return _hours_out(doctor)


@router.put("/hours", response_model=HoursOut)
def put_hours(body: HoursIn, db: DB, doctor: CurrentDoctor) -> HoursOut:
    """Replaces the weekly schedule. Existing bookings are kept as they are."""
    changes: dict = {"hours": [{"weekday": w.weekday, "start_min": _minutes(w.start), "end_min": _minutes(w.end)}
                               for w in body.windows]}
    if body.slot_minutes is not None:
        changes["slot_minutes"] = body.slot_minutes
    if body.notice_minutes is not None:
        changes["notice_minutes"] = body.notice_minutes
    db.doctors.update_one({"_id": doctor["_id"]}, {"$set": changes})
    return _hours_out(db.doctors.find_one({"_id": doctor["_id"]}))


@router.get("/leave", response_model=list[LeaveOut])
def list_leave(db: DB, doctor: CurrentDoctor) -> list[LeaveOut]:
    rows = db.doctor_leave.find({"doctor_id": doctor["_id"], "date": {"$gte": today_ist().isoformat()}}).sort("date", 1)
    return [LeaveOut(id=str(r["_id"]), date=date.fromisoformat(r["date"]), note=r.get("note")) for r in rows]


@router.post("/leave", status_code=201)
def add_leave(body: LeaveIn, db: DB, doctor: CurrentDoctor) -> dict:
    """Mark a day off. "Take today off" is this endpoint with today's date.

    If patients are already booked that day the call returns 409 with the list,
    unless cancel_booked is true, in which case they are cancelled with a full
    refund and told the doctor's note.
    """
    if body.date < today_ist():
        raise HTTPException(422, "That date has passed")
    with doctor_lock(db, doctor["_id"]):
        if db.doctor_leave.find_one({"doctor_id": doctor["_id"], "date": body.date.isoformat()}):
            raise HTTPException(409, "That day is already marked off")
        start, end = ist_day_bounds(body.date)
        booked = appt_svc.active_between(db, doctor["_id"], start, end)
        if booked and not body.cancel_booked:
            raise HTTPException(409, {
                "message": f"{len(booked)} appointment(s) are booked that day. "
                           "Send cancel_booked=true to cancel them with a full refund.",
                "appointments": [appointment_out(a).model_dump(mode="json") for a in booked],
            })
        try:
            leave_id = db.doctor_leave.insert_one({"doctor_id": doctor["_id"], "date": body.date.isoformat(),
                                                   "note": body.note, "created_at": now()}).inserted_id
        except DuplicateKeyError:
            raise HTTPException(409, "That day is already marked off")
    cancelled = 0
    for a in booked:
        try:
            appt_svc.cancel(db, a, by="doctor", actor_id=str(doctor["_id"]),
                            reason=body.note or "Doctor is unavailable that day", refund_percent=100)
            cancelled += 1
        except HTTPException:
            pass
    return {"id": str(leave_id), "date": body.date, "note": body.note, "cancelled_appointments": cancelled}


@router.delete("/leave/{leave_id}", status_code=204)
def remove_leave(leave_id: str, db: DB, doctor: CurrentDoctor) -> None:
    res = db.doctor_leave.delete_one({"_id": oid(leave_id, "Day off"), "doctor_id": doctor["_id"]})
    if res.deleted_count == 0:
        raise HTTPException(404, "Day off not found")


# --- Appointments ----------------------------------------------------------------

def _mine(db: DB, appointment_id: str, doctor: dict) -> dict:
    a = db.appointments.find_one({"_id": oid(appointment_id, "Appointment"), "doctor_id": doctor["_id"]})
    if a is None:
        raise HTTPException(404, "Appointment not found")
    return a


@router.get("/appointments", response_model=list[AppointmentOut])
def list_appointments(
    db: DB,
    doctor: CurrentDoctor,
    date_: date | None = Query(default=None, alias="date", description="IST date; defaults to today"),
    days: int = Query(default=1, ge=1, le=62, description="Number of days from date (7 for 'next 7 days')"),
) -> list[AppointmentOut]:
    first = date_ or today_ist()
    start, _ = ist_day_bounds(first)
    _, end = ist_day_bounds(first + timedelta(days=days - 1))
    rows = db.appointments.find({
        "doctor_id": doctor["_id"], "start_at": {"$gte": start, "$lt": end}, "status": {"$in": VISIBLE},
    }).sort("start_at", 1)
    return [appointment_out(a) for a in rows]


@router.get("/appointments/{appointment_id}", response_model=AppointmentOut)
def get_appointment(appointment_id: str, db: DB, doctor: CurrentDoctor) -> AppointmentOut:
    return appointment_out(_mine(db, appointment_id, doctor))


@router.patch("/appointments/{appointment_id}/status", response_model=AppointmentOut)
def set_outcome(appointment_id: str, body: OutcomeIn, db: DB, doctor: CurrentDoctor) -> AppointmentOut:
    """Done or No-show (the patient didn't join)."""
    a = _mine(db, appointment_id, doctor)
    if a["status"] == AppointmentStatus.NEEDS_REVIEW:
        raise HTTPException(409, "This appointment is with SuppAI support for review")
    a = appt_svc.mark_outcome(db, a, body.status, actor_type="doctor", actor_id=str(doctor["_id"]))
    return appointment_out(a)
