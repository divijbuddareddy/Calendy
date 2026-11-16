"""Public booking JSON API (PRD 22). Prefix /api/v1/public; rate limited; CSRF-exempt (no cookie auth).

GET  /api/v1/public/event-types/{owner_slug}/{event_slug}
GET  /api/v1/public/event-types/{owner_slug}/{event_slug}/availability?from=&to=&timezone=&duration=
POST /api/v1/public/bookings                       (Idempotency-Key header required)
GET  /api/v1/public/bookings/{manage_token}
POST /api/v1/public/bookings/{manage_token}/cancel
POST /api/v1/public/bookings/{manage_token}/reschedule
POST /api/v1/public/verify-email
POST /api/v1/public/waitlist

owner_slug is a user's public slug, or "team:{team_slug}" for team event types.
"""
from __future__ import annotations

import datetime as dt
import secrets
from typing import Any, Optional, Union

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.errors import CALENDAR_UNAVAILABLE, NOT_FOUND, VALIDATION_ERROR, APIError
from app.models import Booking, EventType, Team, User, VerificationCode, WaitlistEntry
from app.ratelimit import client_ip, enforce
from app.security import hash_secret
from app.services import availability as av
from app.services import booking as bk
from app.services import timeutil as tu
from app.services.calendar_facade import CalendarUnavailable
from app.services.messaging import send_email

router = APIRouter(prefix="/api/v1/public", tags=["public"])

VERIFY_PURPOSE = "invitee_booking"
HIDDEN_LOCATION_VALUES = {"custom_url", "google_meet", "zoom", "teams"}  # revealed after booking only


# ------------------------------------------------------------------ resolution helpers (also used by public.py)
def resolve_owner(db: Session, owner_slug: str) -> tuple[Optional[User], Optional[Team]]:
    if owner_slug.startswith("team:"):
        team = db.scalar(select(Team).where(Team.slug == owner_slug[5:]))
        return None, team
    user = db.scalar(select(User).where(User.slug == owner_slug, User.deleted_at.is_(None)))
    if user:
        return user, None
    return None, db.scalar(select(Team).where(Team.slug == owner_slug))


def find_event(db: Session, owner_slug: str, event_slug: str) -> tuple[EventType, Optional[User], Optional[Team]]:
    user, team = resolve_owner(db, owner_slug)
    et = None
    if user:
        et = db.scalar(select(EventType).where(EventType.owner_user_id == user.id, EventType.slug == event_slug,
                                               EventType.deleted_at.is_(None)))
    elif team:
        et = db.scalar(select(EventType).where(EventType.team_id == team.id, EventType.slug == event_slug,
                                               EventType.deleted_at.is_(None)))
    if not et or et.is_managed_template:
        raise APIError(NOT_FOUND, "Event type not found", 404)
    if user is None:
        user = db.get(User, et.owner_user_id)
    return et, user, team


def require_bookable(et: EventType):
    if et.status != "active":
        raise APIError("EVENT_UNAVAILABLE", "This event isn't accepting bookings right now.", 404)
    if et.single_use and et.used_at:
        raise APIError("EVENT_UNAVAILABLE", "This single-use link has already been used.", 410)


def booking_by_token(db: Session, token: str, now: Optional[dt.datetime] = None) -> Booking:
    b = db.scalar(select(Booking).where(Booking.manage_token == token)) if token else None
    if not b:
        raise APIError(NOT_FOUND, "Booking not found", 404)
    if b.manage_token_expires_at and b.manage_token_expires_at < (now or tu.utcnow()):
        raise APIError("LINK_EXPIRED", "This management link has expired.", 410)
    return b


def public_location_options(et: EventType) -> list[dict]:
    out = []
    for i, o in enumerate(et.location_json or []):
        if not isinstance(o, dict) or not o.get("type"):
            continue
        out.append({
            "index": i,
            "type": o["type"],
            "label": o.get("label") or location_label(o["type"]),
            "value": None if o["type"] in HIDDEN_LOCATION_VALUES or o["type"] == "phone" else o.get("value"),
        })
    return out


def location_label(t: str) -> str:
    return {
        "google_meet": "Google Meet", "zoom": "Zoom", "teams": "Microsoft Teams", "phone": "Phone call (host calls or shares number)",
        "invitee_phone": "Phone call (host will call you)", "in_person": "In person", "custom_url": "Web conferencing",
        "host_provides": "Host will provide details",
    }.get(t, t.replace("_", " ").title())


def event_public_dict(db: Session, et: EventType, owner: Optional[User], team: Optional[Team]) -> dict:
    pol = et.cancellation_policy_json or {}
    return {
        "id": et.id,
        "name": et.name,
        "slug": et.slug,
        "description": et.description,
        "kind": et.kind,
        "color": et.color,
        "status": et.status,
        "duration": et.duration,
        "durations": av.allowed_durations(et),
        "locations": public_location_options(et),
        "price_minor": et.price_minor,
        "currency": et.currency,
        "max_invitees": et.max_invitees if et.kind == "group" else None,
        "show_remaining_seats": et.show_remaining_seats,
        "waitlist_enabled": et.waitlist_enabled,
        "requires_email_verification": et.requires_email_verification,
        "cancellation_policy": {
            "allow_cancel": pol.get("allow_cancel", True), "allow_reschedule": pol.get("allow_reschedule", True),
            "min_notice_minutes": pol.get("min_notice_minutes"), "policy_text": pol.get("policy_text"),
        },
        "host": {"name": owner.name, "avatar_url": owner.avatar_url, "headline": owner.headline} if owner else None,
        "team": {"name": team.name, "slug": team.slug, "logo_url": team.logo_url} if team else None,
        "questions": [
            {"id": q.id, "type": q.type, "label": q.label, "help_text": q.help_text, "required": q.required,
             "options": bk._norm_options(q.options_json), "sort_order": q.sort_order, "prefill_key": q.prefill_key}
            for q in sorted(et.questions, key=lambda q: q.sort_order or 0)
        ],
    }


def booking_response(b: Booking) -> dict:
    d = bk.booking_to_dict(b)
    if b.status == "pending_payment":
        next_url = f"/pay/{b.manage_token}"
    else:
        next_url = f"/booking/{b.manage_token}/confirmed"
    return {
        "booking_id": b.id,
        "status": b.status,
        "start_at": d["start_at"],
        "end_at": d["end_at"],
        "timezone": b.timezone,
        "location": d["location"],
        "event_type": d["event_type"],
        "manage": d["manage"],
        "payment_status": b.payment_status,
        "next_url": next_url,
        "manage_token": b.manage_token,
    }


def _parse_date(v: Optional[str], name: str) -> Optional[dt.date]:
    if not v:
        return None
    try:
        return dt.date.fromisoformat(v[:10])
    except ValueError:
        raise APIError(VALIDATION_ERROR, f"Invalid {name} date (use YYYY-MM-DD)", 422)


def availability_payload(db: Session, et: EventType, start: dt.date, end: dt.date, tz: str, duration: Optional[int],
                         exclude_booking_id: Optional[str] = None, for_reschedule: bool = False, now=None) -> dict:
    try:
        dur = av.resolve_duration(et, duration)
    except ValueError as exc:
        raise APIError(VALIDATION_ERROR, str(exc), 422)
    include_full = et.kind == "group" and et.waitlist_enabled and not for_reschedule
    try:
        slots = av.get_available_slots(db, et, start, end, tz, dur, exclude_booking_id=exclude_booking_id, now=now,
                                       include_full=include_full, for_reschedule=for_reschedule)
    except CalendarUnavailable:
        raise APIError(CALENDAR_UNAVAILABLE,
                       "The host's calendar can't be checked right now, so no times can be offered. Please try again later.", 503)
    days: dict[str, list] = {}
    for s in slots:
        d = s.to_dict(tz)
        if et.kind == "group" and not et.show_remaining_seats:
            d["seats_remaining"] = None
        days.setdefault(tu.local_date(s.start, tz).isoformat(), []).append(d)
    return {
        "event_type_id": et.id,
        "timezone": tz,
        "duration": dur,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "days": [{"date": k, "slots": v} for k, v in sorted(days.items())],
        "availability_version": av.availability_version(slots),
    }


# ------------------------------------------------------------------ endpoints
@router.get("/event-types/{owner_slug}/{event_slug}")
def get_event_type(owner_slug: str, event_slug: str, request: Request, db: Session = Depends(get_db)):
    enforce(request, "public_read", 120, 60)
    et, owner, team = find_event(db, owner_slug, event_slug)
    require_bookable(et)
    return event_public_dict(db, et, owner, team)


@router.get("/event-types/{owner_slug}/{event_slug}/availability")
def get_availability(
    owner_slug: str,
    event_slug: str,
    request: Request,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    timezone: str = "UTC",
    duration: Optional[int] = None,
    reschedule_token: Optional[str] = None,
    db: Session = Depends(get_db),
):
    enforce(request, "public_availability", 120, 60)
    et, _, _ = find_event(db, owner_slug, event_slug)
    exclude = None
    for_reschedule = False
    if reschedule_token:
        b = booking_by_token(db, reschedule_token)
        if b.event_type_id != et.id:
            raise APIError(NOT_FOUND, "Booking not found", 404)
        exclude, for_reschedule = b.id, True
        duration = int((b.end_at - b.start_at).total_seconds() // 60)
    else:
        require_bookable(et)
    if not tu.is_valid_tz(timezone):
        raise APIError(VALIDATION_ERROR, "Unknown timezone", 422)
    start = _parse_date(from_, "from") or tu.local_date(tu.utcnow(), timezone)
    end = _parse_date(to, "to") or start + dt.timedelta(days=30)
    if end < start:
        raise APIError(VALIDATION_ERROR, "'to' must be on or after 'from'", 422)
    if (end - start).days > 62:
        end = start + dt.timedelta(days=62)
    return availability_payload(db, et, start, end, timezone, duration, exclude, for_reschedule)


class BookingIn(BaseModel):
    event_type_id: Optional[str] = None
    event_type_slug: Optional[str] = None
    owner_slug: Optional[str] = None
    start_at: str
    timezone: str = "UTC"
    slot_token: Optional[str] = None
    invitee: dict[str, Any]
    answers: Union[list[dict[str, Any]], dict[str, Any]] = []
    consent: Optional[dict[str, Any]] = None
    utm: Optional[dict[str, Any]] = None
    duration: Optional[int] = None
    location: Optional[str] = None
    verification_code: Optional[str] = None
    website: Optional[str] = None  # honeypot - must stay empty
    rsid: Optional[str] = None
    routing_submission_id: Optional[str] = None
    source: Optional[str] = None


UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content")


def verify_code(db: Session, email: str, code: Optional[str], now: dt.datetime):
    """Consume a valid invitee email-verification code or raise APIError (also used by the no-JS form)."""
    if not code:
        raise APIError("EMAIL_VERIFICATION_REQUIRED", "Please verify your email address with the code we sent you.", 422)
    rows = db.scalars(
        select(VerificationCode).where(VerificationCode.email == email, VerificationCode.purpose == VERIFY_PURPOSE,
                                       VerificationCode.consumed_at.is_(None), VerificationCode.expires_at > now)
        .order_by(VerificationCode.created_at.desc())
    ).all()
    for vc in rows[:3]:
        if vc.attempts >= 5:
            continue
        if secrets.compare_digest(vc.code_hash, hash_secret(f"{email}:{code.strip()}")):
            vc.consumed_at = now
            db.commit()
            return
        vc.attempts += 1
    db.commit()
    raise APIError("EMAIL_VERIFICATION_FAILED", "That verification code is incorrect or has expired.", 422)


@router.post("/bookings", status_code=201)
def create_public_booking(
    body: BookingIn,
    request: Request,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
):
    enforce(request, "public_booking", 20, 60)
    if not idempotency_key or len(idempotency_key) > 150:
        raise APIError(VALIDATION_ERROR, "Idempotency-Key header is required.", 400)
    key = f"pub:{idempotency_key}"
    if body.website:
        raise APIError(VALIDATION_ERROR, "Unable to process this booking.", 422)
    existing = db.scalar(select(Booking).where(Booking.idempotency_key == key))
    if existing:
        return JSONResponse(booking_response(existing), status_code=200)
    if body.event_type_id:
        et = db.get(EventType, body.event_type_id)
        if not et or et.deleted_at or et.is_managed_template:
            raise APIError(NOT_FOUND, "Event type not found", 404)
    elif body.owner_slug and body.event_type_slug:
        et, _, _ = find_event(db, body.owner_slug, body.event_type_slug)
    else:
        raise APIError(VALIDATION_ERROR, "event_type_id or owner_slug + event_type_slug is required", 422)
    require_bookable(et)
    try:
        start = tu.parse_iso_utc(body.start_at)
    except ValueError:
        raise APIError(VALIDATION_ERROR, "start_at must be an ISO-8601 timestamp", 422)
    try:
        dur = av.resolve_duration(et, body.duration)
    except ValueError as exc:
        raise APIError(VALIDATION_ERROR, str(exc), 422)
    if body.slot_token and not av.verify_slot_token(body.slot_token, et.id, start, dur):
        raise APIError("SLOT_TOKEN_INVALID", "This time selection has expired. Please choose a time again.", 409)
    now = tu.utcnow()
    invitee = dict(body.invitee)
    email = (invitee.get("email") or "").strip().lower()
    if et.requires_email_verification:
        verify_code(db, email, body.verification_code, now)
    invitee.setdefault("timezone", body.timezone)
    if body.consent is not None:
        invitee["consent"] = body.consent
    if body.location is not None:
        invitee["location"] = body.location
    if isinstance(body.answers, dict):
        answers = body.answers
    else:
        answers = {a.get("question_id"): a.get("value") for a in body.answers if isinstance(a, dict) and a.get("question_id")}
    utm = {k: str(v)[:200] for k, v in (body.utm or {}).items() if k in UTM_KEYS and v}
    source = body.source if body.source in ("booking_page", "embed", "routing", "api") else "booking_page"
    rsid = body.routing_submission_id or body.rsid
    if rsid:
        source = "routing" if source == "booking_page" else source
    booking = bk.create_booking(
        db, et, start, invitee, answers, timezone=body.timezone, duration=dur, idempotency_key=key, utm=utm or None,
        source=source, routing_submission_id=rsid, actor="invitee", now=now,
    )
    return JSONResponse(booking_response(booking), status_code=201)


@router.get("/bookings/{manage_token}")
def get_public_booking(manage_token: str, request: Request, db: Session = Depends(get_db)):
    enforce(request, "public_read", 120, 60)
    b = booking_by_token(db, manage_token)
    return bk.booking_to_dict(b)


class CancelIn(BaseModel):
    reason: Optional[str] = None


@router.post("/bookings/{manage_token}/cancel")
def cancel_public_booking(manage_token: str, request: Request, body: CancelIn = CancelIn(), db: Session = Depends(get_db)):
    enforce(request, "public_manage", 20, 60)
    b = booking_by_token(db, manage_token)
    b = bk.cancel_booking(db, b, "invitee", (body.reason or "")[:2000] or None)
    return {"booking_id": b.id, "status": b.status, "cancellation_requested": bool(b.cancellation_requested_at)}


class RescheduleIn(BaseModel):
    start_at: str
    timezone: Optional[str] = None
    slot_token: Optional[str] = None
    reason: Optional[str] = None


@router.post("/bookings/{manage_token}/reschedule")
def reschedule_public_booking(manage_token: str, body: RescheduleIn, request: Request, db: Session = Depends(get_db)):
    enforce(request, "public_manage", 20, 60)
    b = booking_by_token(db, manage_token)
    try:
        start = tu.parse_iso_utc(body.start_at)
    except ValueError:
        raise APIError(VALIDATION_ERROR, "start_at must be an ISO-8601 timestamp", 422)
    dur = int((b.end_at - b.start_at).total_seconds() // 60)
    if body.slot_token and not av.verify_slot_token(body.slot_token, b.event_type_id, start, dur):
        raise APIError("SLOT_TOKEN_INVALID", "This time selection has expired. Please choose a time again.", 409)
    new = bk.reschedule_booking(db, b, start, "invitee", (body.reason or "")[:2000] or None)
    if body.timezone and tu.is_valid_tz(body.timezone):
        new.timezone = body.timezone
        db.commit()
    return booking_response(new)


class VerifyIn(BaseModel):
    email: str
    event_type_id: Optional[str] = None


@router.post("/verify-email")
def send_verification(body: VerifyIn, request: Request, db: Session = Depends(get_db)):
    email = (body.email or "").strip().lower()
    if not bk.EMAIL_RE.match(email):
        raise APIError(VALIDATION_ERROR, "Enter a valid email address", 422)
    enforce(request, "public_verify", 5, 600, key=f"{client_ip(request)}:{email}")
    et = db.get(EventType, body.event_type_id) if body.event_type_id else None
    issue_verification_code(db, email, et.name if et else None)
    return {"sent": True, "expires_in": 900}


def issue_verification_code(db: Session, email: str, event_name: Optional[str] = None) -> None:
    """Create + email a 6-digit invitee verification code (15 min). Commits. Caller rate-limits."""
    code = f"{secrets.randbelow(1_000_000):06d}"
    now = tu.utcnow()
    db.add(VerificationCode(email=email, purpose=VERIFY_PURPOSE, code_hash=hash_secret(f"{email}:{code}"),
                            expires_at=now + dt.timedelta(minutes=15)))
    name = event_name
    send_email(db, email, "Your verification code",
               f"<p>Your verification code{(' for ' + name) if name else ''} is:</p>"
               f"<p style='font-size:24px;font-weight:700;letter-spacing:4px'>{code}</p>"
               "<p>It expires in 15 minutes. If you didn't request it, you can ignore this email.</p>")
    db.commit()


class WaitlistIn(BaseModel):
    owner_slug: Optional[str] = None
    event_type_slug: Optional[str] = None
    event_type_id: Optional[str] = None
    start_at: str
    name: str
    email: str


@router.post("/waitlist", status_code=201)
def join_waitlist(body: WaitlistIn, request: Request, db: Session = Depends(get_db)):
    enforce(request, "public_waitlist", 10, 60)
    if body.event_type_id:
        et = db.get(EventType, body.event_type_id)
        if not et or et.deleted_at:
            raise APIError(NOT_FOUND, "Event type not found", 404)
    else:
        et, _, _ = find_event(db, body.owner_slug or "", body.event_type_slug or "")
    require_bookable(et)
    if et.kind != "group" or not et.waitlist_enabled:
        raise APIError(VALIDATION_ERROR, "This event does not have a waitlist.", 422)
    email = body.email.strip().lower()
    if not bk.EMAIL_RE.match(email) or not body.name.strip():
        raise APIError(VALIDATION_ERROR, "Enter your name and a valid email address", 422)
    try:
        start = tu.parse_iso_utc(body.start_at)
    except ValueError:
        raise APIError(VALIDATION_ERROR, "start_at must be an ISO-8601 timestamp", 422)
    dup = db.scalar(select(WaitlistEntry).where(WaitlistEntry.event_type_id == et.id, WaitlistEntry.start_at == start,
                                                WaitlistEntry.email == email))
    if not dup:
        db.add(WaitlistEntry(workspace_id=et.workspace_id, event_type_id=et.id, start_at=start, name=body.name.strip()[:200],
                             email=email))
        db.commit()
    return {"ok": True, "message": "You're on the waitlist. We'll email you if a spot opens."}
