"""Inbound provider webhooks (PRD 20.4): verify signature / channel token, persist raw event in
ProviderWebhookInbox (deduplicated by provider event id) BEFORE acknowledging, then process
asynchronously (provider_inbox periodic task, retries + dead-letter)."""
import base64
import hashlib
import hmac
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import IS_TEST, settings
from app.db import get_db
from app.models import CalendarConnection, Contact, Invitee
from app.ratelimit import enforce
from app.security import constant_time_eq
from app.services import webhooks as W
from app.services.providers.payments_stripe import verify_stripe_signature
from app.services.providers.sync import channel_token

router = APIRouter(tags=["Inbound webhooks"])
STOP_WORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}
START_WORDS = {"START", "UNSTOP", "YES"}


def _bad(msg: str, code: int = 400):
    return JSONResponse({"error": {"code": "INVALID_WEBHOOK", "message": msg}}, status_code=code)


@router.post("/webhooks/stripe", include_in_schema=False)
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    enforce(request, "wh_stripe", limit=600, window_seconds=60)
    body = await request.body()
    sig = request.headers.get("stripe-signature", "")
    if settings.stripe_webhook_secret:
        if not verify_stripe_signature(body, sig, settings.stripe_webhook_secret):
            return _bad("Invalid signature or timestamp outside tolerance", 400)
    elif settings.env == "prod":
        return _bad("Stripe webhook secret not configured", 503)
    try:
        event = json.loads(body)
        event_id = event["id"]
    except (ValueError, KeyError, TypeError):
        return _bad("Malformed payload")
    row, created = W.persist_inbound(db, "stripe", event_id, event)
    db.commit()
    return {"received": True, "duplicate": not created}


@router.post("/webhooks/paypal", include_in_schema=False)
async def paypal_webhook(request: Request, db: Session = Depends(get_db)):
    """PayPal webhooks. Authenticity is checked server-to-server with PayPal's
    verify-webhook-signature API (needs PAYPAL_CLIENT_ID/SECRET + PAYPAL_WEBHOOK_ID) BEFORE the
    raw event is persisted. ENV=prod without that configuration rejects every event; dev/test
    without credentials accepts events (mock mode) and marks them `_verified: false`."""
    from app.services.providers import payments_paypal as PP
    from app.services.ext_settings import ext_settings

    enforce(request, "wh_paypal", limit=600, window_seconds=60)
    try:
        event = json.loads(await request.body())
        event_id = event["id"]
    except (ValueError, KeyError, TypeError):
        return _bad("Malformed payload")
    headers = {k: v for k, v in request.headers.items() if k.lower().startswith("paypal-")}
    webhook_id = ext_settings().paypal_webhook_id
    if PP.PayPalProvider.configured() and webhook_id:
        try:
            ok = PP.verify_webhook_signature(headers, event, webhook_id)
        except Exception:  # noqa: BLE001 - PayPal unreachable: 503 so PayPal redelivers later
            return _bad("Could not verify the event with PayPal right now", 503)
        if not ok:
            return _bad("PayPal signature verification failed", 400)
        verified = True
    elif settings.env == "prod":
        return _bad("PayPal webhook verification is not configured (PAYPAL_WEBHOOK_ID)", 503)
    else:
        verified = False  # mock / dev mode
    event["_headers"] = headers
    event["_verified"] = verified
    _, created = W.persist_inbound(db, "paypal", event_id, event)
    db.commit()
    return {"received": True, "duplicate": not created}


@router.post("/webhooks/google", include_in_schema=False)
async def google_webhook(request: Request, db: Session = Depends(get_db)):
    """Google Calendar push notification (events.watch). Auth = channel token we issued."""
    enforce(request, "wh_google", limit=1200, window_seconds=60)
    h = request.headers
    channel_id, token = h.get("x-goog-channel-id", ""), h.get("x-goog-channel-token", "")
    state, msg_no = h.get("x-goog-resource-state", ""), h.get("x-goog-message-number", "")
    conn = None
    for c in db.scalars(select(CalendarConnection).where(CalendarConnection.provider == "google")).all():
        if (c.sync_state_json or {}).get("watch_channel_id") == channel_id:
            conn = c
            break
    if not conn or not constant_time_eq(token, channel_token(conn.id)):
        return _bad("Unknown channel or invalid token", 403)
    if state == "sync":  # handshake message
        return Response(status_code=200)
    W.persist_inbound(db, "google", f"{channel_id}:{msg_no}", {
        "connection_id": conn.id, "resource_state": state, "resource_id": h.get("x-goog-resource-id"),
        "message_number": msg_no})
    db.commit()
    return Response(status_code=200)


@router.post("/webhooks/microsoft", include_in_schema=False)
async def microsoft_webhook(request: Request, db: Session = Depends(get_db)):
    """Microsoft Graph change notifications. Validation handshake echoes validationToken."""
    vt = request.query_params.get("validationToken")
    if vt:
        return PlainTextResponse(vt)
    enforce(request, "wh_microsoft", limit=1200, window_seconds=60)
    try:
        data = json.loads(await request.body())
    except ValueError:
        return _bad("Malformed payload")
    accepted = 0
    for n in data.get("value", []):
        sub_id = n.get("subscriptionId", "")
        conn = None
        for c in db.scalars(select(CalendarConnection).where(CalendarConnection.provider == "microsoft")).all():
            if (c.sync_state_json or {}).get("watch_subscription_id") == sub_id:
                conn = c
                break
        if not conn or not constant_time_eq(n.get("clientState", ""), channel_token(conn.id)):
            continue
        ext_id = hashlib.sha256(json.dumps(n, sort_keys=True).encode()).hexdigest()
        W.persist_inbound(db, "microsoft", ext_id, {"connection_id": conn.id, "change_type": n.get("changeType"),
                                                    "resource": n.get("resource")})
        accepted += 1
    db.commit()
    if not accepted and data.get("value"):
        return _bad("No valid notifications", 403)
    return Response(status_code=202)


def _twilio_valid(request: Request, params: dict) -> bool:
    if not settings.twilio_auth_token or IS_TEST:
        return True
    url = str(request.url)
    data = url + "".join(k + params[k] for k in sorted(params))
    mac = base64.b64encode(hmac.new(settings.twilio_auth_token.encode(), data.encode(), hashlib.sha1).digest()).decode()
    return constant_time_eq(mac, request.headers.get("x-twilio-signature", ""))


@router.post("/webhooks/sms/inbound", include_in_schema=False)
async def sms_inbound(request: Request, db: Session = Depends(get_db)):
    """SMS opt-out handling (STOP / START keywords). Updates consent on every invitee with that number."""
    enforce(request, "wh_sms", limit=300, window_seconds=60)
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    if not _twilio_valid(request, params):
        return _bad("Invalid signature", 403)
    phone = (params.get("From") or "").strip()
    word = (params.get("Body") or "").strip().upper()
    if not phone:
        return _bad("Missing From")
    action = "stop" if word in STOP_WORDS else "start" if word in START_WORDS else None
    if action:
        for inv in db.scalars(select(Invitee).where(Invitee.phone == phone)).all():
            consent = dict(inv.consent_json or {})
            consent["sms_opt_out"] = action == "stop"
            if action == "stop":
                consent["sms"] = False
            inv.consent_json = consent
        for c in db.scalars(select(Contact).where(Contact.phone == phone)).all():
            cj = dict(c.custom_json or {})
            cj["sms_opt_out"] = action == "stop"
            c.custom_json = cj
        db.commit()
    reply = {"stop": "You have been unsubscribed and will receive no further SMS. Reply START to resubscribe.",
             "start": "You have been resubscribed to SMS reminders."}.get(action, "")
    twiml = f'<?xml version="1.0" encoding="UTF-8"?><Response>{"<Message>" + reply + "</Message>" if reply else ""}</Response>'
    return Response(twiml, media_type="application/xml")
