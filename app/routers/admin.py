"""Admin → Onboarding → Doctors, plus doctor and appointment administration.

Every state-changing action writes to audit_log with the admin's id.
"""

import re
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from pymongo import DESCENDING, ReturnDocument

from app.db import now, oid
from app.deps import DB, Admin
from app.models import AppointmentStatus, ApplicationStatus, DoctorStatus
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
from app.services.applications import REVIEWABLE, accept, issue_set_password_link, to_admin_out
from app.services.audit import audit
from app.services.locks import doctor_lock
from app.services.outbox import enqueue

router = APIRouter(prefix="/admin", tags=["Admin"])


def _app(db: DB, application_id: str) -> dict:
    app = db.doctor_applications.find_one({"_id": oid(application_id, "Application")})
    if app is None:
        raise HTTPException(404, "Application not found")
    return app


def _move(db: DB, application_id: str, from_statuses: list[str], changes: dict) -> dict:
    """Conditional status change so two admins can't act on the same application at once."""
    app = db.doctor_applications.find_one_and_update(
        {"_id": oid(application_id, "Application"), "status": {"$in": from_statuses}, "accepting_at": None},
        {"$set": {**changes, "updated_at": now()}},
        return_document=ReturnDocument.AFTER,
    )
    if app is None:
        current = _app(db, application_id)
        raise HTTPException(409, f"Application is {current['status']}")
    return app


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
    query: dict = {"status": status.value} if status else {"status": {"$ne": ApplicationStatus.DRAFT.value}}
    if q:
        rx = {"$regex": re.escape(q), "$options": "i"}
        query["$or"] = [{"full_name": rx}, {"email": rx}, {"mobile": rx}, {"reference": rx}]
    total = db.doctor_applications.count_documents(query)
    rows = (
        db.doctor_applications.find(query)
        .sort([("submitted_at", DESCENDING), ("_id", DESCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    items = [
        ApplicationListItem(
            id=str(a["_id"]),
            reference=a.get("reference"),
            status=a["status"],
            full_name=a.get("full_name"),
            email=a.get("email"),
            mobile=a.get("mobile"),
            specialties=(a.get("data") or {}).get("registration", {}).get("specialties") or [],
            submitted_at=a.get("submitted_at"),
            updated_at=a["updated_at"],
        )
        for a in rows
    ]
    return Page(items=items, total=total, page=page, page_size=page_size)


@router.get("/doctors/applications/{application_id}", response_model=AdminApplicationOut)
def get_application(application_id: str, db: DB, admin: Admin) -> AdminApplicationOut:
    app = _app(db, application_id)
    audit(db, actor_type="admin", actor_id=admin.sub, action="application.viewed",
          entity_type="doctor_application", entity_id=app["_id"])
    return to_admin_out(app)


@router.post("/doctors/applications/{application_id}/review", response_model=AdminApplicationOut)
def mark_reviewing(application_id: str, db: DB, admin: Admin) -> AdminApplicationOut:
    app = _move(db, application_id, [ApplicationStatus.SUBMITTED.value], {
        "status": ApplicationStatus.UNDER_REVIEW.value, "reviewed_by": admin.sub, "reviewed_at": now()})
    audit(db, actor_type="admin", actor_id=admin.sub, action="application.under_review",
          entity_type="doctor_application", entity_id=app["_id"])
    return to_admin_out(app)


def _hold_or_reject(db: DB, admin: Admin, application_id: str, note: str, new_status: ApplicationStatus):
    app = _move(db, application_id, REVIEWABLE, {
        "status": new_status.value, "review_note": note, "reviewed_by": admin.sub, "reviewed_at": now()})
    audit(db, actor_type="admin", actor_id=admin.sub, action=f"application.{new_status.value}",
          entity_type="doctor_application", entity_id=app["_id"], data={"note": note})
    enqueue(db, channel="email", template=f"application_{new_status.value}", recipient=app["email"],
            payload={"name": app["full_name"], "reference": app["reference"], "note": note})
    return to_admin_out(app)


@router.post("/doctors/applications/{application_id}/hold", response_model=AdminApplicationOut)
def hold_application(application_id: str, body: NoteIn, db: DB, admin: Admin) -> AdminApplicationOut:
    """The doctor can edit and resubmit. The note tells them what to fix."""
    return _hold_or_reject(db, admin, application_id, body.note, ApplicationStatus.ON_HOLD)


@router.post("/doctors/applications/{application_id}/reject", response_model=AdminApplicationOut)
def reject_application(application_id: str, body: NoteIn, db: DB, admin: Admin) -> AdminApplicationOut:
    return _hold_or_reject(db, admin, application_id, body.note, ApplicationStatus.REJECTED)


@router.post("/doctors/applications/{application_id}/accept", response_model=AcceptOut)
def accept_application(application_id: str, body: AcceptIn, db: DB, admin: Admin) -> AcceptOut:
    """Creates the doctor and their login and emails a set-password link."""
    if not body.registration_verified:
        raise HTTPException(422, "Verify the registration number on the NMC / state council register first")
    app_id = oid(application_id, "Application")
    doctor, user, expires = accept(db, app_id, admin.sub)
    audit(db, actor_type="admin", actor_id=admin.sub, action="application.accepted",
          entity_type="doctor_application", entity_id=app_id,
          data={"doctor_id": str(doctor["_id"]), "username": user["username"], "note": body.note})
    return AcceptOut(doctor_id=str(doctor["_id"]), username=user["username"], email=user["email"],
                     invite_sent_to=user["email"], invite_expires_at=expires)


# --- Doctors ------------------------------------------------------------------

def _doctor(db: DB, doctor_id: str) -> dict:
    d = db.doctors.find_one({"_id": oid(doctor_id, "Doctor")})
    if d is None:
        raise HTTPException(404, "Doctor not found")
    return d


@router.post("/doctors/{doctor_id}/resend-invite")
def resend_invite(doctor_id: str, db: DB, admin: Admin) -> dict:
    """The "New password" button: revokes old links and emails a fresh set-password link."""
    d = _doctor(db, doctor_id)
    user = db.doctor_users.find_one({"doctor_id": d["_id"]})
    expires = issue_set_password_link(db, user, template="doctor_invite")
    audit(db, actor_type="admin", actor_id=admin.sub, action="doctor.invite_resent",
          entity_type="doctor", entity_id=d["_id"])
    return {"sent_to": user["email"], "expires_at": expires}


class SuspendIn(NoteIn):
    cancel_upcoming: bool = Field(default=True, description="Cancel booked appointments with a full refund")


@router.post("/doctors/{doctor_id}/suspend")
def suspend_doctor(doctor_id: str, body: SuspendIn, db: DB, admin: Admin) -> dict:
    d = _doctor(db, doctor_id)
    with doctor_lock(db, d["_id"]):
        res = db.doctors.update_one({"_id": d["_id"], "status": DoctorStatus.ACTIVE.value},
                                    {"$set": {"status": DoctorStatus.SUSPENDED.value, "status_note": body.note}})
        if res.modified_count == 0:
            raise HTTPException(409, "Already suspended")
    db.doctor_users.update_one({"doctor_id": d["_id"]}, {"$inc": {"token_version": 1}})  # signs the doctor out
    cancelled = 0
    if body.cancel_upcoming:
        for a in db.appointments.find({
            "doctor_id": d["_id"], "start_at": {"$gt": now()},
            "status": {"$in": [AppointmentStatus.BOOKED.value, AppointmentStatus.PENDING_PAYMENT.value]},
        }):
            try:
                appt_svc.cancel(db, a, by="admin", actor_id=admin.sub, reason="Doctor unavailable", refund_percent=100)
                cancelled += 1
            except HTTPException:
                pass  # moved on its own in the meantime
    audit(db, actor_type="admin", actor_id=admin.sub, action="doctor.suspended", entity_type="doctor",
          entity_id=d["_id"], data={"note": body.note, "cancelled": cancelled})
    return {"status": DoctorStatus.SUSPENDED.value, "cancelled_appointments": cancelled}


@router.post("/doctors/{doctor_id}/reactivate")
def reactivate_doctor(doctor_id: str, body: NoteIn, db: DB, admin: Admin) -> dict:
    d = _doctor(db, doctor_id)
    db.doctors.update_one({"_id": d["_id"]}, {"$set": {"status": DoctorStatus.ACTIVE.value, "status_note": body.note}})
    audit(db, actor_type="admin", actor_id=admin.sub, action="doctor.reactivated", entity_type="doctor",
          entity_id=d["_id"], data={"note": body.note})
    return {"status": DoctorStatus.ACTIVE.value}


# --- Profile changes (fee, specialties) ----------------------------------------

@router.get("/doctors/profile-changes")
def list_profile_changes(db: DB, admin: Admin, status: str = "pending") -> list[dict]:
    out = []
    for r in db.profile_change_requests.find({"status": status}).sort("created_at", 1):
        d = db.doctors.find_one({"_id": r["doctor_id"]})
        current = {"fee_inr": d["fee_paise"] / 100, "specialties": d["specialties"]}
        out.append({"id": str(r["_id"]), "doctor_id": str(d["_id"]), "doctor_name": d["full_name"],
                    "requested": r["changes"], "current": {k: current[k] for k in r["changes"]},
                    "created_at": r["created_at"]})
    return out


class DecisionIn(BaseModel):
    decision: Literal["approve", "reject"]
    note: str | None = None


@router.post("/doctors/profile-changes/{request_id}")
def decide_profile_change(request_id: str, body: DecisionIn, db: DB, admin: Admin) -> dict:
    new_status = "approved" if body.decision == "approve" else "rejected"
    r = db.profile_change_requests.find_one_and_update(
        {"_id": oid(request_id, "Request"), "status": "pending"},
        {"$set": {"status": new_status, "note": body.note, "decided_by": admin.sub, "decided_at": now()}},
    )
    if r is None:
        raise HTTPException(404, "No pending request with that id")
    if new_status == "approved":
        changes = {}
        if "fee_inr" in r["changes"]:
            changes["fee_paise"] = int(r["changes"]["fee_inr"]) * 100
        if "specialties" in r["changes"]:
            changes["specialties"] = r["changes"]["specialties"]
        db.doctors.update_one({"_id": r["doctor_id"]}, {"$set": changes})
    d = db.doctors.find_one({"_id": r["doctor_id"]}, {"email": 1})
    audit(db, actor_type="admin", actor_id=admin.sub, action=f"profile_change.{new_status}",
          entity_type="doctor", entity_id=r["doctor_id"], data={"changes": r["changes"], "note": body.note})
    enqueue(db, channel="email", template=f"profile_change_{new_status}", recipient=d["email"],
            payload={"changes": r["changes"], "note": body.note})
    return {"id": str(r["_id"]), "status": new_status}


# --- Appointments that need a decision ----------------------------------------

@router.get("/appointments", response_model=list[AppointmentOut])
def list_appointments(
    db: DB, admin: Admin, status: AppointmentStatus = AppointmentStatus.NEEDS_REVIEW, limit: int = 100
) -> list[AppointmentOut]:
    rows = db.appointments.find({"status": status.value}).sort("start_at", 1).limit(limit)
    return [appointment_out(a) for a in rows]


class ResolveIn(BaseModel):
    outcome: Literal["completed", "no_show", "doctor_no_show"]
    note: str | None = None


@router.post("/appointments/{appointment_id}/resolve", response_model=AppointmentOut)
def resolve_appointment(appointment_id: str, body: ResolveIn, db: DB, admin: Admin) -> AppointmentOut:
    """For appointments the doctor never marked. doctor_no_show refunds the member in full."""
    a = db.appointments.find_one({"_id": oid(appointment_id, "Appointment")})
    if a is None:
        raise HTTPException(404, "Appointment not found")
    a = appt_svc.mark_outcome(db, a, body.outcome, actor_type="admin", actor_id=admin.sub)
    audit(db, actor_type="admin", actor_id=admin.sub, action=f"appointment.{body.outcome}",
          entity_type="appointment", entity_id=a["_id"], data={"note": body.note})
    return appointment_out(a)
