"""Microsoft 365 / Outlook adapter (Microsoft identity platform OAuth + Graph API via httpx).

Used only when MICROSOFT_CLIENT_ID / MICROSOFT_CLIENT_SECRET are configured.
Teams links are created by setting isOnlineMeeting + onlineMeetingProvider=teamsForBusiness.
"""
from __future__ import annotations

import datetime as dt
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

AUTH_BASE = "https://login.microsoftonline.com/common/oauth2/v2.0"
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["offline_access", "User.Read", "Calendars.ReadWrite", "OnlineMeetings.ReadWrite"]
TIMEOUT = 20


def _request(method, url, **kw) -> httpx.Response:
    try:
        return httpx.request(method, url, timeout=TIMEOUT, **kw)
    except httpx.HTTPError as exc:
        raise TransientProviderError(f"Microsoft Graph request failed: {exc}") from exc


class MicrosoftCalendarProvider(CalendarProvider):
    name = "microsoft"
    scopes = SCOPES
    supports_conference = {"teams"}

    def is_configured(self) -> bool:
        return bool(settings.microsoft_client_id and settings.microsoft_client_secret)

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        q = {
            "client_id": settings.microsoft_client_id, "response_type": "code", "redirect_uri": redirect_uri,
            "response_mode": "query", "scope": " ".join(SCOPES), "state": state, "prompt": "select_account",
        }
        return f"{AUTH_BASE}/authorize?{urlencode(q)}"

    def _token(self, data: dict) -> dict:
        r = _request("POST", f"{AUTH_BASE}/token", data={
            "client_id": settings.microsoft_client_id, "client_secret": settings.microsoft_client_secret,
            "scope": " ".join(SCOPES), **data,
        })
        if r.status_code in (400, 401) and data.get("grant_type") == "refresh_token":
            raise AuthError("Microsoft refresh token revoked or expired")
        raise_for_status(r)
        return r.json()

    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        d = self._token({"code": code, "redirect_uri": redirect_uri, "grant_type": "authorization_code"})
        email = None
        try:
            me = _request("GET", f"{GRAPH}/me", headers={"Authorization": f"Bearer {d['access_token']}"}).json()
            email = me.get("mail") or me.get("userPrincipalName")
        except Exception:
            pass
        return {
            "access_token": d["access_token"], "refresh_token": d.get("refresh_token"),
            "expires_at": iso_z(utcnow() + dt.timedelta(seconds=int(d.get("expires_in", 3600)) - 60)),
            "scopes": d.get("scope", " ".join(SCOPES)), "account_email": email,
        }

    def refresh(self, db, conn) -> None:
        tok = get_tokens(conn)
        if not tok.get("refresh_token"):
            raise AuthError("No refresh token; reconnect Microsoft calendar")
        d = self._token({"refresh_token": tok["refresh_token"], "grant_type": "refresh_token"})
        tok["access_token"] = d["access_token"]
        tok["refresh_token"] = d.get("refresh_token") or tok["refresh_token"]
        tok["expires_at"] = iso_z(utcnow() + dt.timedelta(seconds=int(d.get("expires_in", 3600)) - 60))
        set_tokens(conn, tok)

    def _call(self, db, conn, method, path, **kw) -> httpx.Response:
        if not conn.expires_at or conn.expires_at <= utcnow():
            self.refresh(db, conn)
        headers = {"Authorization": f"Bearer {get_tokens(conn).get('access_token')}", "Prefer": 'outlook.timezone="UTC"'}
        headers.update(kw.pop("headers", {}))
        r = _request(method, GRAPH + path, headers=headers, **kw)
        if r.status_code == 401:
            self.refresh(db, conn)
            headers["Authorization"] = f"Bearer {get_tokens(conn).get('access_token')}"
            r = _request(method, GRAPH + path, headers=headers, **kw)
        return r

    def list_calendars(self, db, conn) -> list[CalendarInfo]:
        r = self._call(db, conn, "GET", "/me/calendars")
        raise_for_status(r)
        return [
            CalendarInfo(c["id"], c.get("name", "Calendar"), primary=bool(c.get("isDefaultCalendar")),
                         can_write=bool(c.get("canEdit", True)))
            for c in r.json().get("value", [])
        ]

    def list_events(self, db, conn, calendar_id, start, end) -> list[dict]:
        out = []
        url = f"/me/calendars/{calendar_id}/calendarView"
        params = {"startDateTime": iso_z(start), "endDateTime": iso_z(end), "$top": 200,
                  "$select": "id,subject,start,end,showAs,isCancelled,singleValueExtendedProperties"}
        while url:
            r = self._call(db, conn, "GET", url, params=params)
            raise_for_status(r)
            data = r.json()
            for e in data.get("value", []):
                if e.get("isCancelled"):
                    continue
                out.append({
                    "external_id": e["id"], "title": e.get("subject") or "Busy",
                    "start": parse_dt(e["start"]["dateTime"]), "end": parse_dt(e["end"]["dateTime"]),
                    "transparent": e.get("showAs") in ("free", "workingElsewhere"), "booking_id": None,
                })
            nxt = data.get("@odata.nextLink")
            url, params = (nxt.replace(GRAPH, ""), None) if nxt else (None, None)
        return out

    def free_busy(self, db, conn, calendar_ids, start, end) -> list[BusyInterval]:
        out = []
        for cid in calendar_ids:
            for e in self.list_events(db, conn, cid, start, end):
                if not e["transparent"]:
                    out.append(BusyInterval(e["start"], e["end"], e["title"], "microsoft"))
        return out

    def _body(self, spec: EventSpec) -> dict:
        body = {
            "subject": spec.title,
            "body": {"contentType": "text", "content": spec.description or ""},
            "start": {"dateTime": spec.start.isoformat(), "timeZone": "UTC"},
            "end": {"dateTime": spec.end.isoformat(), "timeZone": "UTC"},
            "attendees": [{"emailAddress": {"address": e}, "type": "required"} for e in spec.attendees],
            "transactionId": None,
        }
        if spec.location:
            body["location"] = {"displayName": spec.location}
        if spec.conference == "teams":
            body["isOnlineMeeting"] = True
            body["onlineMeetingProvider"] = "teamsForBusiness"
        return body

    def create_event(self, db, conn, calendar_id, spec: EventSpec, idempotency_key) -> CreatedEvent:
        body = self._body(spec)
        body["transactionId"] = idempotency_key[:100]  # Graph de-duplicates creates by transactionId
        r = self._call(db, conn, "POST", f"/me/calendars/{calendar_id}/events", json=body)
        raise_for_status(r)
        d = r.json()
        join = ((d.get("onlineMeeting") or {}).get("joinUrl")) or None
        return CreatedEvent(d["id"], join, d.get("iCalUId") if join else None, "teams" if join else None)

    def update_event(self, db, conn, calendar_id, external_id, spec: EventSpec) -> None:
        body = self._body(spec)
        for k in ("attendees", "transactionId", "isOnlineMeeting", "onlineMeetingProvider"):
            body.pop(k, None)
        r = self._call(db, conn, "PATCH", f"/me/events/{external_id}", json=body)
        raise_for_status(r)

    def delete_event(self, db, conn, calendar_id, external_id) -> None:
        r = self._call(db, conn, "DELETE", f"/me/events/{external_id}")
        if r.status_code == 404:
            return
        raise_for_status(r)

    def get_event(self, db, conn, calendar_id, external_id) -> Optional[dict]:
        r = self._call(db, conn, "GET", f"/me/events/{external_id}")
        if r.status_code == 404:
            return None
        raise_for_status(r)
        d = r.json()
        if d.get("isCancelled"):
            return None
        return {"start": parse_dt(d["start"]["dateTime"]), "end": parse_dt(d["end"]["dateTime"]), "cancelled": False}

    def watch(self, db, conn, calendar_id, callback_url, token) -> Optional[dict]:
        if not callback_url.startswith("https://"):
            return None
        exp = utcnow() + dt.timedelta(days=2)
        r = self._call(db, conn, "POST", "/subscriptions", json={
            "changeType": "created,updated,deleted", "notificationUrl": callback_url,
            "resource": f"/me/calendars/{calendar_id}/events", "expirationDateTime": iso_z(exp), "clientState": token,
        })
        raise_for_status(r)
        return {"subscription_id": r.json().get("id"), "expiration": iso_z(exp)}
