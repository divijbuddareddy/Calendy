"""Built-in offline "demo calendar" provider.

Stores events in `external_calendar_events` so the whole product is demoable without any
credentials: hosts add busy blocks in the UI, booking events are written to the destination
calendar (rows with booking_id set, which are excluded from busy computation because internal
bookings already block time), and the UI can simulate token expiry or external moves/deletes.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import secrets
from typing import Optional

from sqlalchemy import select

from app.models import ConnectedCalendar, ExternalCalendarEvent
from app.services.providers.base import (
    AuthError,
    BusyInterval,
    CalendarInfo,
    CalendarProvider,
    CreatedEvent,
    EventSpec,
    PermanentProviderError,
)


def fake_meet_link(seed: str) -> str:
    h = hashlib.sha256(seed.encode()).hexdigest()
    letters = "".join(chr(97 + int(c, 16) % 26) for c in h[:10])
    return f"https://meet.google.com/{letters[:3]}-{letters[3:7]}-{letters[7:10]}"


def fake_teams_link(seed: str) -> str:
    h = hashlib.sha256(seed.encode()).hexdigest()
    return f"https://teams.microsoft.com/l/meetup-join/19%3ameeting_{h[:32]}%40thread.v2/0"


def fake_zoom_meeting(seed: str) -> tuple[str, str]:
    h = int(hashlib.sha256(seed.encode()).hexdigest()[:12], 16)
    mid = str(80000000000 + h % 9999999999)
    pwd = hashlib.sha256((seed + "pwd").encode()).hexdigest()[:10]
    return mid, f"https://zoom.us/j/{mid}?pwd={pwd}"


class MockCalendarProvider(CalendarProvider):
    name = "mock"
    scopes = ["calendar.events", "calendar.readonly"]
    supports_conference = {"google_meet", "teams"}

    def is_configured(self) -> bool:
        return True

    def _check(self, conn):
        if conn.status != "connected":
            raise AuthError("Demo calendar token expired - reconnect required")

    def _cal(self, db, conn, calendar_id: str) -> ConnectedCalendar:
        cal = db.scalar(
            select(ConnectedCalendar).where(
                ConnectedCalendar.connection_id == conn.id, ConnectedCalendar.provider_calendar_id == calendar_id
            )
        )
        if not cal:
            raise PermanentProviderError(f"Calendar {calendar_id} not found")
        return cal

    def list_calendars(self, db, conn) -> list[CalendarInfo]:
        self._check(conn)
        return [
            CalendarInfo(c.provider_calendar_id, c.display_name)
            for c in db.scalars(select(ConnectedCalendar).where(ConnectedCalendar.connection_id == conn.id)).all()
        ]

    def free_busy(self, db, conn, calendar_ids, start, end) -> list[BusyInterval]:
        self._check(conn)
        cals = db.scalars(
            select(ConnectedCalendar).where(
                ConnectedCalendar.connection_id == conn.id, ConnectedCalendar.provider_calendar_id.in_(calendar_ids)
            )
        ).all()
        if not cals:
            return []
        rows = db.scalars(
            select(ExternalCalendarEvent).where(
                ExternalCalendarEvent.calendar_id.in_([c.id for c in cals]),
                ExternalCalendarEvent.start_at < end,
                ExternalCalendarEvent.end_at > start,
                ExternalCalendarEvent.transparent.is_(False),
                ExternalCalendarEvent.booking_id.is_(None),
            )
        ).all()
        return [BusyInterval(r.start_at, r.end_at, r.title, "mock") for r in rows]

    def create_event(self, db, conn, calendar_id, spec: EventSpec, idempotency_key) -> CreatedEvent:
        self._check(conn)
        cal = self._cal(db, conn, calendar_id)
        ext_id = "mockev_" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:20]
        existing = db.scalar(
            select(ExternalCalendarEvent).where(
                ExternalCalendarEvent.calendar_id == cal.id, ExternalCalendarEvent.external_id == ext_id
            )
        )
        if not existing:
            db.add(
                ExternalCalendarEvent(
                    calendar_id=cal.id, external_id=ext_id, title=spec.title, start_at=spec.start, end_at=spec.end,
                    transparent=False, booking_id=spec.booking_id or "external",
                )
            )
            db.flush()
        join = conf_id = conf_provider = None
        if spec.conference == "google_meet":
            join, conf_provider = fake_meet_link(idempotency_key), "google_meet"
            conf_id = join.rsplit("/", 1)[-1]
        elif spec.conference == "teams":
            join, conf_provider = fake_teams_link(idempotency_key), "teams"
            conf_id = "teams_" + ext_id[-12:]
        return CreatedEvent(ext_id, join, conf_id, conf_provider)

    def _find(self, db, conn, external_id) -> Optional[ExternalCalendarEvent]:
        cal_ids = [c.id for c in db.scalars(select(ConnectedCalendar).where(ConnectedCalendar.connection_id == conn.id))]
        if not cal_ids:
            return None
        return db.scalar(
            select(ExternalCalendarEvent).where(
                ExternalCalendarEvent.calendar_id.in_(cal_ids), ExternalCalendarEvent.external_id == external_id
            )
        )

    def update_event(self, db, conn, calendar_id, external_id, spec: EventSpec) -> None:
        self._check(conn)
        row = self._find(db, conn, external_id)
        if not row:
            raise PermanentProviderError("Event no longer exists in the demo calendar")
        row.start_at, row.end_at, row.title = spec.start, spec.end, spec.title
        if spec.booking_id:
            row.booking_id = spec.booking_id
        db.flush()

    def delete_event(self, db, conn, calendar_id, external_id) -> None:
        self._check(conn)
        row = self._find(db, conn, external_id)
        if row:
            db.delete(row)
            db.flush()

    def get_event(self, db, conn, calendar_id, external_id) -> Optional[dict]:
        self._check(conn)
        row = self._find(db, conn, external_id)
        if not row:
            return None
        return {"start": row.start_at, "end": row.end_at, "cancelled": False}

    def list_events(self, db, conn, calendar_id, start, end) -> list[dict]:
        self._check(conn)
        cal = self._cal(db, conn, calendar_id)
        rows = db.scalars(
            select(ExternalCalendarEvent).where(
                ExternalCalendarEvent.calendar_id == cal.id,
                ExternalCalendarEvent.start_at < end,
                ExternalCalendarEvent.end_at > start,
            )
        ).all()
        return [
            {"external_id": r.external_id, "title": r.title, "start": r.start_at, "end": r.end_at,
             "transparent": r.transparent, "booking_id": r.booking_id}
            for r in rows
        ]


def new_mock_account_id(prefix: str) -> str:
    return f"{prefix}_mock_{secrets.token_hex(6)}"
