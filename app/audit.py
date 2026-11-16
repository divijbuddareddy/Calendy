"""Audit logging of critical actions (PRD 17.1). Call within the caller's transaction."""
from typing import Optional

from sqlalchemy.orm import Session

from app.models import AuditLog


def audit(
    db: Session,
    action: str,
    *,
    workspace_id: Optional[str] = None,
    actor_id: Optional[str] = None,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    metadata: Optional[dict] = None,
    ip: Optional[str] = None,
) -> AuditLog:
    row = AuditLog(
        workspace_id=workspace_id,
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        metadata_json=metadata or {},
        ip=ip,
    )
    db.add(row)
    return row
