from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr
from pymongo import ReturnDocument

from app.config import get_settings
from app.db import now
from app.deps import DB, client_ip
from app.models import DoctorStatus
from app.ratelimit import forgot_password_limit, limiter, login_limit
from app.security import (
    create_access_token,
    hash_password,
    hash_token,
    validate_password_strength,
    verify_password,
)
from app.services import otp
from app.services.applications import issue_set_password_link
from app.services.audit import audit

router = APIRouter(prefix="/auth/doctor", tags=["Doctor auth"])


class LoginIn(BaseModel):
    username: str  # username or email
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    doctor_id: str
    username: str
    expires_in: int


class SetPasswordIn(BaseModel):
    token: str
    password: str


class ForgotIn(BaseModel):
    email: EmailStr


def _token_for(user: dict) -> TokenOut:
    s = get_settings()
    return TokenOut(
        access_token=create_access_token({
            "sub": str(user["_id"]), "role": "doctor", "doctor_id": str(user["doctor_id"]),
            "ver": user.get("token_version", 0),
        }),
        doctor_id=str(user["doctor_id"]),
        username=user["username"],
        expires_in=s.access_token_minutes * 60,
    )


def _doctor_active(db: DB, user: dict) -> bool:
    d = db.doctors.find_one({"_id": user["doctor_id"]}, {"status": 1})
    return d is not None and d["status"] == DoctorStatus.ACTIVE


@router.post("/login", response_model=TokenOut)
@limiter.limit(login_limit)
def login(request: Request, body: LoginIn, db: DB) -> TokenOut:
    s = get_settings()
    ident = body.username.strip().lower()
    # Emails are stored lower-case; usernames are always lower-case.
    user = db.doctor_users.find_one({"$or": [{"username": ident}, {"email": ident}]})
    t = now()
    bad = HTTPException(401, "Wrong username or password")
    if user is None:
        raise bad
    if user.get("locked_until") and user["locked_until"] > t:
        raise HTTPException(423, "Too many attempts. Try again later or reset your password.")
    if not user.get("password_hash"):
        raise HTTPException(403, "Set your password from the invite email first")
    if not verify_password(body.password, user["password_hash"]):
        updated = db.doctor_users.find_one_and_update(
            {"_id": user["_id"]}, {"$inc": {"failed_logins": 1}}, return_document=ReturnDocument.AFTER
        )
        if updated["failed_logins"] >= s.login_max_failures:
            db.doctor_users.update_one({"_id": user["_id"]}, {"$set": {
                "locked_until": t + timedelta(minutes=s.login_lock_minutes), "failed_logins": 0}})
        raise bad
    if not _doctor_active(db, user):
        raise HTTPException(403, "Your account is suspended. Contact SuppAI support.")
    db.doctor_users.update_one({"_id": user["_id"]},
                               {"$set": {"failed_logins": 0, "locked_until": None, "last_login_at": t}})
    return _token_for(user)


@router.post("/set-password", response_model=TokenOut)
def set_password(body: SetPasswordIn, db: DB) -> TokenOut:
    """First login from the invite link, or a reset link. Signs the doctor in."""
    try:
        validate_password_strength(body.password)
    except ValueError as e:
        raise HTTPException(422, str(e))
    t = now()
    # Claim the link atomically so it can only be used once.
    pt = db.password_tokens.find_one_and_update(
        {"token_hash": hash_token(body.token), "used_at": None, "expires_at": {"$gt": t}},
        {"$set": {"used_at": t}},
    )
    if pt is None:
        raise HTTPException(400, "This link has expired or was already used. Ask for a new one.")
    user = db.doctor_users.find_one_and_update(
        {"_id": pt["doctor_user_id"]},
        {"$set": {"password_hash": hash_password(body.password), "password_set_at": t, "failed_logins": 0,
                  "locked_until": None, "last_login_at": t},
         "$inc": {"token_version": 1}},  # signs out any other sessions
        return_document=ReturnDocument.AFTER,
    )
    audit(db, actor_type="doctor", actor_id=str(user["_id"]), action="doctor.password_set",
          entity_type="doctor", entity_id=user["doctor_id"])
    return _token_for(user)


@router.post("/forgot-password", status_code=202)
@limiter.limit(forgot_password_limit)
def forgot_password(request: Request, body: ForgotIn, db: DB) -> dict:
    """Always answers 202 so the endpoint can't be used to find registered emails."""
    user = db.doctor_users.find_one({"email": body.email.lower()})
    if user is not None and _doctor_active(db, user):
        issue_set_password_link(db, user, template="doctor_password_reset")
    return {"message": "If that email is registered, a reset link is on its way."}


class OtpRequestIn(BaseModel):
    email: EmailStr


class OtpVerifyIn(BaseModel):
    email: EmailStr
    code: str


@router.post("/otp/request", status_code=202)
def otp_request(body: OtpRequestIn, db: DB, request: Request) -> dict:
    """Send a sign-in code. Always 202 — reveals nothing about registered emails."""
    email = body.email.strip().lower()
    ip = client_ip(request)
    user = db.doctor_users.find_one({"email": email})
    send = user is not None and _doctor_active(db, user)
    otp.issue(db, email, "doctor_login", ip, send=send)
    return {"message": "If that email is registered and your account is active, a sign-in code is on its way."}


@router.post("/otp/verify", response_model=TokenOut)
def otp_verify(body: OtpVerifyIn, db: DB, request: Request) -> TokenOut:
    """Exchange a sign-in code for a bearer token."""
    email = body.email.strip().lower()
    ip = client_ip(request)
    otp.verify(db, email, "doctor_login", body.code, ip)
    user = db.doctor_users.find_one({"email": email})
    if user is None or not _doctor_active(db, user):
        raise HTTPException(400, "The code is wrong or has expired. Request a new one if needed.")
    t = now()
    db.doctor_users.update_one(
        {"_id": user["_id"]},
        {"$set": {"failed_logins": 0, "locked_until": None, "last_login_at": t}},
    )
    return _token_for(user)

