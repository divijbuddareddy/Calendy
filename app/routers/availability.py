"""Availability editor (PRD 7.1): multiple schedules, weekly multi-range grid, timezone,
date overrides, holiday calendars, personal days off, host-wide meeting limits."""
from __future__ import annotations

import datetime as dt
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import audit
from app.db import get_db
from app.deps import Ctx, Forbidden, flash, get_ctx, redirect, render
from app.models import (
    AvailabilityOverride,
    AvailabilityRule,
    AvailabilitySchedule,
    EventType,
    User,
)
from app.services.holidays import COUNTRIES, holidays_for
from app.services.hostutil import all_timezones, parse_ranges, valid_tz

router = APIRouter()
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _schedules(db: Session, ctx: Ctx) -> list[AvailabilitySchedule]:
    return db.scalars(select(AvailabilitySchedule).where(
        AvailabilitySchedule.workspace_id == ctx.workspace.id, AvailabilitySchedule.user_id == ctx.user.id
    ).order_by(AvailabilitySchedule.is_default.desc(), AvailabilitySchedule.created_at)).all()


def get_schedule_or_404(db: Session, ctx: Ctx, sid: str) -> AvailabilitySchedule:
    s = db.get(AvailabilitySchedule, sid)
    if not s or s.workspace_id != ctx.workspace.id or s.user_id != ctx.user.id:
        raise HTTPException(404, "Schedule not found")
    return s


def _guard(ctx: Ctx):
    if ctx.role == "viewer":
        raise Forbidden("Viewers cannot edit availability.")


@router.get("/app/availability")
def availability_page(request: Request, schedule: Optional[str] = None, country: Optional[str] = None,
                      ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    schedules = _schedules(db, ctx)
    if not schedules:
        from app.services.bootstrap import create_default_schedule

        create_default_schedule(db, ctx.user, ctx.workspace.id)
        db.commit()
        schedules = _schedules(db, ctx)
    sel = next((s for s in schedules if s.id == schedule), None) or schedules[0]
    rules = db.scalars(select(AvailabilityRule).where(AvailabilityRule.schedule_id == sel.id)
                       .order_by(AvailabilityRule.weekday, AvailabilityRule.start_local)).all()
    week = {wd: [] for wd in range(7)}
    for r in rules:
        week[r.weekday].append({"start": r.start_local, "end": r.end_local})
    overrides = db.scalars(select(AvailabilityOverride).where(AvailabilityOverride.schedule_id == sel.id)
                           .order_by(AvailabilityOverride.local_date)).all()
    today = dt.date.today()
    country = (country or sel.holiday_country or "").upper()
    holiday_dates = {o.local_date for o in overrides if o.kind == "holiday"}
    holidays = [(d, n, d in holiday_dates) for d, n in holidays_for(country) if d >= today] if country in COUNTRIES else []
    used_by = db.scalars(select(EventType).where(EventType.schedule_id == sel.id, EventType.deleted_at.is_(None))).all()
    return render(request, "app/availability/index.html", ctx, nav="availability", schedules=schedules, sel=sel,
                  week=week, weekdays=WEEKDAYS, overrides=[o for o in overrides if o.local_date >= today],
                  past_overrides=sum(1 for o in overrides if o.local_date < today), timezones=all_timezones(),
                  countries=COUNTRIES, country=country, holidays=holidays, limits=ctx.user.working_hours_json or {},
                  used_by=used_by, today=today)


@router.post("/app/availability/schedules")
async def create_schedule(request: Request, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    _guard(ctx)
    form = await request.form()
    name = (form.get("name") or "").strip() or "New schedule"
    copy_from = form.get("copy_from")
    sch = AvailabilitySchedule(workspace_id=ctx.workspace.id, user_id=ctx.user.id, name=name[:200],
                               timezone=ctx.user.default_timezone, is_default=not _schedules(db, ctx))
    db.add(sch)
    db.flush()
    src = get_schedule_or_404(db, ctx, copy_from) if copy_from else None
    if src:
        sch.timezone = src.timezone
        for r in db.scalars(select(AvailabilityRule).where(AvailabilityRule.schedule_id == src.id)).all():
            db.add(AvailabilityRule(schedule_id=sch.id, weekday=r.weekday, start_local=r.start_local, end_local=r.end_local))
    else:
        for wd in range(5):
            db.add(AvailabilityRule(schedule_id=sch.id, weekday=wd, start_local="09:00", end_local="17:00"))
    audit(db, "availability.schedule_created", workspace_id=ctx.workspace.id, actor_id=ctx.user.id,
          entity_type="schedule", entity_id=sch.id)
    db.commit()
    flash(request, f"Schedule “{sch.name}” created.")
    return redirect(f"/app/availability?schedule={sch.id}")


@router.post("/app/availability/{sid}/weekly")
async def save_weekly(request: Request, sid: str, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    """Form: name, timezone, d{0..6}_start / d{0..6}_end (repeated per range)."""
    _guard(ctx)
    sch = get_schedule_or_404(db, ctx, sid)
    form = await request.form()
    tz = form.get("timezone") or sch.timezone
    if not valid_tz(tz):
        flash(request, "Unknown timezone.", "error")
        return redirect(f"/app/availability?schedule={sch.id}")
    new_rules = []
    for wd in range(7):
        pairs = [(s, "24:00" if e == "23:59" else e)  # time inputs can't express midnight end-of-day
                 for s, e in zip(form.getlist(f"d{wd}_start"), form.getlist(f"d{wd}_end"))]
        ranges, err = parse_ranges(pairs)
        if err:
            flash(request, f"{WEEKDAYS[wd]}: {err}", "error")
            return redirect(f"/app/availability?schedule={sch.id}")
        new_rules += [(wd, r["start"], r["end"]) for r in ranges]
    name = (form.get("name") or "").strip()
    if name:
        sch.name = name[:200]
    sch.timezone = tz
    for r in db.scalars(select(AvailabilityRule).where(AvailabilityRule.schedule_id == sch.id)).all():
        db.delete(r)
    db.flush()
    for wd, s, e in new_rules:
        db.add(AvailabilityRule(schedule_id=sch.id, weekday=wd, start_local=s, end_local=e))
    audit(db, "availability.updated", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="schedule",
          entity_id=sch.id, metadata={"rules": len(new_rules), "timezone": tz})
    db.commit()
    flash(request, "Availability saved." if new_rules else "Saved — this schedule currently has no available hours.",
          "success" if new_rules else "warning")
    return redirect(f"/app/availability?schedule={sch.id}")


@router.post("/app/availability/{sid}/default")
def set_default(request: Request, sid: str, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    _guard(ctx)
    sch = get_schedule_or_404(db, ctx, sid)
    for s in _schedules(db, ctx):
        s.is_default = s.id == sch.id
    db.commit()
    flash(request, f"“{sch.name}” is now your default schedule.")
    return redirect(f"/app/availability?schedule={sch.id}")


@router.post("/app/availability/{sid}/delete")
def delete_schedule(request: Request, sid: str, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    _guard(ctx)
    sch = get_schedule_or_404(db, ctx, sid)
    if sch.is_default or len(_schedules(db, ctx)) <= 1:
        flash(request, "You can't delete your default schedule. Make another schedule the default first.", "error")
        return redirect(f"/app/availability?schedule={sch.id}")
    for ev in db.scalars(select(EventType).where(EventType.schedule_id == sch.id)).all():
        ev.schedule_id = None  # falls back to default schedule
    for o in db.scalars(select(AvailabilityOverride).where(AvailabilityOverride.schedule_id == sch.id)).all():
        db.delete(o)
    for r in db.scalars(select(AvailabilityRule).where(AvailabilityRule.schedule_id == sch.id)).all():
        db.delete(r)
    db.flush()
    db.delete(sch)
    audit(db, "availability.schedule_deleted", workspace_id=ctx.workspace.id, actor_id=ctx.user.id,
          entity_type="schedule", entity_id=sid)
    db.commit()
    flash(request, "Schedule deleted. Event types using it now use your default schedule.")
    return redirect("/app/availability")


@router.post("/app/availability/{sid}/overrides")
async def save_override(request: Request, sid: str, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    """Form: date (YYYY-MM-DD), end_date (optional, for multi-day time off), mode=unavailable|custom,
    start/end (repeated), reason, kind=override|day_off."""
    _guard(ctx)
    sch = get_schedule_or_404(db, ctx, sid)
    form = await request.form()
    try:
        d0 = dt.date.fromisoformat(form.get("date") or "")
        d1 = dt.date.fromisoformat(form.get("end_date")) if form.get("end_date") else d0
    except ValueError:
        flash(request, "Please choose a valid date.", "error")
        return redirect(f"/app/availability?schedule={sch.id}#overrides")
    if d1 < d0 or (d1 - d0).days > 366:
        flash(request, "End date must be on or after the start date (max one year).", "error")
        return redirect(f"/app/availability?schedule={sch.id}#overrides")
    mode = form.get("mode") or "unavailable"
    kind = form.get("kind") if form.get("kind") in ("override", "day_off") else "override"
    intervals = []
    if mode == "custom":
        intervals, err = parse_ranges(list(zip(form.getlist("start"), form.getlist("end"))))
        if err or not intervals:
            flash(request, err or "Add at least one time range, or mark the date unavailable.", "error")
            return redirect(f"/app/availability?schedule={sch.id}#overrides")
        kind = "override"
    reason = (form.get("reason") or "").strip()[:300] or None
    d = d0
    n = 0
    while d <= d1:
        o = db.scalar(select(AvailabilityOverride).where(AvailabilityOverride.schedule_id == sch.id,
                                                         AvailabilityOverride.local_date == d))
        if not o:
            o = AvailabilityOverride(schedule_id=sch.id, local_date=d)
            db.add(o)
        o.intervals_json, o.kind, o.reason = intervals, kind, reason
        d += dt.timedelta(days=1)
        n += 1
    audit(db, "availability.override_saved", workspace_id=ctx.workspace.id, actor_id=ctx.user.id,
          entity_type="schedule", entity_id=sch.id, metadata={"from": d0.isoformat(), "to": d1.isoformat(), "kind": kind})
    db.commit()
    flash(request, f"Date override saved for {n} day(s).")
    return redirect(f"/app/availability?schedule={sch.id}#overrides")


@router.post("/app/availability/{sid}/overrides/{oid}/delete")
def delete_override(request: Request, sid: str, oid: str, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    _guard(ctx)
    sch = get_schedule_or_404(db, ctx, sid)
    o = db.get(AvailabilityOverride, oid)
    if not o or o.schedule_id != sch.id:
        raise HTTPException(404, "Override not found")
    db.delete(o)
    db.commit()
    flash(request, "Override removed.")
    return redirect(f"/app/availability?schedule={sch.id}#overrides")


@router.post("/app/availability/{sid}/holidays")
async def save_holidays(request: Request, sid: str, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    """Form: country (GB|US|IN|''), dates (repeated ISO dates to block)."""
    _guard(ctx)
    sch = get_schedule_or_404(db, ctx, sid)
    form = await request.form()
    country = (form.get("country") or "").upper()
    if country and country not in COUNTRIES:
        flash(request, "Unsupported country.", "error")
        return redirect(f"/app/availability?schedule={sch.id}#holidays")
    chosen = set(form.getlist("dates"))
    names = {d.isoformat(): n for d, n in holidays_for(country)} if country else {}
    existing = {o.local_date.isoformat(): o for o in db.scalars(select(AvailabilityOverride).where(
        AvailabilityOverride.schedule_id == sch.id)).all()}
    added = removed = 0
    for iso, o in existing.items():
        if o.kind == "holiday" and (iso not in chosen or iso not in names):
            db.delete(o)
            removed += 1
    for iso in chosen:
        if iso in names and iso not in existing:
            db.add(AvailabilityOverride(schedule_id=sch.id, local_date=dt.date.fromisoformat(iso), intervals_json=[],
                                        kind="holiday", reason=names[iso]))
            added += 1
    sch.holiday_country = country or None
    audit(db, "availability.holidays_updated", workspace_id=ctx.workspace.id, actor_id=ctx.user.id,
          entity_type="schedule", entity_id=sch.id, metadata={"country": country, "added": added, "removed": removed})
    db.commit()
    flash(request, f"Holidays updated ({added} blocked, {removed} removed).")
    return redirect(f"/app/availability?schedule={sch.id}&country={country}#holidays")


@router.post("/app/availability/limits")
async def save_limits(request: Request, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    _guard(ctx)
    form = await request.form()
    user = db.get(User, ctx.user.id)
    data = dict(user.working_hours_json or {})
    for k in ("daily_limit", "weekly_limit", "monthly_limit"):
        raw = (form.get(k) or "").strip()
        if not raw:
            data[k] = None
            continue
        try:
            v = int(raw)
            if v < 1:
                raise ValueError
        except ValueError:
            flash(request, "Limits must be positive whole numbers.", "error")
            return redirect("/app/availability#limits")
        data[k] = v
    user.working_hours_json = data
    audit(db, "availability.limits_updated", workspace_id=ctx.workspace.id, actor_id=ctx.user.id,
          entity_type="user", entity_id=user.id, metadata={k: data.get(k) for k in ("daily_limit", "weekly_limit", "monthly_limit")})
    db.commit()
    flash(request, "Meeting limits saved.")
    return redirect(f"/app/availability?schedule={form.get('schedule') or ''}#limits")
