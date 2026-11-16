"""Public booking pages (PRD 4.1, 8, 15.4). Included LAST in main.py (catch-all /{username}).

GET  /team/{team_slug}                     team page
GET  /team/{team_slug}/{event_slug}        team booking page
GET  /booking/{token}                      manage page
GET  /booking/{token}/confirmed            confirmation page (optional redirect)
GET  /booking/{token}/ics                  .ics download
GET  /booking/{token}/cancel  + POST       cancel form
GET  /booking/{token}/reschedule  + POST   reschedule (same calendar UI; JS posts to the JSON API, no-JS posts the form)
POST /book/{event_type_id}                 no-JS booking form submit (same rules as POST /api/v1/public/bookings)
GET  /{username}                           public profile
GET  /{username}/{event_slug}              booking page

No-JS fallback: booking pages are fully server-rendered. ?month=YYYY-MM&date=YYYY-MM-DD&tz=Area/City
&duration=N&slot=<UTC ISO> select month/day/time with plain links and GET forms; the details form is a
normal POST. booking.js progressively enhances the same markup (in-page month/day/slot switching,
keyboard date grid, inline validation) when JavaScript runs.

Embed mode: ?embed=1 (+ hide_details=1, primary_color, bg, text, radius) sets
Content-Security-Policy: frame-ancestors <settings.embed_allowed_origins>.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Optional
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.deps import flash, redirect, render
from app.entitlements import has_feature
from app.errors import APIError
from app.i18n import format_date, get_locale, status_label, t
from app.models import Booking, EventType, PageView, Team, TeamMember, User, Workspace
from app.ratelimit import client_ip, enforce
from app.routers.public_api import (
    UTM_KEYS,
    availability_payload,
    booking_response,
    event_public_dict,
    find_event,
    issue_verification_code,
    public_location_options,
    verify_code,
)
from app.security import opaque_token
from app.services import availability as av
from app.services import booking as bk
from app.services import ics
from app.services import timeutil as tu

router = APIRouter(include_in_schema=False)

HEX_RE = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


# ------------------------------------------------------------------ helpers
def _hex(v: Optional[str]) -> Optional[str]:
    if v and HEX_RE.match(v.strip()):
        v = v.strip()
        return v if v.startswith("#") else "#" + v
    return None


def _luminance(hex6: str) -> float:
    def ch(c: int) -> float:
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex6[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def accessible_color(color: Optional[str]) -> Optional[str]:
    """Darken a brand colour until it has >= 4.5:1 contrast with white (WCAG 2.2 AA), since the
    booking page uses it both as button background (white text) and as text on light surfaces."""
    if not color:
        return color
    h = color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    h = h[:6]
    if len(h) != 6:
        return color
    rgb = [int(h[i:i + 2], 16) for i in (0, 2, 4)]
    for _ in range(40):
        lum = _luminance("".join(f"{c:02x}" for c in rgb))
        if 1.05 / (lum + 0.05) >= 4.5:
            break
        rgb = [int(c * 0.9) for c in rgb]
    out = "#" + "".join(f"{c:02x}" for c in rgb)
    return color if out.lower() == "#" + h.lower() and len(color.lstrip("#")) in (3, 6) else out


def _embed_ctx(request: Request) -> dict:
    q = request.query_params
    embedded = q.get("embed") == "1"
    theme = {}
    if embedded:
        for key, var in (("primary_color", "--primary"), ("bg", "--bg"), ("text", "--text")):
            c = _hex(q.get(key))
            if c:
                theme[var] = accessible_color(c) if var == "--primary" else c
        r = q.get("radius")
        if r and r.isdigit() and int(r) <= 40:
            theme["--radius"] = f"{int(r)}px"
            theme["--radius-sm"] = f"{max(0, int(r) - 4)}px"
    return {
        "embedded": embedded,
        "embed_theme": theme,
        "hide_details": embedded and q.get("hide_details") == "1",
        "embed_qs": "embed=1&" + urlencode({k: v for k, v in q.items() if k in ("hide_details", "primary_color", "bg", "text", "radius")}) if embedded else "",
    }


def _branding(db: Session, owner: Optional[User], workspace: Optional[Workspace], request: Request) -> dict:
    b = dict((workspace.branding_json if workspace else None) or {})
    b.update({k: v for k, v in ((owner.branding_json if owner else None) or {}).items() if v not in (None, "")})
    color = _hex(b.get("primary"))
    hide = bool(b.get("hide_branding")) and bool(workspace) and has_feature(workspace, "custom_branding")
    emb = _embed_ctx(request)
    if emb["embed_theme"].get("--primary"):
        color = emb["embed_theme"]["--primary"]
    color = accessible_color(color)
    return {"brand_color": color, "hide_branding": hide, "locale": get_locale(request, owner.locale if owner else None),
            **emb}


def _with_embed_headers(request: Request, resp: Response) -> Response:
    if request.query_params.get("embed") == "1":
        origins = " ".join(o.strip() for o in (settings.embed_allowed_origins or "*").split(",") if o.strip()) or "*"
        resp.headers["Content-Security-Policy"] = f"frame-ancestors {origins}"
    return resp


def _page(request: Request, template: str, status_code: int = 200, **context) -> Response:
    # Every invitee page gets a locale (?lang= > lang cookie > host locale > Accept-Language > en);
    # the `lang` cookie for an explicit ?lang= is set by the security-headers middleware.
    if not context.get("locale"):
        context["locale"] = get_locale(request)
    resp = render(request, template, status_code=status_code, **context)
    return _with_embed_headers(request, resp)


def _utm(request: Request) -> dict:
    return {k: request.query_params[k][:200] for k in UTM_KEYS if request.query_params.get(k)}


def _record_view(db: Session, workspace_id: str, page: str, event_type_id: Optional[str], utm: dict):
    try:
        db.add(PageView(workspace_id=workspace_id, event_type_id=event_type_id, page=page, utm_json=utm or None))
        db.commit()
    except Exception:
        db.rollback()


def _listable(et: EventType) -> bool:
    return (
        et.status == "active" and not et.deleted_at and et.visibility in ("public", "team") and not et.is_one_off
        and not et.single_use and not et.is_managed_template
    )


def _not_found(request: Request, message_key: str = "err.page_missing") -> Response:
    loc = get_locale(request)
    return _page(request, "public/unavailable.html", status_code=404, title=t("err.not_available", loc),
                 message=t(message_key, loc), locale=loc, **_embed_ctx(request))


def _prefill(request: Request, et: EventType) -> dict:
    q = request.query_params
    answers = {}
    for idx, question in enumerate(sorted(et.questions, key=lambda x: x.sort_order or 0), start=1):
        for key in filter(None, (question.prefill_key, f"a{idx}")):
            if q.get(key):
                answers[question.id] = q.get(key)[:2000]
                break
    return {
        "name": (q.get("name") or "")[:200],
        "email": (q.get("email") or "")[:320],
        "phone": (q.get("phone") or "")[:50],
        "company": (q.get("company") or "")[:200],
        "guests": (q.get("guests") or "")[:1000],
        "answers": answers,
    }


def _hosts_display(db: Session, et: EventType, owner: Optional[User]) -> list[User]:
    if et.kind in ("collective", "round_robin") and et.hosts:
        users = [db.get(User, h.user_id) for h in et.hosts if h.active]
        return [u for u in users if u and not u.deleted_at]
    return [owner] if owner else []


def _booking_page(request: Request, db: Session, et: EventType, owner: Optional[User], team: Optional[Team],
                  owner_slug: str, mode: str = "book", booking: Optional[Booking] = None, *,
                  params: Optional[dict] = None, prefill: Optional[dict] = None, form_error: Optional[str] = None,
                  field_errors: Optional[dict] = None, notice: Optional[str] = None, status_code: int = 200) -> Response:
    """Booking page. `params` (default: the query string) drives the server-rendered, no-JS picker:
    ?month=YYYY-MM&date=YYYY-MM-DD&tz=Area/City&duration=N&slot=<UTC ISO>. booking.js enhances it."""
    ws = db.get(Workspace, et.workspace_id)
    brand = _branding(db, owner, ws, request)
    loc = brand["locale"]
    record_view = mode == "book" and params is None
    if mode == "book":
        if et.status != "active":
            return _page(request, "public/unavailable.html", status_code=404, title=t("err.not_available_now", loc),
                         message=t("err.event_paused", loc, name=et.name),
                         back_url=f"/{owner.slug}" if owner and not team else (f"/team/{team.slug}" if team else None), **brand)
        if et.single_use and et.used_at:
            return _page(request, "public/unavailable.html", status_code=410, title=t("err.link_used_title", loc),
                         message=t("err.link_used", loc), **brand)
        if record_view:
            _record_view(db, et.workspace_id, "event", et.id, _utm(request))
    q = params if params is not None else request.query_params
    durations = av.allowed_durations(et)
    try:
        sel_duration = av.resolve_duration(et, int(q.get("duration"))) if (q.get("duration") or "").isdigit() else et.duration
    except ValueError:
        sel_duration = et.duration
    if booking is not None:
        sel_duration = int((booking.end_at - booking.start_at).total_seconds() // 60)
    host_tz = None
    if owner:
        host_tz = owner.default_timezone
    info = event_public_dict(db, et, owner, team)
    q_tz = q.get("tz") or q.get("timezone")
    ssr = _ssr_picker(request, db, et, q, tu.validate_tz(q_tz, "") or tu.validate_tz(host_tz), sel_duration, mode, booking,
                     loc)
    prefill = prefill if prefill is not None else _prefill(request, et)
    config = {
        "owner_slug": owner_slug,
        "event_slug": et.slug,
        "event_type_id": et.id,
        "mode": mode,
        "manage_token": booking.manage_token if booking else None,
        "current_start": tu.iso_z(booking.start_at) if booking else None,
        "durations": durations,
        "duration": sel_duration,
        "kind": et.kind,
        "show_seats": bool(et.show_remaining_seats),
        "waitlist": bool(et.waitlist_enabled and et.kind == "group"),
        "requires_verification": bool(et.requires_email_verification),
        "embed": brand["embedded"],
        "embed_qs": brand["embed_qs"],
        "utm": _utm(request),
        "rsid": (q.get("rsid") or "")[:40] or None,
        "prefill": prefill,
        "host_tz": host_tz,
        "tz": q_tz if tu.is_valid_tz(q_tz) else None,
        "selected_slot": ssr["slot"],
        "month": q.get("month") if re.match(r"^\d{4}-\d{2}$", q.get("month") or "") else None,
        "date": q.get("date") if re.match(r"^\d{4}-\d{2}-\d{2}$", q.get("date") or "") else None,
        "paid": bool(et.price_minor),
        "questions": info["questions"],
        "locations": info["locations"],
    }
    post_url = (f"/booking/{booking.manage_token}/reschedule" if booking is not None else f"/book/{et.id}")
    if brand["embedded"]:
        post_url += "?" + brand["embed_qs"]
    return _page(
        request, "public/booking.html", status_code=status_code, et=et, owner=owner, team=team, info=info, config=config,
        mode=mode, booking=booking,
        hosts=_hosts_display(db, et, owner), durations=durations, duration=sel_duration,
        locations=public_location_options(et), questions=sorted(et.questions, key=lambda x: x.sort_order or 0),
        policy=et.cancellation_policy_json or {}, timezones=tu.all_timezones(), prefill=prefill, ssr=ssr,
        post_url=post_url, form_error=form_error, field_errors=field_errors or {}, notice=notice,
        idempotency_key=opaque_token(16), **brand,
    )


# ------------------------------------------------------------------ server-rendered picker (works without JS)
PICKER_KEYS = ("month", "date", "slot")


def _ssr_url(request: Request, q, path: Optional[str] = None, **overrides) -> str:
    """Booking page URL keeping every query param except the picker ones, plus `overrides`."""
    keep = {k: v for k, v in q.items() if k not in PICKER_KEYS and k not in overrides and k != "timezone"}
    keep.update({k: v for k, v in overrides.items() if v not in (None, "")})
    return (path or request.url.path) + ("?" + urlencode(keep) if keep else "")


def _month_add(y: int, m: int, delta: int) -> tuple[int, int]:
    m += delta
    return y + (m - 1) // 12, (m - 1) % 12 + 1


def _ssr_picker(request: Request, db: Session, et: EventType, q, tz: str, duration: int, mode: str,
                booking: Optional[Booking], locale: str = "en") -> dict:
    """Month grid, day slots and the selected slot computed on the server (PRD 8.1/8.2 without JS)."""
    tz = tu.validate_tz(tz)
    path = q.get("_path") or request.url.path
    q = {k: v for k, v in q.items() if k != "_path"}
    today = tu.local_date(tu.utcnow(), tz)
    date_q = q.get("date") if re.match(r"^\d{4}-\d{2}-\d{2}$", q.get("date") or "") else None
    month_q = q.get("month") if re.match(r"^\d{4}-\d{2}$", q.get("month") or "") else None
    sel_date = None
    if date_q:
        try:
            sel_date = dt.date.fromisoformat(date_q)
        except ValueError:
            sel_date = None
    try:
        y, m = (int(month_q[:4]), int(month_q[5:7])) if month_q else ((sel_date or today).year, (sel_date or today).month)
        dt.date(y, m, 1)
    except ValueError:
        y, m = today.year, today.month
    exclude = booking.id if (mode == "reschedule" and booking is not None) else None
    days: dict = {}
    error = None
    jumps = 0 if (month_q or sel_date) else 2  # like booking.js: skip up to 2 empty months on first load
    while True:
        first = dt.date(y, m, 1)
        last = dt.date(*_month_add(y, m, 1), 1) - dt.timedelta(days=1)
        days = {}
        if last >= today:
            try:
                payload = availability_payload(db, et, max(first, today), last, tz, duration, exclude,
                                               for_reschedule=mode == "reschedule")
                days = {d["date"]: d["slots"] for d in payload["days"]}
            except APIError as exc:
                error = exc.message
        if days or error or jumps <= 0:
            break
        jumps -= 1
        y, m = _month_add(y, m, 1)
    keyfmt = f"{y:04d}-{m:02d}"
    first = dt.date(y, m, 1)
    weeks, week = [], [None] * first.weekday()
    d = first
    while d.month == m:
        key = d.isoformat()
        bookable = [x for x in (days.get(key) or []) if not x.get("full")]
        week.append({
            "day": d.day, "date": key, "available": bool(bookable), "count": len(bookable),
            "label": format_date(d, locale, "full"), "today": d == today, "selected": sel_date == d,
            "url": _ssr_url(request, q, path, tz=tz, month=keyfmt, date=key),
        })
        if len(week) == 7:
            weeks.append(week)
            week = []
        d += dt.timedelta(days=1)
    if week:
        weeks.append(week + [None] * (7 - len(week)))
    py, pm = _month_add(y, m, -1)
    ny, nm = _month_add(y, m, 1)
    day_slots = []
    selected_slot = None
    if sel_date and sel_date.isoformat() in days:
        for x in days[sel_date.isoformat()]:
            item = dict(x)
            item["url"] = _ssr_url(request, q, path, tz=tz, month=keyfmt, date=sel_date.isoformat(), slot=x["start_at"])
            day_slots.append(item)
            if q.get("slot") and not x.get("full") and q.get("slot") == x["start_at"]:
                selected_slot = x
    if q.get("slot") and selected_slot is None and not error:
        error = t("book.time_unavailable", locale)
    summary = None
    if selected_slot:
        start = tu.parse_iso_utc(selected_slot["start_at"])
        end = tu.parse_iso_utc(selected_slot["end_at"])
        summary = (f"{format_date(tu.utc_to_local(start, tz), locale, 'full')}, {selected_slot['label']} – "
                   f"{tu.utc_to_local(end, tz).strftime('%H:%M')} · {t('book.minutes', locale, n=duration)} · "
                   f"{tz.replace('_', ' ')}")
    return {
        "tz": tz,
        "month_title": format_date(first, locale, "month_year"),
        "month": keyfmt,
        "weeks": weeks,
        "prev_url": _ssr_url(request, q, path, tz=tz, month=f"{py:04d}-{pm:02d}") if (y, m) > (today.year, today.month) else None,
        "next_url": _ssr_url(request, q, path, tz=tz, month=f"{ny:04d}-{nm:02d}"),
        "date": sel_date.isoformat() if sel_date else None,
        "date_title": format_date(sel_date, locale, "day_month") if sel_date else None,
        "day_slots": day_slots,
        "slot": selected_slot,
        "summary": summary,
        "back_url": _ssr_url(request, q, path, tz=tz, month=keyfmt, date=sel_date.isoformat() if sel_date else None),
        "path": path,
        "error": error,
        "empty": not days and not error,
        "hidden": {k: v for k, v in q.items() if k not in PICKER_KEYS + ("tz", "timezone", "duration")},
    }


def _booking_ctx(db: Session, b: Booking, request: Optional[Request] = None) -> dict:
    et = db.get(EventType, b.event_type_id)
    host = db.get(User, b.host_id)
    loc = get_locale(request, host.locale if host else None) if request is not None else "en"
    snap = b.snapshot_json or {}
    tz = tu.validate_tz(b.timezone)
    hosts = [db.get(User, h.user_id) for h in b.hosts] or [host]
    return {
        "b": b,
        "et": et,
        "host": host,
        "hosts": [h for h in hosts if h],
        "snap": snap,
        "tz": tz,
        "when": f"{format_date(tu.utc_to_local(b.start_at, tz), loc, en_fmt='%A, %d %B %Y')} {tu.tz_abbrev(b.start_at, tz)}",
        "time_range": f"{tu.utc_to_local(b.start_at, tz).strftime('%H:%M')} – {tu.utc_to_local(b.end_at, tz).strftime('%H:%M')} "
                      f"{tu.tz_abbrev(b.start_at, tz)}",
        "tz_label": f"{tz} ({tu.utc_offset_str(b.start_at, tz)})",
        "duration": int((b.end_at - b.start_at).total_seconds() // 60),
        "location_text": ics.location_text(b),
        "location": b.location_json or {},
        "invitee": b.invitees[0] if b.invitees else None,
        "google_url": ics.google_calendar_url(b),
        "outlook_url": ics.outlook_calendar_url(b),
        "urls": bk.manage_urls(b),
        "policy": snap.get("cancellation_policy") or (et.cancellation_policy_json if et else None) or {},
        "confirmation": snap.get("confirmation") or (et.confirmation_json if et else None) or {},
        "token_expired": bool(b.manage_token_expires_at and b.manage_token_expires_at < tu.utcnow()),
    }


def _get_booking(db: Session, token: str) -> Optional[Booking]:
    return db.scalar(select(Booking).where(Booking.manage_token == token)) if token else None


def _booking_locale(db: Session, b: Booking, request: Request) -> str:
    host = db.get(User, b.host_id)
    return get_locale(request, host.locale if host else None)


def _expired(db: Session, b: Booking, request: Request, brand: Optional[dict] = None) -> Response:
    brand = brand or _booking_brand(db, b, request)
    return _page(request, "public/unavailable.html", status_code=410, title=t("err.link_expired_title", brand["locale"]),
                 message=t("err.link_expired", brand["locale"]), **brand)


def _booking_brand(db: Session, b: Booking, request: Request) -> dict:
    return _branding(db, db.get(User, b.host_id), db.get(Workspace, b.workspace_id), request)


# ------------------------------------------------------------------ team pages
@router.get("/team/{team_slug}")
def team_page(team_slug: str, request: Request, db: Session = Depends(get_db)):
    team = db.scalar(select(Team).where(Team.slug == team_slug))
    if not team:
        return _not_found(request)
    ws = db.get(Workspace, team.workspace_id)
    events = db.scalars(
        select(EventType).where(EventType.team_id == team.id, EventType.deleted_at.is_(None)).order_by(EventType.created_at)
    ).all()
    events = [e for e in events if _listable(e)]
    members = [db.get(User, m.user_id) for m in db.scalars(select(TeamMember).where(TeamMember.team_id == team.id,
                                                                                    TeamMember.active.is_(True))).all()]
    _record_view(db, team.workspace_id, "team", None, _utm(request))
    return _page(request, "public/team.html", team=team, events=events, members=[m for m in members if m],
                 **_branding(db, None, ws, request))


@router.get("/team/{team_slug}/{event_slug}")
def team_booking_page(team_slug: str, event_slug: str, request: Request, db: Session = Depends(get_db)):
    try:
        et, owner, team = find_event(db, f"team:{team_slug}", event_slug)
    except APIError:
        return _not_found(request)
    return _booking_page(request, db, et, owner, team, f"team:{team_slug}")


# ------------------------------------------------------------------ booking management
@router.get("/booking/{token}")
def manage_page(token: str, request: Request, db: Session = Depends(get_db)):
    b = _get_booking(db, token)
    if not b:
        return _not_found(request, "err.booking_not_found_link")
    ctx = _booking_ctx(db, b, request)
    if ctx["token_expired"]:
        return _expired(db, b, request)
    new_b = db.get(Booking, b.rescheduled_to_id) if b.rescheduled_to_id else None
    return _page(request, "public/manage.html", new_booking=new_b, **ctx, **_booking_brand(db, b, request))


@router.get("/booking/{token}/confirmed")
def confirmed_page(token: str, request: Request, db: Session = Depends(get_db)):
    b = _get_booking(db, token)
    if not b:
        return _not_found(request, "err.booking_not_found")
    ctx = _booking_ctx(db, b, request)
    brand = _booking_brand(db, b, request)
    conf = ctx["confirmation"]
    if b.status == "confirmed" and conf.get("redirect_url") and not brand["embedded"]:
        url = conf["redirect_url"]
        if urlparse(url).scheme in ("http", "https"):
            if conf.get("pass_params"):
                parts = urlparse(url)
                params = parse_qsl(parts.query)
                params.append(("booking_id", b.id))
                params += [(k, v) for k, v in (b.utm_json or {}).items() if k in UTM_KEYS]
                url = urlunparse(parts._replace(query=urlencode(params)))
            return RedirectResponse(url, status_code=303)
    message = {
        "booking_id": b.id, "start_at": tu.iso_z(b.start_at), "end_at": tu.iso_z(b.end_at), "status": b.status,
        "event_type": (b.snapshot_json or {}).get("name"), "timezone": b.timezone,
    }
    return _page(request, "public/confirmed.html", embed_message=message, **ctx, **brand)


@router.get("/booking/{token}/ics")
def booking_ics(token: str, db: Session = Depends(get_db)):
    b = _get_booking(db, token)
    if not b:
        return Response("Not found", status_code=404)
    method = "CANCEL" if b.status == "cancelled" else "PUBLISH"
    body = ics.build_ics(b, method=method)
    return Response(body, media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="invite-{b.id}.ics"'})


@router.get("/booking/{token}/cancel")
def cancel_form(token: str, request: Request, db: Session = Depends(get_db)):
    b = _get_booking(db, token)
    if not b:
        return _not_found(request, "err.booking_not_found")
    ctx = _booking_ctx(db, b, request)
    if ctx["token_expired"]:
        return _expired(db, b, request)
    return _page(request, "public/cancel.html", error=None, **ctx, **_booking_brand(db, b, request))


@router.post("/booking/{token}/cancel")
def cancel_submit(token: str, request: Request, reason: str = Form(""), website: str = Form(""),
                  db: Session = Depends(get_db)):
    b = _get_booking(db, token)
    if not b:
        return _not_found(request, "err.booking_not_found")
    enforce(request, "public_manage", 20, 60)
    ctx = _booking_ctx(db, b, request)
    if ctx["token_expired"] or website:
        return _expired(db, b, request)
    try:
        b = bk.cancel_booking(db, b, "invitee", reason[:2000] or None)
    except APIError as exc:
        return _page(request, "public/cancel.html", status_code=exc.status_code if exc.status_code < 500 else 400,
                     error=exc.message, **_booking_ctx(db, b, request), **_booking_brand(db, b, request))
    if b.status == "cancelled":
        flash(request, t("manage.cancelled_flash", _booking_locale(db, b, request)))
    else:
        flash(request, t("manage.request_flash", _booking_locale(db, b, request)), "info")
    emb = _embed_ctx(request)
    return redirect(f"/booking/{token}" + (f"?{emb['embed_qs']}" if emb["embedded"] else ""))


@router.get("/booking/{token}/reschedule")
def reschedule_page(token: str, request: Request, db: Session = Depends(get_db)):
    b = _get_booking(db, token)
    if not b:
        return _not_found(request, "err.booking_not_found")
    ctx = _booking_ctx(db, b, request)
    brand = _booking_brand(db, b, request)
    if ctx["token_expired"]:
        return _expired(db, b, request, brand)
    if b.status != "confirmed":
        loc = brand["locale"]
        status = b.status.replace("_", " ") if loc == "en" else status_label(b.status, loc)
        return _page(request, "public/unavailable.html", status_code=409, title=t("err.cant_reschedule_title", loc),
                     message=t("err.cant_reschedule_status", loc, status=status),
                     back_url=f"/booking/{token}", **brand)
    if ctx["policy"].get("allow_reschedule") is False:
        return _page(request, "public/unavailable.html", status_code=403, title=t("err.reschedule_off_title", brand["locale"]),
                     message=t("err.reschedule_off", brand["locale"]), back_url=f"/booking/{token}", **brand)
    et = ctx["et"]
    owner = db.get(User, et.owner_user_id)
    team = db.get(Team, et.team_id) if et.team_id else None
    owner_slug = f"team:{team.slug}" if team else (owner.slug or "")
    return _booking_page(request, db, et, owner, team, owner_slug, mode="reschedule", booking=b)


# ------------------------------------------------------------------ no-JS form posts (PRD 8.2 without JavaScript)
def _event_page_path(db: Session, et: EventType) -> tuple[Optional[User], Optional[Team], str, str]:
    owner = db.get(User, et.owner_user_id)
    team = db.get(Team, et.team_id) if et.team_id else None
    if team:
        return owner, team, f"team:{team.slug}", f"/team/{team.slug}/{et.slug}"
    slug = (owner.slug if owner else "") or ""
    return owner, team, slug, f"/{slug}/{et.slug}"


def _form_prefill(form, et: EventType) -> dict:
    answers = {}
    for q in et.questions:
        key = f"q-{q.id}"
        if q.type in ("checkbox",) and q.options_json:
            answers[q.id] = [str(v) for v in form.getlist(key)]
        elif q.type in ("checkbox", "consent"):
            answers[q.id] = "on" if form.get(key) else ""
        else:
            answers[q.id] = str(form.get(key) or "")[:5000]
    return {
        "name": str(form.get("name") or "")[:200],
        "email": str(form.get("email") or "")[:320],
        "phone": str(form.get("phone") or "")[:50],
        "company": str(form.get("company") or "")[:200],
        "guests": str(form.get("guests") or "")[:1000],
        "location": str(form.get("location") or ""),
        "answers": answers,
    }


def _form_answers(prefill: dict, et: EventType) -> dict:
    out = {}
    for q in et.questions:
        v = prefill["answers"].get(q.id)
        if q.type in ("checkbox", "consent") and not q.options_json:
            v = bool(v)
        out[q.id] = v
    return out


def _picker_params(form, path: str, tz: str, start: Optional[dt.datetime], duration: int, slot: bool = True) -> dict:
    params = {"_path": path, "tz": tz, "duration": str(duration)}
    for k in UTM_KEYS + ("rsid",):
        if form.get(k):
            params[k] = str(form.get(k))[:200]
    if start is not None:
        params["date"] = tu.local_date(start, tz).isoformat()
        if slot:
            params["slot"] = tu.iso_z(start)
    return params


@router.post("/book/{event_type_id}")
async def book_form_submit(event_type_id: str, request: Request, db: Session = Depends(get_db)):
    """Plain HTML form POST from the booking page (works without JavaScript). Same rules as the
    JSON API: slot token, email verification, honeypot, idempotency, revalidation inside the lock."""
    enforce(request, "public_booking", 20, 60)
    form = await request.form()
    et = db.get(EventType, event_type_id)
    if not et or et.deleted_at or et.is_managed_template:
        return _not_found(request)
    owner, team, owner_slug, path = _event_page_path(db, et)
    tz = tu.validate_tz(str(form.get("tz") or ""), "") or tu.validate_tz(owner.default_timezone if owner else "UTC")
    try:
        dur = av.resolve_duration(et, int(form.get("duration") or et.duration))
    except (TypeError, ValueError):
        dur = et.duration
    try:
        start = tu.parse_iso_utc(str(form.get("start_at") or ""))
    except ValueError:
        start = None
    prefill = _form_prefill(form, et)
    emb = _embed_ctx(request)
    loc = get_locale(request, owner.locale if owner else None)

    def back_to_picker(message: str):
        flash(request, message, "error")
        params = _picker_params(form, path, tz, start, dur, slot=False)
        params.pop("_path")
        if emb["embedded"]:
            params.update(dict(parse_qsl(emb["embed_qs"])))
        return redirect(path + "?" + urlencode(params))

    def rerender(error: Optional[str], fields: Optional[dict] = None, notice: Optional[str] = None, status: int = 422):
        return _booking_page(request, db, et, owner, team, owner_slug, params=_picker_params(form, path, tz, start, dur),
                             prefill=prefill, form_error=error, field_errors=fields, notice=notice, status_code=status)

    if start is None:
        return back_to_picker(t("book.choose_time", loc))
    if form.get("website"):
        return rerender(t("book.unable_to_process", loc))
    if et.status != "active" or (et.single_use and et.used_at):
        return _booking_page(request, db, et, owner, team, owner_slug)
    if not av.verify_slot_token(str(form.get("slot_token") or ""), et.id, start, dur):
        return back_to_picker(t("book.selection_expired", loc))
    email = prefill["email"].strip().lower()
    if form.get("action") == "send_code":
        if not bk.EMAIL_RE.match(email):
            return rerender(t("book.valid_email_first", loc), {"email": t("book.invalid_email", loc)})
        try:
            enforce(request, "public_verify", 5, 600, key=f"{client_ip(request)}:{email}")
        except APIError as exc:
            return rerender(exc.message, status=429)
        issue_verification_code(db, email, et.name)
        return rerender(None, notice=t("book.code_sent_notice", loc, email=email), status=200)
    key = str(form.get("idempotency_key") or "")[:100] or opaque_token(16)
    utm = {k: str(form.get(k))[:200] for k in UTM_KEYS if form.get(k)}
    rsid = (str(form.get("rsid") or "")[:40]) or None
    invitee = {"name": prefill["name"], "email": prefill["email"], "phone": prefill["phone"] or None,
               "guests": prefill["guests"], "timezone": tz, "location": prefill["location"] or None,
               "consent": {"privacy_notice_version": settings.privacy_version, "accepted_at": tu.iso_z(tu.utcnow())}}
    try:
        if et.requires_email_verification:
            verify_code(db, email, str(form.get("verification_code") or "").strip() or None, tu.utcnow())
        b = bk.create_booking(
            db, et, start, invitee, _form_answers(prefill, et), timezone=tz, duration=dur,
            idempotency_key=f"pub:form:{key}", utm=utm or None, source="routing" if rsid else "booking_page",
            routing_submission_id=rsid, actor="invitee",
        )
    except APIError as exc:
        if exc.code in ("SLOT_NO_LONGER_AVAILABLE", "EVENT_UNAVAILABLE"):
            return back_to_picker(exc.message)
        if exc.code == "CALENDAR_UNAVAILABLE":
            return back_to_picker(exc.message)
        return rerender(exc.message, (exc.details or {}).get("fields"))
    nxt = booking_response(b)["next_url"]
    return redirect(nxt + (f"?{emb['embed_qs']}" if emb["embedded"] else ""))


@router.post("/booking/{token}/reschedule")
async def reschedule_form_submit(token: str, request: Request, db: Session = Depends(get_db)):
    """No-JS reschedule: atomic replacement via bk.reschedule_booking; original kept on failure."""
    enforce(request, "public_manage", 20, 60)
    form = await request.form()
    b = _get_booking(db, token)
    if not b:
        return _not_found(request, "err.booking_not_found")
    ctx = _booking_ctx(db, b, request)
    if ctx["token_expired"]:
        return _expired(db, b, request)
    tz = tu.validate_tz(str(form.get("tz") or ""), "") or b.timezone
    path = f"/booking/{token}/reschedule"
    emb = _embed_ctx(request)
    dur = int((b.end_at - b.start_at).total_seconds() // 60)
    loc = _booking_locale(db, b, request)
    try:
        start = tu.parse_iso_utc(str(form.get("start_at") or ""))
    except ValueError:
        start = None

    def back(message: str):
        flash(request, message, "error")
        params = {"tz": tz}
        if start is not None:
            params["date"] = tu.local_date(start, tz).isoformat()
        if emb["embedded"]:
            params.update(dict(parse_qsl(emb["embed_qs"])))
        return redirect(path + "?" + urlencode(params))

    if start is None:
        return back(t("book.choose_time", loc))
    if not av.verify_slot_token(str(form.get("slot_token") or ""), b.event_type_id, start, dur):
        return back(t("book.selection_expired", loc))
    try:
        new = bk.reschedule_booking(db, b, start, "invitee", (str(form.get("reason") or ""))[:2000] or None)
    except APIError as exc:
        return back(exc.message)
    if tu.is_valid_tz(tz) and new.timezone != tz:
        new.timezone = tz
        db.commit()
    nxt = booking_response(new)["next_url"]
    return redirect(nxt + (f"?{emb['embed_qs']}" if emb["embedded"] else ""))


# ------------------------------------------------------------------ profile + booking page (catch-all, keep LAST)
@router.get("/{username}")
def profile_page(username: str, request: Request, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.slug == username, User.deleted_at.is_(None)))
    if not user:
        return _not_found(request)
    events = db.scalars(
        select(EventType).where(EventType.owner_user_id == user.id, EventType.team_id.is_(None), EventType.deleted_at.is_(None))
        .order_by(EventType.created_at)
    ).all()
    events = [e for e in events if _listable(e) and e.visibility == "public"]
    ws_ids = {e.workspace_id for e in events}
    ws = db.get(Workspace, next(iter(ws_ids))) if ws_ids else None
    if ws:
        _record_view(db, ws.id, "profile", None, _utm(request))
    passthrough = urlencode({k: v for k, v in request.query_params.items()
                             if k in UTM_KEYS + ("name", "email", "embed", "hide_details", "primary_color", "bg", "text", "radius")})
    return _page(request, "public/profile.html", owner=user, events=events, passthrough=passthrough,
                 **_branding(db, user, ws, request))


@router.get("/{username}/{event_slug}")
def booking_page(username: str, event_slug: str, request: Request, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.slug == username, User.deleted_at.is_(None)))
    if not user:
        return _not_found(request)
    et = db.scalar(select(EventType).where(EventType.owner_user_id == user.id, EventType.slug == event_slug,
                                           EventType.deleted_at.is_(None)))
    if not et or et.is_managed_template:
        return _not_found(request)
    team = db.get(Team, et.team_id) if et.team_id else None
    return _booking_page(request, db, et, user, team, username)
