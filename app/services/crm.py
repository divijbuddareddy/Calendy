"""CRM integration (PRD 2 "CRM lookup", 10.5 "Salesforce lookup", 11.2 "notify CRM integration").

Adapters (one connection per workspace, `CrmConnection`):
  * MockCrm        - records stored locally in `crm_records`, editable at /app/contacts/crm; always available.
  * SalesforceCrm  - OAuth 2.0 web-server flow + REST/SOQL. Enabled when SALESFORCE_CLIENT_ID (+ SECRET) is set.
                     Owner lookup: Contact by email, then unconverted Lead by email -> Owner.Email.
                     Activities: Task sObject (WhoId = the Contact/Lead when found).
  * HubSpotCrm     - OAuth 2.0 (HUBSPOT_CLIENT_ID/SECRET) or a private-app token (HUBSPOT_ACCESS_TOKEN).
                     Owner lookup: contacts search by email -> hubspot_owner_id -> /crm/v3/owners/{id}.
                     Activities: Note engagement associated with the contact.

All outbound HTTP goes through `http_request()` so tests can monkeypatch it; nothing external is
called unless a real provider is connected.

Public functions:
  get_connection(db, ws_id) / get_adapter(db, ws_id) -> Optional[CrmAdapter]
  lookup_owner(db, ws_id, email) -> Optional[dict(owner_email, record_type, record_id, provider)]
  push_activity_for_event(db, ev)  (outbox subscriber for booking.created & routing.submitted)
  retry_failed(db)                 (@periodic "crm_retry")
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Optional
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.events import subscribe
from app.models import Booking, EventType, OutboxEvent, RoutingForm, RoutingSubmission, User, utcnow
from app.models_crm import CrmActivity, CrmConnection, CrmRecord, RoutingPrefillMap  # noqa: F401 (register tables)
from app.security import decrypt, encrypt

log = logging.getLogger("crm")
PROVIDERS = ("mock", "salesforce", "hubspot")
PROVIDER_LABELS = {"mock": "Built-in demo CRM", "salesforce": "Salesforce", "hubspot": "HubSpot"}
SF_API = "v59.0"
MAX_ATTEMPTS = 5


class CrmError(Exception):
    pass


def _env(name: str) -> str:
    return (os.environ.get(name) or getattr(settings, name.lower(), "") or "").strip()


def provider_available(provider: str) -> bool:
    if provider == "mock":
        return True
    if provider == "salesforce":
        return bool(_env("SALESFORCE_CLIENT_ID") and _env("SALESFORCE_CLIENT_SECRET"))
    if provider == "hubspot":
        return bool(_env("HUBSPOT_ACCESS_TOKEN") or (_env("HUBSPOT_CLIENT_ID") and _env("HUBSPOT_CLIENT_SECRET")))
    return False


def http_request(method: str, url: str, **kwargs):
    """Single HTTP choke point (monkeypatched in tests). Returns an httpx.Response."""
    import httpx

    return httpx.request(method, url, timeout=20, **kwargs)


def callback_url(provider: str) -> str:
    return f"{settings.base_url.rstrip('/')}/app/contacts/crm/{provider}/callback"


# ---------------------------------------------------------------------------------- adapters
class CrmAdapter:
    provider = "base"

    def lookup_owner(self, email: str) -> Optional[dict]:
        raise NotImplementedError

    def push_activity(self, subject: str, body: str, email: Optional[str]) -> Optional[str]:
        raise NotImplementedError


class MockCrm(CrmAdapter):
    provider = "mock"

    def __init__(self, db: Session, workspace_id: str):
        self.db, self.ws = db, workspace_id

    def _record(self, email: str) -> Optional[CrmRecord]:
        e = (email or "").strip().lower()
        rows = self.db.scalars(select(CrmRecord).where(CrmRecord.workspace_id == self.ws,
                                                       CrmRecord.email_normalized == e)).all()
        # Contacts win over leads (same precedence as the Salesforce adapter)
        return sorted(rows, key=lambda r: 0 if r.object_type == "contact" else 1)[0] if rows else None

    def lookup_owner(self, email: str) -> Optional[dict]:
        r = self._record(email)
        if not r or not r.owner_email:
            return None
        return {"owner_email": r.owner_email.lower(), "record_type": r.object_type, "record_id": r.id,
                "provider": self.provider}

    def push_activity(self, subject: str, body: str, email: Optional[str]) -> Optional[str]:
        # The activity row itself (CrmActivity) is the mock CRM's activity timeline.
        r = self._record(email) if email else None
        return f"mock:{r.id}" if r else "mock:unlinked"


class SalesforceCrm(CrmAdapter):
    provider = "salesforce"

    def __init__(self, db: Session, conn: CrmConnection):
        self.db, self.conn = db, conn

    # -- OAuth
    @staticmethod
    def authorize_url(state: str) -> str:
        base = _env("SALESFORCE_LOGIN_URL") or "https://login.salesforce.com"
        return f"{base}/services/oauth2/authorize?" + urlencode({
            "response_type": "code", "client_id": _env("SALESFORCE_CLIENT_ID"), "redirect_uri": callback_url("salesforce"),
            "scope": "api refresh_token", "state": state,
        })

    @staticmethod
    def exchange_code(code: str) -> dict:
        base = _env("SALESFORCE_LOGIN_URL") or "https://login.salesforce.com"
        r = http_request("POST", f"{base}/services/oauth2/token", data={
            "grant_type": "authorization_code", "code": code, "client_id": _env("SALESFORCE_CLIENT_ID"),
            "client_secret": _env("SALESFORCE_CLIENT_SECRET"), "redirect_uri": callback_url("salesforce"),
        })
        if r.status_code != 200:
            raise CrmError(f"Salesforce token exchange failed ({r.status_code})")
        data = r.json()
        return {"access_token": data["access_token"], "refresh_token": data.get("refresh_token"),
                "instance_url": data["instance_url"], "label": data.get("id")}

    def _refresh(self) -> None:
        rt = decrypt(self.conn.refresh_token_enc)
        if not rt:
            raise CrmError("Salesforce session expired; reconnect the CRM")
        base = _env("SALESFORCE_LOGIN_URL") or "https://login.salesforce.com"
        r = http_request("POST", f"{base}/services/oauth2/token", data={
            "grant_type": "refresh_token", "refresh_token": rt, "client_id": _env("SALESFORCE_CLIENT_ID"),
            "client_secret": _env("SALESFORCE_CLIENT_SECRET"),
        })
        if r.status_code != 200:
            raise CrmError(f"Salesforce token refresh failed ({r.status_code})")
        data = r.json()
        self.conn.access_token_enc = encrypt(data["access_token"])
        if data.get("instance_url"):
            self.conn.instance_url = data["instance_url"]

    def _call(self, method: str, path: str, **kw):
        for attempt in (1, 2):
            headers = {"Authorization": f"Bearer {decrypt(self.conn.access_token_enc) or ''}"}
            r = http_request(method, f"{self.conn.instance_url.rstrip('/')}{path}", headers=headers, **kw)
            if r.status_code == 401 and attempt == 1:
                self._refresh()
                continue
            if r.status_code >= 400:
                raise CrmError(f"Salesforce API {method} {path.split('?')[0]} failed ({r.status_code})")
            return r
        raise CrmError("Salesforce authentication failed")

    @staticmethod
    def _soql_str(v: str) -> str:
        return v.replace("\\", "\\\\").replace("'", "\\'")

    def _find(self, email: str) -> Optional[dict]:
        e = self._soql_str(email.strip().lower())
        for obj, extra in (("Contact", ""), ("Lead", " AND IsConverted = false")):
            q = f"SELECT Id, Owner.Email FROM {obj} WHERE Email = '{e}'{extra} ORDER BY LastModifiedDate DESC LIMIT 1"
            r = self._call("GET", f"/services/data/{SF_API}/query?" + urlencode({"q": q}))
            recs = (r.json() or {}).get("records") or []
            if recs:
                rec = recs[0]
                return {"record_type": obj.lower(), "record_id": rec.get("Id"),
                        "owner_email": ((rec.get("Owner") or {}).get("Email") or "").lower() or None}
        return None

    def lookup_owner(self, email: str) -> Optional[dict]:
        rec = self._find(email)
        if not rec or not rec["owner_email"]:
            return None
        return {**rec, "provider": self.provider}

    def push_activity(self, subject: str, body: str, email: Optional[str]) -> Optional[str]:
        rec = self._find(email) if email else None
        task = {"Subject": subject[:255], "Description": body[:32000], "Status": "Completed",
                "ActivityDate": utcnow().date().isoformat()}
        if rec:
            task["WhoId"] = rec["record_id"]
        r = self._call("POST", f"/services/data/{SF_API}/sobjects/Task", json=task)
        return (r.json() or {}).get("id")


class HubSpotCrm(CrmAdapter):
    provider = "hubspot"
    API = "https://api.hubapi.com"

    def __init__(self, db: Session, conn: CrmConnection):
        self.db, self.conn = db, conn

    @staticmethod
    def authorize_url(state: str) -> str:
        return "https://app.hubspot.com/oauth/authorize?" + urlencode({
            "client_id": _env("HUBSPOT_CLIENT_ID"), "redirect_uri": callback_url("hubspot"), "state": state,
            "scope": "crm.objects.contacts.read crm.objects.contacts.write crm.objects.owners.read",
        })

    @staticmethod
    def exchange_code(code: str) -> dict:
        r = http_request("POST", f"{HubSpotCrm.API}/oauth/v1/token", data={
            "grant_type": "authorization_code", "code": code, "client_id": _env("HUBSPOT_CLIENT_ID"),
            "client_secret": _env("HUBSPOT_CLIENT_SECRET"), "redirect_uri": callback_url("hubspot"),
        })
        if r.status_code != 200:
            raise CrmError(f"HubSpot token exchange failed ({r.status_code})")
        data = r.json()
        return {"access_token": data["access_token"], "refresh_token": data.get("refresh_token"),
                "instance_url": None, "label": "HubSpot portal"}

    def _token(self) -> str:
        return decrypt(self.conn.access_token_enc) or _env("HUBSPOT_ACCESS_TOKEN")

    def _refresh(self) -> None:
        rt = decrypt(self.conn.refresh_token_enc)
        if not rt:
            raise CrmError("HubSpot token expired; reconnect the CRM")
        r = http_request("POST", f"{self.API}/oauth/v1/token", data={
            "grant_type": "refresh_token", "refresh_token": rt, "client_id": _env("HUBSPOT_CLIENT_ID"),
            "client_secret": _env("HUBSPOT_CLIENT_SECRET"),
        })
        if r.status_code != 200:
            raise CrmError(f"HubSpot token refresh failed ({r.status_code})")
        self.conn.access_token_enc = encrypt(r.json()["access_token"])

    def _call(self, method: str, path: str, **kw):
        for attempt in (1, 2):
            r = http_request(method, self.API + path, headers={"Authorization": f"Bearer {self._token()}"}, **kw)
            if r.status_code == 401 and attempt == 1 and self.conn.refresh_token_enc:
                self._refresh()
                continue
            if r.status_code >= 400:
                raise CrmError(f"HubSpot API {method} {path} failed ({r.status_code})")
            return r
        raise CrmError("HubSpot authentication failed")

    def _contact(self, email: str) -> Optional[dict]:
        r = self._call("POST", "/crm/v3/objects/contacts/search", json={
            "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email.strip().lower()}]}],
            "properties": ["email", "hubspot_owner_id"], "limit": 1,
        })
        res = (r.json() or {}).get("results") or []
        return res[0] if res else None

    def lookup_owner(self, email: str) -> Optional[dict]:
        c = self._contact(email)
        owner_id = ((c or {}).get("properties") or {}).get("hubspot_owner_id")
        if not owner_id:
            return None
        o = self._call("GET", f"/crm/v3/owners/{owner_id}").json() or {}
        if not o.get("email"):
            return None
        return {"owner_email": o["email"].lower(), "record_type": "contact", "record_id": c.get("id"),
                "provider": self.provider}

    def push_activity(self, subject: str, body: str, email: Optional[str]) -> Optional[str]:
        c = self._contact(email) if email else None
        note = {"properties": {"hs_timestamp": utcnow().isoformat() + "Z", "hs_note_body": f"<b>{subject}</b><br>{body}"}}
        if c:
            note["associations"] = [{"to": {"id": c["id"]}, "types": [
                {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}]}]
        r = self._call("POST", "/crm/v3/objects/notes", json=note)
        return (r.json() or {}).get("id")


# ---------------------------------------------------------------------------------- connection helpers
def get_connection(db: Session, workspace_id: str) -> Optional[CrmConnection]:
    return db.scalar(select(CrmConnection).where(CrmConnection.workspace_id == workspace_id))


def adapter_for(db: Session, conn: Optional[CrmConnection]) -> Optional[CrmAdapter]:
    if not conn:
        return None
    if conn.provider == "mock":
        return MockCrm(db, conn.workspace_id)
    if conn.provider == "salesforce":
        return SalesforceCrm(db, conn)
    if conn.provider == "hubspot":
        return HubSpotCrm(db, conn)
    return None


def get_adapter(db: Session, workspace_id: str) -> Optional[CrmAdapter]:
    return adapter_for(db, get_connection(db, workspace_id))


def connect(db: Session, workspace_id: str, provider: str, *, user_id: Optional[str] = None,
            access_token: Optional[str] = None, refresh_token: Optional[str] = None,
            instance_url: Optional[str] = None, label: Optional[str] = None) -> CrmConnection:
    conn = get_connection(db, workspace_id)
    if not conn:
        conn = CrmConnection(workspace_id=workspace_id, provider=provider)
        db.add(conn)
    conn.provider = provider
    conn.status = "connected"
    conn.access_token_enc = encrypt(access_token) if access_token else None
    conn.refresh_token_enc = encrypt(refresh_token) if refresh_token else None
    conn.instance_url = instance_url
    conn.account_label = label or PROVIDER_LABELS.get(provider)
    conn.settings_json = conn.settings_json or {"push_bookings": True, "push_routing": True}
    conn.connected_by = user_id
    conn.last_error = None
    db.flush()
    return conn


def disconnect(db: Session, workspace_id: str) -> bool:
    conn = get_connection(db, workspace_id)
    if not conn:
        return False
    db.delete(conn)
    db.flush()
    return True


def lookup_owner(db: Session, workspace_id: str, email: Optional[str]) -> Optional[dict]:
    """CRM record-owner lookup by invitee email. Never raises: failures are logged and return None."""
    if not email or "@" not in email:
        return None
    conn = get_connection(db, workspace_id)
    adapter = adapter_for(db, conn)
    if not adapter:
        return None
    try:
        return adapter.lookup_owner(email)
    except Exception as exc:  # CRM outage must never break routing -> fallback destination
        log.warning("CRM lookup failed for workspace %s: %s", workspace_id, exc)
        conn.last_error = str(exc)[:500]
        return None


# ---------------------------------------------------------------------------------- activity push (outbox)
def _booking_activity(db: Session, ev: OutboxEvent) -> Optional[tuple[str, str, Optional[str], str]]:
    b = db.get(Booking, (ev.payload_json or {}).get("booking_id") or "")
    if not b:
        return None
    et = db.get(EventType, b.event_type_id)
    host = db.get(User, b.host_id)
    inv = b.invitees[0] if b.invitees else None
    subject = f"Meeting booked: {et.name if et else 'Meeting'}"
    lines = [
        f"When: {b.start_at.isoformat(timespec='minutes')} - {b.end_at.isoformat(timespec='minutes')} UTC",
        f"Host: {host.name if host else ''} <{host.email if host else ''}>",
    ]
    if inv:
        lines.append(f"Invitee: {inv.name} <{inv.email}>" + (f" ({inv.company})" if inv.company else ""))
    for a in b.answers:
        lines.append(f"{a.label}: {a.answer_json}")
    if b.utm_json:
        lines.append("UTM: " + ", ".join(f"{k}={v}" for k, v in b.utm_json.items()))
    return subject, "\n".join(lines), (inv.email if inv else None), b.workspace_id


def submission_email(form: Optional[RoutingForm], answers: dict, mapping: Optional[dict] = None) -> Optional[str]:
    """Email captured by a routing submission: the explicitly mapped email field, else the first email field,
    else any answer that looks like an email."""
    answers = answers or {}
    for fid, target in (mapping or {}).items():
        v = answers.get(fid)
        if target == "email" and isinstance(v, str) and "@" in v:
            return v.strip().lower()
    for f in (form.form_schema_json if form else []) or []:
        v = answers.get(f.get("id"))
        if f.get("type") == "email" and isinstance(v, str) and "@" in v:
            return v.strip().lower()
    for v in answers.values():
        if isinstance(v, str) and "@" in v and "." in v.split("@")[-1]:
            return v.strip().lower()
    return None


def prefill_map(db: Session, form_id: str) -> dict:
    row = db.scalar(select(RoutingPrefillMap).where(RoutingPrefillMap.form_id == form_id))
    return dict(row.mapping_json or {}) if row else {}


def _routing_activity(db: Session, ev: OutboxEvent) -> Optional[tuple[str, str, Optional[str], str]]:
    sub = db.get(RoutingSubmission, (ev.payload_json or {}).get("submission_id") or "")
    if not sub:
        return None
    form = db.get(RoutingForm, sub.form_id)
    labels = {f["id"]: f.get("label") or f["id"] for f in (form.form_schema_json if form else []) or []}
    lines = [f"{labels.get(k, k)}: {', '.join(map(str, v)) if isinstance(v, list) else v}"
             for k, v in (sub.answers_json or {}).items()]
    dest = sub.destination_json or {}
    lines.append(f"Routed to: {dest.get('type')} {dest.get('value') or ''}".strip())
    if dest.get("crm_owner"):
        lines.append(f"CRM owner: {dest['crm_owner']}")
    email = submission_email(form, sub.answers_json, prefill_map(db, sub.form_id))
    return f"Routing form submitted: {form.name if form else 'form'}", "\n".join(lines), email, sub.workspace_id


def _send(db: Session, act: CrmActivity, adapter: CrmAdapter) -> None:
    act.attempts += 1
    try:
        act.external_id = adapter.push_activity(act.subject, act.body or "", act.email)
        act.status, act.sent_at, act.error = "sent", utcnow(), None
    except Exception as exc:
        act.status, act.error = "failed", str(exc)[:1000]
        log.warning("CRM activity push failed: %s", exc)


@subscribe("booking.created")
@subscribe("routing.submitted")
def push_activity_for_event(db: Session, ev: OutboxEvent) -> None:
    """Push booking.created / routing.submitted to the workspace's connected CRM. Idempotent per outbox event;
    failures are recorded on the CrmActivity row (retried by the crm_retry periodic task), never raised, so a CRM
    outage cannot block the other outbox subscribers."""
    builder = _booking_activity if ev.topic == "booking.created" else _routing_activity
    conn_ws = ev.workspace_id
    built = None
    if not conn_ws:
        built = builder(db, ev)
        if not built:
            return
        conn_ws = built[3]
    conn = get_connection(db, conn_ws)
    if not conn:
        return
    flags = conn.settings_json or {}
    if (ev.topic == "booking.created" and flags.get("push_bookings") is False) or (
            ev.topic == "routing.submitted" and flags.get("push_routing") is False):
        return
    existing = db.scalar(select(CrmActivity).where(CrmActivity.workspace_id == conn_ws,
                                                   CrmActivity.source_event_id == ev.id))
    if existing and existing.status in ("sent", "skipped"):
        return
    built = built or builder(db, ev)
    if not built:
        return
    subject, body, email, ws_id = built
    if ws_id != conn_ws:  # tenant guard
        return
    act = existing or CrmActivity(workspace_id=ws_id, provider=conn.provider, topic=ev.topic, source_event_id=ev.id,
                                  subject=subject[:300], body=body, email=email, status="pending", attempts=0)
    if not existing:
        try:
            with db.begin_nested():
                db.add(act)
                db.flush()
        except IntegrityError:
            return  # a concurrent worker already recorded it
    _send(db, act, adapter_for(db, conn))
    db.flush()


def retry_activity(db: Session, act: CrmActivity) -> bool:
    adapter = get_adapter(db, act.workspace_id)
    if not adapter:
        return False
    _send(db, act, adapter)
    db.flush()
    return act.status == "sent"


def retry_failed(db: Session) -> int:
    since = utcnow() - dt.timedelta(days=2)
    n = 0
    for act in db.scalars(select(CrmActivity).where(CrmActivity.status == "failed", CrmActivity.attempts < MAX_ATTEMPTS,
                                                    CrmActivity.created_at >= since).limit(50)).all():
        n += int(retry_activity(db, act))
    return n


try:
    from app.worker import periodic

    @periodic("crm_retry")
    def _crm_retry_task(db: Session) -> None:
        retry_failed(db)
except Exception:  # pragma: no cover
    pass


def delete_email_data(db: Session, workspace_id: str, email: str) -> dict:
    """Org data-deletion hook: anonymise CRM/follow-up rows for one email in a workspace. Caller commits."""
    from app.models_crm import ContactFollowUp

    e = (email or "").strip().lower()
    counts = {"crm_records": 0, "crm_activities": 0, "follow_ups": 0}
    for r in db.scalars(select(CrmRecord).where(CrmRecord.workspace_id == workspace_id,
                                                CrmRecord.email_normalized == e)).all():
        db.delete(r)
        counts["crm_records"] += 1
    for a in db.scalars(select(CrmActivity).where(CrmActivity.workspace_id == workspace_id,
                                                  CrmActivity.email == e)).all():
        a.email, a.body = None, "[deleted]"
        counts["crm_activities"] += 1
    for f in db.scalars(select(ContactFollowUp).where(ContactFollowUp.workspace_id == workspace_id,
                                                      ContactFollowUp.email_normalized == e)).all():
        f.email_normalized = f"deleted+{f.id}@redacted.invalid"
        counts["follow_ups"] += 1
    db.flush()
    return counts
