"""Booking analytics (PRD 14). Pure read-side aggregation over bookings / page views /
routing submissions / payments, always scoped to one workspace.

Dataset definition (so every KPI reconciles with the underlying rows / CSV export):
  * bookings whose `created_at` falls in [start, end) and whose status is a "real" booking
    (held / expired / failed reservations are excluded).
  * created     = every row of the dataset
  * cancelled   = status == cancelled
  * rescheduled = status == rescheduled (the original booking that was moved)
  * no_shows    = no_show flag set
  * completed   = status confirmed, already ended, not a no-show
  * upcoming    = status confirmed/pending/pending_payment and starts in the future
Retention: analytics reads raw rows. Tenant-configurable `settings_json.retention_days`
(default 365, min 30) is enforced by the `analytics_retention` periodic task: invitee PII of
bookings that ended before the cutoff is anonymised, but booking rows (status, times, event type,
host, duration, UTM source/medium/campaign) are kept so aggregate counts stay correct.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Booking,
    BookingAnswer,
    Workspace,
    EventType,
    Invitee,
    PageView,
    PaymentRecord,
    RoutingForm,
    RoutingSubmission,
    User,
    utcnow,
)

EXCLUDED_STATUSES = ("held", "expired", "failed")
DEFAULT_RETENTION_DAYS = 365
REDACTED_DOMAIN = "@redacted.invalid"
# booking lead time histogram (hours from booking creation to meeting start)
LEAD_BUCKETS = [(0, 1, "< 1 hour"), (1, 24, "1-24 hours"), (24, 72, "1-3 days"), (72, 168, "3-7 days"),
                (168, 336, "1-2 weeks"), (336, None, "2+ weeks")]
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass
class Filters:
    workspace_id: str
    start: dt.datetime  # naive UTC, inclusive
    end: dt.datetime  # naive UTC, exclusive
    tz: str = "UTC"
    host_id: Optional[str] = None
    team_id: Optional[str] = None
    event_type_id: Optional[str] = None
    extra: dict = field(default_factory=dict)


def _zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def local_range_to_utc(start_date: dt.date, end_date: dt.date, tz: str) -> tuple[dt.datetime, dt.datetime]:
    """Inclusive local date range -> [start, end) naive UTC."""
    z = _zone(tz)
    s = dt.datetime.combine(start_date, dt.time.min, tzinfo=z).astimezone(dt.timezone.utc).replace(tzinfo=None)
    e = dt.datetime.combine(end_date + dt.timedelta(days=1), dt.time.min, tzinfo=z)
    return s, e.astimezone(dt.timezone.utc).replace(tzinfo=None)


def to_local(value: dt.datetime, tz: str) -> dt.datetime:
    return value.replace(tzinfo=dt.timezone.utc).astimezone(_zone(tz))


def booking_query(f: Filters):
    q = (
        select(Booking, EventType)
        .join(EventType, EventType.id == Booking.event_type_id)
        .where(
            Booking.workspace_id == f.workspace_id,
            Booking.created_at >= f.start,
            Booking.created_at < f.end,
            Booking.status.notin_(EXCLUDED_STATUSES),
        )
    )
    if f.host_id:
        q = q.where(Booking.host_id == f.host_id)
    if f.team_id:
        q = q.where(EventType.team_id == f.team_id)
    if f.event_type_id:
        q = q.where(Booking.event_type_id == f.event_type_id)
    return q.order_by(Booking.created_at)


def fetch_bookings(db: Session, f: Filters) -> list[tuple[Booking, EventType]]:
    return [(b, e) for b, e in db.execute(booking_query(f)).all()]


def classify(b: Booking, now: dt.datetime) -> str:
    if b.status == "cancelled":
        return "cancelled"
    if b.status == "rescheduled":
        return "rescheduled"
    if b.no_show:
        return "no_show"
    if b.status == "confirmed" and b.end_at <= now:
        return "completed"
    if b.status in ("confirmed", "pending", "pending_payment") and b.start_at >= now:
        return "upcoming"
    return "other"  # e.g. confirmed and currently in progress, or pending in the past


def _bars(counter: Counter | dict, order: Optional[list] = None, labels: Optional[dict] = None) -> list[dict]:
    keys = order if order is not None else [k for k, _ in sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0])))]
    mx = max([counter.get(k, 0) for k in keys] + [1])
    return [
        {"key": k, "label": (labels or {}).get(k, str(k)), "count": counter.get(k, 0), "pct": round(100 * counter.get(k, 0) / mx)}
        for k in keys
    ]


def compute(db: Session, f: Filters) -> dict:
    now = utcnow()
    rows = fetch_bookings(db, f)
    kpi = Counter()
    by_event = Counter()
    event_names: dict[str, str] = {}
    by_weekday = Counter()
    by_hour = Counter()
    durations = Counter()
    lead_hours: list[float] = []
    utm = Counter()
    rr_hosts: dict[str, Counter] = defaultdict(Counter)
    host_ids: set[str] = set()
    heat = [[0] * 24 for _ in range(7)]
    for b, et in rows:
        kpi["created"] += 1
        c = classify(b, now)
        if c != "other":
            kpi[c] += 1
        by_event[et.id] += 1
        event_names[et.id] = et.name
        loc = to_local(b.start_at, f.tz)
        by_weekday[loc.weekday()] += 1
        by_hour[loc.hour] += 1
        heat[loc.weekday()][loc.hour] += 1
        durations[int((b.end_at - b.start_at).total_seconds() // 60)] += 1
        lead_hours.append(max(0.0, (b.start_at - b.created_at).total_seconds() / 3600))
        u = b.utm_json or {}
        if any(u.get(k) for k in ("utm_source", "utm_medium", "utm_campaign", "source", "medium", "campaign")):
            key = (
                u.get("utm_source") or u.get("source") or "-",
                u.get("utm_medium") or u.get("medium") or "-",
                u.get("utm_campaign") or u.get("campaign") or "-",
            )
            utm[key] += 1
        if et.kind == "round_robin":
            rr_hosts[et.id][b.host_id] += 1
            host_ids.add(b.host_id)
            event_names[et.id] = et.name

    # page views -> conversion
    pv_q = select(PageView).where(
        PageView.workspace_id == f.workspace_id, PageView.created_at >= f.start, PageView.created_at < f.end
    )
    if f.event_type_id:
        pv_q = pv_q.where(PageView.event_type_id == f.event_type_id)
    views = db.scalars(pv_q).all()
    booking_page_views = [v for v in views if v.page in ("event", "profile")]
    event_views = len([v for v in views if v.page == "event"]) or len(booking_page_views)
    conversion = round(100 * kpi["created"] / event_views, 1) if event_views else None

    # routing
    forms = {fm.id: fm for fm in db.scalars(select(RoutingForm).where(RoutingForm.workspace_id == f.workspace_id)).all()}
    subs = db.scalars(
        select(RoutingSubmission).where(
            RoutingSubmission.workspace_id == f.workspace_id,
            RoutingSubmission.created_at >= f.start,
            RoutingSubmission.created_at < f.end,
        )
    ).all()
    routed_booking_sub_ids = set(
        db.scalars(
            select(Booking.routing_submission_id).where(
                Booking.workspace_id == f.workspace_id, Booking.routing_submission_id.is_not(None)
            )
        ).all()
    )
    routing = []
    per_form: dict[str, list] = defaultdict(list)
    for s in subs:
        per_form[s.form_id].append(s)
    for fid, ss in per_form.items():
        dist = Counter()
        rules = {r.get("id"): r for r in (forms[fid].rules_json or [])} if fid in forms else {}
        for s in ss:
            if s.matched_rule_id:
                r = rules.get(s.matched_rule_id) or {}
                label = r.get("name") or f"Rule {s.matched_rule_id}"
            else:
                label = "Fallback"
            dest = (s.destination_json or {}).get("type", "?")
            dist[f"{label} → {dest}"] += 1
        converted = sum(1 for s in ss if s.booking_id or s.id in routed_booking_sub_ids)
        routing.append(
            {
                "form": forms[fid].name if fid in forms else fid,
                "submissions": len(ss),
                "converted": converted,
                "conversion": round(100 * converted / len(ss), 1) if ss else 0,
                "distribution": _bars(dist),
            }
        )

    # round robin balance
    users = {u.id: u for u in db.scalars(select(User).where(User.id.in_(host_ids))).all()} if host_ids else {}
    rr = []
    for eid, cnt in rr_hosts.items():
        total = sum(cnt.values())
        vals = list(cnt.values())
        rr.append(
            {
                "event": event_names.get(eid, eid),
                "total": total,
                "hosts": _bars(cnt, labels={h: (users[h].name or users[h].email) if h in users else h for h in cnt}),
                "spread": (max(vals) - min(vals)) if vals else 0,
            }
        )

    # revenue (succeeded payments for bookings in the dataset)
    booking_ids = [b.id for b, _ in rows]
    revenue: dict[tuple[str, str], int] = defaultdict(int)
    if booking_ids:
        et_by_booking = {b.id: et for b, et in rows}
        for i in range(0, len(booking_ids), 500):
            chunk = booking_ids[i : i + 500]
            for p in db.scalars(
                select(PaymentRecord).where(
                    PaymentRecord.workspace_id == f.workspace_id,
                    PaymentRecord.booking_id.in_(chunk),
                    PaymentRecord.status == "succeeded",
                )
            ).all():
                revenue[(et_by_booking[p.booking_id].name, p.currency)] += p.amount_minor
    revenue_rows = [{"event": k[0], "currency": k[1], "amount_minor": v} for k, v in sorted(revenue.items())]

    # popular time slots heatmap (weekday x hour, viewer timezone)
    heat_max = max([max(r) for r in heat] + [0])
    heatmap = {
        "max": heat_max,
        "rows": [{"day": WEEKDAYS[d], "cells": [{"hour": h, "count": heat[d][h],
                                                   "level": (0 if not heat[d][h] else max(1, round(4 * heat[d][h] / heat_max)))}
                                                  for h in range(24)]} for d in range(7)],
        "top": sorted(({"day": WEEKDAYS[d], "hour": f"{h:02d}:00", "count": heat[d][h]}
                       for d in range(7) for h in range(24) if heat[d][h]), key=lambda x: -x["count"])[:5],
    }
    lead_hist = Counter()
    for h in lead_hours:
        for lo, hi, label in LEAD_BUCKETS:
            if h >= lo and (hi is None or h < hi):
                lead_hist[label] += 1
                break
    srt = sorted(lead_hours)
    median_lead = None
    if srt:
        mid = len(srt) // 2
        median_lead = round(srt[mid] if len(srt) % 2 else (srt[mid - 1] + srt[mid]) / 2, 1)
    popular_duration = max(durations.items(), key=lambda kv: (kv[1], -kv[0]))[0] if durations else None

    return {
        "kpi": {k: kpi.get(k, 0) for k in ("created", "completed", "cancelled", "rescheduled", "no_show", "upcoming")},
        "heatmap": heatmap,
        "lead_time": {
            "avg_hours": round(sum(lead_hours) / len(lead_hours), 1) if lead_hours else None,
            "median_hours": median_lead,
            "buckets": _bars(lead_hist, order=[b[2] for b in LEAD_BUCKETS]),
        },
        "popular_event": event_names.get(by_event.most_common(1)[0][0]) if by_event else None,
        "popular_duration": popular_duration,
        "by_event": _bars(by_event, labels=event_names),
        "by_weekday": _bars(by_weekday, order=list(range(7)), labels=dict(enumerate(WEEKDAYS))),
        "by_hour": _bars(by_hour, order=list(range(24)), labels={h: f"{h:02d}:00" for h in range(24)}),
        "durations": _bars(durations, labels={d: f"{d} min" for d in durations}),
        "avg_lead_hours": round(sum(lead_hours) / len(lead_hours), 1) if lead_hours else None,
        "page_views": len(booking_page_views),
        "event_page_views": event_views,
        "conversion": conversion,
        "utm": [{"source": k[0], "medium": k[1], "campaign": k[2], "count": v} for k, v in utm.most_common()],
        "routing": routing,
        "routing_total": len(subs),
        "rr": rr,
        "revenue": revenue_rows,
        "rows": rows,
    }


CSV_HEADER = [
    "booking_id", "event_type", "host_email", "invitee_name", "invitee_email", "status", "outcome", "no_show",
    "start_utc", "end_utc", "created_utc", "duration_min", "source", "utm_source", "utm_medium", "utm_campaign",
    "routing_submission_id", "lead_time_hours",
]


def csv_rows(db: Session, rows: list[tuple[Booking, EventType]]) -> list[list]:
    now = utcnow()
    host_ids = {b.host_id for b, _ in rows}
    hosts = {u.id: u for u in db.scalars(select(User).where(User.id.in_(host_ids))).all()} if host_ids else {}
    out = []
    for b, et in rows:
        inv = db.scalar(select(Invitee).where(Invitee.booking_id == b.id).order_by(Invitee.created_at))
        u = b.utm_json or {}
        out.append(
            [
                b.id, et.name, hosts[b.host_id].email if b.host_id in hosts else b.host_id,
                inv.name if inv else "", inv.email if inv else "", b.status, classify(b, now), "yes" if b.no_show else "no",
                b.start_at.isoformat(), b.end_at.isoformat(), b.created_at.isoformat(),
                int((b.end_at - b.start_at).total_seconds() // 60), b.source or "",
                u.get("utm_source", ""), u.get("utm_medium", ""), u.get("utm_campaign", ""), b.routing_submission_id or "",
                round(max(0.0, (b.start_at - b.created_at).total_seconds() / 3600), 1),
            ]
        )
    return out


def csv_safe(value) -> str:
    """Neutralise spreadsheet formula injection in exported cells."""
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


# --------------------------------------------------------------------------- previous-period comparison
def compare_previous(db: Session, f: Filters) -> dict:
    """KPI counts for the equally long period immediately before f (for created/completed/... deltas)."""
    span = f.end - f.start
    prev = Filters(workspace_id=f.workspace_id, start=f.start - span, end=f.start, tz=f.tz, host_id=f.host_id,
                   team_id=f.team_id, event_type_id=f.event_type_id)
    now = utcnow()
    c = Counter()
    for b, _ in fetch_bookings(db, prev):
        c["created"] += 1
        k = classify(b, now)
        if k != "other":
            c[k] += 1
    return {k: c.get(k, 0) for k in ("created", "completed", "cancelled", "rescheduled", "no_show", "upcoming")}


# --------------------------------------------------------------------------- retention
def retention_days(ws: Workspace) -> int:
    try:
        days = int((ws.settings_json or {}).get("retention_days") or DEFAULT_RETENTION_DAYS)
    except (TypeError, ValueError):
        days = DEFAULT_RETENTION_DAYS
    return max(30, days)


KEEP_UTM = ("utm_source", "utm_medium", "utm_campaign", "source", "medium", "campaign")


def purge_workspace(db: Session, ws: Workspace, now: Optional[dt.datetime] = None, batch: int = 500) -> int:
    """Anonymise invitee PII of bookings that ended before the retention cutoff. Idempotent.
    Returns the number of invitees anonymised. Booking rows are kept for aggregate analytics."""
    now = now or utcnow()
    cutoff = now - dt.timedelta(days=retention_days(ws))
    invs = db.scalars(
        select(Invitee).join(Booking, Booking.id == Invitee.booking_id).where(
            Booking.workspace_id == ws.id, Booking.end_at < cutoff, ~Invitee.email.like("%" + REDACTED_DOMAIN),
        ).limit(batch)
    ).all()
    booking_ids = set()
    for inv in invs:
        booking_ids.add(inv.booking_id)
        inv.name = "Anonymised"
        inv.email = f"retained+{inv.id}{REDACTED_DOMAIN}"
        inv.phone = None
        inv.company = None
        inv.consent_json = None
        inv.guests_json = None
    if booking_ids:
        for a in db.scalars(select(BookingAnswer).where(BookingAnswer.booking_id.in_(booking_ids))).all():
            a.answer_json = "[purged]"
        for b in db.scalars(select(Booking).where(Booking.id.in_(booking_ids))).all():
            b.host_notes = None
            b.cancel_reason = None
            if b.utm_json:
                b.utm_json = {k: v for k, v in b.utm_json.items() if k in KEEP_UTM} or None
    # routing submissions: keep the row (route distribution / conversion), drop the answers
    for sub in db.scalars(select(RoutingSubmission).where(
        RoutingSubmission.workspace_id == ws.id, RoutingSubmission.created_at < cutoff,
    )).all():
        if sub.answers_json and "_purged" not in sub.answers_json:
            sub.answers_json = {"_purged": "[purged]"}
    db.flush()
    return len(invs)


def purge_expired_pii(db: Session, now: Optional[dt.datetime] = None) -> int:
    total = 0
    for ws in db.scalars(select(Workspace)).all():
        total += purge_workspace(db, ws, now)
    return total


from app.worker import periodic  # noqa: E402


@periodic("analytics_retention")
def _analytics_retention_task(db: Session):
    purge_expired_pii(db)
