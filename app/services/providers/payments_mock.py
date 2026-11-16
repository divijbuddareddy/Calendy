"""Offline mock payment provider: a hosted-checkout simulation page (/pay/{token}/checkout/mock)
with "Pay" and "Simulate failure" buttons. Never collects card data."""
from __future__ import annotations

import secrets
from dataclasses import dataclass


@dataclass
class CheckoutSession:
    provider_payment_id: str
    checkout_url: str


class PaymentProviderBase:
    name = "base"
    mock = False

    def create_checkout(self, record, booking, *, account, title: str, success_url: str, cancel_url: str) -> CheckoutSession:
        raise NotImplementedError

    def fetch_status(self, record, account) -> str:
        """Return created|pending|succeeded|failed."""
        return record.status

    def capture(self, record, account, params: dict) -> str:
        """Complete a payment after redirect (PayPal capture). Return status."""
        return self.fetch_status(record, account)

    def refund(self, record, account) -> str:
        """Return a provider refund id; raise on failure."""
        raise NotImplementedError


class MockPaymentProvider(PaymentProviderBase):
    name = "mock"
    mock = True

    def __init__(self, label: str = "stripe"):
        self.label = label

    def create_checkout(self, record, booking, *, account, title, success_url, cancel_url) -> CheckoutSession:
        pid = record.provider_payment_id or f"mock_{self.label}_{secrets.token_hex(10)}"
        return CheckoutSession(pid, f"/pay/{booking.manage_token}/checkout/mock?record={record.id}")

    def refund(self, record, account) -> str:
        return f"mock_re_{secrets.token_hex(8)}"


def is_mock_account(account) -> bool:
    return bool(account is None or "_mock_" in (account.account_id or "") or (account.account_id or "").startswith("mock"))

