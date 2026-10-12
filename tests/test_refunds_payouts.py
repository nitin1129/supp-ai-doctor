from datetime import UTC, datetime, timedelta

import pytest
from bson import ObjectId

from app import worker
from app.config import get_settings
from app.services import payments, payouts
from tests.conftest import MEMBER, book_and_pay, onboard_doctor


def _cancelled_and_paid(client, db):
    d = onboard_doctor(client, db)
    b = book_and_pay(client, d["doctor_id"])
    assert client.post(f"/appointments/{b['id']}/cancel", headers=MEMBER, json={}).status_code == 200
    return d, b


def _completed(client, db):
    d = onboard_doctor(client, db)
    b = book_and_pay(client, d["doctor_id"])
    start = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=30)
    db.appointments.update_one({"_id": ObjectId(b["id"])},
                               {"$set": {"start_at": start, "end_at": start + timedelta(minutes=15)}})
    r = client.patch(f"/doctor/appointments/{b['id']}/status", headers=d["headers"], json={"status": "completed"})
    assert r.status_code == 200, r.text
    return d, b


# --- Refunds -----------------------------------------------------------------------

def test_refunds_use_the_configured_provider(monkeypatch):
    monkeypatch.setattr(get_settings(), "payments_provider", "razorpay")
    payments._provider = None
    assert isinstance(payments.provider(), payments.RazorpayProvider)
    payments._provider = None
    monkeypatch.setattr(get_settings(), "payments_provider", "fake")
    assert isinstance(payments.provider(), payments.FakeProvider)


def test_refund_job_calls_provider_once(client, db):
    _, b = _cancelled_and_paid(client, db)
    fake = payments.provider()
    worker.run_once()
    appt = db.appointments.find_one({"_id": ObjectId(b["id"])})
    assert len(fake.refunds) == 1
    (payment_id, receipt), refund = next(iter(fake.refunds.items()))
    assert payment_id == appt["razorpay_payment_id"] and refund["amount"] == 60000
    assert appt["razorpay_refund_id"] == refund["id"]
    assert [r["receipt"] for r in appt["refunds"]] == [receipt]


def test_refund_retry_after_crash_does_not_refund_twice(client, db, monkeypatch):
    _, b = _cancelled_and_paid(client, db)
    fake = payments.provider()
    calls = []
    original = fake.refund

    def counting_refund(*args, **kwargs):
        calls.append(kwargs["receipt"])
        return original(*args, **kwargs)

    monkeypatch.setattr(fake, "refund", counting_refund)
    # The provider accepts the refund, then the worker dies before recording it.
    real_update = db.appointments.update_one
    state = {"crashed": False}

    def crash_once(filter, update, *a, **kw):
        if "refunds" in update.get("$push", {}) and not state["crashed"]:
            state["crashed"] = True
            raise ConnectionError("lost connection to MongoDB")
        return real_update(filter, update, *a, **kw)

    monkeypatch.setattr(db.appointments, "update_one", crash_once)
    t = datetime.now(UTC)
    worker.run_once(now=t, db=db)
    row = db.outbox.find_one({"template": "refund"})
    assert (row["status"], row["attempts"]) == ("pending", 1)

    worker.run_once(now=t + timedelta(hours=1), db=db)
    assert db.outbox.find_one({"template": "refund"})["status"] == "sent"
    assert len(calls) == 2 and calls[0] == calls[1]  # same receipt on the retry
    assert len(fake.refunds) == 1  # ...so only one refund exists
    appt = db.appointments.find_one({"_id": ObjectId(b["id"])})
    assert len(appt["refunds"]) == 1


def test_refund_job_run_twice_records_once(client, db):
    _, b = _cancelled_and_paid(client, db)
    row = db.outbox.find_one({"template": "refund"})
    worker._job_refund(db, row)
    worker._job_refund(db, row)
    appt = db.appointments.find_one({"_id": ObjectId(b["id"])})
    assert len(payments.provider().refunds) == 1 and len(appt["refunds"]) == 1
    assert sum(e["type"] == "refund_requested" for e in appt["events"]) == 1


def test_refund_failure_is_retried_then_marked_failed(client, db, monkeypatch):
    _, b = _cancelled_and_paid(client, db)

    def boom(*a, **kw):
        raise ConnectionError("Razorpay unreachable")

    monkeypatch.setattr(payments.provider(), "refund", boom)
    t = datetime.now(UTC)
    for i in range(worker.MAX_ATTEMPTS):
        worker.run_once(now=t + timedelta(hours=i))
    assert db.outbox.find_one({"template": "refund"})["status"] == "failed"
    assert db.appointments.find_one({"_id": ObjectId(b["id"])})["refund_status"] == "failed"


# --- Payouts ------------------------------------------------------------------------

def test_completed_consult_payout_is_sent_once(client, db):
    d, b = _completed(client, db)
    fake = payouts.provider()
    worker.run_once()
    p = db.payouts.find_one()
    assert p["status"] == "sent" and p["provider_payout_id"].startswith("pout_fake_")
    assert len(fake.payouts) == 1
    sent = fake.payouts[p["idempotency_key"]]
    assert (sent["amount"], sent["account_number"], sent["ifsc"]) == (51000, "123456789012", "HDFC0001234")
    assert sent["reference_id"] == str(p["_id"])

    for i in range(3):
        worker.run_once(now=datetime.now(UTC) + timedelta(hours=i + 1))
    assert fake.calls == 1 and db.payouts.find_one()["status"] == "sent"


def test_payout_transient_error_retries_with_same_key(client, db, monkeypatch):
    _completed(client, db)
    fake = payouts.provider()
    keys = []
    original = fake.transfer

    def flaky(**kw):
        keys.append(kw["idempotency_key"])
        result = original(**kw)
        if len(keys) == 1:
            raise TimeoutError("response lost after RazorpayX accepted it")
        return result

    monkeypatch.setattr(fake, "transfer", flaky)
    t = datetime.now(UTC)
    worker.run_once(now=t)
    p = db.payouts.find_one()
    assert (p["status"], p["attempts"]) == ("queued", 1)
    worker.run_once(now=t)  # backoff: not retried yet
    assert len(keys) == 1
    worker.run_once(now=t + timedelta(hours=1))
    assert db.payouts.find_one()["status"] == "sent"
    assert keys[0] == keys[1] and len(fake.payouts) == 1


def test_payout_rejected_is_marked_failed_and_not_retried(client, db, monkeypatch):
    _completed(client, db)
    fake = payouts.provider()

    def reject(**kw):
        raise payouts.PayoutRejected("invalid IFSC")

    monkeypatch.setattr(fake, "transfer", reject)
    worker.run_once()
    p = db.payouts.find_one()
    assert p["status"] == "failed" and "invalid IFSC" in p["last_error"]
    monkeypatch.undo()
    worker.run_once(now=datetime.now(UTC) + timedelta(days=1))
    assert db.payouts.find_one()["status"] == "failed" and fake.payouts == {}


def test_payout_without_bank_details_fails(client, db):
    d, _ = _completed(client, db)
    db.doctors.update_one({"_id": ObjectId(d["doctor_id"])}, {"$set": {"bank_account_encrypted": None}})
    worker.run_once()
    p = db.payouts.find_one()
    assert p["status"] == "failed" and "bank account" in p["last_error"]
    assert payouts.provider().payouts == {}


def test_payout_gives_up_after_max_attempts(client, db, monkeypatch):
    _completed(client, db)

    def down(**kw):
        raise ConnectionError("RazorpayX down")

    monkeypatch.setattr(payouts.provider(), "transfer", down)
    t = datetime.now(UTC)
    for i in range(worker.MAX_ATTEMPTS):
        worker.run_once(now=t + timedelta(hours=i))
    p = db.payouts.find_one()
    assert p["status"] == "failed" and p["attempts"] == worker.MAX_ATTEMPTS


def test_stuck_payout_is_reclaimed_with_same_key(client, db):
    _completed(client, db)
    t = datetime.now(UTC)
    key = "11111111-2222-4333-8444-555555555555"
    # A worker claimed it and died mid-call.
    db.payouts.update_one({}, {"$set": {"status": "sending", "claimed_at": t, "idempotency_key": key}})
    worker.run_once(now=t + timedelta(minutes=5))
    assert db.payouts.find_one()["status"] == "sending"  # still owned by the first worker
    worker.run_once(now=t + timedelta(minutes=11))
    assert db.payouts.find_one()["status"] == "sent"
    assert list(payouts.provider().payouts) == [key]


def test_concurrent_claim_only_one_wins(client, db):
    _completed(client, db)
    t = datetime.now(UTC)
    first = worker._claim_payout(db, t)
    assert first is not None
    assert worker._claim_payout(db, t) is None


def test_payouts_provider_switch(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "payouts_provider", "razorpayx")
    payouts._provider = None
    with pytest.raises(RuntimeError, match="RAZORPAYX_ACCOUNT_NUMBER"):
        payouts.provider()
    monkeypatch.setattr(s, "razorpayx_account_number", "2323230000000000")
    payouts._provider = None
    assert isinstance(payouts.provider(), payouts.RazorpayXProvider)
    monkeypatch.setattr(s, "payouts_provider", "nope")
    payouts._provider = None
    with pytest.raises(ValueError):
        payouts.provider()
