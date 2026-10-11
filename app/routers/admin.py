"""Admin → Onboarding → Doctors, plus doctor and appointment administration.

Every state-changing action writes to audit_log with the admin's id.
"""

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from app.deps import DB, Admin
from app.models import (
    Appointment,
    AppointmentStatus,
    ApplicationStatus,
    Doctor,
    DoctorApplication,
    DoctorStatus,
    ProfileChangeRequest,
)
from app.schemas.applications import (
    AcceptIn,
    AcceptOut,
    AdminApplicationOut,
    ApplicationListItem,
    NoteIn,
)
from app.schemas.booking import AppointmentOut
from app.serializers import appointment_out
from app.services import appointments as appt_svc
from app.services.applications import accept, issue_set_password_link, to_admin_out
from app.services.audit import audit
from app.services.outbox import enqueue

router = APIRouter(prefix="/admin", tags=["Admin"])


def _app(db: DB, application_id: int) -> DoctorApplication:
    app = db.scalar(
        select(DoctorApplication)
        .where(DoctorApplication.id == application_id)
        .options(selectinload(DoctorApplication.documents))
        .with_for_update()
    )
    if app is None:
        raise HTTPException(404, "Application not found")
    return app


REVIEWABLE = {ApplicationStatus.SUBMITTED, ApplicationStatus.UNDER_REVIEW}


class Page(BaseModel):
    items: list
    total: int
    page: int
    page_size: int


@router.get("/doctors/applications", response_model=Page)
def list_applications(
    db: DB,
    admin: Admin,
    status: ApplicationStatus | None = None,
    q: str | None = Query(default=None, description="Name, email, mobile or reference"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
) -> Page:
    query = select(DoctorApplication)
    if status:
        query = query.where(DoctorApplication.status == status)
    else:
        query = query.where(DoctorApplication.status != ApplicationStatus.DRAFT)
    if q:
        like = f"%{q.lower()}%"
        query = query.where(
            or_(
                func.lower(DoctorApplication.full_name).like(like),
                func.lower(DoctorApplication.email).like(like),
                DoctorApplication.mobile.like(like),
                func.lower(DoctorApplication.reference).like(like),
            )
        )
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.scalars(
        query.order_by(DoctorApplication.submitted_at.desc().nulls_last(), DoctorApplication.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    items = [
        ApplicationListItem(
            id=a.id,
            reference=a.reference,
            status=a.status,
            full_name=a.full_name,
            email=a.email,
            mobile=a.mobile,
            specialties=(a.data or {}).get("registration", {}).get("specialties") or [],
            submitted_at=a.submitted_at,
            updated_at=a.updated_at,
        )
        for a in rows
    ]
    return Page(items=items, total=total, page=page, page_size=page_size)


@router.get("/doctors/applications/{application_id}", response_model=AdminApplicationOut)
def get_application(application_id: int, db: DB, admin: Admin) -> AdminApplicationOut:
    app = _app(db, application_id)
    audit(db, actor_type="admin", actor_id=admin.sub, action="application.viewed",
          entity_type="doctor_application", entity_id=app.id)
    db.commit()
    return to_admin_out(app)


@router.post("/doctors/applications/{application_id}/review", response_model=AdminApplicationOut)
def mark_reviewing(application_id: int, db: DB, admin: Admin) -> AdminApplicationOut:
    app = _app(db, application_id)
    if app.status != ApplicationStatus.SUBMITTED:
        raise HTTPException(409, f"Application is {app.status}")
    app.status = ApplicationStatus.UNDER_REVIEW
    app.reviewed_by, app.reviewed_at = admin.sub, datetime.now(UTC)
    audit(db, actor_type="admin", actor_id=admin.sub, action="application.under_review",
          entity_type="doctor_application", entity_id=app.id)
    db.commit()
    return to_admin_out(app)


def _hold_or_reject(db: DB, admin: Admin, application_id: int, note: str, new_status: ApplicationStatus):
    app = _app(db, application_id)
    if app.status not in REVIEWABLE:
        raise HTTPException(409, f"Application is {app.status}")
    app.status = new_status
    app.review_note = note
    app.reviewed_by, app.reviewed_at = admin.sub, datetime.now(UTC)
    audit(db, actor_type="admin", actor_id=admin.sub, action=f"application.{new_status.value}",
          entity_type="doctor_application", entity_id=app.id, data={"note": note})
    enqueue(db, channel="email", template=f"application_{new_status.value}", recipient=app.email,
            payload={"name": app.full_name, "reference": app.reference, "note": note})
    db.commit()
    return to_admin_out(app)


@router.post("/doctors/applications/{application_id}/hold", response_model=AdminApplicationOut)
def hold_application(application_id: int, body: NoteIn, db: DB, admin: Admin) -> AdminApplicationOut:
    """The doctor can edit and resubmit. The note tells them what to fix."""
    return _hold_or_reject(db, admin, application_id, body.note, ApplicationStatus.ON_HOLD)


@router.post("/doctors/applications/{application_id}/reject", response_model=AdminApplicationOut)
def reject_application(application_id: int, body: NoteIn, db: DB, admin: Admin) -> AdminApplicationOut:
    return _hold_or_reject(db, admin, application_id, body.note, ApplicationStatus.REJECTED)


@router.post("/doctors/applications/{application_id}/accept", response_model=AcceptOut)
def accept_application(application_id: int, body: AcceptIn, db: DB, admin: Admin) -> AcceptOut:
    """Creates the doctor and their login in one transaction and emails a set-password link."""
    if not body.registration_verified:
        raise HTTPException(422, "Verify the registration number on the NMC / state council register first")
    app = _app(db, application_id)
    if app.status not in REVIEWABLE:
        raise HTTPException(409, f"Application is {app.status}")
    doctor, user, expires = accept(db, app, admin.sub)
    audit(db, actor_type="admin", actor_id=admin.sub, action="application.accepted",
          entity_type="doctor_application", entity_id=app.id,
          data={"doctor_id": doctor.id, "username": user.username, "note": body.note})
    db.commit()
    return AcceptOut(doctor_id=doctor.id, username=user.username, email=user.email,
                     invite_sent_to=user.email, invite_expires_at=expires)


# --- Doctors ------------------------------------------------------------------

def _doctor(db: DB, doctor_id: int) -> Doctor:
    d = db.get(Doctor, doctor_id)
    if d is None:
        raise HTTPException(404, "Doctor not found")
    return d


@router.post("/doctors/{doctor_id}/resend-invite")
def resend_invite(doctor_id: int, db: DB, admin: Admin) -> dict:
    """The "New password" button: revokes old links and emails a fresh set-password link."""
    d = _doctor(db, doctor_id)
    expires = issue_set_password_link(db, d.user, template="doctor_invite")
    audit(db, actor_type="admin", actor_id=admin.sub, action="doctor.invite_resent",
          entity_type="doctor", entity_id=d.id)
    db.commit()
    return {"sent_to": d.user.email, "expires_at": expires}


class SuspendIn(NoteIn):
    cancel_upcoming: bool = Field(default=True, description="Cancel booked appointments with a full refund")


@router.post("/doctors/{doctor_id}/suspend")
def suspend_doctor(doctor_id: int, body: SuspendIn, db: DB, admin: Admin) -> dict:
    d = appt_svc.lock_doctor(db, doctor_id)
    if d.status == DoctorStatus.SUSPENDED:
        raise HTTPException(409, "Already suspended")
    d.status, d.status_note = DoctorStatus.SUSPENDED, body.note
    d.user.token_version += 1  # signs the doctor out
    cancelled = 0
    if body.cancel_upcoming:
        upcoming = db.scalars(
            select(Appointment).where(
                Appointment.doctor_id == d.id,
                Appointment.start_at > datetime.now(UTC),
                Appointment.status.in_([AppointmentStatus.BOOKED, AppointmentStatus.PENDING_PAYMENT]),
            )
        ).all()
        for a in upcoming:
            appt_svc.cancel(db, a, by="admin", actor_id=admin.sub, reason="Doctor unavailable", refund_percent=100)
            cancelled += 1
    audit(db, actor_type="admin", actor_id=admin.sub, action="doctor.suspended", entity_type="doctor",
          entity_id=d.id, data={"note": body.note, "cancelled": cancelled})
    db.commit()
    return {"status": d.status, "cancelled_appointments": cancelled}


@router.post("/doctors/{doctor_id}/reactivate")
def reactivate_doctor(doctor_id: int, body: NoteIn, db: DB, admin: Admin) -> dict:
    d = _doctor(db, doctor_id)
    d.status, d.status_note = DoctorStatus.ACTIVE, body.note
    audit(db, actor_type="admin", actor_id=admin.sub, action="doctor.reactivated", entity_type="doctor",
          entity_id=d.id, data={"note": body.note})
    db.commit()
    return {"status": d.status}


# --- Profile changes (fee, specialties) ----------------------------------------

@router.get("/doctors/profile-changes")
def list_profile_changes(db: DB, admin: Admin, status: str = "pending") -> list[dict]:
    rows = db.scalars(
        select(ProfileChangeRequest).where(ProfileChangeRequest.status == status)
        .order_by(ProfileChangeRequest.created_at)
    ).all()
    out = []
    for r in rows:
        d = db.get(Doctor, r.doctor_id)
        current = {"fee_inr": d.fee_paise / 100, "specialties": d.specialties}
        out.append({"id": r.id, "doctor_id": d.id, "doctor_name": d.full_name, "requested": r.changes,
                    "current": {k: current[k] for k in r.changes}, "created_at": r.created_at})
    return out


class DecisionIn(BaseModel):
    decision: Literal["approve", "reject"]
    note: str | None = None


@router.post("/doctors/profile-changes/{request_id}")
def decide_profile_change(request_id: int, body: DecisionIn, db: DB, admin: Admin) -> dict:
    r = db.get(ProfileChangeRequest, request_id)
    if r is None or r.status != "pending":
        raise HTTPException(404, "No pending request with that id")
    d = _doctor(db, r.doctor_id)
    if body.decision == "approve":
        if "fee_inr" in r.changes:
            d.fee_paise = int(r.changes["fee_inr"]) * 100
        if "specialties" in r.changes:
            d.specialties = r.changes["specialties"]
    r.status = "approved" if body.decision == "approve" else "rejected"
    r.note, r.decided_by, r.decided_at = body.note, admin.sub, datetime.now(UTC)
    audit(db, actor_type="admin", actor_id=admin.sub, action=f"profile_change.{r.status}",
          entity_type="doctor", entity_id=d.id, data={"changes": r.changes, "note": body.note})
    enqueue(db, channel="email", template=f"profile_change_{r.status}", recipient=d.email,
            payload={"changes": r.changes, "note": body.note})
    db.commit()
    return {"id": r.id, "status": r.status}


# --- Appointments that need a decision ----------------------------------------

@router.get("/appointments", response_model=list[AppointmentOut])
def list_appointments(
    db: DB, admin: Admin, status: AppointmentStatus = AppointmentStatus.NEEDS_REVIEW, limit: int = 100
) -> list[AppointmentOut]:
    rows = db.scalars(
        select(Appointment).where(Appointment.status == status).order_by(Appointment.start_at).limit(limit)
    ).all()
    return [appointment_out(a) for a in rows]


class ResolveIn(BaseModel):
    outcome: Literal["completed", "no_show", "doctor_no_show"]
    note: str | None = None


@router.post("/appointments/{appointment_id}/resolve", response_model=AppointmentOut)
def resolve_appointment(appointment_id: int, body: ResolveIn, db: DB, admin: Admin) -> AppointmentOut:
    """For appointments the doctor never marked. doctor_no_show refunds the member in full."""
    a = db.get(Appointment, appointment_id)
    if a is None:
        raise HTTPException(404, "Appointment not found")
    appt_svc.mark_outcome(db, a, body.outcome, actor_type="admin", actor_id=admin.sub)
    audit(db, actor_type="admin", actor_id=admin.sub, action=f"appointment.{body.outcome}",
          entity_type="appointment", entity_id=a.id, data={"note": body.note})
    db.commit()
    return appointment_out(a)
