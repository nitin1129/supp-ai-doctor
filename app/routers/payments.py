import hashlib
import json
import logging

from fastapi import APIRouter, Header, HTTPException, Request
from pymongo.errors import DuplicateKeyError

from app.db import now
from app.deps import DB
from app.services import appointments as appt_svc
from app.services.payments import verify_webhook_signature

log = logging.getLogger(__name__)
router = APIRouter(prefix="/payments", tags=["Payments"])


@router.post("/razorpay/webhook")
async def razorpay_webhook(
    request: Request,
    db: DB,
    x_razorpay_signature: str | None = Header(default=None),
    x_razorpay_event_id: str | None = Header(default=None),
) -> dict:
    """Subscribe to payment.captured, order.paid, refund.processed and refund.failed."""
    body = await request.body()
    if not verify_webhook_signature(body, x_razorpay_signature):
        raise HTTPException(400, "Bad signature")
    payload = json.loads(body)
    event = payload.get("event", "")
    event_id = x_razorpay_event_id or hashlib.sha256(body).hexdigest()

    if db.payment_webhook_events.find_one({"provider_event_id": event_id}, {"_id": 1}):
        return {"ok": True, "duplicate": True}

    entity = payload.get("payload", {})
    if event in ("payment.captured", "order.paid"):
        pay = entity.get("payment", {}).get("entity", {})
        if pay.get("order_id") and pay.get("id"):
            appt_svc.mark_paid(db, order_id=pay["order_id"], payment_id=pay["id"], amount_paise=int(pay["amount"]))
    elif event in ("refund.processed", "refund.failed"):
        ref = entity.get("refund", {}).get("entity", {})
        db.appointments.update_one(
            {"razorpay_payment_id": ref.get("payment_id")},
            {"$set": {"refund_status": "processed" if event == "refund.processed" else "failed"}},
        )
    else:
        log.info("Ignoring Razorpay event %s", event)

    try:
        db.payment_webhook_events.insert_one(
            {"provider_event_id": event_id, "event": event, "payload": payload, "received_at": now()})
    except DuplicateKeyError:
        pass  # processed concurrently; the handlers above are idempotent
    return {"ok": True}
