import re
import unicodedata
from datetime import datetime, timedelta

from fastapi import HTTPException
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from app.config import get_settings
from app.db import next_sequence, now
from app.models import ApplicationStatus, DoctorStatus
from app.schemas.applications import (
    REQUIRED_DOCUMENTS,
    AdminApplicationOut,
    ApplicationOut,
    ApplicationUpdate,
    DocumentOut,
)
from app.security import decrypt, encrypt, hash_token, mask, new_token
from app.services import storage
from app.services.outbox import enqueue

EDITABLE = [ApplicationStatus.DRAFT.value, ApplicationStatus.ON_HOLD.value]
REVIEWABLE = [ApplicationStatus.SUBMITTED.value, ApplicationStatus.UNDER_REVIEW.value]
REFERENCE_OFFSET = 3300  # first reference is SA-DOC-3301

REQUIRED_FIELDS: dict[str, list[str]] = {
    "personal": ["full_name", "gender", "date_of_birth", "mobile", "email", "languages", "address"],
    "registration": ["registration_number", "qualifications", "specialties"],
    "teleconsult": ["registration_council", "experience_years", "modes", "fee_inr"],
    "bank": ["account_holder", "ifsc"],
    "compliance": [
        "info_is_true",
        "follows_telemedicine_guidelines",
        "consents_to_verification",
        "accepts_terms",
    ],
}


def new_application(token: str) -> dict:
    t = now()
    return {
        "reference": None,
        "status": ApplicationStatus.DRAFT.value,
        "resume_token_hash": hash_token(token),
        "current_step": 1,
        "data": {},
        "documents": [],
        "full_name": None,
        "email": None,
        "mobile": None,
        "pan_encrypted": None,
        "aadhaar_last4": None,
        "bank_account_encrypted": None,
        "bank_account_last4": None,
        "review_note": None,
        "submitted_at": None,
        "reviewed_at": None,
        "reviewed_by": None,
        "doctor_id": None,
        "created_at": t,
        "updated_at": t,
    }


def apply_update(app: dict, body: ApplicationUpdate) -> dict:
    """Applies the update to `app` in place and returns the fields to $set."""
    if app["status"] not in EDITABLE:
        raise HTTPException(409, f"Application is {app['status']} and can no longer be edited")
    changes: dict = {}
    data = dict(app.get("data") or {})
    for section in ("personal", "registration", "teleconsult", "compliance"):
        part = getattr(body, section)
        if part is not None:
            data[section] = {**data.get(section, {}), **part.model_dump(mode="json", exclude_unset=True)}

    if body.bank is not None:
        bank = body.bank.model_dump(mode="json", exclude_unset=True)
        account = bank.pop("account_number", None)
        if account:
            changes["bank_account_encrypted"] = encrypt(account)
            changes["bank_account_last4"] = account[-4:]
        data["bank"] = {**data.get("bank", {}), **bank}

    if body.identity is not None:
        if body.identity.pan:
            changes["pan_encrypted"] = encrypt(body.identity.pan)
        if body.identity.aadhaar:
            changes["aadhaar_last4"] = body.identity.aadhaar[-4:]

    changes["data"] = data
    personal = data.get("personal", {})
    changes["full_name"] = personal.get("full_name", app.get("full_name"))
    changes["email"] = (personal.get("email") or app.get("email") or "").lower() or None
    changes["mobile"] = personal.get("mobile", app.get("mobile"))
    if body.step:
        changes["current_step"] = max(app.get("current_step", 1), body.step)
    changes["updated_at"] = now()
    app.update(changes)
    return changes


def missing_items(app: dict) -> list[str]:
    data = app.get("data") or {}
    missing = []
    for section, fields in REQUIRED_FIELDS.items():
        values = data.get(section, {})
        for f in fields:
            v = values.get(f)
            if v in (None, "", []) or (section == "compliance" and v is not True):
                missing.append(f"{section}.{f}")
    if not app.get("pan_encrypted"):
        missing.append("identity.pan")
    if not app.get("aadhaar_last4"):
        missing.append("identity.aadhaar")
    if not app.get("bank_account_encrypted"):
        missing.append("bank.account_number")
    tele = data.get("teleconsult", {})
    if "clinic" in (tele.get("modes") or []):
        for f in ("clinic_address", "clinic_pincode"):
            if not tele.get(f):
                missing.append(f"teleconsult.{f}")
    have = {d["kind"] for d in app.get("documents", [])}
    missing += [f"documents.{k}" for k in REQUIRED_DOCUMENTS if k not in have]
    return missing


def document_out(d: dict, signed: bool = False) -> DocumentOut:
    return DocumentOut(
        id=str(d["id"]),
        kind=d["kind"],
        filename=d["filename"],
        content_type=d["content_type"],
        size_bytes=d["size_bytes"],
        uploaded_at=d["uploaded_at"],
        url=storage.signed_url(d["storage_key"]) if signed else None,
    )


def to_out(app: dict) -> ApplicationOut:
    held = app["status"] in (ApplicationStatus.ON_HOLD, ApplicationStatus.REJECTED)
    return ApplicationOut(
        id=str(app["_id"]),
        reference=app.get("reference"),
        status=app["status"],
        current_step=app.get("current_step", 1),
        data=app.get("data") or {},
        pan_masked=mask(decrypt(app["pan_encrypted"])) if app.get("pan_encrypted") else None,
        aadhaar_masked=f"XXXXXXXX{app['aadhaar_last4']}" if app.get("aadhaar_last4") else None,
        bank_account_masked=f"XXXX{app['bank_account_last4']}" if app.get("bank_account_last4") else None,
        review_note=app.get("review_note") if held else None,
        submitted_at=app.get("submitted_at"),
        documents=[document_out(d) for d in app.get("documents", [])],
        missing=missing_items(app) if app["status"] in EDITABLE else [],
    )


def to_admin_out(app: dict) -> AdminApplicationOut:
    base = to_out(app).model_dump()
    base["documents"] = [document_out(d, signed=True) for d in app.get("documents", [])]
    base["review_note"] = app.get("review_note")
    return AdminApplicationOut(
        **base,
        pan=decrypt(app["pan_encrypted"]) if app.get("pan_encrypted") else None,
        full_name=app.get("full_name"),
        email=app.get("email"),
        mobile=app.get("mobile"),
        reviewed_by=app.get("reviewed_by"),
        reviewed_at=app.get("reviewed_at"),
        doctor_id=str(app["doctor_id"]) if app.get("doctor_id") else None,
    )


def reference_for(db: Database, app: dict) -> str:
    return app.get("reference") or f"SA-DOC-{REFERENCE_OFFSET + next_sequence(db, 'doctor_application_ref')}"


# --- Accepting --------------------------------------------------------------

_TITLES = {"dr", "doctor", "prof", "mr", "mrs", "ms"}


def username_base(full_name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", full_name).encode("ascii", "ignore").decode()
    parts = [p for p in re.split(r"[^a-z]+", ascii_name.lower()) if p and p not in _TITLES]
    return "dr." + ".".join(parts or ["doctor"])


def _username_candidates(db: Database, full_name: str):
    base = username_base(full_name)
    taken = {u["username"] for u in db.doctor_users.find({"username": {"$regex": f"^{re.escape(base)}\\d*$"}}, {"username": 1})}
    if base not in taken:
        yield base
    n = 2
    while True:
        if f"{base}{n}" not in taken:
            yield f"{base}{n}"
        n += 1


def issue_set_password_link(db: Database, user: dict, template: str) -> datetime:
    """Revokes earlier unused links and queues a fresh one-time link by email."""
    s = get_settings()
    t = now()
    db.password_tokens.update_many({"doctor_user_id": user["_id"], "used_at": None}, {"$set": {"used_at": t}})
    token = new_token()
    expires = t + timedelta(hours=s.invite_link_hours)
    db.password_tokens.insert_one(
        {"doctor_user_id": user["_id"], "token_hash": hash_token(token), "expires_at": expires, "used_at": None,
         "created_at": t}
    )
    enqueue(
        db,
        channel="email",
        template=template,
        recipient=user["email"],
        payload={
            "username": user["username"],
            "link": f"{s.public_site_url}/doctor/set-password?token={token}",
            "expires_at": expires.isoformat(),
        },
    )
    return expires


def build_doctor(app: dict) -> dict:
    s = get_settings()
    d = app.get("data") or {}
    personal, reg, tele, bank = (d.get(k, {}) for k in ("personal", "registration", "teleconsult", "bank"))
    t = now()
    return {
        "application_id": app["_id"],
        "status": DoctorStatus.ACTIVE.value,
        "status_note": None,
        "full_name": app["full_name"],
        "gender": personal.get("gender"),
        "email": app["email"],
        "mobile": app.get("mobile"),
        "specialties": reg.get("specialties") or [],
        "qualifications": reg.get("qualifications") or [],
        "languages": personal.get("languages") or [],
        "modes": tele.get("modes") or ["video"],
        "registration_number": reg.get("registration_number"),
        "registration_council": tele.get("registration_council"),
        "registration_verified_at": t,
        "experience_years": tele.get("experience_years") or 0,
        "bio": None,
        "fee_paise": (tele.get("fee_inr") or 0) * 100,
        "clinic_address": tele.get("clinic_address"),
        "clinic_pincode": tele.get("clinic_pincode"),
        "hours": [],
        "slot_minutes": s.default_slot_minutes,
        "notice_minutes": s.default_notice_minutes,
        "rating_sum": 0,
        "rating_count": 0,
        "pan_encrypted": app.get("pan_encrypted"),
        "aadhaar_last4": app.get("aadhaar_last4"),
        "bank_account_encrypted": app.get("bank_account_encrypted"),
        "bank_account_last4": app.get("bank_account_last4"),
        "bank_ifsc": bank.get("ifsc"),
        "lock_until": None,
        "lock_token": None,
        "created_at": t,
    }


def accept(db: Database, app_id, admin_id: str) -> tuple[dict, dict, datetime]:
    """Creates the doctor, their login and the invite.

    The application is claimed first with a conditional update, so two admins
    can't accept it twice. If a later step fails, earlier ones are undone.
    """
    t = now()
    app = db.doctor_applications.find_one_and_update(
        {"_id": app_id, "status": {"$in": REVIEWABLE}, "accepting_at": None},
        {"$set": {"accepting_at": t}},
    )
    if app is None:
        current = db.doctor_applications.find_one({"_id": app_id}, {"status": 1})
        if current is None:
            raise HTTPException(404, "Application not found")
        raise HTTPException(409, f"Application is {current['status']}")

    doctor_id = user_id = None
    try:
        if not app.get("email"):
            raise HTTPException(422, "Application has no email")
        if db.doctor_users.count_documents({"email": app["email"]}, limit=1):
            raise HTTPException(409, "A doctor login with this email already exists")

        doctor = build_doctor(app)
        doctor_id = db.doctors.insert_one(doctor).inserted_id

        user = None
        for username in _username_candidates(db, app["full_name"] or "doctor"):
            user = {"doctor_id": doctor_id, "username": username, "email": app["email"], "password_hash": None,
                    "password_set_at": None, "failed_logins": 0, "locked_until": None, "last_login_at": None,
                    "token_version": 0, "created_at": t}
            try:
                user_id = db.doctor_users.insert_one(user).inserted_id
                break
            except DuplicateKeyError as e:
                if "email" in str(e):
                    raise HTTPException(409, "A doctor login with this email already exists")
                continue  # someone took this username a moment ago; try the next number

        expires = issue_set_password_link(db, user, template="doctor_invite")
        db.doctor_applications.update_one(
            {"_id": app_id},
            {"$set": {"status": ApplicationStatus.ACCEPTED.value, "doctor_id": doctor_id, "reviewed_at": t,
                      "reviewed_by": admin_id, "updated_at": t}, "$unset": {"accepting_at": ""}},
        )
        return doctor, user, expires
    except BaseException:
        if user_id is not None:
            db.doctor_users.delete_one({"_id": user_id})
            db.password_tokens.delete_many({"doctor_user_id": user_id})
        if doctor_id is not None:
            db.doctors.delete_one({"_id": doctor_id})
        db.doctor_applications.update_one({"_id": app_id}, {"$unset": {"accepting_at": ""}})
        raise
