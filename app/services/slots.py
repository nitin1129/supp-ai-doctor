"""Slot engine.

Slots are built from the doctor's weekly hours (IST wall-clock windows), slot
length and booking notice. Booked or held slots and days off are removed.
Everything returned is in UTC; the API layer adds the IST label.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import SLOT_HOLDING_STATUSES, Appointment, AppointmentStatus, Doctor, DoctorHours, DoctorLeave


@dataclass
class Slot:
    start_at: datetime
    end_at: datetime
    available: bool


def ist_day_bounds(day: date) -> tuple[datetime, datetime]:
    tz = get_settings().tz
    start = datetime.combine(day, time.min, tzinfo=tz)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def today_ist(now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(get_settings().tz).date()


def active_appointment_filter(now: datetime):
    """Appointments that occupy their slot right now (expired holds do not)."""
    return and_(
        Appointment.status.in_(SLOT_HOLDING_STATUSES),
        or_(
            Appointment.status != AppointmentStatus.PENDING_PAYMENT,
            Appointment.hold_expires_at > now,
        ),
    )


def busy_intervals(
    db: Session, doctor_id: int, start: datetime, end: datetime, now: datetime, exclude_id: int | None = None
) -> list[tuple[datetime, datetime]]:
    q = select(Appointment.start_at, Appointment.end_at).where(
        Appointment.doctor_id == doctor_id,
        Appointment.start_at < end,
        Appointment.end_at > start,
        active_appointment_filter(now),
    )
    if exclude_id is not None:
        q = q.where(Appointment.id != exclude_id)
    return [(a, b) for a, b in db.execute(q)]


def leave_on(db: Session, doctor_id: int, day: date) -> DoctorLeave | None:
    return db.scalar(select(DoctorLeave).where(DoctorLeave.doctor_id == doctor_id, DoctorLeave.date == day))


def day_slots(
    db: Session, doctor: Doctor, day: date, now: datetime | None = None, exclude_appointment_id: int | None = None
) -> list[Slot]:
    """All slots for one IST day, each marked available or not.

    Booked slots are returned with available=False so the UI can cross them out.
    Slots inside the notice period or outside the booking window are dropped.
    """
    s = get_settings()
    tz = s.tz
    now = now or datetime.now(UTC)
    if day < today_ist(now) or day > today_ist(now) + timedelta(days=s.booking_window_days):
        return []
    if leave_on(db, doctor.id, day):
        return []

    windows = db.scalars(
        select(DoctorHours)
        .where(DoctorHours.doctor_id == doctor.id, DoctorHours.weekday == day.weekday())
        .order_by(DoctorHours.start_time)
    ).all()
    if not windows:
        return []

    earliest = now + timedelta(minutes=doctor.notice_minutes)
    length = timedelta(minutes=doctor.slot_minutes)
    day_start, day_end = ist_day_bounds(day)
    busy = busy_intervals(db, doctor.id, day_start, day_end, now, exclude_appointment_id)

    slots: list[Slot] = []
    for w in windows:
        cursor = datetime.combine(day, w.start_time, tzinfo=tz)
        window_end = datetime.combine(day, w.end_time, tzinfo=tz)
        while cursor + length <= window_end:
            start_utc = cursor.astimezone(UTC)
            end_utc = (cursor + length).astimezone(UTC)
            if start_utc >= earliest:
                taken = any(b0 < end_utc and b1 > start_utc for b0, b1 in busy)
                slots.append(Slot(start_utc, end_utc, not taken))
            cursor += length
    return slots


def find_slot(
    db: Session, doctor: Doctor, start_at: datetime, now: datetime | None = None, exclude_appointment_id: int | None = None
) -> Slot | None:
    start_at = start_at.astimezone(UTC)
    day = start_at.astimezone(get_settings().tz).date()
    for slot in day_slots(db, doctor, day, now, exclude_appointment_id):
        if slot.start_at == start_at:
            return slot
    return None


def next_available(db: Session, doctor: Doctor, now: datetime | None = None, days: int = 14) -> datetime | None:
    now = now or datetime.now(UTC)
    first = today_ist(now)
    for i in range(days):
        for slot in day_slots(db, doctor, first + timedelta(days=i), now):
            if slot.available:
                return slot.start_at
    return None
