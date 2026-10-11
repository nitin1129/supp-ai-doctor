from sqlalchemy.orm import Session

from app.models import AppointmentEvent, AuditLog


def audit(
    db: Session,
    *,
    actor_type: str,
    actor_id: str | None,
    action: str,
    entity_type: str,
    entity_id: int | str,
    data: dict | None = None,
) -> None:
    db.add(
        AuditLog(
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id),
            data=data or {},
        )
    )


def appointment_event(
    db: Session,
    appointment_id: int,
    type_: str,
    actor_type: str,
    actor_id: str | None = None,
    data: dict | None = None,
) -> None:
    db.add(
        AppointmentEvent(
            appointment_id=appointment_id,
            type=type_,
            actor_type=actor_type,
            actor_id=actor_id,
            data=data or {},
        )
    )
