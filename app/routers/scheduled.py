"""Scheduled events (PRD 8.3): tabs, filters, CSV export, detail page and host actions
(cancel, reschedule, request reschedule, no-show, approve/decline cancellation, reassign, notes).

Booking state changes are delegated to app.services.booking (imported lazily)."""
from __future__ import annotations

import csv
import datetime as dt
import io
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.audit import audit
from app.config import settings
from app.db import get_db
from app.deps import Ctx, Forbidden, flash, get_ctx, redirect, render
from app.models import (
    Booking,
    BookingAnswer,
    BookingEvent,
    BookingHost,
    CalendarEvent,
    EventType,
    EventTypeHost,
    Invitee,
    PaymentRecord,
    User,
    utcnow,
)
from app.services.hostutil import can_view_booking, event_public_url, visible_bookings_filter

router = APIRouter()
TABS = {"upcoming": "Upcoming", "past": "Past", "pending": "Pending", "cancelled": "Cancelled"}


def _tab_filter(tab: str, now: dt.datetime):
    if tab == "past":
        return and_(Booking.status == "confirmed", Booking.end_at < now)
    if tab == "pending":
        return or_(Booking.status.in_(("pending", "pending_payment")),
                   and_(Booking.status == "confirmed", Booking.cancellation_requested_at.is_not(None)))
    if tab == "cancelled":
        return Booking.status.in_(("cancelled", "rescheduled"))
    return and_(Booking.status == "confirmed", Booking.end_at >= now)


def _parse_date(s: Optional[str]) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(s) if s else None
    except ValueError:
        return None


def filtered_bookings(db: Session, ctx: Ctx, tab: str, event_type: str = "", date_from: str = "", date_to: str = "",
                      q: str = "", limit: Optional[int] = 200) -> list[Booking]:
    now = utcnow()
    stmt = select(Booking).where(visible_bookings_filter(ctx), _tab_filter(tab, now))
    if event_type:
        stmt = stmt.where(Booking.event_type_id == event_type)
    tz = ZoneInfo(ctx.user.default_timezone or "UTC")
    d0, d1 = _parse_date(date_from), _parse_date(date_to)
    if d0:
        stmt = stmt.where(Booking.start_at >= dt.datetime.combine(d0, dt.time(), tz).astimezone(dt.timezone.utc).replace(tzinfo=None))
    if d1:
        stmt = stmt.where(Booking.start_at < dt.datetime.combine(d1 + dt.timedelta(days=1), dt.time(), tz)
                          .astimezone(dt.timezone.utc).replace(tzinfo=None))
    if q:
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(Booking.id.in_(select(Invitee.booking_id).where(
            or_(Invitee.name.ilike(like), Invitee.email.ilike(like)))))
    order = Booking.start_at.desc() if tab in ("past", "cancelled") else Booking.start_at.asc()
    stmt = stmt.order_by(order)
    if limit:
        stmt = stmt.limit(limit)
    return db.scalars(stmt).all()


def _lookups(db: Session, bookings: list[Booking]):
    ets = {e.id: e for e in db.scalars(select(EventType).where(EventType.id.in_({b.event_type_id for b in bookings} or {""})))}
    hosts = {u.id: u for u in db.scalars(select(User).where(User.id.in_({b.host_id for b in bookings} or {""})))}
    return ets, hosts


@router.get("/app/scheduled")
def scheduled_list(request: Request, tab: str = "upcoming", event_type: str = "", date_from: str = "",
                   date_to: str = "", q: str = "", ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    tab = tab if tab in TABS else "upcoming"
    rows = filtered_bookings(db, ctx, tab, event_type, date_from, date_to, q)
    ets, hosts = _lookups(db, rows)
    all_ets = db.scalars(select(EventType).where(EventType.workspace_id == ctx.workspace.id,
                                                 EventType.deleted_at.is_(None)).order_by(EventType.name)).all()
    if not ctx.can("admin"):
        all_ets = [e for e in all_ets if e.owner_user_id == ctx.user.id or any(h.user_id == ctx.user.id for h in e.hosts)]
    pending_count = len(filtered_bookings(db, ctx, "pending", limit=100))
    tz = ctx.user.default_timezone or "UTC"
    groups: dict[str, list] = {}
    for b in rows:
        key = b.start_at.replace(tzinfo=dt.timezone.utc).astimezone(ZoneInfo(tz)).strftime("%A, %d %B %Y")
        groups.setdefault(key, []).append(b)
    qs = request.url.query
    return render(request, "app/scheduled/list.html", ctx, nav="scheduled", tab=tab, tabs=TABS, groups=groups,
                  rows=rows, ets=ets, hosts=hosts, all_ets=all_ets, filters=dict(event_type=event_type,
                  date_from=date_from, date_to=date_to, q=q), pending_count=pending_count, tz=tz, qs=qs,
                  now=utcnow())


@router.get("/app/scheduled/export.csv")
def export_csv(tab: str = "upcoming", event_type: str = "", date_from: str = "", date_to: str = "", q: str = "",
               ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    tab = tab if tab in TABS else "upcoming"
    rows = filtered_bookings(db, ctx, tab, event_type, date_from, date_to, q, limit=None)
    ets, hosts = _lookups(db, rows)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["booking_id", "event_type", "status", "start_utc", "end_utc", "invitee_timezone", "host", "invitee_name",
                "invitee_email", "invitee_phone", "location", "no_show", "cancel_reason", "cancelled_by", "source",
                "created_at_utc"])
    for b in rows:
        inv = b.invitees[0] if b.invitees else None
        loc = b.location_json or {}
        w.writerow([b.id, ets[b.event_type_id].name if b.event_type_id in ets else "", b.status,
                    b.start_at.isoformat(), b.end_at.isoformat(), b.timezone,
                    hosts[b.host_id].name if b.host_id in hosts else "", inv.name if inv else "", inv.email if inv else "",
                    inv.phone if inv and inv.phone else "", loc.get("join_url") or loc.get("value") or loc.get("type") or "",
                    "yes" if b.no_show else "no", b.cancel_reason or "", b.cancelled_by or "", b.source or "",
                    b.created_at.isoformat()])
    audit(db, "bookings.exported", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, metadata={"tab": tab, "rows": len(rows)})
    db.commit()
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="bookings-{tab}.csv"'})


def get_booking_or_404(db: Session, ctx: Ctx, booking_id: str) -> Booking:
    b = db.get(Booking, booking_id)
    if not can_view_booking(db, ctx, b):
        raise HTTPException(404, "Booking not found")
    return b


def _manage_urls(b: Booking) -> dict:
    try:
        from app.services.booking import manage_urls

        urls = manage_urls(b)
        if isinstance(urls, dict):
            return {"cancel": urls.get("cancel_url") or urls.get("cancel"),
                    "reschedule": urls.get("reschedule_url") or urls.get("reschedule"),
                    "manage": urls.get("manage_url") or urls.get("manage")}
    except Exception:
        pass
    base = settings.base_url.rstrip("/")
    return {"cancel": f"{base}/booking/{b.manage_token}/cancel", "reschedule": f"{base}/booking/{b.manage_token}/reschedule",
            "manage": f"{base}/booking/{b.manage_token}"}


@router.get("/app/scheduled/{booking_id}")
def booking_detail(request: Request, booking_id: str, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    b = get_booking_or_404(db, ctx, booking_id)
    ev = db.get(EventType, b.event_type_id)
    host = db.get(User, b.host_id)
    history = db.scalars(select(BookingEvent).where(BookingEvent.booking_id == b.id).order_by(BookingEvent.created_at)).all()
    answers = db.scalars(select(BookingAnswer).where(BookingAnswer.booking_id == b.id)).all()
    cal_events = db.scalars(select(CalendarEvent).where(CalendarEvent.booking_id == b.id)).all()
    payments = db.scalars(select(PaymentRecord).where(PaymentRecord.booking_id == b.id)).all()
    co_hosts = db.scalars(select(User).join(BookingHost, BookingHost.user_id == User.id)
                          .where(BookingHost.booking_id == b.id)).all()
    rr_hosts = []
    if ev and ev.kind == "round_robin" and ctx.can("admin"):
        rr_hosts = db.scalars(select(User).join(EventTypeHost, EventTypeHost.user_id == User.id).where(
            EventTypeHost.event_type_id == ev.id, EventTypeHost.active.is_(True), User.id != b.host_id)).all()
    related = {}
    for key in ("rescheduled_from_id", "rescheduled_to_id"):
        rid = getattr(b, key)
        if rid:
            rb = db.get(Booking, rid)
            if rb and rb.workspace_id == ctx.workspace.id:
                related[key] = rb
    snap = b.snapshot_json or {}
    join_url = (b.location_json or {}).get("join_url") or next((c.join_url for c in cal_events if c.join_url), None)
    now = utcnow()
    return render(request, "app/scheduled/detail.html", ctx, nav="scheduled", b=b, ev=ev, host=host, history=history,
                  answers=answers, cal_events=cal_events, payments=payments, co_hosts=co_hosts, rr_hosts=rr_hosts,
                  related=related, snap=snap, join_url=join_url, urls=_manage_urls(b), now=now,
                  is_upcoming=b.status == "confirmed" and b.end_at >= now, is_past=b.end_at < now,
                  tz=ctx.user.default_timezone or "UTC", event_url=event_public_url(db, ev) if ev else None)


@router.post("/app/scheduled/{booking_id}/notes")
async def save_notes(request: Request, booking_id: str, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    b = get_booking_or_404(db, ctx, booking_id)
    if ctx.role == "viewer":
        raise Forbidden("Viewers cannot edit bookings.")
    form = await request.form()
    b.host_notes = (form.get("host_notes") or "").strip()[:10000] or None
    db.add(BookingEvent(booking_id=b.id, actor=f"user:{ctx.user.id}", event_type="notes_updated", payload_json={}))
    db.commit()
    flash(request, "Notes saved.")
    return redirect(f"/app/scheduled/{b.id}")


def _service(name: str):
    try:
        import importlib

        mod = importlib.import_module("app.services.booking")
        return getattr(mod, name)
    except (ImportError, AttributeError):
        return None


_AUDIT_NAMES = {"cancel_booking": "cancelled", "reschedule_booking": "rescheduled", "mark_no_show": "no_show_marked",
                "approve_cancellation": "cancellation_approved", "decline_cancellation": "cancellation_declined",
                "reassign_booking": "reassigned_via_app"}


def _run(request: Request, db: Session, ctx: Ctx, b: Booking, fn_name: str, ok_msg: str, *args, **kwargs):
    if ctx.role == "viewer":
        raise Forbidden("Viewers cannot change bookings.")
    fn = _service(fn_name)
    if not fn:
        flash(request, "Booking service is unavailable right now. Please try again later.", "error")
        return None
    try:
        result = fn(db, b, *args, **kwargs)
        # PRD 17.1: booking edits by hosts/admins land in the workspace audit log too.
        audit(db, f"booking.{_AUDIT_NAMES.get(fn_name, fn_name)}", workspace_id=ctx.workspace.id,
              actor_id=ctx.user.id, entity_type="booking", entity_id=b.id,
              metadata={"via": "app", **({"reason": kwargs["reason"]} if kwargs.get("reason") else {})})
        db.commit()
    except Exception as exc:  # APIError / ValueError from the booking service
        db.rollback()
        flash(request, getattr(exc, "message", None) or str(exc) or "Action failed.", "error")
        return None
    flash(request, ok_msg)
    return result if result is not None else True


@router.post("/app/scheduled/{booking_id}/cancel")
async def cancel(request: Request, booking_id: str, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    b = get_booking_or_404(db, ctx, booking_id)
    form = await request.form()
    reason = (form.get("reason") or "").strip() or None
    _run(request, db, ctx, b, "cancel_booking", "Meeting cancelled. The invitee has been notified.",
         "admin" if ctx.can("admin") and b.host_id != ctx.user.id else f"user:{ctx.user.id}", reason=reason)
    return redirect(f"/app/scheduled/{b.id}")


@router.get("/app/scheduled/{booking_id}/reschedule")
def reschedule_page(request: Request, booking_id: str, date: str = "", ctx: Ctx = Depends(get_ctx),
                    db: Session = Depends(get_db)):
    b = get_booking_or_404(db, ctx, booking_id)
    ev = db.get(EventType, b.event_type_id)
    tzname = ctx.user.default_timezone or "UTC"
    tz = ZoneInfo(tzname)
    today = utcnow().replace(tzinfo=dt.timezone.utc).astimezone(tz).date()
    start = _parse_date(date) or today
    start = max(start, today)
    end = start + dt.timedelta(days=6)
    slots, error = [], None
    try:
        from app.services.availability import get_available_slots

        duration = int((b.end_at - b.start_at).total_seconds() // 60)
        slots = get_available_slots(db, ev, start, end, tzname, duration=duration, exclude_booking_id=b.id,
                                    for_reschedule=True)
    except ImportError:
        error = "Availability service is unavailable right now."
    except Exception as exc:
        error = f"Could not load availability: {exc}"
    days: dict[str, list] = {}
    for s in slots:
        local = s.start.replace(tzinfo=dt.timezone.utc).astimezone(tz)
        days.setdefault(local.strftime("%A %d %b"), []).append((s.start.isoformat(), local.strftime("%H:%M")))
    return render(request, "app/scheduled/reschedule.html", ctx, nav="scheduled", b=b, ev=ev, days=days, error=error,
                  start=start, prev=(start - dt.timedelta(days=7)) if start > today else None,
                  next=start + dt.timedelta(days=7), tz=tzname)


@router.post("/app/scheduled/{booking_id}/reschedule")
async def reschedule(request: Request, booking_id: str, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    b = get_booking_or_404(db, ctx, booking_id)
    form = await request.form()
    try:
        new_start = dt.datetime.fromisoformat(form.get("start") or "").replace(tzinfo=None)
    except ValueError:
        flash(request, "Choose a new time.", "error")
        return redirect(f"/app/scheduled/{b.id}/reschedule")
    res = _run(request, db, ctx, b, "reschedule_booking", "Meeting rescheduled. The invitee has been notified.",
               new_start, f"user:{ctx.user.id}", reason=(form.get("reason") or "").strip() or None)
    if isinstance(res, Booking):
        return redirect(f"/app/scheduled/{res.id}")
    return redirect(f"/app/scheduled/{b.id}" if res else f"/app/scheduled/{b.id}/reschedule")


@router.post("/app/scheduled/{booking_id}/request-reschedule")
async def request_reschedule(request: Request, booking_id: str, ctx: Ctx = Depends(get_ctx),
                             db: Session = Depends(get_db)):
    b = get_booking_or_404(db, ctx, booking_id)
    if ctx.role == "viewer":
        raise Forbidden("Viewers cannot change bookings.")
    form = await request.form()
    note = (form.get("message") or "").strip()
    ev = db.get(EventType, b.event_type_id)
    link = _manage_urls(b).get("reschedule")
    from markupsafe import escape

    from app.services.messaging import send_email

    for inv in b.invitees:
        send_email(db, inv.email, f"Please pick a new time: {ev.name if ev else 'meeting'}",
                   f'<div style="font-family:system-ui,sans-serif;max-width:520px">'
                   f"<p>Hi {escape(inv.name)},</p><p>{escape(ctx.user.name)} has asked you to choose a new time for "
                   f"<strong>{escape(ev.name if ev else 'your meeting')}</strong>.</p>"
                   + (f"<p><em>{escape(note)}</em></p>" if note else "")
                   + f'<p><a href="{link}">Pick a new time</a></p></div>')
    db.add(BookingEvent(booking_id=b.id, actor=f"user:{ctx.user.id}", event_type="reschedule_requested",
                        payload_json={"message": note}))
    audit(db, "booking.reschedule_requested", workspace_id=ctx.workspace.id, actor_id=ctx.user.id,
          entity_type="booking", entity_id=b.id)
    db.commit()
    flash(request, "We've emailed the invitee a link to pick a new time.")
    return redirect(f"/app/scheduled/{b.id}")


@router.post("/app/scheduled/{booking_id}/no-show")
async def no_show(request: Request, booking_id: str, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    b = get_booking_or_404(db, ctx, booking_id)
    form = await request.form()
    value = (form.get("value") or "1") == "1"
    _run(request, db, ctx, b, "mark_no_show", "Marked as no-show." if value else "No-show removed.",
         f"user:{ctx.user.id}", value=value)
    return redirect(f"/app/scheduled/{b.id}")


@router.post("/app/scheduled/{booking_id}/approve-cancel")
def approve_cancel(request: Request, booking_id: str, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    b = get_booking_or_404(db, ctx, booking_id)
    _run(request, db, ctx, b, "approve_cancellation", "Cancellation approved.", f"user:{ctx.user.id}")
    return redirect(f"/app/scheduled/{b.id}")


@router.post("/app/scheduled/{booking_id}/decline-cancel")
def decline_cancel(request: Request, booking_id: str, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    b = get_booking_or_404(db, ctx, booking_id)
    _run(request, db, ctx, b, "decline_cancellation", "Cancellation request declined; the meeting stays booked.",
         f"user:{ctx.user.id}")
    return redirect(f"/app/scheduled/{b.id}")


@router.post("/app/scheduled/{booking_id}/reassign")
async def reassign(request: Request, booking_id: str, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    b = get_booking_or_404(db, ctx, booking_id)
    if not ctx.can("admin"):
        raise Forbidden("Only admins can reassign meetings.")
    form = await request.form()
    new_host = form.get("new_host_id") or ""
    ok = db.scalar(select(EventTypeHost.id).where(EventTypeHost.event_type_id == b.event_type_id,
                                                  EventTypeHost.user_id == new_host))
    if not ok:
        flash(request, "Choose a host from this event's host pool.", "error")
        return redirect(f"/app/scheduled/{b.id}")
    reason = (form.get("reason") or "").strip() or "Reassigned by admin"
    _run(request, db, ctx, b, "reassign_booking", "Meeting reassigned.", new_host, f"user:{ctx.user.id}", reason)
    return redirect(f"/app/scheduled/{b.id}")
