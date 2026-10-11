"""Background worker: delivers the outbox and runs timed jobs.

Run with `python -m app.worker` (loops) or call run_once() from a cron/Celery
beat. Rows are claimed with SKIP LOCKED, so several workers can run safely.

Delivery is pluggable: replace `deliver_message` with your email (SES,
SendGrid), SMS and WhatsApp (MSG91, Gupshup) senders. Recipients of the form
"member:<id>" and "doctor:<id>" are resolved to contact details here; member
contacts live in the main SuppAI platform.
"""

import logging
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import Appointment, AppointmentStatus, Outbox
from app.services import payments, video
from app.services.audit import appointment_event
from app.services.outbox import enqueue

log = logging.getLogger("suppai.worker")
MAX_ATTEMPTS = 5


def deliver_message(row: Outbox) -> None:
    """Default sender: logs. Swap in real providers for production."""
    log.info("SEND %s %s → %s %s", row.channel, row.template, row.recipient, row.payload)


def _job_create_video_room(db: Session, row: Outbox) -> None:
    appt = db.get(Appointment, row.appointment_id)
    if appt is None or appt.status != AppointmentStatus.BOOKED or appt.mode != "video":
        return
    if not appt.video_room_url:
        appt.video_room_url = video.create_room(appt.id)
        appointment_event(db, appt.id, "video_room_created", "system")
    for r in (f"member:{appt.member_id}", f"doctor:{appt.doctor_id}"):
        enqueue(db, channel="notify", template="video_link", recipient=r, appointment_id=appt.id,
                payload={"url": appt.video_room_url})


def _job_check_unmarked(db: Session, row: Outbox) -> None:
    appt = db.get(Appointment, row.appointment_id)
    if appt is None or appt.status != AppointmentStatus.BOOKED:
        return
    appt.status = AppointmentStatus.NEEDS_REVIEW
    appointment_event(db, appt.id, "needs_review", "system", data={"reason": "doctor did not mark Done or No-show"})
    enqueue(db, channel="email", template="appointment_needs_review", recipient="ops", appointment_id=appt.id)


def _job_refund(db: Session, row: Outbox) -> None:
    appt = db.get(Appointment, row.appointment_id)
    if appt is None or not appt.razorpay_payment_id:
        return
    refund_id = payments.provider().refund(
        appt.razorpay_payment_id, int(row.payload["amount_paise"]),
        notes={"appointment_id": str(appt.id), "reason": row.payload.get("reason", "")},
    )
    appt.razorpay_refund_id = refund_id
    appointment_event(db, appt.id, "refund_requested", "system", data={**row.payload, "refund_id": refund_id})


JOBS = {
    "create_video_room": _job_create_video_room,
    "check_unmarked": _job_check_unmarked,
    "refund": _job_refund,
}


def _still_relevant(db: Session, row: Outbox) -> bool:
    if row.template != "appointment_reminder" or row.appointment_id is None:
        return True
    appt = db.get(Appointment, row.appointment_id)
    return appt is not None and appt.status == AppointmentStatus.BOOKED


def expire_holds(db: Session, now: datetime) -> int:
    res = db.execute(
        update(Appointment)
        .where(Appointment.status == AppointmentStatus.PENDING_PAYMENT, Appointment.hold_expires_at <= now)
        .values(status=AppointmentStatus.EXPIRED)
    )
    return res.rowcount


def run_once(now: datetime | None = None, batch: int = 100, db: Session | None = None) -> int:
    now = now or datetime.now(UTC)
    own = db is None
    db = db or SessionLocal()
    processed = 0
    try:
        expire_holds(db, now)
        db.commit()
        rows = db.scalars(
            select(Outbox)
            .where(Outbox.status == "pending", Outbox.due_at <= now)
            .order_by(Outbox.due_at, Outbox.id)
            .limit(batch)
            .with_for_update(skip_locked=True)
        ).all()
        for row in rows:
            try:
                with db.begin_nested():
                    if row.channel == "job":
                        JOBS[row.template](db, row)
                    elif _still_relevant(db, row):
                        deliver_message(row)
                row.status, row.sent_at = "sent", now
            except Exception as e:  # retry with backoff
                log.exception("Outbox %s failed", row.id)
                row.attempts += 1
                row.last_error = str(e)[:2000]
                if row.attempts >= MAX_ATTEMPTS:
                    row.status = "failed"
                    if row.template == "refund" and row.appointment_id:
                        appt = db.get(Appointment, row.appointment_id)
                        if appt:
                            appt.refund_status = "failed"
                else:
                    row.due_at = now + timedelta(minutes=2 ** row.attempts)
            processed += 1
        db.commit()
        return processed
    finally:
        if own:
            db.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("Worker started")
    while True:
        try:
            if run_once() == 0:
                time.sleep(5)
        except Exception:
            log.exception("Worker loop error")
            time.sleep(10)


if __name__ == "__main__":
    main()
