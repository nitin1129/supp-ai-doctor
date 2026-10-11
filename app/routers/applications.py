"""Public doctor application at /join/doctor.

The applicant has no account yet, so a draft is protected by a resume token
returned once on create and sent back in the X-Application-Token header.
"""

from pathlib import Path
from typing import Annotated

from bson import ObjectId
from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile, status

from app.db import now, oid
from app.deps import DB
from app.models import ApplicationStatus
from app.schemas.applications import (
    DOCUMENT_KINDS,
    ApplicationCreated,
    ApplicationOut,
    ApplicationUpdate,
    DocumentOut,
)
from app.security import hash_token, new_token
from app.services import storage
from app.services.applications import (
    EDITABLE,
    apply_update,
    document_out,
    missing_items,
    new_application,
    reference_for,
    to_out,
)
from app.services.audit import audit
from app.services.outbox import enqueue

router = APIRouter(prefix="/doctors/applications", tags=["Doctor application"])

Token = Annotated[str, Header(alias="X-Application-Token")]


def _load(db: DB, application_id: str, token: str) -> dict:
    app = db.doctor_applications.find_one({"_id": oid(application_id, "Application")})
    if app is None or app["resume_token_hash"] != hash_token(token):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Application not found")
    return app


def _save(db: DB, app: dict, changes: dict) -> None:
    """Writes only if the application is still editable (guards against a racing submit)."""
    res = db.doctor_applications.update_one({"_id": app["_id"], "status": {"$in": EDITABLE}}, {"$set": changes})
    if res.matched_count == 0:
        raise HTTPException(409, "Application can no longer be edited")


@router.post("", response_model=ApplicationCreated, status_code=201)
def create_application(db: DB, body: ApplicationUpdate | None = None) -> ApplicationCreated:
    """Start a draft. Any steps already filled in can be sent now."""
    token = new_token()
    app = new_application(token)
    if body:
        apply_update(app, body)
    app["_id"] = db.doctor_applications.insert_one(app).inserted_id
    return ApplicationCreated(**to_out(app).model_dump(), resume_token=token)


@router.get("/{application_id}", response_model=ApplicationOut)
def get_application(application_id: str, db: DB, token: Token) -> ApplicationOut:
    return to_out(_load(db, application_id, token))


@router.patch("/{application_id}", response_model=ApplicationOut)
def save_step(application_id: str, body: ApplicationUpdate, db: DB, token: Token) -> ApplicationOut:
    app = _load(db, application_id, token)
    _save(db, app, apply_update(app, body))
    return to_out(app)


@router.post("/{application_id}/documents", response_model=DocumentOut, status_code=201)
async def upload_document(
    application_id: str,
    db: DB,
    token: Token,
    kind: Annotated[DOCUMENT_KINDS, Form()],
    file: Annotated[UploadFile, File()],
) -> DocumentOut:
    app = _load(db, application_id, token)
    if app["status"] not in EDITABLE:
        raise HTTPException(409, "Application can no longer be edited")
    if file.content_type not in storage.ALLOWED_TYPES:
        raise HTTPException(415, "Upload a PDF, JPEG or PNG")
    content = await file.read()
    if len(content) > storage.MAX_BYTES:
        raise HTTPException(413, "File is larger than 10 MB")
    suffix = Path(file.filename or "").suffix.lower()[:8]
    doc = {
        "id": ObjectId(),
        "kind": kind,
        "filename": (file.filename or "upload")[:255],
        "content_type": file.content_type,
        "size_bytes": len(content),
        "storage_key": storage.save(content, suffix),
        "uploaded_at": now(),
    }
    res = db.doctor_applications.update_one(
        {"_id": app["_id"], "status": {"$in": EDITABLE}},
        {"$push": {"documents": doc}, "$set": {"updated_at": now()}},
    )
    if res.matched_count == 0:
        raise HTTPException(409, "Application can no longer be edited")
    return document_out(doc)


@router.delete("/{application_id}/documents/{document_id}", status_code=204)
def delete_document(application_id: str, document_id: str, db: DB, token: Token) -> None:
    app = _load(db, application_id, token)
    if app["status"] not in EDITABLE:
        raise HTTPException(409, "Application can no longer be edited")
    res = db.doctor_applications.update_one(
        {"_id": app["_id"], "status": {"$in": EDITABLE}},
        {"$pull": {"documents": {"id": oid(document_id, "Document")}}},
    )
    if res.modified_count == 0:
        raise HTTPException(404, "Document not found")


@router.post("/{application_id}/submit", response_model=ApplicationOut)
def submit_application(application_id: str, db: DB, token: Token) -> ApplicationOut:
    """Submit (or resubmit after a hold). Keeps the same SA-DOC reference."""
    app = _load(db, application_id, token)
    if app["status"] not in EDITABLE:
        raise HTTPException(409, f"Application is already {app['status']}")
    missing = missing_items(app)
    if missing:
        raise HTTPException(422, {"message": "Application is incomplete", "missing": missing})
    resubmission = app["status"] == ApplicationStatus.ON_HOLD
    changes = {
        "reference": reference_for(db, app),
        "status": ApplicationStatus.SUBMITTED.value,
        "submitted_at": now(),
        "updated_at": now(),
    }
    _save(db, app, changes)
    app.update(changes)
    audit(db, actor_type="applicant", actor_id=None,
          action="application.resubmitted" if resubmission else "application.submitted",
          entity_type="doctor_application", entity_id=app["_id"])
    enqueue(db, channel="email", template="application_received", recipient=app["email"],
            payload={"name": app["full_name"], "reference": app["reference"]})
    return to_out(app)
