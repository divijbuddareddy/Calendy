"""Notetaker recording-bot table (PRD 16.2). Kept out of app/models.py (owned elsewhere).

One row per recap that has a meeting bot. `provider` is the recording adapter name
(`mock` or `recall`), `external_bot_id` the provider's bot id. Status lifecycle:
  scheduled -> joining -> in_call -> recording -> stopped -> done | removed | failed
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import TimestampMixin, _id


class NotetakerBot(Base, TimestampMixin):
    __tablename__ = "notetaker_bots"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("bot"))
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    recap_id: Mapped[str] = mapped_column(ForeignKey("meeting_recaps.id", ondelete="CASCADE"), unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(20), default="mock")
    external_bot_id: Mapped[Optional[str]] = mapped_column(String(200), index=True)
    bot_name: Mapped[str] = mapped_column(String(200))
    meeting_url: Mapped[Optional[str]] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(String(20), default="scheduled")
    status_detail: Mapped[Optional[str]] = mapped_column(String(300))
    consent_sent_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    consent_recipients: Mapped[Optional[int]] = mapped_column(default=0)
    recording_started_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    recording_stopped_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    transcript_fetched_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
