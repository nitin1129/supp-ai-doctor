from app.config import get_settings
from tests.conftest import onboard_doctor


def _create(client):
    return client.post("/doctors/applications", json={})


def test_application_create_limited_per_ip(client):
    limit = int(get_settings().rate_limit_applications.split("/")[0])
    for _ in range(limit):
        assert _create(client).status_code == 201
    r = _create(client)
    assert r.status_code == 429
    assert r.json() == {"detail": "Too many requests. Please wait a minute and try again."}


def test_application_submit_limited_per_ip(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "rate_limit_applications", "2/minute")
    r = _create(client)
    app_id, token = r.json()["id"], r.json()["resume_token"]
    url, headers = f"/doctors/applications/{app_id}/submit", {"X-Application-Token": token}
    assert client.post(url, headers=headers).status_code == 422  # incomplete, but counted
    assert client.post(url, headers=headers).status_code == 422
    assert client.post(url, headers=headers).status_code == 429


def test_login_limited_per_ip(client, db):
    d = onboard_doctor(client, db)
    limit = int(get_settings().rate_limit_login.split("/")[0])
    body = {"username": d["username"], "password": "Maple2891secure"}
    for _ in range(limit):
        assert client.post("/auth/doctor/login", json=body).status_code == 200
    assert client.post("/auth/doctor/login", json=body).status_code == 429


def test_forgot_password_limited_per_ip(client):
    limit = int(get_settings().rate_limit_forgot_password.split("/")[0])
    for _ in range(limit):
        assert client.post("/auth/doctor/forgot-password", json={"email": "a@example.com"}).status_code == 202
    assert client.post("/auth/doctor/forgot-password", json={"email": "b@example.com"}).status_code == 429


def test_limits_are_configurable_and_separate_per_endpoint(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "rate_limit_applications", "1/minute")
    assert _create(client).status_code == 201
    assert _create(client).status_code == 429
    # Other endpoints keep their own counters
    assert client.post("/auth/doctor/forgot-password", json={"email": "a@example.com"}).status_code == 202


def test_limits_are_per_ip(client, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setattr(get_settings(), "rate_limit_applications", "1/minute")
    assert _create(client).status_code == 201
    assert _create(client).status_code == 429
    other = TestClient(app, client=("203.0.113.9", 50000))
    assert _create(other).status_code == 201
