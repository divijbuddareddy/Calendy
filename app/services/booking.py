"""Booking integrity (PRD 7.3, 7.4, 8.3, 10.2-10.4, 20.3).

Transactions: unlike most services, the functions here COMMIT (they implement multi-step sagas
with locking and external provider calls). Callers may commit again harmlessly.

    create_booking(db, event_type, start_utc, invitee, answers, *, timezone, duration=None,
                   idempotency_key=None, utm=None, source="booking_page", routing_submission_id=None,
                   actor="invitee", now=None) -> Booking
    finalize_booking(db, booking, now=None) -> Booking
    fail_pending_booking(db, booking, reason) -> Booking
    cancel_booking(db, booking, actor, reason=None, now=None) -> Booking
    approve_cancellation(db, booking, actor, now=None) / decline_cancellation(db, booking, actor, reason=None)
    reschedule_booking(db, booking, new_start_utc, actor, reason=None, now=None) -> Booking (the NEW booking)
    mark_no_show(db, booking, actor, value=True) -> Booking
    reassign_booking(db, booking, new_host_id, actor, reason) -> Booking
    expire_holds(db, now=None) -> int                  (@periodic "expire_holds")
    retry_pending_calendar(db, now=None) -> int        (@periodic "retry_pending_calendar")
    booking_to_dict(booking) -> dict ; manage_urls(booking) -> dict

Errors are APIError (PRD 22.4 envelope): SLOT_NO_LONGER_AVAILABLE (409), VALIDATION_ERROR (422),
IDEMPOTENCY_CONFLICT (409), CANCELLATION_NOT_ALLOWED (403), POLICY_WINDOW_PASSED (409),
INVALID_STATE (409), CALENDAR_UNAVAILABLE (503), RESCHEDULE_FAILED (502).
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, object_session

from app.audit import audit
from app.config import settings
from app.db import lock_hosts
from app.errors import (
    CALENDAR_UNAVAILABLE,
    IDEMPOTENCY_CONFLICT,
    SLOT_NO_LONGER_AVAILABLE,
    VALIDATION_ERROR,
    APIError,
)
from app.events import publish
from app.models import (
    ACTIVE_BOOKING_STATUSES,
    Booking,
    BookingAnswer,
    BookingEvent,
    BookingHold,
    BookingHost,
    CalendarEvent,
    EventType,
    EventTypeHost,
    Invitee,
    RoutingSubmission,
    User,
    WaitlistEntry,
)
from app.security import opaque_token
from app.services import availability as av
from app.services import calendar_facade as cf
from app.services import timeutil as tu
from app.worker import periodic

log = logging.getLogger("booking")

SLOT_MSG = "This time was just booked. Please choose another time."
PAYMENT_HOLD_MINUTES = 15
MAX_CALENDAR_RETRIES = 6
CONFERENCE_TYPES = {"google_meet", "zoom", "teams"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+?[0-9 ().\-]{6,25}$")
URL_RE = re.compile(r"^https?://[^\s/$.?#].[^\s]*$", re.I)


def _begin_write(db: Session, booking: Optional[Booking] = None, extra_ids=()) -> None:
    """End any read snapshot and start a write transaction by locking the booking's hosts first
    (avoids SQLite read->write upgrade failures and serialises with concurrent bookers)."""
    db.commit()
    ids = set(extra_ids)
    if booking is not None:
        ids |= {h.user_id for h in booking.hosts} | {booking.host_id}
    lock_hosts(db, ids or {"__system__"})
    if booking is not None:
        db.refresh(booking)


def _slot_error() -> APIError:
    return APIError(SLOT_NO_LONGER_AVAILABLE, SLOT_MSG, 409)


STALE_MSG = "The host's availability changed after this page was loaded, so this time is no longer offered. Please choose another time."
FULL_MSG = "This session just filled up. Please choose another time."


def _stale_slot_error(db: Session, et: EventType, start_utc: dt.datetime, dur: int, host_ids, now) -> APIError:
    """Explain why a slot that passed the (browser) slot list failed revalidation inside the lock:
    'capacity' (group full), 'booked' (another booking/hold overlaps) or 'availability_changed'
    (host edited hours/overrides, calendar became busy, limits reached, ...). Same error code,
    so clients keep one code path, plus details.reason for a precise message."""
    end = start_utc + dt.timedelta(minutes=dur)
    if et.kind == "group" and av.group_seats_remaining(db, et, start_utc, now=now) <= 0:
        return APIError(SLOT_NO_LONGER_AVAILABLE, FULL_MSG, 409, {"reason": "capacity"})
    for hid in host_ids:
        for b in av._host_bookings(db, hid, start_utc - dt.timedelta(days=1), end + dt.timedelta(days=1), None, now):
            if b.start_at < end and b.end_at > start_utc and not (et.kind == "group" and b.event_type_id == et.id):
                return APIError(SLOT_NO_LONGER_AVAILABLE, SLOT_MSG, 409, {"reason": "booked"})
        held = db.scalar(select(BookingHold).where(
            BookingHold.host_id == hid, BookingHold.released.is_(False), BookingHold.expires_at > now,
            BookingHold.start_at < end, BookingHold.end_at > start_utc))
        if held:
            return APIError(SLOT_NO_LONGER_AVAILABLE, SLOT_MSG, 409, {"reason": "booked"})
    return APIError(SLOT_NO_LONGER_AVAILABLE, STALE_MSG, 409, {"reason": "availability_changed"})


def _expire_stale_overlapping(db: Session, host_ids, start: dt.datetime, end: dt.datetime, now: dt.datetime) -> None:
    """Inside the host lock: mark abandoned holds/unpaid bookings that overlap the requested
    interval as expired, so the DB backstops (unique index / PG exclusion constraint) never reject
    a slot that availability correctly reports as free just because the sweeper hasn't run yet."""
    if not host_ids:
        return
    for b in db.scalars(
        select(Booking).where(Booking.host_id.in_(list(host_ids)), Booking.status.in_(("held", "pending_payment")),
                              Booking.hold_expires_at.is_not(None), Booking.hold_expires_at < now,
                              Booking.start_at < end, Booking.end_at > start)
    ).all():
        b.status = "expired"
        if b.payment_status == "pending":
            b.payment_status = "failed"
        _release_holds(db, b)
        _history(db, b, "expired", "system")
    db.flush()


def _now(now):
    return now or tu.utcnow()


def _history(db: Session, booking: Booking, event: str, actor: str = "system", **payload):
    db.add(BookingEvent(booking_id=booking.id, actor=str(actor)[:60], event_type=event, payload_json=payload or None))


def _actor_role(actor: str) -> str:
    a = (actor or "system").lower()
    if a in ("host", "invitee", "admin", "provider", "system"):
        return a
    if a.startswith("admin"):
        return "admin"
    if a.startswith("user:") or a.startswith("api"):
        return "host"
    return a[:20]


def _policy(booking: Booking, et: Optional[EventType]) -> dict:
    snap = (booking.snapshot_json or {}).get("cancellation_policy")
    if snap is not None:
        return dict(snap)
    return dict((et.cancellation_policy_json if et else None) or {})


# ------------------------------------------------------------------ validation
def _norm_options(opts) -> list[str]:
    out = []
    for o in opts or []:
        if isinstance(o, dict):
            out.append(str(o.get("value") or o.get("label") or ""))
        else:
            out.append(str(o))
    return [o for o in out if o]


def _empty(v) -> bool:
    return v is None or v is False or (isinstance(v, (str, list, dict)) and len(v) == 0) or (isinstance(v, str) and not v.strip())


def validate_answers(questions, answers: dict) -> dict:
    """Validate answers {question_id: value} against questions; returns cleaned dict. Raises APIError 422."""
    answers = answers or {}
    errors: dict = {}
    clean: dict = {}
    for q in sorted(questions or [], key=lambda q: q.sort_order or 0):
        raw = answers.get(q.id)
        if isinstance(raw, str):
            raw = raw.strip()
        if _empty(raw):
            if q.required:
                errors[q.id] = f"{q.label} is required" if q.type != "consent" else f"Please accept: {q.label}"
            continue
        opts = _norm_options(q.options_json)
        t = q.type
        val = raw
        if t == "email":
            if not EMAIL_RE.match(str(raw)):
                errors[q.id] = f"{q.label}: enter a valid email address"
        elif t == "url":
            if not URL_RE.match(str(raw)):
                errors[q.id] = f"{q.label}: enter a valid URL starting with http:// or https://"
        elif t == "phone":
            if not PHONE_RE.match(str(raw)):
                errors[q.id] = f"{q.label}: enter a valid phone number"
        elif t == "number":
            try:
                val = float(raw)
                val = int(val) if val.is_integer() else val
            except (TypeError, ValueError):
                errors[q.id] = f"{q.label}: enter a number"
        elif t in ("select", "radio"):
            if opts and str(raw) not in opts:
                errors[q.id] = f"{q.label}: choose one of the listed options"
            val = str(raw)
        elif t == "checkbox":
            if opts:
                vals = raw if isinstance(raw, list) else [raw]
                vals = [str(v) for v in vals if not _empty(v)]
                if any(v not in opts for v in vals):
                    errors[q.id] = f"{q.label}: invalid option"
                if q.required and not vals:
                    errors[q.id] = f"{q.label} is required"
                val = vals
            else:
                val = raw in (True, "true", "on", "1", 1, "yes")
                if q.required and not val:
                    errors[q.id] = f"{q.label} is required"
        elif t == "consent":
            val = raw in (True, "true", "on", "1", 1, "yes")
            if q.required and not val:
                errors[q.id] = f"Please accept: {q.label}"
        else:
            val = str(raw)
            if len(val) > 5000:
                errors[q.id] = f"{q.label}: too long"
        clean[q.id] = val
    if errors:
        first = next(iter(errors.values()))
        raise APIError(VALIDATION_ERROR, first, 422, {"fields": errors})
    return clean


def validate_invitee(invitee: dict) -> dict:
    inv = dict(invitee or {})
    errors = {}
    name = (inv.get("name") or "").strip()
    email = (inv.get("email") or "").strip().lower()
    if not name:
        errors["name"] = "Name is required"
    elif len(name) > 200:
        errors["name"] = "Name is too long"
    if not EMAIL_RE.match(email) or len(email) > 320:
        errors["email"] = "Enter a valid email address"
    phone = (inv.get("phone") or "").strip() or None
    if phone and not PHONE_RE.match(phone):
        errors["phone"] = "Enter a valid phone number"
    guests = inv.get("guests") or []
    if isinstance(guests, str):
        guests = [g for g in re.split(r"[,;\s]+", guests) if g]
    guests = [str(g).strip().lower() for g in guests if str(g).strip()]
    if len(guests) > 10:
        errors["guests"] = "At most 10 guests"
    elif any(not EMAIL_RE.match(g) for g in guests):
        errors["guests"] = "Guest emails must be valid email addresses"
    if errors:
        raise APIError(VALIDATION_ERROR, next(iter(errors.values())), 422, {"fields": errors})
    return {
        "name": name,
        "email": email,
        "phone": phone,
        "company": ((inv.get("company") or "").strip() or None),
        "timezone": tu.validate_tz(inv.get("timezone"), None) if inv.get("timezone") else None,
        "guests": list(dict.fromkeys(guests)),
        "consent": inv.get("consent"),
        "location": inv.get("location"),
    }


# ------------------------------------------------------------------ snapshot / location
def _choose_location(et: EventType, choice, invitee: dict) -> dict:
    options = [o for o in (et.location_json or []) if isinstance(o, dict) and o.get("type")]
    if not options:
        return {"type": "host_provides", "value": None}
    opt = options[0]
    if choice is not None:
        for i, o in enumerate(options):
            if str(choice) in (str(i), o.get("type")):
                opt = o
                break
    loc = {"type": opt.get("type"), "value": opt.get("value")}
    if opt.get("type") == "invitee_phone":
        loc["value"] = invitee.get("phone")
    if opt.get("label"):
        loc["label"] = opt.get("label")
    return loc


def build_snapshot(db: Session, et: EventType, duration: int) -> dict:
    owner = db.get(User, et.owner_user_id)
    return {
        "name": et.name,
        "slug": et.slug,
        "description": et.description,
        "kind": et.kind,
        "duration": duration,
        "locations": et.location_json or [],
        "cancellation_policy": et.cancellation_policy_json or {},
        "confirmation": et.confirmation_json or {},
        "questions": [
            {"id": q.id, "label": q.label, "type": q.type, "required": q.required} for q in et.questions
        ],
        "price_minor": et.price_minor,
        "currency": et.currency,
        "buffer_before": et.buffer_before or 0,
        "buffer_after": et.buffer_after or 0,
        "calendar_event_title": et.calendar_event_title,
        "owner_name": owner.name if owner else None,
        "event_version": et.version,
        "_ics": {"uid": None, "seq": 0},
    }


# ------------------------------------------------------------------ round robin
RR_DEFAULT_WINDOW_DAYS = 30
RR_UNCOUNTED_STATUSES = ("cancelled", "failed", "expired", "rescheduled")


def rr_balancing_window_days(et: EventType) -> int:
    """Equal-distribution balancing window in days. Configured per event type through
    confirmation_json["rr_balancing_window_days"] (1-365); default 30."""
    raw = (et.confirmation_json or {}).get("rr_balancing_window_days")
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return RR_DEFAULT_WINDOW_DAYS
    return max(1, min(365, v))


def rr_window_counts(db: Session, et: EventType, host_ids: list[str], now: Optional[dt.datetime] = None) -> dict:
    """Assignments per host for this event type made within the balancing window
    (cancelled / failed / expired / rescheduled-away bookings don't count)."""
    now = _now(now)
    since = now - dt.timedelta(days=rr_balancing_window_days(et))
    rows = db.execute(
        select(Booking.host_id).where(
            Booking.event_type_id == et.id,
            Booking.host_id.in_(list(host_ids) or [""]),
            Booking.created_at >= since,
            Booking.status.notin_(RR_UNCOUNTED_STATUSES),
        )
    ).all()
    counts = {h: 0 for h in host_ids}
    for (hid,) in rows:
        counts[hid] = counts.get(hid, 0) + 1
    return counts


def choose_round_robin_host(db: Session, et: EventType, eligible: list[str],
                            now: Optional[dt.datetime] = None) -> tuple[str, str]:
    """PRD 10.3. 'equal': lowest assignment count over the balancing window, then priority, then
    least recently assigned. 'availability' (maximise availability): priority, then least recently
    assigned."""
    rows = {h.user_id: h for h in et.hosts if h.user_id in eligible}
    cands = [rows[u] for u in eligible if u in rows] or []
    if not cands:
        return eligible[0], "round_robin: only eligible host"
    epoch = dt.datetime(1970, 1, 1)
    if et.rr_strategy == "equal":
        days = rr_balancing_window_days(et)
        counts = rr_window_counts(db, et, [h.user_id for h in cands], now)
        cands.sort(key=lambda h: (counts.get(h.user_id, 0), -(h.priority or 0), h.last_assigned_at or epoch, h.user_id))
        h = cands[0]
        reason = (f"round_robin(equal distribution): lowest assignment count in the last {days} days "
                  f"({counts.get(h.user_id, 0)}) among {len(cands)} eligible host(s)")
    else:
        cands.sort(key=lambda h: (-(h.priority or 0), h.last_assigned_at or epoch, h.assignment_count or 0, h.user_id))
        h = cands[0]
        reason = (f"round_robin(maximise availability): priority {h.priority}, least recently assigned "
                  f"among {len(cands)} eligible host(s)")
    return h.user_id, reason


# ------------------------------------------------------------------ create
def create_booking(
    db: Session,
    event_type: EventType,
    start_utc: dt.datetime,
    invitee: dict,
    answers: dict,
    *,
    timezone: str,
    duration: Optional[int] = None,
    idempotency_key: Optional[str] = None,
    utm: Optional[dict] = None,
    source: str = "booking_page",
    routing_submission_id: Optional[str] = None,
    actor: str = "invitee",
    now: Optional[dt.datetime] = None,
) -> Booking:
    now = _now(now)
    et = event_type
    start_utc = tu.to_naive_utc(start_utc)
    if idempotency_key:
        existing = db.scalar(select(Booking).where(Booking.idempotency_key == idempotency_key))
        if existing:
            return _idempotent_match(existing, et, start_utc)
    if et.deleted_at or et.status != "active" or et.is_managed_template:
        raise APIError("EVENT_UNAVAILABLE", "This event is not accepting bookings right now.", 409)
    if et.single_use and et.used_at:
        raise APIError("EVENT_UNAVAILABLE", "This single-use link has already been used.", 410)
    try:
        dur = av.resolve_duration(et, duration)
    except ValueError as exc:
        raise APIError(VALIDATION_ERROR, str(exc), 422)
    if not tu.is_valid_tz(timezone):
        raise APIError(VALIDATION_ERROR, "Unknown timezone", 422, {"fields": {"timezone": "Unknown timezone"}})
    inv = validate_invitee(invitee)
    clean_answers = validate_answers(et.questions, answers)
    candidate_ids = av.event_host_ids(db, et)
    optional_ids = av.optional_host_ids(db, et)
    questions = {q.id: q for q in et.questions}
    paid = bool(et.price_minor and et.price_minor > 0)
    snapshot = build_snapshot(db, et, dur)
    location = _choose_location(et, inv.get("location"), inv)

    db.commit()  # end any open read transaction before taking the host locks
    try:
        lock_hosts(db, candidate_ids)  # FIRST statement of the write transaction
        if idempotency_key:
            existing = db.scalar(select(Booking).where(Booking.idempotency_key == idempotency_key))
            if existing:
                db.rollback()
                return _idempotent_match(existing, et, start_utc)
        db.refresh(et)
        _expire_stale_overlapping(db, candidate_ids, start_utc, start_utc + dt.timedelta(minutes=dur), now)
        if et.status != "active" or (et.single_use and et.used_at):
            raise APIError("EVENT_UNAVAILABLE", "This event is not accepting bookings right now.", 409)
        try:
            eligible = av.check_slot(db, et, start_utc, dur, now=now)  # revalidate inside the lock
        except cf.CalendarUnavailable:
            raise APIError(CALENDAR_UNAVAILABLE, "The host's calendar can't be checked right now. Please try again later.", 503)
        if not eligible:
            raise _stale_slot_error(db, et, start_utc, dur, candidate_ids, now)
        assignment_reason = None
        if et.kind == "round_robin":
            host_id, assignment_reason = choose_round_robin_host(db, et, eligible, now)
            eth = next((h for h in et.hosts if h.user_id == host_id), None)
            if eth:
                eth.assignment_count = (eth.assignment_count or 0) + 1
                eth.last_assigned_at = now
            participants = [(host_id, True)]
        elif et.kind == "collective":
            host_id = et.owner_user_id if et.owner_user_id in eligible else eligible[0]
            participants = [(h, True) for h in eligible]
            end = start_utc + dt.timedelta(minutes=dur)
            for oid in optional_ids:
                if oid not in eligible and av.host_is_free(db, et, oid, start_utc, end, now=now):
                    participants.append((oid, False))
        else:
            host_id = eligible[0]
            participants = [(host_id, True)]

        booking = Booking(
            workspace_id=et.workspace_id,
            event_type_id=et.id,
            host_id=host_id,
            start_at=start_utc,
            end_at=start_utc + dt.timedelta(minutes=dur),
            timezone=timezone,
            status="pending_payment" if paid else "held",
            exclusive=et.kind != "group",
            hold_expires_at=now + dt.timedelta(minutes=PAYMENT_HOLD_MINUTES if paid else settings.hold_minutes),
            idempotency_key=idempotency_key,
            manage_token=opaque_token(24),
            snapshot_json=snapshot,
            location_json=location,
            utm_json={k: v for k, v in (utm or {}).items() if v} or None,
            source=source,
            routing_submission_id=routing_submission_id,
            assignment_reason=assignment_reason,
            payment_status="pending" if paid else None,
        )
        booking.manage_token_expires_at = booking.end_at + dt.timedelta(days=30)
        db.add(booking)
        db.flush()
        snap = dict(snapshot)
        snap["_ics"] = {"uid": f"{booking.id}@aal-scheduler", "seq": 0}
        booking.snapshot_json = snap
        for uid, req in participants:
            booking.hosts.append(BookingHost(user_id=uid, required=req))
        booking.invitees.append(
            Invitee(
                name=inv["name"],
                email=inv["email"],
                phone=inv["phone"],
                company=inv["company"],
                timezone=inv["timezone"] or timezone,
                consent_json=inv["consent"] if isinstance(inv["consent"], dict) else ({"accepted": True} if inv["consent"] else None),
                guests_json=inv["guests"] or None,
            )
        )
        for qid, val in clean_answers.items():
            booking.answers.append(BookingAnswer(question_id=qid, label=questions[qid].label, answer_json=val))
        if et.single_use:
            et.used_at = now
        if routing_submission_id:
            sub = db.get(RoutingSubmission, routing_submission_id)
            if sub and sub.workspace_id == et.workspace_id:
                sub.booking_id = booking.id
        _history(db, booking, "created", actor, status=booking.status, assignment_reason=assignment_reason, source=source)
        db.flush()
        db.commit()
    except IntegrityError:
        db.rollback()
        if idempotency_key:
            existing = db.scalar(select(Booking).where(Booking.idempotency_key == idempotency_key))
            if existing:
                return _idempotent_match(existing, et, start_utc)
        raise _slot_error()
    except Exception:
        db.rollback()
        raise
    if paid:
        return booking  # payments module redirects to /pay/{manage_token} and later calls finalize_booking
    return finalize_booking(db, booking, now=now)


def _idempotent_match(existing: Booking, et: EventType, start_utc: dt.datetime) -> Booking:
    if existing.event_type_id != et.id or existing.start_at != start_utc:
        raise APIError(IDEMPOTENCY_CONFLICT, "Idempotency-Key was already used for a different booking request.", 409)
    return existing


# ------------------------------------------------------------------ finalize / pending
def _sync_meta(booking: Booking) -> dict:
    return dict((booking.snapshot_json or {}).get("_sync") or {})


def _set_sync_meta(booking: Booking, meta: dict):
    snap = dict(booking.snapshot_json or {})
    snap["_sync"] = meta
    booking.snapshot_json = snap


def finalize_booking(db: Session, booking: Booking, now: Optional[dt.datetime] = None) -> Booking:
    """Create the external calendar event(s) and confirm. Commits.
    Transient provider error -> status 'pending' (slot stays reserved; retried by worker with backoff).
    Permanent provider error -> confirmed with location fallback + 'host_action_required' history entry."""
    now = _now(now)
    if booking.status == "confirmed":
        return booking
    if booking.status in ("cancelled", "rescheduled"):
        raise APIError("INVALID_STATE", f"Booking is {booking.status}.", 409)
    if booking.status in ("expired", "failed"):
        _rereserve(db, booking, now)
    _begin_write(db, booking)
    if booking.status == "confirmed":
        db.commit()
        return booking
    sp = db.begin_nested()
    try:
        cf.create_calendar_events(db, booking)
        sp.commit()
    except cf.TransientProviderError as exc:
        sp.rollback()
        meta = _sync_meta(booking)
        attempts = int(meta.get("attempts", 0)) + 1
        meta.update(attempts=attempts, error=str(exc)[:500],
                    next_at=tu.iso_z(now + dt.timedelta(minutes=min(60, 2 ** (attempts - 1)))))
        _set_sync_meta(booking, meta)
        first = booking.status != "pending"
        booking.status = "pending"
        booking.hold_expires_at = None
        _history(db, booking, "calendar_pending", "system", error=str(exc)[:500], attempts=attempts)
        if first:
            publish(db, "calendar.sync_failed", {"booking_id": booking.id, "transient": True, "error": str(exc)[:300]},
                    workspace_id=booking.workspace_id)
        db.commit()
        return booking
    except cf.PermanentProviderError as exc:
        sp.rollback()
        loc = dict(booking.location_json or {})
        if loc.get("type") in CONFERENCE_TYPES:
            loc["fallback_from"] = loc.get("type")
            loc["type"] = "host_provides"
            loc["note"] = "The host will share meeting details."
        loc["host_action_required"] = True
        booking.location_json = loc
        _history(db, booking, "host_action_required", "system", error=str(exc)[:500])
        publish(db, "calendar.sync_failed", {"booking_id": booking.id, "transient": False, "error": str(exc)[:300]},
                workspace_id=booking.workspace_id)
    booking.status = "confirmed"
    booking.hold_expires_at = None
    meta = _sync_meta(booking)
    if meta:
        meta["next_at"] = None
        _set_sync_meta(booking, meta)
    _history(db, booking, "confirmed", "system")
    publish(db, "booking.created", {"booking_id": booking.id, "actor": "invitee"}, workspace_id=booking.workspace_id)
    db.commit()
    return booking


def _rereserve(db: Session, booking: Booking, now: dt.datetime):
    et = db.get(EventType, booking.event_type_id)
    host_ids = [h.user_id for h in booking.hosts] or [booking.host_id]
    db.commit()
    try:
        lock_hosts(db, host_ids)
        dur = int((booking.end_at - booking.start_at).total_seconds() // 60)
        eligible = av.check_slot(db, et, booking.start_at, dur, exclude_booking_id=booking.id, now=now - dt.timedelta(days=1),
                                 for_reschedule=True, host_ids=host_ids)
        if booking.host_id not in eligible and not (et.kind == "collective" and eligible):
            raise _slot_error()
        booking.status = "held"
        _history(db, booking, "re_reserved", "system")
        db.flush()
        db.commit()
    except IntegrityError:
        db.rollback()
        raise _slot_error()
    except cf.CalendarUnavailable:
        db.rollback()
        raise APIError(CALENDAR_UNAVAILABLE, "The host's calendar can't be checked right now.", 503)
    except Exception:
        db.rollback()
        raise


def fail_pending_booking(db: Session, booking: Booking, reason: str = "") -> Booking:
    """Release the reservation of a held/pending/pending_payment booking (no booking.created). Commits."""
    _begin_write(db, booking)
    if booking.status in ACTIVE_BOOKING_STATUSES and booking.status != "confirmed":
        booking.status = "failed"
        booking.hold_expires_at = None
        _release_holds(db, booking)
        _history(db, booking, "failed", "system", reason=reason)
    db.commit()
    return booking


def _release_holds(db: Session, booking: Booking):
    for h in db.scalars(select(BookingHold).where(BookingHold.booking_id == booking.id, BookingHold.released.is_(False))).all():
        h.released = True


@periodic("retry_pending_calendar")
def retry_pending_calendar(db: Session, now: Optional[dt.datetime] = None) -> int:
    now = _now(now)
    n = 0
    for b in db.scalars(select(Booking).where(Booking.status == "pending")).all():
        meta = _sync_meta(b)
        nxt = meta.get("next_at")
        if nxt and tu.parse_iso_utc(nxt) > now:
            continue
        if int(meta.get("attempts", 0)) >= MAX_CALENDAR_RETRIES:
            fail_pending_booking(db, b, "calendar provider unavailable after retries")
            publish(db, "calendar.sync_failed", {"booking_id": b.id, "transient": False, "error": "retries exhausted"},
                    workspace_id=b.workspace_id)
            db.commit()
            continue
        try:
            finalize_booking(db, b, now=now)
            n += 1
        except Exception:  # keep going for other bookings
            db.rollback()
            log.exception("retry of pending booking %s failed", b.id)
    return n


@periodic("expire_holds")
def expire_holds(db: Session, now: Optional[dt.datetime] = None) -> int:
    """Expire abandoned holds / unpaid bookings past hold_expires_at."""
    now = _now(now)
    n = 0
    _begin_write(db)
    for b in db.scalars(
        select(Booking).where(Booking.status.in_(("held", "pending_payment")), Booking.hold_expires_at.is_not(None),
                              Booking.hold_expires_at < now)
    ).all():
        b.status = "expired"
        if b.payment_status == "pending":
            b.payment_status = "failed"
        _release_holds(db, b)
        _history(db, b, "expired", "system")
        n += 1
    for h in db.scalars(select(BookingHold).where(BookingHold.released.is_(False), BookingHold.expires_at < now)).all():
        h.released = True
    db.commit()
    return n


# ------------------------------------------------------------------ cancel
def cancel_booking(db: Session, booking: Booking, actor: str, reason: Optional[str] = None,
                   now: Optional[dt.datetime] = None, *, enforce_policy: Optional[bool] = None) -> Booking:
    """Cancel (or, with host approval policy for invitees, request cancellation). Commits."""
    try:
        return _cancel_impl(db, booking, actor, reason, now, enforce_policy)
    except Exception:
        db.rollback()
        raise


def _cancel_impl(db: Session, booking: Booking, actor: str, reason, now, enforce_policy) -> Booking:
    now = _now(now)
    reason = (reason or "").strip() or None
    _begin_write(db, booking)
    if booking.status == "cancelled":
        db.commit()
        return booking
    if booking.status not in ACTIVE_BOOKING_STATUSES:
        db.rollback()
        raise APIError("INVALID_STATE", f"This booking is {booking.status} and can't be cancelled.", 409)
    et = db.get(EventType, booking.event_type_id)
    role = _actor_role(actor)
    if enforce_policy is None:
        enforce_policy = role == "invitee"
    if enforce_policy:
        pol = _policy(booking, et)
        if pol.get("allow_cancel") is False:
            raise APIError("CANCELLATION_NOT_ALLOWED", "This booking can't be cancelled online. Please contact the host.", 403)
        mn = int(pol.get("min_notice_minutes") or 0)
        if mn and booking.status == "confirmed" and now > booking.start_at - dt.timedelta(minutes=mn):
            raise APIError("POLICY_WINDOW_PASSED",
                           f"Cancellations must be made at least {_fmt_minutes(mn)} before the meeting. Please contact the host.", 409)
        if pol.get("require_reason") and not reason:
            raise APIError(VALIDATION_ERROR, "Please give a reason for cancelling.", 422, {"fields": {"reason": "required"}})
        if pol.get("host_approval") and booking.status == "confirmed":
            if not booking.cancellation_requested_at:
                booking.cancellation_requested_at = now
                booking.cancel_reason = reason
                _history(db, booking, "cancellation_requested", actor, reason=reason)
                publish(db, "booking.cancellation_requested", {"booking_id": booking.id, "actor": actor, "reason": reason},
                        workspace_id=booking.workspace_id)
                db.commit()
            return booking
    _do_cancel(db, booking, et, actor, role, reason, now)
    db.commit()
    return booking


def _fmt_minutes(m: int) -> str:
    if m % 1440 == 0:
        return f"{m // 1440} day(s)"
    if m % 60 == 0:
        return f"{m // 60} hour(s)"
    return f"{m} minutes"


def _do_cancel(db: Session, booking: Booking, et: Optional[EventType], actor: str, role: str, reason, now):
    was_confirmed = booking.status in ("confirmed", "pending")
    booking.status = "cancelled"
    booking.cancelled_by = role
    booking.cancelled_at = now
    booking.cancel_reason = reason or booking.cancel_reason
    booking.hold_expires_at = None
    booking.version = (booking.version or 1) + 1
    _release_holds(db, booking)
    sp = db.begin_nested()
    try:
        cf.delete_calendar_events(db, booking)
        sp.commit()
    except Exception as exc:  # never block a cancellation on the provider; reconcile later
        sp.rollback()
        _history(db, booking, "calendar_delete_failed", "system", error=str(exc)[:500])
    _history(db, booking, "cancelled", actor, reason=reason)
    if was_confirmed or booking.payment_status == "paid":
        publish(db, "booking.cancelled", {"booking_id": booking.id, "actor": role, "reason": reason},
                workspace_id=booking.workspace_id)
    if et is not None and et.kind == "group":
        waiting = db.scalars(
            select(WaitlistEntry).where(WaitlistEntry.event_type_id == et.id, WaitlistEntry.start_at == booking.start_at,
                                        WaitlistEntry.notified_at.is_(None))
        ).all()
        if waiting:
            publish(db, "waitlist.spot_open",
                    {"event_type_id": et.id, "start_at": tu.iso_z(booking.start_at), "booking_id": booking.id,
                     "waitlist_entry_ids": [w.id for w in waiting]},
                    workspace_id=booking.workspace_id)


def approve_cancellation(db: Session, booking: Booking, actor: str, now: Optional[dt.datetime] = None) -> Booking:
    now = _now(now)
    _begin_write(db, booking)
    if not booking.cancellation_requested_at:
        db.rollback()
        raise APIError("INVALID_STATE", "No cancellation request is pending for this booking.", 409)
    et = db.get(EventType, booking.event_type_id)
    _history(db, booking, "cancellation_approved", actor)
    _do_cancel(db, booking, et, actor, "invitee", booking.cancel_reason, now)
    db.commit()
    return booking


def decline_cancellation(db: Session, booking: Booking, actor: str, reason: Optional[str] = None) -> Booking:
    _begin_write(db, booking)
    if not booking.cancellation_requested_at:
        db.rollback()
        raise APIError("INVALID_STATE", "No cancellation request is pending for this booking.", 409)
    booking.cancellation_requested_at = None
    _history(db, booking, "cancellation_declined", actor, reason=reason)
    db.commit()
    return booking


# ------------------------------------------------------------------ reschedule saga
def reschedule_booking(db: Session, booking: Booking, new_start_utc: dt.datetime, actor: str,
                       reason: Optional[str] = None, now: Optional[dt.datetime] = None) -> Booking:
    """Atomic replacement (PRD 8.3 / 20.3). Returns the NEW booking. On any failure the original booking
    is untouched and an APIError is raised. Commits."""
    now = _now(now)
    new_start_utc = tu.to_naive_utc(new_start_utc)
    if booking.status != "confirmed":
        raise APIError("INVALID_STATE", f"This booking is {booking.status} and can't be rescheduled.", 409)
    et = db.get(EventType, booking.event_type_id)
    role = _actor_role(actor)
    if role == "invitee":
        pol = _policy(booking, et)
        if pol.get("allow_reschedule") is False:
            raise APIError("RESCHEDULE_NOT_ALLOWED", "This booking can't be rescheduled online. Please contact the host.", 403)
        mn = int(pol.get("min_notice_minutes") or 0)
        if mn and now > booking.start_at - dt.timedelta(minutes=mn):
            raise APIError("POLICY_WINDOW_PASSED",
                           f"Changes must be made at least {_fmt_minutes(mn)} before the meeting. Please contact the host.", 409)
    if new_start_utc == booking.start_at:
        raise APIError(VALIDATION_ERROR, "Please choose a different time.", 422)
    dur = int((booking.end_at - booking.start_at).total_seconds() // 60)
    old_hosts = [(h.user_id, h.required) for h in booking.hosts] or [(booking.host_id, True)]
    lock_ids = sorted({h for h, _ in old_hosts} | set(av.event_host_ids(db, et)))
    old_cal_ids = [c.id for c in db.scalars(select(CalendarEvent).where(CalendarEvent.booking_id == booking.id)).all()]
    invitees = [(i.name, i.email, i.phone, i.company, i.timezone, i.consent_json, i.guests_json) for i in booking.invitees]
    answers = [(a.question_id, a.label, a.answer_json) for a in booking.answers]

    db.commit()
    try:
        lock_hosts(db, lock_ids)
        db.refresh(booking)
        if booking.status != "confirmed":
            raise APIError("INVALID_STATE", f"This booking is {booking.status} and can't be rescheduled.", 409)
        try:
            same = av.check_slot(db, et, new_start_utc, dur, exclude_booking_id=booking.id, now=now,
                                 for_reschedule=True, host_ids=[h for h, req in old_hosts if req])
        except cf.CalendarUnavailable:
            raise APIError(CALENDAR_UNAVAILABLE, "The host's calendar can't be checked right now. Please try again later.", 503)
        participants = old_hosts
        host_id = booking.host_id
        reason_txt = booking.assignment_reason
        if et.kind == "collective":
            if len(same) < len([h for h, req in old_hosts if req]):
                raise _slot_error()
        elif booking.host_id not in same:
            if et.kind != "round_robin":
                raise _slot_error()
            eligible = av.check_slot(db, et, new_start_utc, dur, exclude_booking_id=booking.id, now=now, for_reschedule=True)
            if not eligible:
                raise _slot_error()
            host_id, reason_txt = choose_round_robin_host(db, et, eligible, now)
            participants = [(host_id, True)]
        snap = dict(booking.snapshot_json or {})
        ics = dict(snap.get("_ics") or {"uid": f"{booking.id}@aal-scheduler", "seq": 0})
        ics["seq"] = int(ics.get("seq") or 0) + 1
        snap["_ics"] = ics
        snap.pop("_sync", None)
        new = Booking(
            workspace_id=booking.workspace_id, event_type_id=booking.event_type_id, host_id=host_id,
            start_at=new_start_utc, end_at=new_start_utc + dt.timedelta(minutes=dur), timezone=booking.timezone,
            status="held", exclusive=booking.exclusive, hold_expires_at=now + dt.timedelta(minutes=settings.hold_minutes),
            manage_token=opaque_token(24), snapshot_json=snap, location_json=dict(booking.location_json or {}),
            utm_json=booking.utm_json, source=booking.source, routing_submission_id=booking.routing_submission_id,
            rescheduled_from_id=booking.id, assignment_reason=reason_txt, payment_status=booking.payment_status,
            version=(booking.version or 1) + 1,
        )
        new.manage_token_expires_at = new.end_at + dt.timedelta(days=30)
        if new.location_json.get("type") in CONFERENCE_TYPES and host_id != booking.host_id:
            new.location_json.pop("join_url", None)
        db.add(new)
        db.flush()
        for uid, req in participants:
            new.hosts.append(BookingHost(user_id=uid, required=req))
        for name, email, phone, company, tz, consent, guests in invitees:
            new.invitees.append(Invitee(name=name, email=email, phone=phone, company=company, timezone=tz,
                           consent_json=consent, guests_json=guests))
        for qid, label, val in answers:
            new.answers.append(BookingAnswer(question_id=qid, label=label, answer_json=val))
        db.flush()
        try:
            sp = db.begin_nested()
            cf.update_calendar_events(db, new, previous={
                "booking_id": booking.id, "start_at": booking.start_at, "end_at": booking.end_at,
                "host_id": booking.host_id, "calendar_event_ids": old_cal_ids,
            })
            sp.commit()
        except Exception as exc:
            raise _RescheduleProviderFailure(str(exc)) from exc
        new.status = "confirmed"
        new.hold_expires_at = None
        booking.status = "rescheduled"
        booking.rescheduled_to_id = new.id
        booking.cancel_reason = reason
        booking.cancelled_by = role
        booking.cancelled_at = now
        _history(db, booking, "rescheduled", actor, to_booking_id=new.id, reason=reason)
        _history(db, new, "created", actor, rescheduled_from_id=booking.id, reason=reason)
        _history(db, new, "confirmed", "system")
        publish(db, "booking.rescheduled",
                {"booking_id": new.id, "old_booking_id": booking.id, "actor": role, "reason": reason},
                workspace_id=booking.workspace_id)
        db.commit()
        return new
    except _RescheduleProviderFailure as exc:
        db.rollback()
        db.refresh(booking)
        _history(db, booking, "reschedule_failed", actor, error=str(exc)[:500], attempted_start=tu.iso_z(new_start_utc))
        db.commit()
        raise APIError("RESCHEDULE_FAILED",
                       "We couldn't move this meeting right now. Your original booking is unchanged; please try again.", 502)
    except IntegrityError:
        db.rollback()
        raise _slot_error()
    except Exception:
        db.rollback()
        raise


class _RescheduleProviderFailure(Exception):
    pass


# ------------------------------------------------------------------ no-show / reassignment
def mark_no_show(db: Session, booking: Booking, actor: str, value: bool = True) -> Booking:
    _begin_write(db, booking)
    booking.no_show = bool(value)
    booking.no_show_marked_at = tu.utcnow() if value else None
    _history(db, booking, "no_show" if value else "no_show_cleared", actor)
    if value:
        publish(db, "booking.no_show", {"booking_id": booking.id, "actor": _actor_role(actor)}, workspace_id=booking.workspace_id)
    db.commit()
    return booking


def reassign_booking(db: Session, booking: Booking, new_host_id: str, actor: str, reason: Optional[str] = None,
                     *, force: bool = False) -> Booking:
    """Admin reassignment of a (round robin) booking to another host, with audit trail. Commits."""
    if booking.status not in ACTIVE_BOOKING_STATUSES:
        raise APIError("INVALID_STATE", f"This booking is {booking.status}.", 409)
    if new_host_id == booking.host_id:
        return booking
    et = db.get(EventType, booking.event_type_id)
    eth = db.scalar(select(EventTypeHost).where(EventTypeHost.event_type_id == et.id, EventTypeHost.user_id == new_host_id))
    if not eth and new_host_id != et.owner_user_id:
        raise APIError(VALIDATION_ERROR, "That person is not a host of this event type.", 422)
    old_host = booking.host_id
    db.commit()
    try:
        lock_hosts(db, [old_host, new_host_id])
        if not force and not av.host_is_free(db, et, new_host_id, booking.start_at, booking.end_at,
                                             exclude_booking_id=booking.id, respect_schedule=False):
            raise APIError(SLOT_NO_LONGER_AVAILABLE, "That host is busy at this time.", 409)
        booking.host_id = new_host_id
        for bh in list(booking.hosts):
            if bh.user_id == old_host:
                booking.hosts.remove(bh)
        booking.hosts.append(BookingHost(user_id=new_host_id, required=True))
        booking.assignment_reason = f"reassigned by {actor}: {reason or 'no reason given'}"[:300]
        booking.version = (booking.version or 1) + 1
        if eth:
            eth.assignment_count = (eth.assignment_count or 0) + 1
            eth.last_assigned_at = tu.utcnow()
        old_eth = db.scalar(select(EventTypeHost).where(EventTypeHost.event_type_id == et.id, EventTypeHost.user_id == old_host))
        if old_eth and old_eth.assignment_count:
            old_eth.assignment_count -= 1
        db.flush()
        sp = db.begin_nested()
        try:
            cf.update_calendar_events(db, booking, previous={"host_id": old_host, "booking_id": booking.id,
                                                             "start_at": booking.start_at, "end_at": booking.end_at})
            sp.commit()
        except Exception as exc:
            sp.rollback()
            _history(db, booking, "host_action_required", "system", error=f"calendar update after reassignment failed: {exc}"[:500])
        _history(db, booking, "reassigned", actor, from_host=old_host, to_host=new_host_id, reason=reason)
        audit(db, "booking.reassigned", workspace_id=booking.workspace_id, actor_id=actor, entity_type="booking",
              entity_id=booking.id, metadata={"from": old_host, "to": new_host_id, "reason": reason})
        publish(db, "booking.reassigned", {"booking_id": booking.id, "actor": actor, "from_host": old_host, "to_host": new_host_id},
                workspace_id=booking.workspace_id)
        db.commit()
        return booking
    except IntegrityError:
        db.rollback()
        raise APIError(SLOT_NO_LONGER_AVAILABLE, "That host is busy at this time.", 409)
    except Exception:
        db.rollback()
        raise


# ------------------------------------------------------------------ serializers
def manage_urls(booking: Booking) -> dict:
    base = settings.base_url.rstrip("/")
    t = booking.manage_token
    return {
        "manage_url": f"{base}/booking/{t}",
        "cancel_url": f"{base}/booking/{t}/cancel",
        "reschedule_url": f"{base}/booking/{t}/reschedule",
    }


def booking_to_dict(booking: Booking) -> dict:
    db = object_session(booking)
    et = db.get(EventType, booking.event_type_id) if db else None
    host = db.get(User, booking.host_id) if db else None
    snap = booking.snapshot_json or {}
    hosts = []
    for bh in booking.hosts:
        u = db.get(User, bh.user_id) if db else None
        hosts.append({"id": bh.user_id, "name": u.name if u else None, "email": u.email if u else None, "required": bh.required})
    return {
        "id": booking.id,
        "booking_id": booking.id,
        "status": booking.status,
        "event_type": {"id": booking.event_type_id, "name": snap.get("name") or (et.name if et else None),
                       "slug": snap.get("slug") or (et.slug if et else None), "kind": snap.get("kind")},
        "start_at": tu.iso_z(booking.start_at),
        "end_at": tu.iso_z(booking.end_at),
        "duration": int((booking.end_at - booking.start_at).total_seconds() // 60),
        "timezone": booking.timezone,
        "location": {k: v for k, v in (booking.location_json or {}).items()},
        "invitees": [
            {"name": i.name, "email": i.email, "phone": i.phone, "company": i.company, "timezone": i.timezone,
             "guests": i.guests_json or []}
            for i in booking.invitees
        ],
        "answers": [{"question_id": a.question_id, "label": a.label, "value": a.answer_json} for a in booking.answers],
        "host": {"id": booking.host_id, "name": host.name if host else None, "email": host.email if host else None},
        "hosts": hosts,
        "manage": manage_urls(booking),
        "utm": booking.utm_json or {},
        "source": booking.source,
        "no_show": booking.no_show,
        "payment_status": booking.payment_status,
        "cancel_reason": booking.cancel_reason,
        "cancelled_by": booking.cancelled_by,
        "cancellation_requested": bool(booking.cancellation_requested_at),
        "rescheduled_from_id": booking.rescheduled_from_id,
        "rescheduled_to_id": booking.rescheduled_to_id,
        "assignment_reason": booking.assignment_reason,
        "created_at": tu.iso_z(booking.created_at),
    }


# ------------------------------------------------------------------ PostgreSQL integrity backstop
PG_EXCLUSION_CONSTRAINT = "bookings_no_overlap"


def ensure_postgres_constraints(engine) -> bool:
    """PRD 7.4: on PostgreSQL create the btree_gist extension and an exclusion constraint so that
    two ACTIVE exclusive bookings of the same host can never overlap (the DB-level backstop behind
    host locking). Idempotent; a no-op (returns False) on other dialects such as SQLite.
    Call once at startup after metadata.create_all()."""
    if engine.dialect.name != "postgresql":
        return False
    from sqlalchemy import text

    statuses = ", ".join(f"'{s}'" for s in ACTIVE_BOOKING_STATUSES)
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
        exists = conn.execute(
            text("SELECT 1 FROM pg_constraint WHERE conname = :n"), {"n": PG_EXCLUSION_CONSTRAINT}
        ).first()
        if not exists:
            conn.execute(text(
                f"ALTER TABLE bookings ADD CONSTRAINT {PG_EXCLUSION_CONSTRAINT} EXCLUDE USING gist "
                f"(host_id WITH =, tsrange(start_at, end_at) WITH &&) "
                f"WHERE (exclusive AND status IN ({statuses}))"
            ))
    return True
