"""Managed event templates (PRD 17.2): admins define a template, lock sections, assign to members, sync.

A template is an EventType with is_managed_template=True (never bookable: status "paused",
visibility "private"). Children are EventTypes with managed_template_id = template.id owned by each
assigned member. locked_fields_json uses the event-type field keys understood by the event type editor
(e.g. "duration", "location", "questions", "description", "cancellation_policy").
"""
from __future__ import annotations

from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import EventQuestion, EventType, EventTypeHost, User, utcnow

# section key -> (label, list of EventType columns synced when locked)
LOCKABLE_SECTIONS: dict[str, tuple[str, list[str]]] = {
    "name": ("Event name", ["name"]),
    "description": ("Description", ["description"]),
    "duration": ("Duration", ["duration"]),
    "durations": ("Duration options", ["durations_json"]),
    "location": ("Location", ["location_json"]),
    "questions": ("Invitee questions", []),
    "cancellation_policy": ("Cancellation / reschedule policy", ["cancellation_policy_json"]),
    "confirmation": ("Confirmation page", ["confirmation_json"]),
    "min_notice_minutes": ("Minimum notice", ["min_notice_minutes"]),
    "max_days_ahead": ("Booking window", ["max_days_ahead"]),
    "buffer_before": ("Buffer before", ["buffer_before"]),
    "buffer_after": ("Buffer after", ["buffer_after"]),
    "daily_limit": ("Daily limit", ["daily_limit"]),
    "color": ("Colour", ["color"]),
    "price": ("Price / payment", ["price_minor", "currency", "payment_provider", "payment_description"]),
}

# Columns copied when a child is first created (everything bookable, not identity/ownership).
COPY_COLUMNS = [
    "name", "description", "color", "kind", "duration", "durations_json", "location_json", "slot_interval",
    "min_notice_minutes", "max_days_ahead", "buffer_before", "buffer_after", "daily_limit", "weekly_limit",
    "monthly_limit", "max_invitees", "show_remaining_seats", "calendar_event_title", "confirmation_json",
    "cancellation_policy_json", "freebusy_exceptions_json", "requires_email_verification", "price_minor",
    "currency", "payment_provider", "payment_description", "locked_fields_json",
]
TEMPLATE_SUFFIX = "-template"


def child_slug_base(template: EventType) -> str:
    s = template.slug or "managed"
    return s[: -len(TEMPLATE_SUFFIX)] if s.endswith(TEMPLATE_SUFFIX) else s


def _unique_slug(db: Session, owner_id: str, base: str) -> str:
    slug, n = base, 1
    while db.scalar(select(EventType.id).where(EventType.owner_user_id == owner_id, EventType.slug == slug)):
        n += 1
        slug = f"{base}-{n}"
    return slug


def children(db: Session, template: EventType) -> list[EventType]:
    return list(db.scalars(select(EventType).where(
        EventType.workspace_id == template.workspace_id, EventType.managed_template_id == template.id,
        EventType.deleted_at.is_(None))).all())


def _copy_questions(db: Session, src: EventType, dst: EventType) -> None:
    for q in list(dst.questions):
        db.delete(q)
    db.flush()
    for q in src.questions:
        db.add(EventQuestion(event_type_id=dst.id, type=q.type, label=q.label, help_text=q.help_text,
                             required=q.required, options_json=q.options_json, sort_order=q.sort_order,
                             prefill_key=q.prefill_key))
    db.flush()
    db.expire(dst, ["questions"])


def assign(db: Session, template: EventType, user_ids: Iterable[str]) -> list[EventType]:
    """Create a child event type for each member that doesn't already have one. Returns created children."""
    existing = {c.owner_user_id for c in children(db, template)}
    created = []
    for uid in dict.fromkeys(user_ids):
        if uid in existing or not db.get(User, uid):
            continue
        data = {c: getattr(template, c) for c in COPY_COLUMNS}
        child = EventType(
            workspace_id=template.workspace_id, owner_user_id=uid, team_id=template.team_id,
            slug=_unique_slug(db, uid, child_slug_base(template)), status="active", visibility="public",
            managed_template_id=template.id, is_managed_template=False, version=template.version, **data,
        )
        db.add(child)
        db.flush()
        _copy_questions(db, template, child)
        if child.kind != "one_on_one":
            db.add(EventTypeHost(event_type_id=child.id, user_id=uid, priority=1, required=True, active=True))
        created.append(child)
    db.flush()
    return created


def sync(db: Session, template: EventType) -> int:
    """Push locked sections from the template to every child. Returns number of children updated."""
    locked = list(template.locked_fields_json or [])
    n = 0
    for child in children(db, template):
        for key in locked:
            for col in LOCKABLE_SECTIONS.get(key, ("", [key] if hasattr(EventType, key) else []))[1]:
                setattr(child, col, getattr(template, col))
            if key == "questions":
                _copy_questions(db, template, child)
        child.locked_fields_json = locked
        child.version = (child.version or 1) + 1
        child.updated_at = utcnow()
        n += 1
    db.flush()
    return n


def detach(db: Session, child: EventType) -> None:
    child.managed_template_id = None
    child.locked_fields_json = []
