"""Doctor application: seven steps, each saved as it is filled in.

Every field is optional while the application is a draft. Completeness is
checked on submit (see REQUIRED_* below and services.applications).
"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.security import PAN_RE, is_valid_aadhaar

PINCODE = r"^[1-9]\d{5}$"
MOBILE = r"^[6-9]\d{9}$"


class Address(BaseModel):
    line1: str | None = None
    line2: str | None = None
    city: str | None = None
    state: str | None = None
    pincode: str | None = Field(default=None, pattern=PINCODE)


class PersonalDetails(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=200)
    gender: Literal["male", "female", "other"] | None = None
    date_of_birth: date | None = None
    mobile: str | None = Field(default=None, pattern=MOBILE)
    email: EmailStr | None = None
    languages: list[str] | None = None
    address: Address | None = None


class Identity(BaseModel):
    """Write-only. PAN is encrypted; only the last 4 digits of Aadhaar are kept."""

    pan: str | None = None
    aadhaar: str | None = None

    @field_validator("pan")
    @classmethod
    def _pan(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip().upper()
        if not PAN_RE.match(v):
            raise ValueError("PAN must look like ABCDE1234F")
        return v

    @field_validator("aadhaar")
    @classmethod
    def _aadhaar(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.replace(" ", "")
        if not is_valid_aadhaar(v):
            raise ValueError("Aadhaar number is not valid")
        return v


class Registration(BaseModel):
    registration_number: str | None = Field(default=None, max_length=60)
    qualifications: list[str] | None = None
    specialties: list[str] | None = None


class Teleconsult(BaseModel):
    registration_council: str | None = Field(default=None, max_length=120)
    experience_years: int | None = Field(default=None, ge=0, le=70)
    modes: list[Literal["video", "clinic"]] | None = None
    fee_inr: int | None = Field(default=None, ge=0, le=100000)
    clinic_address: str | None = None
    clinic_pincode: str | None = Field(default=None, pattern=PINCODE)


class Bank(BaseModel):
    account_holder: str | None = None
    account_number: str | None = Field(default=None, pattern=r"^\d{9,18}$")
    ifsc: str | None = Field(default=None, pattern=r"^[A-Z]{4}0[A-Z0-9]{6}$")


class Compliance(BaseModel):
    info_is_true: bool | None = None
    follows_telemedicine_guidelines: bool | None = None
    consents_to_verification: bool | None = None
    accepts_terms: bool | None = None


class ApplicationUpdate(BaseModel):
    """Any subset of sections. `step` records where the doctor left off."""

    model_config = ConfigDict(extra="forbid")

    step: int | None = Field(default=None, ge=1, le=7)
    personal: PersonalDetails | None = None
    identity: Identity | None = None
    registration: Registration | None = None
    teleconsult: Teleconsult | None = None
    bank: Bank | None = None
    compliance: Compliance | None = None


DOCUMENT_KINDS = Literal[
    "registration_certificate",
    "degree_certificate",
    "pan_card",
    "aadhaar_card",
    "photo",
    "cancelled_cheque",
    "other",
]
REQUIRED_DOCUMENTS = ["registration_certificate", "degree_certificate", "pan_card", "photo"]


class DocumentOut(BaseModel):
    id: str
    kind: str
    filename: str
    content_type: str
    size_bytes: int
    uploaded_at: datetime
    url: str | None = None  # short-lived signed link, admin views only


class ApplicationOut(BaseModel):
    id: str
    reference: str | None
    status: str
    current_step: int
    data: dict
    pan_masked: str | None
    aadhaar_masked: str | None
    bank_account_masked: str | None
    review_note: str | None
    submitted_at: datetime | None
    documents: list[DocumentOut]
    missing: list[str] = []
    email_verified: bool = False


class ApplicationCreated(ApplicationOut):
    resume_token: str = Field(description="Keep this to resume the draft. Shown once.")


class ApplicationListItem(BaseModel):
    id: str
    reference: str | None
    status: str
    full_name: str | None
    email: str | None
    mobile: str | None
    specialties: list[str]
    submitted_at: datetime | None
    updated_at: datetime


class AdminApplicationOut(ApplicationOut):
    pan: str | None = None
    full_name: str | None
    email: str | None
    mobile: str | None
    reviewed_by: str | None
    reviewed_at: datetime | None
    doctor_id: str | None


class NoteIn(BaseModel):
    note: str = Field(min_length=5, max_length=2000)


class AcceptIn(BaseModel):
    registration_verified: bool = Field(
        description="Admin confirms the registration number was checked against the NMC / state council register."
    )
    note: str | None = None


class AcceptOut(BaseModel):
    doctor_id: str
    username: str
    email: str
    invite_sent_to: str
    invite_expires_at: datetime
