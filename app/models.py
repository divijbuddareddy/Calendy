"""Data model (PRD section 18). All tenant-owned rows carry workspace_id.

Conventions:
  * ids are opaque string ids with a prefix (see security.new_id)
  * all datetimes are naive UTC (`utcnow()`); IANA timezone strings stored separately
  * money = integer minor units + ISO 4217 currency
  * availability rules stored as local wall-clock "HH:MM" strings
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _id(prefix: str):
    def gen():
        from app.security import new_id

        return new_id(prefix)

    return gen


class TimestampMixin:
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


# --------------------------------------------------------------------------- identity / tenancy
class User(Base, TimestampMixin):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("usr"))
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    password_hash: Mapped[Optional[str]] = mapped_column(String(200))
    google_sub: Mapped[Optional[str]] = mapped_column(String(100), unique=True)
    avatar_url: Mapped[Optional[str]] = mapped_column(String(500))
    slug: Mapped[Optional[str]] = mapped_column(String(80), unique=True, index=True)  # public username
    headline: Mapped[Optional[str]] = mapped_column(String(300))
    default_timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    locale: Mapped[str] = mapped_column(String(16), default="en")
    working_hours_json: Mapped[Optional[dict]] = mapped_column(JSON)
    email_verified_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    onboarding_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    notification_prefs_json: Mapped[Optional[dict]] = mapped_column(JSON)  # {"new_booking_email":true,...}
    branding_json: Mapped[Optional[dict]] = mapped_column(JSON)  # {"primary":"#..","hide_branding":false}
    deleted_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    # Bumped on password change / "sign out other sessions"; sessions carrying an older epoch are rejected.
    session_epoch: Mapped[Optional[int]] = mapped_column(Integer, default=0)


class Workspace(Base, TimestampMixin):
    __tablename__ = "workspaces"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("ws"))
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    plan_id: Mapped[str] = mapped_column(String(32), default="free")  # see entitlements.PLANS
    status: Mapped[str] = mapped_column(String(20), default="active")
    branding_json: Mapped[Optional[dict]] = mapped_column(JSON)
    # quiet_hours {"start":"21:00","end":"08:00","tz":".."}, retention_days, approved_domains[], sso{}
    settings_json: Mapped[Optional[dict]] = mapped_column(JSON)


class WorkspaceMember(Base, TimestampMixin):
    __tablename__ = "workspace_members"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("wm"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    user_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(20), default="member")  # owner|admin|member|viewer
    status: Mapped[str] = mapped_column(String(20), default="active")  # invited|active|deactivated
    invite_email: Mapped[Optional[str]] = mapped_column(String(320))
    invite_token: Mapped[Optional[str]] = mapped_column(String(100), unique=True)
    invited_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)


class Team(Base, TimestampMixin):
    __tablename__ = "teams"
    __table_args__ = (UniqueConstraint("workspace_id", "slug"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("team"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text)
    logo_url: Mapped[Optional[str]] = mapped_column(String(500))


class TeamMember(Base, TimestampMixin):
    __tablename__ = "team_members"
    __table_args__ = (UniqueConstraint("team_id", "user_id"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("tm"))
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(20), default="member")  # owner|admin|manager|member
    priority: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class HostLock(Base):
    """One row per host used to serialise booking transactions (see db.lock_hosts)."""

    __tablename__ = "host_locks"
    host_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    counter: Mapped[int] = mapped_column(Integer, default=0)


# --------------------------------------------------------------------------- calendars
class CalendarConnection(Base, TimestampMixin):
    __tablename__ = "calendar_connections"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("cc"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(20))  # google|microsoft|mock|ics
    account_email: Mapped[Optional[str]] = mapped_column(String(320))
    encrypted_tokens: Mapped[Optional[str]] = mapped_column(Text)
    scopes: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="connected")  # connected|error|revoked
    expires_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    last_synced_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    sync_state_json: Mapped[Optional[dict]] = mapped_column(JSON)  # watch channel / delta tokens


class ConnectedCalendar(Base, TimestampMixin):
    __tablename__ = "connected_calendars"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("cal"))
    connection_id: Mapped[str] = mapped_column(ForeignKey("calendar_connections.id", ondelete="CASCADE"), index=True)
    provider_calendar_id: Mapped[str] = mapped_column(String(300))
    display_name: Mapped[str] = mapped_column(String(300))
    is_conflict_calendar: Mapped[bool] = mapped_column(Boolean, default=True)
    is_write_calendar: Mapped[bool] = mapped_column(Boolean, default=False)


class ExternalCalendarEvent(Base, TimestampMixin):
    """Busy events synced from providers (and the editable store for the built-in mock provider)."""

    __tablename__ = "external_calendar_events"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("xev"))
    calendar_id: Mapped[str] = mapped_column(ForeignKey("connected_calendars.id", ondelete="CASCADE"), index=True)
    external_id: Mapped[str] = mapped_column(String(300))
    title: Mapped[str] = mapped_column(String(500), default="Busy")
    start_at: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    end_at: Mapped[dt.datetime] = mapped_column(DateTime)
    transparent: Mapped[bool] = mapped_column(Boolean, default=False)  # free/"show as available"
    booking_id: Mapped[Optional[str]] = mapped_column(String(40))  # set when created by us


# --------------------------------------------------------------------------- availability
class AvailabilitySchedule(Base, TimestampMixin):
    __tablename__ = "availability_schedules"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("sch"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(200), default="Working hours")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    holiday_country: Mapped[Optional[str]] = mapped_column(String(8))  # e.g. "GB", "US", "IN"


class AvailabilityRule(Base):
    __tablename__ = "availability_rules"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("ar"))
    schedule_id: Mapped[str] = mapped_column(ForeignKey("availability_schedules.id", ondelete="CASCADE"), index=True)
    weekday: Mapped[int] = mapped_column(Integer)  # 0=Monday .. 6=Sunday
    start_local: Mapped[str] = mapped_column(String(5))  # "09:00"
    end_local: Mapped[str] = mapped_column(String(5))  # "17:00" ("24:00" allowed)


class AvailabilityOverride(Base, TimestampMixin):
    __tablename__ = "availability_overrides"
    __table_args__ = (UniqueConstraint("schedule_id", "local_date"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("ao"))
    schedule_id: Mapped[str] = mapped_column(ForeignKey("availability_schedules.id", ondelete="CASCADE"), index=True)
    local_date: Mapped[dt.date] = mapped_column(Date)
    # list of {"start":"HH:MM","end":"HH:MM"}; empty list = unavailable all day
    intervals_json: Mapped[list] = mapped_column(JSON, default=list)
    kind: Mapped[str] = mapped_column(String(20), default="override")  # override|holiday|day_off
    reason: Mapped[Optional[str]] = mapped_column(String(300))


# --------------------------------------------------------------------------- event types
class EventType(Base, TimestampMixin):
    __tablename__ = "event_types"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("evt"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    team_id: Mapped[Optional[str]] = mapped_column(ForeignKey("teams.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(120))
    description: Mapped[Optional[str]] = mapped_column(Text)
    color: Mapped[str] = mapped_column(String(16), default="#4f46e5")
    # one_on_one | group | collective | round_robin
    kind: Mapped[str] = mapped_column(String(20), default="one_on_one")
    duration: Mapped[int] = mapped_column(Integer, default=30)  # default minutes
    durations_json: Mapped[Optional[list]] = mapped_column(JSON)  # e.g. [15,30,60] when multiple allowed
    # list of location options: [{"type":"google_meet|zoom|teams|phone|in_person|custom_url|host_provides|invitee_phone","value":".."}]
    location_json: Mapped[Optional[list]] = mapped_column(JSON)
    schedule_id: Mapped[Optional[str]] = mapped_column(ForeignKey("availability_schedules.id"))
    custom_rules_json: Mapped[Optional[list]] = mapped_column(JSON)  # per-event weekly override rules
    slot_interval: Mapped[Optional[int]] = mapped_column(Integer)  # minutes; None = duration
    min_notice_minutes: Mapped[int] = mapped_column(Integer, default=240)
    max_days_ahead: Mapped[int] = mapped_column(Integer, default=60)
    buffer_before: Mapped[int] = mapped_column(Integer, default=0)
    buffer_after: Mapped[int] = mapped_column(Integer, default=0)
    daily_limit: Mapped[Optional[int]] = mapped_column(Integer)
    weekly_limit: Mapped[Optional[int]] = mapped_column(Integer)
    monthly_limit: Mapped[Optional[int]] = mapped_column(Integer)
    max_invitees: Mapped[int] = mapped_column(Integer, default=1)  # group capacity
    show_remaining_seats: Mapped[bool] = mapped_column(Boolean, default=True)
    waitlist_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    rr_strategy: Mapped[str] = mapped_column(String(20), default="availability")  # availability|equal
    calendar_event_title: Mapped[str] = mapped_column(String(300), default="{event_name} with {invitee_name}")
    # {"message":"..","redirect_url":"..","pass_params":true,"links":[{"label":"..","url":".."}]}
    confirmation_json: Mapped[Optional[dict]] = mapped_column(JSON)
    # {"allow_cancel":true,"allow_reschedule":true,"min_notice_minutes":60,"require_reason":false,"host_approval":false,"policy_text":".."}
    cancellation_policy_json: Mapped[Optional[dict]] = mapped_column(JSON)
    freebusy_exceptions_json: Mapped[Optional[list]] = mapped_column(JSON)  # calendar titles that don't block
    requires_email_verification: Mapped[bool] = mapped_column(Boolean, default=False)
    price_minor: Mapped[Optional[int]] = mapped_column(Integer)
    currency: Mapped[Optional[str]] = mapped_column(String(3))
    payment_provider: Mapped[Optional[str]] = mapped_column(String(20))  # stripe|paypal
    payment_description: Mapped[Optional[str]] = mapped_column(String(300))
    visibility: Mapped[str] = mapped_column(String(20), default="public")  # public|secret|private|team
    shared_with_json: Mapped[Optional[list]] = mapped_column(JSON)  # user ids allowed to edit
    status: Mapped[str] = mapped_column(String(20), default="active")  # active|paused|archived
    is_one_off: Mapped[bool] = mapped_column(Boolean, default=False)
    one_off_slots_json: Mapped[Optional[list]] = mapped_column(JSON)  # explicit UTC ISO start times
    single_use: Mapped[bool] = mapped_column(Boolean, default=False)
    used_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    is_managed_template: Mapped[bool] = mapped_column(Boolean, default=False)
    managed_template_id: Mapped[Optional[str]] = mapped_column(String(40))
    locked_fields_json: Mapped[Optional[list]] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1)
    deleted_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)

    questions: Mapped[list["EventQuestion"]] = relationship(
        back_populates="event_type", order_by="EventQuestion.sort_order", cascade="all, delete-orphan"
    )
    hosts: Mapped[list["EventTypeHost"]] = relationship(back_populates="event_type", cascade="all, delete-orphan")


Index("ix_event_type_owner_slug", EventType.owner_user_id, EventType.slug, unique=True)


class EventTypeHost(Base):
    __tablename__ = "event_type_hosts"
    __table_args__ = (UniqueConstraint("event_type_id", "user_id"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("eth"))
    event_type_id: Mapped[str] = mapped_column(ForeignKey("event_types.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    priority: Mapped[int] = mapped_column(Integer, default=1)  # higher = preferred
    required: Mapped[bool] = mapped_column(Boolean, default=True)  # collective: required vs optional
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    daily_capacity: Mapped[Optional[int]] = mapped_column(Integer)
    assignment_count: Mapped[int] = mapped_column(Integer, default=0)
    last_assigned_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    event_type: Mapped[EventType] = relationship(back_populates="hosts")


class EventQuestion(Base):
    __tablename__ = "event_questions"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("q"))
    event_type_id: Mapped[str] = mapped_column(ForeignKey("event_types.id", ondelete="CASCADE"), index=True)
    # text|textarea|phone|company|select|checkbox|radio|consent|url|email|number
    type: Mapped[str] = mapped_column(String(20), default="text")
    label: Mapped[str] = mapped_column(String(300))
    help_text: Mapped[Optional[str]] = mapped_column(String(500))
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    options_json: Mapped[Optional[list]] = mapped_column(JSON)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    prefill_key: Mapped[Optional[str]] = mapped_column(String(60))  # query param name for prefill (a1..a10)
    event_type: Mapped[EventType] = relationship(back_populates="questions")


# --------------------------------------------------------------------------- bookings
ACTIVE_BOOKING_STATUSES = ("held", "pending", "pending_payment", "confirmed")


class Booking(Base, TimestampMixin):
    __tablename__ = "bookings"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("bk"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    event_type_id: Mapped[str] = mapped_column(ForeignKey("event_types.id"), index=True)
    host_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)  # primary/assigned host
    start_at: Mapped[dt.datetime] = mapped_column(DateTime, index=True)  # UTC
    end_at: Mapped[dt.datetime] = mapped_column(DateTime, index=True)  # UTC
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")  # invitee timezone
    # held | pending | pending_payment | confirmed | cancelled | rescheduled | expired | failed
    status: Mapped[str] = mapped_column(String(20), default="held", index=True)
    exclusive: Mapped[bool] = mapped_column(Boolean, default=True)  # False for group-event seats
    hold_expires_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(200), unique=True)
    manage_token: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    manage_token_expires_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    snapshot_json: Mapped[Optional[dict]] = mapped_column(JSON)  # event config at booking time
    location_json: Mapped[Optional[dict]] = mapped_column(JSON)  # {"type":..,"value":..,"join_url":..}
    utm_json: Mapped[Optional[dict]] = mapped_column(JSON)
    source: Mapped[Optional[str]] = mapped_column(String(40))  # booking_page|embed|api|routing|assistant
    cancel_reason: Mapped[Optional[str]] = mapped_column(Text)
    cancelled_by: Mapped[Optional[str]] = mapped_column(String(20))  # host|invitee|admin|provider|system
    cancelled_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    cancellation_requested_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)  # host approval flow
    rescheduled_from_id: Mapped[Optional[str]] = mapped_column(String(40), index=True)
    rescheduled_to_id: Mapped[Optional[str]] = mapped_column(String(40))
    no_show: Mapped[bool] = mapped_column(Boolean, default=False)
    no_show_marked_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    host_notes: Mapped[Optional[str]] = mapped_column(Text)
    assignment_reason: Mapped[Optional[str]] = mapped_column(String(300))
    routing_submission_id: Mapped[Optional[str]] = mapped_column(String(40))
    conflict_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    conflict_detail: Mapped[Optional[str]] = mapped_column(Text)
    payment_status: Mapped[Optional[str]] = mapped_column(String(20))  # none|pending|paid|failed|refunded
    version: Mapped[int] = mapped_column(Integer, default=1)

    invitees: Mapped[list["Invitee"]] = relationship(back_populates="booking", cascade="all, delete-orphan")
    answers: Mapped[list["BookingAnswer"]] = relationship(cascade="all, delete-orphan")
    hosts: Mapped[list["BookingHost"]] = relationship(cascade="all, delete-orphan")


# DB-level guard against double booking of the same exact start for exclusive bookings
Index(
    "ux_booking_host_start_active",
    Booking.host_id,
    Booking.start_at,
    unique=True,
    sqlite_where=text("exclusive = 1 AND status IN ('held','pending','pending_payment','confirmed')"),
    postgresql_where=text("exclusive AND status IN ('held','pending','pending_payment','confirmed')"),
)


class BookingHost(Base):
    """Participation of each host (collective events: all hosts; others: the assigned host)."""

    __tablename__ = "booking_hosts"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("bh"))
    booking_id: Mapped[str] = mapped_column(ForeignKey("bookings.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    required: Mapped[bool] = mapped_column(Boolean, default=True)


class Invitee(Base, TimestampMixin):
    __tablename__ = "invitees"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("inv"))
    booking_id: Mapped[str] = mapped_column(ForeignKey("bookings.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320), index=True)
    phone: Mapped[Optional[str]] = mapped_column(String(50))
    company: Mapped[Optional[str]] = mapped_column(String(200))
    timezone: Mapped[Optional[str]] = mapped_column(String(64))
    consent_json: Mapped[Optional[dict]] = mapped_column(JSON)
    guests_json: Mapped[Optional[list]] = mapped_column(JSON)  # additional guest emails
    booking: Mapped[Booking] = relationship(back_populates="invitees")


class BookingAnswer(Base):
    __tablename__ = "booking_answers"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("ans"))
    booking_id: Mapped[str] = mapped_column(ForeignKey("bookings.id", ondelete="CASCADE"), index=True)
    question_id: Mapped[Optional[str]] = mapped_column(String(40))
    label: Mapped[str] = mapped_column(String(300))  # snapshot of question label
    answer_json: Mapped[Any] = mapped_column(JSON)


class BookingEvent(Base):
    """Immutable booking history / audit trail."""

    __tablename__ = "booking_events"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("be"))
    booking_id: Mapped[str] = mapped_column(ForeignKey("bookings.id", ondelete="CASCADE"), index=True)
    actor: Mapped[str] = mapped_column(String(60))  # host|invitee|admin|provider|system|user:<id>
    event_type: Mapped[str] = mapped_column(String(60))  # created|confirmed|cancelled|rescheduled|no_show|...
    payload_json: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class CalendarEvent(Base, TimestampMixin):
    """External calendar event created for a booking (one per host calendar)."""

    __tablename__ = "calendar_events"
    __table_args__ = (UniqueConstraint("provider", "external_event_id"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("ce"))
    booking_id: Mapped[str] = mapped_column(ForeignKey("bookings.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    connection_id: Mapped[Optional[str]] = mapped_column(String(40))
    provider: Mapped[str] = mapped_column(String(20))
    external_event_id: Mapped[Optional[str]] = mapped_column(String(300))
    join_url: Mapped[Optional[str]] = mapped_column(String(1000))
    conference_provider: Mapped[Optional[str]] = mapped_column(String(20))
    conference_id: Mapped[Optional[str]] = mapped_column(String(300))
    sync_status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|synced|failed|deleted
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(200), unique=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)


class BookingHold(Base):
    """Short-lived reservation of a host/time interval (e.g. during reschedule or checkout)."""

    __tablename__ = "booking_holds"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("hold"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    host_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    event_type_id: Mapped[Optional[str]] = mapped_column(String(40))
    booking_id: Mapped[Optional[str]] = mapped_column(String(40))
    start_at: Mapped[dt.datetime] = mapped_column(DateTime)
    end_at: Mapped[dt.datetime] = mapped_column(DateTime)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(200), unique=True)
    released: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class WaitlistEntry(Base, TimestampMixin):
    __tablename__ = "waitlist_entries"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("wl"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    event_type_id: Mapped[str] = mapped_column(ForeignKey("event_types.id"), index=True)
    start_at: Mapped[dt.datetime] = mapped_column(DateTime)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320))
    notified_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)


class VerificationCode(Base):
    """One-time codes: user email verification, invitee pre-booking verification."""

    __tablename__ = "verification_codes"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("vc"))
    email: Mapped[str] = mapped_column(String(320), index=True)
    purpose: Mapped[str] = mapped_column(String(30))  # invitee_booking|email_verify|login
    code_hash: Mapped[str] = mapped_column(String(200))
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime)
    consumed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


# --------------------------------------------------------------------------- notifications / workflows
class Workflow(Base, TimestampMixin):
    """workflow_rules in the PRD."""

    __tablename__ = "workflow_rules"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("wf"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    owner_user_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(200))
    # invitee_created|before_event|after_event|invitee_canceled|no_show|routing_submitted|rescheduled
    trigger: Mapped[str] = mapped_column(String(30))
    offset_minutes: Mapped[int] = mapped_column(Integer, default=0)  # before/after offsets
    channel: Mapped[str] = mapped_column(String(10), default="email")  # email|sms
    audience: Mapped[str] = mapped_column(String(20), default="invitee")  # invitee|host|custom
    custom_recipient: Mapped[Optional[str]] = mapped_column(String(320))
    subject_template: Mapped[Optional[str]] = mapped_column(String(500))
    body_template: Mapped[str] = mapped_column(Text, default="")
    conditions_json: Mapped[Optional[dict]] = mapped_column(JSON)  # {"event_type_ids":[..]}
    actions_json: Mapped[Optional[dict]] = mapped_column(JSON)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    managed: Mapped[bool] = mapped_column(Boolean, default=False)  # admin-managed, locked for members


class NotificationJob(Base, TimestampMixin):
    __tablename__ = "notification_jobs"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("nj"))
    workspace_id: Mapped[Optional[str]] = mapped_column(String(40), index=True)
    booking_id: Mapped[Optional[str]] = mapped_column(ForeignKey("bookings.id", ondelete="SET NULL"), index=True)
    workflow_id: Mapped[Optional[str]] = mapped_column(String(40), index=True)
    trigger: Mapped[str] = mapped_column(String(40))
    channel: Mapped[str] = mapped_column(String(10), default="email")
    recipient: Mapped[str] = mapped_column(String(320))
    subject: Mapped[Optional[str]] = mapped_column(String(500))
    body: Mapped[Optional[str]] = mapped_column(Text)
    attachments_json: Mapped[Optional[list]] = mapped_column(JSON)  # [{"filename":"invite.ics","content":".."}]
    scheduled_at: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    status: Mapped[str] = mapped_column(String(20), default="scheduled")  # scheduled|sent|failed|cancelled|skipped
    dedupe_key: Mapped[Optional[str]] = mapped_column(String(200), unique=True)
    provider_id: Mapped[Optional[str]] = mapped_column(String(200))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    error: Mapped[Optional[str]] = mapped_column(Text)
    sent_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)


class SentMessage(Base):
    """Log of every email/SMS handed to a provider; doubles as the dev inbox."""

    __tablename__ = "sent_messages"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("msg"))
    channel: Mapped[str] = mapped_column(String(10))
    recipient: Mapped[str] = mapped_column(String(320), index=True)
    subject: Mapped[Optional[str]] = mapped_column(String(500))
    body: Mapped[Optional[str]] = mapped_column(Text)
    attachments_json: Mapped[Optional[list]] = mapped_column(JSON)
    provider: Mapped[str] = mapped_column(String(30), default="devinbox")
    provider_id: Mapped[Optional[str]] = mapped_column(String(200))
    job_id: Mapped[Optional[str]] = mapped_column(String(40))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class InAppNotification(Base):
    __tablename__ = "in_app_notifications"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("ian"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[Optional[str]] = mapped_column(Text)
    link: Mapped[Optional[str]] = mapped_column(String(500))
    read_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class OutboxEvent(Base):
    """Transactional outbox: written in the same DB transaction as the state change."""

    __tablename__ = "outbox_events"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("evo"))
    workspace_id: Mapped[Optional[str]] = mapped_column(String(40), index=True)
    topic: Mapped[str] = mapped_column(String(60), index=True)  # booking.created etc.
    payload_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    processed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text)


class ProviderWebhookInbox(Base):
    """Raw inbound provider webhooks (Google/MS/Stripe), persisted before acknowledging."""

    __tablename__ = "provider_webhook_inbox"
    __table_args__ = (UniqueConstraint("provider", "external_event_id"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("pwi"))
    provider: Mapped[str] = mapped_column(String(20))
    external_event_id: Mapped[str] = mapped_column(String(300))
    payload_json: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="received")  # received|processed|failed|dead
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


# --------------------------------------------------------------------------- contacts
class Contact(Base, TimestampMixin):
    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("workspace_id", "email_normalized"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("con"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    owner_user_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"))
    email_normalized: Mapped[str] = mapped_column(String(320))
    name: Mapped[str] = mapped_column(String(200), default="")
    phone: Mapped[Optional[str]] = mapped_column(String(50))
    company: Mapped[Optional[str]] = mapped_column(String(200))
    timezone: Mapped[Optional[str]] = mapped_column(String(64))
    custom_json: Mapped[Optional[dict]] = mapped_column(JSON)
    tags_json: Mapped[Optional[list]] = mapped_column(JSON)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    unsubscribed: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(20), default="booking")  # booking|manual|import|routing
    deleted_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)


# --------------------------------------------------------------------------- routing
class RoutingForm(Base, TimestampMixin):
    __tablename__ = "routing_forms"
    __table_args__ = (UniqueConstraint("workspace_id", "slug"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("rf"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(120), index=True)
    # [{"id":"f1","type":"text|email|phone|select|radio|checkbox|consent|number","label":..,"required":..,"options":[..]}]
    form_schema_json: Mapped[list] = mapped_column(JSON, default=list)
    # [{"id":"r1","logic":"and|or","conditions":[{"field":"f1","op":"equals|contains|gt|lt|empty|not_empty|not_equals","value":..}],
    #   "destination":{"type":"event_type|message|url|team|host","value":..}}]
    rules_json: Mapped[list] = mapped_column(JSON, default=list)
    fallback_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="active")


class RoutingSubmission(Base):
    __tablename__ = "routing_submissions"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("rs"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    form_id: Mapped[str] = mapped_column(ForeignKey("routing_forms.id", ondelete="CASCADE"), index=True)
    answers_json: Mapped[dict] = mapped_column(JSON)
    matched_rule_id: Mapped[Optional[str]] = mapped_column(String(40))
    destination_json: Mapped[Optional[dict]] = mapped_column(JSON)
    booking_id: Mapped[Optional[str]] = mapped_column(String(40))  # set when routed booking completes
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


# --------------------------------------------------------------------------- payments
class PaymentAccount(Base, TimestampMixin):
    __tablename__ = "payment_accounts"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("pa"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(20))  # stripe|paypal|mock
    account_id: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="connected")
    encrypted_credentials: Mapped[Optional[str]] = mapped_column(Text)


class PaymentRecord(Base, TimestampMixin):
    __tablename__ = "payment_records"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("pay"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    booking_id: Mapped[str] = mapped_column(ForeignKey("bookings.id"), index=True)
    provider: Mapped[str] = mapped_column(String(20))
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    # created|pending|succeeded|failed|refund_pending|refunded|cancelled
    status: Mapped[str] = mapped_column(String(20), default="created")
    refund_status: Mapped[Optional[str]] = mapped_column(String(20))
    provider_payment_id: Mapped[Optional[str]] = mapped_column(String(200), unique=True)
    checkout_url: Mapped[Optional[str]] = mapped_column(String(1000))
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(200), unique=True)


# --------------------------------------------------------------------------- API / webhooks
class ApiKey(Base, TimestampMixin):
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("key"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(200))
    prefix: Mapped[str] = mapped_column(String(20), index=True)
    key_hash: Mapped[str] = mapped_column(String(200), unique=True)
    scopes: Mapped[str] = mapped_column(String(500), default="read write")
    last_used_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    revoked_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("scope", "key"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("idem"))
    scope: Mapped[str] = mapped_column(String(100))  # e.g. workspace id / "public"
    key: Mapped[str] = mapped_column(String(200))
    request_hash: Mapped[Optional[str]] = mapped_column(String(100))
    response_json: Mapped[Optional[dict]] = mapped_column(JSON)
    status_code: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class WebhookSubscription(Base, TimestampMixin):
    __tablename__ = "webhook_subscriptions"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("whs"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    endpoint: Mapped[str] = mapped_column(String(1000))
    events: Mapped[list] = mapped_column(JSON, default=list)
    secret_encrypted: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class WebhookDelivery(Base, TimestampMixin):
    __tablename__ = "webhook_deliveries"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("whd"))
    subscription_id: Mapped[str] = mapped_column(ForeignKey("webhook_subscriptions.id", ondelete="CASCADE"), index=True)
    event_id: Mapped[str] = mapped_column(String(40), index=True)  # outbox event id
    topic: Mapped[str] = mapped_column(String(60))
    payload_json: Mapped[dict] = mapped_column(JSON)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|delivered|failed|dead
    response_code: Mapped[Optional[int]] = mapped_column(Integer)
    response_body: Mapped[Optional[str]] = mapped_column(Text)
    next_retry_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, index=True)


# --------------------------------------------------------------------------- audit / usage / billing
class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("aud"))
    workspace_id: Mapped[Optional[str]] = mapped_column(String(40), index=True)
    actor_id: Mapped[Optional[str]] = mapped_column(String(60))
    action: Mapped[str] = mapped_column(String(100), index=True)
    entity_type: Mapped[Optional[str]] = mapped_column(String(60))
    entity_id: Mapped[Optional[str]] = mapped_column(String(60))
    metadata_json: Mapped[Optional[dict]] = mapped_column(JSON)
    ip: Mapped[Optional[str]] = mapped_column(String(60))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)


class UsageEvent(Base):
    __tablename__ = "usage_events"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("use"))
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    meter: Mapped[str] = mapped_column(String(60))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    occurred_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Subscription(Base, TimestampMixin):
    __tablename__ = "subscriptions"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("sub"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    provider_subscription_id: Mapped[Optional[str]] = mapped_column(String(200))
    plan: Mapped[str] = mapped_column(String(32))
    seats: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="active")
    period_end: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)


class PageView(Base):
    """Booking page views for conversion analytics."""

    __tablename__ = "page_views"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("pv"))
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    event_type_id: Mapped[Optional[str]] = mapped_column(String(40), index=True)
    page: Mapped[str] = mapped_column(String(40))  # profile|event|routing
    utm_json: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)


class DataRequest(Base, TimestampMixin):
    __tablename__ = "data_requests"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("dr"))
    user_id: Mapped[str] = mapped_column(String(40), index=True)
    workspace_id: Mapped[Optional[str]] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(20))  # export|deletion
    status: Mapped[str] = mapped_column(String(20), default="requested")  # requested|completed|cancelled
    result_json: Mapped[Optional[dict]] = mapped_column(JSON)
    completed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)


# --------------------------------------------------------------------------- P3: polls / AI / notetaker / enterprise
class MeetingPoll(Base, TimestampMixin):
    __tablename__ = "meeting_polls"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("poll"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[Optional[str]] = mapped_column(Text)
    duration: Mapped[int] = mapped_column(Integer, default=30)
    location_json: Mapped[Optional[dict]] = mapped_column(JSON)
    options_json: Mapped[list] = mapped_column(JSON, default=list)  # [{"id":..,"start_at":ISO UTC}]
    public_token: Mapped[str] = mapped_column(String(100), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="open")  # open|closed|booked
    chosen_option_id: Mapped[Optional[str]] = mapped_column(String(40))
    booking_id: Mapped[Optional[str]] = mapped_column(String(40))


class PollVote(Base):
    __tablename__ = "poll_votes"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("pvt"))
    poll_id: Mapped[str] = mapped_column(ForeignKey("meeting_polls.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320))
    option_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class AssistantRequest(Base, TimestampMixin):
    __tablename__ = "assistant_requests"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("ai"))
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    user_id: Mapped[str] = mapped_column(String(40), index=True)
    prompt: Mapped[str] = mapped_column(Text)
    parsed_json: Mapped[Optional[dict]] = mapped_column(JSON)  # intent, duration, range, participant, confidence, rationale
    proposal_json: Mapped[Optional[dict]] = mapped_column(JSON)  # suggested slots, draft email
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft|approved|rejected|executed
    executed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)


class MeetingRecap(Base, TimestampMixin):
    __tablename__ = "meeting_recaps"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("rcp"))
    workspace_id: Mapped[str] = mapped_column(String(40), index=True)
    booking_id: Mapped[Optional[str]] = mapped_column(String(40), index=True)
    owner_user_id: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(300))
    consent_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    recording_status: Mapped[str] = mapped_column(String(20), default="none")  # none|recording|stopped|processed
    transcript: Mapped[Optional[str]] = mapped_column(Text)  # stored encrypted (security.encrypt)
    summary: Mapped[Optional[str]] = mapped_column(Text)
    decisions_json: Mapped[Optional[list]] = mapped_column(JSON)
    action_items_json: Mapped[Optional[list]] = mapped_column(JSON)  # [{"text","owner","due"}]
    share_token: Mapped[Optional[str]] = mapped_column(String(100), unique=True)
    share_password_hash: Mapped[Optional[str]] = mapped_column(String(200))
    retention_until: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)
    deleted_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime)


class SSOConfig(Base, TimestampMixin):
    __tablename__ = "sso_configs"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=_id("sso"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    idp_entity_id: Mapped[Optional[str]] = mapped_column(String(500))
    idp_sso_url: Mapped[Optional[str]] = mapped_column(String(1000))
    idp_x509_cert: Mapped[Optional[str]] = mapped_column(Text)
    enforce_sso: Mapped[bool] = mapped_column(Boolean, default=False)
    scim_token_hash: Mapped[Optional[str]] = mapped_column(String(200))
    approved_domains_json: Mapped[Optional[list]] = mapped_column(JSON)
