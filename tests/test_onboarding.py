from bson import ObjectId

from tests.conftest import (
    ADMIN,
    MEMBER,
    aadhaar,
    bearer,
    full_application,
    latest_link_token,
    onboard_doctor,
    upload_docs,
)


def _draft(client, body=None):
    r = client.post("/doctors/applications", json=body or {})
    assert r.status_code == 201, r.text
    return r.json()["id"], {"X-Application-Token": r.json()["resume_token"]}


def test_draft_saves_steps_and_resumes(client):
    app_id, tok = _draft(client, {"step": 1, "personal": {"full_name": "Dr. Leela Menon", "mobile": "9876543210"}})
    r = client.patch(f"/doctors/applications/{app_id}", headers=tok,
                     json={"step": 2, "identity": {"pan": "ABCDE1234F", "aadhaar": aadhaar()}})
    assert r.status_code == 200
    body = client.get(f"/doctors/applications/{app_id}", headers=tok).json()
    assert body["status"] == "draft"
    assert body["current_step"] == 2
    assert body["data"]["personal"]["full_name"] == "Dr. Leela Menon"
    assert body["pan_masked"] == "XXXXXX234F"
    assert body["aadhaar_masked"].startswith("XXXXXXXX")
    assert "documents.photo" in body["missing"]


def test_wrong_resume_token_is_404(client):
    app_id, _ = _draft(client)
    assert client.get(f"/doctors/applications/{app_id}", headers={"X-Application-Token": "nope"}).status_code == 404


def test_validation_rejects_bad_ids(client):
    app_id, tok = _draft(client)
    assert client.patch(f"/doctors/applications/{app_id}", headers=tok,
                        json={"identity": {"aadhaar": "234567890123"}}).status_code == 422
    assert client.patch(f"/doctors/applications/{app_id}", headers=tok,
                        json={"identity": {"pan": "12345"}}).status_code == 422


def test_full_aadhaar_and_pan_never_stored_in_plain_text(client, db):
    app_id, tok = _draft(client, full_application())
    row = db.doctor_applications.find_one({"_id": ObjectId(app_id)})
    dump = str(row)
    assert aadhaar() not in dump
    assert "ABCDE1234F" not in dump
    assert "123456789012" not in dump
    assert row["aadhaar_last4"] == aadhaar()[-4:]


def test_submit_requires_everything(client):
    app_id, tok = _draft(client, full_application())
    r = client.post(f"/doctors/applications/{app_id}/submit", headers=tok)
    assert r.status_code == 422
    assert "documents.registration_certificate" in r.json()["detail"]["missing"]
    upload_docs(client, app_id, tok["X-Application-Token"])
    r = client.post(f"/doctors/applications/{app_id}/submit", headers=tok)
    assert r.status_code == 200
    assert r.json()["status"] == "submitted"
    assert r.json()["reference"] == "SA-DOC-3301"
    # No more edits once submitted
    assert client.patch(f"/doctors/applications/{app_id}", headers=tok, json={"step": 3}).status_code == 409


def test_clinic_mode_needs_clinic_address(client):
    body = full_application(modes=("video", "clinic"))
    body["teleconsult"].pop("clinic_address")
    app_id, tok = _draft(client, body)
    missing = client.get(f"/doctors/applications/{app_id}", headers=tok).json()["missing"]
    assert "teleconsult.clinic_address" in missing


def _submitted(client):
    app_id, tok = _draft(client, full_application())
    upload_docs(client, app_id, tok["X-Application-Token"])
    client.post(f"/doctors/applications/{app_id}/submit", headers=tok)
    return app_id, tok


def test_admin_endpoints_need_admin(client):
    assert client.get("/admin/doctors/applications").status_code == 401
    assert client.get("/admin/doctors/applications", headers=MEMBER).status_code == 403


def test_admin_list_filter_and_detail(client, db):
    app_id, _ = _submitted(client)
    _draft(client)  # drafts are hidden from the default list
    r = client.get("/admin/doctors/applications", headers=ADMIN).json()
    assert r["total"] == 1 and r["items"][0]["reference"] == "SA-DOC-3301"
    assert client.get("/admin/doctors/applications", headers=ADMIN, params={"status": "on_hold"}).json()["total"] == 0
    assert client.get("/admin/doctors/applications", headers=ADMIN, params={"q": "leela"}).json()["total"] == 1

    detail = client.get(f"/admin/doctors/applications/{app_id}", headers=ADMIN).json()
    assert detail["pan"] == "ABCDE1234F"
    assert len(detail["documents"]) == 4
    url = detail["documents"][0]["url"]
    assert client.get(url).status_code == 200
    assert client.get(url.replace("sig=", "sig=x")).status_code == 403
    assert db.audit_log.count_documents({"action": "application.viewed"}) == 1


def test_hold_then_resubmit_keeps_reference(client, db):
    app_id, tok = _submitted(client)
    assert client.post(f"/admin/doctors/applications/{app_id}/review", headers=ADMIN).json()["status"] == "under_review"
    assert client.post(f"/admin/doctors/applications/{app_id}/hold", headers=ADMIN, json={}).status_code == 422
    r = client.post(f"/admin/doctors/applications/{app_id}/hold", headers=ADMIN,
                    json={"note": "Registration certificate is blurred, please re-upload"})
    assert r.json()["status"] == "on_hold"

    mine = client.get(f"/doctors/applications/{app_id}", headers=tok).json()
    assert mine["review_note"].startswith("Registration certificate")
    assert client.patch(f"/doctors/applications/{app_id}", headers=tok, json={"step": 6}).status_code == 200
    r = client.post(f"/doctors/applications/{app_id}/submit", headers=tok)
    assert r.json()["status"] == "submitted" and r.json()["reference"] == "SA-DOC-3301"
    emails = [r["template"] for r in db.outbox.find().sort("_id", 1)]
    assert emails == ["application_received", "application_on_hold", "application_received"]


def test_accept_requires_registration_check(client):
    app_id, _ = _submitted(client)
    r = client.post(f"/admin/doctors/applications/{app_id}/accept", headers=ADMIN, json={"registration_verified": False})
    assert r.status_code == 422


def test_accept_creates_login_and_emails_link_not_password(client, db):
    app_id, _ = _submitted(client)
    r = client.post(f"/admin/doctors/applications/{app_id}/accept", headers=ADMIN, json={"registration_verified": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["username"] == "dr.leela.menon"
    assert "password" not in str(body).lower()
    user = db.doctor_users.find_one()
    assert user["password_hash"] is None
    invite = db.outbox.find_one({"template": "doctor_invite"})["payload"]
    assert "/doctor/set-password?token=" in invite["link"]
    # Accepting twice is refused
    assert client.post(f"/admin/doctors/applications/{app_id}/accept", headers=ADMIN,
                       json={"registration_verified": True}).status_code == 409
    assert db.audit_log.count_documents({"action": "application.accepted"}) == 1


def test_same_name_gets_numbered_username(client, db):
    a = onboard_doctor(client, db)
    b = onboard_doctor(client, db, email="leela.m2@gmail.com")
    assert (a["username"], b["username"]) == ("dr.leela.menon", "dr.leela.menon2")


def test_set_password_link_is_single_use_and_login_works(client, db):
    d = onboard_doctor(client, db, password="Maple2891secure")
    # The link used in onboard_doctor can't be reused
    tok = latest_link_token(db, "leela.menon@gmail.com")
    assert client.post("/auth/doctor/set-password", json={"token": tok, "password": "Another123456"}).status_code == 400

    r = client.post("/auth/doctor/login", json={"username": "dr.leela.menon", "password": "Maple2891secure"})
    assert r.status_code == 200 and r.json()["doctor_id"] == d["doctor_id"]
    r = client.post("/auth/doctor/login", json={"username": "LEELA.MENON@gmail.com", "password": "Maple2891secure"})
    assert r.status_code == 200


def test_weak_password_rejected(client, db):
    app_id, _ = _submitted(client)
    client.post(f"/admin/doctors/applications/{app_id}/accept", headers=ADMIN, json={"registration_verified": True})
    tok = latest_link_token(db, "leela.menon@gmail.com")
    assert client.post("/auth/doctor/set-password", json={"token": tok, "password": "short"}).status_code == 422


def test_login_locks_after_repeated_failures(client, db):
    onboard_doctor(client, db)
    for _ in range(5):
        assert client.post("/auth/doctor/login", json={"username": "dr.leela.menon", "password": "wrong"}).status_code == 401
    r = client.post("/auth/doctor/login", json={"username": "dr.leela.menon", "password": "Maple2891secure"})
    assert r.status_code == 423


def test_resend_invite_revokes_old_link(client, db):
    app_id, _ = _submitted(client)
    doctor_id = client.post(f"/admin/doctors/applications/{app_id}/accept", headers=ADMIN,
                            json={"registration_verified": True}).json()["doctor_id"]
    old = latest_link_token(db, "leela.menon@gmail.com")
    assert client.post(f"/admin/doctors/{doctor_id}/resend-invite", headers=ADMIN).status_code == 200
    new = latest_link_token(db, "leela.menon@gmail.com")
    assert client.post("/auth/doctor/set-password", json={"token": old, "password": "Maple2891secure"}).status_code == 400
    assert client.post("/auth/doctor/set-password", json={"token": new, "password": "Maple2891secure"}).status_code == 200


def test_forgot_password_does_not_reveal_accounts(client, db):
    onboard_doctor(client, db)
    assert client.post("/auth/doctor/forgot-password", json={"email": "nobody@example.com"}).status_code == 202
    assert client.post("/auth/doctor/forgot-password", json={"email": "leela.menon@gmail.com"}).status_code == 202
    tok = latest_link_token(db, "leela.menon@gmail.com")
    assert client.post("/auth/doctor/set-password", json={"token": tok, "password": "Newpass12345"}).status_code == 200


def test_doctor_token_cannot_be_forged_for_another_doctor(client, db):
    a = onboard_doctor(client, db)
    forged = bearer("doctor", str(ObjectId()), doctor_id=a["doctor_id"], ver=1)
    assert client.get("/doctor/me", headers=forged).status_code == 401
    assert client.get("/doctor/me", headers=a["headers"]).json()["id"] == a["doctor_id"]
    assert client.get("/doctor/me", headers=MEMBER).status_code == 403
