"""MongoDB collections for the SuppAI doctor flow.

Money is stored in paise (integers). Times are stored as UTC datetimes and
converted to IST at the edge. Weekly hours are IST wall-clock minutes.

Collections
-----------
doctor_applications  one per application; the seven steps live in `data`,
                     uploaded files in `documents` (embedded)
doctors              approved doctors; weekly hours embedded in `hours`
doctor_users         login for each doctor
password_tokens      one-time set-password links (hash only)
doctor_leave         days off, one per (doctor_id, date)
appointments         bookings; timeline embedded in `events`, doctor snapshot
                     in `doctor`
payment_webhook_events, payouts, reviews, profile_change_requests,
audit_log, outbox, counters
"""

from enum import StrEnum

from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.database import Database


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


# Statuses that occupy a slot. `holds_slot` is kept in step with these.
SLOT_HOLDING_STATUSES = {
    AppointmentStatus.PENDING_PAYMENT,
    AppointmentStatus.BOOKED,
    AppointmentStatus.COMPLETED,
    AppointmentStatus.NO_SHOW,
    AppointmentStatus.NEEDS_REVIEW,
}


def status_fields(status: AppointmentStatus) -> dict:
    """Use this whenever an appointment's status changes."""
    return {"status": status.value, "holds_slot": status in SLOT_HOLDING_STATUSES}


INDEXES: dict[str, list[IndexModel]] = {
    "doctor_applications": [
        IndexModel("resume_token_hash", unique=True),
        IndexModel("reference", unique=True, partialFilterExpression={"reference": {"$type": "string"}}),
        IndexModel([("status", ASCENDING), ("submitted_at", DESCENDING)]),
    ],
    "doctors": [
        IndexModel("status"),
        IndexModel("specialties"),
        IndexModel("clinic_pincode"),
    ],
    "doctor_users": [
        IndexModel("doctor_id", unique=True),
        IndexModel("username", unique=True),
        IndexModel("email", unique=True),
    ],
    "password_tokens": [IndexModel("token_hash", unique=True), IndexModel("doctor_user_id")],
    "doctor_leave": [IndexModel([("doctor_id", ASCENDING), ("date", ASCENDING)], unique=True)],
    "appointments": [
        # Makes a double booking impossible at the database level.
        IndexModel(
            [("doctor_id", ASCENDING), ("start_at", ASCENDING)],
            unique=True,
            partialFilterExpression={"holds_slot": True},
            name="uq_doctor_start_active",
        ),
        IndexModel([("doctor_id", ASCENDING), ("status", ASCENDING), ("start_at", ASCENDING)], name="doctor_status_start"),
        IndexModel([("member_id", ASCENDING), ("start_at", DESCENDING)]),
        IndexModel("razorpay_order_id", unique=True, partialFilterExpression={"razorpay_order_id": {"$type": "string"}}),
        IndexModel("razorpay_payment_id"),
        IndexModel("status"),
    ],
    "payment_webhook_events": [IndexModel("provider_event_id", unique=True)],
    "payouts": [IndexModel("appointment_id", unique=True), IndexModel("doctor_id")],
    "reviews": [IndexModel("appointment_id", unique=True), IndexModel("doctor_id")],
    "profile_change_requests": [IndexModel([("status", ASCENDING), ("created_at", ASCENDING)]), IndexModel("doctor_id")],
    "audit_log": [IndexModel([("entity_type", ASCENDING), ("entity_id", ASCENDING)]), IndexModel("action")],
    "outbox": [IndexModel([("status", ASCENDING), ("due_at", ASCENDING)]), IndexModel("appointment_id")],
}


def ensure_indexes(db: Database) -> None:
    # One create_index call per index (rather than create_indexes) so the
    # in-memory test database also keeps partialFilterExpression.
    for name, indexes in INDEXES.items():
        for idx in indexes:
            spec = dict(idx.document)
            keys = list(spec.pop("key").items())
            db[name].create_index(keys, **spec)
    from app.services.otp import ensure_indexes as _otp_indexes
    _otp_indexes(db)


if __name__ == "__main__":
    from app.db import get_db

    ensure_indexes(get_db())
    print("Indexes are in place.")
