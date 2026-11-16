"""Installable PWA + mobile quick actions (PRD 2 / roadmap phase 5: "mobile apps"; PRD 3.2 says a
responsive web app is sufficient for MVP, so the "mobile app" is an installable PWA).

    /manifest.webmanifest   web app manifest
    /sw.js                  service worker (offline shell; never caches private /app pages)
    /icons/icon-{n}.png     generated PNG icons (192, 512, maskable) — no binary assets in the repo
    /icons/icon.svg         vector icon
    /offline                offline fallback page
    /app/m                  mobile quick actions: upcoming meetings, copy links, single-use + one-off links, cancel
"""
from __future__ import annotations

import datetime as dt
import secrets
import struct
import zlib
from functools import lru_cache
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import audit
from app.config import settings
from app.db import get_db
from app.deps import Ctx, flash, get_ctx, redirect, render
from app.models import Booking, EventType, utcnow
from app.services.hostutil import can_edit_event, event_public_url, visible_bookings_filter

router = APIRouter()

BRAND = (0x3B, 0x47, 0xD6)


# ------------------------------------------------------------------ icons
def _png(width: int, height: int, rows: list[bytes]) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + r for r in rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


@lru_cache(maxsize=8)
def icon_png(size: int, maskable: bool = False) -> bytes:
    """Brand-coloured calendar glyph. Maskable icons fill the canvas (safe zone = centre 80%)."""
    n = size
    radius = 0 if maskable else n * 0.18
    white = (255, 255, 255, 255)
    soft = (0xC9, 0xCD, 0xF7, 255)
    brand = BRAND + (255,)
    # calendar body inside the maskable safe zone
    x0, x1, y0, y1 = n * 0.27, n * 0.73, n * 0.29, n * 0.73
    head = y0 + (y1 - y0) * 0.24
    rows = []
    for y in range(n):
        row = bytearray()
        for x in range(n):
            px = (0, 0, 0, 0)
            # rounded-square background
            cx = min(max(x + 0.5, radius), n - radius)
            cy = min(max(y + 0.5, radius), n - radius)
            if radius == 0 or (x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2 <= radius ** 2:
                px = brand
            if x0 <= x < x1 and y0 <= y < y1:
                px = white if y >= head else soft
                # 3x2 grid of brand "days"
                gx = (x - x0) / (x1 - x0)
                gy = (y - head) / (y1 - head) if y >= head else -1
                if 0 <= gy:
                    cell_x, cell_y = gx * 3 % 1, gy * 2 % 1
                    if 0.22 < cell_x < 0.78 and 0.25 < cell_y < 0.75:
                        px = brand
            # binder rings
            for rx in (n * 0.38, n * 0.62):
                if abs(x + 0.5 - rx) < n * 0.025 and n * 0.24 <= y < n * 0.33:
                    px = white
            row += bytes(px)
        rows.append(bytes(row))
    return _png(n, n, rows)


ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="12"
fill="#3b47d6"/><rect x="17" y="18.5" width="30" height="28.5" rx="2" fill="#fff"/><rect x="17" y="18.5" width="30"
height="7" fill="#c9cdf7"/><g fill="#3b47d6"><rect x="20" y="29" width="6" height="5"/><rect x="29" y="29" width="6"
height="5"/><rect x="38" y="29" width="6" height="5"/><rect x="20" y="38" width="6" height="5"/><rect x="29" y="38"
width="6" height="5"/><rect x="38" y="38" width="6" height="5"/></g><rect x="23" y="15" width="3" height="6"
fill="#fff"/><rect x="38" y="15" width="3" height="6" fill="#fff"/></svg>"""


@router.get("/icons/icon-{size}.png", include_in_schema=False)
def icon(size: int, maskable: int = 0):
    if size not in (48, 72, 96, 128, 144, 180, 192, 256, 384, 512):
        raise HTTPException(404, "Unknown icon size")
    return Response(icon_png(size, bool(maskable)), media_type="image/png",
                    headers={"Cache-Control": "public, max-age=604800"})


@router.get("/icons/icon.svg", include_in_schema=False)
def icon_svg():
    return Response(ICON_SVG, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=604800"})


@router.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(icon_png(48), media_type="image/png", headers={"Cache-Control": "public, max-age=604800"})


# ------------------------------------------------------------------ manifest / service worker
@router.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    data = {
        "name": settings.app_name,
        "short_name": "Scheduler",
        "description": "Share booking links, see upcoming meetings and manage your schedule.",
        "id": "/app",
        "start_url": "/app/m?source=pwa",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "background_color": "#f6f7fb",
        "theme_color": "#3b47d6",
        "categories": ["productivity", "business"],
        "icons": [
            {"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/icons/icon-512.png?maskable=1", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
            {"src": "/icons/icon.svg", "sizes": "any", "type": "image/svg+xml"},
        ],
        "shortcuts": [
            {"name": "Upcoming meetings", "url": "/app/m#upcoming", "icons": [{"src": "/icons/icon-96.png", "sizes": "96x96"}]},
            {"name": "Share a link", "url": "/app/m#links", "icons": [{"src": "/icons/icon-96.png", "sizes": "96x96"}]},
            {"name": "Scheduled events", "url": "/app/scheduled"},
        ],
    }
    return JSONResponse(data, media_type="application/manifest+json")


@router.get("/sw.js", include_in_schema=False)
def service_worker():
    # Served from the root so its scope covers the whole origin.
    return FileResponse("app/static/sw.js", media_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


@router.get("/offline", include_in_schema=False)
def offline(request: Request):
    return render(request, "offline.html")


# ------------------------------------------------------------------ mobile quick actions
def _upcoming(db: Session, ctx: Ctx, limit: int = 15):
    return db.scalars(select(Booking).where(
        visible_bookings_filter(ctx), Booking.end_at >= utcnow(),
        Booking.status.in_(("confirmed", "pending", "pending_payment")),
    ).order_by(Booking.start_at).limit(limit)).all()


@router.get("/app/m")
def mobile_home(request: Request, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    upcoming = _upcoming(db, ctx)
    ets = db.scalars(select(EventType).where(
        EventType.workspace_id == ctx.workspace.id, EventType.deleted_at.is_(None), EventType.status == "active",
        EventType.single_use.is_(False), EventType.is_one_off.is_(False), EventType.is_managed_template.is_(False),
    ).order_by(EventType.created_at)).all()
    ets = [e for e in ets if can_edit_event(ctx, e) or e.visibility == "team"]
    names = {e.id: e for e in db.scalars(select(EventType).where(
        EventType.id.in_({b.event_type_id for b in upcoming}))).all()} if upcoming else {}
    tz = ctx.user.default_timezone or "UTC"
    now_local = dt.datetime.now(ZoneInfo(tz)).replace(second=0, microsecond=0)
    default_time = (now_local + dt.timedelta(days=1)).replace(minute=0).strftime("%Y-%m-%dT%H:%M")
    return render(request, "app/mobile.html", ctx, nav="mobile", upcoming=upcoming, event_types=ets,
                  urls={e.id: event_public_url(db, e) for e in ets}, names=names, tz=tz,
                  can_edit=lambda e: can_edit_event(ctx, e), default_time=default_time,
                  last_link=request.session.pop("m_last_link", None),
                  profile_url=f"{settings.base_url.rstrip('/')}/{ctx.user.slug}" if ctx.user.slug else None)


@router.post("/app/m/bookings/{booking_id}/cancel")
async def mobile_cancel(request: Request, booking_id: str, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    from app.routers.scheduled import _run, get_booking_or_404

    b = get_booking_or_404(db, ctx, booking_id)
    form = await request.form()
    reason = (form.get("reason") or "").strip() or None
    _run(request, db, ctx, b, "cancel_booking", "Meeting cancelled. The invitee has been notified.",
         "admin" if ctx.can("admin") and b.host_id != ctx.user.id else f"user:{ctx.user.id}", reason=reason)
    return redirect("/app/m#upcoming")


@router.post("/app/m/one-off")
async def mobile_one_off(request: Request, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    """Quick one-off link: one or more specific times, default video location."""
    from app.routers.event_types import LOCATION_TYPES, unique_event_slug
    from app.services.bootstrap import slugify

    if not ctx.feature("one_off"):
        flash(request, "One-off meetings are not included in your plan.", "error")
        return redirect("/app/m#links")
    form = await request.form()
    loc = form.get("location") if form.get("location") in LOCATION_TYPES else "google_meet"
    name = (form.get("name") or "").strip()[:200] or "Meeting"
    try:
        duration = max(5, min(720, int(form.get("duration") or 30)))
    except ValueError:
        duration = 30
    z = ZoneInfo(ctx.user.default_timezone or "UTC")
    starts = []
    for raw in form.getlist("slots"):
        raw = (raw or "").strip()
        if not raw:
            continue
        try:
            utc = dt.datetime.fromisoformat(raw).replace(tzinfo=z).astimezone(dt.timezone.utc).replace(tzinfo=None)
        except ValueError:
            flash(request, "Invalid date/time.", "error")
            return redirect("/app/m#links")
        if utc <= utcnow():
            flash(request, "Pick a time in the future.", "error")
            return redirect("/app/m#links")
        starts.append(utc)
    if not starts:
        flash(request, "Pick at least one time.", "error")
        return redirect("/app/m#links")
    ev = EventType(
        workspace_id=ctx.workspace.id, owner_user_id=ctx.user.id, name=name,
        slug=unique_event_slug(db, ctx.user.id, f"{slugify(name)[:40]}-{secrets.token_hex(3)}"),
        duration=duration, kind="one_on_one", location_json=[{"type": loc, "value": ""}],
        visibility="secret", is_one_off=True, one_off_slots_json=sorted({s.isoformat() for s in starts}),
        min_notice_minutes=0, max_days_ahead=730, color="#0f766e",
        cancellation_policy_json={"allow_cancel": True, "allow_reschedule": True, "min_notice_minutes": 0,
                                  "require_reason": False, "host_approval": False, "policy_text": ""},
    )
    db.add(ev)
    db.flush()
    audit(db, "event_type.created", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="event_type",
          entity_id=ev.id, metadata={"one_off": True, "via": "mobile"})
    db.commit()
    url = event_public_url(db, ev)
    request.session["m_last_link"] = url
    flash(request, "One-off link created — copy it below.")
    return redirect("/app/m#links")
