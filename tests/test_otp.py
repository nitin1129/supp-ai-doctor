"""OTP feature tests — application email verification, doctor OTP login, member OTP auth."""

from datetime import timedelta

import pytest
from bson import ObjectId

from app.config import get_settings
from app.db import now
from tests.conftest import (
    ADMIN,
    _captured,
    codes_sent_to,
    full_application,
    last_code,
    onboard_doctor,
    upload_docs,
    verify_application_email,
)

_EMAIL = "leela.menon@gmail.com"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _draft_with_email(client, email=_EMAIL):
    r = client.post("/doctors/applications", json=full_application(email=email))
    assert r.status_code == 201
    return r.json()["id"], r.json()["resume_token"]


def _send_url(app_id):
    return f"/doctors/applications/{app_id}/email/send-code"


def _verify_url(app_id):
    return f"/doctors/applications/{app_id}/email/verify"


# ---------------------------------------------------------------------------
# Application email verification
# ---------------------------------------------------------------------------

def test_application_email_send_and_verify(client):
    app_id, token = _draft_with_email(client)
    tok = {"X-Application-Token": token}

    r = client.post(_send_url(app_id), headers=tok)
    assert r.status_code == 202
    assert r.json()["email"] == _EMAIL

    code = last_code(_EMAIL)
    r = client.post(_verify_url(app_id), headers=tok, json={"code": code})
    assert r.status_code == 200
    assert r.json()["email_verified"] is True

    # Application is now submittable after uploading docs
    upload_docs(client, app_id, token)
    r = client.post(f"/doctors/applications/{app_id}/submit", headers=tok)
    assert r.status_code == 200


def test_changing_email_resets_verification(client):
    app_id, token = _draft_with_email(client)
    tok = {"X-Application-Token": token}

    verify_application_email(client, app_id, token, _EMAIL)
    assert client.get(f"/doctors/applications/{app_id}", headers=tok).json()["email_verified"] is True

    # Patch with a different email — clears verification
    r = client.patch(f"/doctors/applications/{app_id}", headers=tok,
                     json={"personal": {"email": "other@example.com"}})
    assert r.status_code == 200
    assert r.json()["email_verified"] is False

    # Patch back to the same email — still unverified (requires re-verification)
    r = client.patch(f"/doctors/applications/{app_id}", headers=tok,
                     json={"personal": {"email": _EMAIL}})
    assert r.json()["email_verified"] is False


def test_code_from_one_application_cant_verify_another(client):
    app1_id, tok1 = _draft_with_email(client, email="shared@example.com")
    app2_id, tok2 = _draft_with_email(client, email="shared@example.com")

    r = client.post(_send_url(app1_id), headers={"X-Application-Token": tok1})
    assert r.status_code == 202
    code = last_code("shared@example.com")

    # Code was issued in app1's context — must not verify app2
    r = client.post(_verify_url(app2_id), headers={"X-Application-Token": tok2},
                    json={"code": code})
    assert r.status_code == 400

    # And the code is now consumed, so app1 can't use it either
    r = client.post(_verify_url(app1_id), headers={"X-Application-Token": tok1},
                    json={"code": code})
    assert r.status_code == 400


def test_code_not_stored_in_plain_text(client, db):
    app_id, token = _draft_with_email(client)
    client.post(_send_url(app_id), headers={"X-Application-Token": token})
    code = last_code(_EMAIL)

    record = db.otp_codes.find_one({"email": _EMAIL})
    assert record is not None
    assert code not in str(record)  # raw code not in any field


def test_five_wrong_tries_burn_code(client):
    app_id, token = _draft_with_email(client)
    tok = {"X-Application-Token": token}

    client.post(_send_url(app_id), headers=tok)
    correct = last_code(_EMAIL)

    for _ in range(5):
        r = client.post(_verify_url(app_id), headers=tok, json={"code": "000000"})
        assert r.status_code == 400

    # Code is now burned — correct code no longer works
    r = client.post(_verify_url(app_id), headers=tok, json={"code": correct})
    assert r.status_code == 400


def test_code_works_once(client):
    app_id, token = _draft_with_email(client)
    tok = {"X-Application-Token": token}

    client.post(_send_url(app_id), headers=tok)
    code = last_code(_EMAIL)

    assert client.post(_verify_url(app_id), headers=tok, json={"code": code}).status_code == 200
    assert client.post(_verify_url(app_id), headers=tok, json={"code": code}).status_code == 400


def test_expired_code_fails(client, db):
    app_id, token = _draft_with_email(client)
    tok = {"X-Application-Token": token}

    client.post(_send_url(app_id), headers=tok)
    code = last_code(_EMAIL)

    # Wind back the expiry so the code is already expired
    past = now() - timedelta(minutes=1)
    db.otp_codes.update_one({"email": _EMAIL, "used_at": None}, {"$set": {"expires_at": past}})

    r = client.post(_verify_url(app_id), headers=tok, json={"code": code})
    assert r.status_code == 400


def test_resend_within_60s_is_429(client):
    app_id, token = _draft_with_email(client)
    tok = {"X-Application-Token": token}

    assert client.post(_send_url(app_id), headers=tok).status_code == 202
    code1 = last_code(_EMAIL)

    # Immediate resend
    r = client.post(_send_url(app_id), headers=tok)
    assert r.status_code == 429
    detail = r.json()["detail"]
    assert "retry_after" in detail
    assert detail["retry_after"] > 0

    # The original code is still valid (not replaced, since resend was blocked)
    r = client.post(_verify_url(app_id), headers=tok, json={"code": code1})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Doctor OTP login
# ---------------------------------------------------------------------------

def test_doctor_otp_login(client, db):
    d = onboard_doctor(client, db)

    r = client.post("/auth/doctor/otp/request", json={"email": _EMAIL})
    assert r.status_code == 202

    code = last_code(_EMAIL)
    r = client.post("/auth/doctor/otp/verify", json={"email": _EMAIL, "code": code})
    assert r.status_code == 200
    token = r.json()["access_token"]

    r = client.get("/doctor/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["id"] == d["doctor_id"]


def test_unknown_doctor_email_202_no_email(client):
    r = client.post("/auth/doctor/otp/request", json={"email": "nobody@example.com"})
    assert r.status_code == 202
    assert codes_sent_to("nobody@example.com") == []


def test_suspended_doctor_gets_no_code(client, db):
    d = onboard_doctor(client, db)
    db.doctors.update_one({"_id": ObjectId(d["doctor_id"])}, {"$set": {"status": "suspended"}})

    _captured.clear()  # drop codes captured during onboarding
    r = client.post("/auth/doctor/otp/request", json={"email": _EMAIL})
    assert r.status_code == 202
    assert codes_sent_to(_EMAIL) == []


# ---------------------------------------------------------------------------
# Member OTP auth
# ---------------------------------------------------------------------------

def test_new_member_signup_token_works(client):
    email = "new.patient@example.com"

    r = client.post("/auth/member/otp/request", json={"email": email})
    assert r.status_code == 202

    code = last_code(email)
    r = client.post("/auth/member/otp/verify", json={"email": email, "code": code, "name": "New Patient"})
    assert r.status_code == 200
    body = r.json()
    assert body["member"]["is_new"] is True
    assert body["member"]["email"] == email
    assert body["member"]["name"] == "New Patient"

    # Token works on the member-only appointments list
    r = client.get("/appointments", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert r.status_code == 200


def test_existing_member_keeps_id(client, db):
    email = "existing@example.com"
    s = get_settings()

    # Create member directly
    result = db[s.members_collection].insert_one({s.member_email_field: email, s.member_name_field: "Existing"})
    original_id = str(result.inserted_id)

    r = client.post("/auth/member/otp/request", json={"email": email})
    assert r.status_code == 202
    code = last_code(email)

    r = client.post("/auth/member/otp/verify", json={"email": email, "code": code})
    assert r.status_code == 200
    body = r.json()
    assert body["member"]["is_new"] is False
    assert body["member"]["id"] == original_id


def test_signup_off_unknown_member_no_code(client, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "member_otp_signup", False)

    email = "brand.new@example.com"
    r = client.post("/auth/member/otp/request", json={"email": email})
    assert r.status_code == 202
    assert codes_sent_to(email) == []
