"""Settings: profile, branding, notifications, security, workspace, billing (entitlements demo),
data export / account deletion (ACC-006)."""
from __future__ import annotations

import json
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit import audit
from app.config import settings as app_settings
from app.db import get_db
from app.deps import (
    Ctx,
    Forbidden,
    bump_session_epoch,
    flash,
    get_ctx,
    logout_session,
    redirect,
    refresh_session_epoch,
    render,
)
from app.entitlements import PLANS, plan_of
from app.models import (
    AvailabilityOverride,
    AvailabilityRule,
    AvailabilitySchedule,
    Booking,
    DataRequest,
    EventType,
    InAppNotification,
    Subscription,
    User,
    Workspace,
    WorkspaceMember,
    utcnow,
)
from app.security import hash_password, verify_password
from app.services import ai_key
from app.services.bootstrap import slugify
from app.services.hostutil import HHMM, all_timezones, valid_tz

router = APIRouter()
TABS = {"profile": "Profile", "branding": "Branding", "notifications": "Notifications", "security": "Security",
        "workspace": "Workspace", "ai": "AI", "billing": "Billing", "data": "Data & privacy"}
# Languages for invitee-facing pages (booking, confirmation, manage/cancel); see app/i18n.py.
LOCALES = {"en": "English", "en-GB": "English (UK)", "fr": "Français", "de": "Deutsch", "es": "Español",
           "hi": "हिन्दी"}
NOTIF_PREFS = [
    ("new_booking_email", "Email me when a meeting is booked"),
    ("cancellation_email", "Email me when a meeting is cancelled or rescheduled"),
    ("reminder_email", "Email me a reminder before each meeting"),
    ("daily_agenda", "Send me a daily agenda email"),
    ("in_app", "Show in-app notifications"),
]


def _backup_available() -> bool:
    from app.routers.ops import backup_available

    return backup_available()


def _user(db: Session, ctx: Ctx) -> User:
    return db.get(User, ctx.user.id)


@router.get("/app/settings")
def settings_page(request: Request, tab: str = "profile", ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    tab = tab if tab in TABS else "profile"
    memberships = db.execute(select(WorkspaceMember, Workspace).join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
                             .where(WorkspaceMember.user_id == ctx.user.id, WorkspaceMember.status == "active")).all()
    seats = db.scalar(select(func.count(WorkspaceMember.id)).where(WorkspaceMember.workspace_id == ctx.workspace.id,
                                                                   WorkspaceMember.status == "active")) or 0
    sub = db.scalar(select(Subscription).where(Subscription.workspace_id == ctx.workspace.id)
                    .order_by(Subscription.created_at.desc()))
    requests_ = db.scalars(select(DataRequest).where(DataRequest.user_id == ctx.user.id)
                           .order_by(DataRequest.created_at.desc()).limit(10)).all()
    return render(request, "app/settings/index.html", ctx, nav="settings", tab=tab, tabs=TABS,
                  timezones=all_timezones(), locales=LOCALES, notif_prefs=NOTIF_PREFS,
                  prefs=ctx.user.notification_prefs_json or {}, branding=ctx.user.branding_json or {},
                  wh=ctx.user.working_hours_json or {}, memberships=memberships, seats=seats, sub=sub,
                  plans=PLANS, plan=plan_of(ctx.workspace), data_requests=requests_,
                  db_backup_available=_backup_available() and ctx.role == "owner",
                  session_idle_days=max(1, app_settings.session_idle_minutes // 1440),
                  session_max_days=max(1, app_settings.session_max_age_minutes // 1440),
                  ai_key_source=ai_key.key_source(ctx.workspace),
                  ai_key_masked=ai_key.mask(ai_key.workspace_key(ctx.workspace)))


@router.post("/app/settings/ai")
async def save_ai_key(request: Request, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    if not ctx.can("admin"):
        raise Forbidden("Only workspace admins can change the AI API key.")
    form = await request.form()
    back = "/app/assistant" if form.get("next") == "/app/assistant" else "/app/settings?tab=ai"
    ws = db.get(Workspace, ctx.workspace.id)
    if form.get("action") == "remove":
        ai_key.set_workspace_key(ws, None)
        audit(db, "workspace.ai_key_removed", workspace_id=ws.id, actor_id=ctx.user.id, entity_type="workspace",
              entity_id=ws.id)
        db.commit()
        flash(request, "API key removed.")
        return redirect(back)
    key = (form.get("api_key") or "").strip()
    if not key:
        flash(request, "Paste your Anthropic API key first.", "error")
        return redirect(back)
    if len(key) > 300 or any(c.isspace() for c in key):
        flash(request, "That doesn't look like an API key.", "error")
        return redirect(back)
    ok, message = ai_key.verify_key(key)
    if not ok:
        flash(request, message, "error")
        return redirect(back)
    ai_key.set_workspace_key(ws, key)
    audit(db, "workspace.ai_key_set", workspace_id=ws.id, actor_id=ctx.user.id, entity_type="workspace",
          entity_id=ws.id)
    db.commit()
    flash(request, message)
    return redirect(back)


@router.post("/app/settings/profile")
async def save_profile(request: Request, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    from app.routers.auth import validate_user_slug

    form = await request.form()
    user = _user(db, ctx)
    name = (form.get("name") or "").strip()
    slug = slugify(form.get("slug") or "") if form.get("slug") else ""
    tz = form.get("timezone") or user.default_timezone
    avatar = (form.get("avatar_url") or "").strip()
    errors = []
    if not name:
        errors.append("Name is required.")
    err = validate_user_slug(db, slug, user.id)
    if err:
        errors.append(err)
    if not valid_tz(tz):
        errors.append("Unknown timezone.")
    if avatar and not re.match(r"^https://", avatar):
        errors.append("Profile photo URL must start with https://")
    ws, we = form.get("work_start") or "", form.get("work_end") or ""
    if (ws or we) and not (HHMM.match(ws) and HHMM.match(we) and ws < we):
        errors.append("Working hours must be valid times with start before end.")
    locale = form.get("locale") or "en"
    if locale not in LOCALES:
        errors.append("Unsupported language.")
    if errors:
        for e in errors:
            flash(request, e, "error")
        return redirect("/app/settings?tab=profile")
    changed_slug = user.slug != slug
    user.name, user.slug, user.default_timezone, user.locale = name[:200], slug, tz, locale
    user.headline = (form.get("headline") or "").strip()[:300] or None
    user.avatar_url = avatar or None
    wh = dict(user.working_hours_json or {})
    wh.update({"start": ws or None, "end": we or None})
    user.working_hours_json = wh
    audit(db, "user.profile_updated", workspace_id=ctx.workspace.id, actor_id=user.id, entity_type="user",
          entity_id=user.id, metadata={"slug_changed": changed_slug})
    db.commit()
    flash(request, "Profile saved." + (" Your booking links now use the new URL." if changed_slug else ""))
    return redirect("/app/settings?tab=profile")


@router.post("/app/settings/branding")
async def save_branding(request: Request, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    form = await request.form()
    user = _user(db, ctx)
    color = form.get("primary") or "#3b47d6"
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        flash(request, "Brand colour must be a hex value like #3b47d6.", "error")
        return redirect("/app/settings?tab=branding")
    hide = form.get("hide_branding") in ("1", "on")
    if hide and not ctx.feature("custom_branding"):
        flash(request, "Removing “Powered by” branding requires the Standard plan or higher.", "error")
        return redirect("/app/settings?tab=branding")
    logo = (form.get("logo_url") or "").strip()
    if logo and not logo.startswith("https://"):
        flash(request, "Logo URL must start with https://", "error")
        return redirect("/app/settings?tab=branding")
    user.branding_json = {"primary": color, "hide_branding": hide, "logo_url": logo or None}
    if form.get("apply_workspace") in ("1", "on"):
        if not ctx.can("admin"):
            raise Forbidden("Only admins can change workspace branding.")
        ws = db.get(Workspace, ctx.workspace.id)
        ws.branding_json = dict(user.branding_json)
    audit(db, "user.branding_updated", workspace_id=ctx.workspace.id, actor_id=user.id, entity_type="user", entity_id=user.id)
    db.commit()
    flash(request, "Branding saved.")
    return redirect("/app/settings?tab=branding")


@router.post("/app/settings/notifications")
async def save_notifications(request: Request, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    form = await request.form()
    user = _user(db, ctx)
    prefs = dict(user.notification_prefs_json or {})
    for key, _ in NOTIF_PREFS:
        prefs[key] = form.get(key) in ("1", "on")
    user.notification_prefs_json = prefs
    db.commit()
    flash(request, "Notification preferences saved.")
    return redirect("/app/settings?tab=notifications")


@router.post("/app/settings/security/password")
async def change_password(request: Request, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    form = await request.form()
    user = _user(db, ctx)
    current, new, confirm = form.get("current_password") or "", form.get("new_password") or "", form.get("confirm_password") or ""
    if user.password_hash and not verify_password(current, user.password_hash):
        flash(request, "Your current password is incorrect.", "error")
    elif len(new) < 8 or new != confirm:
        flash(request, "New passwords must match and be at least 8 characters.", "error")
    else:
        user.password_hash = hash_password(new)
        bump_session_epoch(user)  # every other signed-in session is invalidated
        audit(db, "user.password_changed", workspace_id=ctx.workspace.id, actor_id=user.id, entity_type="user", entity_id=user.id)
        db.commit()
        refresh_session_epoch(request, user)
        flash(request, "Password updated. You've been signed out on all other devices.")
    return redirect("/app/settings?tab=security")


@router.post("/app/settings/security/sign-out-others")
def sign_out_other_sessions(request: Request, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    user = _user(db, ctx)
    bump_session_epoch(user)
    audit(db, "user.sessions_revoked", workspace_id=ctx.workspace.id, actor_id=user.id, entity_type="user",
          entity_id=user.id)
    db.commit()
    refresh_session_epoch(request, user)
    flash(request, "Signed out of all other sessions.")
    return redirect("/app/settings?tab=security")


@router.post("/app/settings/workspace")
async def save_workspace(request: Request, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    if not ctx.can("admin"):
        raise Forbidden("Only workspace admins can change workspace settings.")
    form = await request.form()
    ws = db.get(Workspace, ctx.workspace.id)
    name = (form.get("name") or "").strip()
    slug = slugify(form.get("slug") or "")
    if not name or not slug:
        flash(request, "Workspace name and slug are required.", "error")
        return redirect("/app/settings?tab=workspace")
    if db.scalar(select(Workspace.id).where(Workspace.slug == slug, Workspace.id != ws.id)):
        flash(request, f"The workspace slug '{slug}' is already taken.", "error")
        return redirect("/app/settings?tab=workspace")
    ws.name, ws.slug = name[:200], slug
    audit(db, "workspace.updated", workspace_id=ws.id, actor_id=ctx.user.id, entity_type="workspace", entity_id=ws.id)
    db.commit()
    flash(request, "Workspace saved.")
    return redirect("/app/settings?tab=workspace")


@router.post("/app/settings/workspace/switch")
async def switch_workspace(request: Request, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    form = await request.form()
    wsid = form.get("workspace_id") or ""
    m = db.scalar(select(WorkspaceMember).where(WorkspaceMember.user_id == ctx.user.id, WorkspaceMember.workspace_id == wsid,
                                                WorkspaceMember.status == "active"))
    if not m:
        raise HTTPException(404, "Workspace not found")
    request.session["workspace_id"] = wsid
    flash(request, "Switched workspace.")
    return redirect("/app")


@router.post("/app/settings/billing/plan")
async def change_plan(request: Request, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    if ctx.role != "owner":
        raise Forbidden("Only the workspace owner can change the plan.")
    form = await request.form()
    plan = form.get("plan") or ""
    if plan not in PLANS:
        flash(request, "Unknown plan.", "error")
        return redirect("/app/settings?tab=billing")
    ws = db.get(Workspace, ctx.workspace.id)
    old = ws.plan_id
    ws.plan_id = plan
    seats = db.scalar(select(func.count(WorkspaceMember.id)).where(WorkspaceMember.workspace_id == ws.id,
                                                                   WorkspaceMember.status == "active")) or 1
    sub = db.scalar(select(Subscription).where(Subscription.workspace_id == ws.id).order_by(Subscription.created_at.desc()))
    if not sub:
        sub = Subscription(workspace_id=ws.id, plan=plan, seats=seats, status="active")
        db.add(sub)
    sub.plan, sub.seats, sub.status = plan, seats, "active"
    audit(db, "billing.plan_changed", workspace_id=ws.id, actor_id=ctx.user.id, entity_type="workspace",
          entity_id=ws.id, metadata={"from": old, "to": plan, "seats": seats})
    db.commit()
    flash(request, f"Plan changed to {PLANS[plan]['label']}. Features update immediately.")
    return redirect("/app/settings?tab=billing")


# ---------------------------------------------------------------- ACC-006 data rights
def build_export(db: Session, user: User) -> dict:
    def row(obj, cols):
        out = {}
        for c in cols:
            v = getattr(obj, c)
            out[c] = v.isoformat() if hasattr(v, "isoformat") else v
        return out

    schedules = db.scalars(select(AvailabilitySchedule).where(AvailabilitySchedule.user_id == user.id)).all()
    events = db.scalars(select(EventType).where(EventType.owner_user_id == user.id)).all()
    bookings = db.scalars(select(Booking).where(Booking.host_id == user.id).order_by(Booking.start_at)).all()
    return {
        "generated_at": utcnow().isoformat(),
        "user": row(user, ["id", "email", "name", "slug", "headline", "avatar_url", "default_timezone", "locale",
                           "working_hours_json", "notification_prefs_json", "branding_json", "email_verified_at",
                           "created_at"]),
        "memberships": [row(m, ["workspace_id", "role", "status", "created_at"]) for m in db.scalars(
            select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).all()],
        "availability_schedules": [{
            **row(s, ["id", "name", "timezone", "is_default", "holiday_country"]),
            "rules": [row(r, ["weekday", "start_local", "end_local"]) for r in db.scalars(
                select(AvailabilityRule).where(AvailabilityRule.schedule_id == s.id)).all()],
            "overrides": [row(o, ["local_date", "intervals_json", "kind", "reason"]) for o in db.scalars(
                select(AvailabilityOverride).where(AvailabilityOverride.schedule_id == s.id)).all()],
        } for s in schedules],
        "event_types": [{
            **row(e, ["id", "name", "slug", "description", "kind", "duration", "durations_json", "location_json",
                      "status", "visibility", "created_at", "deleted_at"]),
            "questions": [row(q, ["type", "label", "required", "options_json"]) for q in e.questions],
        } for e in events],
        "bookings_hosted": [{
            **row(b, ["id", "event_type_id", "start_at", "end_at", "status", "timezone", "location_json",
                      "cancel_reason", "no_show", "host_notes", "created_at"]),
            "invitees": [row(i, ["name", "email", "phone", "company", "timezone"]) for i in b.invitees],
        } for b in bookings],
        "notifications": [row(n, ["kind", "title", "body", "created_at", "read_at"]) for n in db.scalars(
            select(InAppNotification).where(InAppNotification.user_id == user.id)).all()],
    }


@router.post("/app/settings/data/export")
def request_export(request: Request, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    user = _user(db, ctx)
    dr = DataRequest(user_id=user.id, workspace_id=ctx.workspace.id, kind="export", status="completed",
                     result_json=build_export(db, user), completed_at=utcnow())
    db.add(dr)
    db.flush()
    audit(db, "data.export_requested", workspace_id=ctx.workspace.id, actor_id=user.id, entity_type="data_request",
          entity_id=dr.id)
    db.commit()
    flash(request, "Your data export is ready to download.")
    return redirect("/app/settings?tab=data")


@router.get("/app/settings/data/export/{rid}.json")
def download_export(rid: str, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    dr = db.get(DataRequest, rid)
    if not dr or dr.user_id != ctx.user.id or dr.kind != "export" or not dr.result_json:
        raise HTTPException(404, "Export not found")
    return Response(json.dumps(dr.result_json, indent=2, default=str), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="aal-data-export-{rid}.json"'})


@router.post("/app/settings/data/delete")
async def delete_account(request: Request, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    form = await request.form()
    user = _user(db, ctx)
    if (form.get("confirm_email") or "").strip().lower() != user.email.lower():
        flash(request, "Type your email address exactly to confirm account deletion.", "error")
        return redirect("/app/settings?tab=data")
    if user.password_hash and not verify_password(form.get("password") or "", user.password_hash):
        flash(request, "Password is incorrect.", "error")
        return redirect("/app/settings?tab=data")
    owned_shared = db.scalar(select(func.count(WorkspaceMember.id)).join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
                             .where(Workspace.owner_user_id == user.id, WorkspaceMember.user_id != user.id,
                                    WorkspaceMember.status == "active")) or 0
    if owned_shared:
        flash(request, "You own a workspace with other members. Transfer ownership before deleting your account.", "error")
        return redirect("/app/settings?tab=data")
    now = utcnow()
    db.add(DataRequest(user_id=user.id, workspace_id=ctx.workspace.id, kind="deletion", status="completed",
                       completed_at=now, result_json={"soft_deleted_at": now.isoformat()}))
    user.deleted_at = now
    for e in db.scalars(select(EventType).where(EventType.owner_user_id == user.id, EventType.deleted_at.is_(None))).all():
        e.status = "paused"
    for m in db.scalars(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).all():
        m.status = "deactivated"
    audit(db, "user.deleted", workspace_id=ctx.workspace.id, actor_id=user.id, entity_type="user", entity_id=user.id,
          ip=request.client.host if request.client else None)
    db.commit()
    logout_session(request)
    flash(request, "Your account has been deleted. Personal data will be purged per our retention policy.", "info")
    return redirect("/")
