"""CRM integration + routing prefill mapping tables (PRD 2 "CRM lookup", 10.5 "Salesforce lookup",
11.2 "notify CRM integration"). Kept out of app/models.py; imported by app.services.crm so the
tables are registered on Base.metadata before init_db()/create_all runs.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import TimestampMixin, _id, utcnow


class CrmConnection(Base, TimestampMixin):
    """One connected CRM per workspace. Tokens are stored encrypted (security.encrypt)."""

    __tablename__ = "crm_connections"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("crm"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(20))  # mock | salesforce | hubspot
    status: Mapped[str] = mapped_column(String(20), default="connected")  # connected | error
    access_token_enc: Mapped[Optional[str]] = mapped_column(Text)
    refresh_token_enc: Mapped[Optional[str]] = mapped_column(Text)
    instance_url: Mapped[Optional[str]] = mapped_column(String(500))  # Salesforce instance
    account_label: Mapped[Optional[str]] = mapped_column(String(300))
    settings_json: Mapped[Optional[dict]] = mapped_column(JSON)  # {"push_bookings":true,"push_routing":true}
    connected_by: Mapped[Optional[str]] = mapped_column(String(40))
    last_error: Mapped[Optional[str]] = mapped_column(Text)


class CrmRecord(Base, TimestampMixin):
    """Records of the built-in mock CRM (editable in /app/contacts/crm)."""

    __tablename__ = "crm_records"
    __table_args__ = (UniqueConstraint("workspace_id", "object_type", "email_normalized"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("crr"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    object_type: Mapped[str] = mapped_column(String(20), default="contact")  # contact | lead
    email_normalized: Mapped[str] = mapped_column(String(320), index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    company: Mapped[Optional[str]] = mapped_column(String(200))
    owner_email: Mapped[Optional[str]] = mapped_column(String(320))


class CrmActivity(Base):
    """Activity pushed (or attempted) to the connected CRM. Unique per outbox event -> idempotent."""

    __tablename__ = "crm_activities"
    __table_args__ = (UniqueConstraint("workspace_id", "source_event_id"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("cra"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    provider: Mapped[str] = mapped_column(String(20))
    topic: Mapped[str] = mapped_column(String(60))
    source_event_id: Mapped[str] = mapped_column(String(40))
    subject: Mapped[str] = mapped_column(String(300))
    email: Mapped[Optional[str]] = mapped_column(String(320))
    body: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | sent | failed | skipped
    external_id: Mapped[Optional[str]] = mapped_column(String(200))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    sent_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)


class RoutingPrefillMap(Base, TimestampMixin):
    """Explicit routing-form field -> booking prefill mapping (PRD 10.5 "safe prefilled values").

    mapping_json: {"<form field id>": "name" | "email" | "phone" | "company" | "q:<event question id>"
                   | "label:<question label>"}
    """

    __tablename__ = "routing_prefill_maps"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("rpm"))
    form_id: Mapped[str] = mapped_column(ForeignKey("routing_forms.id", ondelete="CASCADE"), unique=True, index=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    mapping_json: Mapped[dict] = mapped_column(JSON, default=dict)


class ContactFollowUp(Base):
    """One-click follow-up emails sent from Contacts > Follow-ups (PRD 13.1 P3 no-booking opportunities)."""

    __tablename__ = "contact_follow_ups"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("cfu"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    email_normalized: Mapped[str] = mapped_column(String(320), index=True)
    source: Mapped[str] = mapped_column(String(20))  # routing | cancelled
    source_id: Mapped[Optional[str]] = mapped_column(String(40))
    sent_by: Mapped[Optional[str]] = mapped_column(String(40))
    message_id: Mapped[Optional[str]] = mapped_column(String(40))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
