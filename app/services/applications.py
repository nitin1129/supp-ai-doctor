import re
import unicodedata
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    ApplicationStatus,
    Doctor,
    DoctorApplication,
    DoctorUser,
    PasswordToken,
)
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

EDITABLE = {ApplicationStatus.DRAFT, ApplicationStatus.ON_HOLD}

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


def apply_update(app: DoctorApplication, body: ApplicationUpdate) -> None:
    if app.status not in EDITABLE:
        raise HTTPException(409, f"Application is {app.status} and can no longer be edited")

    data = dict(app.data or {})
    for section in ("personal", "registration", "teleconsult", "compliance"):
        part = getattr(body, section)
        if part is not None:
            merged = {**data.get(section, {}), **part.model_dump(mode="json", exclude_unset=True)}
            data[section] = merged

    if body.bank is not None:
        bank = body.bank.model_dump(mode="json", exclude_unset=True)
        account = bank.pop("account_number", None)
        if account:
            app.bank_account_encrypted = encrypt(account)
            app.bank_account_last4 = account[-4:]
        data["bank"] = {**data.get("bank", {}), **bank}

    if body.identity is not None:
        if body.identity.pan:
            app.pan_encrypted = encrypt(body.identity.pan)
        if body.identity.aadhaar:
            app.aadhaar_last4 = body.identity.aadhaar[-4:]

    app.data = data
    personal = data.get("personal", {})
    app.full_name = personal.get("full_name", app.full_name)
    app.email = (personal.get("email") or app.email or "").lower() or None
    app.mobile = personal.get("mobile", app.mobile)
    if body.step:
        app.current_step = max(app.current_step, body.step)


def missing_items(app: DoctorApplication) -> list[str]:
    data = app.data or {}
    missing = []
    for section, fields in REQUIRED_FIELDS.items():
        values = data.get(section, {})
        for f in fields:
            v = values.get(f)
            if v in (None, "", []) or (section == "compliance" and v is not True):
                missing.append(f"{section}.{f}")
    if not app.pan_encrypted:
        missing.append("identity.pan")
    if not app.aadhaar_last4:
        missing.append("identity.aadhaar")
    if not app.bank_account_encrypted:
        missing.append("bank.account_number")
    tele = data.get("teleconsult", {})
    if "clinic" in (tele.get("modes") or []):
        for f in ("clinic_address", "clinic_pincode"):
            if not tele.get(f):
                missing.append(f"teleconsult.{f}")
    have = {d.kind for d in app.documents}
    missing += [f"documents.{k}" for k in REQUIRED_DOCUMENTS if k not in have]
    return missing


def _docs(app: DoctorApplication, signed: bool) -> list[DocumentOut]:
    return [
        DocumentOut(
            id=d.id,
            kind=d.kind,
            filename=d.filename,
            content_type=d.content_type,
            size_bytes=d.size_bytes,
            uploaded_at=d.uploaded_at,
            url=storage.signed_url(d.storage_key) if signed else None,
        )
        for d in app.documents
    ]


def to_out(app: DoctorApplication) -> ApplicationOut:
    return ApplicationOut(
        id=app.id,
        reference=app.reference,
        status=app.status,
        current_step=app.current_step,
        data=app.data or {},
        pan_masked=mask(decrypt(app.pan_encrypted)) if app.pan_encrypted else None,
        aadhaar_masked=f"XXXXXXXX{app.aadhaar_last4}" if app.aadhaar_last4 else None,
        bank_account_masked=f"XXXX{app.bank_account_last4}" if app.bank_account_last4 else None,
        review_note=app.review_note if app.status in (ApplicationStatus.ON_HOLD, ApplicationStatus.REJECTED) else None,
        submitted_at=app.submitted_at,
        documents=_docs(app, signed=False),
        missing=missing_items(app) if app.status in EDITABLE else [],
    )


def to_admin_out(app: DoctorApplication) -> AdminApplicationOut:
    base = to_out(app).model_dump()
    base["documents"] = _docs(app, signed=True)
    base["review_note"] = app.review_note
    return AdminApplicationOut(
        **base,
        pan=decrypt(app.pan_encrypted) if app.pan_encrypted else None,
        full_name=app.full_name,
        email=app.email,
        mobile=app.mobile,
        reviewed_by=app.reviewed_by,
        reviewed_at=app.reviewed_at,
        doctor_id=app.doctor_id,
    )


def assign_reference(db: Session, app: DoctorApplication) -> None:
    if app.reference:
        return
    n = db.execute(text("SELECT nextval('doctor_application_ref_seq')")).scalar_one()
    app.reference = f"SA-DOC-{n}"


# --- Accepting --------------------------------------------------------------

_TITLES = {"dr", "doctor", "prof", "mr", "mrs", "ms"}


def username_base(full_name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", full_name).encode("ascii", "ignore").decode()
    parts = [p for p in re.split(r"[^a-z]+", ascii_name.lower()) if p and p not in _TITLES]
    return "dr." + ".".join(parts or ["doctor"])


def unique_username(db: Session, full_name: str) -> str:
    base = username_base(full_name)
    taken = set(
        db.scalars(
            select(DoctorUser.username).where(
                (DoctorUser.username == base) | DoctorUser.username.like(f"{base}%")
            )
        )
    )
    if base not in taken:
        return base
    n = 2
    while f"{base}{n}" in taken:
        n += 1
    return f"{base}{n}"


def issue_set_password_link(db: Session, user: DoctorUser, template: str) -> datetime:
    """Revokes earlier unused links and queues a fresh one-time link by email."""
    s = get_settings()
    now = datetime.now(UTC)
    db.query(PasswordToken).filter(
        PasswordToken.doctor_user_id == user.id, PasswordToken.used_at.is_(None)
    ).update({PasswordToken.used_at: now})
    token = new_token()
    expires = now + timedelta(hours=s.invite_link_hours)
    db.add(PasswordToken(doctor_user_id=user.id, token_hash=hash_token(token), expires_at=expires))
    enqueue(
        db,
        channel="email",
        template=template,
        recipient=user.email,
        payload={
            "username": user.username,
            "link": f"{s.public_site_url}/doctor/set-password?token={token}",
            "expires_at": expires.isoformat(),
        },
    )
    return expires


def accept(db: Session, app: DoctorApplication, admin_id: str) -> tuple[Doctor, DoctorUser, datetime]:
    """Create the doctor, their login and the invite in the caller's transaction."""
    s = get_settings()
    if not app.email:
        raise HTTPException(422, "Application has no email")
    if db.scalar(select(func.count()).select_from(DoctorUser).where(DoctorUser.email == app.email)):
        raise HTTPException(409, "A doctor login with this email already exists")

    d = app.data or {}
    personal, reg, tele, bank = (d.get(k, {}) for k in ("personal", "registration", "teleconsult", "bank"))
    now = datetime.now(UTC)

    doctor = Doctor(
        application_id=app.id,
        full_name=app.full_name,
        gender=personal.get("gender"),
        email=app.email,
        mobile=app.mobile,
        specialties=reg.get("specialties") or [],
        qualifications=reg.get("qualifications") or [],
        languages=personal.get("languages") or [],
        modes=tele.get("modes") or ["video"],
        registration_number=reg.get("registration_number"),
        registration_council=tele.get("registration_council"),
        registration_verified_at=now,
        experience_years=tele.get("experience_years") or 0,
        fee_paise=(tele.get("fee_inr") or 0) * 100,
        clinic_address=tele.get("clinic_address"),
        clinic_pincode=tele.get("clinic_pincode"),
        slot_minutes=s.default_slot_minutes,
        notice_minutes=s.default_notice_minutes,
        pan_encrypted=app.pan_encrypted,
        aadhaar_last4=app.aadhaar_last4,
        bank_account_encrypted=app.bank_account_encrypted,
        bank_account_last4=app.bank_account_last4,
        bank_ifsc=bank.get("ifsc"),
    )
    db.add(doctor)
    db.flush()

    user = DoctorUser(doctor_id=doctor.id, username=unique_username(db, app.full_name or "doctor"), email=app.email)
    db.add(user)
    db.flush()

    expires = issue_set_password_link(db, user, template="doctor_invite")

    app.status = ApplicationStatus.ACCEPTED
    app.doctor_id = doctor.id
    app.reviewed_at = now
    app.reviewed_by = admin_id
    return doctor, user, expires
