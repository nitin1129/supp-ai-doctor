"""Database tables for the SuppAI doctor flow.

Money is stored in paise (integers). Times are stored in UTC (timestamptz) and
converted to IST at the edge.
"""

from datetime import date, datetime, time
from enum import StrEnum

from sqlalchemy import (
    ARRAY,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Sequence,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


# Human-facing application references: SA-DOC-3301, SA-DOC-3302, ...
application_ref_seq = Sequence("doctor_application_ref_seq", start=3301, metadata=Base.metadata)


class ApplicationStatus(StrEnum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    UNDER_REVIEW = "under_review"
    ON_HOLD = "on_hold"
    REJECTED = "rejected"
    ACCEPTED = "accepted"


class DoctorStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class AppointmentStatus(StrEnum):
    PENDING_PAYMENT = "pending_payment"  # slot held while the member pays
    EXPIRED = "expired"  # hold lapsed without payment
    BOOKED = "booked"
    COMPLETED = "completed"
    NO_SHOW = "no_show"
    CANCELLED = "cancelled"
    NEEDS_REVIEW = "needs_review"  # doctor never marked Done/No-show; admin resolves


# Statuses that occupy a slot. Used by the partial unique index and slot engine.
SLOT_HOLDING_STATUSES = (
    AppointmentStatus.PENDING_PAYMENT,
    AppointmentStatus.BOOKED,
    AppointmentStatus.COMPLETED,
    AppointmentStatus.NO_SHOW,
    AppointmentStatus.NEEDS_REVIEW,
)


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _str_enum_check(table: str, column: str, enum: type[StrEnum]) -> CheckConstraint:
    values = ", ".join(f"'{v.value}'" for v in enum)
    return CheckConstraint(f"{column} IN ({values})", name=f"ck_{table}_{column}")


class DoctorApplication(Base):
    __tablename__ = "doctor_applications"
    __table_args__ = (_str_enum_check("doctor_applications", "status", ApplicationStatus),)

    id: Mapped[int] = mapped_column(primary_key=True)
    reference: Mapped[str | None] = mapped_column(String(20), unique=True)
    status: Mapped[str] = mapped_column(String(20), default=ApplicationStatus.DRAFT, index=True)
    resume_token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    current_step: Mapped[int] = mapped_column(Integer, default=1)

    # Non-sensitive answers from the seven steps, keyed by section.
    data: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Denormalised for listing and search.
    full_name: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(254))
    mobile: Mapped[str | None] = mapped_column(String(15))

    # Sensitive identity and bank fields. Aadhaar is never stored in full.
    pan_encrypted: Mapped[str | None] = mapped_column(Text)
    aadhaar_last4: Mapped[str | None] = mapped_column(String(4))
    bank_account_encrypted: Mapped[str | None] = mapped_column(Text)
    bank_account_last4: Mapped[str | None] = mapped_column(String(4))

    review_note: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[str | None] = mapped_column(String(64))
    doctor_id: Mapped[int | None] = mapped_column(ForeignKey("doctors.id"))

    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    documents: Mapped[list["ApplicationDocument"]] = relationship(
        back_populates="application", order_by="ApplicationDocument.id"
    )


class ApplicationDocument(Base):
    __tablename__ = "application_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("doctor_applications.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(50))
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer)
    storage_key: Mapped[str] = mapped_column(String(255), unique=True)
    uploaded_at: Mapped[datetime] = _ts()

    application: Mapped[DoctorApplication] = relationship(back_populates="documents")


class Doctor(Base):
    __tablename__ = "doctors"
    __table_args__ = (
        _str_enum_check("doctors", "status", DoctorStatus),
        CheckConstraint("fee_paise >= 0", name="ck_fee_nonnegative"),
        CheckConstraint("slot_minutes BETWEEN 5 AND 120", name="ck_slot_minutes"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    status: Mapped[str] = mapped_column(String(20), default=DoctorStatus.ACTIVE, index=True)
    status_note: Mapped[str | None] = mapped_column(Text)

    full_name: Mapped[str] = mapped_column(String(200))
    gender: Mapped[str | None] = mapped_column(String(20))
    email: Mapped[str] = mapped_column(String(254))
    mobile: Mapped[str | None] = mapped_column(String(15))

    specialties: Mapped[list[str]] = mapped_column(ARRAY(String(80)), default=list)
    qualifications: Mapped[list[str]] = mapped_column(ARRAY(String(120)), default=list)
    languages: Mapped[list[str]] = mapped_column(ARRAY(String(40)), default=list)
    modes: Mapped[list[str]] = mapped_column(ARRAY(String(10)), default=lambda: ["video"])

    registration_number: Mapped[str | None] = mapped_column(String(60))
    registration_council: Mapped[str | None] = mapped_column(String(120))
    registration_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    experience_years: Mapped[int] = mapped_column(Integer, default=0)

    bio: Mapped[str | None] = mapped_column(Text)
    fee_paise: Mapped[int] = mapped_column(Integer, default=0)
    clinic_address: Mapped[str | None] = mapped_column(Text)
    clinic_pincode: Mapped[str | None] = mapped_column(String(6), index=True)

    slot_minutes: Mapped[int] = mapped_column(Integer, default=15)
    notice_minutes: Mapped[int] = mapped_column(Integer, default=120)

    rating_sum: Mapped[int] = mapped_column(Integer, default=0)
    rating_count: Mapped[int] = mapped_column(Integer, default=0)

    pan_encrypted: Mapped[str | None] = mapped_column(Text)
    aadhaar_last4: Mapped[str | None] = mapped_column(String(4))
    bank_account_encrypted: Mapped[str | None] = mapped_column(Text)
    bank_account_last4: Mapped[str | None] = mapped_column(String(4))
    bank_ifsc: Mapped[str | None] = mapped_column(String(11))

    created_at: Mapped[datetime] = _ts()

    user: Mapped["DoctorUser"] = relationship(back_populates="doctor", uselist=False)
    hours: Mapped[list["DoctorHours"]] = relationship(
        back_populates="doctor", cascade="all, delete-orphan", order_by="DoctorHours.weekday"
    )


class DoctorUser(Base):
    __tablename__ = "doctor_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"), unique=True)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str | None] = mapped_column(Text)
    password_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    token_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _ts()

    doctor: Mapped[Doctor] = relationship(back_populates="user")


class PasswordToken(Base):
    """One-time set-password link sent by email. Only the hash is stored."""

    __tablename__ = "password_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    doctor_user_id: Mapped[int] = mapped_column(ForeignKey("doctor_users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _ts()


class DoctorHours(Base):
    __tablename__ = "doctor_hours"
    __table_args__ = (
        CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_weekday"),
        CheckConstraint("end_time > start_time", name="ck_window_order"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id", ondelete="CASCADE"), index=True)
    weekday: Mapped[int] = mapped_column(Integer)  # 0 = Monday, IST
    start_time: Mapped[time] = mapped_column(Time)  # IST wall clock
    end_time: Mapped[time] = mapped_column(Time)

    doctor: Mapped[Doctor] = relationship(back_populates="hours")


class DoctorLeave(Base):
    __tablename__ = "doctor_leave"
    __table_args__ = (UniqueConstraint("doctor_id", "date", name="uq_leave_doctor_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id", ondelete="CASCADE"), index=True)
    date: Mapped[date] = mapped_column(Date)  # IST calendar date
    note: Mapped[str | None] = mapped_column(String(280))  # shown to patients
    created_at: Mapped[datetime] = _ts()


class Appointment(Base):
    __tablename__ = "appointments"
    __table_args__ = (
        _str_enum_check("appointments", "status", AppointmentStatus),
        CheckConstraint("mode IN ('video', 'clinic')", name="ck_mode"),
        CheckConstraint("end_at > start_at", name="ck_appt_order"),
        # Makes a double booking impossible at the database level.
        Index(
            "uq_appointment_doctor_start_active",
            "doctor_id",
            "start_at",
            unique=True,
            postgresql_where=text(
                "status IN (" + ", ".join(f"'{s.value}'" for s in SLOT_HOLDING_STATUSES) + ")"
            ),
        ),
        Index("ix_appointments_doctor_start", "doctor_id", "start_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"))
    member_id: Mapped[str] = mapped_column(String(64), index=True)
    patient: Mapped[dict] = mapped_column(JSONB)
    mode: Mapped[str] = mapped_column(String(10))
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default=AppointmentStatus.PENDING_PAYMENT, index=True)
    hold_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    fee_paise: Mapped[int] = mapped_column(Integer)
    amount_paid_paise: Mapped[int] = mapped_column(Integer, default=0)
    razorpay_order_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    razorpay_payment_id: Mapped[str | None] = mapped_column(String(64))
    refund_paise: Mapped[int] = mapped_column(Integer, default=0)
    refund_status: Mapped[str | None] = mapped_column(String(20))  # pending / processed / failed
    razorpay_refund_id: Mapped[str | None] = mapped_column(String(64))

    video_room_url: Mapped[str | None] = mapped_column(Text)
    clinic_address: Mapped[str | None] = mapped_column(Text)
    cancelled_by: Mapped[str | None] = mapped_column(String(10))  # member / doctor / admin / system
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    reschedule_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    doctor: Mapped[Doctor] = relationship()
    events: Mapped[list["AppointmentEvent"]] = relationship(
        back_populates="appointment", order_by="AppointmentEvent.id"
    )


class AppointmentEvent(Base):
    __tablename__ = "appointment_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    appointment_id: Mapped[int] = mapped_column(
        ForeignKey("appointments.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[str] = mapped_column(String(40))
    actor_type: Mapped[str] = mapped_column(String(10))
    actor_id: Mapped[str | None] = mapped_column(String(64))
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = _ts()

    appointment: Mapped[Appointment] = relationship(back_populates="events")


class PaymentWebhookEvent(Base):
    """Stores each Razorpay webhook once so retries are idempotent."""

    __tablename__ = "payment_webhook_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_event_id: Mapped[str] = mapped_column(String(100), unique=True)
    event: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict] = mapped_column(JSONB)
    received_at: Mapped[datetime] = _ts()


class Payout(Base):
    __tablename__ = "payouts"

    id: Mapped[int] = mapped_column(primary_key=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"), index=True)
    appointment_id: Mapped[int] = mapped_column(ForeignKey("appointments.id"), unique=True)
    gross_paise: Mapped[int] = mapped_column(Integer)
    commission_paise: Mapped[int] = mapped_column(Integer)
    net_paise: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued / paid / void
    created_at: Mapped[datetime] = _ts()
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (CheckConstraint("rating BETWEEN 1 AND 5", name="ck_rating"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    appointment_id: Mapped[int] = mapped_column(ForeignKey("appointments.id"), unique=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"), index=True)
    member_id: Mapped[str] = mapped_column(String(64))
    rating: Mapped[int] = mapped_column(Integer)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts()


class ProfileChangeRequest(Base):
    """Fee or specialty changes wait here for admin approval."""

    __tablename__ = "profile_change_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"), index=True)
    changes: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending/approved/rejected
    note: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[str | None] = mapped_column(String(64))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _ts()


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    actor_type: Mapped[str] = mapped_column(String(10))
    actor_id: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(60), index=True)
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str] = mapped_column(String(40))
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = _ts()


class Outbox(Base):
    """Notifications and timed jobs, processed by app.worker."""

    __tablename__ = "outbox"
    __table_args__ = (Index("ix_outbox_due", "status", "due_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    channel: Mapped[str] = mapped_column(String(10))  # email / sms / whatsapp / job
    template: Mapped[str] = mapped_column(String(60))
    recipient: Mapped[str | None] = mapped_column(String(254))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    appointment_id: Mapped[int | None] = mapped_column(
        ForeignKey("appointments.id", ondelete="CASCADE"), index=True
    )
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    status: Mapped[str] = mapped_column(String(10), default="pending")  # pending/sent/failed/cancelled
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _ts()
