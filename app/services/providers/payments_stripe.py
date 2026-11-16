"""Stripe adapter (Checkout Sessions on the host's connected account, refunds, Connect onboarding,
webhook signature verification). Payment goes straight to the host's Stripe account
(Stripe-Account header) - funds never pass through the scheduler. Used when STRIPE_SECRET_KEY is set."""
from __future__ import annotations

import hashlib
import hmac
import time

import httpx

from app.config import settings
from app.services.calendar_facade import PermanentProviderError, TransientProviderError
from app.services.providers.payments_mock import CheckoutSession, PaymentProviderBase

API = "https://api.stripe.com/v1"


def _req(method: str, path: str, *, account_id: str | None = None, idem: str | None = None, data=None, params=None):
    headers = {"Authorization": f"Bearer {settings.stripe_secret_key}"}
    if account_id:
        headers["Stripe-Account"] = account_id
    if idem:
        headers["Idempotency-Key"] = idem
    try:
        r = httpx.request(method, API + path, headers=headers, data=data, params=params, timeout=20)
    except httpx.HTTPError as exc:
        raise TransientProviderError(f"Stripe unreachable: {exc}") from exc
    if r.status_code >= 500 or r.status_code == 429:
        raise TransientProviderError(f"Stripe {r.status_code}: {r.text[:300]}")
    if r.status_code >= 400:
        raise PermanentProviderError(f"Stripe {r.status_code}: {r.text[:300]}")
    return r.json()


class StripeProvider(PaymentProviderBase):
    name = "stripe"

    @staticmethod
    def configured() -> bool:
        return bool(settings.stripe_secret_key)

    def create_checkout(self, record, booking, *, account, title, success_url, cancel_url) -> CheckoutSession:
        data = {
            "mode": "payment",
            "success_url": success_url + ("&" if "?" in success_url else "?") + "session_id={CHECKOUT_SESSION_ID}",
            "cancel_url": cancel_url,
            "client_reference_id": record.id,
            "line_items[0][quantity]": 1,
            "line_items[0][price_data][currency]": record.currency.lower(),
            "line_items[0][price_data][unit_amount]": record.amount_minor,
            "line_items[0][price_data][product_data][name]": title[:250],
            "metadata[payment_record_id]": record.id,
            "metadata[booking_id]": booking.id,
            "payment_intent_data[metadata][payment_record_id]": record.id,
            "expires_at": int(time.time()) + 31 * 60,
        }
        if booking.invitees:
            data["customer_email"] = booking.invitees[0].email
        s = _req("POST", "/checkout/sessions", account_id=account.account_id if account else None,
                 idem=record.idempotency_key, data=data)
        return CheckoutSession(s["id"], s["url"])

    def fetch_status(self, record, account) -> str:
        s = _req("GET", f"/checkout/sessions/{record.provider_payment_id}", account_id=account.account_id if account else None)
        if s.get("payment_status") == "paid":
            return "succeeded"
        if s.get("status") == "expired":
            return "failed"
        return "pending"

    def refund(self, record, account) -> str:
        acct = account.account_id if account else None
        s = _req("GET", f"/checkout/sessions/{record.provider_payment_id}", account_id=acct)
        pi = s.get("payment_intent")
        if not pi:
            raise PermanentProviderError("No payment intent to refund")
        r = _req("POST", "/refunds", account_id=acct, idem=f"refund:{record.id}", data={"payment_intent": pi})
        return r["id"]

    # --- Connect onboarding (Express accounts + Account Links)
    def create_connected_account(self, email: str, idem: str) -> str:
        return _req("POST", "/accounts", idem=idem, data={"type": "express", "email": email})["id"]

    def onboarding_link(self, account_id: str, refresh_url: str, return_url: str) -> str:
        return _req("POST", "/account_links", data={
            "account": account_id, "refresh_url": refresh_url, "return_url": return_url, "type": "account_onboarding",
        })["url"]

    def account_ready(self, account_id: str) -> bool:
        a = _req("GET", f"/accounts/{account_id}")
        return bool(a.get("charges_enabled"))


def verify_stripe_signature(payload: bytes, header: str, secret: str, tolerance: int = 300) -> bool:
    """Stripe-Signature: t=..,v1=..[,v1=..] ; HMAC-SHA256 over f"{t}.{payload}"."""
    try:
        items = [p.split("=", 1) for p in (header or "").split(",") if "=" in p]
        ts = int(next(v for k, v in items if k == "t"))
        sigs = [v for k, v in items if k == "v1"]
    except (StopIteration, ValueError):
        return False
    if abs(time.time() - ts) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in sigs)
