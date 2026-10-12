"""Background worker: delivers the outbox and runs timed jobs.

Run with `python -m app.worker` (loops) or call run_once() from cron. It also
sends queued doctor payouts (see app.services.payouts). Each outbox
entry is claimed atomically (pending → processing), so several workers can run
side by side. Entries stuck in processing for 10 minutes are retried.

Delivery is pluggable: replace `deliver_message` with your email (SES,
SendGrid), SMS and WhatsApp (MSG91, Gupshup) senders. Recipients of the form
"member:<id>" are resolved to contact details through app.services.contacts;
member contacts live in the main SuppAI platform. "doctor:<id>" recipients are
not resolved yet.
"""

import logging
import time
from datetime import datetime, timedelta

from pymongo import ReturnDocument
from pymongo.database import Database

from app.db import get_db
from app.db import now as db_now
from app.models import AppointmentStatus, status_fields
from app.security import decrypt, mask
from app.services import contacts, payments, payouts, video
from app.services.audit import appointment_event
from app.services.outbox import enqueue

log = logging.getLogger("suppai.worker")
MAX_ATTEMPTS = 5
STUCK_AFTER = timedelta(minutes=10)


def send(row: dict, contact: contacts.Contact | None) -> None:
    """Default sender: logs. Swap in real email/SMS/WhatsApp providers for production."""
    to = row["recipient"]
    if contact is not None:
        to = f"{to} (email={mask(contact.email)} phone={mask(contact.phone)})"
    log.info("SEND %s %s → %s %s", row["channel"], row["template"], to, row["payload"])


def deliver_message(row: dict) -> None:
    """Resolves "member:<id>" recipients to contact details, then sends.

    An unknown member raises ContactNotFound, so the row is retried with
    backoff and marked failed after MAX_ATTEMPTS.
    """
    contact = None
    if contacts.is_member_recipient(row.get("recipient")):
        contact = contacts.resolve_recipient(row["recipient"])
    send(row, contact)


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
    """Sends one queued refund to the payments provider.

    The receipt is derived from the outbox row, so a retry (after a crash, a
    lost response or a failed write below) finds the first refund instead of
    making a second one. An appointment can have several refunds; each is
    recorded once in `refunds`.
    """
    appt = db.appointments.find_one({"_id": row["appointment_id"]})
    if appt is None or not appt.get("razorpay_payment_id"):
        return
    receipt = f"rfnd-{row['_id']}"
    if any(r.get("receipt") == receipt for r in appt.get("refunds", [])):
        return  # already requested; the row is being retried after a later step failed
    amount = int(row["payload"]["amount_paise"])
    refund_id = payments.provider().refund(
        appt["razorpay_payment_id"], amount,
        notes={"appointment_id": str(appt["_id"]), "reason": row["payload"].get("reason", "")},
        receipt=receipt,
    )
    res = db.appointments.update_one(
        {"_id": appt["_id"], "refunds.receipt": {"$ne": receipt}},
        {"$set": {"razorpay_refund_id": refund_id},
         "$push": {"refunds": {"receipt": receipt, "refund_id": refund_id, "amount_paise": amount,
                               "reason": row["payload"].get("reason", ""), "requested_at": db_now()}}},
    )
    if res.modified_count:
        appointment_event(db, appt["_id"], "refund_requested", "system",
                          data={**row["payload"], "refund_id": refund_id})


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


# --- Doctor payouts -------------------------------------------------------------

def _beneficiary(db: Database, payout: dict) -> payouts.Beneficiary:
    doctor = db.doctors.find_one({"_id": payout["doctor_id"]})
    if doctor is None:
        raise payouts.PayoutRejected("Doctor not found")
    if not (doctor.get("bank_account_encrypted") and doctor.get("bank_ifsc")):
        raise payouts.PayoutRejected("Doctor has no bank account on file")
    app = db.doctor_applications.find_one({"_id": doctor.get("application_id")}, {"data.bank": 1}) or {}
    holder = ((app.get("data") or {}).get("bank") or {}).get("account_holder") or doctor["full_name"]
    return payouts.Beneficiary(
        doctor_id=str(doctor["_id"]), name=holder, email=doctor.get("email"), phone=doctor.get("mobile"),
        account_number=decrypt(doctor["bank_account_encrypted"]), ifsc=doctor["bank_ifsc"],
    )


def _claim_payout(db: Database, now: datetime) -> dict | None:
    row = db.payouts.find_one_and_update(
        {"$or": [
            {"status": "queued", "$or": [{"next_attempt_at": None}, {"next_attempt_at": {"$lte": now}}]},
            {"status": "sending", "claimed_at": {"$lt": now - STUCK_AFTER}},
        ]},
        {"$set": {"status": "sending", "claimed_at": now}},
        sort=[("created_at", 1), ("_id", 1)],
        return_document=ReturnDocument.AFTER,
    )
    if row is not None and not row.get("idempotency_key"):
        # Fixed before the first call and reused by every retry of this payout.
        db.payouts.update_one({"_id": row["_id"], "idempotency_key": None},
                              {"$set": {"idempotency_key": payouts.new_idempotency_key()}})
        row = db.payouts.find_one({"_id": row["_id"]})
    return row


def _process_payout(db: Database, row: dict, now: datetime) -> None:
    def finish(update: dict) -> None:
        db.payouts.update_one({"_id": row["_id"], "status": "sending"}, {"$set": update})

    try:
        if row["net_paise"] <= 0:
            raise payouts.PayoutRejected("Nothing to pay")
        result = payouts.provider().transfer(
            beneficiary=_beneficiary(db, row), amount_paise=row["net_paise"], reference_id=str(row["_id"]),
            idempotency_key=row["idempotency_key"],
            notes={"appointment_id": str(row["appointment_id"]), "doctor_id": str(row["doctor_id"])},
        )
    except payouts.PayoutRejected as e:
        log.warning("Payout %s rejected: %s", row["_id"], e)
        finish({"status": "failed", "failed_at": now, "last_error": str(e)[:2000]})
    except Exception as e:  # unknown outcome; the idempotency key makes the retry safe
        log.exception("Payout %s failed", row["_id"])
        attempts = row.get("attempts", 0) + 1
        update = {"attempts": attempts, "last_error": str(e)[:2000]}
        if attempts >= MAX_ATTEMPTS:
            update.update(status="failed", failed_at=now)
        else:
            update.update(status="queued", next_attempt_at=now + timedelta(minutes=2 ** attempts))
        finish(update)
    else:
        finish({"status": "sent", "sent_at": now, "provider_payout_id": result.provider_payout_id,
                "provider_status": result.provider_status, "last_error": None})


def process_payouts(db: Database, now: datetime, batch: int = 100) -> int:
    processed = 0
    while processed < batch:
        row = _claim_payout(db, now)
        if row is None:
            break
        _process_payout(db, row, now)
        processed += 1
    return processed


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
    paid_out = process_payouts(db, now, batch)
    processed = 0
    while processed < batch:
        row = _claim(db, now)
        if row is None:
            break
        _process(db, row, now)
        processed += 1
    return processed + paid_out


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
