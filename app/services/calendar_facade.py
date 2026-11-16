"""Calendar facade - the ONLY entry point booking/availability code uses for calendar providers.

CONTRACT (stable; owned by the integrations module, consumed by availability/booking):

    get_busy_intervals(db, user_id, start_utc, end_utc) -> list[BusyInterval]
        Busy intervals from all *conflict* calendars of the user's healthy connections,
        normalised to naive UTC. Transparent (free) events are excluded.
        Raises CalendarUnavailable if any connection the user relies on is in error/revoked
        state (PRD acceptance #35: never show unverified availability as safe).
        A user with zero connections returns [] (internal bookings still apply).

    create_calendar_events(db, booking) -> None
        For each host of the booking, create an event on the host's write calendar using an
        idempotency key, store CalendarEvent rows (external id, join_url, sync_status), and
        when the location type is google_meet/zoom/teams set booking.location_json["join_url"].
        Raises TransientProviderError (retryable) or PermanentProviderError.
        If no connection exists: records a CalendarEvent(provider="none", sync_status="synced")
        so the flow still succeeds (ICS email is the fallback).

    update_calendar_events(db, booking) -> None     (reschedule: move existing events)
    delete_calendar_events(db, booking) -> None     (cancellation)

Callers own the transaction (these functions only add/flush, never commit).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session


class CalendarUnavailable(Exception):
    """Host calendar connection is broken; availability cannot be verified."""


class TransientProviderError(Exception):
    pass


class PermanentProviderError(Exception):
    pass


@dataclass
class BusyInterval:
    start: dt.datetime  # naive UTC
    end: dt.datetime  # naive UTC
    title: str = "Busy"
    source: str = "calendar"


def get_busy_intervals(db: Session, user_id: str, start_utc: dt.datetime, end_utc: dt.datetime) -> list[BusyInterval]:
    from app.services.providers import registry

    return registry.get_busy_intervals(db, user_id, start_utc, end_utc)


def create_calendar_events(db: Session, booking) -> None:
    from app.services.providers import registry

    registry.create_calendar_events(db, booking)


def update_calendar_events(db: Session, booking, previous: Optional[dict] = None) -> None:
    from app.services.providers import registry

    registry.update_calendar_events(db, booking, previous)


def delete_calendar_events(db: Session, booking) -> None:
    from app.services.providers import registry

    registry.delete_calendar_events(db, booking)
