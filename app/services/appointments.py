"""Appointment lifecycle: hold → paid → done / no-show / cancelled.

One appointment row is read by both the doctor's dashboard and the member's
tracker. member_view_status() maps the stored status to what the member sees.
"""

import logging
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    Appointment,
    AppointmentStatus,
    Doctor,
    DoctorStatus,
    Payout,
)
from app.services import payments
from app.services.audit import appointment_event
from app.services.outbox import cancel_pending_for_appointment, enqueue
from app.services.slots import busy_intervals, find_slot

log = logging.getLogger(__name__)

S = AppointmentStatus
TIMED_TEMPLATES = ["appointment_reminder", "create_video_room", "check_unmarked"]


def member_recipient(member_id: str) -> str:
    """Member contact details live in the main SuppAI platform; the worker resolves them."""
    return f"member:{member_id}"


def member_view_status(appt: Appointment, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    lead = timedelta(minutes=get_settings().video_room_lead_minutes)
    match appt.status:
        case S.PENDING_PAYMENT:
            return "awaiting_payment" if appt.hold_expires_at and appt.hold_expires_at > now else "expired"
        case S.BOOKED:
            return "in_progress" if appt.start_at - lead <= now < appt.end_at else "upcoming"
        case S.NEEDS_REVIEW:
            return "completed"  # pending admin confirmation; member is not shown the dispute state
        case _:
            return appt.status


def lock_doctor(db: Session, doctor_id: int) -> Doctor:
    """Serialises bookings per doctor so overlap checks can't race."""
    doctor = db.scalar(select(Doctor).where(Doctor.id == doctor_id).with_for_update())
    if doctor is None:
        raise HTTPException(404, "Doctor not found")
    return doctor


def _expire_stale_holds(db: Session, doctor_id: int, start: datetime, end: datetime, now: datetime) -> None:
    db.execute(
        update(Appointment)
        .where(
            Appointment.doctor_id == doctor_id,
            Appointment.status == S.PENDING_PAYMENT,
            Appointment.hold_expires_at <= now,
            Appointment.start_at < end,
            Appointment.end_at > start,
        )
        .values(status=S.EXPIRED)
    )


# --- Booking ----------------------------------------------------------------

def create_hold(
    db: Session, *, doctor_id: int, member_id: str, start_at: datetime, mode: str, patient: dict
) -> Appointment:
    s = get_settings()
    now = datetime.now(UTC)
    doctor = lock_doctor(db, doctor_id)
    if doctor.status != DoctorStatus.ACTIVE:
        raise HTTPException(409, "This doctor is not taking bookings")
    if mode not in doctor.modes:
        raise HTTPException(422, f"This doctor does not offer {mode} consults")
    slot = find_slot(db, doctor, start_at, now)
    if slot is None:
        raise HTTPException(422, "That time is not one of the doctor's slots")
    if not slot.available:
        raise HTTPException(409, "That slot was just taken. Pick another.")

    _expire_stale_holds(db, doctor.id, slot.start_at, slot.end_at, now)
    appt = Appointment(
        doctor_id=doctor.id,
        member_id=member_id,
        patient=patient,
        mode=mode,
        start_at=slot.start_at,
        end_at=slot.end_at,
        status=S.PENDING_PAYMENT,
        hold_expires_at=now + timedelta(minutes=s.slot_hold_minutes),
        fee_paise=doctor.fee_paise,
        clinic_address=doctor.clinic_address if mode == "clinic" else None,
    )
    db.add(appt)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "That slot was just taken. Pick another.")
    appointment_event(db, appt.id, "held", "member", member_id)
    db.commit()

    try:
        appt.razorpay_order_id = payments.provider().create_order(
            appt.fee_paise, receipt=f"appt-{appt.id}", notes={"appointment_id": str(appt.id)}
        )
    except Exception:
        log.exception("Razorpay order failed for appointment %s", appt.id)
        appt.status = S.EXPIRED
        db.commit()
        raise HTTPException(502, "Could not start the payment. Please try again.")
    db.commit()
    return appt


def schedule_jobs(db: Session, appt: Appointment) -> None:
    s = get_settings()
    now = datetime.now(UTC)
    for minutes in s.reminder_offsets_minutes:
        due = appt.start_at - timedelta(minutes=minutes)
        if due > now:
            for recipient in (member_recipient(appt.member_id), f"doctor:{appt.doctor_id}"):
                enqueue(db, channel="notify", template="appointment_reminder", recipient=recipient,
                        payload={"minutes_before": minutes}, due_at=due, appointment_id=appt.id)
    if appt.mode == "video":
        enqueue(db, channel="job", template="create_video_room",
                due_at=max(now, appt.start_at - timedelta(minutes=s.video_room_lead_minutes)),
                appointment_id=appt.id)
    enqueue(db, channel="job", template="check_unmarked",
            due_at=appt.end_at + timedelta(hours=s.unmarked_review_after_hours), appointment_id=appt.id)


def _queue_refund(db: Session, appt: Appointment, amount_paise: int, reason: str) -> None:
    amount = min(amount_paise, appt.amount_paid_paise - appt.refund_paise)
    if amount <= 0 or not appt.razorpay_payment_id:
        return
    appt.refund_paise += amount
    appt.refund_status = "pending"
    enqueue(db, channel="job", template="refund", payload={"amount_paise": amount, "reason": reason},
            appointment_id=appt.id)


def mark_paid(db: Session, *, order_id: str, payment_id: str, amount_paise: int) -> Appointment | None:
    """Called from the Razorpay webhook. Safe to call more than once."""
    now = datetime.now(UTC)
    appt = db.scalar(select(Appointment).where(Appointment.razorpay_order_id == order_id))
    if appt is None:
        return None
    lock_doctor(db, appt.doctor_id)
    db.refresh(appt)

    if appt.razorpay_payment_id == payment_id and appt.status != S.PENDING_PAYMENT:
        return appt  # duplicate delivery

    appt.razorpay_payment_id = payment_id
    appt.amount_paid_paise = amount_paise

    if appt.status in (S.PENDING_PAYMENT, S.EXPIRED):
        clash = busy_intervals(db, appt.doctor_id, appt.start_at, appt.end_at, now, exclude_id=appt.id)
        if not clash and appt.start_at > now:
            appt.status = S.BOOKED
            appt.hold_expires_at = None
            try:
                db.flush()
            except IntegrityError:  # someone booked the exact start in between
                db.rollback()
                return mark_paid(db, order_id=order_id, payment_id=payment_id, amount_paise=amount_paise)
            appointment_event(db, appt.id, "paid", "system", data={"payment_id": payment_id})
            enqueue(db, channel="notify", template="appointment_booked", recipient=f"doctor:{appt.doctor_id}",
                    appointment_id=appt.id)
            enqueue(db, channel="notify", template="appointment_confirmed",
                    recipient=member_recipient(appt.member_id), appointment_id=appt.id)
            schedule_jobs(db, appt)
            db.commit()
            return appt

        # Paid after the hold lapsed and the slot is gone (or now in the past): refund in full.
        appt.status = S.CANCELLED
        appt.cancelled_by = "system"
        appt.cancel_reason = "Payment arrived after the slot was released"
        appointment_event(db, appt.id, "late_payment_refunded", "system", data={"payment_id": payment_id})
        _queue_refund(db, appt, amount_paise, "late_payment")
        enqueue(db, channel="notify", template="late_payment_refunded",
                recipient=member_recipient(appt.member_id), appointment_id=appt.id)
        db.commit()
        return appt

    if appt.status == S.CANCELLED:
        _queue_refund(db, appt, amount_paise, "paid_after_cancel")
        db.commit()
    return appt


# --- Changes after booking --------------------------------------------------

def cancel(db: Session, appt: Appointment, *, by: str, actor_id: str | None, reason: str | None,
           refund_percent: int) -> Appointment:
    if appt.status not in (S.BOOKED, S.PENDING_PAYMENT, S.NEEDS_REVIEW):
        raise HTTPException(409, f"Appointment is {appt.status} and can't be cancelled")
    was_paid = appt.status != S.PENDING_PAYMENT
    appt.status = S.CANCELLED  # a late payment for a cancelled hold is refunded in mark_paid
    appt.cancelled_by = by
    appt.cancel_reason = reason
    cancel_pending_for_appointment(db, appt.id, TIMED_TEMPLATES)
    appointment_event(db, appt.id, "cancelled", by, actor_id, {"reason": reason, "refund_percent": refund_percent})
    if was_paid:
        _queue_refund(db, appt, appt.amount_paid_paise * refund_percent // 100, f"cancelled_by_{by}")
        targets = [member_recipient(appt.member_id), f"doctor:{appt.doctor_id}"]
        for r in targets:
            enqueue(db, channel="notify", template="appointment_cancelled", recipient=r, appointment_id=appt.id,
                    payload={"by": by, "reason": reason, "refund_paise": appt.refund_paise})
    return appt


def member_cancel_refund_percent(appt: Appointment, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    cutoff = appt.start_at - timedelta(hours=get_settings().cancel_full_refund_cutoff_hours)
    return 100 if now <= cutoff else 0


def reschedule(db: Session, appt: Appointment, new_start: datetime, member_id: str) -> Appointment:
    s = get_settings()
    now = datetime.now(UTC)
    if appt.status != S.BOOKED:
        raise HTTPException(409, "Only booked appointments can be rescheduled")
    if now > appt.start_at - timedelta(hours=s.reschedule_cutoff_hours):
        raise HTTPException(409, f"Rescheduling closes {s.reschedule_cutoff_hours} hours before the consult")
    doctor = lock_doctor(db, appt.doctor_id)
    slot = find_slot(db, doctor, new_start, now, exclude_appointment_id=appt.id)
    if slot is None:
        raise HTTPException(422, "That time is not one of the doctor's slots")
    if not slot.available:
        raise HTTPException(409, "That slot is taken. Pick another.")
    _expire_stale_holds(db, doctor.id, slot.start_at, slot.end_at, now)
    old = appt.start_at
    appt.start_at, appt.end_at = slot.start_at, slot.end_at
    appt.reschedule_count += 1
    appt.video_room_url = None
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "That slot was just taken. Pick another.")
    cancel_pending_for_appointment(db, appt.id, TIMED_TEMPLATES)
    schedule_jobs(db, appt)
    appointment_event(db, appt.id, "rescheduled", "member", member_id,
                      {"from": old.isoformat(), "to": slot.start_at.isoformat()})
    enqueue(db, channel="notify", template="appointment_rescheduled", recipient=f"doctor:{appt.doctor_id}",
            appointment_id=appt.id, payload={"from": old.isoformat()})
    return appt


def _queue_payout(db: Session, appt: Appointment) -> None:
    gross = appt.amount_paid_paise - appt.refund_paise
    if gross <= 0 or db.scalar(select(Payout.id).where(Payout.appointment_id == appt.id)):
        return
    commission = round(gross * get_settings().commission_percent / 100)
    db.add(Payout(doctor_id=appt.doctor_id, appointment_id=appt.id, gross_paise=gross,
                  commission_paise=commission, net_paise=gross - commission))


def mark_outcome(db: Session, appt: Appointment, outcome: str, *, actor_type: str, actor_id: str | None) -> Appointment:
    """outcome: completed | no_show (patient didn't join) | doctor_no_show (admin only)."""
    now = datetime.now(UTC)
    allowed_from = (S.BOOKED, S.NEEDS_REVIEW)
    if appt.status not in allowed_from:
        raise HTTPException(409, f"Appointment is {appt.status}")
    if now < appt.start_at:
        raise HTTPException(409, "The consult hasn't started yet")

    cancel_pending_for_appointment(db, appt.id, ["check_unmarked", "appointment_reminder"])
    if outcome == "completed":
        appt.status = S.COMPLETED
        _queue_payout(db, appt)
        enqueue(db, channel="notify", template="review_request", recipient=member_recipient(appt.member_id),
                appointment_id=appt.id)
    elif outcome == "no_show":
        appt.status = S.NO_SHOW
        pct = get_settings().patient_no_show_refund_percent
        _queue_refund(db, appt, appt.amount_paid_paise * pct // 100, "patient_no_show")
        _queue_payout(db, appt)
        enqueue(db, channel="notify", template="appointment_no_show", recipient=member_recipient(appt.member_id),
                appointment_id=appt.id)
    elif outcome == "doctor_no_show":
        appt.status = S.CANCELLED
        appt.cancelled_by = "doctor"
        appt.cancel_reason = "Doctor did not join"
        _queue_refund(db, appt, appt.amount_paid_paise, "doctor_no_show")
        enqueue(db, channel="notify", template="appointment_cancelled", recipient=member_recipient(appt.member_id),
                appointment_id=appt.id, payload={"by": "doctor", "reason": appt.cancel_reason})
    else:
        raise HTTPException(422, "Unknown outcome")
    appointment_event(db, appt.id, outcome, actor_type, actor_id)
    return appt


def active_on_day(db: Session, doctor_id: int, start: datetime, end: datetime) -> list[Appointment]:
    return list(
        db.scalars(
            select(Appointment)
            .where(
                Appointment.doctor_id == doctor_id,
                Appointment.start_at >= start,
                Appointment.start_at < end,
                Appointment.status.in_([S.BOOKED, S.PENDING_PAYMENT]),
            )
            .order_by(Appointment.start_at)
        )
    )

