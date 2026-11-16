"""Provider registry - implements the calendar_facade contract on top of provider adapters.

  * get_busy_intervals  - busy time from the conflict calendars of all of a host's calendar
                          connections; raises CalendarUnavailable when any is broken.
  * create/update/delete_calendar_events - write booking events to each host's single
                          destination ("write") calendar with idempotency keys, create video
                          meetings (Meet via conferenceData, Teams via Graph, Zoom via API) and
                          store join URLs. Provider failures never fail the booking: the
                          CalendarEvent row is marked `failed` (retried by the `calendar_retry`
                          task), the connection is flagged when auth broke, and when a video link
                          cannot be created the location falls back to manual instructions with
                          `host_action_required`.

Callers own the transaction (we only add/flush).
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Booking,
    CalendarConnection,
    CalendarEvent,
    ConnectedCalendar,
    EventType,
    InAppNotification,
    User,
    utcnow,
)
from app.services.calendar_facade import (
    BusyInterval,
    CalendarUnavailable,
    PermanentProviderError,
    TransientProviderError,
)
from app.services.providers.base import AuthError, CalendarProvider, EventSpec

log = logging.getLogger("calendar")
CALENDAR_PROVIDERS = ("google", "microsoft", "mock")
VIDEO_TYPES = {"google_meet", "zoom", "teams"}
FALLBACK_TEXT = "The host will share video meeting details before the meeting."
MAX_SYNC_ATTEMPTS = 5

_busy_cache: dict = {}
_cache_lock = threading.Lock()
BUSY_CACHE_TTL = 60


# ------------------------------------------------------------------ adapters
def calendar_adapter(name: str) -> Optional[CalendarProvider]:
    if name == "mock":
        from app.services.providers.mock import MockCalendarProvider

        return MockCalendarProvider()
    if name == "google":
        from app.services.providers.google import GoogleCalendarProvider

        return GoogleCalendarProvider()
    if name == "microsoft":
        from app.services.providers.microsoft import MicrosoftCalendarProvider

        return MicrosoftCalendarProvider()
    return None


def calendar_connections(db: Session, user_id: str) -> list[CalendarConnection]:
    return list(
        db.scalars(
            select(CalendarConnection)
            .where(CalendarConnection.user_id == user_id, CalendarConnection.provider.in_(CALENDAR_PROVIDERS))
            .order_by(CalendarConnection.created_at)
        ).all()
    )


def video_connection(db: Session, user_id: str, provider: str) -> Optional[CalendarConnection]:
    return db.scalar(
        select(CalendarConnection).where(CalendarConnection.user_id == user_id, CalendarConnection.provider == provider)
    )


def calendars_of(db: Session, conn: CalendarConnection) -> list[ConnectedCalendar]:
    return list(db.scalars(select(ConnectedCalendar).where(ConnectedCalendar.connection_id == conn.id)).all())


def write_target(db: Session, user_id: str) -> tuple[Optional[CalendarConnection], Optional[ConnectedCalendar]]:
    for conn in calendar_connections(db, user_id):
        for cal in calendars_of(db, conn):
            if cal.is_write_calendar:
                return conn, cal
    return None, None


def invalidate_busy_cache(conn_id: Optional[str] = None) -> None:
    with _cache_lock:
        if conn_id is None:
            _busy_cache.clear()
        else:
            _busy_cache.pop(conn_id, None)


def mark_connection_error(db: Session, conn: CalendarConnection, error: str) -> None:
    """Flag a broken connection (reconnect state) and emit calendar.sync_failed once."""
    from app.events import publish

    conn.last_error = (error or "")[:2000]
    invalidate_busy_cache(conn.id)
    if conn.status != "error":
        conn.status = "error"
        publish(
            db, "calendar.sync_failed",
            {"connection_id": conn.id, "user_id": conn.user_id, "provider": conn.provider, "error": conn.last_error},
            workspace_id=conn.workspace_id,
        )
    db.flush()


# ------------------------------------------------------------------ busy
def get_busy_intervals(db: Session, user_id: str, start_utc: dt.datetime, end_utc: dt.datetime) -> list[BusyInterval]:
    out: list[BusyInterval] = []
    for conn in calendar_connections(db, user_id):
        if conn.status != "connected":
            raise CalendarUnavailable(f"{conn.provider} calendar needs reconnecting")
        cal_ids = [c.provider_calendar_id for c in calendars_of(db, conn) if c.is_conflict_calendar]
        if not cal_ids:
            continue
        adapter = calendar_adapter(conn.provider)
        if adapter is None:
            continue
        cache_key = (tuple(sorted(cal_ids)), start_utc, end_utc)
        if conn.provider != "mock":
            with _cache_lock:
                hit = _busy_cache.get(conn.id)
            if hit and hit[0] == cache_key and time.monotonic() - hit[1] < BUSY_CACHE_TTL:
                out += hit[2]
                continue
        try:
            intervals = adapter.free_busy(db, conn, cal_ids, start_utc, end_utc)
        except AuthError as exc:
            mark_connection_error(db, conn, str(exc))
            raise CalendarUnavailable(f"{conn.provider} calendar needs reconnecting") from exc
        except (TransientProviderError, PermanentProviderError) as exc:
            conn.last_error = str(exc)[:2000]
            raise CalendarUnavailable(f"Could not verify {conn.provider} calendar availability") from exc
        if conn.provider != "mock":
            with _cache_lock:
                _busy_cache[conn.id] = (cache_key, time.monotonic(), intervals)
        out += intervals
    return out


# ------------------------------------------------------------------ booking events
def _host_ids(booking) -> list[str]:
    ids = [h.user_id for h in (getattr(booking, "hosts", None) or [])]
    if booking.host_id:
        ids = [booking.host_id] + ids
    return list(dict.fromkeys(i for i in ids if i))


def _event_name(db: Session, booking) -> str:
    snap = booking.snapshot_json or {}
    if snap.get("name"):
        return snap["name"]
    et = db.get(EventType, booking.event_type_id) if booking.event_type_id else None
    return et.name if et else "Meeting"


def build_spec(db: Session, booking, conference: Optional[str] = None, location_text: str = "") -> EventSpec:
    et = db.get(EventType, booking.event_type_id) if booking.event_type_id else None
    invitees = list(getattr(booking, "invitees", None) or [])
    inv_name = invitees[0].name if invitees else "Invitee"
    host = db.get(User, booking.host_id) if booking.host_id else None
    tmpl = (et.calendar_event_title if et and et.calendar_event_title else "{event_name} with {invitee_name}")
    title = (
        tmpl.replace("{event_name}", _event_name(db, booking))
        .replace("{invitee_name}", inv_name)
        .replace("{host_name}", host.name if host else "")
    )
    attendees = []
    for inv in invitees:
        attendees.append(inv.email)
        attendees += [g for g in (inv.guests_json or []) if isinstance(g, str)]
    lines = [f"Booked via AAL Scheduler.", f"Invitee: {', '.join(f'{i.name} <{i.email}>' for i in invitees)}"]
    for a in getattr(booking, "answers", None) or []:
        lines.append(f"{a.label}: {a.answer_json}")
    try:
        from app.services.booking import manage_urls

        lines.append(f"Manage: {manage_urls(booking).get('manage_url', '')}")
    except Exception:
        pass
    loc = booking.location_json or {}
    if not location_text:
        location_text = loc.get("join_url") or loc.get("value") or ""
    return EventSpec(
        title=title[:300], start=booking.start_at, end=booking.end_at, timezone=booking.timezone or "UTC",
        description="\n".join(lines), location=location_text, attendees=attendees, conference=conference,
        booking_id=booking.id,
    )


def _notify_host_action(db: Session, booking, user_id: str, msg: str) -> None:
    db.add(InAppNotification(
        user_id=user_id, kind="host_action_required", title="Action required: add meeting details",
        body=msg, link=f"/app/scheduled/{booking.id}",
    ))


def _set_location(booking, **fields) -> None:
    loc = dict(booking.location_json or {})
    loc.update({k: v for k, v in fields.items() if v is not None})
    booking.location_json = loc


def _create_for_host(db: Session, booking, uid: str, row: CalendarEvent, primary: bool) -> None:
    loc = booking.location_json or {}
    loc_type = loc.get("type")
    conn, cal = write_target(db, uid)
    join_url = conf_id = conf_provider = None
    video_error = None

    # Zoom / standalone Teams meetings are created before the calendar event (primary host only).
    if primary and loc_type == "zoom":
        zc = video_connection(db, uid, "zoom")
        if not zc:
            video_error = "Zoom is not connected"
        else:
            from app.services.providers.video import ZoomProvider

            try:
                m = ZoomProvider().create_meeting(db, zc, build_spec(db, booking))
                join_url, conf_id, conf_provider = m.join_url, m.meeting_id, "zoom"
            except Exception as exc:  # noqa: BLE001 - fall back to manual details
                video_error = f"Zoom meeting creation failed: {exc}"
    conference = None
    if primary and loc_type == "google_meet":
        if conn and conn.provider in ("google", "mock") and conn.status == "connected":
            conference = "google_meet"
        else:
            video_error = "Google Meet needs a connected Google Calendar as destination calendar"
    if primary and loc_type == "teams":
        if conn and conn.provider in ("microsoft", "mock") and conn.status == "connected":
            conference = "teams"
        else:
            ms = next((c for c in calendar_connections(db, uid) if c.provider == "microsoft" and c.status == "connected"), None)
            if ms:
                from app.services.providers.video import TeamsProvider

                try:
                    m = TeamsProvider().create_meeting(db, ms, build_spec(db, booking))
                    join_url, conf_id, conf_provider = m.join_url, m.meeting_id, "teams"
                except Exception as exc:  # noqa: BLE001
                    video_error = f"Teams meeting creation failed: {exc}"
            else:
                video_error = "Microsoft Teams needs a connected Microsoft 365 calendar"

    if not primary and loc.get("join_url"):
        join_url = loc.get("join_url")

    if conn is None:
        row.provider, row.connection_id, row.sync_status = "none", None, "synced"
        row.join_url, row.conference_id, row.conference_provider = join_url, conf_id, conf_provider
    else:
        row.provider, row.connection_id = conn.provider, conn.id
        adapter = calendar_adapter(conn.provider)
        spec = build_spec(db, booking, conference=conference, location_text=join_url or "")
        row.attempts = (row.attempts or 0) + 1
        try:
            if conn.status != "connected":
                raise AuthError(f"{conn.provider} calendar needs reconnecting")
            created = adapter.create_event(db, conn, cal.provider_calendar_id, spec, row.idempotency_key)
            row.external_event_id = created.external_id
            row.sync_status, row.last_error = "synced", None
            if created.join_url:
                join_url, conf_id, conf_provider = created.join_url, created.conference_id, created.conference_provider
            elif conference and not join_url:
                video_error = f"{conference} link was not returned by the calendar provider"
            row.join_url, row.conference_id, row.conference_provider = join_url, conf_id, conf_provider
        except (AuthError, TransientProviderError, PermanentProviderError) as exc:
            row.sync_status, row.last_error = "failed", str(exc)[:2000]
            row.join_url, row.conference_id, row.conference_provider = join_url, conf_id, conf_provider
            if isinstance(exc, AuthError):
                mark_connection_error(db, conn, str(exc))
            elif conn.status == "connected":
                from app.events import publish

                publish(db, "calendar.sync_failed", {
                    "connection_id": conn.id, "user_id": uid, "booking_id": booking.id, "provider": conn.provider,
                    "error": row.last_error,
                }, workspace_id=booking.workspace_id)
            if conference and not join_url:
                video_error = f"Could not create {conference} link: calendar write failed"

    if primary:
        if join_url:
            _set_location(booking, join_url=join_url, conference_provider=conf_provider, conference_id=conf_id,
                          host_action_required=False)
            loc2 = dict(booking.location_json or {})
            loc2.pop("fallback", None)
            booking.location_json = loc2
        elif loc_type in VIDEO_TYPES and video_error:
            _set_location(booking, host_action_required=True, fallback=FALLBACK_TEXT, video_error=video_error)
            _notify_host_action(db, booking, uid, f"{video_error}. Please send the invitee meeting details.")


def create_calendar_events(db: Session, booking) -> None:
    hosts = _host_ids(booking)
    for i, uid in enumerate(hosts):
        key = f"cal:create:{booking.id}:{uid}"
        row = db.scalar(select(CalendarEvent).where(CalendarEvent.idempotency_key == key))
        if row and row.sync_status in ("synced", "deleted"):
            continue  # idempotent: already created (or since deleted)
        if not row:
            row = CalendarEvent(booking_id=booking.id, user_id=uid, provider="none", sync_status="pending",
                                idempotency_key=key, attempts=0)
            db.add(row)
            db.flush()
        _create_for_host(db, booking, uid, row, primary=(i == 0))
    db.flush()


def _conn_for_row(db: Session, row: CalendarEvent) -> Optional[CalendarConnection]:
    return db.get(CalendarConnection, row.connection_id) if row.connection_id else None


def _cal_for_row(db: Session, conn: CalendarConnection) -> Optional[ConnectedCalendar]:
    cals = calendars_of(db, conn)
    return next((c for c in cals if c.is_write_calendar), cals[0] if cals else None)


def _apply(db: Session, row: CalendarEvent, booking, op: str) -> None:
    """Run update/delete for one CalendarEvent row, recording failures instead of raising."""
    from app.services.providers.video import ZoomProvider, TeamsProvider

    conn = _conn_for_row(db, row)
    # video meetings (zoom / standalone teams)
    if row.conference_provider in ("zoom", "teams") and row.conference_id:
        vconn = video_connection(db, row.user_id, "zoom") if row.conference_provider == "zoom" else (
            conn if conn and conn.provider == "microsoft" else None)
        vp = ZoomProvider() if row.conference_provider == "zoom" else TeamsProvider()
        if vconn and (row.conference_provider == "zoom" or not (conn and conn.provider == "microsoft" and row.external_event_id)):
            try:
                if op == "delete":
                    vp.delete_meeting(db, vconn, row.conference_id)
                else:
                    vp.update_meeting(db, vconn, row.conference_id, build_spec(db, booking))
            except Exception as exc:  # noqa: BLE001
                log.warning("video %s failed: %s", op, exc)
    if row.provider == "none" or not conn or not row.external_event_id:
        if op == "delete":
            row.sync_status = "deleted"
        return
    adapter = calendar_adapter(conn.provider)
    cal = _cal_for_row(db, conn)
    row.attempts = (row.attempts or 0) + 1
    try:
        if conn.status != "connected":
            raise AuthError(f"{conn.provider} calendar needs reconnecting")
        if op == "delete":
            adapter.delete_event(db, conn, cal.provider_calendar_id if cal else "", row.external_event_id)
            row.sync_status, row.last_error = "deleted", None
        else:
            spec = build_spec(db, booking, location_text=row.join_url or "")
            adapter.update_event(db, conn, cal.provider_calendar_id if cal else "", row.external_event_id, spec)
            row.sync_status, row.last_error = "synced", None
    except (AuthError, TransientProviderError, PermanentProviderError) as exc:
        row.sync_status, row.last_error = "failed", f"{op}: {exc}"[:2000]
        if isinstance(exc, AuthError):
            mark_connection_error(db, conn, str(exc))


def update_calendar_events(db: Session, booking, previous: Optional[dict] = None) -> None:
    rows = list(db.scalars(select(CalendarEvent).where(
        CalendarEvent.booking_id == booking.id, CalendarEvent.sync_status != "deleted")).all())
    if not rows and getattr(booking, "rescheduled_from_id", None):
        # Reschedule creates a new booking row: move the old booking's events onto it.
        rows = list(db.scalars(select(CalendarEvent).where(
            CalendarEvent.booking_id == booking.rescheduled_from_id, CalendarEvent.sync_status != "deleted")).all())
        for r in rows:
            r.booking_id = booking.id
            r.idempotency_key = f"cal:create:{booking.id}:{r.user_id}"
            if r.join_url and r.user_id == booking.host_id:
                _set_location(booking, join_url=r.join_url, conference_provider=r.conference_provider,
                              conference_id=r.conference_id)
        db.flush()
    if not rows:
        create_calendar_events(db, booking)
        return
    for r in rows:
        if r.external_event_id or r.provider == "none":
            _apply(db, r, booking, "update")
    # hosts without a row yet (e.g. new collective host) get created
    create_calendar_events(db, booking)
    db.flush()


def delete_calendar_events(db: Session, booking) -> None:
    for r in db.scalars(select(CalendarEvent).where(
            CalendarEvent.booking_id == booking.id, CalendarEvent.sync_status != "deleted")).all():
        _apply(db, r, booking, "delete")
    db.flush()


# ------------------------------------------------------------------ retry of failed writes
def retry_failed_events(db: Session, now: Optional[dt.datetime] = None) -> int:
    now = now or utcnow()
    n = 0
    rows = db.scalars(select(CalendarEvent).where(
        CalendarEvent.sync_status == "failed", CalendarEvent.attempts < MAX_SYNC_ATTEMPTS)).all()
    for r in rows:
        if r.updated_at and r.updated_at > now - dt.timedelta(minutes=2 ** max(0, (r.attempts or 1) - 1)):
            continue  # exponential backoff
        conn = _conn_for_row(db, r)
        if conn and conn.status != "connected":
            continue
        booking = db.get(Booking, r.booking_id)
        if not booking:
            continue
        if booking.status in ("cancelled", "rescheduled", "expired", "failed"):
            _apply(db, r, booking, "delete")
        elif not r.external_event_id:
            _create_for_host(db, booking, r.user_id, r, primary=(r.user_id == booking.host_id))
        else:
            _apply(db, r, booking, "update")
        n += 1
    db.flush()
    return n
