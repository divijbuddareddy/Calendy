"""Small API surface for the browser extension and mobile quick actions (PRD P3: mobile + browser
extension). Authenticated with the same workspace API keys / OAuth tokens as /api/v1 (and the same
rate limits, plan check and scopes), tenant-scoped to the key's workspace.

    GET  /api/v1/ext/event-types                         shareable event types with booking URLs
    POST /api/v1/ext/event-types/{id}/single-use-link    create a single-use link   (write scope)
    GET  /api/v1/ext/upcoming?limit=10                   upcoming meetings, soonest first, with local times
"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.audit import audit
from app.db import get_db
from app.deps import _fmt_dt
from app.entitlements import has_feature
from app.errors import FORBIDDEN, APIError
from app.models import Booking, BookingHost, EventType, WorkspaceMember, utcnow
from app.routers.api_v1 import reader, writer
from app.services import apikeys
from app.services.hostutil import event_public_url

router = APIRouter(prefix="/api/v1/ext", tags=["Extension"])

ROLE_ORDER = {"viewer": 0, "member": 1, "admin": 2, "owner": 3}


def _role(db: Session, p: apikeys.ApiPrincipal) -> str:
    m = db.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == p.workspace.id,
                                                WorkspaceMember.user_id == p.user.id,
                                                WorkspaceMember.status == "active"))
    return m.role if m else "viewer"


def _shareable(db: Session, p: apikeys.ApiPrincipal):
    return (select(EventType).where(
        EventType.workspace_id == p.workspace.id, EventType.deleted_at.is_(None), EventType.status == "active",
        EventType.single_use.is_(False), EventType.is_one_off.is_(False), EventType.is_managed_template.is_(False),
    ).order_by(EventType.created_at))


def _et_out(db: Session, et: EventType, user_id: str) -> dict:
    return {"id": et.id, "name": et.name, "duration": et.duration, "kind": et.kind, "color": et.color,
            "url": event_public_url(db, et), "mine": et.owner_user_id == user_id, "team_id": et.team_id}


@router.get("/event-types")
def ext_event_types(p: apikeys.ApiPrincipal = Depends(reader), db: Session = Depends(get_db)):
    rows = db.scalars(_shareable(db, p)).all()
    mine_first = sorted(rows, key=lambda e: (e.owner_user_id != p.user.id, e.name.lower()))
    return {"data": [_et_out(db, e, p.user.id) for e in mine_first],
            "profile_url": f"{event_public_url(db, rows[0]).rsplit('/', 1)[0]}" if rows and not rows[0].team_id else None}


@router.post("/event-types/{event_type_id}/single-use-link", status_code=201)
def ext_single_use(event_type_id: str, p: apikeys.ApiPrincipal = Depends(writer), db: Session = Depends(get_db)):
    from app.routers.event_types import clone_event, slug_taken

    src = db.scalar(_shareable(db, p).where(EventType.id == event_type_id))
    if not src:
        raise APIError("NOT_FOUND", "Event type not found", 404)
    if not has_feature(p.workspace, "single_use"):
        raise APIError(FORBIDDEN, "Single-use links are not included in your plan", 403)
    role = _role(db, p)
    if role == "viewer" or (src.owner_user_id != p.user.id and ROLE_ORDER.get(role, 0) < ROLE_ORDER["admin"]
                            and p.user.id not in (src.shared_with_json or [])):
        raise APIError(FORBIDDEN, "You can only create single-use links for event types you manage", 403)
    slug = f"su-{secrets.token_hex(4)}"
    while slug_taken(db, src.owner_user_id, slug):
        slug = f"su-{secrets.token_hex(4)}"
    ev = clone_event(db, src, slug=slug, visibility="secret", single_use=True, status="active",
                     is_managed_template=False, is_one_off=False, managed_template_id=src.id)
    audit(db, "event_type.created", workspace_id=p.workspace.id, actor_id=p.user.id, entity_type="event_type",
          entity_id=ev.id, metadata={"single_use_of": src.id, "via": "extension"})
    db.commit()
    return {"id": ev.id, "url": event_public_url(db, ev), "source_event_type_id": src.id}


@router.get("/upcoming")
def ext_upcoming(limit: int = Query(10, ge=1, le=50), p: apikeys.ApiPrincipal = Depends(reader),
                 db: Session = Depends(get_db)):
    q = select(Booking).where(Booking.workspace_id == p.workspace.id, Booking.end_at >= utcnow(),
                              Booking.status.in_(("confirmed", "pending", "pending_payment")))
    if ROLE_ORDER.get(_role(db, p), 0) < ROLE_ORDER["admin"]:  # members see the meetings they host
        hosted = select(BookingHost.booking_id).where(BookingHost.user_id == p.user.id)
        q = q.where(or_(Booking.host_id == p.user.id, Booking.id.in_(hosted)))
    rows = db.scalars(q.order_by(Booking.start_at).limit(limit)).all()
    tz = p.user.default_timezone or "UTC"
    out = []
    for b in rows:
        et = db.get(EventType, b.event_type_id)
        inv = b.invitees[0] if b.invitees else None
        out.append({
            "id": b.id, "status": b.status, "event_type": et.name if et else None,
            "start_at": b.start_at.isoformat() + "Z", "end_at": b.end_at.isoformat() + "Z",
            "start_local": _fmt_dt(b.start_at, tz), "end_local": _fmt_dt(b.end_at, tz, "%H:%M"), "timezone": tz,
            "invitee": {"name": inv.name, "email": inv.email} if inv else None,
            "join_url": (b.location_json or {}).get("join_url") if isinstance(b.location_json, dict) else None,
        })
    return {"data": out, "timezone": tz}
