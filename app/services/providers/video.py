"""Video conferencing adapters: Zoom (OAuth + REST), Microsoft Teams (Graph onlineMeetings),
Google Meet (created through Google Calendar conferenceData - see google.py).

Video connections are stored as CalendarConnection rows with provider "zoom" (they have no
ConnectedCalendar children and are ignored for availability). A connection whose
sync_state_json has {"mock": true} uses the offline mock that generates realistic fake links.
"""
from __future__ import annotations

import base64
import datetime as dt
from typing import Optional
from urllib.parse import urlencode

import httpx

from app.config import settings
from app.models import utcnow
from app.services.providers.base import (
    AuthError,
    EventSpec,
    TransientProviderError,
    VideoMeeting,
    VideoProvider,
    get_tokens,
    iso_z,
    raise_for_status,
    set_tokens,
)
from app.services.providers.mock import fake_meet_link, fake_teams_link, fake_zoom_meeting

VIDEO_PROVIDERS = {"zoom"}
ZOOM_AUTH = "https://zoom.us/oauth/authorize"
ZOOM_TOKEN = "https://zoom.us/oauth/token"
ZOOM_API = "https://api.zoom.us/v2"


def _request(method, url, **kw) -> httpx.Response:
    try:
        return httpx.request(method, url, timeout=20, **kw)
    except httpx.HTTPError as exc:
        raise TransientProviderError(f"Video provider request failed: {exc}") from exc


def is_mock(conn) -> bool:
    return bool((conn.sync_state_json or {}).get("mock"))


class ZoomProvider(VideoProvider):
    name = "zoom"
    scopes = ["meeting:write", "user:read"]

    def is_configured(self) -> bool:
        return bool(settings.zoom_client_id and settings.zoom_client_secret)

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        return f"{ZOOM_AUTH}?{urlencode({'response_type': 'code', 'client_id': settings.zoom_client_id, 'redirect_uri': redirect_uri, 'state': state})}"

    def _basic(self) -> dict:
        raw = f"{settings.zoom_client_id}:{settings.zoom_client_secret}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}

    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        r = _request("POST", ZOOM_TOKEN, headers=self._basic(),
                     data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri})
        raise_for_status(r)
        d = r.json()
        email = None
        try:
            me = _request("GET", f"{ZOOM_API}/users/me", headers={"Authorization": f"Bearer {d['access_token']}"}).json()
            email = me.get("email")
        except Exception:
            pass
        return {
            "access_token": d["access_token"], "refresh_token": d.get("refresh_token"),
            "expires_at": iso_z(utcnow() + dt.timedelta(seconds=int(d.get("expires_in", 3600)) - 60)),
            "scopes": d.get("scope", ""), "account_email": email,
        }

    def refresh(self, db, conn) -> None:
        tok = get_tokens(conn)
        r = _request("POST", ZOOM_TOKEN, headers=self._basic(),
                     data={"grant_type": "refresh_token", "refresh_token": tok.get("refresh_token", "")})
        if r.status_code in (400, 401):
            raise AuthError("Zoom authorization expired - reconnect Zoom")
        raise_for_status(r)
        d = r.json()
        tok.update(access_token=d["access_token"], refresh_token=d.get("refresh_token") or tok.get("refresh_token"),
                   expires_at=iso_z(utcnow() + dt.timedelta(seconds=int(d.get("expires_in", 3600)) - 60)))
        set_tokens(conn, tok)

    def _call(self, db, conn, method, path, **kw):
        if not conn.expires_at or conn.expires_at <= utcnow():
            self.refresh(db, conn)
        r = _request(method, ZOOM_API + path, headers={"Authorization": f"Bearer {get_tokens(conn)['access_token']}"}, **kw)
        return r

    def _body(self, spec: EventSpec) -> dict:
        return {
            "topic": spec.title[:200], "type": 2, "start_time": iso_z(spec.start),
            "duration": max(1, int((spec.end - spec.start).total_seconds() // 60)), "timezone": "UTC",
            "agenda": (spec.description or "")[:2000], "settings": {"join_before_host": False, "waiting_room": True},
        }

    def create_meeting(self, db, conn, spec: EventSpec) -> VideoMeeting:
        if conn.status != "connected":
            raise AuthError("Zoom connection needs reconnecting")
        if is_mock(conn):
            mid, url = fake_zoom_meeting(f"{conn.id}:{spec.booking_id}")
            return VideoMeeting(mid, url, "zoom")
        r = self._call(db, conn, "POST", "/users/me/meetings", json=self._body(spec))
        raise_for_status(r)
        d = r.json()
        return VideoMeeting(str(d["id"]), d["join_url"], "zoom")

    def update_meeting(self, db, conn, meeting_id, spec: EventSpec) -> None:
        if is_mock(conn):
            return
        r = self._call(db, conn, "PATCH", f"/meetings/{meeting_id}", json=self._body(spec))
        raise_for_status(r)

    def delete_meeting(self, db, conn, meeting_id) -> None:
        if is_mock(conn):
            return
        r = self._call(db, conn, "DELETE", f"/meetings/{meeting_id}")
        if r.status_code == 404:
            return
        raise_for_status(r)

    def revoke(self, db, conn) -> None:
        if is_mock(conn):
            return
        tok = get_tokens(conn)
        try:
            httpx.post("https://zoom.us/oauth/revoke", headers=self._basic(), data={"token": tok.get("access_token", "")}, timeout=10)
        except httpx.HTTPError:
            pass


class TeamsProvider(VideoProvider):
    """Standalone Teams meeting via Graph /me/onlineMeetings (used when the Microsoft connection is
    not the destination calendar). When the write calendar is Microsoft the link is created as part
    of the calendar event instead (isOnlineMeeting)."""

    name = "teams"

    def create_meeting(self, db, conn, spec: EventSpec) -> VideoMeeting:
        if conn.provider == "mock" or is_mock(conn):
            url = fake_teams_link(f"{conn.id}:{spec.booking_id}")
            return VideoMeeting("teams_" + url[-20:-4], url, "teams")
        from app.services.providers.microsoft import MicrosoftCalendarProvider

        r = MicrosoftCalendarProvider()._call(db, conn, "POST", "/me/onlineMeetings", json={
            "startDateTime": iso_z(spec.start), "endDateTime": iso_z(spec.end), "subject": spec.title,
        })
        raise_for_status(r)
        d = r.json()
        return VideoMeeting(d["id"], d["joinWebUrl"], "teams")

    def update_meeting(self, db, conn, meeting_id, spec) -> None:
        if conn.provider == "mock" or is_mock(conn):
            return
        from app.services.providers.microsoft import MicrosoftCalendarProvider

        r = MicrosoftCalendarProvider()._call(db, conn, "PATCH", f"/me/onlineMeetings/{meeting_id}", json={
            "startDateTime": iso_z(spec.start), "endDateTime": iso_z(spec.end), "subject": spec.title,
        })
        raise_for_status(r)

    def delete_meeting(self, db, conn, meeting_id) -> None:
        if conn.provider == "mock" or is_mock(conn):
            return
        from app.services.providers.microsoft import MicrosoftCalendarProvider

        r = MicrosoftCalendarProvider()._call(db, conn, "DELETE", f"/me/onlineMeetings/{meeting_id}")
        if r.status_code not in (404,):
            raise_for_status(r)


def mock_meet_link(seed: str) -> str:
    return fake_meet_link(seed)


def provider_for(name: str) -> Optional[VideoProvider]:
    return {"zoom": ZoomProvider(), "teams": TeamsProvider()}.get(name)
