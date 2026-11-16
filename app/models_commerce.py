"""Tables owned by the integrations / payments / API module (kept out of models.py to avoid
merge conflicts). Imported by the routers that use them, so `init_db()` creates them.

  * IcsFeedToken          - revocable/rotatable ICS feed secret (hashed)
  * OAuthApp / OAuthAuthorizationCode / OAuthToken - OAuth 2.0 auth-code + PKCE (PRD 15.3 P3)
  * PackageProduct / PackagePurchase / PackageRedemption - meeting packages (PRD 12 P3)
  * Invoice / PaymentLink / CommerceCheckout           - invoices + standalone payment links (PRD 12 P3)
  * BillingAccount        - self-service subscription billing state (ACC-005)
  * WorkerTaskHealth      - worker heartbeat + failure streaks for alerts (PRD 23)

Money is integer minor units + ISO 4217 currency; datetimes naive UTC (see models.py).
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import TimestampMixin, _id, utcnow


# --------------------------------------------------------------------------- ICS feed
class IcsFeedToken(Base, TimestampMixin):
    __tablename__ = "ics_feed_tokens"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("icsf"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True)
    token_hash: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    hint: Mapped[str] = mapped_column(String(12), default="")  # last chars, for display only
    revoked_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    last_used_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)


# --------------------------------------------------------------------------- OAuth 2.0 provider
class OAuthApp(Base, TimestampMixin):
    __tablename__ = "oauth_apps"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("oap"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)  # developer's workspace
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(200))
    homepage_url: Mapped[Optional[str]] = mapped_column(String(500))
    client_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    client_secret_hash: Mapped[str] = mapped_column(String(200))
    redirect_uris: Mapped[list] = mapped_column(JSON, default=list)
    scopes: Mapped[str] = mapped_column(String(200), default="read")  # max scopes the app may request
    revoked_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)


class OAuthAuthorizationCode(Base):
    __tablename__ = "oauth_authorization_codes"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("oac"))
    code_hash: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    app_id: Mapped[str] = mapped_column(ForeignKey("oauth_apps.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    redirect_uri: Mapped[str] = mapped_column(String(1000))
    scopes: Mapped[str] = mapped_column(String(200))
    code_challenge: Mapped[str] = mapped_column(String(200))
    code_challenge_method: Mapped[str] = mapped_column(String(10), default="S256")
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime)
    used_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class OAuthToken(Base, TimestampMixin):
    """One grant = an access token + refresh token pair. Refresh rotates both."""

    __tablename__ = "oauth_tokens"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("oat"))
    app_id: Mapped[str] = mapped_column(ForeignKey("oauth_apps.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    scopes: Mapped[str] = mapped_column(String(200))
    access_token_hash: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    refresh_token_hash: Mapped[Optional[str]] = mapped_column(String(200), unique=True, index=True)
    prefix: Mapped[str] = mapped_column(String(20), default="")
    access_expires_at: Mapped[dt.datetime] = mapped_column(DateTime)
    refresh_expires_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    revoked_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    last_used_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    family_id: Mapped[Optional[str]] = mapped_column(String(40), index=True)  # refresh-token rotation chain


# --------------------------------------------------------------------------- commerce (P3)
class PackageProduct(Base, TimestampMixin):
    __tablename__ = "package_products"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("pkg"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    event_type_id: Mapped[str] = mapped_column(ForeignKey("event_types.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    sessions: Mapped[int] = mapped_column(Integer, default=5)
    price_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    public_token: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class PackagePurchase(Base, TimestampMixin):
    __tablename__ = "package_purchases"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("pkp"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("package_products.id"), index=True)
    buyer_name: Mapped[str] = mapped_column(String(200), default="")
    buyer_email: Mapped[str] = mapped_column(String(320))
    code_hash: Mapped[Optional[str]] = mapped_column(String(200), unique=True, index=True)
    code_hint: Mapped[str] = mapped_column(String(12), default="")
    sessions_total: Mapped[int] = mapped_column(Integer)
    sessions_used: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|active|exhausted|void
    expires_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)


class PackageRedemption(Base):
    __tablename__ = "package_redemptions"
    __table_args__ = (UniqueConstraint("booking_id"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("pkr"))
    purchase_id: Mapped[str] = mapped_column(ForeignKey("package_purchases.id"), index=True)
    booking_id: Mapped[str] = mapped_column(ForeignKey("bookings.id"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Invoice(Base, TimestampMixin):
    __tablename__ = "invoices"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("invc"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    number: Mapped[str] = mapped_column(String(40))
    recipient_name: Mapped[str] = mapped_column(String(200), default="")
    recipient_email: Mapped[str] = mapped_column(String(320))
    description: Mapped[str] = mapped_column(Text, default="")
    line_items_json: Mapped[list] = mapped_column(JSON, default=list)  # [{"description","quantity","unit_minor"}]
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    due_date: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft|sent|paid|void
    public_token: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    sent_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    paid_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    voided_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)


class PaymentLink(Base, TimestampMixin):
    __tablename__ = "payment_links"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("plk"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    description: Mapped[str] = mapped_column(String(500))
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    public_token: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    paid_count: Mapped[int] = mapped_column(Integer, default=0)


class CommerceCheckout(Base, TimestampMixin):
    """A checkout for a package / invoice / payment link (bookings use PaymentRecord)."""

    __tablename__ = "commerce_checkouts"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("cco"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # package|invoice|link
    ref_id: Mapped[str] = mapped_column(String(40), index=True)
    provider: Mapped[str] = mapped_column(String(20), default="stripe")
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    buyer_name: Mapped[str] = mapped_column(String(200), default="")
    buyer_email: Mapped[str] = mapped_column(String(320), default="")
    status: Mapped[str] = mapped_column(String(20), default="created")  # created|pending|succeeded|failed|refunded
    provider_payment_id: Mapped[Optional[str]] = mapped_column(String(200), unique=True)
    checkout_url: Mapped[Optional[str]] = mapped_column(String(1000))
    access_token: Mapped[str] = mapped_column(String(60), unique=True, index=True)  # buyer's return-page secret
    completed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)


# --------------------------------------------------------------------------- subscription billing
class BillingAccount(Base, TimestampMixin):
    __tablename__ = "billing_accounts"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("bill"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(20), default="mock")  # mock|stripe
    customer_id: Mapped[Optional[str]] = mapped_column(String(200))
    subscription_id: Mapped[Optional[str]] = mapped_column(String(40))  # models.Subscription.id
    pending_checkout_id: Mapped[Optional[str]] = mapped_column(String(200))
    pending_checkout_plan: Mapped[Optional[str]] = mapped_column(String(32))
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False)
    scheduled_plan: Mapped[Optional[str]] = mapped_column(String(32))  # downgrade applied at period end
    last_seat_sync_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)


# --------------------------------------------------------------------------- observability
class WorkerTaskHealth(Base):
    __tablename__ = "worker_task_health"
    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_success_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    last_failure_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    alerted_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
