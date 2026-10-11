from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, select

from app.config import get_settings
from app.deps import DB
from app.models import DoctorStatus, DoctorUser, PasswordToken
from app.security import (
    create_access_token,
    hash_password,
    hash_token,
    validate_password_strength,
    verify_password,
)
from app.services.applications import issue_set_password_link
from app.services.audit import audit

router = APIRouter(prefix="/auth/doctor", tags=["Doctor auth"])


class LoginIn(BaseModel):
    username: str  # username or email
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    doctor_id: int
    username: str
    expires_in: int


class SetPasswordIn(BaseModel):
    token: str
    password: str


class ForgotIn(BaseModel):
    email: EmailStr


def _token_for(user: DoctorUser) -> TokenOut:
    s = get_settings()
    return TokenOut(
        access_token=create_access_token(
            {"sub": str(user.id), "role": "doctor", "doctor_id": user.doctor_id, "ver": user.token_version}
        ),
        doctor_id=user.doctor_id,
        username=user.username,
        expires_in=s.access_token_minutes * 60,
    )


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, db: DB) -> TokenOut:
    s = get_settings()
    ident = body.username.strip().lower()
    user = db.scalar(
        select(DoctorUser).where((DoctorUser.username == ident) | (func.lower(DoctorUser.email) == ident))
    )
    now = datetime.now(UTC)
    bad = HTTPException(401, "Wrong username or password")
    if user is None:
        raise bad
    if user.locked_until and user.locked_until > now:
        raise HTTPException(423, "Too many attempts. Try again later or reset your password.")
    if user.password_hash is None:
        raise HTTPException(403, "Set your password from the invite email first")
    if not verify_password(body.password, user.password_hash):
        user.failed_logins += 1
        if user.failed_logins >= s.login_max_failures:
            user.locked_until = now + timedelta(minutes=s.login_lock_minutes)
            user.failed_logins = 0
        db.commit()
        raise bad
    if user.doctor.status != DoctorStatus.ACTIVE:
        raise HTTPException(403, "Your account is suspended. Contact SuppAI support.")
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = now
    db.commit()
    return _token_for(user)


@router.post("/set-password", response_model=TokenOut)
def set_password(body: SetPasswordIn, db: DB) -> TokenOut:
    """First login from the invite link, or a reset link. Signs the doctor in."""
    now = datetime.now(UTC)
    pt = db.scalar(select(PasswordToken).where(PasswordToken.token_hash == hash_token(body.token)))
    if pt is None or pt.used_at is not None or pt.expires_at < now:
        raise HTTPException(400, "This link has expired or was already used. Ask for a new one.")
    try:
        validate_password_strength(body.password)
    except ValueError as e:
        raise HTTPException(422, str(e))
    user = db.get(DoctorUser, pt.doctor_user_id)
    user.password_hash = hash_password(body.password)
    user.password_set_at = now
    user.token_version += 1  # signs out any other sessions
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = now
    pt.used_at = now
    audit(db, actor_type="doctor", actor_id=str(user.id), action="doctor.password_set",
          entity_type="doctor", entity_id=user.doctor_id)
    db.commit()
    return _token_for(user)


@router.post("/forgot-password", status_code=202)
def forgot_password(body: ForgotIn, db: DB) -> dict:
    """Always answers 202 so the endpoint can't be used to find registered emails."""
    user = db.scalar(select(DoctorUser).where(func.lower(DoctorUser.email) == body.email.lower()))
    if user is not None and user.doctor.status == DoctorStatus.ACTIVE:
        issue_set_password_link(db, user, template="doctor_password_reset")
        db.commit()
    return {"message": "If that email is registered, a reset link is on its way."}
