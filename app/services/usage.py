"""Usage metering (ACC-005 / roadmap phase 4). Meters:

  bookings_created  - outbox booking.created (once per booking via the outbox's idempotent processing)
  emails_sent       - notifications sender + commerce/billing emails
  sms_sent          - notifications sender
  api_calls         - every authenticated /api/v1 request (API key or OAuth token); buffered in
                      memory via count_usage() and written by flush_usage()/the usage_flush task

`record_usage(db, workspace_id, meter, quantity)` adds a row in the caller's transaction.
`usage_summary(db, workspace_id)` aggregates for /app/billing.
"""
from __future__ import annotations

import datetime as dt
import threading
from collections import defaultdict
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.events import subscribe
from app.models import Booking, UsageEvent, utcnow
from app.worker import periodic

METERS = {
    "bookings_created": "Bookings created",
    "emails_sent": "Emails sent",
    "sms_sent": "SMS sent",
    "api_calls": "API calls",
}


def record_usage(db: Session, workspace_id: Optional[str], meter: str, quantity: int = 1) -> None:
    if not workspace_id or quantity <= 0:
        return
    db.add(UsageEvent(workspace_id=workspace_id, meter=meter, quantity=quantity))


# High-frequency meters (API calls) are counted in memory and flushed in batches by the
# `usage_flush` worker task (and before usage is displayed), so requests never block on a write.
_pending: dict[tuple[str, str], int] = defaultdict(int)
_pending_lock = threading.Lock()


def count_usage(workspace_id: Optional[str], meter: str, quantity: int = 1) -> None:
    if workspace_id and quantity > 0:
        with _pending_lock:
            _pending[(workspace_id, meter)] += quantity


def flush_usage(db: Session) -> int:
    """Write buffered counters as UsageEvent rows (caller commits). Returns rows written."""
    with _pending_lock:
        items = list(_pending.items())
        _pending.clear()
    for (ws, meter), qty in items:
        record_usage(db, ws, meter, qty)
    db.flush()
    return len(items)


@periodic("usage_flush")
def usage_flush_task(db: Session) -> None:
    flush_usage(db)


@subscribe("booking.created")
def _meter_booking_created(db: Session, ev) -> None:
    bid = (ev.payload_json or {}).get("booking_id")
    b = db.get(Booking, bid) if bid else None
    ws = ev.workspace_id or (b.workspace_id if b else None)
    if not ws:
        return
    # A failing sibling handler rolls back this row too, so an outbox retry never double counts.
    record_usage(db, ws, "bookings_created", 1)


def period_start(now: Optional[dt.datetime] = None) -> dt.datetime:
    now = now or utcnow()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def usage_summary(db: Session, workspace_id: str, since: Optional[dt.datetime] = None) -> list[dict]:
    since = since or period_start()
    rows = dict(db.execute(select(UsageEvent.meter, func.sum(UsageEvent.quantity)).where(
        UsageEvent.workspace_id == workspace_id, UsageEvent.occurred_at >= since).group_by(UsageEvent.meter)).all())
    total = dict(db.execute(select(UsageEvent.meter, func.sum(UsageEvent.quantity)).where(
        UsageEvent.workspace_id == workspace_id).group_by(UsageEvent.meter)).all())
    out = []
    for m, label in METERS.items():
        out.append({"meter": m, "label": label, "period": int(rows.get(m) or 0), "total": int(total.get(m) or 0)})
    for m in sorted(set(total) - set(METERS)):
        out.append({"meter": m, "label": m.replace("_", " ").capitalize(), "period": int(rows.get(m) or 0),
                    "total": int(total.get(m) or 0)})
    return out
