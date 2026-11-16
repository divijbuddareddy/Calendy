"""PayPal adapter (Orders v2: create -> buyer approval -> capture; refunds on captures).
The host's PayPal account is the payee (merchant id or email) so funds go directly to them.
Used when PAYPAL_CLIENT_ID / PAYPAL_SECRET are configured (sandbox unless ENV=prod)."""
from __future__ import annotations

import httpx

from app.config import settings
from app.services.calendar_facade import PermanentProviderError, TransientProviderError
from app.services.providers.payments_mock import CheckoutSession, PaymentProviderBase


def _base() -> str:
    return "https://api-m.paypal.com" if settings.env == "prod" else "https://api-m.sandbox.paypal.com"


def _token() -> str:
    try:
        r = httpx.post(f"{_base()}/v1/oauth2/token", auth=(settings.paypal_client_id, settings.paypal_secret),
                       data={"grant_type": "client_credentials"}, timeout=20)
    except httpx.HTTPError as exc:
        raise TransientProviderError(str(exc)) from exc
    if r.status_code >= 400:
        raise PermanentProviderError(f"PayPal auth failed: {r.status_code}")
    return r.json()["access_token"]


def _req(method, path, *, json=None, idem=None):
    headers = {"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"}
    if idem:
        headers["PayPal-Request-Id"] = idem
    try:
        r = httpx.request(method, _base() + path, headers=headers, json=json, timeout=20)
    except httpx.HTTPError as exc:
        raise TransientProviderError(str(exc)) from exc
    if r.status_code >= 500 or r.status_code == 429:
        raise TransientProviderError(f"PayPal {r.status_code}")
    if r.status_code >= 400:
        raise PermanentProviderError(f"PayPal {r.status_code}: {r.text[:300]}")
    return r.json() if r.text else {}


class PayPalProvider(PaymentProviderBase):
    name = "paypal"

    @staticmethod
    def configured() -> bool:
        return bool(settings.paypal_client_id and settings.paypal_secret)

    def create_checkout(self, record, booking, *, account, title, success_url, cancel_url) -> CheckoutSession:
        payee = {}
        if account and account.account_id:
            payee = {"email_address": account.account_id} if "@" in account.account_id else {"merchant_id": account.account_id}
        unit = {
            "reference_id": record.id, "custom_id": record.id, "description": title[:127],
            "amount": {"currency_code": record.currency.upper(), "value": f"{record.amount_minor / 100:.2f}"},
        }
        if payee:
            unit["payee"] = payee
        order = _req("POST", "/v2/checkout/orders", idem=record.idempotency_key, json={
            "intent": "CAPTURE", "purchase_units": [unit],
            "application_context": {"return_url": success_url, "cancel_url": cancel_url, "user_action": "PAY_NOW",
                                    "shipping_preference": "NO_SHIPPING"},
        })
        link = next(l["href"] for l in order.get("links", []) if l.get("rel") in ("approve", "payer-action"))
        return CheckoutSession(order["id"], link)

    def capture(self, record, account, params) -> str:
        res = _req("POST", f"/v2/checkout/orders/{record.provider_payment_id}/capture", idem=f"capture:{record.id}")
        return "succeeded" if res.get("status") == "COMPLETED" else "failed"

    def fetch_status(self, record, account) -> str:
        o = _req("GET", f"/v2/checkout/orders/{record.provider_payment_id}")
        return {"COMPLETED": "succeeded", "VOIDED": "failed"}.get(o.get("status"), "pending")

    def refund(self, record, account) -> str:
        o = _req("GET", f"/v2/checkout/orders/{record.provider_payment_id}")
        caps = o["purchase_units"][0].get("payments", {}).get("captures", [])
        if not caps:
            raise PermanentProviderError("No capture to refund")
        return _req("POST", f"/v2/payments/captures/{caps[0]['id']}/refund", idem=f"refund:{record.id}", json={}).get("id", "")


# ------------------------------------------------------------------ webhook verification
VERIFY_HEADERS = {
    "transmission_id": "paypal-transmission-id",
    "transmission_time": "paypal-transmission-time",
    "cert_url": "paypal-cert-url",
    "auth_algo": "paypal-auth-algo",
    "transmission_sig": "paypal-transmission-sig",
}


def verify_webhook_signature(headers: dict, event: dict, webhook_id: str) -> bool:
    """Server-to-server check via POST /v1/notifications/verify-webhook-signature.
    Returns True only for verification_status == SUCCESS. Missing headers -> False.
    Raises TransientProviderError when PayPal is unreachable (caller answers 5xx so PayPal retries)."""
    h = {k.lower(): v for k, v in (headers or {}).items()}
    body = {k: h.get(v) for k, v in VERIFY_HEADERS.items()}
    if not all(body.values()) or not webhook_id:
        return False
    body["webhook_id"] = webhook_id
    body["webhook_event"] = event
    try:
        res = _req("POST", "/v1/notifications/verify-webhook-signature", json=body)
    except PermanentProviderError:
        return False
    return (res or {}).get("verification_status") == "SUCCESS"
