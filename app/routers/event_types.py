"""Event types (PRD 6): list, create, sectioned editor, duplicate/pause/archive/delete,
questions, hosts, one-off meetings, single-use links, embed snippets.

Config edits never touch existing bookings (they carry snapshot_json); every saved change
bumps `EventType.version` and is audit-logged.

custom_rules_json format: [{"weekday": 0-6 (Mon=0), "start": "HH:MM", "end": "HH:MM"}, ...]
"""
from __future__ import annotations

import datetime as dt
import re
import secrets
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.audit import audit
from app.db import get_db
from app.deps import Ctx, Forbidden, flash, get_ctx, redirect, render
from app.entitlements import has_feature, limit
from app.models import (
    AvailabilitySchedule,
    Booking,
    EventQuestion,
    EventType,
    EventTypeHost,
    Team,
    TeamMember,
    User,
    WorkspaceMember,
    utcnow,
)
from app.services.bootstrap import slugify
from app.services.hostutil import (
    can_edit_event,
    can_view_event,
    embed_snippets,
    event_public_path,
    event_public_url,
    is_host_public,
    parse_range_text,
)

router = APIRouter()

KINDS = {"one_on_one": "One-on-one", "group": "Group", "collective": "Collective", "round_robin": "Round robin"}
KIND_FEATURE = {"group": "group", "collective": "collective", "round_robin": "round_robin"}
LOCATION_TYPES = {
    "google_meet": "Google Meet", "zoom": "Zoom", "teams": "Microsoft Teams", "phone": "Phone call (host calls invitee)",
    "invitee_phone": "Phone call (invitee calls host)", "in_person": "In-person meeting", "custom_url": "Custom link",
    "host_provides": "Host will provide details",
}
QUESTION_TYPES = {
    "text": "Short text", "textarea": "Paragraph", "phone": "Phone number", "company": "Company", "email": "Email",
    "url": "URL", "number": "Number", "select": "Dropdown", "radio": "Radio buttons", "checkbox": "Checkboxes",
    "consent": "Consent checkbox",
}
OPTION_TYPES = {"select", "radio", "checkbox"}
SECTIONS = ["basics", "location", "availability", "hosts", "questions", "confirmation", "policy", "payment",
            "permissions"]
SECTION_LABELS = {"basics": "Basics", "location": "Location", "availability": "Availability", "hosts": "Hosts",
                  "questions": "Questions", "confirmation": "Notifications & confirmation", "policy": "Policy",
                  "payment": "Payment", "permissions": "Permissions"}
TITLE_VARS = {"event_name", "invitee_name", "host_name", "invitee_email", "event_date", "event_time"}
CURRENCIES = ["USD", "GBP", "EUR", "INR", "AUD", "CAD"]
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
LOCK_ALIASES = {"location": "location_json", "durations": "durations_json", "confirmation": "confirmation_json",
                "cancellation_policy": "cancellation_policy_json", "price": "price_minor", "schedule": "schedule_id",
                "custom_rules": "custom_rules_json", "shared_with": "shared_with_json"}
CLONE_SKIP = {"id", "created_at", "updated_at", "slug", "deleted_at", "used_at", "version"}


class ValidationFailed(Exception):
    pass


class LockedField(Exception):
    pass


# ================================================================ helpers
def locked_fields(ev: EventType) -> set[str]:
    out = set()
    for f in ev.locked_fields_json or []:
        out.add(LOCK_ALIASES.get(f, f))
    return out


def is_locked(ctx: Ctx, ev: EventType, field: str) -> bool:
    return field in locked_fields(ev) and not ctx.can("admin")


def get_event_or_404(db: Session, ctx: Ctx, event_id: str, edit: bool = True) -> EventType:
    ev = db.get(EventType, event_id)
    if not ev or ev.workspace_id != ctx.workspace.id or ev.deleted_at:
        raise HTTPException(404, "Event type not found")
    if edit and not can_edit_event(ctx, ev):
        if not can_view_event(db, ctx, ev):
            raise HTTPException(404, "Event type not found")
        raise Forbidden("You don't have permission to edit this event type.")
    if not edit and not can_view_event(db, ctx, ev):
        raise HTTPException(404, "Event type not found")
    return ev


def unique_event_slug(db: Session, owner_id: str, base: str, exclude_id: Optional[str] = None) -> str:
    base = slugify(base)[:100]
    slug, n = base, 1
    while True:
        q = select(EventType.id).where(EventType.owner_user_id == owner_id, EventType.slug == slug)
        if exclude_id:
            q = q.where(EventType.id != exclude_id)
        if not db.scalar(q):
            return slug
        n += 1
        slug = f"{base}-{n}"


def slug_taken(db: Session, owner_id: str, slug: str, exclude_id: Optional[str] = None) -> bool:
    q = select(EventType.id).where(EventType.owner_user_id == owner_id, EventType.slug == slug)
    if exclude_id:
        q = q.where(EventType.id != exclude_id)
    return bool(db.scalar(q))


def active_event_count(db: Session, ctx: Ctx) -> int:
    return db.scalar(select(func.count(EventType.id)).where(
        EventType.workspace_id == ctx.workspace.id, EventType.status == "active", EventType.deleted_at.is_(None),
        EventType.is_one_off.is_(False), EventType.single_use.is_(False))) or 0


def can_activate_another(db: Session, ctx: Ctx) -> bool:
    cap = limit(ctx.workspace, "max_active_event_types")
    return cap is None or active_event_count(db, ctx) < cap


def create_event_type_basic(db: Session, ctx: Ctx, name: str, duration: int = 30, kind: str = "one_on_one",
                            slug: Optional[str] = None, color: str = "#3b47d6", description: str = "",
                            locations: Optional[list] = None) -> tuple[Optional[EventType], Optional[str]]:
    """Create an event type for ctx.user. Returns (event, error)."""
    if kind not in KINDS:
        return None, "Unknown event format."
    if kind in KIND_FEATURE and not ctx.feature(KIND_FEATURE[kind]):
        return None, f"{KINDS[kind]} events are not included in your plan. Upgrade in Settings › Billing."
    if not can_activate_another(db, ctx):
        return None, (f"Your plan allows {limit(ctx.workspace, 'max_active_event_types')} active event type(s). "
                      "Pause another event type or upgrade.")
    if slug:
        slug = slugify(slug)
        if slug_taken(db, ctx.user.id, slug):
            return None, f"You already have an event type with the link '{slug}'."
    else:
        slug = unique_event_slug(db, ctx.user.id, name)
    ev = EventType(
        workspace_id=ctx.workspace.id, owner_user_id=ctx.user.id, name=name[:200], slug=slug, kind=kind,
        duration=duration, color=color, description=description or None,
        location_json=[l for l in (locations or []) if l.get("type") in LOCATION_TYPES] or [{"type": "google_meet", "value": ""}],
        cancellation_policy_json={"allow_cancel": True, "allow_reschedule": True, "min_notice_minutes": 0,
                                  "require_reason": False, "host_approval": False, "policy_text": ""},
        confirmation_json={"message": "", "redirect_url": "", "pass_params": False, "links": []},
        max_invitees=1 if kind != "group" else 10,
    )
    db.add(ev)
    db.flush()
    if kind != "one_on_one":
        db.add(EventTypeHost(event_type_id=ev.id, user_id=ctx.user.id, priority=1, required=True, active=True))
    audit(db, "event_type.created", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="event_type",
          entity_id=ev.id, metadata={"name": ev.name, "kind": kind})
    return ev, None


def clone_event(db: Session, src: EventType, **overrides) -> EventType:
    data = {c.key: getattr(src, c.key) for c in EventType.__table__.columns if c.key not in CLONE_SKIP}
    data.update(overrides)
    ev = EventType(**data)
    db.add(ev)
    db.flush()
    for q in src.questions:
        db.add(EventQuestion(event_type_id=ev.id, type=q.type, label=q.label, help_text=q.help_text,
                             required=q.required, options_json=q.options_json, sort_order=q.sort_order,
                             prefill_key=q.prefill_key))
    for h in src.hosts:
        db.add(EventTypeHost(event_type_id=ev.id, user_id=h.user_id, priority=h.priority, required=h.required,
                             active=h.active, daily_capacity=h.daily_capacity))
    db.flush()
    return ev


def _int(v, default=None, lo=None, hi=None):
    if v is None or str(v).strip() == "":
        return default
    try:
        n = int(float(str(v).strip()))
    except ValueError:
        raise ValidationFailed(f"'{v}' is not a number.")
    if lo is not None and n < lo:
        raise ValidationFailed(f"Value {n} must be at least {lo}.")
    if hi is not None and n > hi:
        raise ValidationFailed(f"Value {n} must be at most {hi}.")
    return n


def _bool(form, key) -> bool:
    return form.get(key) in ("1", "on", "true", "yes")


def _members(db: Session, ctx: Ctx) -> list[User]:
    return db.scalars(select(User).join(WorkspaceMember, WorkspaceMember.user_id == User.id).where(
        WorkspaceMember.workspace_id == ctx.workspace.id, WorkspaceMember.status == "active",
        User.deleted_at.is_(None)).order_by(User.name)).all()


class Changes:
    """Collects field changes for an event type and enforces managed-template locks."""

    def __init__(self, form):
        self.form = form
        self.items: dict[str, tuple] = {}

    def set(self, col: str, value, *keys):
        self.items[col] = (value, keys or (col,))

    def apply(self, ctx: Ctx, ev: EventType) -> list[str]:
        locked = locked_fields(ev) if not ctx.can("admin") else set()
        changed = []
        for col, (val, keys) in self.items.items():
            if col in locked:
                if not any(k in self.form for k in keys):
                    continue  # disabled input: keep stored value
                if getattr(ev, col) != val:
                    raise LockedField(col)
                continue
            if getattr(ev, col) != val:
                setattr(ev, col, val)
                changed.append(col)
        return changed


# ================================================================ section parsers
def parse_basics(db: Session, ctx: Ctx, ev: EventType, form, ch: Changes):
    name = (form.get("name") or "").strip()
    if not name:
        raise ValidationFailed("Name is required.")
    slug = slugify(form.get("slug") or name)
    if slug_taken(db, ev.owner_user_id, slug, ev.id):
        raise ValidationFailed(f"Another event type already uses the link '{slug}'.")
    kind = form.get("kind") or ev.kind
    if kind not in KINDS:
        raise ValidationFailed("Unknown event format.")
    if kind != ev.kind and kind in KIND_FEATURE and not ctx.feature(KIND_FEATURE[kind]):
        raise ValidationFailed(f"{KINDS[kind]} events are not included in your plan.")
    color = form.get("color") or ev.color
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise ValidationFailed("Colour must be a hex value like #3b47d6.")
    duration = _int(form.get("duration"), ev.duration, 5, 1440)
    durations = None
    if _bool(form, "multiple_durations"):
        if not ctx.feature("multiple_durations"):
            raise ValidationFailed("Multiple durations are not included in your plan.")
        try:
            durations = sorted({int(x) for x in re.split(r"[,\s]+", form.get("durations") or "") if x.strip()})
        except ValueError:
            raise ValidationFailed("Durations must be numbers like 15, 30, 60.")
        if any(d < 5 or d > 1440 for d in durations):
            raise ValidationFailed("Each duration must be between 5 and 1440 minutes.")
        if duration not in durations:
            durations = sorted(set(durations) | {duration})
        if len(durations) < 2:
            durations = None
    ch.set("name", name[:200])
    ch.set("slug", slug)
    ch.set("description", (form.get("description") or "").strip() or None)
    ch.set("color", color)
    ch.set("kind", kind)
    ch.set("duration", duration)
    ch.set("durations_json", durations, "durations", "multiple_durations")


def parse_location(db, ctx, ev, form, ch: Changes):
    types, values = form.getlist("loc_type"), form.getlist("loc_value")
    locs = []
    for i, t in enumerate(types):
        if not t:
            continue
        if t not in LOCATION_TYPES:
            raise ValidationFailed("Unknown location type.")
        v = (values[i] if i < len(values) else "").strip()
        if t == "in_person" and not v:
            raise ValidationFailed("Enter an address for the in-person location.")
        if t == "custom_url" and not re.match(r"^https?://", v):
            raise ValidationFailed("Custom link must start with http:// or https://")
        if t == "invitee_phone" and not v:
            raise ValidationFailed("Enter the phone number invitees should call.")
        locs.append({"type": t, "value": v})
    ch.set("location_json", locs, "loc_type")


def parse_availability(db, ctx, ev, form, ch: Changes):
    src = form.get("availability_source") or "schedule"
    rules = None
    sid = None
    if src == "custom":
        rules = []
        for wd in range(7):
            ranges, err = parse_range_text(form.get(f"custom_{wd}") or "")
            if err:
                raise ValidationFailed(f"{WEEKDAYS[wd]}: {err}")
            rules += [{"weekday": wd, "start": r["start"], "end": r["end"]} for r in ranges]
        if not rules:
            raise ValidationFailed("Custom hours need at least one time range.")
    else:
        sid = form.get("schedule_id") or None
        if sid:
            sch = db.get(AvailabilitySchedule, sid)
            if not sch or sch.workspace_id != ctx.workspace.id or sch.user_id != ev.owner_user_id:
                raise ValidationFailed("Unknown schedule.")
    ch.set("schedule_id", sid, "schedule_id", "availability_source")
    ch.set("custom_rules_json", rules, "availability_source")
    ch.set("slot_interval", _int(form.get("slot_interval"), None, 5, 720))
    notice_val = _int(form.get("min_notice_value"), 0, 0)
    unit = {"minutes": 1, "hours": 60, "days": 1440}.get(form.get("min_notice_unit") or "minutes", 1)
    ch.set("min_notice_minutes", notice_val * unit, "min_notice_value")
    ch.set("max_days_ahead", _int(form.get("max_days_ahead"), 60, 1, 730))
    ch.set("buffer_before", _int(form.get("buffer_before"), 0, 0, 720))
    ch.set("buffer_after", _int(form.get("buffer_after"), 0, 0, 720))
    ch.set("daily_limit", _int(form.get("daily_limit"), None, 1))
    ch.set("weekly_limit", _int(form.get("weekly_limit"), None, 1))
    ch.set("monthly_limit", _int(form.get("monthly_limit"), None, 1))


def parse_hosts(db, ctx, ev, form, ch: Changes):
    strategy = form.get("rr_strategy") or "availability"
    if strategy not in ("availability", "equal"):
        raise ValidationFailed("Unknown distribution strategy.")
    ch.set("rr_strategy", strategy)
    if ev.kind == "group":
        ch.set("max_invitees", _int(form.get("max_invitees"), 10, 2, 1000))
        ch.set("show_remaining_seats", _bool(form, "show_remaining_seats"), "max_invitees")
        ch.set("waitlist_enabled", _bool(form, "waitlist_enabled"), "max_invitees")
    team_id = form.get("team_id") or None
    if team_id:
        team = db.get(Team, team_id)
        if not team or team.workspace_id != ctx.workspace.id:
            raise ValidationFailed("Unknown team.")
    ch.set("team_id", team_id)


def save_hosts(db: Session, ctx: Ctx, ev: EventType, form) -> bool:
    """Persist EventTypeHost rows from the hosts form. Returns True if anything changed."""
    if is_locked(ctx, ev, "hosts"):
        if "host_ids" in form:
            raise LockedField("hosts")
        return False
    if ev.kind == "one_on_one":
        return False
    members = {u.id for u in _members(db, ctx)}
    selected = [u for u in form.getlist("host_ids") if u in members]
    if not selected:
        raise ValidationFailed("Select at least one host.")
    existing = {h.user_id: h for h in ev.hosts}
    changed = False
    for uid, h in list(existing.items()):
        if uid not in selected:
            ev.hosts.remove(h)
            changed = True
    for uid in selected:
        h = existing.get(uid)
        if not h:
            h = EventTypeHost(event_type_id=ev.id, user_id=uid)
            ev.hosts.append(h)
            changed = True
        vals = dict(priority=_int(form.get(f"priority_{uid}"), 1, 0, 10),
                    required=_bool(form, f"required_{uid}") if ev.kind == "collective" else True,
                    active=_bool(form, f"active_{uid}"),
                    daily_capacity=_int(form.get(f"capacity_{uid}"), None, 1))
        for k, v in vals.items():
            if getattr(h, k) != v:
                setattr(h, k, v)
                changed = True
    return changed


def parse_confirmation(db, ctx, ev, form, ch: Changes):
    redirect_url = (form.get("redirect_url") or "").strip()
    if redirect_url and not re.match(r"^https?://", redirect_url):
        raise ValidationFailed("Redirect URL must start with http:// or https://")
    links = []
    for line in (form.get("links") or "").splitlines():
        if not line.strip():
            continue
        label, _, url = line.partition("|")
        url = url.strip() or label.strip()
        if not re.match(r"^https?://", url):
            raise ValidationFailed(f"Resource link '{line.strip()}' must be 'Label | https://…'.")
        links.append({"label": label.strip() if _ else url, "url": url})
    title = (form.get("calendar_event_title") or "").strip() or "{event_name} with {invitee_name}"
    unknown = set(re.findall(r"\{(\w+)\}", title)) - TITLE_VARS
    if unknown:
        raise ValidationFailed(f"Unknown variable(s) in event title: {', '.join(sorted(unknown))}.")
    ch.set("confirmation_json", {"message": (form.get("confirmation_message") or "").strip(),
                                 "redirect_url": redirect_url, "pass_params": _bool(form, "pass_params"),
                                 "links": links}, "confirmation_message", "redirect_url")
    ch.set("calendar_event_title", title[:300])


def parse_policy(db, ctx, ev, form, ch: Changes):
    if _bool(form, "requires_email_verification") and not ctx.feature("email_verification"):
        raise ValidationFailed("Invitee email verification is not included in your plan.")
    ch.set("cancellation_policy_json", {
        "allow_cancel": _bool(form, "allow_cancel"), "allow_reschedule": _bool(form, "allow_reschedule"),
        "min_notice_minutes": _int(form.get("policy_min_notice"), 0, 0),
        "require_reason": _bool(form, "require_reason"), "host_approval": _bool(form, "host_approval"),
        "policy_text": (form.get("policy_text") or "").strip(),
    }, "policy_min_notice", "policy_text")
    ch.set("requires_email_verification", _bool(form, "requires_email_verification"), "policy_min_notice")


def parse_payment(db, ctx, ev, form, ch: Changes):
    if not ctx.feature("payments"):
        raise ValidationFailed("Payments are not included in your plan.")
    if not _bool(form, "payment_enabled"):
        ch.set("price_minor", None, "price", "payment_enabled")
        ch.set("currency", None, "currency")
        ch.set("payment_provider", None, "payment_provider")
        ch.set("payment_description", None, "payment_description")
        return
    try:
        price = round(float(form.get("price") or "0") * 100)
    except ValueError:
        raise ValidationFailed("Price must be a number.")
    if price <= 0:
        raise ValidationFailed("Price must be greater than zero.")
    cur = (form.get("currency") or "USD").upper()
    if cur not in CURRENCIES:
        raise ValidationFailed("Unsupported currency.")
    prov = form.get("payment_provider") or "stripe"
    if prov not in ("stripe", "paypal"):
        raise ValidationFailed("Unsupported payment provider.")
    ch.set("price_minor", price, "price")
    ch.set("currency", cur)
    ch.set("payment_provider", prov)
    ch.set("payment_description", (form.get("payment_description") or "").strip()[:300] or None)


def parse_permissions(db, ctx, ev, form, ch: Changes):
    vis = form.get("visibility") or "public"
    if vis not in ("public", "secret", "private", "team"):
        raise ValidationFailed("Unknown visibility.")
    members = {u.id for u in _members(db, ctx)}
    shared = [u for u in form.getlist("shared_with") if u in members and u != ev.owner_user_id]
    ch.set("visibility", vis)
    ch.set("shared_with_json", shared or None, "visibility")


PARSERS = {"basics": parse_basics, "location": parse_location, "availability": parse_availability,
           "hosts": parse_hosts, "confirmation": parse_confirmation, "policy": parse_policy,
           "payment": parse_payment, "permissions": parse_permissions}


# ================================================================ list
def _visible_events_query(ctx: Ctx):
    q = select(EventType).where(EventType.workspace_id == ctx.workspace.id, EventType.deleted_at.is_(None))
    if not ctx.can("admin"):
        hosted = select(EventTypeHost.event_type_id).where(EventTypeHost.user_id == ctx.user.id)
        q = q.where(or_(EventType.owner_user_id == ctx.user.id, EventType.id.in_(hosted),
                        EventType.visibility == "team"))
    return q


@router.get("/app/event-types")
def list_event_types(request: Request, tab: str = "active", ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    rows = db.scalars(_visible_events_query(ctx).order_by(EventType.created_at)).all()
    # shared_with is JSON -> filter in Python for portability
    if not ctx.can("admin"):
        extra = db.scalars(select(EventType).where(EventType.workspace_id == ctx.workspace.id,
                                                   EventType.deleted_at.is_(None))).all()
        ids = {r.id for r in rows}
        rows += [e for e in extra if e.id not in ids and ctx.user.id in (e.shared_with_json or [])]
    regular = [e for e in rows if not e.is_one_off and not e.single_use]
    shown = [e for e in regular if (e.status == "archived") == (tab == "archived")]
    one_offs = [e for e in rows if e.is_one_off]
    single_use = [e for e in rows if e.single_use]
    owners = {u.id: u for u in db.scalars(select(User).where(User.id.in_({e.owner_user_id for e in rows} or {""})))}
    urls = {e.id: event_public_url(db, e) for e in rows}
    snippets = {e.id: embed_snippets(urls[e.id], e.color) for e in shown}
    counts = dict(db.execute(select(Booking.event_type_id, func.count(Booking.id)).where(
        Booking.workspace_id == ctx.workspace.id, Booking.status == "confirmed", Booking.start_at >= utcnow()
    ).group_by(Booking.event_type_id)).all())
    cap = limit(ctx.workspace, "max_active_event_types")
    return render(request, "app/event_types/list.html", ctx, nav="event_types", events=shown, tab=tab,
                  one_offs=one_offs, single_use=single_use, owners=owners, urls=urls, snippets=snippets,
                  kinds=KINDS, upcoming_counts=counts, cap=cap, active_count=active_event_count(db, ctx),
                  host_public=is_host_public(ctx.user), can_edit=lambda e: can_edit_event(ctx, e),
                  archived_count=sum(1 for e in regular if e.status == "archived"))


# ================================================================ create
@router.get("/app/event-types/new")
def new_event_type(request: Request, kind: str = "one_on_one", team_id: str = "", ctx: Ctx = Depends(get_ctx)):
    if ctx.role == "viewer":
        raise Forbidden("Viewers cannot create event types.")
    if team_id and kind == "one_on_one":
        kind = "round_robin" if ctx.feature("round_robin") else kind
    return render(request, "app/event_types/new.html", ctx, nav="event_types", kinds=KINDS, kind=kind,
                  kind_feature=KIND_FEATURE, location_types=LOCATION_TYPES, error=None, form={"team_id": team_id})


@router.post("/app/event-types")
async def create_event_type(request: Request, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    if ctx.role == "viewer":
        raise Forbidden("Viewers cannot create event types.")
    form = await request.form()
    name = (form.get("name") or "").strip()
    err = None
    duration = 30
    try:
        duration = _int(form.get("duration"), 30, 5, 1440)
    except ValidationFailed as e:
        err = str(e)
    if not name:
        err = "Name is required."
    ev = None
    if not err:
        loc = form.get("location") or "google_meet"
        ev, err = create_event_type_basic(db, ctx, name=name, duration=duration, kind=form.get("kind") or "one_on_one",
                                          slug=form.get("slug") or None, color=form.get("color") or "#3b47d6",
                                          description=(form.get("description") or "").strip(),
                                          locations=[{"type": loc, "value": (form.get("location_value") or "").strip()}])
    if err:
        return render(request, "app/event_types/new.html", ctx, nav="event_types", kinds=KINDS,
                      kind=form.get("kind") or "one_on_one", kind_feature=KIND_FEATURE, location_types=LOCATION_TYPES,
                      error=err, form=dict(form), status_code=400)
    team_id = form.get("team_id") or None
    if team_id:
        team = db.get(Team, team_id)
        if team and team.workspace_id == ctx.workspace.id:
            ev.team_id = team.id
    db.commit()
    flash(request, f"“{ev.name}” created. Fine-tune it below or share the link.")
    return redirect(f"/app/event-types/{ev.id}/edit")


# ================================================================ one-off meetings
@router.get("/app/event-types/one-off/new")
def one_off_new(request: Request, ctx: Ctx = Depends(get_ctx)):
    if not ctx.feature("one_off"):
        raise Forbidden("One-off meetings are not included in your plan.")
    return render(request, "app/event_types/one_off.html", ctx, nav="event_types", location_types=LOCATION_TYPES,
                  error=None, form={})


@router.post("/app/event-types/one-off")
async def one_off_create(request: Request, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    if not ctx.feature("one_off"):
        raise Forbidden("One-off meetings are not included in your plan.")
    form = await request.form()
    name = (form.get("name") or "").strip() or "Meeting"
    err = None
    tz = ZoneInfo(ctx.user.default_timezone or "UTC")
    starts = []
    try:
        duration = _int(form.get("duration"), 30, 5, 1440)
        for raw in form.getlist("slots"):
            raw = (raw or "").strip()
            if not raw:
                continue
            local = dt.datetime.fromisoformat(raw).replace(tzinfo=tz)
            utc = local.astimezone(dt.timezone.utc).replace(tzinfo=None)
            if utc <= utcnow():
                raise ValidationFailed(f"{raw} is in the past.")
            starts.append(utc)
        if not starts:
            raise ValidationFailed("Add at least one time slot.")
    except ValueError:
        err = "Invalid date/time."
    except ValidationFailed as e:
        err = str(e)
    loc = form.get("location") or "google_meet"
    if not err and loc not in LOCATION_TYPES:
        err = "Unknown location."
    if err:
        return render(request, "app/event_types/one_off.html", ctx, nav="event_types", location_types=LOCATION_TYPES,
                      error=err, form=dict(form), status_code=400)
    ev = EventType(
        workspace_id=ctx.workspace.id, owner_user_id=ctx.user.id, name=name[:200],
        slug=unique_event_slug(db, ctx.user.id, f"{slugify(name)[:40]}-{secrets.token_hex(3)}"),
        description=(form.get("description") or "").strip() or None, duration=duration, kind="one_on_one",
        location_json=[{"type": loc, "value": (form.get("location_value") or "").strip()}], visibility="secret",
        is_one_off=True, one_off_slots_json=sorted({s.isoformat() for s in starts}), min_notice_minutes=0,
        max_days_ahead=730, color="#0f766e",
        cancellation_policy_json={"allow_cancel": True, "allow_reschedule": True, "min_notice_minutes": 0,
                                  "require_reason": False, "host_approval": False, "policy_text": ""},
    )
    db.add(ev)
    db.flush()
    audit(db, "event_type.created", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="event_type",
          entity_id=ev.id, metadata={"one_off": True})
    db.commit()
    flash(request, f"One-off meeting created. Share this link: {event_public_url(db, ev)}")
    return redirect("/app/event-types#one-off")


# ================================================================ editor
def _editor_context(db: Session, ctx: Ctx, ev: EventType, section: str) -> dict:
    owner = db.get(User, ev.owner_user_id)
    schedules = db.scalars(select(AvailabilitySchedule).where(
        AvailabilitySchedule.workspace_id == ctx.workspace.id, AvailabilitySchedule.user_id == ev.owner_user_id
    ).order_by(AvailabilitySchedule.created_at)).all()
    custom = {wd: [] for wd in range(7)}
    for r in ev.custom_rules_json or []:
        custom.setdefault(int(r.get("weekday", 0)), []).append(f"{r.get('start')}-{r.get('end')}")
    notice = ev.min_notice_minutes or 0
    if notice and notice % 1440 == 0:
        notice_val, notice_unit = notice // 1440, "days"
    elif notice and notice % 60 == 0:
        notice_val, notice_unit = notice // 60, "hours"
    else:
        notice_val, notice_unit = notice, "minutes"
    teams = db.scalars(select(Team).where(Team.workspace_id == ctx.workspace.id).order_by(Team.name)).all()
    url = event_public_url(db, ev)
    return dict(
        nav="event_types", ev=ev, owner=owner, section=section, sections=SECTIONS, section_labels=SECTION_LABELS,
        kinds=KINDS, kind_feature=KIND_FEATURE, location_types=LOCATION_TYPES, question_types=QUESTION_TYPES,
        option_types=OPTION_TYPES, schedules=schedules, custom={k: ", ".join(v) for k, v in custom.items()},
        weekdays=WEEKDAYS, notice_val=notice_val, notice_unit=notice_unit, members=_members(db, ctx),
        hosts={h.user_id: h for h in ev.hosts}, teams=teams, currencies=CURRENCIES, url=url,
        path=event_public_path(db, ev), snippets=embed_snippets(url, ev.color),
        locked=lambda f: is_locked(ctx, ev, LOCK_ALIASES.get(f, f)), policy=ev.cancellation_policy_json or {},
        confirmation=ev.confirmation_json or {}, title_vars=sorted(TITLE_VARS),
    )


@router.get("/app/event-types/{event_id}/edit")
def edit_event_type(request: Request, event_id: str, section: str = "basics", ctx: Ctx = Depends(get_ctx),
                    db: Session = Depends(get_db)):
    ev = get_event_or_404(db, ctx, event_id)
    section = section if section in SECTIONS else "basics"
    return render(request, "app/event_types/edit.html", ctx, **_editor_context(db, ctx, ev, section))


@router.post("/app/event-types/{event_id}")
async def save_event_type(request: Request, event_id: str, section: str = "basics", ctx: Ctx = Depends(get_ctx),
                          db: Session = Depends(get_db)):
    ev = get_event_or_404(db, ctx, event_id)
    form = await request.form()
    section = form.get("section") or section
    if section not in PARSERS:
        raise HTTPException(400, "Unknown section")
    ch = Changes(form)
    try:
        PARSERS[section](db, ctx, ev, form, ch)
        changed = ch.apply(ctx, ev)
        if section == "hosts" and save_hosts(db, ctx, ev, form):
            changed.append("hosts")
    except ValidationFailed as e:
        db.rollback()
        flash(request, str(e), "error")
        return redirect(f"/app/event-types/{ev.id}/edit?section={section}")
    except LockedField as e:
        db.rollback()
        raise Forbidden(f"The field '{e}' is locked by your workspace admin (managed event type).")
    if changed:
        ev.version = (ev.version or 1) + 1
        audit(db, "event_type.updated", workspace_id=ctx.workspace.id, actor_id=ctx.user.id,
              entity_type="event_type", entity_id=ev.id, metadata={"fields": changed, "version": ev.version})
        db.commit()
        flash(request, "Changes saved. Existing bookings keep the details they were booked with.")
    else:
        flash(request, "No changes to save.", "info")
    return redirect(f"/app/event-types/{ev.id}/edit?section={section}")


# ================================================================ questions
def _question_from_form(form) -> dict:
    qtype = form.get("type") or "text"
    if qtype not in QUESTION_TYPES:
        raise ValidationFailed("Unknown question type.")
    label = (form.get("label") or "").strip()
    if not label:
        raise ValidationFailed("Question label is required.")
    options = [o.strip() for o in (form.get("options") or "").splitlines() if o.strip()]
    if qtype in OPTION_TYPES and not options:
        raise ValidationFailed("Add at least one option (one per line).")
    prefill = (form.get("prefill_key") or "").strip() or None
    if prefill and not re.fullmatch(r"[A-Za-z0-9_]{1,60}", prefill):
        raise ValidationFailed("Prefill key may contain letters, numbers and underscores only.")
    return dict(type=qtype, label=label[:300], help_text=(form.get("help_text") or "").strip()[:500] or None,
                required=_bool(form, "required"), options_json=options if qtype in OPTION_TYPES else None,
                prefill_key=prefill)


def _q_guard(ctx, ev):
    if is_locked(ctx, ev, "questions"):
        raise Forbidden("Questions are locked by your workspace admin (managed event type).")


def _bump(db, ctx, ev, what):
    ev.version = (ev.version or 1) + 1
    audit(db, "event_type.updated", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="event_type",
          entity_id=ev.id, metadata={"fields": [what], "version": ev.version})


@router.post("/app/event-types/{event_id}/questions")
async def add_question(request: Request, event_id: str, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    ev = get_event_or_404(db, ctx, event_id)
    _q_guard(ctx, ev)
    try:
        data = _question_from_form(await request.form())
    except ValidationFailed as e:
        flash(request, str(e), "error")
        return redirect(f"/app/event-types/{ev.id}/edit?section=questions")
    order = max([q.sort_order for q in ev.questions] or [0]) + 1
    ev.questions.append(EventQuestion(event_type_id=ev.id, sort_order=order, **data))
    _bump(db, ctx, ev, "questions")
    db.commit()
    flash(request, "Question added.")
    return redirect(f"/app/event-types/{ev.id}/edit?section=questions")


def _get_q(ev: EventType, qid: str) -> EventQuestion:
    q = next((q for q in ev.questions if q.id == qid), None)
    if not q:
        raise HTTPException(404, "Question not found")
    return q


@router.post("/app/event-types/{event_id}/questions/{qid}")
async def update_question(request: Request, event_id: str, qid: str, ctx: Ctx = Depends(get_ctx),
                          db: Session = Depends(get_db)):
    ev = get_event_or_404(db, ctx, event_id)
    _q_guard(ctx, ev)
    q = _get_q(ev, qid)
    try:
        for k, v in _question_from_form(await request.form()).items():
            setattr(q, k, v)
    except ValidationFailed as e:
        db.rollback()
        flash(request, str(e), "error")
        return redirect(f"/app/event-types/{ev.id}/edit?section=questions")
    _bump(db, ctx, ev, "questions")
    db.commit()
    flash(request, "Question updated.")
    return redirect(f"/app/event-types/{ev.id}/edit?section=questions")


@router.post("/app/event-types/{event_id}/questions/{qid}/delete")
def delete_question(request: Request, event_id: str, qid: str, ctx: Ctx = Depends(get_ctx),
                    db: Session = Depends(get_db)):
    ev = get_event_or_404(db, ctx, event_id)
    _q_guard(ctx, ev)
    ev.questions.remove(_get_q(ev, qid))
    _bump(db, ctx, ev, "questions")
    db.commit()
    flash(request, "Question removed.")
    return redirect(f"/app/event-types/{ev.id}/edit?section=questions")


@router.post("/app/event-types/{event_id}/questions/{qid}/move")
def move_question(request: Request, event_id: str, qid: str, dir: str = "up", ctx: Ctx = Depends(get_ctx),
                  db: Session = Depends(get_db)):
    ev = get_event_or_404(db, ctx, event_id)
    _q_guard(ctx, ev)
    qs = sorted(ev.questions, key=lambda q: (q.sort_order, q.id))
    q = _get_q(ev, qid)
    i = qs.index(q)
    j = i - 1 if dir == "up" else i + 1
    if 0 <= j < len(qs):
        qs[i], qs[j] = qs[j], qs[i]
    for n, item in enumerate(qs):
        item.sort_order = n + 1
    _bump(db, ctx, ev, "questions")
    db.commit()
    return redirect(f"/app/event-types/{ev.id}/edit?section=questions")


# ================================================================ lifecycle actions
@router.post("/app/event-types/{event_id}/duplicate")
def duplicate_event_type(request: Request, event_id: str, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    src = get_event_or_404(db, ctx, event_id)
    status = "active" if can_activate_another(db, ctx) else "paused"
    owner_id = src.owner_user_id if src.team_id else ctx.user.id
    ev = clone_event(db, src, name=f"{src.name} (copy)"[:200], owner_user_id=owner_id,
                     slug=unique_event_slug(db, owner_id, f"{src.slug}-copy"), status=status,
                     schedule_id=src.schedule_id if owner_id == src.owner_user_id else None,
                     is_managed_template=False, single_use=False)
    audit(db, "event_type.created", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="event_type",
          entity_id=ev.id, metadata={"duplicated_from": src.id})
    db.commit()
    flash(request, f"Duplicated as “{ev.name}”." + (" It is paused because your plan limit was reached." if status == "paused" else ""))
    return redirect(f"/app/event-types/{ev.id}/edit")


@router.post("/app/event-types/{event_id}/status")
async def set_status(request: Request, event_id: str, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    ev = get_event_or_404(db, ctx, event_id)
    form = await request.form()
    action = form.get("action") or request.query_params.get("action")
    new = {"pause": "paused", "activate": "active", "archive": "archived", "unarchive": "paused"}.get(action)
    if not new:
        raise HTTPException(400, "Unknown action")
    if is_locked(ctx, ev, "status"):
        raise Forbidden("Status is locked by your workspace admin.")
    if new == "active" and ev.status != "active" and not ev.is_one_off and not ev.single_use and not can_activate_another(db, ctx):
        flash(request, f"Your plan allows {limit(ctx.workspace, 'max_active_event_types')} active event type(s). "
                       "Pause another one or upgrade in Settings › Billing.", "error")
        return redirect("/app/event-types")
    old = ev.status
    ev.status = new
    audit(db, "event_type.updated", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="event_type",
          entity_id=ev.id, metadata={"status": [old, new]})
    db.commit()
    flash(request, f"“{ev.name}” is now {new}.")
    return redirect(form.get("next") or ("/app/event-types?tab=archived" if new == "archived" else "/app/event-types"))


@router.post("/app/event-types/{event_id}/delete")
def delete_event_type(request: Request, event_id: str, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    ev = get_event_or_404(db, ctx, event_id)
    if ev.locked_fields_json and not ctx.can("admin"):
        raise Forbidden("Managed event types can only be deleted by an admin.")
    ev.deleted_at = utcnow()
    ev.status = "archived"
    ev.slug = f"{ev.slug[:90]}-deleted-{secrets.token_hex(3)}"  # free the slug for reuse
    audit(db, "event_type.deleted", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="event_type",
          entity_id=ev.id, metadata={"name": ev.name})
    db.commit()
    flash(request, f"“{ev.name}” deleted. Existing bookings are not affected.")
    return redirect("/app/event-types")


@router.post("/app/event-types/{event_id}/single-use")
def create_single_use(request: Request, event_id: str, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    if not ctx.feature("single_use"):
        raise Forbidden("Single-use links are not included in your plan.")
    src = get_event_or_404(db, ctx, event_id)
    slug = f"su-{secrets.token_hex(4)}"
    while slug_taken(db, src.owner_user_id, slug):
        slug = f"su-{secrets.token_hex(4)}"
    ev = clone_event(db, src, slug=slug, visibility="secret", single_use=True, status="active",
                     is_managed_template=False, is_one_off=False, managed_template_id=src.id)
    audit(db, "event_type.created", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="event_type",
          entity_id=ev.id, metadata={"single_use_of": src.id})
    db.commit()
    url = event_public_url(db, ev)
    if "application/json" in request.headers.get("accept", ""):
        from fastapi.responses import JSONResponse

        return JSONResponse({"id": ev.id, "url": url})
    flash(request, f"Single-use link created: {url}")
    return redirect("/app/event-types#single-use")


@router.get("/app/event-types/{event_id}/embed")
def embed_codes(event_id: str, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    ev = get_event_or_404(db, ctx, event_id, edit=False)
    url = event_public_url(db, ev)
    return {"url": url, **embed_snippets(url, ev.color)}
