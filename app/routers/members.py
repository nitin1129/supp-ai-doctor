"""Member side: Consult (find and book) and Track → Consults."""

from datetime import UTC, date, datetime

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, or_, select

from app.config import get_settings
from app.deps import DB, Member
from app.models import Appointment, AppointmentStatus, Doctor, DoctorStatus, Review
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
    query = select(Doctor).where(Doctor.status == DoctorStatus.ACTIVE, Doctor.fee_paise >= 0)
    if specialty:
        query = query.where(func.lower(func.array_to_string(Doctor.specialties, "|")).like(f"%{specialty.lower()}%"))
    if mode:
        query = query.where(Doctor.modes.any(mode))
    if pincode:
        if mode == "clinic":
            query = query.where(Doctor.clinic_pincode == pincode)
        else:
            query = query.where(or_(Doctor.modes.any("video"), Doctor.clinic_pincode == pincode))
    if q:
        query = query.where(func.lower(Doctor.full_name).like(f"%{q.lower()}%"))
    doctors = db.scalars(
        query.order_by(Doctor.rating_count.desc(), Doctor.experience_years.desc(), Doctor.id)
        .offset(offset).limit(limit)
    ).all()
    now = datetime.now(UTC)
    cards = [doctor_card(d, next_available(db, d, now)) for d in doctors]
    # Doctors with an opening soonest first; those without hours go last.
    cards.sort(key=lambda c: (c.next_available_at is None, c.next_available_at or now))
    return cards


@router.get("/doctors/{doctor_id}", response_model=DoctorCard)
def doctor_profile(doctor_id: int, db: DB) -> DoctorCard:
    d = db.get(Doctor, doctor_id)
    if d is None or d.status != DoctorStatus.ACTIVE:
        raise HTTPException(404, "Doctor not found")
    return doctor_card(d, next_available(db, d))


@router.get("/doctors/{doctor_id}/slots", response_model=DaySlotsOut)
def doctor_slots(doctor_id: int, db: DB, date_: date = Query(alias="date", description="IST date")) -> DaySlotsOut:
    d = db.get(Doctor, doctor_id)
    if d is None or d.status != DoctorStatus.ACTIVE:
        raise HTTPException(404, "Doctor not found")
    leave = leave_on(db, d.id, date_)
    slots = day_slots(db, d, date_)
    return DaySlotsOut(
        doctor_id=d.id,
        date=date_,
        day_off=leave is not None,
        day_off_note=leave.note if leave else None,
        slots=[SlotOut(start_at=s.start_at, end_at=s.end_at, start_ist=ist_label(s.start_at), available=s.available)
               for s in slots],
    )


# --- Book and track ---------------------------------------------------------------

def _mine(db: DB, appointment_id: int, member: Member, lock: bool = False) -> Appointment:
    q = select(Appointment).where(Appointment.id == appointment_id)
    if lock:
        q = q.with_for_update()
    a = db.scalar(q)
    if a is None or a.member_id != member.sub:
        raise HTTPException(404, "Appointment not found")
    return a


@router.post("/appointments", response_model=BookingOut, status_code=201)
def book(body: BookIn, db: DB, member: Member) -> BookingOut:
    """Holds the slot for a few minutes and returns a Razorpay order to pay.
    The booking is confirmed when Razorpay's webhook reports the payment."""
    a = appt_svc.create_hold(
        db, doctor_id=body.doctor_id, member_id=member.sub, start_at=body.start_at,
        mode=body.mode, patient=body.patient.model_dump(),
    )
    out = appointment_out(a)
    return BookingOut(**out.model_dump(), razorpay_key_id=get_settings().razorpay_key_id, amount_paise=a.fee_paise)


@router.get("/appointments", response_model=list[AppointmentOut])
def my_appointments(
    db: DB, member: Member, scope: str = Query(default="all", pattern="^(upcoming|past|all)$")
) -> list[AppointmentOut]:
    q = select(Appointment).where(
        Appointment.member_id == member.sub, Appointment.status != AppointmentStatus.EXPIRED
    )
    now = datetime.now(UTC)
    if scope == "upcoming":
        q = q.where(Appointment.end_at >= now).order_by(Appointment.start_at)
    elif scope == "past":
        q = q.where(Appointment.end_at < now).order_by(Appointment.start_at.desc())
    else:
        q = q.order_by(Appointment.start_at.desc())
    return [appointment_out(a, now) for a in db.scalars(q.limit(200))]


@router.get("/appointments/{appointment_id}", response_model=AppointmentOut)
def get_appointment(appointment_id: int, db: DB, member: Member) -> AppointmentOut:
    return appointment_out(_mine(db, appointment_id, member))


@router.patch("/appointments/{appointment_id}/reschedule", response_model=AppointmentOut)
def reschedule(appointment_id: int, body: RescheduleIn, db: DB, member: Member) -> AppointmentOut:
    a = _mine(db, appointment_id, member, lock=True)
    appt_svc.reschedule(db, a, body.start_at, member.sub)
    db.commit()
    return appointment_out(a)


@router.post("/appointments/{appointment_id}/cancel", response_model=AppointmentOut)
def cancel(appointment_id: int, body: CancelIn, db: DB, member: Member) -> AppointmentOut:
    """Full refund up to the cut-off before the consult; no refund after it."""
    a = _mine(db, appointment_id, member, lock=True)
    if a.status == AppointmentStatus.BOOKED and a.start_at <= datetime.now(UTC):
        raise HTTPException(409, "The consult has already started")
    pct = appt_svc.member_cancel_refund_percent(a)
    appt_svc.cancel(db, a, by="member", actor_id=member.sub, reason=body.reason, refund_percent=pct)
    db.commit()
    return appointment_out(a)


@router.post("/appointments/{appointment_id}/review", status_code=201)
def review(appointment_id: int, body: ReviewIn, db: DB, member: Member) -> dict:
    a = _mine(db, appointment_id, member)
    if a.status != AppointmentStatus.COMPLETED:
        raise HTTPException(409, "You can review a consult once it's completed")
    if db.scalar(select(Review.id).where(Review.appointment_id == a.id)):
        raise HTTPException(409, "You've already reviewed this consult")
    db.add(Review(appointment_id=a.id, doctor_id=a.doctor_id, member_id=member.sub,
                  rating=body.rating, comment=body.comment))
    doctor = appt_svc.lock_doctor(db, a.doctor_id)
    doctor.rating_sum += body.rating
    doctor.rating_count += 1
    db.commit()
    return {"ok": True}
