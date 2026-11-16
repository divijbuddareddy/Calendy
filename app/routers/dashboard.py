"""Home dashboard (GET /app): upcoming meetings, event type shortcuts, calendar health,
booking stats, in-app notifications, pending approvals, conflict-flagged bookings."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import Ctx, get_ctx, redirect, render
from app.models import (
    Booking,
    CalendarConnection,
    EventType,
    InAppNotification,
    User,
    utcnow,
)
from app.services.hostutil import event_public_url, is_host_public, visible_bookings_filter

router = APIRouter()


@router.get("/app")
def home(request: Request, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    now = utcnow()
    vis = visible_bookings_filter(ctx)
    upcoming = db.scalars(select(Booking).where(
        vis, Booking.status == "confirmed", Booking.end_at >= now, Booking.start_at < now + dt.timedelta(days=7)
    ).order_by(Booking.start_at).limit(20)).all()
    since = now - dt.timedelta(days=30)

    def count(*conds):
        return db.scalar(select(func.count(Booking.id)).where(vis, *conds)) or 0

    stats = {
        "created": count(Booking.created_at >= since, Booking.status.notin_(("held", "expired", "failed"))),
        "upcoming": count(Booking.status == "confirmed", Booking.start_at >= now),
        "cancelled": count(Booking.status == "cancelled", Booking.updated_at >= since),
        "rescheduled": count(Booking.status == "rescheduled", Booking.updated_at >= since),
    }
    pending_approvals = db.scalars(select(Booking).where(
        vis, Booking.status == "confirmed", Booking.cancellation_requested_at.is_not(None)
    ).order_by(Booking.start_at).limit(10)).all()
    pending_other = count(Booking.status.in_(("pending", "pending_payment")))
    conflicts = db.scalars(select(Booking).where(
        vis, Booking.conflict_flag.is_(True), Booking.status == "confirmed", Booking.end_at >= now
    ).order_by(Booking.start_at).limit(10)).all()
    events = db.scalars(select(EventType).where(
        EventType.workspace_id == ctx.workspace.id, EventType.owner_user_id == ctx.user.id,
        EventType.deleted_at.is_(None), EventType.status == "active", EventType.is_one_off.is_(False),
        EventType.single_use.is_(False)).order_by(EventType.created_at).limit(8)).all()
    conns = db.scalars(select(CalendarConnection).where(CalendarConnection.user_id == ctx.user.id,
                                                        CalendarConnection.workspace_id == ctx.workspace.id)).all()
    notifications = db.scalars(select(InAppNotification).where(InAppNotification.user_id == ctx.user.id)
                               .order_by(InAppNotification.created_at.desc()).limit(10)).all()
    unread = db.scalar(select(func.count(InAppNotification.id)).where(
        InAppNotification.user_id == ctx.user.id, InAppNotification.read_at.is_(None))) or 0
    ets = {e.id: e for e in db.scalars(select(EventType).where(
        EventType.id.in_({b.event_type_id for b in upcoming + pending_approvals + conflicts} or {""})))}
    hosts = {u.id: u for u in db.scalars(select(User).where(
        User.id.in_({b.host_id for b in upcoming} or {""})))}
    return render(request, "app/home.html", ctx, nav="home", upcoming=upcoming, stats=stats,
                  pending_approvals=pending_approvals, pending_other=pending_other, conflicts=conflicts,
                  events=events, urls={e.id: event_public_url(db, e) for e in events}, connections=conns,
                  broken=[c for c in conns if c.status != "connected"], notifications=notifications, unread=unread,
                  ets=ets, hosts=hosts, tz=ctx.user.default_timezone or "UTC", host_public=is_host_public(ctx.user),
                  now=now)


@router.post("/app/notifications/{nid}/read")
def mark_read(nid: str, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    n = db.get(InAppNotification, nid)
    if not n or n.user_id != ctx.user.id:
        raise HTTPException(404, "Notification not found")
    if not n.read_at:
        n.read_at = utcnow()
        db.commit()
    return redirect(n.link if n.link and n.link.startswith("/") else "/app")


@router.post("/app/notifications/read-all")
def mark_all_read(ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    for n in db.scalars(select(InAppNotification).where(InAppNotification.user_id == ctx.user.id,
                                                        InAppNotification.read_at.is_(None))).all():
        n.read_at = utcnow()
    db.commit()
    return redirect("/app")
