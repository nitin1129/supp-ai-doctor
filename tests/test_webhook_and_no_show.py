import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest
from bson import ObjectId

from app import worker
from app.config import get_settings
from app.services import payments
from tests.conftest import MEMBER, PATIENT, book_and_pay, first_free_slot, onboard_doctor, pay

STATE = ("appointments", "outbox", "payouts", "payment_webhook_events", "audit_log")


def _snapshot(db) -> dict:
    return {name: sorted(db[name].find(), key=lambda d: str(d["_id"])) for name in STATE}


def _webhook(client, event: str, entity_key: str, entity: dict, event_id: str | None = None):
    body = json.dumps({"event": event, "payload": {entity_key: {"entity": entity}}}).encode()
    sig = hmac.new(get_settings().razorpay_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {"X-Razorpay-Signature": sig, "Content-Type": "application/json"}
    if event_id:
        headers["X-Razorpay-Event-Id"] = event_id
    return client.post("/payments/razorpay/webhook", content=body, headers=headers)


def _held(client, db):
    d = onboard_doctor(client, db)
    r = client.post("/appointments", headers=MEMBER, json={
        "doctor_id": d["doctor_id"], "start_at": first_free_slot(client, d["doctor_id"]), "mode": "video",
        "patient": PATIENT})
    assert r.status_code == 201, r.text
    return d, r.json()


# --- Duplicate webhooks ---------------------------------------------------------------

@pytest.mark.parametrize("event_id", ["evt_dup_1", None])  # None: deduplicated by body hash
def test_duplicate_payment_webhook_changes_nothing(client, db, event_id):
    _, b = _held(client, db)
    first = pay(client, b["razorpay_order_id"], 60000, payment_id="pay_dup", event_id=event_id)
    assert first.status_code == 200 and "duplicate" not in first.json()
    assert db.appointments.find_one({"_id": ObjectId(b["id"])})["status"] == "booked"
    before = _snapshot(db)

    second = pay(client, b["razorpay_order_id"], 60000, payment_id="pay_dup", event_id=event_id)
    assert second.status_code == 200 and second.json()["duplicate"] is True
    assert _snapshot(db) == before


def test_order_paid_after_payment_captured_changes_no_booking_state(client, db):
    # Razorpay sends both events for one payment, with different event ids.
    _, b = _held(client, db)
    assert pay(client, b["razorpay_order_id"], 60000, payment_id="pay_x", event_id="evt_a").status_code == 200
    before = _snapshot(db)
    r = _webhook(client, "order.paid", "payment",
                 {"id": "pay_x", "order_id": b["razorpay_order_id"], "amount": 60000}, event_id="evt_b")
    assert r.status_code == 200
    after = _snapshot(db)
    for name in ("appointments", "outbox", "payouts"):
        assert after[name] == before[name], name
    assert len(after["payment_webhook_events"]) == len(before["payment_webhook_events"]) + 1  # logged only


def test_duplicate_refund_webhook_changes_nothing(client, db):
    d = onboard_doctor(client, db)
    b = book_and_pay(client, d["doctor_id"])
    client.post(f"/appointments/{b['id']}/cancel", headers=MEMBER, json={})
    worker.run_once()
    appt = db.appointments.find_one({"_id": ObjectId(b["id"])})
    refund = {"id": appt["razorpay_refund_id"], "payment_id": appt["razorpay_payment_id"], "amount": 60000}
    assert _webhook(client, "refund.processed", "refund", refund, event_id="evt_r1").status_code == 200
    assert db.appointments.find_one({"_id": ObjectId(b["id"])})["refund_status"] == "processed"
    before = _snapshot(db)
    r = _webhook(client, "refund.processed", "refund", refund, event_id="evt_r1")
    assert r.json()["duplicate"] is True
    assert _snapshot(db) == before


# --- Patient no-show ------------------------------------------------------------------

def _no_show(client, db):
    d = onboard_doctor(client, db)
    b = book_and_pay(client, d["doctor_id"])
    start = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=30)
    db.appointments.update_one({"_id": ObjectId(b["id"])},
                               {"$set": {"start_at": start, "end_at": start + timedelta(minutes=15)}})
    r = client.patch(f"/doctor/appointments/{b['id']}/status", headers=d["headers"], json={"status": "no_show"})
    assert r.status_code == 200, r.text
    return d, b, r.json()


@pytest.mark.parametrize("commission", [15.0, 20.0, 0.0])
def test_no_show_queues_payout_with_commission_setting(client, db, monkeypatch, commission):
    monkeypatch.setattr(get_settings(), "commission_percent", commission)
    d, b, _ = _no_show(client, db)
    p = db.payouts.find_one({"appointment_id": ObjectId(b["id"])})
    expected = round(60000 * commission / 100)
    assert p["doctor_id"] == ObjectId(d["doctor_id"]) and p["status"] == "queued"
    assert (p["gross_paise"], p["commission_paise"], p["net_paise"]) == (60000, expected, 60000 - expected)


def test_no_show_does_not_refund_member_when_percent_is_zero(client, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "patient_no_show_refund_percent", 0)
    _, b, out = _no_show(client, db)
    assert out["member_status"] == "no_show" and out["refund_inr"] == 0
    appt = db.appointments.find_one({"_id": ObjectId(b["id"])})
    assert (appt["refund_paise"], appt["refund_status"]) == (0, None)
    assert db.outbox.count_documents({"template": "refund"}) == 0

    worker.run_once()
    assert payments.provider().refunds == {}
    assert db.payouts.find_one()["status"] == "sent"  # doctor still paid


def test_no_show_partial_refund_reduces_payout(client, db, monkeypatch):
    # Guards the other branch: a non-zero setting refunds and the payout is on what's left.
    monkeypatch.setattr(get_settings(), "patient_no_show_refund_percent", 50)
    _, b, out = _no_show(client, db)
    assert out["refund_inr"] == 300
    p = db.payouts.find_one()
    assert (p["gross_paise"], p["net_paise"]) == (30000, 25500)
