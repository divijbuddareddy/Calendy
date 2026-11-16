"""Enterprise admin services (PRD 17.2): organisation-wide invitee data deletion and audit search.

`delete_invitee_data` is used by the admin UI, the session JSON API (/app/api/admin/data-deletion)
and the API-key endpoint (/api/v1/admin/data-deletion) in app.routers.admin.
"""
import datetime as dt
import json
from typing import Optional

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session

from app.audit import audit
from app.models import (
    AuditLog,
    Booking,
    BookingAnswer,
    Contact,
    DataRequest,
    Invitee,
    MeetingPoll,
    PollVote,
    RoutingSubmission,
    User,
    WaitlistEntry,
    utcnow,
)
from app.security import hash_secret


def delete_invitee_data(db: Session, workspace_id: str, email: str, actor_id: Optional[str], ip: Optional[str] = None) -> dict:
    """Anonymise every trace of an invitee email across one workspace. Caller commits."""
    email_n = email.strip().lower()
    counts = {"invitees": 0, "answers": 0, "contacts": 0, "routing_submissions": 0, "poll_votes": 0, "waitlist": 0}
    invitees = db.scalars(
        select(Invitee).join(Booking, Booking.id == Invitee.booking_id).where(
            Booking.workspace_id == workspace_id, func.lower(Invitee.email) == email_n
        )
    ).all()
    booking_ids = set()
    for inv in invitees:
        booking_ids.add(inv.booking_id)
        inv.name = "Deleted"
        inv.email = f"deleted+{inv.id}@redacted.invalid"
        inv.phone = None
        inv.company = None
        inv.consent_json = None
        inv.guests_json = None
        counts["invitees"] += 1
    if booking_ids:
        for a in db.scalars(select(BookingAnswer).where(BookingAnswer.booking_id.in_(booking_ids))).all():
            a.answer_json = "[deleted]"
            counts["answers"] += 1
        for b in db.scalars(select(Booking).where(Booking.id.in_(booking_ids))).all():
            b.host_notes = None
            b.utm_json = None
    for c in db.scalars(select(Contact).where(Contact.workspace_id == workspace_id, Contact.email_normalized == email_n)).all():
        c.email_normalized = f"deleted+{c.id}@redacted.invalid"
        c.name = "Deleted"
        c.phone = c.company = c.notes = c.timezone = None
        c.custom_json = None
        c.tags_json = None
        c.unsubscribed = True
        c.deleted_at = c.deleted_at or utcnow()
        counts["contacts"] += 1
    for s in db.scalars(select(RoutingSubmission).where(RoutingSubmission.workspace_id == workspace_id)).all():
        vals = (s.answers_json or {}).values()
        if any(email_n in str(v).lower() for v in vals):
            s.answers_json = {k: "[deleted]" for k in (s.answers_json or {})}
            counts["routing_submissions"] += 1
    for v in db.scalars(
        select(PollVote).join(MeetingPoll, MeetingPoll.id == PollVote.poll_id).where(
            MeetingPoll.workspace_id == workspace_id, func.lower(PollVote.email) == email_n
        )
    ).all():
        v.name = "Deleted"
        v.email = f"deleted+{v.id}@redacted.invalid"
        counts["poll_votes"] += 1
    for w in db.scalars(
        select(WaitlistEntry).where(WaitlistEntry.workspace_id == workspace_id, func.lower(WaitlistEntry.email) == email_n)
    ).all():
        w.name = "Deleted"
        w.email = f"deleted+{w.id}@redacted.invalid"
        counts["waitlist"] += 1
    for extra in _extra_deleters():
        try:
            counts.update(extra(db, workspace_id, email_n) or {})
        except Exception:  # pragma: no cover - an optional module must not block deletion
            import logging

            logging.getLogger("app.enterprise").exception("data deletion hook failed")
    dr = DataRequest(user_id=actor_id or "system", workspace_id=workspace_id, kind="deletion", status="completed",
                     result_json={"email_hash": hash_secret(email_n), **counts}, completed_at=utcnow())
    db.add(dr)
    # Never store the raw email in the audit log - only a keyed hash.
    audit(db, "data.invitee_deleted", workspace_id=workspace_id, actor_id=actor_id, entity_type="data_request",
          entity_id=dr.id, metadata={"email_hash": hash_secret(email_n), **counts}, ip=ip)
    db.flush()
    return counts


def _extra_deleters():
    """Deletion hooks of optional modules (CRM mirror, ...)."""
    out = []
    try:
        from app.services import crm

        out.append(crm.delete_email_data)
    except Exception:  # pragma: no cover
        pass
    return out


# --------------------------------------------------------------------------- audit search
def audit_query(db: Session, workspace_id: str, qp) -> tuple:
    """Build the audit-log search query from request params:
    q (free text over action/entity/actor/metadata), action, actor (email or id), entity, from, to (dates)."""
    q = select(AuditLog).where(AuditLog.workspace_id == workspace_id)
    text = (qp.get("q") or "").strip()
    action = (qp.get("action") or "").strip()
    actor = (qp.get("actor") or "").strip()
    entity = (qp.get("entity") or "").strip()
    if text:
        like = f"%{text}%"
        uids = db.scalars(select(User.id).where(User.email.ilike(like))).all()
        q = q.where(or_(AuditLog.action.ilike(like), AuditLog.entity_type.ilike(like), AuditLog.entity_id.ilike(like),
                        AuditLog.actor_id.ilike(like), AuditLog.ip.ilike(like),
                        cast(AuditLog.metadata_json, String).ilike(like),
                        AuditLog.actor_id.in_(uids or [""])))
    if action:
        q = q.where(AuditLog.action.ilike(f"%{action}%"))
    if actor:
        uid = db.scalar(select(User.id).where(User.email == actor.lower()))
        q = q.where(AuditLog.actor_id == (uid or actor))
    if entity:
        q = q.where((AuditLog.entity_type.ilike(f"%{entity}%")) | (AuditLog.entity_id == entity))
    for key in ("from", "to"):
        v = qp.get(key)
        if not v:
            continue
        try:
            d = dt.date.fromisoformat(v)
        except ValueError:
            continue
        if key == "from":
            q = q.where(AuditLog.created_at >= dt.datetime.combine(d, dt.time.min))
        else:
            q = q.where(AuditLog.created_at < dt.datetime.combine(d + dt.timedelta(days=1), dt.time.min))
    params = {"q": text, "action": action, "actor": actor, "entity": entity,
              "from": qp.get("from", ""), "to": qp.get("to", "")}
    return q.order_by(AuditLog.created_at.desc()), params


def audit_row_json(r: AuditLog, actors: dict) -> dict:
    return {"id": r.id, "created_at": r.created_at.isoformat() + "Z", "action": r.action, "actor_id": r.actor_id,
            "actor_email": actors.get(r.actor_id), "entity_type": r.entity_type, "entity_id": r.entity_id,
            "ip": r.ip, "metadata": json.loads(json.dumps(r.metadata_json or {}, default=str))}
