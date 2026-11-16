"""Calendar sync: busy-cache refresh, conflict detection, drift detection, watch renewal.

Registered periodic tasks:
  * calendar_sync  - per healthy connection (throttled): refresh tokens, pull events into the
                     busy cache (ExternalCalendarEvent), flag confirmed bookings that now overlap
                     an external busy event or whose calendar event was moved/deleted externally.
                     Never deletes either side; sets booking.conflict_flag, notifies host,
                     publishes booking.conflict.
  * calendar_retry - retries failed calendar writes with exponential backoff.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.events import publish
from app.models import (
    Booking,
    BookingHost,
    CalendarConnection,
    CalendarEvent,
    ConnectedCalendar,
    ExternalCalendarEvent,
    InAppNotification,
    utcnow,
)
from app.security import hash_secret
from app.services.calendar_facade import PermanentProviderError, TransientProviderError
from app.services.providers import registry
from app.services.providers.base import AuthError
from app.worker import periodic

log = logging.getLogger("calendar.sync")
SYNC_INTERVAL_SECONDS = 120
SYNC_HORIZON_DAYS = 60


def channel_token(conn_id: str) -> str:
    return hash_secret(f"calendar-channel:{conn_id}")[:48]


def flag_conflict(db: Session, booking: Booking, detail: str) -> bool:
    """Flag a booking as conflicting (idempotent per detail). Returns True if newly flagged."""
    if booking.conflict_flag and booking.conflict_detail == detail:
        return False
    booking.conflict_flag = True
    booking.conflict_detail = detail[:2000]
    db.add(InAppNotification(
        user_id=booking.host_id, kind="booking_conflict", title="Calendar conflict detected",
        body=detail, link=f"/app/scheduled/{booking.id}",
    ))
    publish(db, "booking.conflict", {"booking_id": booking.id, "detail": detail}, workspace_id=booking.workspace_id)
    db.flush()
    return True


def _user_confirmed_bookings(db: Session, user_id: str, now: dt.datetime) -> list[Booking]:
    sub = select(BookingHost.booking_id).where(BookingHost.user_id == user_id)
    return list(db.scalars(select(Booking).where(
        Booking.status == "confirmed", Booking.end_at > now,
        or_(Booking.host_id == user_id, Booking.id.in_(sub)),
    )).all())


def _refresh_cache(db: Session, conn: CalendarConnection, adapter, now: dt.datetime) -> None:
    """Pull external events into ExternalCalendarEvent rows (busy cache) for real providers."""
    end = now + dt.timedelta(days=SYNC_HORIZON_DAYS)
    for cal in registry.calendars_of(db, conn):
        if not cal.is_conflict_calendar:
            continue
        events = adapter.list_events(db, conn, cal.provider_calendar_id, now, end)
        existing = {r.external_id: r for r in db.scalars(select(ExternalCalendarEvent).where(
            ExternalCalendarEvent.calendar_id == cal.id, ExternalCalendarEvent.end_at > now)).all()}
        seen = set()
        for e in events:
            seen.add(e["external_id"])
            r = existing.get(e["external_id"])
            if not r:
                r = ExternalCalendarEvent(calendar_id=cal.id, external_id=e["external_id"])
                db.add(r)
            r.title, r.start_at, r.end_at = e["title"][:500], e["start"], e["end"]
            r.transparent = bool(e.get("transparent"))
            r.booking_id = e.get("booking_id") or None
        for ext_id, r in existing.items():
            if ext_id not in seen:
                db.delete(r)
    db.flush()


def detect_conflicts(db: Session, conn: CalendarConnection, now: Optional[dt.datetime] = None) -> int:
    """Flag confirmed bookings overlapping external busy events, and drifted booking events."""
    now = now or utcnow()
    adapter = registry.calendar_adapter(conn.provider)
    flagged = 0
    conflict_cal_ids = [c.id for c in registry.calendars_of(db, conn) if c.is_conflict_calendar]
    our_external_ids = set()
    for booking in _user_confirmed_bookings(db, conn.user_id, now):
        ces = db.scalars(select(CalendarEvent).where(
            CalendarEvent.booking_id == booking.id, CalendarEvent.connection_id == conn.id)).all()
        our_external_ids.update(ce.external_event_id for ce in ces if ce.external_event_id)
        if conflict_cal_ids:
            q = select(ExternalCalendarEvent).where(
                ExternalCalendarEvent.calendar_id.in_(conflict_cal_ids),
                ExternalCalendarEvent.start_at < booking.end_at,
                ExternalCalendarEvent.end_at > booking.start_at,
                ExternalCalendarEvent.transparent.is_(False),
                ExternalCalendarEvent.booking_id.is_(None),
            )
            overlaps = [x for x in db.scalars(q).all() if x.external_id not in our_external_ids]
            if overlaps:
                x = overlaps[0]
                detail = (f"External calendar event '{x.title}' "
                          f"({x.start_at:%Y-%m-%d %H:%M}-{x.end_at:%H:%M} UTC) overlaps this booking.")
                flagged += flag_conflict(db, booking, detail)
                continue
        # drift: our created event moved or deleted outside the scheduler
        for ce in ces:
            if ce.sync_status != "synced" or not ce.external_event_id:
                continue
            cal = registry._cal_for_row(db, conn)
            got = adapter.get_event(db, conn, cal.provider_calendar_id if cal else "", ce.external_event_id)
            if got is None:
                flagged += flag_conflict(db, booking, "The calendar event for this booking was deleted outside "
                                                      "AAL Scheduler. The booking is still confirmed.")
            elif abs((got["start"] - booking.start_at).total_seconds()) > 60 or abs(
                    (got["end"] - booking.end_at).total_seconds()) > 60:
                flagged += flag_conflict(db, booking, f"The calendar event was moved externally to "
                                                      f"{got['start']:%Y-%m-%d %H:%M} UTC; the booking time is unchanged.")
    return flagged


def sync_connection(db: Session, conn: CalendarConnection, now: Optional[dt.datetime] = None) -> None:
    now = now or utcnow()
    adapter = registry.calendar_adapter(conn.provider)
    if adapter is None or conn.status != "connected":
        return
    try:
        if conn.provider != "mock":
            if conn.expires_at and conn.expires_at <= now + dt.timedelta(minutes=5):
                adapter.refresh(db, conn)
            _refresh_cache(db, conn, adapter, now)
            _ensure_watch(db, conn, adapter)
        detect_conflicts(db, conn, now)
        conn.last_synced_at = now
        conn.last_error = None
        if (conn.sync_state_json or {}).get("consecutive_failures") or (conn.sync_state_json or {}).get("next_attempt_at"):
            state = dict(conn.sync_state_json or {})
            state["consecutive_failures"] = 0
            state.pop("next_attempt_at", None)
            conn.sync_state_json = state
        registry.invalidate_busy_cache(conn.id)
    except AuthError as exc:
        registry.mark_connection_error(db, conn, str(exc))
    except (TransientProviderError, PermanentProviderError) as exc:
        conn.last_error = f"Sync failed: {exc}"[:2000]
        state = dict(conn.sync_state_json or {})
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        # exponential backoff for rate limits (429) / timeouts / 5xx: 2, 4, 8 ... min (cap 1 h)
        delay = min(SYNC_INTERVAL_SECONDS * 2 ** (state["consecutive_failures"] - 1), 3600)
        state["next_attempt_at"] = (now + dt.timedelta(seconds=delay)).isoformat()
        conn.sync_state_json = state
        if state["consecutive_failures"] >= 5:
            registry.mark_connection_error(db, conn, conn.last_error)
    db.flush()


def _ensure_watch(db: Session, conn, adapter) -> None:
    state = dict(conn.sync_state_json or {})
    state["consecutive_failures"] = 0
    state.pop("next_attempt_at", None)
    exp = state.get("watch_expiration")
    if exp and str(exp) > str(int(utcnow().timestamp() * 1000)) and conn.provider == "google":
        conn.sync_state_json = state
        return
    path = "/webhooks/google" if conn.provider == "google" else "/webhooks/microsoft"
    cal = next((c for c in registry.calendars_of(db, conn) if c.is_conflict_calendar), None)
    if cal and settings.base_url.startswith("https://") and not state.get("watch_disabled"):
        try:
            res = adapter.watch(db, conn, cal.provider_calendar_id, settings.base_url + path, channel_token(conn.id))
            if res:
                state.update({f"watch_{k}": v for k, v in res.items()})
        except Exception as exc:  # noqa: BLE001 - watch is an optimisation; polling still works
            log.info("watch setup failed for %s: %s", conn.id, exc)
            state["watch_disabled"] = True
    conn.sync_state_json = state


@periodic("calendar_sync")
def calendar_sync_task(db: Session, force: bool = False) -> None:
    now = utcnow()
    for conn in db.scalars(select(CalendarConnection).where(
            CalendarConnection.provider.in_(registry.CALENDAR_PROVIDERS), CalendarConnection.status == "connected")).all():
        if not force and conn.last_synced_at and conn.last_synced_at > now - dt.timedelta(seconds=SYNC_INTERVAL_SECONDS):
            continue
        nxt = (conn.sync_state_json or {}).get("next_attempt_at")
        if not force and nxt and dt.datetime.fromisoformat(nxt) > now:
            continue  # backing off after transient provider errors
        sync_connection(db, conn, now)
        db.commit()


@periodic("calendar_retry")
def calendar_retry_task(db: Session) -> None:
    registry.retry_failed_events(db)


# ------------------------------------------------------------------ demo/mock helpers
def connect_mock_calendar(db: Session, user, workspace_id: str) -> CalendarConnection:
    from app.models import User  # noqa: F401

    conn = CalendarConnection(
        workspace_id=workspace_id, user_id=user.id, provider="mock", account_email=user.email,
        scopes="calendar.events calendar.readonly", status="connected", last_synced_at=utcnow(),
        sync_state_json={"mock": True},
    )
    db.add(conn)
    db.flush()
    has_write = registry.write_target(db, user.id)[1] is not None
    work = ConnectedCalendar(connection_id=conn.id, provider_calendar_id=f"{conn.id}-work", display_name="Work",
                             is_conflict_calendar=True, is_write_calendar=not has_write)
    personal = ConnectedCalendar(connection_id=conn.id, provider_calendar_id=f"{conn.id}-personal",
                                 display_name="Personal", is_conflict_calendar=True, is_write_calendar=False)
    db.add_all([work, personal])
    db.flush()
    return conn
