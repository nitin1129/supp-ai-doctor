import hashlib
import hmac
import json
import os
import re
import shutil
import tempfile
from datetime import timedelta

# Set SUPPAI_MONGO_URL=mongodb://localhost:27017 to run against a real MongoDB.
os.environ.setdefault("SUPPAI_MONGO_URL", "mongomock://")
os.environ.setdefault("SUPPAI_MONGO_DB", "suppai_test")
os.environ["SUPPAI_STORAGE_DIR"] = tempfile.mkdtemp(prefix="suppai-test-")
os.environ["SUPPAI_PAYMENTS_PROVIDER"] = "fake"
os.environ["SUPPAI_JWT_SECRET"] = "test-secret-that-is-at-least-32-bytes-long"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import get_db  # noqa: E402
from app.models import ensure_indexes  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.security import _VERHOEFF_D, _VERHOEFF_P, create_access_token  # noqa: E402
from app.services.slots import today_ist  # noqa: E402
import app.services.otp as _otp_mod  # noqa: E402
from app.services import contacts as _contacts_mod  # noqa: E402
from app.ratelimit import limiter as _limiter  # noqa: E402
from app.services import payments as _payments_mod, payouts as _payouts_mod  # noqa: E402


_captured: dict[str, list[str]] = {}


@pytest.fixture(scope="session", autouse=True)
def _storage():
    yield
    shutil.rmtree(get_settings().storage_dir, ignore_errors=True)


@pytest.fixture(autouse=True)
def _clean():
    db = get_db()
    for name in db.list_collection_names():
        db.drop_collection(name)
    ensure_indexes(db)
    _contacts_mod.reset()
    _limiter.reset()
    _payments_mod._provider = _payouts_mod._provider = None  # fresh fakes per test
    yield
    _contacts_mod.reset()


@pytest.fixture(autouse=True)
def _mock_otp_deliver(monkeypatch):
    _captured.clear()

    def _fake_deliver(to: str, subject: str, text: str) -> None:
        m = re.match(r"^(\d{6})", subject)
        if m:
            _captured.setdefault(to.strip().lower(), []).append(m.group(1))

    monkeypatch.setattr(_otp_mod, "_deliver", _fake_deliver)
    yield


@pytest.fixture
def client():
    return TestClient(fastapi_app)


@pytest.fixture
def db():
    return get_db()


def bearer(role: str, sub: str = "1", **extra) -> dict:
    return {"Authorization": "Bearer " + create_access_token({"role": role, "sub": sub, **extra})}


ADMIN = bearer("admin", "admin-1")
MEMBER = bearer("member", "member-1")
MEMBER2 = bearer("member", "member-2")


def aadhaar(prefix: str = "23456789012") -> str:
    """Builds a valid Aadhaar number by appending the Verhoeff check digit."""
    for d in range(10):
        n = prefix + str(d)
        c = 0
        for i, ch in enumerate(reversed(n)):
            c = _VERHOEFF_D[c][_VERHOEFF_P[i % 8][int(ch)]]
        if c == 0:
            return n
    raise AssertionError


def full_application(name="Dr. Leela Menon", email="leela.menon@gmail.com", modes=("video",)) -> dict:
    return {
        "step": 7,
        "personal": {
            "full_name": name, "gender": "female", "date_of_birth": "1985-04-12", "mobile": "9876543210",
            "email": email, "languages": ["English", "Hindi", "Malayalam"],
            "address": {"line1": "12 MG Road", "city": "Ranchi", "state": "Jharkhand", "pincode": "834001"},
        },
        "identity": {"pan": "abcde1234f", "aadhaar": aadhaar()},
        "registration": {"registration_number": "KMC-45821", "qualifications": ["MBBS", "MD (Medicine)"],
                         "specialties": ["Diabetology", "General Medicine"]},
        "teleconsult": {"registration_council": "Kerala Medical Council", "experience_years": 12,
                        "modes": list(modes), "fee_inr": 600, "clinic_address": "Healix, Ranchi",
                        "clinic_pincode": "834005"},
        "bank": {"account_holder": "Leela Menon", "account_number": "123456789012", "ifsc": "HDFC0001234"},
        "compliance": {"info_is_true": True, "follows_telemedicine_guidelines": True,
                       "consents_to_verification": True, "accepts_terms": True},
    }


REQUIRED_DOCS = ["registration_certificate", "degree_certificate", "pan_card", "photo"]


def upload_docs(client, app_id, token):
    for kind in REQUIRED_DOCS:
        r = client.post(f"/doctors/applications/{app_id}/documents", headers={"X-Application-Token": token},
                        data={"kind": kind}, files={"file": (f"{kind}.pdf", b"%PDF-1.4 test", "application/pdf")})
        assert r.status_code == 201, r.text


def latest_link_token(db, email: str) -> str:
    row = db.outbox.find_one({"recipient": email, "template": {"$in": ["doctor_invite", "doctor_password_reset"]}},
                             sort=[("_id", -1)])
    return row["payload"]["link"].split("token=")[1]


def last_code(email: str) -> str:
    codes = _captured.get(email.strip().lower(), [])
    assert codes, f"No OTP captured for {email!r}"
    return codes[-1]


def codes_sent_to(email: str) -> list[str]:
    return list(_captured.get(email.strip().lower(), []))


def verify_application_email(client, app_id: str, token: str, email: str) -> None:
    r = client.post(
        f"/doctors/applications/{app_id}/email/send-code",
        headers={"X-Application-Token": token},
    )
    assert r.status_code == 202, r.text
    r = client.post(
        f"/doctors/applications/{app_id}/email/verify",
        headers={"X-Application-Token": token},
        json={"code": last_code(email)},
    )
    assert r.status_code == 200, r.text


def onboard_doctor(client, db, name="Dr. Leela Menon", email="leela.menon@gmail.com", modes=("video",),
                   password="Maple2891secure") -> dict:
    """Applies, accepts, sets password, logs in, sets hours. Returns ids and headers."""
    r = client.post("/doctors/applications", json=full_application(name, email, modes))
    app_id, token = r.json()["id"], r.json()["resume_token"]
    upload_docs(client, app_id, token)
    verify_application_email(client, app_id, token, email)
    assert client.post(f"/doctors/applications/{app_id}/submit", headers={"X-Application-Token": token}).status_code == 200
    r = client.post(f"/admin/doctors/applications/{app_id}/accept", headers=ADMIN, json={"registration_verified": True})
    assert r.status_code == 200, r.text
    accepted = r.json()
    r = client.post("/auth/doctor/set-password", json={"token": latest_link_token(db, email), "password": password})
    assert r.status_code == 200, r.text
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    windows = [{"weekday": d, "start": "09:00", "end": "13:00"} for d in range(7)]
    windows += [{"weekday": d, "start": "16:00", "end": "19:00"} for d in range(7)]
    r = client.put("/doctor/hours", headers=headers, json={"windows": windows, "slot_minutes": 15, "notice_minutes": 120})
    assert r.status_code == 200, r.text
    return {"doctor_id": accepted["doctor_id"], "username": accepted["username"], "headers": headers,
            "application_id": app_id}


def booking_day(days_ahead: int = 2):
    return today_ist() + timedelta(days=days_ahead)


def first_free_slot(client, doctor_id, day=None, index=0) -> str:
    day = day or booking_day()
    slots = client.get(f"/doctors/{doctor_id}/slots", params={"date": day.isoformat()}).json()["slots"]
    free = [s for s in slots if s["available"]]
    return free[index]["start_at"]


def pay(client, order_id: str, amount: int, payment_id: str | None = None, event_id: str | None = None):
    payment_id = payment_id or f"pay_{order_id[-8:]}"
    body = json.dumps({
        "event": "payment.captured",
        "payload": {"payment": {"entity": {"id": payment_id, "order_id": order_id, "amount": amount, "status": "captured"}}},
    }).encode()
    sig = hmac.new(get_settings().razorpay_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {"X-Razorpay-Signature": sig, "Content-Type": "application/json"}
    if event_id:
        headers["X-Razorpay-Event-Id"] = event_id
    return client.post("/payments/razorpay/webhook", content=body, headers=headers)


PATIENT = {"name": "Ravi Kumar", "age": 52, "gender": "male", "relation": "self", "concern": "Sugar levels high"}


def book_and_pay(client, doctor_id, start_at=None, member=MEMBER, mode="video") -> dict:
    start_at = start_at or first_free_slot(client, doctor_id)
    r = client.post("/appointments", headers=member,
                    json={"doctor_id": doctor_id, "start_at": start_at, "mode": mode, "patient": PATIENT})
    assert r.status_code == 201, r.text
    b = r.json()
    assert pay(client, b["razorpay_order_id"], b["amount_paise"]).status_code == 200
    return b
