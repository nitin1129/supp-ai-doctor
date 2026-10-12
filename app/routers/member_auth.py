"""Member (patient) authentication via email OTP."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr
from pymongo.errors import DuplicateKeyError

from app.config import get_settings
from app.db import now
from app.deps import DB, client_ip
from app.security import create_access_token
from app.services import otp

router = APIRouter(prefix="/auth/member", tags=["Member auth"])


class OtpRequestIn(BaseModel):
    email: EmailStr


class OtpVerifyIn(BaseModel):
    email: EmailStr
    code: str
    name: str | None = None


class MemberOut(BaseModel):
    id: str
    email: str
    name: str | None
    is_new: bool


class MemberTokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    member: MemberOut


@router.post("/otp/request", status_code=202)
def otp_request(body: OtpRequestIn, db: DB, request: Request) -> dict:
    """Send a sign-in / sign-up code. Always 202."""
    s = get_settings()
    email = body.email.strip().lower()
    ip = client_ip(request)
    member = db[s.members_collection].find_one({s.member_email_field: email}, {"_id": 1})
    send = s.member_otp_signup or member is not None
    otp.issue(db, email, "member_login", ip, send=send)
    return {"message": "If sign-up is open or your account exists, a code is on its way."}


@router.post("/otp/verify", response_model=MemberTokenOut)
def otp_verify(body: OtpVerifyIn, db: DB, request: Request) -> MemberTokenOut:
    """Exchange a code for a bearer token, creating the account on first use."""
    s = get_settings()
    email = body.email.strip().lower()
    ip = client_ip(request)

    otp.verify(db, email, "member_login", body.code, ip)

    t = now()
    collection = db[s.members_collection]
    member = collection.find_one({s.member_email_field: email})

    is_new = False
    if member is None:
        if not s.member_otp_signup:
            raise HTTPException(400, "The code is wrong or has expired. Request a new one if needed.")
        new_doc = {
            s.member_email_field: email,
            s.member_name_field: body.name,
            "email_verified_at": t,
            "created_at": t,
            "created_via": "email_otp",
        }
        try:
            result = collection.insert_one(new_doc)
            new_doc["_id"] = result.inserted_id
            member = new_doc
            is_new = True
        except DuplicateKeyError:
            # Race condition: another request created the member first
            member = collection.find_one({s.member_email_field: email})
            if member is None:
                raise HTTPException(500, "Unexpected error during member creation")
    else:
        collection.update_one(
            {"_id": member["_id"]},
            {"$set": {"email_verified_at": t, "last_login_at": t}},
        )

    token = create_access_token({"sub": str(member["_id"]), "role": "member"})
    return MemberTokenOut(
        access_token=token,
        expires_in=s.access_token_minutes * 60,
        member=MemberOut(
            id=str(member["_id"]),
            email=member[s.member_email_field],
            name=member.get(s.member_name_field),
            is_new=is_new,
        ),
    )
