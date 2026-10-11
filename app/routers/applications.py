"""Public doctor application at /join/doctor.

The applicant has no account yet, so a draft is protected by a resume token
returned once on create and sent back in the X-Application-Token header.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.deps import DB
from app.models import ApplicationDocument, ApplicationStatus, DoctorApplication
from app.schemas.applications import (
    DOCUMENT_KINDS,
    ApplicationCreated,
    ApplicationOut,
    ApplicationUpdate,
    DocumentOut,
)
from app.security import hash_token, new_token
from app.services import storage
from app.services.applications import EDITABLE, apply_update, assign_reference, missing_items, to_out
from app.services.audit import audit
from app.services.outbox import enqueue

router = APIRouter(prefix="/doctors/applications", tags=["Doctor application"])

Token = Annotated[str, Header(alias="X-Application-Token")]


def _load(db: DB, application_id: int, token: str) -> DoctorApplication:
    app = db.scalar(
        select(DoctorApplication)
        .where(DoctorApplication.id == application_id)
        .options(selectinload(DoctorApplication.documents))
    )
    if app is None or app.resume_token_hash != hash_token(token):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Application not found")
    return app


@router.post("", response_model=ApplicationCreated, status_code=201)
def create_application(db: DB, body: ApplicationUpdate | None = None) -> ApplicationCreated:
    """Start a draft. Any steps already filled in can be sent now."""
    token = new_token()
    app = DoctorApplication(
        resume_token_hash=hash_token(token), data={}, status=ApplicationStatus.DRAFT, current_step=1
    )
    app.documents = []
    if body:
        apply_update(app, body)
    db.add(app)
    db.commit()
    db.refresh(app)
    return ApplicationCreated(**to_out(app).model_dump(), resume_token=token)


@router.get("/{application_id}", response_model=ApplicationOut)
def get_application(application_id: int, db: DB, token: Token) -> ApplicationOut:
    return to_out(_load(db, application_id, token))


@router.patch("/{application_id}", response_model=ApplicationOut)
def save_step(application_id: int, body: ApplicationUpdate, db: DB, token: Token) -> ApplicationOut:
    app = _load(db, application_id, token)
    apply_update(app, body)
    db.commit()
    return to_out(app)


@router.post("/{application_id}/documents", response_model=DocumentOut, status_code=201)
async def upload_document(
    application_id: int,
    db: DB,
    token: Token,
    kind: Annotated[DOCUMENT_KINDS, Form()],
    file: Annotated[UploadFile, File()],
) -> DocumentOut:
    app = _load(db, application_id, token)
    if app.status not in EDITABLE:
        raise HTTPException(409, "Application can no longer be edited")
    if file.content_type not in storage.ALLOWED_TYPES:
        raise HTTPException(415, "Upload a PDF, JPEG or PNG")
    content = await file.read()
    if len(content) > storage.MAX_BYTES:
        raise HTTPException(413, "File is larger than 10 MB")
    suffix = Path(file.filename or "").suffix.lower()[:8]
    doc = ApplicationDocument(
        application_id=app.id,
        kind=kind,
        filename=(file.filename or "upload")[:255],
        content_type=file.content_type,
        size_bytes=len(content),
        storage_key=storage.save(content, suffix),
    )
    db.add(doc)
    db.commit()
    return DocumentOut.model_validate(doc, from_attributes=True)


@router.delete("/{application_id}/documents/{document_id}", status_code=204)
def delete_document(application_id: int, document_id: int, db: DB, token: Token) -> None:
    app = _load(db, application_id, token)
    if app.status not in EDITABLE:
        raise HTTPException(409, "Application can no longer be edited")
    doc = db.get(ApplicationDocument, document_id)
    if doc is None or doc.application_id != app.id:
        raise HTTPException(404, "Document not found")
    db.delete(doc)
    db.commit()


@router.post("/{application_id}/submit", response_model=ApplicationOut)
def submit_application(application_id: int, db: DB, token: Token) -> ApplicationOut:
    """Submit (or resubmit after a hold). Keeps the same SA-DOC reference."""
    app = _load(db, application_id, token)
    if app.status not in EDITABLE:
        raise HTTPException(409, f"Application is already {app.status}")
    missing = missing_items(app)
    if missing:
        raise HTTPException(422, {"message": "Application is incomplete", "missing": missing})
    resubmission = app.status == ApplicationStatus.ON_HOLD
    assign_reference(db, app)
    app.status = ApplicationStatus.SUBMITTED
    app.submitted_at = datetime.now(UTC)
    audit(
        db,
        actor_type="applicant",
        actor_id=None,
        action="application.resubmitted" if resubmission else "application.submitted",
        entity_type="doctor_application",
        entity_id=app.id,
    )
    enqueue(
        db,
        channel="email",
        template="application_received",
        recipient=app.email,
        payload={"name": app.full_name, "reference": app.reference},
    )
    db.commit()
    return to_out(app)
