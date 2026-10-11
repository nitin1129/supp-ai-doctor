from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.config import get_settings


def ist_label(dt: datetime) -> str:
    return dt.astimezone(get_settings().tz).strftime("%a %d %b %Y, %I:%M %p IST")


# --- Doctor settings ----------------------------------------------------------

class HoursWindow(BaseModel):
    weekday: int = Field(ge=0, le=6, description="0 = Monday")
    start: time
    end: time

    @model_validator(mode="after")
    def _order(self):
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class HoursIn(BaseModel):
    windows: list[HoursWindow]
    slot_minutes: int | None = Field(default=None, ge=5, le=120)
    notice_minutes: int | None = Field(default=None, ge=0, le=7 * 24 * 60)

    @model_validator(mode="after")
    def _no_overlap(self):
        by_day: dict[int, list[HoursWindow]] = {}
        for w in self.windows:
            by_day.setdefault(w.weekday, []).append(w)
        for day, ws in by_day.items():
            ws.sort(key=lambda w: w.start)
            for a, b in zip(ws, ws[1:]):
                if b.start < a.end:
                    raise ValueError(f"Windows overlap on weekday {day}")
        return self


class HoursOut(HoursIn):
    pass


class LeaveIn(BaseModel):
    date: date
    note: str | None = Field(default=None, max_length=280, description="Shown to patients")
    cancel_booked: bool = Field(
        default=False,
        description="If appointments are already booked that day, cancel them with a full refund.",
    )


class LeaveOut(BaseModel):
    id: str
    date: date
    note: str | None


class OutcomeIn(BaseModel):
    status: Literal["completed", "no_show"]


class ProfileOut(BaseModel):
    id: str
    full_name: str
    email: str
    mobile: str | None
    specialties: list[str]
    qualifications: list[str]
    languages: list[str]
    modes: list[str]
    bio: str | None
    fee_inr: float
    experience_years: int
    clinic_address: str | None
    clinic_pincode: str | None
    registration_number: str | None
    registration_council: str | None
    rating: float | None
    rating_count: int
    pending_changes: list[dict] = []


class ProfileIn(BaseModel):
    """bio, languages, modes and clinic details apply immediately.
    fee_inr and specialties go to admin for approval."""

    bio: str | None = Field(default=None, max_length=2000)
    languages: list[str] | None = None
    modes: list[Literal["video", "clinic"]] | None = Field(default=None, min_length=1)
    clinic_address: str | None = None
    clinic_pincode: str | None = Field(default=None, pattern=r"^[1-9]\d{5}$")
    fee_inr: int | None = Field(default=None, ge=0, le=100000)
    specialties: list[str] | None = Field(default=None, min_length=1)


# --- Appointments ------------------------------------------------------------

class Patient(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    age: int = Field(ge=0, le=120)
    gender: Literal["male", "female", "other"]
    relation: Literal["self", "spouse", "child", "parent", "sibling", "other"] = "self"
    concern: str | None = Field(default=None, max_length=1000)


class BookIn(BaseModel):
    doctor_id: str
    start_at: datetime = Field(description="Slot start exactly as returned by /doctors/{id}/slots")
    mode: Literal["video", "clinic"]
    patient: Patient

    @field_validator("start_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("start_at must include a timezone, e.g. 2026-10-20T04:30:00Z")
        return v


class RescheduleIn(BaseModel):
    start_at: datetime

    @field_validator("start_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("start_at must include a timezone")
        return v


class CancelIn(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class ReviewIn(BaseModel):
    rating: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=2000)


class DoctorBrief(BaseModel):
    id: str
    full_name: str
    specialties: list[str]


class AppointmentOut(BaseModel):
    id: str
    doctor: DoctorBrief
    patient: dict
    mode: str
    start_at: datetime
    end_at: datetime
    start_ist: str
    status: str  # stored status (doctor dashboard)
    member_status: str  # what the member's tracker shows
    fee_inr: float
    amount_paid_inr: float
    refund_inr: float
    refund_status: str | None
    razorpay_order_id: str | None
    hold_expires_at: datetime | None
    video_room_url: str | None
    clinic_address: str | None
    can_reschedule: bool
    cancel_refund_percent: int | None


class BookingOut(AppointmentOut):
    razorpay_key_id: str
    amount_paise: int
    currency: str = "INR"


class SlotOut(BaseModel):
    start_at: datetime
    end_at: datetime
    start_ist: str
    available: bool


class DaySlotsOut(BaseModel):
    doctor_id: str
    date: date
    day_off: bool
    day_off_note: str | None
    slots: list[SlotOut]


class DoctorCard(BaseModel):
    id: str
    full_name: str
    specialties: list[str]
    qualifications: list[str]
    languages: list[str]
    modes: list[str]
    experience_years: int
    fee_inr: float
    rating: float | None
    rating_count: int
    clinic_pincode: str | None
    next_available_at: datetime | None
    next_available_ist: str | None
