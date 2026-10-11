"""Slot engine.

Slots are built from the doctor's weekly hours (IST wall-clock windows), slot
length and booking notice. Booked or held slots and days off are removed.
Everything returned is in UTC; the API layer adds the IST label.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from bson import ObjectId
from pymongo.database import Database

from app.config import get_settings
from app.db import now as db_now
from app.models import AppointmentStatus


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
    return (now or db_now()).astimezone(get_settings().tz).date()


def active_filter(now: datetime) -> dict:
    """Appointments that occupy their slot right now (lapsed holds do not)."""
    return {
        "holds_slot": True,
        "$or": [
            {"status": {"$ne": AppointmentStatus.PENDING_PAYMENT.value}},
            {"hold_expires_at": {"$gt": now}},
        ],
    }


def busy_intervals(db: Database, doctor_id: ObjectId, start: datetime, end: datetime, now: datetime,
                   exclude_id: ObjectId | None = None) -> list[tuple[datetime, datetime]]:
    q = {"doctor_id": doctor_id, "start_at": {"$lt": end}, "end_at": {"$gt": start}, **active_filter(now)}
    if exclude_id is not None:
        q["_id"] = {"$ne": exclude_id}
    return [(a["start_at"], a["end_at"]) for a in db.appointments.find(q, {"start_at": 1, "end_at": 1})]


def leave_on(db: Database, doctor_id: ObjectId, day: date) -> dict | None:
    return db.doctor_leave.find_one({"doctor_id": doctor_id, "date": day.isoformat()})


def _minutes_to_time(m: int) -> time:
    return time(m // 60, m % 60)


def day_slots(db: Database, doctor: dict, day: date, now: datetime | None = None,
              exclude_appointment_id: ObjectId | None = None) -> list[Slot]:
    """All slots for one IST day, each marked available or not.

    Booked slots are returned with available=False so the UI can cross them out.
    Slots inside the notice period or outside the booking window are dropped.
    """
    s = get_settings()
    tz = s.tz
    now = now or db_now()
    if day < today_ist(now) or day > today_ist(now) + timedelta(days=s.booking_window_days):
        return []
    windows = sorted((w for w in doctor.get("hours", []) if w["weekday"] == day.weekday()),
                     key=lambda w: w["start_min"])
    if not windows or leave_on(db, doctor["_id"], day):
        return []

    earliest = now + timedelta(minutes=doctor["notice_minutes"])
    length = timedelta(minutes=doctor["slot_minutes"])
    day_start, day_end = ist_day_bounds(day)
    busy = busy_intervals(db, doctor["_id"], day_start, day_end, now, exclude_appointment_id)

    slots: list[Slot] = []
    for w in windows:
        cursor = datetime.combine(day, _minutes_to_time(w["start_min"]), tzinfo=tz)
        window_end = datetime.combine(day, time.min, tzinfo=tz) + timedelta(minutes=w["end_min"])
        while cursor + length <= window_end:
            start_utc = cursor.astimezone(UTC)
            end_utc = (cursor + length).astimezone(UTC)
            if start_utc >= earliest:
                taken = any(b0 < end_utc and b1 > start_utc for b0, b1 in busy)
                slots.append(Slot(start_utc, end_utc, not taken))
            cursor += length
    return slots


def find_slot(db: Database, doctor: dict, start_at: datetime, now: datetime | None = None,
              exclude_appointment_id: ObjectId | None = None) -> Slot | None:
    start_at = start_at.astimezone(UTC)
    day = start_at.astimezone(get_settings().tz).date()
    for slot in day_slots(db, doctor, day, now, exclude_appointment_id):
        if slot.start_at == start_at:
            return slot
    return None


def next_available(db: Database, doctor: dict, now: datetime | None = None, days: int = 14) -> datetime | None:
    now = now or db_now()
    first = today_ist(now)
    for i in range(days):
        for slot in day_slots(db, doctor, first + timedelta(days=i), now):
            if slot.available:
                return slot.start_at
    return None
