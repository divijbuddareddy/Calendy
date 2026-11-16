"""Public REST API v1 (PRD 15.2/15.3).

Auth: `Authorization: Bearer <api key | OAuth access token>` (workspace-scoped, hashed, revocable;
scopes read/write). OAuth tokens come from the authorization-code + PKCE flow in routers/oauth.py.
Every query is filtered by the key's workspace - ids from other tenants return 404 (acceptance #37).
Rate limits per key+endpoint and per tenant. Cursor pagination with a stable (created_at, id) sort.
POST /bookings requires an Idempotency-Key header; responses are replayed from IdempotencyRecord.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
from typing import Any, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import audit
from app.config import settings
from app.db import get_db
from app.entitlements import has_feature
from app.errors import (
    CALENDAR_UNAVAILABLE,
    FORBIDDEN,
    IDEMPOTENCY_CONFLICT,
    NOT_FOUND,
    VALIDATION_ERROR,
    APIError,
)
from app.models import (
    AvailabilitySchedule,
    Booking,
    Contact,
    EventType,
    EventTypeHost,
    IdempotencyRecord,
    PaymentRecord,
    WebhookSubscription,
    utcnow,
)
from app.ratelimit import enforce
from app.services import apikeys
from app.services import webhooks as webhooks_service
from app.services.providers.base import parse_dt

router = APIRouter(prefix="/api/v1", tags=["Public API v1"])
bearer = HTTPBearer(auto_error=False, description="Workspace API key (Integrations > API keys) or an OAuth 2.0 "
                                                   "access token obtained via /oauth/authorize + /oauth/token")
PER_KEY_LIMIT = 120  # requests / minute / key / endpoint
PER_TENANT_LIMIT = 1200  # requests / minute / workspace


# ------------------------------------------------------------------ auth
def principal(request: Request, creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
              db: Session = Depends(get_db)) -> apikeys.ApiPrincipal:
    header = request.headers.get("authorization") or (f"Bearer {creds.credentials}" if creds else None)
    p = apikeys.authenticate(db, header)
    if not has_feature(p.workspace, "api"):
        raise APIError(FORBIDDEN, "Your plan does not include API access", 403)
    route = request.scope.get("route")
    path = getattr(route, "path", request.url.path)
    enforce(request, f"api:{request.method}:{path}", PER_KEY_LIMIT, 60, key=p.key.id)
    enforce(request, "api:tenant", PER_TENANT_LIMIT, 60, key=p.workspace.id)
    request.state.api_principal = p
    from app.services.usage import count_usage

    count_usage(p.workspace.id, "api_calls")
    return p


def reader(p: apikeys.ApiPrincipal = Depends(principal)) -> apikeys.ApiPrincipal:
    p.require("read")
    return p


def writer(p: apikeys.ApiPrincipal = Depends(principal)) -> apikeys.ApiPrincipal:
    p.require("write")
    return p


# ------------------------------------------------------------------ helpers
def _iso(v: Optional[dt.datetime]) -> Optional[str]:
    return v.replace(microsecond=0).isoformat() + "Z" if v else None


def encode_cursor(created_at: dt.datetime, rid: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([created_at.isoformat(), rid]).encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[dt.datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        ts, rid = json.loads(raw)
        return dt.datetime.fromisoformat(ts), str(rid)
    except Exception:
        raise APIError(VALIDATION_ERROR, "Invalid cursor", 400)


def paginate(db: Session, q, model, limit: int, cursor: Optional[str], serialize) -> dict:
    """Stable keyset pagination ordered by (created_at DESC, id DESC)."""
    if cursor:
        ts, rid = decode_cursor(cursor)
        q = q.where(or_(model.created_at < ts, and_(model.created_at == ts, model.id < rid)))
    rows = db.scalars(q.order_by(model.created_at.desc(), model.id.desc()).limit(limit + 1)).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {"data": [serialize(r) for r in rows],
            "next_cursor": encode_cursor(rows[-1].created_at, rows[-1].id) if has_more and rows else None}


def _tz(name: Optional[str]) -> str:
    name = name or "UTC"
    try:
        ZoneInfo(name)
    except Exception:
        raise APIError(VALIDATION_ERROR, f"Unknown timezone '{name}'", 400)
    return name


def _event_type(db: Session, p: apikeys.ApiPrincipal, et_id: str) -> EventType:
    et = db.get(EventType, et_id)
    if not et or et.workspace_id != p.workspace.id or et.deleted_at:
        raise APIError(NOT_FOUND, "Event type not found", 404)
    return et


def _booking(db: Session, p: apikeys.ApiPrincipal, booking_id: str) -> Booking:
    b = db.get(Booking, booking_id)
    if not b or b.workspace_id != p.workspace.id:
        raise APIError(NOT_FOUND, "Booking not found", 404)
    return b


def event_type_json(db: Session, et: EventType) -> dict:
    from app.models import User

    owner = db.get(User, et.owner_user_id)
    return {
        "id": et.id, "name": et.name, "slug": et.slug, "description": et.description, "kind": et.kind,
        "duration": et.duration, "durations": et.durations_json, "color": et.color, "locations": et.location_json or [],
        "status": et.status, "visibility": et.visibility, "min_notice_minutes": et.min_notice_minutes,
        "max_days_ahead": et.max_days_ahead, "buffer_before": et.buffer_before, "buffer_after": et.buffer_after,
        "max_invitees": et.max_invitees, "price_minor": et.price_minor, "currency": et.currency,
        "owner_user_id": et.owner_user_id, "team_id": et.team_id,
        "booking_url": f"{settings.base_url.rstrip('/')}/{owner.slug}/{et.slug}" if owner and owner.slug else None,
        "created_at": _iso(et.created_at), "updated_at": _iso(et.updated_at),
    }


def booking_json(db: Session, b: Booking) -> dict:
    from app.services.notifications import manage_links

    d = webhooks_service.booking_payload(db, b.id) or {}
    d.setdefault("id", b.id)
    d["status"] = b.status
    d["manage"] = manage_links(b)
    if b.status == "pending_payment":
        d["payment_url"] = f"{settings.base_url.rstrip('/')}/pay/{b.manage_token}"
    return d


def contact_json(c: Contact) -> dict:
    return {"id": c.id, "email": c.email_normalized, "name": c.name, "phone": c.phone, "company": c.company,
            "timezone": c.timezone, "tags": c.tags_json or [], "custom_fields": c.custom_json or {},
            "unsubscribed": c.unsubscribed, "source": c.source, "created_at": _iso(c.created_at)}


def _booking_service():
    try:
        from app.services import booking as svc
    except ImportError:
        raise APIError("SERVICE_UNAVAILABLE", "Booking service unavailable", 503)
    return svc


# ------------------------------------------------------------------ schemas
class EventTypeIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: Optional[str] = Field(default=None, max_length=120)
    description: Optional[str] = None
    duration: int = Field(default=30, ge=5, le=720)
    durations: Optional[list[int]] = None
    kind: str = Field(default="one_on_one", pattern="^(one_on_one|group|collective|round_robin)$")
    locations: Optional[list[dict]] = None
    color: Optional[str] = None
    min_notice_minutes: Optional[int] = Field(default=None, ge=0)
    max_days_ahead: Optional[int] = Field(default=None, ge=1, le=730)
    buffer_before: Optional[int] = Field(default=None, ge=0)
    buffer_after: Optional[int] = Field(default=None, ge=0)
    max_invitees: Optional[int] = Field(default=None, ge=1)
    status: Optional[str] = Field(default=None, pattern="^(active|paused)$")
    visibility: Optional[str] = Field(default=None, pattern="^(public|secret)$")
    price_minor: Optional[int] = Field(default=None, ge=0)
    currency: Optional[str] = Field(default=None, min_length=3, max_length=3)


class EventTypePatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = None
    duration: Optional[int] = Field(default=None, ge=5, le=720)
    durations: Optional[list[int]] = None
    locations: Optional[list[dict]] = None
    color: Optional[str] = None
    min_notice_minutes: Optional[int] = Field(default=None, ge=0)
    max_days_ahead: Optional[int] = Field(default=None, ge=1, le=730)
    buffer_before: Optional[int] = Field(default=None, ge=0)
    buffer_after: Optional[int] = Field(default=None, ge=0)
    max_invitees: Optional[int] = Field(default=None, ge=1)
    status: Optional[str] = Field(default=None, pattern="^(active|paused)$")
    visibility: Optional[str] = Field(default=None, pattern="^(public|secret)$")
    price_minor: Optional[int] = Field(default=None, ge=0)
    currency: Optional[str] = Field(default=None, min_length=3, max_length=3)


class InviteeIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    phone: Optional[str] = Field(default=None, max_length=50)
    company: Optional[str] = Field(default=None, max_length=200)
    guests: Optional[list[EmailStr]] = None
    sms_consent: bool = False


class BookingIn(BaseModel):
    event_type_id: str
    start_at: str = Field(description="ISO-8601 start instant, e.g. 2026-11-18T14:00:00Z")
    timezone: str = "UTC"
    duration: Optional[int] = None
    invitee: InviteeIn
    answers: Optional[Any] = Field(default=None, description="{question_id: value} or [{question_id, value}]")
    utm: Optional[dict] = None


class CancelIn(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=2000)


class RescheduleIn(BaseModel):
    start_at: str
    reason: Optional[str] = Field(default=None, max_length=2000)


class WebhookIn(BaseModel):
    endpoint: str
    events: list[str] = Field(default_factory=lambda: list(webhooks_service.WEBHOOK_EVENTS))


# ------------------------------------------------------------------ me
@router.get("/me")
def me(p: apikeys.ApiPrincipal = Depends(reader)):
    from app.entitlements import plan_of

    u, ws = p.user, p.workspace
    return {
        "user": {"id": u.id, "name": u.name, "email": u.email, "slug": u.slug, "timezone": u.default_timezone,
                 "scheduling_url": f"{settings.base_url.rstrip('/')}/{u.slug}" if u.slug else None},
        "workspace": {"id": ws.id, "name": ws.name, "slug": ws.slug, "plan": plan_of(ws)["label"]},
        "api_key": {"id": p.key.id, "name": p.key.name, "prefix": p.key.prefix, "scopes": sorted(p.scopes)},
        "auth": {"type": p.kind, "scopes": sorted(p.scopes)},
    }


# ------------------------------------------------------------------ event types
@router.get("/event-types")
def list_event_types(limit: int = Query(25, ge=1, le=100), cursor: Optional[str] = None, status: Optional[str] = None,
                     p: apikeys.ApiPrincipal = Depends(reader), db: Session = Depends(get_db)):
    q = select(EventType).where(EventType.workspace_id == p.workspace.id, EventType.deleted_at.is_(None))
    if status:
        q = q.where(EventType.status == status)
    return paginate(db, q, EventType, limit, cursor, lambda et: event_type_json(db, et))


@router.post("/event-types", status_code=201)
def create_event_type(body: EventTypeIn, p: apikeys.ApiPrincipal = Depends(writer), db: Session = Depends(get_db)):
    from app.services.bootstrap import slugify

    slug = slugify(body.slug or body.name)
    if db.scalar(select(EventType.id).where(EventType.owner_user_id == p.user.id, EventType.slug == slug)):
        raise APIError("CONFLICT", f"An event type with slug '{slug}' already exists", 409)
    sched = db.scalar(select(AvailabilitySchedule).where(
        AvailabilitySchedule.user_id == p.user.id, AvailabilitySchedule.is_default.is_(True)))
    et = EventType(workspace_id=p.workspace.id, owner_user_id=p.user.id, name=body.name, slug=slug,
                   description=body.description, duration=body.duration, durations_json=body.durations, kind=body.kind,
                   location_json=body.locations or [{"type": "host_provides", "value": ""}],
                   schedule_id=sched.id if sched else None)
    for f in ("color", "min_notice_minutes", "max_days_ahead", "buffer_before", "buffer_after", "max_invitees",
              "status", "visibility", "price_minor"):
        v = getattr(body, f)
        if v is not None:
            setattr(et, f, v)
    if body.currency:
        et.currency = body.currency.upper()
    db.add(et)
    db.flush()
    db.add(EventTypeHost(event_type_id=et.id, user_id=p.user.id))
    audit(db, "event_type.created", workspace_id=p.workspace.id, actor_id=f"api_key:{p.key.id}", entity_type="event_type",
          entity_id=et.id)
    db.commit()
    return event_type_json(db, et)


@router.get("/event-types/{event_type_id}")
def get_event_type(event_type_id: str, p: apikeys.ApiPrincipal = Depends(reader), db: Session = Depends(get_db)):
    return event_type_json(db, _event_type(db, p, event_type_id))


@router.patch("/event-types/{event_type_id}")
def patch_event_type(event_type_id: str, body: EventTypePatch, p: apikeys.ApiPrincipal = Depends(writer),
                     db: Session = Depends(get_db)):
    et = _event_type(db, p, event_type_id)
    if et.locked_fields_json:
        locked = set(et.locked_fields_json) & set(body.model_dump(exclude_unset=True))
        if locked:
            raise APIError(FORBIDDEN, f"Fields locked by managed template: {', '.join(sorted(locked))}", 403)
    mapping = {"durations": "durations_json", "locations": "location_json"}
    changed = body.model_dump(exclude_unset=True)
    for k, v in changed.items():
        if k == "currency" and v:
            v = v.upper()
        setattr(et, mapping.get(k, k), v)
    et.version = (et.version or 1) + 1
    audit(db, "event_type.updated", workspace_id=p.workspace.id, actor_id=f"api_key:{p.key.id}",
          entity_type="event_type", entity_id=et.id, metadata={"fields": sorted(changed)})
    db.commit()
    return event_type_json(db, et)


# ------------------------------------------------------------------ availability
@router.get("/availability")
def availability(event_type_id: str, from_: str = Query(..., alias="from"), to: str = Query(...),
                 timezone: str = "UTC", duration: Optional[int] = None,
                 p: apikeys.ApiPrincipal = Depends(reader), db: Session = Depends(get_db)):
    et = _event_type(db, p, event_type_id)
    tz = _tz(timezone)
    try:
        start, end = dt.date.fromisoformat(from_[:10]), dt.date.fromisoformat(to[:10])
    except ValueError:
        raise APIError(VALIDATION_ERROR, "from/to must be YYYY-MM-DD dates", 400)
    if end < start or (end - start).days > 62:
        raise APIError(VALIDATION_ERROR, "Date range must be 0-62 days", 400)
    from app.services.availability import get_available_slots
    from app.services.calendar_facade import CalendarUnavailable

    try:
        slots = get_available_slots(db, et, start, end, tz, duration=duration)
    except CalendarUnavailable as exc:
        raise APIError(CALENDAR_UNAVAILABLE, f"Availability cannot be verified: {exc}", 503)
    z = ZoneInfo(tz)
    out = []
    for s in slots:
        st = s.start if isinstance(s.start, dt.datetime) else parse_dt(str(s.start))
        en = s.end if isinstance(s.end, dt.datetime) else parse_dt(str(s.end))
        if st.tzinfo:
            st, en = st.astimezone(dt.timezone.utc).replace(tzinfo=None), en.astimezone(dt.timezone.utc).replace(tzinfo=None)
        out.append({"start_at": _iso(st), "end_at": _iso(en),
                    "local_start": st.replace(tzinfo=dt.timezone.utc).astimezone(z).isoformat(),
                    "seats_remaining": getattr(s, "seats_remaining", None)})
    return {"event_type_id": et.id, "timezone": tz, "from": start.isoformat(), "to": end.isoformat(), "slots": out}


# ------------------------------------------------------------------ bookings
@router.post("/bookings", status_code=201)
def create_booking(body: BookingIn, request: Request,
                   idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
                   p: apikeys.ApiPrincipal = Depends(writer), db: Session = Depends(get_db)):
    if not idempotency_key or len(idempotency_key) > 200:
        raise APIError(VALIDATION_ERROR, "Idempotency-Key header is required (max 200 chars)", 400)
    scope = f"api:{p.workspace.id}:bookings"
    req_hash = hashlib.sha256(json.dumps(body.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
    rec = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.scope == scope, IdempotencyRecord.key == idempotency_key))
    if rec:
        if rec.request_hash != req_hash:
            raise APIError(IDEMPOTENCY_CONFLICT, "Idempotency-Key was already used with a different request body", 409)
        if rec.response_json is None:
            raise APIError(IDEMPOTENCY_CONFLICT, "A request with this Idempotency-Key is still in progress", 409)
        return JSONResponse(rec.response_json, status_code=rec.status_code or 201, headers={"Idempotent-Replayed": "true"})

    et = _event_type(db, p, body.event_type_id)
    tz = _tz(body.timezone)
    try:
        start = parse_dt(body.start_at)
    except ValueError:
        raise APIError(VALIDATION_ERROR, "start_at must be an ISO-8601 datetime", 400)
    answers = body.answers or {}
    if isinstance(answers, list):
        answers = {a.get("question_id"): a.get("value") for a in answers if isinstance(a, dict) and a.get("question_id")}
    if not isinstance(answers, dict):
        raise APIError(VALIDATION_ERROR, "answers must be an object or a list", 400)
    inv = body.invitee.model_dump(mode="json")
    if inv.get("sms_consent"):
        inv["consent"] = {"sms": True}
    svc = _booking_service()
    from app.services.calendar_facade import CalendarUnavailable

    try:
        booking = svc.create_booking(db, et, start, inv, answers, timezone=tz, duration=body.duration,
                                     idempotency_key=f"api:{p.workspace.id}:{idempotency_key}", utm=body.utm,
                                     source="api", actor="api")
    except APIError:
        db.rollback()
        raise
    except CalendarUnavailable as exc:
        db.rollback()
        raise APIError(CALENDAR_UNAVAILABLE, f"Availability cannot be verified: {exc}", 503)
    except (ValueError, LookupError) as exc:
        db.rollback()
        raise APIError("BOOKING_FAILED", str(exc) or "Booking could not be created", 409)
    db.refresh(booking)
    payload = booking_json(db, booking)
    try:
        db.add(IdempotencyRecord(scope=scope, key=idempotency_key, request_hash=req_hash, response_json=payload,
                                 status_code=201))
        db.commit()
    except IntegrityError:
        db.rollback()
    return JSONResponse(payload, status_code=201)


@router.get("/bookings")
def list_bookings(limit: int = Query(25, ge=1, le=100), cursor: Optional[str] = None, status: Optional[str] = None,
                  event_type_id: Optional[str] = None, invitee_email: Optional[str] = None,
                  start_from: Optional[str] = Query(None, alias="from"), start_to: Optional[str] = Query(None, alias="to"),
                  p: apikeys.ApiPrincipal = Depends(reader), db: Session = Depends(get_db)):
    from app.models import Invitee

    q = select(Booking).where(Booking.workspace_id == p.workspace.id)
    if status:
        q = q.where(Booking.status.in_(status.split(",")))
    if event_type_id:
        q = q.where(Booking.event_type_id == event_type_id)
    if invitee_email:
        q = q.where(Booking.id.in_(select(Invitee.booking_id).where(Invitee.email == invitee_email.strip().lower())))
    try:
        if start_from:
            q = q.where(Booking.start_at >= parse_dt(start_from if "T" in start_from else start_from + "T00:00:00"))
        if start_to:
            q = q.where(Booking.start_at < parse_dt(start_to if "T" in start_to else start_to + "T00:00:00"))
    except ValueError:
        raise APIError(VALIDATION_ERROR, "from/to must be ISO dates", 400)
    return paginate(db, q, Booking, limit, cursor, lambda b: booking_json(db, b))


@router.get("/bookings/{booking_id}")
def get_booking(booking_id: str, p: apikeys.ApiPrincipal = Depends(reader), db: Session = Depends(get_db)):
    return booking_json(db, _booking(db, p, booking_id))


@router.post("/bookings/{booking_id}/cancel")
def cancel_booking(booking_id: str, body: CancelIn = CancelIn(), p: apikeys.ApiPrincipal = Depends(writer),
                   db: Session = Depends(get_db)):
    b = _booking(db, p, booking_id)
    if b.status == "cancelled":
        return booking_json(db, b)
    if b.status not in ("confirmed", "pending", "pending_payment", "held"):
        raise APIError("INVALID_STATE", f"Booking is {b.status} and cannot be cancelled", 409)
    _booking_service().cancel_booking(db, b, "api", reason=body.reason)
    db.refresh(b)
    return booking_json(db, b)


@router.post("/bookings/{booking_id}/reschedule")
def reschedule_booking(booking_id: str, body: RescheduleIn, p: apikeys.ApiPrincipal = Depends(writer),
                       db: Session = Depends(get_db)):
    b = _booking(db, p, booking_id)
    if b.status not in ("confirmed", "pending"):
        raise APIError("INVALID_STATE", f"Booking is {b.status} and cannot be rescheduled", 409)
    try:
        start = parse_dt(body.start_at)
    except ValueError:
        raise APIError(VALIDATION_ERROR, "start_at must be an ISO-8601 datetime", 400)
    new = _booking_service().reschedule_booking(db, b, start, "api", reason=body.reason)
    return booking_json(db, new)


@router.post("/bookings/{booking_id}/no-show")
def mark_no_show(booking_id: str, p: apikeys.ApiPrincipal = Depends(writer), db: Session = Depends(get_db)):
    b = _booking(db, p, booking_id)
    _booking_service().mark_no_show(db, b, "api", value=True)
    db.refresh(b)
    return booking_json(db, b)


# ------------------------------------------------------------------ contacts
@router.get("/contacts")
def list_contacts(limit: int = Query(25, ge=1, le=100), cursor: Optional[str] = None, email: Optional[str] = None,
                  p: apikeys.ApiPrincipal = Depends(reader), db: Session = Depends(get_db)):
    q = select(Contact).where(Contact.workspace_id == p.workspace.id, Contact.deleted_at.is_(None))
    if email:
        q = q.where(Contact.email_normalized == email.strip().lower())
    return paginate(db, q, Contact, limit, cursor, contact_json)


# ------------------------------------------------------------------ webhook subscriptions
def _webhooks_enabled(p: apikeys.ApiPrincipal):
    if not has_feature(p.workspace, "webhooks"):
        raise APIError(FORBIDDEN, "Your plan does not include webhooks", 403)


@router.post("/webhook-subscriptions", status_code=201)
def create_webhook(body: WebhookIn, p: apikeys.ApiPrincipal = Depends(writer), db: Session = Depends(get_db)):
    _webhooks_enabled(p)
    try:
        sub, secret = webhooks_service.create_subscription(db, p.workspace.id, body.endpoint, body.events)
    except webhooks_service.InvalidEndpoint as exc:
        raise APIError(VALIDATION_ERROR, str(exc), 400)
    audit(db, "webhook.created", workspace_id=p.workspace.id, actor_id=f"api_key:{p.key.id}", entity_type="webhook",
          entity_id=sub.id)
    db.commit()
    return {**webhooks_service.subscription_json(sub), "secret": secret}


@router.get("/webhook-subscriptions")
def list_webhooks(limit: int = Query(25, ge=1, le=100), cursor: Optional[str] = None,
                  p: apikeys.ApiPrincipal = Depends(reader), db: Session = Depends(get_db)):
    q = select(WebhookSubscription).where(WebhookSubscription.workspace_id == p.workspace.id)
    return paginate(db, q, WebhookSubscription, limit, cursor, webhooks_service.subscription_json)


@router.delete("/webhook-subscriptions/{sub_id}", status_code=204)
def delete_webhook(sub_id: str, p: apikeys.ApiPrincipal = Depends(writer), db: Session = Depends(get_db)):
    from app.models import WebhookDelivery

    sub = db.get(WebhookSubscription, sub_id)
    if not sub or sub.workspace_id != p.workspace.id:
        raise APIError(NOT_FOUND, "Webhook subscription not found", 404)
    db.query(WebhookDelivery).filter(WebhookDelivery.subscription_id == sub.id).delete()
    db.delete(sub)
    audit(db, "webhook.deleted", workspace_id=p.workspace.id, actor_id=f"api_key:{p.key.id}", entity_id=sub_id)
    db.commit()
    return Response(status_code=204)


# ------------------------------------------------------------------ analytics
@router.get("/analytics/summary")
def analytics_summary(start_from: Optional[str] = Query(None, alias="from"), start_to: Optional[str] = Query(None, alias="to"),
                      p: apikeys.ApiPrincipal = Depends(reader), db: Session = Depends(get_db)):
    from app.services.payments import revenue_summary

    now = utcnow()
    try:
        frm = dt.datetime.fromisoformat(start_from[:10]) if start_from else now - dt.timedelta(days=30)
        to = dt.datetime.fromisoformat(start_to[:10]) + dt.timedelta(days=1) if start_to else now + dt.timedelta(days=1)
    except ValueError:
        raise APIError(VALIDATION_ERROR, "from/to must be YYYY-MM-DD", 400)
    base = (Booking.workspace_id == p.workspace.id, Booking.created_at >= frm, Booking.created_at < to,
            Booking.status.notin_(("held", "expired")))
    by_status = dict(db.execute(select(Booking.status, func.count()).where(*base).group_by(Booking.status)).all())
    no_shows = db.scalar(select(func.count()).select_from(Booking).where(*base, Booking.no_show.is_(True))) or 0
    upcoming = db.scalar(select(func.count()).select_from(Booking).where(
        Booking.workspace_id == p.workspace.id, Booking.status == "confirmed", Booking.start_at >= now)) or 0
    completed = db.scalar(select(func.count()).select_from(Booking).where(
        *base, Booking.status == "confirmed", Booking.end_at < now, Booking.no_show.is_(False))) or 0
    by_et = db.execute(select(Booking.event_type_id, EventType.name, func.count()).join(
        EventType, EventType.id == Booking.event_type_id).where(*base).group_by(Booking.event_type_id, EventType.name)).all()
    by_source = dict(db.execute(select(Booking.source, func.count()).where(*base).group_by(Booking.source)).all())
    return {
        "from": frm.date().isoformat(), "to": (to - dt.timedelta(days=1)).date().isoformat(),
        "bookings": {"created": sum(by_status.values()), "by_status": by_status, "upcoming": upcoming,
                     "completed": completed, "cancelled": by_status.get("cancelled", 0),
                     "rescheduled": by_status.get("rescheduled", 0), "no_show": no_shows},
        "by_event_type": [{"event_type_id": r[0], "name": r[1], "count": r[2]} for r in by_et],
        "by_source": {k or "unknown": v for k, v in by_source.items()},
        "revenue": revenue_summary(db, p.workspace.id, frm, to),
        "payments": {"succeeded": db.scalar(select(func.count()).select_from(PaymentRecord).where(
            PaymentRecord.workspace_id == p.workspace.id, PaymentRecord.status == "succeeded",
            PaymentRecord.created_at >= frm, PaymentRecord.created_at < to)) or 0},
    }
