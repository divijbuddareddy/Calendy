"""Availability engine (PRD 7.1, 7.2, 20.1, 20.2, 10.2-10.4).

Public API
----------
    get_available_slots(db, event_type, start_date, end_date, tz, duration=None,
                        exclude_booking_id=None, now=None, include_full=False) -> list[Slot]
    get_available_days(db, event_type, start_date, end_date, tz, ...) -> list[date]
    check_slot(db, event_type, start_utc, duration=None, exclude_booking_id=None, now=None) -> list[str]
    slot_token(event_type_id, start_utc, duration) / verify_slot_token(token, event_type_id, start_utc, duration)

`start_date`/`end_date` are dates in the invitee timezone `tz` (inclusive).

Calendar connections in error state raise `CalendarUnavailable` (re-exported here) so callers can
show a "host calendar unavailable" state instead of unverified availability (acceptance #35).
For round robin, hosts with a broken calendar are simply not eligible; it raises only when every
host is unverifiable.

Algorithm
---------
1. Per host: resolve schedule (event schedule for the owner, else the host's default schedule),
   weekly rules (or the event's custom_rules_json, which applies to EVERY host of the event and is
   read as wall-clock time in each host's own schedule timezone), date overrides / holidays /
   days off, plus the public holidays of the schedule's `holiday_country` (an explicit override
   row for that date wins) -> availability *windows* as UTC intervals. Local ranges are converted with zoneinfo, so DST
   gaps shrink a window and DST overlaps lengthen it.
2. Candidate starts step `slot_interval` minutes of *elapsed* time from each window start. Within
   a window this equals stepping local wall time, skips nonexistent spring-forward times, and
   yields both instants of the repeated fall-back hour (labelled with their UTC offsets).
   Collective: windows are intersected across required hosts first. Round robin: union.
   One-off events use only their explicit `one_off_slots_json` starts.
3. Filters: requested range, min notice, max days ahead, buffers around internal bookings, holds
   and calendar busy (minus free/busy exceptions), event daily/weekly/monthly limits, host-wide
   limits (User.working_hours_json {"daily_limit","weekly_limit","monthly_limit"}), round-robin
   daily_capacity, group capacity.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field
from typing import Iterable, Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    ACTIVE_BOOKING_STATUSES,
    AvailabilityOverride,
    AvailabilityRule,
    AvailabilitySchedule,
    Booking,
    BookingHold,
    BookingHost,
    EventType,
    EventTypeHost,
    User,
)
from app.security import sign, unsign
from app.services import timeutil as tu
from app.services.calendar_facade import CalendarUnavailable, get_busy_intervals

__all__ = [
    "Slot",
    "CalendarUnavailable",
    "get_available_slots",
    "get_available_days",
    "check_slot",
    "slot_token",
    "verify_slot_token",
    "event_host_ids",
    "allowed_durations",
    "resolve_duration",
    "availability_version",
]

SLOT_SALT = "slot-token"


@dataclass
class Slot:
    start: dt.datetime  # naive UTC
    end: dt.datetime  # naive UTC
    host_ids: list[str] = field(default_factory=list)  # eligible hosts
    seats_remaining: Optional[int] = None  # group events
    full: bool = False  # group session at capacity (only returned with include_full=True)
    token: str = ""  # short-lived signed slot token

    def to_dict(self, tz: str, hour12: bool = False) -> dict:
        return {
            "start_at": tu.iso_z(self.start),
            "end_at": tu.iso_z(self.end),
            "label": tu.slot_label(self.start, tz),
            "label_12h": tu.slot_label(self.start, tz, hour12=True),
            "seats_remaining": self.seats_remaining,
            "full": self.full,
            "slot_token": self.token,
        }


# ------------------------------------------------------------------ helpers
def slot_token(event_type_id: str, start_utc: dt.datetime, duration: int) -> str:
    return sign({"e": event_type_id, "s": tu.iso_z(start_utc), "d": int(duration)}, SLOT_SALT)


def verify_slot_token(token: str, event_type_id: str, start_utc: dt.datetime, duration: int) -> bool:
    data = unsign(token or "", SLOT_SALT, max_age=settings.slot_token_ttl_seconds)
    if not isinstance(data, dict):
        return False
    return data.get("e") == event_type_id and data.get("s") == tu.iso_z(start_utc) and int(data.get("d", 0)) == int(duration)


def allowed_durations(et: EventType) -> list[int]:
    vals = [int(d) for d in (et.durations_json or []) if str(d).isdigit() and int(d) > 0]
    if et.duration and et.duration not in vals:
        vals.append(int(et.duration))
    return sorted(set(vals))


def resolve_duration(et: EventType, duration: Optional[int]) -> int:
    """Validated duration in minutes (ValueError if not offered by the event)."""
    if duration in (None, "", 0):
        return int(et.duration)
    d = int(duration)
    if d not in allowed_durations(et):
        raise ValueError(f"Duration {d} minutes is not offered for this event")
    return d


def _active_hosts(db: Session, et: EventType) -> list[EventTypeHost]:
    rows = [h for h in et.hosts if h.active]
    out = []
    for h in rows:
        u = db.get(User, h.user_id)
        if u and not u.deleted_at:
            out.append(h)
    return sorted(out, key=lambda h: (-(h.priority or 0), h.user_id))


def event_host_ids(db: Session, et: EventType) -> list[str]:
    """Hosts whose availability matters (candidates to lock when booking)."""
    hosts = _active_hosts(db, et)
    if et.kind == "collective":
        req = [h.user_id for h in hosts if h.required]
        return req or [et.owner_user_id]
    if et.kind == "round_robin":
        return [h.user_id for h in hosts] or [et.owner_user_id]
    ids = [h.user_id for h in hosts]
    if not ids or et.owner_user_id in ids:
        return [et.owner_user_id]
    return ids[:1]


def optional_host_ids(db: Session, et: EventType) -> list[str]:
    if et.kind != "collective":
        return []
    return [h.user_id for h in _active_hosts(db, et) if not h.required]


def _merge(intervals: Iterable[tuple]) -> list[tuple]:
    out: list[list] = []
    for s, e in sorted(intervals):
        if e <= s:
            continue
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def _intersect(a: list[tuple], b: list[tuple]) -> list[tuple]:
    out, i, j = [], 0, 0
    while i < len(a) and j < len(b):
        s, e = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if s < e:
            out.append((s, e))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def _overlaps(s: dt.datetime, e: dt.datetime, intervals: list[tuple]) -> bool:
    for bs, be in intervals:
        if bs < e and be > s:
            return True
    return False


def _rule_ranges(rules: list) -> dict[int, list[tuple[int, int]]]:
    by_wd: dict[int, list[tuple[int, int]]] = {}
    for r in rules or []:
        try:
            if isinstance(r, dict):
                wd = int(r.get("weekday"))
                s = tu.parse_hhmm(r.get("start") or r.get("start_local"))
                e = tu.parse_hhmm(r.get("end") or r.get("end_local"))
            else:
                wd, s, e = int(r.weekday), tu.parse_hhmm(r.start_local), tu.parse_hhmm(r.end_local)
        except (TypeError, ValueError, AttributeError):
            continue
        if 0 <= wd <= 6 and e > s:
            by_wd.setdefault(wd, []).append((s, e))
    return by_wd


# ------------------------------------------------------------------ per-host state
@dataclass
class _Host:
    user_id: str
    tz: str
    windows: list = field(default_factory=list)
    busy: list = field(default_factory=list)  # (start, end) already expanded with existing buffers
    bookings: list = field(default_factory=list)  # active Booking rows involving host
    limits: dict = field(default_factory=dict)
    eth: Optional[EventTypeHost] = None
    unavailable: bool = False


def _schedule_for(db: Session, et: EventType, user_id: str) -> Optional[AvailabilitySchedule]:
    if et.schedule_id:
        sch = db.get(AvailabilitySchedule, et.schedule_id)
        if sch and sch.user_id == user_id:
            return sch
    sch = db.scalar(
        select(AvailabilitySchedule).where(AvailabilitySchedule.user_id == user_id, AvailabilitySchedule.is_default.is_(True))
    )
    if not sch:
        sch = db.scalar(
            select(AvailabilitySchedule).where(AvailabilitySchedule.user_id == user_id).order_by(AvailabilitySchedule.created_at)
        )
    return sch


def _holiday_dates(country: Optional[str], d0: dt.date, d1: dt.date) -> set:
    """Public holidays (local dates) for the schedule's holiday_country within [d0, d1]."""
    if not country:
        return set()
    from app.services.holidays import holidays_for

    out = set()
    for year in range(d0.year, d1.year + 1):
        out |= {d for d, _ in holidays_for(country, year) if d0 <= d <= d1}
    return out


def _host_windows(db: Session, et: EventType, user_id: str, ws: dt.datetime, we: dt.datetime) -> tuple[str, list]:
    sch = _schedule_for(db, et, user_id)
    user = db.get(User, user_id)
    if not sch:
        return tu.validate_tz(user.default_timezone if user else "UTC"), []
    tz = tu.validate_tz(sch.timezone or (user.default_timezone if user else "UTC"))
    if et.custom_rules_json:
        # Event-specific hours apply to EVERY host of the event (team events included), each
        # interpreted as wall-clock times in that host's own schedule timezone. Without custom
        # rules each host uses their own schedule (owner: the event's schedule, others: default).
        weekly = _rule_ranges(et.custom_rules_json)
    else:
        weekly = _rule_ranges(db.scalars(select(AvailabilityRule).where(AvailabilityRule.schedule_id == sch.id)).all())
    d0 = tu.local_date(ws, tz) - dt.timedelta(days=1)
    d1 = tu.local_date(we, tz) + dt.timedelta(days=1)
    overrides = {
        o.local_date: o
        for o in db.scalars(
            select(AvailabilityOverride).where(
                AvailabilityOverride.schedule_id == sch.id,
                AvailabilityOverride.local_date >= d0,
                AvailabilityOverride.local_date <= d1,
            )
        ).all()
    }
    holiday_dates = _holiday_dates(sch.holiday_country, d0, d1)
    windows = []
    d = d0
    while d <= d1:
        ov = overrides.get(d)
        if ov is None and d in holiday_dates:
            ranges = []  # public holiday of the schedule's country (an explicit override row wins)
        elif ov is not None:
            if ov.kind in ("holiday", "day_off") and not ov.intervals_json:
                ranges = []
            else:
                ranges = []
                for iv in ov.intervals_json or []:
                    try:
                        s, e = tu.parse_hhmm(iv["start"]), tu.parse_hhmm(iv["end"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if e > s:
                        ranges.append((s, e))
        else:
            ranges = weekly.get(d.weekday(), [])
        for s, e in _merge(ranges):
            us = tu.wall_to_utc(d, s, tz, "earliest")
            ue = tu.wall_to_utc(d, e, tz, "latest")
            if ue > us:
                windows.append((us, ue))
        d += dt.timedelta(days=1)
    return tz, _merge(windows)


def _is_active(b: Booking, now: dt.datetime) -> bool:
    if b.status not in ACTIVE_BOOKING_STATUSES:
        return False
    if b.status in ("held", "pending_payment") and b.hold_expires_at and b.hold_expires_at < now:
        return False
    return True


def _host_bookings(db: Session, user_id: str, s: dt.datetime, e: dt.datetime, exclude_id: Optional[str], now) -> list[Booking]:
    sub = select(BookingHost.booking_id).where(BookingHost.user_id == user_id)
    rows = db.scalars(
        select(Booking).where(
            or_(Booking.host_id == user_id, Booking.id.in_(sub)),
            Booking.status.in_(ACTIVE_BOOKING_STATUSES),
            Booking.start_at < e,
            Booking.end_at > s,
        )
    ).all()
    return [b for b in rows if b.id != exclude_id and _is_active(b, now)]


def _build_host(
    db: Session, et: EventType, user_id: str, ws: dt.datetime, we: dt.datetime, exclude_id, now, max_buffer: int
) -> _Host:
    tz, windows = _host_windows(db, et, user_id, ws, we)
    h = _Host(user_id=user_id, tz=tz, windows=windows)
    user = db.get(User, user_id)
    h.limits = dict((user.working_hours_json or {}) if user else {})
    h.eth = next((x for x in et.hosts if x.user_id == user_id), None)
    pad = dt.timedelta(minutes=max_buffer + 24 * 60)
    # wide load window so monthly limits count correctly
    h.bookings = _host_bookings(db, user_id, ws - dt.timedelta(days=32), we + dt.timedelta(days=32), exclude_id, now)
    busy = []
    for b in h.bookings:
        snap = b.snapshot_json or {}
        bb = int(snap.get("buffer_before") or 0)
        ba = int(snap.get("buffer_after") or 0)
        if et.kind == "group" and b.event_type_id == et.id and not b.exclusive:
            continue  # seats of this group event are handled as capacity, not busy time
        busy.append((b.start_at - dt.timedelta(minutes=bb), b.end_at + dt.timedelta(minutes=ba), "booking"))
    for hold in db.scalars(
        select(BookingHold).where(
            BookingHold.host_id == user_id,
            BookingHold.released.is_(False),
            BookingHold.expires_at > now,
            BookingHold.start_at < we + pad,
            BookingHold.end_at > ws - pad,
        )
    ).all():
        if exclude_id and hold.booking_id == exclude_id:
            continue
        busy.append((hold.start_at, hold.end_at, "hold"))
    try:
        cal = get_busy_intervals(db, user_id, ws - pad, we + pad)
    except CalendarUnavailable:
        h.unavailable = True
        cal = []
    exceptions = [str(x).strip().lower() for x in (et.freebusy_exceptions_json or []) if str(x).strip()]
    for bi in cal:
        title = (bi.title or "").strip().lower()
        if exceptions and any(x == title or x in title for x in exceptions):
            continue
        busy.append((bi.start, bi.end, "calendar"))
    h.busy = [(s, e) for s, e, _ in busy]
    return h


def _period_keys(start: dt.datetime, tz: str) -> tuple:
    d = tu.local_date(start, tz)
    iso = d.isocalendar()
    return d, (iso[0], iso[1]), (d.year, d.month)


def _count_ok(bookings: list[Booking], start: dt.datetime, tz: str, limits: dict, group_sessions: bool = False) -> bool:
    lim = {k: limits.get(k) for k in ("daily_limit", "weekly_limit", "monthly_limit")}
    if not any(lim.values()):
        return True
    day, week, month = _period_keys(start, tz)
    seen = set()
    counts = {"daily_limit": 0, "weekly_limit": 0, "monthly_limit": 0}
    for b in bookings:
        key = (b.event_type_id, b.start_at) if (group_sessions or not b.exclusive) else b.id
        if key in seen:
            continue
        seen.add(key)
        bd, bw, bm = _period_keys(b.start_at, tz)
        if bd == day:
            counts["daily_limit"] += 1
        if bw == week:
            counts["weekly_limit"] += 1
        if bm == month:
            counts["monthly_limit"] += 1
    for k, v in lim.items():
        if v and int(v) > 0 and counts[k] >= int(v):
            return False
    return True


def _host_ok(h: _Host, et: EventType, s: dt.datetime, e: dt.datetime, existing_session: bool) -> bool:
    if h.unavailable:
        return False
    if not any(ws <= s and e <= we for ws, we in h.windows):
        return False
    bb = dt.timedelta(minutes=et.buffer_before or 0)
    ba = dt.timedelta(minutes=et.buffer_after or 0)
    if _overlaps(s - bb, e + ba, h.busy):
        return False
    if existing_session:
        return True  # joining an existing group session does not add a meeting
    ev_bookings = [b for b in h.bookings if b.event_type_id == et.id]
    ev_limits = {"daily_limit": et.daily_limit, "weekly_limit": et.weekly_limit, "monthly_limit": et.monthly_limit}
    if not _count_ok(ev_bookings, s, h.tz, ev_limits, group_sessions=et.kind == "group"):
        return False
    if not _count_ok(h.bookings, s, h.tz, h.limits):
        return False
    if h.eth is not None and h.eth.daily_capacity:
        assigned = [b for b in ev_bookings if b.host_id == h.user_id]
        if not _count_ok(assigned, s, h.tz, {"daily_limit": h.eth.daily_capacity}):
            return False
    return True


def _group_seats(db: Session, et: EventType, ws: dt.datetime, we: dt.datetime, exclude_id, now) -> dict:
    rows = db.scalars(
        select(Booking).where(
            Booking.event_type_id == et.id,
            Booking.status.in_(ACTIVE_BOOKING_STATUSES),
            Booking.start_at >= ws - dt.timedelta(days=1),
            Booking.start_at < we + dt.timedelta(days=1),
        )
    ).all()
    seats: dict = {}
    for b in rows:
        if b.id == exclude_id or not _is_active(b, now):
            continue
        seats[b.start_at] = seats.get(b.start_at, 0) + 1  # one seat per booking
    return seats


# ------------------------------------------------------------------ core
def _compute(
    db: Session,
    et: EventType,
    ws: dt.datetime,
    we: dt.datetime,
    duration: Optional[int],
    exclude_booking_id: Optional[str],
    now: Optional[dt.datetime],
    *,
    include_full: bool = False,
    for_reschedule: bool = False,
    only_start: Optional[dt.datetime] = None,
    host_ids: Optional[list[str]] = None,
) -> list[Slot]:
    now = now or tu.utcnow()
    if et.deleted_at:
        return []
    if not for_reschedule:
        if et.status != "active" or et.is_managed_template:
            return []
        if et.single_use and et.used_at:
            return []
    dur = resolve_duration(et, duration)
    dur_td = dt.timedelta(minutes=dur)
    step = dt.timedelta(minutes=max(5, int(et.slot_interval or dur)))
    earliest = now + dt.timedelta(minutes=et.min_notice_minutes or 0)
    horizon = now + dt.timedelta(days=et.max_days_ahead) if (et.max_days_ahead and not et.is_one_off) else None
    lo, hi = max(ws, earliest), we
    if horizon:
        hi = min(hi, horizon + dt.timedelta(seconds=1))
    if lo >= hi:
        return []

    hosts_ids = host_ids or event_host_ids(db, et)
    max_buffer = (et.buffer_before or 0) + (et.buffer_after or 0)
    hosts = [_build_host(db, et, hid, lo - dur_td, hi + dur_td, exclude_booking_id, now, max_buffer) for hid in hosts_ids]
    if hosts and all(h.unavailable for h in hosts):
        raise CalendarUnavailable("Host calendar is unavailable; availability cannot be verified right now.")
    if et.kind != "round_robin" and any(h.unavailable for h in hosts):
        raise CalendarUnavailable("Host calendar is unavailable; availability cannot be verified right now.")

    # candidate starts
    candidates: set = set()
    if et.is_one_off:
        for v in et.one_off_slots_json or []:
            try:
                candidates.add(tu.parse_iso_utc(v if isinstance(v, str) else v.get("start_at")))
            except (ValueError, AttributeError, TypeError):
                continue
        # one-off slots are explicit; they are not constrained by the weekly schedule
        for h in hosts:
            h.windows = _merge(h.windows + [(c, c + dur_td) for c in candidates])
    else:
        if et.kind == "collective":
            win = hosts[0].windows if hosts else []
            for h in hosts[1:]:
                win = _intersect(win, h.windows)
            window_sets = [win]
        else:
            window_sets = [h.windows for h in hosts]
        for wins in window_sets:
            for s0, e0 in wins:
                s = s0
                while s + dur_td <= e0:
                    candidates.add(s)
                    s += step
    if only_start is not None:
        candidates = {c for c in candidates if c == only_start}

    seats = _group_seats(db, et, lo, hi, exclude_booking_id, now) if et.kind == "group" else {}
    capacity = max(1, int(et.max_invitees or 1))
    out: list[Slot] = []
    for s in sorted(candidates):
        if s < lo or s >= hi:
            continue
        e = s + dur_td
        taken = seats.get(s, 0)
        existing = taken > 0
        eligible = [h.user_id for h in hosts if _host_ok(h, et, s, e, existing)]
        if et.kind == "collective":
            if len(eligible) != len(hosts):
                continue
        elif not eligible:
            continue
        remaining = None
        full = False
        if et.kind == "group":
            remaining = capacity - taken
            if remaining <= 0:
                if not include_full:
                    continue
                full, remaining = True, 0
        out.append(Slot(start=s, end=e, host_ids=eligible, seats_remaining=remaining, full=full,
                        token="" if full else slot_token(et.id, s, dur)))
    return out


def _range_utc(start_date: dt.date, end_date: dt.date, tz: str) -> tuple[dt.datetime, dt.datetime]:
    tz = tu.validate_tz(tz)
    return tu.local_midnight_utc(start_date, tz), tu.local_midnight_utc(end_date + dt.timedelta(days=1), tz)


def get_available_slots(
    db: Session,
    event_type: EventType,
    start_date: dt.date,
    end_date: dt.date,
    tz: str,
    duration: Optional[int] = None,
    exclude_booking_id: Optional[str] = None,
    now: Optional[dt.datetime] = None,
    include_full: bool = False,
    for_reschedule: bool = False,
) -> list[Slot]:
    """Bookable slots whose start falls on [start_date, end_date] in timezone `tz`.
    Raises CalendarUnavailable when host availability cannot be verified."""
    if end_date < start_date:
        return []
    end_date = min(end_date, start_date + dt.timedelta(days=62))  # bound the work per request
    ws, we = _range_utc(start_date, end_date, tz)
    return _compute(db, event_type, ws, we, duration, exclude_booking_id, now,
                    include_full=include_full, for_reschedule=for_reschedule)


def get_available_days(
    db: Session,
    event_type: EventType,
    start_date: dt.date,
    end_date: dt.date,
    tz: str,
    duration: Optional[int] = None,
    exclude_booking_id: Optional[str] = None,
    now: Optional[dt.datetime] = None,
) -> list[dt.date]:
    tz = tu.validate_tz(tz)
    slots = get_available_slots(db, event_type, start_date, end_date, tz, duration, exclude_booking_id, now)
    return sorted({tu.local_date(s.start, tz) for s in slots})


def check_slot(
    db: Session,
    event_type: EventType,
    start_utc: dt.datetime,
    duration: Optional[int] = None,
    exclude_booking_id: Optional[str] = None,
    now: Optional[dt.datetime] = None,
    *,
    for_reschedule: bool = False,
    host_ids: Optional[list[str]] = None,
) -> list[str]:
    """Eligible host ids for this exact start ([] = unavailable). Collective returns all required hosts.
    Raises CalendarUnavailable if availability cannot be verified."""
    start_utc = tu.to_naive_utc(start_utc)
    ws, we = start_utc - dt.timedelta(minutes=1), start_utc + dt.timedelta(minutes=1)
    slots = _compute(db, event_type, ws, we, duration, exclude_booking_id, now,
                     for_reschedule=for_reschedule, only_start=start_utc, host_ids=host_ids)
    return slots[0].host_ids if slots else []


def group_seats_remaining(db: Session, et: EventType, start_utc: dt.datetime, now=None, exclude_booking_id=None) -> int:
    now = now or tu.utcnow()
    seats = _group_seats(db, et, start_utc, start_utc + dt.timedelta(minutes=1), exclude_booking_id, now)
    return max(0, int(et.max_invitees or 1) - seats.get(start_utc, 0))


def host_is_free(db: Session, et: EventType, user_id: str, start_utc: dt.datetime, end_utc: dt.datetime,
                 exclude_booking_id: Optional[str] = None, now=None, respect_schedule: bool = True) -> bool:
    """Is a specific host free (schedule + busy) for an interval? Used for reassignment/optional hosts."""
    now = now or tu.utcnow()
    try:
        h = _build_host(db, et, user_id, start_utc - dt.timedelta(days=1), end_utc + dt.timedelta(days=1),
                        exclude_booking_id, now, (et.buffer_before or 0) + (et.buffer_after or 0))
    except CalendarUnavailable:
        return False
    if h.unavailable:
        return False
    if respect_schedule and not any(ws <= start_utc and end_utc <= we for ws, we in h.windows):
        return False
    return not _overlaps(start_utc, end_utc, h.busy)


def availability_version(slots: list[Slot]) -> str:
    h = hashlib.sha1()
    for s in slots:
        h.update(f"{s.start.isoformat()}|{s.seats_remaining}|{','.join(s.host_ids)};".encode())
    return h.hexdigest()[:16]
