from bson import ObjectId
from pymongo.database import Database

from app.db import now


def audit(
    db: Database,
    *,
    actor_type: str,
    actor_id: str | None,
    action: str,
    entity_type: str,
    entity_id: ObjectId | str,
    data: dict | None = None,
) -> None:
    db.audit_log.insert_one(
        {
            "actor_type": actor_type,
            "actor_id": actor_id,
            "action": action,
            "entity_type": entity_type,
            "entity_id": str(entity_id),
            "data": data or {},
            "created_at": now(),
        }
    )


def event(type_: str, actor_type: str, actor_id: str | None = None, data: dict | None = None) -> dict:
    """An entry for an appointment's embedded `events` timeline."""
    return {"type": type_, "actor_type": actor_type, "actor_id": actor_id, "data": data or {}, "at": now()}


def appointment_event(db: Database, appointment_id: ObjectId, type_: str, actor_type: str,
                      actor_id: str | None = None, data: dict | None = None) -> None:
    db.appointments.update_one({"_id": appointment_id}, {"$push": {"events": event(type_, actor_type, actor_id, data)}})
