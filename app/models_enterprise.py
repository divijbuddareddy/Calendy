"""Enterprise tables (PRD 17.2): SAML request/replay tracking and domain claims.

Kept out of app/models.py so the enterprise module can evolve independently. Imported by
app.services.sso / app.routers.admin, which are imported at app start (before init_db), so
`Base.metadata.create_all` creates these tables.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import _id, utcnow


class SamlAuthnRequest(Base):
    """An AuthnRequest we issued; the Response's InResponseTo must match an unused, unexpired row."""

    __tablename__ = "saml_authn_requests"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)  # the AuthnRequest ID ("_" + hex)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    relay_next: Mapped[Optional[str]] = mapped_column(String(500))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime)
    used_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)


class SamlAssertionSeen(Base):
    """Replay protection: every consumed Assertion ID, kept until the assertion would have expired."""

    __tablename__ = "saml_assertions_seen"
    __table_args__ = (UniqueConstraint("workspace_id", "assertion_id"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("sas"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    assertion_id: Mapped[str] = mapped_column(String(200))
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class DomainClaim(Base):
    """Workspace claim on an email domain, proven by a DNS TXT record. A domain can be *verified*
    by at most one workspace (enforced in services.sso.verify_domain)."""

    __tablename__ = "domain_claims"
    __table_args__ = (UniqueConstraint("workspace_id", "domain"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("dom"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    domain: Mapped[str] = mapped_column(String(253), index=True)
    token: Mapped[str] = mapped_column(String(100))
    verified_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    last_checked_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    last_error: Mapped[Optional[str]] = mapped_column(String(500))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
