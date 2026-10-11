"""Appointment lifecycle: hold → paid → done / no-show / cancelled.

One appointment document is read by both the doctor's dashboard and the
member's tracker. member_view_status() maps the stored status to what the
member sees.

Every status change is a conditional update on the current status, so two
racing requests can't both move the same appointment.
"""

import logging
from datetime import datetime, timedelta

from bson import ObjectId
from fastapi import HTTPException
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from app.config import get_settings
from app.db import now as db_now
from app.models import AppointmentStatus, DoctorStatus, status_fields
from app.services import payments
from app.services.audit import event
from app.services.locks import doctor_lock
from app.services.outbox import cancel_pending_for_appointment, enqueue
from app.services.slots import busy_intervals, find_slot

log = logging.getLogger(__name__)

S = AppointmentStatus
TIMED_TEMPLATES = ["appointment_reminder", "create_video_room", "check_unmarked"]


def member_recipient(member_id: str) -> str:
    """Member contact details live in the main SuppAI platform; the worker resolves them."""
    return f"member:{member_id}"


def doctor_recipient(doctor_id: ObjectId) -> str:
    return f"doctor:{doctor_id}"


def member_view_status(appt: dict, now: datetime | None = None) -> str:
    now = now or db_now()
    lead = timedelta(minutes=get_settings().video_room_lead_minutes)
    match appt["status"]:
        case S.PENDING_PAYMENT:
            exp = appt.get("hold_expires_at")
            return "awaiting_payment" if exp and exp > now else "expired"
        case S.BOOKED:
            return "in_progress" if appt["start_at"] - lead <= now < appt["end_at"] else "upcoming"
        case S.NEEDS_REVIEW:
            return "completed"  # pending admin confirmation; member is not shown the dispute state
        case _:
            return appt["status"]


def transition(db: Database, appt_id: ObjectId, from_statuses: list[S], update: dict,
               extra_filter: dict | None = None) -> dict:
    """Atomically moves an appointment out of one of `from_statuses`. Returns the document *before*."""
    q = {"_id": appt_id, "status": {"$in": [s.value for s in from_statuses]}, **(extra_filter or {})}
    update = {**update, "$set": {**update.get("$set", {}), "updated_at": db_now()}}
    before = db.appointments.find_one_and_update(q, update)
    if before is None:
        current = db.appointments.find_one({"_id": appt_id}, {"status": 1})
        if current is None:
            raise HTTPException(404, "Appointment not found")
        raise HTTPException(409, f"Appointment is {current['status']}")
    return before


def get(db: Database, appt_id: ObjectId) -> dict:
    return db.appointments.find_one({"_id": appt_id})


def _expire_stale_holds(db: Database, doctor_id: ObjectId, start: datetime, end: datetime, now: datetime) -> None:
    db.appointments.update_many(
        {"doctor_id": doctor_id, "status": S.PENDING_PAYMENT.value, "hold_expires_at": {"$lte": now},
         "start_at": {"$lt": end}, "end_at": {"$gt": start}},
        {"$set": status_fields(S.EXPIRED)},
    )


# --- Booking ----------------------------------------------------------------

def create_hold(db: Database, *, doctor_id: ObjectId, member_id: str, start_at: datetime, mode: str,
                patient: dict) -> dict:
    s = get_settings()
    now = db_now()
    with doctor_lock(db, doctor_id) as doctor:
        if doctor["status"] != DoctorStatus.ACTIVE:
            raise HTTPException(409, "This doctor is not taking bookings")
        if mode not in doctor["modes"]:
            raise HTTPException(422, f"This doctor does not offer {mode} consults")
        slot = find_slot(db, doctor, start_at, now)
        if slot is None:
            raise HTTPException(422, "That time is not one of the doctor's slots")
        if not slot.available:
            raise HTTPException(409, "That slot was just taken. Pick another.")

        _expire_stale_holds(db, doctor["_id"], slot.start_at, slot.end_at, now)
        appt = {
            "doctor_id": doctor["_id"],
            "doctor": {"id": doctor["_id"], "full_name": doctor["full_name"], "specialties": doctor["specialties"]},
            "member_id": member_id,
            "patient": patient,
            "mode": mode,
            "start_at": slot.start_at,
            "end_at": slot.end_at,
            **status_fields(S.PENDING_PAYMENT),
            "hold_expires_at": now + timedelta(minutes=s.slot_hold_minutes),
            "fee_paise": doctor["fee_paise"],
            "amount_paid_paise": 0,
            "razorpay_order_id": None,
            "razorpay_payment_id": None,
            "refund_paise": 0,
            "refund_status": None,
            "razorpay_refund_id": None,
            "video_room_url": None,
            "clinic_address": doctor.get("clinic_address") if mode == "clinic" else None,
            "cancelled_by": None,
            "cancel_reason": None,
            "reschedule_count": 0,
            "events": [event("held", "member", member_id)],
            "created_at": now,
            "updated_at": now,
        }
        try:
            appt["_id"] = db.appointments.insert_one(appt).inserted_id
        except DuplicateKeyError:
            raise HTTPException(409, "That slot was just taken. Pick another.")

    try:
        order_id = payments.provider().create_order(
            appt["fee_paise"], receipt=f"appt-{appt['_id']}", notes={"appointment_id": str(appt["_id"])}
        )
    except Exception:
        log.exception("Razorpay order failed for appointment %s", appt["_id"])
        db.appointments.update_one({"_id": appt["_id"]}, {"$set": status_fields(S.EXPIRED)})
        raise HTTPException(502, "Could not start the payment. Please try again.")
    db.appointments.update_one({"_id": appt["_id"]}, {"$set": {"razorpay_order_id": order_id}})
    appt["razorpay_order_id"] = order_id
    return appt


def schedule_jobs(db: Database, appt: dict) -> None:
    s = get_settings()
    now = db_now()
    for minutes in s.reminder_offsets_minutes:
        due = appt["start_at"] - timedelta(minutes=minutes)
        if due > now:
            for r in (member_recipient(appt["member_id"]), doctor_recipient(appt["doctor_id"])):
                enqueue(db, channel="notify", template="appointment_reminder", recipient=r,
                        payload={"minutes_before": minutes}, due_at=due, appointment_id=appt["_id"])
    if appt["mode"] == "video":
        enqueue(db, channel="job", template="create_video_room",
                due_at=max(now, appt["start_at"] - timedelta(minutes=s.video_room_lead_minutes)),
                appointment_id=appt["_id"])
    enqueue(db, channel="job", template="check_unmarked",
            due_at=appt["end_at"] + timedelta(hours=s.unmarked_review_after_hours), appointment_id=appt["_id"])


def _queue_refund(db: Database, appt_id: ObjectId, amount_paise: int, reason: str) -> int:
    """Reserves the refund on the appointment (never more than was paid) and queues it."""
    appt = get(db, appt_id)
    amount = min(amount_paise, appt["amount_paid_paise"] - appt["refund_paise"])
    if amount <= 0 or not appt.get("razorpay_payment_id"):
        return 0
    res = db.appointments.update_one(
        {"_id": appt_id, "refund_paise": appt["refund_paise"]},
        {"$inc": {"refund_paise": amount}, "$set": {"refund_status": "pending"}},
    )
    if res.modified_count == 0:  # a concurrent refund got there first; recompute
        return _queue_refund(db, appt_id, amount_paise, reason)
    enqueue(db, channel="job", template="refund", payload={"amount_paise": amount, "reason": reason},
            appointment_id=appt_id)
    return amount


def mark_paid(db: Database, *, order_id: str, payment_id: str, amount_paise: int) -> dict | None:
    """Called from the Razorpay webhook. Safe to call more than once."""
    appt = db.appointments.find_one({"razorpay_order_id": order_id})
    if appt is None:
        return None
    if appt.get("razorpay_payment_id") == payment_id and appt["status"] != S.PENDING_PAYMENT:
        return appt  # duplicate delivery

    paid = {"razorpay_payment_id": payment_id, "amount_paid_paise": amount_paise}

    if appt["status"] in (S.PENDING_PAYMENT, S.EXPIRED):
        outcome = None  # "booked" | "refund" | "retry"
        with doctor_lock(db, appt["doctor_id"]):
            now = db_now()
            clash = busy_intervals(db, appt["doctor_id"], appt["start_at"], appt["end_at"], now, exclude_id=appt["_id"])
            if not clash and appt["start_at"] > now:
                try:
                    transition(db, appt["_id"], [S.PENDING_PAYMENT, S.EXPIRED], {
                        "$set": {**status_fields(S.BOOKED), "hold_expires_at": None, **paid},
                        "$push": {"events": event("paid", "system", data={"payment_id": payment_id})},
                    })
                    outcome = "booked"
                except DuplicateKeyError:  # someone holds the exact start; refund below
                    pass
                except HTTPException:  # status changed under us (e.g. cancelled)
                    outcome = "retry"
            if outcome is None:
                # Paid after the hold lapsed and the slot is gone (or already past): refund in full.
                try:
                    transition(db, appt["_id"], [S.PENDING_PAYMENT, S.EXPIRED], {"$set": {
                        **status_fields(S.CANCELLED), **paid, "cancelled_by": "system",
                        "cancel_reason": "Payment arrived after the slot was released"},
                        "$push": {"events": event("late_payment_refunded", "system",
                                                  data={"payment_id": payment_id})}})
                    outcome = "refund"
                except HTTPException:
                    outcome = "retry"

        if outcome == "retry":
            return mark_paid(db, order_id=order_id, payment_id=payment_id, amount_paise=amount_paise)
        if outcome == "booked":
            booked = get(db, appt["_id"])
            enqueue(db, channel="notify", template="appointment_booked",
                    recipient=doctor_recipient(booked["doctor_id"]), appointment_id=booked["_id"])
            enqueue(db, channel="notify", template="appointment_confirmed",
                    recipient=member_recipient(booked["member_id"]), appointment_id=booked["_id"])
            schedule_jobs(db, booked)
            return booked
        _queue_refund(db, appt["_id"], amount_paise, "late_payment")
        enqueue(db, channel="notify", template="late_payment_refunded",
                recipient=member_recipient(appt["member_id"]), appointment_id=appt["_id"])
        return get(db, appt["_id"])

    if appt["status"] == S.CANCELLED:
        # Cancelled while the member was paying: record the payment once and refund it.
        res = db.appointments.update_one({"_id": appt["_id"], "razorpay_payment_id": None}, {"$set": paid})
        if res.modified_count:
            _queue_refund(db, appt["_id"], amount_paise, "paid_after_cancel")
    return get(db, appt["_id"])


# --- Changes after booking --------------------------------------------------

def cancel(db: Database, appt: dict, *, by: str, actor_id: str | None, reason: str | None,
           refund_percent: int) -> dict:
    before = transition(db, appt["_id"], [S.BOOKED, S.PENDING_PAYMENT, S.NEEDS_REVIEW], {
        # A late payment for a cancelled hold is refunded in mark_paid.
        "$set": {**status_fields(S.CANCELLED), "cancelled_by": by, "cancel_reason": reason},
        "$push": {"events": event("cancelled", by, actor_id, {"reason": reason, "refund_percent": refund_percent})},
    })
    cancel_pending_for_appointment(db, appt["_id"], TIMED_TEMPLATES)
    if before["status"] != S.PENDING_PAYMENT:
        refund = _queue_refund(db, appt["_id"], before["amount_paid_paise"] * refund_percent // 100, f"cancelled_by_{by}")
        for r in (member_recipient(before["member_id"]), doctor_recipient(before["doctor_id"])):
            enqueue(db, channel="notify", template="appointment_cancelled", recipient=r, appointment_id=appt["_id"],
                    payload={"by": by, "reason": reason, "refund_paise": refund})
    return get(db, appt["_id"])


def member_cancel_refund_percent(appt: dict, now: datetime | None = None) -> int:
    now = now or db_now()
    cutoff = appt["start_at"] - timedelta(hours=get_settings().cancel_full_refund_cutoff_hours)
    return 100 if now <= cutoff else 0


def reschedule(db: Database, appt: dict, new_start: datetime, member_id: str) -> dict:
    s = get_settings()
    if appt["status"] != S.BOOKED:
        raise HTTPException(409, "Only booked appointments can be rescheduled")
    with doctor_lock(db, appt["doctor_id"]) as doctor:
        now = db_now()
        if now > appt["start_at"] - timedelta(hours=s.reschedule_cutoff_hours):
            raise HTTPException(409, f"Rescheduling closes {s.reschedule_cutoff_hours} hours before the consult")
        slot = find_slot(db, doctor, new_start, now, exclude_appointment_id=appt["_id"])
        if slot is None:
            raise HTTPException(422, "That time is not one of the doctor's slots")
        if not slot.available:
            raise HTTPException(409, "That slot is taken. Pick another.")
        _expire_stale_holds(db, doctor["_id"], slot.start_at, slot.end_at, now)
        old = appt["start_at"]
        try:
            transition(db, appt["_id"], [S.BOOKED], {
                "$set": {"start_at": slot.start_at, "end_at": slot.end_at, "video_room_url": None},
                "$inc": {"reschedule_count": 1},
                "$push": {"events": event("rescheduled", "member", member_id,
                                          {"from": old.isoformat(), "to": slot.start_at.isoformat()})},
            }, extra_filter={"start_at": old})
        except DuplicateKeyError:
            raise HTTPException(409, "That slot was just taken. Pick another.")
    updated = get(db, appt["_id"])
    cancel_pending_for_appointment(db, appt["_id"], TIMED_TEMPLATES)
    schedule_jobs(db, updated)
    enqueue(db, channel="notify", template="appointment_rescheduled", recipient=doctor_recipient(updated["doctor_id"]),
            appointment_id=updated["_id"], payload={"from": old.isoformat()})
    return updated


def _queue_payout(db: Database, appt_id: ObjectId) -> None:
    appt = get(db, appt_id)
    gross = appt["amount_paid_paise"] - appt["refund_paise"]
    if gross <= 0:
        return
    commission = round(gross * get_settings().commission_percent / 100)
    try:
        db.payouts.insert_one({"doctor_id": appt["doctor_id"], "appointment_id": appt_id, "gross_paise": gross,
                               "commission_paise": commission, "net_paise": gross - commission,
                               "status": "queued", "created_at": db_now(), "paid_at": None})
    except DuplicateKeyError:
        pass


OUTCOME_STATUS = {"completed": S.COMPLETED, "no_show": S.NO_SHOW, "doctor_no_show": S.CANCELLED}


def mark_outcome(db: Database, appt: dict, outcome: str, *, actor_type: str, actor_id: str | None) -> dict:
    """outcome: completed | no_show (patient didn't join) | doctor_no_show (admin only)."""
    if outcome not in OUTCOME_STATUS:
        raise HTTPException(422, "Unknown outcome")
    if db_now() < appt["start_at"]:
        raise HTTPException(409, "The consult hasn't started yet")
    extra = {"cancelled_by": "doctor", "cancel_reason": "Doctor did not join"} if outcome == "doctor_no_show" else {}
    before = transition(db, appt["_id"], [S.BOOKED, S.NEEDS_REVIEW], {
        "$set": {**status_fields(OUTCOME_STATUS[outcome]), **extra},
        "$push": {"events": event(outcome, actor_type, actor_id)},
    })
    cancel_pending_for_appointment(db, appt["_id"], ["check_unmarked", "appointment_reminder"])
    member = member_recipient(before["member_id"])
    if outcome == "completed":
        _queue_payout(db, appt["_id"])
        enqueue(db, channel="notify", template="review_request", recipient=member, appointment_id=appt["_id"])
    elif outcome == "no_show":
        pct = get_settings().patient_no_show_refund_percent
        _queue_refund(db, appt["_id"], before["amount_paid_paise"] * pct // 100, "patient_no_show")
        _queue_payout(db, appt["_id"])
        enqueue(db, channel="notify", template="appointment_no_show", recipient=member, appointment_id=appt["_id"])
    else:
        _queue_refund(db, appt["_id"], before["amount_paid_paise"], "doctor_no_show")
        enqueue(db, channel="notify", template="appointment_cancelled", recipient=member, appointment_id=appt["_id"],
                payload={"by": "doctor", "reason": extra["cancel_reason"]})
    return get(db, appt["_id"])


def active_between(db: Database, doctor_id: ObjectId, start: datetime, end: datetime) -> list[dict]:
    return list(db.appointments.find({
        "doctor_id": doctor_id, "start_at": {"$gte": start, "$lt": end},
        "status": {"$in": [S.BOOKED.value, S.PENDING_PAYMENT.value]},
    }).sort("start_at", 1))

