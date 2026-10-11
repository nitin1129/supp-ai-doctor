"""Doctor dashboard at /doctor: Today, Calendar and Settings.

The doctor is always taken from the token (CurrentDoctor); no endpoint here
accepts a doctor_id from the request.
"""

from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import delete, select

from app.deps import DB, CurrentDoctor
from app.models import Appointment, AppointmentStatus, DoctorHours, DoctorLeave, ProfileChangeRequest
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
from app.services.slots import ist_day_bounds, today_ist

router = APIRouter(prefix="/doctor", tags=["Doctor dashboard"])

VISIBLE = [
    AppointmentStatus.BOOKED,
    AppointmentStatus.COMPLETED,
    AppointmentStatus.NO_SHOW,
    AppointmentStatus.CANCELLED,
    AppointmentStatus.NEEDS_REVIEW,
]


# --- Profile -------------------------------------------------------------------

def _profile(db: DB, d) -> ProfileOut:
    pending = db.scalars(
        select(ProfileChangeRequest).where(
            ProfileChangeRequest.doctor_id == d.id, ProfileChangeRequest.status == "pending"
        )
    ).all()
    return ProfileOut(
        id=d.id, full_name=d.full_name, email=d.email, mobile=d.mobile, specialties=d.specialties,
        qualifications=d.qualifications, languages=d.languages, modes=d.modes, bio=d.bio,
        fee_inr=d.fee_paise / 100, experience_years=d.experience_years, clinic_address=d.clinic_address,
        clinic_pincode=d.clinic_pincode, registration_number=d.registration_number,
        registration_council=d.registration_council, rating=rating(d), rating_count=d.rating_count,
        pending_changes=[{"id": p.id, "changes": p.changes, "created_at": p.created_at.isoformat()} for p in pending],
    )


@router.get("/me", response_model=ProfileOut)
def get_profile(db: DB, doctor: CurrentDoctor) -> ProfileOut:
    return _profile(db, doctor)


@router.put("/me", response_model=ProfileOut)
def update_profile(body: ProfileIn, db: DB, doctor: CurrentDoctor) -> ProfileOut:
    data = body.model_dump(exclude_unset=True)
    needs_approval = {k: data.pop(k) for k in ("fee_inr", "specialties") if k in data}
    modes = data.get("modes", doctor.modes)
    if "clinic" in modes and not (data.get("clinic_address", doctor.clinic_address)
                                  and data.get("clinic_pincode", doctor.clinic_pincode)):
        raise HTTPException(422, "In-clinic consults need a clinic address and pincode")
    for k, v in data.items():
        setattr(doctor, k, v)
    if needs_approval:
        db.add(ProfileChangeRequest(doctor_id=doctor.id, changes=needs_approval))
    audit(db, actor_type="doctor", actor_id=str(doctor.id), action="doctor.profile_updated",
          entity_type="doctor", entity_id=doctor.id, data={"applied": data, "for_approval": needs_approval})
    db.commit()
    return _profile(db, doctor)


# --- Hours and days off -----------------------------------------------------------

@router.get("/hours", response_model=HoursOut)
def get_hours(doctor: CurrentDoctor) -> HoursOut:
    return HoursOut(
        windows=[HoursWindow(weekday=h.weekday, start=h.start_time, end=h.end_time) for h in doctor.hours],
        slot_minutes=doctor.slot_minutes,
        notice_minutes=doctor.notice_minutes,
    )


@router.put("/hours", response_model=HoursOut)
def put_hours(body: HoursIn, db: DB, doctor: CurrentDoctor) -> HoursOut:
    """Replaces the weekly schedule. Existing bookings are kept as they are."""
    db.execute(delete(DoctorHours).where(DoctorHours.doctor_id == doctor.id))
    for w in body.windows:
        db.add(DoctorHours(doctor_id=doctor.id, weekday=w.weekday, start_time=w.start, end_time=w.end))
    if body.slot_minutes is not None:
        doctor.slot_minutes = body.slot_minutes
    if body.notice_minutes is not None:
        doctor.notice_minutes = body.notice_minutes
    db.commit()
    db.refresh(doctor)
    return get_hours(doctor)


@router.get("/leave", response_model=list[LeaveOut])
def list_leave(db: DB, doctor: CurrentDoctor) -> list[LeaveOut]:
    rows = db.scalars(
        select(DoctorLeave).where(DoctorLeave.doctor_id == doctor.id, DoctorLeave.date >= today_ist())
        .order_by(DoctorLeave.date)
    ).all()
    return [LeaveOut(id=r.id, date=r.date, note=r.note) for r in rows]


@router.post("/leave", status_code=201)
def add_leave(body: LeaveIn, db: DB, doctor: CurrentDoctor) -> dict:
    """Mark a day off. "Take today off" is this endpoint with today's date.

    If patients are already booked that day the call returns 409 with the list,
    unless cancel_booked is true, in which case they are cancelled with a full
    refund and told the doctor's note.
    """
    if body.date < today_ist():
        raise HTTPException(422, "That date has passed")
    doctor = appt_svc.lock_doctor(db, doctor.id)
    if db.scalar(select(DoctorLeave).where(DoctorLeave.doctor_id == doctor.id, DoctorLeave.date == body.date)):
        raise HTTPException(409, "That day is already marked off")
    start, end = ist_day_bounds(body.date)
    booked = appt_svc.active_on_day(db, doctor.id, start, end)
    if booked and not body.cancel_booked:
        raise HTTPException(409, {
            "message": f"{len(booked)} appointment(s) are booked that day. "
                       "Send cancel_booked=true to cancel them with a full refund.",
            "appointments": [appointment_out(a).model_dump(mode="json") for a in booked],
        })
    for a in booked:
        appt_svc.cancel(db, a, by="doctor", actor_id=str(doctor.id),
                        reason=body.note or "Doctor is unavailable that day", refund_percent=100)
    leave = DoctorLeave(doctor_id=doctor.id, date=body.date, note=body.note)
    db.add(leave)
    db.commit()
    return {"id": leave.id, "date": leave.date, "note": leave.note, "cancelled_appointments": len(booked)}


@router.delete("/leave/{leave_id}", status_code=204)
def remove_leave(leave_id: int, db: DB, doctor: CurrentDoctor) -> None:
    leave = db.get(DoctorLeave, leave_id)
    if leave is None or leave.doctor_id != doctor.id:
        raise HTTPException(404, "Day off not found")
    db.delete(leave)
    db.commit()


# --- Appointments ----------------------------------------------------------------

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
    rows = db.scalars(
        select(Appointment).where(
            Appointment.doctor_id == doctor.id,
            Appointment.start_at >= start,
            Appointment.start_at < end,
            Appointment.status.in_(VISIBLE),
        ).order_by(Appointment.start_at)
    ).all()
    return [appointment_out(a) for a in rows]


@router.get("/appointments/{appointment_id}", response_model=AppointmentOut)
def get_appointment(appointment_id: int, db: DB, doctor: CurrentDoctor) -> AppointmentOut:
    a = db.get(Appointment, appointment_id)
    if a is None or a.doctor_id != doctor.id:
        raise HTTPException(404, "Appointment not found")
    return appointment_out(a)


@router.patch("/appointments/{appointment_id}/status", response_model=AppointmentOut)
def set_outcome(appointment_id: int, body: OutcomeIn, db: DB, doctor: CurrentDoctor) -> AppointmentOut:
    """Done or No-show (the patient didn't join)."""
    a = db.scalar(select(Appointment).where(Appointment.id == appointment_id).with_for_update())
    if a is None or a.doctor_id != doctor.id:
        raise HTTPException(404, "Appointment not found")
    if a.status == AppointmentStatus.NEEDS_REVIEW:
        raise HTTPException(409, "This appointment is with SuppAI support for review")
    appt_svc.mark_outcome(db, a, body.status, actor_type="doctor", actor_id=str(doctor.id))
    db.commit()
    return appointment_out(a, datetime.now(UTC))
