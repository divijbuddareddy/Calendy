"""Provider adapter interfaces (calendar, video, payments).

Calendar adapters are stateless objects; every call receives the DB session and the
`CalendarConnection` row so adapters can refresh/persist tokens. All datetimes are naive UTC.

Error model:
  * TransientProviderError  - retry later (5xx, timeouts, rate limits)
  * PermanentProviderError  - will not succeed on retry (4xx, invalid request)
  * AuthError (subclass of PermanentProviderError) - token revoked/expired and refresh failed;
    callers mark the connection `error` so the UI shows a reconnect state.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Optional

from app.security import decrypt, encrypt
from app.services.calendar_facade import BusyInterval, PermanentProviderError, TransientProviderError

__all__ = [
    "AuthError", "BusyInterval", "CalendarInfo", "CalendarProvider", "CreatedEvent", "EventSpec",
    "PermanentProviderError", "TransientProviderError", "VideoMeeting", "VideoProvider", "get_tokens",
    "set_tokens", "raise_for_status", "iso_z", "parse_dt",
]


class AuthError(PermanentProviderError):
    """Credentials invalid/expired and could not be refreshed."""


@dataclass
class CalendarInfo:
    provider_calendar_id: str
    display_name: str
    primary: bool = False
    can_write: bool = True


@dataclass
class EventSpec:
    title: str
    start: dt.datetime  # naive UTC
    end: dt.datetime  # naive UTC
    timezone: str = "UTC"
    description: str = ""
    location: str = ""
    attendees: list[str] = field(default_factory=list)
    conference: Optional[str] = None  # google_meet | teams | None
    booking_id: Optional[str] = None


@dataclass
class CreatedEvent:
    external_id: str
    join_url: Optional[str] = None
    conference_id: Optional[str] = None
    conference_provider: Optional[str] = None


@dataclass
class VideoMeeting:
    meeting_id: str
    join_url: str
    provider: str


class CalendarProvider:
    name = "base"
    scopes: list[str] = []
    supports_conference: set[str] = set()

    # --- OAuth
    def is_configured(self) -> bool:
        return False

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        raise NotImplementedError

    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        """Return {"access_token","refresh_token","expires_at"(iso),"account_email","scopes"}."""
        raise NotImplementedError

    def refresh(self, db, conn) -> None:
        """Refresh access token in place (persist via set_tokens)."""

    def revoke(self, db, conn) -> None:
        """Best-effort revoke at provider."""

    # --- calendars
    def list_calendars(self, db, conn) -> list[CalendarInfo]:
        raise NotImplementedError

    def free_busy(self, db, conn, calendar_ids: list[str], start: dt.datetime, end: dt.datetime) -> list[BusyInterval]:
        raise NotImplementedError

    def create_event(self, db, conn, calendar_id: str, spec: EventSpec, idempotency_key: str) -> CreatedEvent:
        raise NotImplementedError

    def update_event(self, db, conn, calendar_id: str, external_id: str, spec: EventSpec) -> None:
        raise NotImplementedError

    def delete_event(self, db, conn, calendar_id: str, external_id: str) -> None:
        raise NotImplementedError

    def get_event(self, db, conn, calendar_id: str, external_id: str) -> Optional[dict]:
        """Return {"start","end","cancelled"} or None if deleted (drift detection)."""
        return None

    def list_events(self, db, conn, calendar_id: str, start: dt.datetime, end: dt.datetime) -> list[dict]:
        """Return [{"external_id","title","start","end","transparent"}] (busy cache sync)."""
        return []

    def watch(self, db, conn, calendar_id: str, callback_url: str, token: str) -> Optional[dict]:
        """Create a push channel / subscription. Return state to persist."""
        return None


class VideoProvider:
    name = "base"

    def is_configured(self) -> bool:
        return False

    def create_meeting(self, db, conn, spec: EventSpec) -> VideoMeeting:
        raise NotImplementedError

    def update_meeting(self, db, conn, meeting_id: str, spec: EventSpec) -> None:
        raise NotImplementedError

    def delete_meeting(self, db, conn, meeting_id: str) -> None:
        raise NotImplementedError


# ------------------------------------------------------------------ helpers
def get_tokens(conn) -> dict:
    raw = decrypt(conn.encrypted_tokens) if conn.encrypted_tokens else None
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def set_tokens(conn, tokens: dict) -> None:
    """Persist OAuth tokens encrypted at rest."""
    conn.encrypted_tokens = encrypt(json.dumps(tokens))
    exp = tokens.get("expires_at")
    if exp:
        conn.expires_at = parse_dt(exp) if isinstance(exp, str) else exp


def iso_z(value: dt.datetime) -> str:
    return value.replace(microsecond=0).isoformat() + "Z"


def parse_dt(value: str) -> dt.datetime:
    """Parse ISO-8601 (with Z/offset or naive=UTC) into naive UTC."""
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    # Graph returns 7 fractional digits
    if "." in v:
        head, _, tail = v.partition(".")
        frac = "".join(ch for ch in tail if ch.isdigit())
        rest = tail[len(frac):]
        v = f"{head}.{frac[:6]}{rest}"
    d = dt.datetime.fromisoformat(v)
    if d.tzinfo:
        d = d.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return d


def raise_for_status(resp) -> None:
    """Map HTTP status codes to provider error classes."""
    if resp.status_code < 400:
        return
    text = resp.text[:500]
    if resp.status_code in (401,):
        raise AuthError(f"HTTP {resp.status_code}: {text}")
    if resp.status_code == 429 or resp.status_code >= 500:
        raise TransientProviderError(f"HTTP {resp.status_code}: {text}")
    raise PermanentProviderError(f"HTTP {resp.status_code}: {text}")
