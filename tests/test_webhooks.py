"""Outbound webhook signing/retries/dead-letter + inbound provider webhook inbox."""
import datetime as dt
import hashlib
import hmac
import json
import time

from sqlalchemy import select

from app.events import process_outbox, publish
from app.models import ProviderWebhookInbox, WebhookDelivery, WebhookSubscription, utcnow
from app.security import decrypt, verify_webhook_signature, webhook_signature
from app.services import webhooks as W


class Recorder:
    def __init__(self, codes):
        self.codes = list(codes)
        self.calls = []

    def __call__(self, url, body, headers):
        self.calls.append((url, body, headers))
        code = self.codes.pop(0) if self.codes else 200
        if code == "timeout":
            raise TimeoutError("timed out")
        return code, "ok" if code < 300 else "error"


def _sub(db, ws, events=None):
    sub, secret = W.create_subscription(db, ws.id, "https://hooks.example.com/aal", events or ["booking.created"])
    db.commit()
    return sub, secret


def test_fanout_signature_and_verification(db, host, monkeypatch):
    user, ws = host
    sub, secret = _sub(db, ws)
    assert sub.secret_encrypted != secret and decrypt(sub.secret_encrypted) == secret  # encrypted at rest
    _sub_other, _ = W.create_subscription(db, ws.id, "https://other.example.com", ["booking.cancelled"])
    publish(db, "booking.created", {"booking_id": "bk_missing", "actor": "invitee"}, workspace_id=ws.id)
    publish(db, "calendar.sync_failed", {"x": 1}, workspace_id=ws.id)  # internal topic: never sent
    db.commit()
    process_outbox(db)
    process_outbox(db)
    deliveries = db.scalars(select(WebhookDelivery)).all()
    assert len(deliveries) == 1 and deliveries[0].subscription_id == sub.id

    rec = Recorder([200])
    monkeypatch.setattr(W, "_http_post", rec)
    W.deliver_due(db)
    d = db.get(WebhookDelivery, deliveries[0].id)
    assert d.status == "delivered" and d.response_code == 200 and d.attempts == 1
    url, body, headers = rec.calls[0]
    assert url == sub.endpoint
    env = json.loads(body)
    assert env["event"] == "booking.created" and env["id"] == d.event_id and env["payload"]["booking_id"] == "bk_missing"
    assert headers["X-AAL-Event-Id"] == d.event_id
    assert verify_webhook_signature(secret, body, headers["X-AAL-Signature"])
    # receiver-side checks: tampered body / wrong secret fail
    assert not verify_webhook_signature(secret, body + b" ", headers["X-AAL-Signature"])
    assert not verify_webhook_signature("whsec_wrong", body, headers["X-AAL-Signature"])
    # nothing left to deliver
    assert W.deliver_due(db) == 0


def test_replay_and_timestamp_rejection():
    secret, body = "whsec_test", b'{"id":"evt_1"}'
    old_header, _ = webhook_signature(secret, body, int(time.time()) - 3600)
    assert not W.verify_signature(secret, body, old_header)  # replayed / stale timestamp
    future_header, _ = webhook_signature(secret, body, int(time.time()) + 3600)
    assert not W.verify_signature(secret, body, future_header)
    fresh, _ = webhook_signature(secret, body)
    assert W.verify_signature(secret, body, fresh)
    assert not W.verify_signature(secret, body, "garbage")
    # a fresh signature cannot be moved to another body
    assert not W.verify_signature(secret, b'{"id":"evt_2"}', fresh)


def test_retries_with_backoff_then_dead_letter(db, host, monkeypatch):
    user, ws = host
    _sub(db, ws)
    publish(db, "booking.created", {"booking_id": "bk_1"}, workspace_id=ws.id)
    db.commit()
    process_outbox(db)
    rec = Recorder([500, "timeout"] + [503] * 10)
    monkeypatch.setattr(W, "_http_post", rec)
    d = db.scalar(select(WebhookDelivery))
    now = utcnow()
    W.deliver(db, d, now)
    db.commit()
    assert d.status == "failed" and d.attempts == 1 and d.next_retry_at == now + dt.timedelta(seconds=30)
    assert W.deliver_due(db, now) == 0  # not due yet
    W.deliver(db, d, now)
    assert d.status == "failed" and d.response_code is None and "timed out" in d.response_body
    assert d.next_retry_at == now + dt.timedelta(seconds=60)
    for _ in range(W.MAX_ATTEMPTS):
        if d.status == "dead":
            break
        W.deliver(db, d, now)
    assert d.status == "dead" and d.attempts == W.MAX_ATTEMPTS and d.next_retry_at is None
    db.commit()
    assert W.deliver_due(db, now + dt.timedelta(days=2)) == 0  # dead letters are not retried automatically
    # manual retry
    rec.codes = [200]
    W.retry_delivery(db, d)
    W.deliver(db, d)
    assert d.status == "delivered"


def test_webhook_ui(db, host, client, login, monkeypatch):
    user, ws = host
    login(client, user)
    assert client.get("/app/integrations/webhooks").status_code == 200
    r = client.post("/app/integrations/webhooks", data={"endpoint": "https://hooks.example.com/x",
                                                       "events": ["booking.created", "payment.succeeded"]},
                    follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/app/integrations/webhooks")
    assert "whsec_" in page.text  # shown once
    assert "whsec_" not in client.get("/app/integrations/webhooks").text
    sub = db.scalar(select(WebhookSubscription))
    rec = Recorder([200])
    monkeypatch.setattr(W, "_http_post", rec)
    client.post(f"/app/integrations/webhooks/{sub.id}/test", follow_redirects=False)
    db.commit()
    assert db.scalar(select(WebhookDelivery).where(WebhookDelivery.topic == "webhook.test")).status == "delivered"
    assert client.post("/app/integrations/webhooks", data={"endpoint": "ftp://bad", "events": ["booking.created"]},
                       follow_redirects=False).status_code == 303
    assert len(db.scalars(select(WebhookSubscription)).all()) == 1


def _stripe_sig(secret, body, ts=None):
    ts = ts or int(time.time())
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def test_stripe_inbound_signature_dedupe_and_async_processing(db, client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_stripe_test")
    body = json.dumps({"id": "evt_123", "type": "checkout.session.completed",
                       "data": {"object": {"id": "cs_unknown", "metadata": {}}}}).encode()
    bad = client.post("/webhooks/stripe", content=body, headers={"Stripe-Signature": "t=1,v1=deadbeef"})
    assert bad.status_code == 400
    stale = client.post("/webhooks/stripe", content=body,
                        headers={"Stripe-Signature": _stripe_sig("whsec_stripe_test", body, int(time.time()) - 3600)})
    assert stale.status_code == 400
    ok = client.post("/webhooks/stripe", content=body, headers={"Stripe-Signature": _stripe_sig("whsec_stripe_test", body)})
    assert ok.status_code == 200 and ok.json() == {"received": True, "duplicate": False}
    again = client.post("/webhooks/stripe", content=body, headers={"Stripe-Signature": _stripe_sig("whsec_stripe_test", body)})
    assert again.json()["duplicate"] is True
    rows = db.scalars(select(ProviderWebhookInbox)).all()
    assert len(rows) == 1 and rows[0].status == "received"  # persisted before processing
    assert W.process_inbox(db) == 1
    db.commit(); db.expire_all()
    assert db.scalar(select(ProviderWebhookInbox)).status == "processed"


def test_inbox_retries_then_dead(db, monkeypatch):
    calls = []

    def failing(db_, row):
        calls.append(row.id)
        raise RuntimeError("downstream unavailable")

    monkeypatch.setitem(W.HANDLERS, "stripe", failing)
    row, created = W.persist_inbound(db, "stripe", "evt_fail", {"id": "evt_fail"})
    db.commit()
    assert created
    W.process_inbox(db)
    db.commit(); db.expire_all()
    row = db.scalar(select(ProviderWebhookInbox))
    assert row.status == "failed" and row.attempts == 1 and "downstream" in row.last_error
    W.process_inbox(db)  # backoff: not retried immediately
    assert len(calls) == 1
    for _ in range(W.INBOX_MAX_ATTEMPTS):
        row = db.scalar(select(ProviderWebhookInbox))
        row.payload_json = {**row.payload_json, "_last_attempt_at": "2000-01-01T00:00:00"}
        db.commit()
        W.process_inbox(db)
        db.commit(); db.expire_all()
    row = db.scalar(select(ProviderWebhookInbox))
    assert row.status == "dead" and row.attempts == W.INBOX_MAX_ATTEMPTS


def test_google_push_requires_channel_token(db, host, client):
    from app.services.providers.sync import channel_token, connect_mock_calendar

    user, ws = host
    conn = connect_mock_calendar(db, user, ws.id)
    conn.provider = "google"
    conn.sync_state_json = {"watch_channel_id": "chan-1"}
    db.commit()
    hdr = {"X-Goog-Channel-ID": "chan-1", "X-Goog-Resource-State": "exists", "X-Goog-Message-Number": "7"}
    assert client.post("/webhooks/google", headers={**hdr, "X-Goog-Channel-Token": "nope"}).status_code == 403
    ok = client.post("/webhooks/google", headers={**hdr, "X-Goog-Channel-Token": channel_token(conn.id)})
    assert ok.status_code == 200
    client.post("/webhooks/google", headers={**hdr, "X-Goog-Channel-Token": channel_token(conn.id)})
    db.commit()
    assert len(db.scalars(select(ProviderWebhookInbox).where(ProviderWebhookInbox.provider == "google")).all()) == 1
    r = client.post("/webhooks/microsoft?validationToken=abc123")
    assert r.status_code == 200 and r.text == "abc123"


def test_paypal_webhook_verification(db, client, monkeypatch):
    from app.config import settings
    from app.services.ext_settings import ext_settings
    from app.services.providers import payments_paypal as PP

    event = {"id": "WH-1", "event_type": "PAYMENT.CAPTURE.COMPLETED", "resource": {"id": "cap_1"}}
    hdrs = {"PayPal-Transmission-Id": "t1", "PayPal-Transmission-Time": "2026-11-16T00:00:00Z",
            "PayPal-Cert-Url": "https://api.paypal.com/cert", "PayPal-Auth-Algo": "SHA256withRSA",
            "PayPal-Transmission-Sig": "sig"}
    # dev mock mode: accepted but marked unverified
    r = client.post("/webhooks/paypal", json=event, headers=hdrs)
    assert r.status_code == 200
    row = db.scalar(select(ProviderWebhookInbox).where(ProviderWebhookInbox.external_event_id == "WH-1"))
    assert row.payload_json["_verified"] is False
    # prod without verification configured: rejected, nothing persisted
    monkeypatch.setattr(settings, "env", "prod")
    r = client.post("/webhooks/paypal", json=dict(event, id="WH-2"), headers=hdrs)
    assert r.status_code == 503
    assert not db.scalar(select(ProviderWebhookInbox).where(ProviderWebhookInbox.external_event_id == "WH-2"))
    # an unverified row that slipped in is never applied in prod
    assert W.process_inbox(db) == 0
    db.expire_all()
    assert db.get(ProviderWebhookInbox, row.id).status in ("failed", "dead")
    # configured: server-to-server verification decides
    monkeypatch.setattr(settings, "paypal_client_id", "cid")
    monkeypatch.setattr(settings, "paypal_secret", "sec")
    monkeypatch.setattr(ext_settings(), "paypal_webhook_id", "WHID")
    sent = {}

    def fake_req(method, path, *, json=None, idem=None):
        sent.update(path=path, body=json)
        return {"verification_status": "SUCCESS" if json["transmission_sig"] == "good" else "FAILURE"}

    monkeypatch.setattr(PP, "_req", fake_req)
    r = client.post("/webhooks/paypal", json=dict(event, id="WH-3"), headers=hdrs)
    assert r.status_code == 400 and sent["path"] == "/v1/notifications/verify-webhook-signature"
    assert sent["body"]["webhook_id"] == "WHID" and sent["body"]["webhook_event"]["id"] == "WH-3"
    r = client.post("/webhooks/paypal", json=dict(event, id="WH-4"), headers=dict(hdrs, **{"PayPal-Transmission-Sig": "good"}))
    assert r.status_code == 200
    db.commit()
    ok = db.scalar(select(ProviderWebhookInbox).where(ProviderWebhookInbox.external_event_id == "WH-4"))
    assert ok.payload_json["_verified"] is True
    # missing headers -> rejected without calling PayPal
    sent.clear()
    assert client.post("/webhooks/paypal", json=dict(event, id="WH-5")).status_code == 400 and not sent

    def boom(*a, **k):
        raise PP.TransientProviderError("down")

    monkeypatch.setattr(PP, "_req", boom)
    assert client.post("/webhooks/paypal", json=dict(event, id="WH-6"), headers=hdrs).status_code == 503
