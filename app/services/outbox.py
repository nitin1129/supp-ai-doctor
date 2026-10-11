"""Queue notifications and timed jobs in the outbox collection; app.worker delivers them.

Without multi-document transactions, an outbox entry is written right after
the change it belongs to. Jobs re-check the appointment's state before acting,
so an entry that outlives its reason does nothing.
"""

from datetime import datetime

from bson import ObjectId
from pymongo.database import Database

from app.db import now


def enqueue(
    db: Database,
    *,
    channel: str,
    template: str,
    recipient: str | None = None,
    payload: dict | None = None,
    due_at: datetime | None = None,
    appointment_id: ObjectId | None = None,
) -> None:
    t = now()
    db.outbox.insert_one(
        {
            "channel": channel,
            "template": template,
            "recipient": recipient,
            "payload": payload or {},
            "appointment_id": appointment_id,
            "due_at": due_at or t,
            "status": "pending",
            "attempts": 0,
            "last_error": None,
            "sent_at": None,
            "created_at": t,
        }
    )


def cancel_pending_for_appointment(db: Database, appointment_id: ObjectId, templates: list[str]) -> None:
    db.outbox.update_many(
        {"appointment_id": appointment_id, "status": "pending", "template": {"$in": templates}},
        {"$set": {"status": "cancelled"}},
    )
