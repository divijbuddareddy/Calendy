"""Outbound webhooks (PRD 15.3) + inbound provider webhook inbox processing (PRD 20.4).

Outbound: a "*" outbox subscriber fans each public event out to the workspace's active
subscriptions as WebhookDelivery rows; the `webhook_deliveries` task POSTs
{id, event, created_at, payload} signed with `X-AAL-Signature: t=<ts>,v1=<hmac>` (secret is
stored encrypted) and retries with exponential backoff; exhausted deliveries become "dead".

Inbound: routers persist the raw provider event in ProviderWebhookInbox (deduped by provider
event id) before acknowledging; the `provider_inbox` task processes rows asynchronously with
retries and dead-lettering.
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import json
import logging
import secrets
from typing import Optional
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.events import subscribe
from app.models import (
    Booking,
    CalendarConnection,
    OutboxEvent,
    ProviderWebhookInbox,
    WebhookDelivery,
    WebhookSubscription,
    utcnow,
)
from app.security import decrypt, encrypt, verify_webhook_signature, webhook_signature
from app.worker import periodic

log = logging.getLogger("webhooks")
WEBHOOK_EVENTS = [
    "booking.created", "booking.cancelled", "booking.rescheduled", "booking.no_show", "routing.submitted",
    "payment.succeeded", "payment.failed",
]
MAX_ATTEMPTS = 6
INBOX_MAX_ATTEMPTS = 5
TIMEOUT = 10


# ------------------------------------------------------------------ subscriptions
class InvalidEndpoint(ValueError):
    pass


def validate_endpoint(url: str) -> str:
    url = (url or "").strip()
    p = urlparse(url)
    if p.scheme not in ("https", "http") or not p.hostname:
        raise InvalidEndpoint("Endpoint must be a valid http(s) URL")
    if settings.env == "prod":
        if p.scheme != "https":
            raise InvalidEndpoint("Endpoint must use HTTPS")
        host = p.hostname
        if host in ("localhost",) or host.endswith(".local") or host.endswith(".internal"):
            raise InvalidEndpoint("Endpoint must be publicly reachable")
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise InvalidEndpoint("Endpoint must be publicly reachable")
        except ValueError:
            pass
    return url


def create_subscription(db: Session, workspace_id: str, endpoint: str, events: list[str]) -> tuple[WebhookSubscription, str]:
    events = [e for e in events if e in WEBHOOK_EVENTS or e == "*"]
    if not events:
        raise InvalidEndpoint("Choose at least one event")
    secret = "whsec_" + secrets.token_urlsafe(24)
    sub = WebhookSubscription(workspace_id=workspace_id, endpoint=validate_endpoint(endpoint), events=events,
                              secret_encrypted=encrypt(secret), active=True)
    db.add(sub)
    db.flush()
    return sub, secret


def subscription_json(sub: WebhookSubscription) -> dict:
    return {"id": sub.id, "endpoint": sub.endpoint, "events": sub.events, "active": sub.active,
            "created_at": sub.created_at.isoformat() + "Z" if sub.created_at else None}


# ------------------------------------------------------------------ payloads
def _iso(v):
    return v.replace(microsecond=0).isoformat() + "Z" if isinstance(v, dt.datetime) else v


def booking_payload(db: Session, booking_id: str) -> Optional[dict]:
    b = db.get(Booking, booking_id)
    if not b:
        return None
    try:
        from app.services.booking import booking_to_dict

        d = booking_to_dict(b)
        if d:
            return json.loads(json.dumps(d, default=_iso))
    except Exception:
        pass
    return {
        "id": b.id, "status": b.status, "event_type_id": b.event_type_id, "host_id": b.host_id,
        "start_at": _iso(b.start_at), "end_at": _iso(b.end_at), "timezone": b.timezone,
        "location": b.location_json, "no_show": b.no_show, "payment_status": b.payment_status,
        "cancel_reason": b.cancel_reason, "cancelled_by": b.cancelled_by, "rescheduled_from_id": b.rescheduled_from_id,
        "utm": b.utm_json, "source": b.source,
        "invitees": [{"name": i.name, "email": i.email, "phone": i.phone, "timezone": i.timezone} for i in b.invitees],
        "answers": [{"question": a.label, "answer": a.answer_json} for a in b.answers],
        "created_at": _iso(b.created_at),
    }


def event_payload(db: Session, topic: str, payload: dict) -> dict:
    out = dict(payload or {})
    if out.get("booking_id"):
        bp = booking_payload(db, out["booking_id"])
        if bp:
            out["booking"] = bp
    return json.loads(json.dumps(out, default=_iso))


@subscribe("*")
def fan_out(db: Session, ev: OutboxEvent) -> None:
    if ev.topic not in WEBHOOK_EVENTS or not ev.workspace_id:
        return
    subs = db.scalars(select(WebhookSubscription).where(
        WebhookSubscription.workspace_id == ev.workspace_id, WebhookSubscription.active.is_(True))).all()
    payload = None
    for sub in subs:
        if ev.topic not in (sub.events or []) and "*" not in (sub.events or []):
            continue
        if db.scalar(select(WebhookDelivery.id).where(
                WebhookDelivery.subscription_id == sub.id, WebhookDelivery.event_id == ev.id)):
            continue  # idempotent fan-out
        if payload is None:
            payload = event_payload(db, ev.topic, ev.payload_json)
        db.add(WebhookDelivery(subscription_id=sub.id, event_id=ev.id, topic=ev.topic, payload_json=payload,
                               status="pending", next_retry_at=utcnow()))
    db.flush()


# ------------------------------------------------------------------ delivery
def _http_post(url: str, body: bytes, headers: dict):
    """Indirection so tests can stub the network. Returns (status_code, text)."""
    import httpx

    r = httpx.post(url, content=body, headers=headers, timeout=TIMEOUT, follow_redirects=False)
    return r.status_code, r.text


def envelope(d: WebhookDelivery) -> dict:
    return {"id": d.event_id, "event": d.topic, "created_at": _iso(d.created_at), "payload": d.payload_json}


def signed_request(d: WebhookDelivery, secret: str, timestamp: Optional[int] = None) -> tuple[bytes, dict]:
    body = json.dumps(envelope(d), separators=(",", ":"), sort_keys=True).encode()
    sig, _ = webhook_signature(secret, body, timestamp)
    headers = {
        "Content-Type": "application/json", "User-Agent": f"{settings.app_name}-Webhooks/1.0",
        "X-AAL-Signature": sig, "X-AAL-Event-Id": d.event_id, "X-AAL-Event": d.topic, "X-AAL-Delivery-Id": d.id,
    }
    return body, headers


def backoff(attempts: int) -> dt.timedelta:
    return dt.timedelta(seconds=min(30 * (2 ** (attempts - 1)), 6 * 3600))


def deliver(db: Session, d: WebhookDelivery, now: Optional[dt.datetime] = None) -> str:
    now = now or utcnow()
    sub = db.get(WebhookSubscription, d.subscription_id)
    if not sub:
        d.status = "dead"
        return d.status
    secret = decrypt(sub.secret_encrypted) or ""
    body, headers = signed_request(d, secret)
    d.attempts = (d.attempts or 0) + 1
    try:
        code, text = _http_post(sub.endpoint, body, headers)
        d.response_code, d.response_body = code, (text or "")[:2000]
        ok = 200 <= code < 300
    except Exception as exc:  # noqa: BLE001 - network errors are retried
        d.response_code, d.response_body, ok = None, f"{type(exc).__name__}: {exc}"[:2000], False
    if ok:
        d.status, d.next_retry_at = "delivered", None
    elif d.attempts >= MAX_ATTEMPTS:
        d.status, d.next_retry_at = "dead", None
    else:
        d.status, d.next_retry_at = "failed", now + backoff(d.attempts)
    return d.status


def deliver_due(db: Session, now: Optional[dt.datetime] = None, limit: int = 100) -> int:
    now = now or utcnow()
    rows = db.scalars(select(WebhookDelivery).where(
        WebhookDelivery.status.in_(("pending", "failed")),
        (WebhookDelivery.next_retry_at.is_(None)) | (WebhookDelivery.next_retry_at <= now),
    ).order_by(WebhookDelivery.created_at).limit(limit)).all()
    for d in rows:
        deliver(db, d, now)
        db.commit()
    return len(rows)


@periodic("webhook_deliveries")
def webhook_deliveries_task(db: Session) -> None:
    deliver_due(db)


def retry_delivery(db: Session, d: WebhookDelivery) -> None:
    d.status, d.next_retry_at = "pending", utcnow()
    if d.attempts >= MAX_ATTEMPTS:
        d.attempts = MAX_ATTEMPTS - 1  # one more manual attempt


def send_test_event(db: Session, sub: WebhookSubscription) -> WebhookDelivery:
    from app.security import new_id

    d = WebhookDelivery(subscription_id=sub.id, event_id=new_id("evt_test"), topic="webhook.test",
                        payload_json={"message": "This is a test event from AAL Scheduler", "subscription_id": sub.id},
                        status="pending", next_retry_at=utcnow())
    db.add(d)
    db.flush()
    deliver(db, d)
    return d


def verify_signature(secret: str, body: bytes, header: str, tolerance: int = 300) -> bool:
    """For receivers (and our own inbound endpoints): HMAC + timestamp tolerance (replay window)."""
    return verify_webhook_signature(secret, body, header or "", tolerance)


# ------------------------------------------------------------------ inbound inbox
def persist_inbound(db: Session, provider: str, external_event_id: str, payload: dict) -> tuple[ProviderWebhookInbox, bool]:
    """Persist raw event before ack. Returns (row, created). Duplicate provider ids are ignored."""
    existing = db.scalar(select(ProviderWebhookInbox).where(
        ProviderWebhookInbox.provider == provider, ProviderWebhookInbox.external_event_id == external_event_id))
    if existing:
        return existing, False
    row = ProviderWebhookInbox(provider=provider, external_event_id=external_event_id[:300], payload_json=payload,
                               status="received")
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        row = db.scalar(select(ProviderWebhookInbox).where(
            ProviderWebhookInbox.provider == provider, ProviderWebhookInbox.external_event_id == external_event_id))
        return row, False
    return row, True


def _process_calendar_push(db: Session, row: ProviderWebhookInbox) -> None:
    from app.services.providers.sync import sync_connection

    conn = db.get(CalendarConnection, row.payload_json.get("connection_id") or "")
    if conn:
        sync_connection(db, conn)


HANDLERS = {
    "google": _process_calendar_push,
    "microsoft": _process_calendar_push,
}


def _stripe_handler(db, row):
    from app.services.payments import handle_stripe_event

    handle_stripe_event(db, row.payload_json)


def _paypal_handler(db, row):
    from app.services.payments import handle_paypal_event

    if settings.env == "prod" and not (row.payload_json or {}).get("_verified"):
        raise ValueError("Unverified PayPal event ignored in production")  # dead-lettered, never applied
    handle_paypal_event(db, row.payload_json)


HANDLERS["stripe"] = _stripe_handler
HANDLERS["paypal"] = _paypal_handler


def process_inbox(db: Session, limit: int = 50) -> int:
    rows = db.scalars(select(ProviderWebhookInbox).where(
        ProviderWebhookInbox.status.in_(("received", "failed")), ProviderWebhookInbox.attempts < INBOX_MAX_ATTEMPTS,
    ).order_by(ProviderWebhookInbox.created_at).limit(limit)).all()
    n = 0
    for row in rows:
        if row.status == "failed" and row.attempts:
            # exponential backoff based on attempts (created_at is the only timestamp we have)
            wait = dt.timedelta(seconds=30 * (2 ** (row.attempts - 1)))
            last = (row.payload_json or {}).get("_last_attempt_at")
            if last and dt.datetime.fromisoformat(last) + wait > utcnow():
                continue
        handler = HANDLERS.get(row.provider)
        rid = row.id
        try:
            if handler:
                handler(db, row)
            row.status, row.last_error = "processed", None
            row.attempts += 1
            db.commit()
            n += 1
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            row = db.get(ProviderWebhookInbox, rid)
            row.attempts += 1
            row.last_error = f"{type(exc).__name__}: {exc}"[:2000]
            row.payload_json = {**(row.payload_json or {}), "_last_attempt_at": utcnow().isoformat()}
            row.status = "dead" if row.attempts >= INBOX_MAX_ATTEMPTS else "failed"
            db.commit()
            log.warning("inbound %s event %s failed: %s", row.provider, row.external_event_id, exc)
    return n


@periodic("provider_inbox")
def provider_inbox_task(db: Session) -> None:
    process_inbox(db)
