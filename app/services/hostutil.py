"""Host-side helpers shared by the host app and the public booking pages.

PUBLIC CONTRACT (other modules may import):
    is_host_public(user) -> bool        # public booking links only work for verified, active hosts
    event_public_path(db, event) -> str  # "/alice/intro" or "/team/sales/demo"
    event_public_url(db, event) -> str   # absolute URL using settings.base_url
    embed_snippets(url, color) -> dict   # {"inline","popup_widget","popup_text"}
    visible_bookings_filter(ctx)         # SQLAlchemy where-clause for bookings the ctx may see
    can_view_booking(db, ctx, booking) / can_edit_event(ctx, event) / can_view_event(db, ctx, event)
"""
from __future__ import annotations

import html
import re
from functools import lru_cache
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Booking, BookingHost, EventType, EventTypeHost, Team, User

HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$|^24:00$")


def is_host_public(user: Optional[User]) -> bool:
    """A host's public pages are live only once their email is verified and the account is active."""
    return bool(user and not user.deleted_at and user.email_verified_at is not None)


def event_public_path(db: Session, ev: EventType) -> str:
    if ev.team_id:
        team = db.get(Team, ev.team_id)
        if team:
            return f"/team/{team.slug}/{ev.slug}"
    owner = db.get(User, ev.owner_user_id)
    return f"/{owner.slug if owner else 'unknown'}/{ev.slug}"


def event_public_url(db: Session, ev: EventType) -> str:
    return settings.base_url.rstrip("/") + event_public_path(db, ev)


def embed_snippets(url: str, color: str = "#3b47d6", text: str = "Schedule time with me") -> dict:
    base = settings.base_url.rstrip("/")
    u = html.escape(url, quote=True)
    c = html.escape(color or "#3b47d6", quote=True)
    t = html.escape(text, quote=True)
    script = f'<script src="{base}/embed.js" async></script>'
    return {
        "inline": f'<!-- {settings.app_name} inline widget -->\n<div class="aal-inline-widget" data-url="{u}" '
        f'style="min-width:320px;height:700px;"></div>\n{script}',
        "popup_widget": f'<!-- {settings.app_name} popup widget -->\n{script}\n<script>\nwindow.addEventListener("load", function () {{\n'
        f'  AAL.initPopupWidget({{ url: "{u}", text: "{t}", color: "{c}" }});\n}});\n</script>',
        "popup_text": f'<!-- {settings.app_name} popup text link -->\n{script}\n'
        f'<a href="" onclick="AAL.initPopupText({{ url: \'{u}\' }});return false;">{t}</a>',
    }


# ---------------------------------------------------------------- permissions
def visible_bookings_filter(ctx):
    """Members see bookings they host; admins/owners see every booking in the workspace."""
    if ctx.can("admin"):
        return Booking.workspace_id == ctx.workspace.id
    hosted = select(BookingHost.booking_id).where(BookingHost.user_id == ctx.user.id)
    return (Booking.workspace_id == ctx.workspace.id) & or_(Booking.host_id == ctx.user.id, Booking.id.in_(hosted))


def can_view_booking(db: Session, ctx, b: Optional[Booking]) -> bool:
    if not b or b.workspace_id != ctx.workspace.id:
        return False
    if ctx.can("admin") or b.host_id == ctx.user.id:
        return True
    return bool(db.scalar(select(BookingHost.id).where(BookingHost.booking_id == b.id, BookingHost.user_id == ctx.user.id)))


def can_edit_event(ctx, ev: EventType) -> bool:
    if ev.workspace_id != ctx.workspace.id or ev.deleted_at:
        return False
    if ctx.role == "viewer":
        return False
    return ev.owner_user_id == ctx.user.id or ctx.can("admin") or ctx.user.id in (ev.shared_with_json or [])


def can_view_event(db: Session, ctx, ev: Optional[EventType]) -> bool:
    if not ev or ev.workspace_id != ctx.workspace.id or ev.deleted_at:
        return False
    if can_edit_event(ctx, ev) or ev.visibility == "team":
        return True
    return bool(
        db.scalar(select(EventTypeHost.id).where(EventTypeHost.event_type_id == ev.id, EventTypeHost.user_id == ctx.user.id))
    )


# ---------------------------------------------------------------- misc
@lru_cache
def all_timezones() -> list[str]:
    from zoneinfo import available_timezones

    return sorted(z for z in available_timezones() if "/" in z or z == "UTC")


def valid_tz(tz: Optional[str]) -> bool:
    if not tz:
        return False
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(tz)
        return True
    except Exception:
        return False


def parse_ranges(pairs: list[tuple[str, str]]) -> tuple[list[dict], Optional[str]]:
    """Validate [(start,end)] HH:MM pairs: well-formed, start<end, no overlap. Returns (sorted ranges, error)."""
    out = []
    for s, e in pairs:
        s, e = (s or "").strip(), (e or "").strip()
        if not s and not e:
            continue
        if not HHMM.match(s) or not HHMM.match(e):
            return [], f"Invalid time '{s}–{e}'. Use HH:MM."
        if s == "24:00" or s >= e:
            return [], f"Start time must be before end time ({s}–{e})."
        out.append({"start": s, "end": e})
    out.sort(key=lambda r: r["start"])
    for a, b in zip(out, out[1:]):
        if b["start"] < a["end"]:
            return [], f"Time ranges overlap ({a['start']}–{a['end']} and {b['start']}–{b['end']})."
    return out, None


def parse_range_text(text: str) -> tuple[list[dict], Optional[str]]:
    """Parse '09:00-12:00, 13:00-17:00' into validated ranges."""
    pairs = []
    for part in re.split(r"[,;]", text or ""):
        part = part.strip()
        if not part:
            continue
        bits = re.split(r"\s*[-–]\s*", part)
        if len(bits) != 2:
            return [], f"Could not read '{part}'. Use e.g. 09:00-17:00."
        pairs.append((bits[0], bits[1]))
    return parse_ranges(pairs)
