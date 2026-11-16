"""Google Calendar adapter (OAuth 2.0 + Calendar API v3 via httpx).

Used only when GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are configured. Scopes are the minimum
needed: calendar.events (write booking events) + calendar.readonly (list calendars/free-busy).
Refresh tokens are encrypted at rest (base.set_tokens -> security.encrypt).
"""
from __future__ import annotations

import datetime as dt
import hashlib
from typing import Optional
from urllib.parse import urlencode

import httpx

from app.config import settings
from app.models import utcnow
from app.services.providers.base import (
    AuthError,
    BusyInterval,
    CalendarInfo,
    CalendarProvider,
    CreatedEvent,
    EventSpec,
    TransientProviderError,
    get_tokens,
    iso_z,
    parse_dt,
    raise_for_status,
    set_tokens,
)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://www.googleapis.com/calendar/v3"
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
    "openid",
    "email",
]
TIMEOUT = 20


def _request(method: str, url: str, **kw) -> httpx.Response:
    try:
        return httpx.request(method, url, timeout=TIMEOUT, **kw)
    except httpx.HTTPError as exc:
        raise TransientProviderError(f"Google request failed: {exc}") from exc


class GoogleCalendarProvider(CalendarProvider):
    name = "google"
    scopes = SCOPES
    supports_conference = {"google_meet"}

    def is_configured(self) -> bool:
        return bool(settings.google_client_id and settings.google_client_secret)

    # ----------------------------------------------------------- OAuth
    def authorize_url(self, state: str, redirect_uri: str) -> str:
        q = {
            "client_id": settings.google_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
        return f"{AUTH_URL}?{urlencode(q)}"

    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        r = _request(
            "POST", TOKEN_URL,
            data={
                "code": code, "client_id": settings.google_client_id, "client_secret": settings.google_client_secret,
                "redirect_uri": redirect_uri, "grant_type": "authorization_code",
            },
        )
        raise_for_status(r)
        data = r.json()
        email = None
        if data.get("id_token"):
            try:
                import base64
                import json

                payload = data["id_token"].split(".")[1]
                payload += "=" * (-len(payload) % 4)
                email = json.loads(base64.urlsafe_b64decode(payload)).get("email")
            except Exception:
                email = None
        return {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token"),
            "expires_at": iso_z(utcnow() + dt.timedelta(seconds=int(data.get("expires_in", 3600)) - 60)),
            "scopes": data.get("scope", " ".join(SCOPES)),
            "account_email": email,
        }

    def refresh(self, db, conn) -> None:
        tok = get_tokens(conn)
        if not tok.get("refresh_token"):
            raise AuthError("No refresh token; reconnect Google Calendar")
        r = _request(
            "POST", TOKEN_URL,
            data={
                "client_id": settings.google_client_id, "client_secret": settings.google_client_secret,
                "refresh_token": tok["refresh_token"], "grant_type": "refresh_token",
            },
        )
        if r.status_code in (400, 401):
            raise AuthError("Google refresh token revoked or expired")
        raise_for_status(r)
        data = r.json()
        tok["access_token"] = data["access_token"]
        tok["expires_at"] = iso_z(utcnow() + dt.timedelta(seconds=int(data.get("expires_in", 3600)) - 60))
        if data.get("refresh_token"):  # rotation
            tok["refresh_token"] = data["refresh_token"]
        set_tokens(conn, tok)

    def revoke(self, db, conn) -> None:
        tok = get_tokens(conn)
        t = tok.get("refresh_token") or tok.get("access_token")
        if t:
            try:
                httpx.post(REVOKE_URL, data={"token": t}, timeout=10)
            except httpx.HTTPError:
                pass

    def _headers(self, db, conn) -> dict:
        if not conn.expires_at or conn.expires_at <= utcnow():
            self.refresh(db, conn)
        return {"Authorization": f"Bearer {get_tokens(conn).get('access_token')}"}

    def _call(self, db, conn, method, path, **kw) -> httpx.Response:
        r = _request(method, API + path, headers=self._headers(db, conn), **kw)
        if r.status_code == 401:  # token expired early -> one forced refresh
            self.refresh(db, conn)
            r = _request(method, API + path, headers=self._headers(db, conn), **kw)
        return r

    # ----------------------------------------------------------- calendars
    def list_calendars(self, db, conn) -> list[CalendarInfo]:
        r = self._call(db, conn, "GET", "/users/me/calendarList", params={"minAccessRole": "reader"})
        raise_for_status(r)
        out = []
        for item in r.json().get("items", []):
            out.append(
                CalendarInfo(
                    item["id"], item.get("summaryOverride") or item.get("summary") or item["id"],
                    primary=bool(item.get("primary")), can_write=item.get("accessRole") in ("owner", "writer"),
                )
            )
        return out

    def free_busy(self, db, conn, calendar_ids, start, end) -> list[BusyInterval]:
        if not calendar_ids:
            return []
        body = {"timeMin": iso_z(start), "timeMax": iso_z(end), "items": [{"id": c} for c in calendar_ids]}
        r = self._call(db, conn, "POST", "/freeBusy", json=body)
        raise_for_status(r)
        out = []
        for cal_id, info in r.json().get("calendars", {}).items():
            if info.get("errors"):
                raise TransientProviderError(f"freeBusy error for {cal_id}: {info['errors']}")
            for b in info.get("busy", []):
                out.append(BusyInterval(parse_dt(b["start"]), parse_dt(b["end"]), "Busy", "google"))
        return out

    def _body(self, spec: EventSpec, idempotency_key: Optional[str] = None) -> dict:
        body = {
            "summary": spec.title,
            "description": spec.description,
            "location": spec.location or None,
            "start": {"dateTime": iso_z(spec.start), "timeZone": "UTC"},
            "end": {"dateTime": iso_z(spec.end), "timeZone": "UTC"},
            "attendees": [{"email": e} for e in spec.attendees],
            "extendedProperties": {"private": {"aal_booking_id": spec.booking_id or ""}},
            "reminders": {"useDefault": True},
        }
        if spec.conference == "google_meet" and idempotency_key:
            body["conferenceData"] = {
                "createRequest": {
                    "requestId": hashlib.sha256(idempotency_key.encode()).hexdigest()[:40],
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            }
        return body

    def create_event(self, db, conn, calendar_id, spec: EventSpec, idempotency_key) -> CreatedEvent:
        # Deterministic event id => provider-side idempotency (Google returns 409 on duplicate).
        event_id = "aal" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:40]
        body = self._body(spec, idempotency_key)
        body["id"] = event_id
        r = self._call(
            db, conn, "POST", f"/calendars/{calendar_id}/events",
            params={"conferenceDataVersion": 1, "sendUpdates": "none"}, json=body,
        )
        if r.status_code == 409:
            r = self._call(db, conn, "GET", f"/calendars/{calendar_id}/events/{event_id}")
        raise_for_status(r)
        data = r.json()
        join = conf_id = None
        cd = data.get("conferenceData") or {}
        for ep in cd.get("entryPoints", []):
            if ep.get("entryPointType") == "video":
                join = ep.get("uri")
        conf_id = cd.get("conferenceId")
        return CreatedEvent(data["id"], join or data.get("hangoutLink"), conf_id, "google_meet" if join else None)

    def update_event(self, db, conn, calendar_id, external_id, spec: EventSpec) -> None:
        body = self._body(spec)
        body.pop("attendees", None)
        r = self._call(
            db, conn, "PATCH", f"/calendars/{calendar_id}/events/{external_id}", params={"sendUpdates": "none"}, json=body
        )
        raise_for_status(r)

    def delete_event(self, db, conn, calendar_id, external_id) -> None:
        r = self._call(db, conn, "DELETE", f"/calendars/{calendar_id}/events/{external_id}", params={"sendUpdates": "none"})
        if r.status_code in (404, 410):  # already gone => idempotent success
            return
        raise_for_status(r)

    def get_event(self, db, conn, calendar_id, external_id) -> Optional[dict]:
        r = self._call(db, conn, "GET", f"/calendars/{calendar_id}/events/{external_id}")
        if r.status_code in (404, 410):
            return None
        raise_for_status(r)
        d = r.json()
        if d.get("status") == "cancelled":
            return None
        return {"start": parse_dt(d["start"]["dateTime"]), "end": parse_dt(d["end"]["dateTime"]), "cancelled": False}

    def list_events(self, db, conn, calendar_id, start, end) -> list[dict]:
        out, page = [], None
        while True:
            params = {"timeMin": iso_z(start), "timeMax": iso_z(end), "singleEvents": "true", "maxResults": 250}
            if page:
                params["pageToken"] = page
            r = self._call(db, conn, "GET", f"/calendars/{calendar_id}/events", params=params)
            raise_for_status(r)
            data = r.json()
            for e in data.get("items", []):
                if e.get("status") == "cancelled" or "dateTime" not in e.get("start", {}):
                    continue
                out.append({
                    "external_id": e["id"], "title": e.get("summary") or "Busy",
                    "start": parse_dt(e["start"]["dateTime"]), "end": parse_dt(e["end"]["dateTime"]),
                    "transparent": e.get("transparency") == "transparent",
                    "booking_id": (e.get("extendedProperties", {}).get("private", {}) or {}).get("aal_booking_id") or None,
                })
            page = data.get("nextPageToken")
            if not page:
                return out

    def watch(self, db, conn, calendar_id, callback_url, token) -> Optional[dict]:
        if not callback_url.startswith("https://"):
            return None  # Google requires a public HTTPS endpoint
        import secrets

        channel_id = "aal-" + secrets.token_hex(12)
        r = self._call(
            db, conn, "POST", f"/calendars/{calendar_id}/events/watch",
            json={"id": channel_id, "type": "web_hook", "address": callback_url, "token": token},
        )
        raise_for_status(r)
        d = r.json()
        return {"channel_id": channel_id, "resource_id": d.get("resourceId"), "expiration": d.get("expiration")}
