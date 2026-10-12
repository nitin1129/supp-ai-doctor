import inspect

import pytest
from bson import ObjectId

from app.deps import require_doctor
from app.routers import admin, applications, auth, doctor, member_auth, members, payments
from app.security import decode_access_token
from tests.conftest import ADMIN, MEMBER, bearer, onboard_doctor


def _doctor_routes():
    """Every route that authenticates with require_doctor, with path params filled in."""
    out = []
    routers = (admin, applications, auth, doctor, member_auth, members, payments)
    for route in [r for m in routers for r in m.router.routes]:
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        calls = []
        stack = list(dependant.dependencies)
        while stack:
            d = stack.pop()
            calls.append(d.call)
            stack.extend(d.dependencies)
        if require_doctor in calls:
            path = route.path
            for p in dependant.path_params:
                path = path.replace("{" + p.name + "}", str(ObjectId()))
            for method in route.methods:
                out.append((method, path, route))
    return out


DOCTOR_ROUTES = _doctor_routes()


def test_doctor_routes_were_found():
    paths = {p for _, p, _ in DOCTOR_ROUTES}
    assert any(p == "/doctor/me" for p in paths) and len(DOCTOR_ROUTES) >= 10


def test_login_token_carries_role_and_doctor_id(client, db):
    d = onboard_doctor(client, db)
    r = client.post("/auth/doctor/login", json={"username": d["username"], "password": "Maple2891secure"})
    assert r.status_code == 200, r.text
    claims = decode_access_token(r.json()["access_token"])
    user = db.doctor_users.find_one({"doctor_id": ObjectId(d["doctor_id"])})
    assert claims["role"] == "doctor"
    assert claims["doctor_id"] == d["doctor_id"] == r.json()["doctor_id"]
    assert claims["sub"] == str(user["_id"])
    assert claims["ver"] == user["token_version"]


def test_no_doctor_route_takes_doctor_id_from_the_request():
    for _, path, route in DOCTOR_ROUTES:
        d = route.dependant
        names = {p.name for p in d.path_params + d.query_params + d.header_params + d.cookie_params}
        names |= {p.alias for p in d.path_params + d.query_params + d.header_params + d.cookie_params}
        assert "doctor_id" not in names, path
        for p in d.body_params:
            ann = p.field_info.annotation
            fields = getattr(ann, "model_fields", {}) if inspect.isclass(ann) else {}
            assert "doctor_id" not in fields, path


@pytest.mark.parametrize("who", ["member", "admin", "member_with_doctor_claims", "admin_with_doctor_claims"])
def test_member_and_admin_tokens_cannot_call_doctor_endpoints(client, db, who):
    d = onboard_doctor(client, db)
    user = db.doctor_users.find_one({"doctor_id": ObjectId(d["doctor_id"])})
    # Even a token that copies a real doctor's claims is refused unless role is "doctor".
    copied = {"doctor_id": d["doctor_id"], "ver": user["token_version"]}
    headers = {
        "member": MEMBER,
        "admin": ADMIN,
        "member_with_doctor_claims": bearer("member", str(user["_id"]), **copied),
        "admin_with_doctor_claims": bearer("admin", str(user["_id"]), **copied),
    }[who]
    for method, path, _ in DOCTOR_ROUTES:
        r = client.request(method, path, headers=headers, json={})
        assert r.status_code == 403, (method, path, r.status_code, r.text)


def test_doctor_token_without_doctor_id_is_refused(client, db):
    d = onboard_doctor(client, db)
    user = db.doctor_users.find_one({"doctor_id": ObjectId(d["doctor_id"])})
    no_claim = bearer("doctor", str(user["_id"]), ver=user["token_version"])
    assert client.get("/doctor/me", headers=no_claim).status_code == 403
    assert client.get("/doctor/me", headers=d["headers"]).status_code == 200


def test_doctor_id_in_request_is_ignored(client, db):
    a = onboard_doctor(client, db)
    b = onboard_doctor(client, db, name="Dr. Arjun Rao", email="arjun@x.in")
    r = client.get("/doctor/me", headers=a["headers"], params={"doctor_id": b["doctor_id"]})
    assert r.json()["id"] == a["doctor_id"]
    r = client.put("/doctor/me", headers=a["headers"], json={"doctor_id": b["doctor_id"], "bio": "Hello"})
    assert r.json()["id"] == a["doctor_id"]
    assert db.doctors.find_one({"_id": ObjectId(b["doctor_id"])}).get("bio") is None
