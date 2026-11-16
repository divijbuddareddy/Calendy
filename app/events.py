"""Transactional outbox + in-process pub/sub.

Producers call `publish(db, topic, payload, workspace_id=...)` inside the same transaction as
their state change (do NOT commit inside publish). The worker (or tests via
`process_outbox(db)`) later delivers each event to subscribers registered with
`@subscribe("booking.created")` or `@subscribe("*")`.

Topics (PRD webhook event names): booking.created, booking.cancelled, booking.rescheduled,
booking.no_show, routing.submitted, payment.succeeded, payment.failed,
plus internal: booking.confirmed_pending_calendar, calendar.sync_failed, booking.conflict,
booking.cancellation_requested, waitlist.spot_open.

Payload convention for booking.* topics: {"booking_id": ..., "actor": ..., plus extras}.
Handlers must be idempotent (an event may be retried) and must not commit partial work on error.
"""
import logging
import traceback
from collections import defaultdict
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OutboxEvent, utcnow

log = logging.getLogger("events")
_subscribers: dict[str, list[Callable]] = defaultdict(list)
MAX_ATTEMPTS = 8


def subscribe(topic: str):
    def deco(fn: Callable[[Session, OutboxEvent], None]):
        if fn not in _subscribers[topic]:
            _subscribers[topic].append(fn)
        return fn

    return deco


def publish(db: Session, topic: str, payload: dict, workspace_id: str | None = None) -> OutboxEvent:
    ev = OutboxEvent(topic=topic, payload_json=payload, workspace_id=workspace_id)
    db.add(ev)
    db.flush()
    return ev


def process_outbox(db: Session, limit: int = 100) -> int:
    """Deliver pending outbox events to subscribers. Returns number processed."""
    events = db.scalars(
        select(OutboxEvent)
        .where(OutboxEvent.processed_at.is_(None), OutboxEvent.attempts < MAX_ATTEMPTS)
        .order_by(OutboxEvent.created_at)
        .limit(limit)
    ).all()
    done = 0
    for ev in events:
        handlers = list(_subscribers.get(ev.topic, [])) + list(_subscribers.get("*", []))
        try:
            for h in handlers:
                h(db, ev)
            ev.processed_at = utcnow()
            ev.attempts += 1
            db.commit()
            done += 1
        except Exception as exc:  # retry later
            db.rollback()
            ev = db.get(OutboxEvent, ev.id)
            ev.attempts += 1
            ev.last_error = f"{exc}\n{traceback.format_exc()[-2000:]}"
            db.commit()
            log.exception("outbox handler failed for %s", ev.topic)
    return done
