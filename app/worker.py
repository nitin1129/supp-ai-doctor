"""Background worker: delivers the outbox and runs timed jobs.

Run with `python -m app.worker` (loops) or call run_once() from cron. Each
entry is claimed atomically (pending → processing), so several workers can run
side by side. Entries stuck in processing for 10 minutes are retried.

Delivery is pluggable: replace `deliver_message` with your email (SES,
SendGrid), SMS and WhatsApp (MSG91, Gupshup) senders. Recipients of the form
"member:<id>" and "doctor:<id>" are resolved to contact details here; member
contacts live in the main SuppAI platform.
"""

import logging
import time
from datetime import datetime, timedelta

from pymongo.database import Database

from app.db import get_db
from app.db import now as db_now
from app.models import AppointmentStatus, status_fields
from app.services import payments, video
from app.services.audit import appointment_event
from app.services.outbox import enqueue

log = logging.getLogger("suppai.worker")
MAX_ATTEMPTS = 5
STUCK_AFTER = timedelta(minutes=10)


def deliver_message(row: dict) -> None:
    """Default sender: logs. Swap in real providers for production."""
    log.info("SEND %s %s → %s %s", row["channel"], row["template"], row["recipient"], row["payload"])


def _job_create_video_room(db: Database, row: dict) -> None:
    appt = db.appointments.find_one({"_id": row["appointment_id"]})
    if appt is None or appt["status"] != AppointmentStatus.BOOKED or appt["mode"] != "video":
        return
    url = appt.get("video_room_url")
    if not url:
        url = video.create_room(str(appt["_id"]))
        db.appointments.update_one({"_id": appt["_id"], "video_room_url": None}, {"$set": {"video_room_url": url}})
        appointment_event(db, appt["_id"], "video_room_created", "system")
    for r in (f"member:{appt['member_id']}", f"doctor:{appt['doctor_id']}"):
        enqueue(db, channel="notify", template="video_link", recipient=r, appointment_id=appt["_id"],
                payload={"url": url})


def _job_check_unmarked(db: Database, row: dict) -> None:
    res = db.appointments.update_one(
        {"_id": row["appointment_id"], "status": AppointmentStatus.BOOKED.value},
        {"$set": status_fields(AppointmentStatus.NEEDS_REVIEW)},
    )
    if res.modified_count:
        appointment_event(db, row["appointment_id"], "needs_review", "system",
                          data={"reason": "doctor did not mark Done or No-show"})
        enqueue(db, channel="email", template="appointment_needs_review", recipient="ops",
                appointment_id=row["appointment_id"])


def _job_refund(db: Database, row: dict) -> None:
    appt = db.appointments.find_one({"_id": row["appointment_id"]})
    if appt is None or not appt.get("razorpay_payment_id"):
        return
    refund_id = payments.provider().refund(
        appt["razorpay_payment_id"], int(row["payload"]["amount_paise"]),
        notes={"appointment_id": str(appt["_id"]), "reason": row["payload"].get("reason", "")},
    )
    db.appointments.update_one({"_id": appt["_id"]}, {"$set": {"razorpay_refund_id": refund_id}})
    appointment_event(db, appt["_id"], "refund_requested", "system", data={**row["payload"], "refund_id": refund_id})


JOBS = {
    "create_video_room": _job_create_video_room,
    "check_unmarked": _job_check_unmarked,
    "refund": _job_refund,
}


def _still_relevant(db: Database, row: dict) -> bool:
    if row["template"] != "appointment_reminder" or row.get("appointment_id") is None:
        return True
    appt = db.appointments.find_one({"_id": row["appointment_id"]}, {"status": 1})
    return appt is not None and appt["status"] == AppointmentStatus.BOOKED


def expire_holds(db: Database, now: datetime) -> int:
    res = db.appointments.update_many(
        {"status": AppointmentStatus.PENDING_PAYMENT.value, "hold_expires_at": {"$lte": now}},
        {"$set": status_fields(AppointmentStatus.EXPIRED)},
    )
    return res.modified_count


def _claim(db: Database, now: datetime) -> dict | None:
    return db.outbox.find_one_and_update(
        {"$or": [
            {"status": "pending", "due_at": {"$lte": now}},
            {"status": "processing", "claimed_at": {"$lt": now - STUCK_AFTER}},
        ]},
        {"$set": {"status": "processing", "claimed_at": now}},
        sort=[("due_at", 1), ("_id", 1)],
    )


def _process(db: Database, row: dict, now: datetime) -> None:
    try:
        if row["channel"] == "job":
            JOBS[row["template"]](db, row)
        elif _still_relevant(db, row):
            deliver_message(row)
        db.outbox.update_one({"_id": row["_id"]}, {"$set": {"status": "sent", "sent_at": now}})
    except Exception as e:  # retry with backoff
        log.exception("Outbox %s failed", row["_id"])
        attempts = row.get("attempts", 0) + 1
        update = {"attempts": attempts, "last_error": str(e)[:2000]}
        if attempts >= MAX_ATTEMPTS:
            update["status"] = "failed"
            if row["template"] == "refund" and row.get("appointment_id"):
                db.appointments.update_one({"_id": row["appointment_id"]}, {"$set": {"refund_status": "failed"}})
        else:
            update.update(status="pending", due_at=now + timedelta(minutes=2 ** attempts))
        db.outbox.update_one({"_id": row["_id"]}, {"$set": update})


def run_once(now: datetime | None = None, batch: int = 100, db: Database | None = None) -> int:
    db = db if db is not None else get_db()
    now = now or db_now()
    expire_holds(db, now)
    processed = 0
    while processed < batch:
        row = _claim(db, now)
        if row is None:
            break
        _process(db, row, now)
        processed += 1
    return processed


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
