"""Queue notifications and timed jobs in the outbox table.

Writing to the outbox inside the same transaction as the business change means
a message is sent if and only if the change commits. app.worker delivers them.
"""

from datetime import UTC, datetime

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models import Outbox


def enqueue(
    db: Session,
    *,
    channel: str,
    template: str,
    recipient: str | None = None,
    payload: dict | None = None,
    due_at: datetime | None = None,
    appointment_id: int | None = None,
) -> None:
    db.add(
        Outbox(
            channel=channel,
            template=template,
            recipient=recipient,
            payload=payload or {},
            due_at=due_at or datetime.now(UTC),
            appointment_id=appointment_id,
        )
    )


def cancel_pending_for_appointment(db: Session, appointment_id: int, templates: list[str]) -> None:
    db.execute(
        update(Outbox)
        .where(
            Outbox.appointment_id == appointment_id,
            Outbox.status == "pending",
            Outbox.template.in_(templates),
        )
        .values(status="cancelled")
    )
