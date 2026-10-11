"""Request authentication.

Admin and member tokens are issued by the main SuppAI auth service and signed
with the shared SUPPAI_JWT_SECRET. They carry {"sub": <user id>, "role":
"admin" | "member"}. Doctor tokens are issued by this service at
/auth/doctor/login and also carry doctor_id and a token version.
"""

from dataclasses import dataclass
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Doctor, DoctorStatus, DoctorUser
from app.security import decode_access_token

_bearer = HTTPBearer(auto_error=False)

DB = Annotated[Session, Depends(get_db)]


@dataclass
class Principal:
    role: str
    sub: str
    doctor_id: int | None = None
    ver: int | None = None


def _principal(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> Principal:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    try:
        claims = decode_access_token(creds.credentials)
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    role, sub = claims.get("role"), claims.get("sub")
    if role not in {"admin", "member", "doctor"} or not sub:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token claims")
    return Principal(role=role, sub=str(sub), doctor_id=claims.get("doctor_id"), ver=claims.get("ver"))


def require_admin(p: Principal = Depends(_principal)) -> Principal:
    if p.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin only")
    return p


def require_member(p: Principal = Depends(_principal)) -> Principal:
    if p.role != "member":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Members only")
    return p


def require_doctor(db: DB, p: Principal = Depends(_principal)) -> Doctor:
    """Returns the signed-in doctor. doctor_id always comes from the token."""
    if p.role != "doctor" or p.doctor_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Doctors only")
    user = db.get(DoctorUser, int(p.sub))
    if user is None or user.doctor_id != p.doctor_id or user.token_version != p.ver:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session no longer valid")
    doctor = db.get(Doctor, p.doctor_id)
    if doctor is None or doctor.status != DoctorStatus.ACTIVE:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Doctor account is not active")
    return doctor


Admin = Annotated[Principal, Depends(require_admin)]
Member = Annotated[Principal, Depends(require_member)]
CurrentDoctor = Annotated[Doctor, Depends(require_doctor)]
