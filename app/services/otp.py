"""OTP issuance, verification, and rate-limiting via MongoDB.

All rate-limit counters live in MongoDB so they work across multiple
processes and are automatically reset when collections are dropped in tests.
"""

import hashlib
import hmac
import secrets
from datetime import timedelta

from fastapi import HTTPException
from pymongo import ASCENDING, ReturnDocument
from pymongo.database import Database

from app.config import get_settings
from app.db import now
from app.services.notify import send_email

_PURPOSE_LABELS = {
    "doctor_application": "your doctor application on SuppAI",
    "doctor_login": "signing in to your SuppAI doctor account",
    "member_login": "signing in to your SuppAI account",
}


def _deliver(to: str, subject: str, text: str) -> None:
    send_email(to, "", subject, text)


def _hmac(purpose: str, email: str, code: str) -> str:
    s = get_settings()
    key = s.jwt_secret.encode()
    msg = f"{purpose}:{email}:{code}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def _check_rate_limits(db: Database, email: str, purpose: str, ip: str) -> None:
    s = get_settings()
    t = now()

    # 1 code per email+purpose per resend window
    since_resend = t - timedelta(seconds=s.otp_resend_seconds)
    recent = db.otp_codes.find_one(
        {"email": email, "purpose": purpose, "created_at": {"$gt": since_resend}},
        sort=[("created_at", -1)],
    )
    if recent is not None:
        elapsed = (t - recent["created_at"]).total_seconds()
        retry_after = max(1, round(s.otp_resend_seconds - elapsed))
        raise HTTPException(429, {"message": "Please wait before requesting another code.", "retry_after": retry_after})

    since_hour = t - timedelta(hours=1)

    # 5 codes per email per hour
    if db.otp_codes.count_documents({"email": email, "created_at": {"$gt": since_hour}}) >= s.otp_max_per_email_hour:
        raise HTTPException(429, {"message": "Too many codes sent to this address. Try again in an hour.", "retry_after": 3600})

    # 20 codes per IP per hour
    if db.otp_codes.count_documents({"ip": ip, "created_at": {"$gt": since_hour}}) >= s.otp_max_per_ip_hour:
        raise HTTPException(429, {"message": "Too many requests from this IP. Try again in an hour.", "retry_after": 3600})


def _check_verify_rate_limit(db: Database, ip: str) -> None:
    s = get_settings()
    t = now()
    since = t - timedelta(minutes=15)
    count = db.otp_verify_attempts.count_documents({"ip": ip, "at": {"$gt": since}})
    if count >= s.otp_max_verify_per_ip_15min:
        raise HTTPException(429, {"message": "Too many verification attempts. Try again later.", "retry_after": 900})
    db.otp_verify_attempts.insert_one({"ip": ip, "at": t})


def issue(
    db: Database,
    email: str,
    purpose: str,
    ip: str,
    send: bool = True,
    context: dict | None = None,
) -> dict:
    s = get_settings()
    email = email.strip().lower()
    t = now()

    _check_rate_limits(db, email, purpose, ip)

    code = str(secrets.randbelow(1_000_000)).zfill(6)
    code_hash = _hmac(purpose, email, code)
    expires_at = t + timedelta(minutes=s.otp_ttl_minutes)

    # Cancel earlier unused codes for the same email+purpose
    db.otp_codes.update_many(
        {"email": email, "purpose": purpose, "used_at": None},
        {"$set": {"used_at": t}},
    )

    record = {
        "email": email,
        "purpose": purpose,
        "code_hash": code_hash,
        "expires_at": expires_at,
        "attempts": 0,
        "used_at": None,
        "created_at": t,
        "ip": ip,
        "context": context,
        "sent": send,
    }
    db.otp_codes.insert_one(record)

    if send:
        label = _PURPOSE_LABELS.get(purpose, "SuppAI")
        subject = f"{code} is your SuppAI code"
        text = (
            f"Your one-time code for {label} is:\n\n"
            f"    {code}\n\n"
            f"This code expires in {s.otp_ttl_minutes} minutes, works once only, "
            f"and is valid only for this request.\n\n"
            "Never share this code with anyone — SuppAI will never ask for it.\n\n"
            "If you did not request this code, you can safely ignore this message."
        )
        try:
            _deliver(email, subject, text)
        except Exception:
            db.otp_codes.delete_one({"_id": record["_id"]})
            raise HTTPException(502, "We couldn't send the email just now. Please try again in a minute.")

    return record


def verify(db: Database, email: str, purpose: str, code: str, ip: str) -> dict:
    s = get_settings()
    email = email.strip().lower()
    t = now()

    _check_verify_rate_limit(db, ip)

    err = HTTPException(400, "The code is wrong or has expired. Request a new one if needed.")

    expected_hash = _hmac(purpose, email, code.strip())

    # Only sent codes can be verified; find the newest unused, unexpired one
    record = db.otp_codes.find_one(
        {"email": email, "purpose": purpose, "used_at": None, "expires_at": {"$gt": t}, "sent": True},
        sort=[("created_at", -1)],
    )
    if record is None:
        raise err

    if not hmac.compare_digest(record["code_hash"], expected_hash):
        updated = db.otp_codes.find_one_and_update(
            {"_id": record["_id"], "used_at": None},
            {"$inc": {"attempts": 1}},
            return_document=ReturnDocument.AFTER,
        )
        if updated and updated["attempts"] >= s.otp_max_attempts:
            db.otp_codes.update_one({"_id": record["_id"]}, {"$set": {"used_at": t}})
        raise err

    # Claim atomically — prevents double-use in concurrent requests
    claimed = db.otp_codes.find_one_and_update(
        {"_id": record["_id"], "used_at": None},
        {"$set": {"used_at": t}},
    )
    if claimed is None:
        raise err

    return record


def ensure_indexes(db: Database) -> None:
    s = get_settings()

    db.otp_codes.create_index([("created_at", ASCENDING)], expireAfterSeconds=3600, name="ttl_otp_codes")
    db.otp_verify_attempts.create_index([("at", ASCENDING)], expireAfterSeconds=3600, name="ttl_otp_verify")
    db.otp_codes.create_index(
        [("email", ASCENDING), ("purpose", ASCENDING), ("created_at", ASCENDING)],
        name="otp_codes_email_purpose_created",
    )
    db.otp_codes.create_index([("ip", ASCENDING), ("created_at", ASCENDING)], name="otp_codes_ip_created")

    try:
        db[s.members_collection].create_index(
            [(s.member_email_field, ASCENDING)],
            unique=True,
            partialFilterExpression={s.member_email_field: {"$type": "string"}},
            name="uq_members_email",
        )
    except Exception:
        pass  # duplicates may already exist; index is advisory
