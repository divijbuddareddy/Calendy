"""Workspace members & roles (ACC-002/004), teams (PRD 10.1) and managed event templates (PRD 17.2).

Routes
  GET  /app/teams                                  members + teams overview
  POST /app/teams/invite                           invite by email (admin+; approved-domain restriction)
  POST /app/teams/members/{mid}/{action}           role | resend | revoke | deactivate | reactivate | remove
  POST /app/teams/create                           create team (admin+, plan feature "teams")
  GET  /app/teams/{team_id}                        team detail (members, event types, settings)
  POST /app/teams/{team_id}/edit | delete
  POST /app/teams/{team_id}/members/add | /members/{tmid}/update | /members/{tmid}/remove
  GET/POST /app/admin/managed-events ...           managed templates (admin+, feature "managed_events")
"""
from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit import audit
from app.config import settings
from app.db import get_db
from app.deps import ROLE_ORDER, Ctx, Forbidden, flash, get_ctx, redirect, render, require_role
from app.models import (
    EventQuestion,
    EventType,
    Team,
    TeamMember,
    User,
    WorkspaceMember,
    utcnow,
)
from app.ratelimit import client_ip
from app.security import opaque_token
from app.services.bootstrap import slugify

router = APIRouter()

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TEAM_ROLES = ("owner", "admin", "manager", "member")
WS_ROLES = ("owner", "admin", "member", "viewer")
ROLE_HELP = {
    "owner": "Full control incl. billing, roles and deletion",
    "admin": "Manage members, integrations, policies and reporting",
    "member": "Host meetings and manage their own event types",
    "viewer": "Read-only access",
}


# ================================================================ helpers
def _approved_domains(ws) -> list[str]:
    return [d.lower().lstrip("@") for d in ((ws.settings_json or {}).get("approved_domains") or []) if d]


def domain_allowed(ws, email: str) -> bool:
    domains = _approved_domains(ws)
    if not domains:
        return True
    dom = email.rsplit("@", 1)[-1].lower()
    return dom in domains


def _active_owner_count(db: Session, ws_id: str) -> int:
    return db.scalar(select(func.count(WorkspaceMember.id)).where(
        WorkspaceMember.workspace_id == ws_id, WorkspaceMember.role == "owner",
        WorkspaceMember.status == "active")) or 0


def _get_member(db: Session, ctx: Ctx, mid: str) -> WorkspaceMember:
    m = db.get(WorkspaceMember, mid)
    if not m or m.workspace_id != ctx.workspace.id:
        raise Forbidden("Member not found in this workspace.")
    return m


def _get_team(db: Session, ctx: Ctx, team_id: str) -> Team:
    t = db.get(Team, team_id)
    if not t or t.workspace_id != ctx.workspace.id:
        raise Forbidden("Team not found in this workspace.")
    return t


def _can_manage_team(db: Session, ctx: Ctx, team: Team) -> bool:
    if ctx.can("admin"):
        return True
    if ctx.role == "viewer":
        return False
    tm = db.scalar(select(TeamMember).where(TeamMember.team_id == team.id, TeamMember.user_id == ctx.user.id,
                                            TeamMember.active.is_(True)))
    return bool(tm and tm.role in ("owner", "admin", "manager"))


def _send_invite(db: Session, ctx: Ctx, m: WorkspaceMember) -> None:
    from app.services.messaging import send_email

    link = f"{settings.base_url}/invite/{m.invite_token}"
    inviter = ctx.user.name or ctx.user.email
    html = (f"<p>{inviter} invited you to join <strong>{ctx.workspace.name}</strong> on {settings.app_name} "
            f"as <strong>{m.role}</strong>.</p><p><a href=\"{link}\">Accept invitation</a></p>"
            f"<p style=\"color:#666\">If you weren't expecting this, you can ignore this email.</p>")
    send_email(db, m.invite_email, f"You're invited to {ctx.workspace.name}", html)


def _members(db: Session, ws_id: str) -> list[tuple[WorkspaceMember, Optional[User]]]:
    rows = db.scalars(select(WorkspaceMember).where(WorkspaceMember.workspace_id == ws_id)
                      .order_by(WorkspaceMember.created_at)).all()
    users = {u.id: u for u in db.scalars(select(User).where(User.id.in_({m.user_id for m in rows if m.user_id} or {""})))}
    order = {"active": 0, "invited": 1, "deactivated": 2}
    out = [(m, users.get(m.user_id)) for m in rows]
    out.sort(key=lambda r: (order.get(r[0].status, 3), -ROLE_ORDER.get(r[0].role, 0)))
    return out


# ================================================================ members
@router.get("/app/teams")
def teams_home(request: Request, tab: str = "members", ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    teams = db.scalars(select(Team).where(Team.workspace_id == ctx.workspace.id).order_by(Team.name)).all()
    counts = dict(db.execute(select(TeamMember.team_id, func.count(TeamMember.id))
                             .where(TeamMember.team_id.in_([t.id for t in teams] or [""]), TeamMember.active.is_(True))
                             .group_by(TeamMember.team_id)).all())
    ev_counts = dict(db.execute(select(EventType.team_id, func.count(EventType.id))
                                .where(EventType.team_id.in_([t.id for t in teams] or [""]), EventType.deleted_at.is_(None))
                                .group_by(EventType.team_id)).all())
    return render(request, "app/teams/index.html", ctx, nav="teams", tab=tab, members=_members(db, ctx.workspace.id),
                  teams=teams, counts=counts, ev_counts=ev_counts, roles=WS_ROLES, role_help=ROLE_HELP,
                  approved_domains=_approved_domains(ctx.workspace), owner_count=_active_owner_count(db, ctx.workspace.id))


@router.post("/app/teams/invite")
async def invite_member(request: Request, ctx: Ctx = Depends(require_role("admin")), db: Session = Depends(get_db)):
    form = await request.form()
    emails = [e.strip().lower() for e in re.split(r"[,\s;]+", form.get("email") or "") if e.strip()]
    role = form.get("role") or "member"
    if role not in WS_ROLES:
        role = "member"
    if role in ("owner", "admin") and ctx.role != "owner":
        flash(request, "Only workspace owners can invite admins or owners.", "error")
        return redirect("/app/teams")
    if not emails:
        flash(request, "Enter at least one email address.", "error")
        return redirect("/app/teams")
    sent, errors = 0, []
    for email in emails[:50]:
        if not EMAIL_RE.match(email):
            errors.append(f"{email}: not a valid email")
            continue
        if not domain_allowed(ctx.workspace, email):
            errors.append(f"{email}: domain not approved for this workspace")
            continue
        user = db.scalar(select(User).where(func.lower(User.email) == email))
        dup = db.scalar(select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == ctx.workspace.id,
            (func.lower(WorkspaceMember.invite_email) == email) | (WorkspaceMember.user_id == (user.id if user else "-"))))
        if dup and dup.status != "deactivated":
            errors.append(f"{email}: already {'invited' if dup.status == 'invited' else 'a member'}")
            continue
        if dup:  # previously deactivated -> re-invite
            dup.status, dup.role = "invited", role
            dup.invite_email, dup.invite_token, dup.invited_at = email, opaque_token(), utcnow()
            m = dup
        else:
            m = WorkspaceMember(workspace_id=ctx.workspace.id, user_id=None, role=role, status="invited",
                                invite_email=email, invite_token=opaque_token(), invited_at=utcnow())
            db.add(m)
        db.flush()
        _send_invite(db, ctx, m)
        audit(db, "member.invited", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="workspace_member",
              entity_id=m.id, metadata={"email": email, "role": role}, ip=client_ip(request))
        sent += 1
    db.commit()
    if sent:
        flash(request, f"Sent {sent} invitation{'s' if sent != 1 else ''}.")
    if errors:
        flash(request, "Not invited — " + "; ".join(errors), "error")
    return redirect("/app/teams")


@router.post("/app/teams/members/{mid}/{action}")
async def member_action(request: Request, mid: str, action: str, ctx: Ctx = Depends(require_role("admin")),
                        db: Session = Depends(get_db)):
    m = _get_member(db, ctx, mid)
    form = await request.form()
    ip = client_ip(request)
    is_owner_target = m.role in ("owner", "admin")
    label = m.invite_email or (db.get(User, m.user_id).email if m.user_id and db.get(User, m.user_id) else m.id)

    def fail(msg: str):
        flash(request, msg, "error")
        return redirect("/app/teams")

    if action == "role":
        new = form.get("role")
        if new not in WS_ROLES:
            return fail("Unknown role.")
        if (new in ("owner", "admin") or is_owner_target) and ctx.role != "owner":
            return fail("Only workspace owners can grant or change admin/owner roles.")
        if m.role == "owner" and new != "owner" and m.status == "active" and _active_owner_count(db, ctx.workspace.id) <= 1:
            return fail("A workspace must keep at least one owner. Promote someone else first.")
        old = m.role
        if old == new:
            return redirect("/app/teams")
        m.role = new
        audit(db, "member.role_changed", workspace_id=ctx.workspace.id, actor_id=ctx.user.id,
              entity_type="workspace_member", entity_id=m.id, metadata={"from": old, "to": new, "member": label}, ip=ip)
        flash(request, f"{label} is now {new}.")
    elif action in ("resend", "revoke"):
        if m.status != "invited":
            return fail("That member has no pending invitation.")
        if action == "resend":
            m.invite_token = opaque_token()
            m.invited_at = utcnow()
            _send_invite(db, ctx, m)
            flash(request, f"Invitation re-sent to {m.invite_email}.")
        else:
            db.delete(m)
            flash(request, f"Invitation for {m.invite_email} revoked.")
        audit(db, f"member.invite_{action}", workspace_id=ctx.workspace.id, actor_id=ctx.user.id,
              entity_type="workspace_member", entity_id=m.id, metadata={"email": m.invite_email}, ip=ip)
    elif action in ("deactivate", "remove", "reactivate"):
        if m.user_id == ctx.user.id and action != "reactivate":
            return fail("You can't deactivate or remove yourself.")
        if is_owner_target and ctx.role != "owner":
            return fail("Only owners can deactivate or remove admins and owners.")
        if m.role == "owner" and m.status == "active" and action != "reactivate" and _active_owner_count(db, ctx.workspace.id) <= 1:
            return fail("You can't remove the last owner.")
        if action == "reactivate":
            if m.status != "deactivated":
                return fail("Member is not deactivated.")
            if not m.user_id:
                return fail("Re-invite this person instead.")
            m.status = "active"
            flash(request, f"{label} reactivated.")
        else:
            if m.user_id:  # pull them out of the workspace's teams as well
                team_ids = select(Team.id).where(Team.workspace_id == ctx.workspace.id)
                for tm in db.scalars(select(TeamMember).where(TeamMember.user_id == m.user_id,
                                                              TeamMember.team_id.in_(team_ids))):
                    if action == "remove":
                        db.delete(tm)
                    else:
                        tm.active = False
            if action == "remove":
                db.delete(m)
                flash(request, f"{label} removed from the workspace.")
            else:
                m.status = "deactivated"
                m.invite_token = None
                flash(request, f"{label} deactivated. Their history is kept.")
        audit(db, {"deactivate": "member.deactivated", "remove": "member.removed", "reactivate": "member.reactivated"}[action],
              workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="workspace_member", entity_id=m.id,
              metadata={"member": label, "role": m.role}, ip=ip)
    else:
        return fail("Unknown action.")
    db.commit()
    return redirect("/app/teams")


# ================================================================ teams
def _require_teams_feature(ctx: Ctx):
    if not ctx.feature("teams"):
        raise Forbidden("Teams are available on the Teams plan. Upgrade in Settings > Billing.")


def _validate_logo(url: str) -> Optional[str]:
    url = (url or "").strip()
    if not url:
        return None
    if not re.match(r"^https?://", url):
        raise ValueError("Logo URL must start with http:// or https://")
    return url[:500]


@router.post("/app/teams/create")
async def create_team(request: Request, ctx: Ctx = Depends(require_role("admin")), db: Session = Depends(get_db)):
    _require_teams_feature(ctx)
    form = await request.form()
    name = (form.get("name") or "").strip()
    slug = slugify(form.get("slug") or name)
    if not name:
        flash(request, "Team name is required.", "error")
        return redirect("/app/teams?tab=teams")
    if db.scalar(select(Team.id).where(Team.slug == slug)):
        flash(request, f"The team link '/team/{slug}' is already taken.", "error")
        return redirect("/app/teams?tab=teams")
    try:
        logo = _validate_logo(form.get("logo_url"))
    except ValueError as e:
        flash(request, str(e), "error")
        return redirect("/app/teams?tab=teams")
    t = Team(workspace_id=ctx.workspace.id, name=name[:200], slug=slug,
             description=(form.get("description") or "").strip() or None, logo_url=logo)
    db.add(t)
    db.flush()
    db.add(TeamMember(team_id=t.id, user_id=ctx.user.id, role="owner", priority=1, active=True))
    audit(db, "team.created", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="team", entity_id=t.id,
          metadata={"name": t.name, "slug": slug})
    db.commit()
    flash(request, f"Team “{t.name}” created.")
    return redirect(f"/app/teams/{t.id}")


@router.get("/app/teams/{team_id}")
def team_detail(request: Request, team_id: str, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    t = _get_team(db, ctx, team_id)
    tms = db.scalars(select(TeamMember).where(TeamMember.team_id == t.id).order_by(TeamMember.priority.desc())).all()
    users = {u.id: u for u in db.scalars(select(User).where(User.id.in_([m.user_id for m in tms] or [""])))}
    in_team = {m.user_id for m in tms}
    candidates = [(m, u) for m, u in _members(db, ctx.workspace.id)
                  if u and m.status == "active" and u.id not in in_team]
    events = db.scalars(select(EventType).where(EventType.team_id == t.id, EventType.workspace_id == ctx.workspace.id,
                                                EventType.deleted_at.is_(None)).order_by(EventType.created_at)).all()
    return render(request, "app/teams/detail.html", ctx, nav="teams", team=t, tms=tms, users=users,
                  candidates=candidates, events=events, team_roles=TEAM_ROLES,
                  can_manage=_can_manage_team(db, ctx, t), public_url=f"{settings.base_url}/team/{t.slug}")


@router.post("/app/teams/{team_id}/edit")
async def edit_team(request: Request, team_id: str, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    t = _get_team(db, ctx, team_id)
    if not _can_manage_team(db, ctx, t):
        raise Forbidden("You can't edit this team.")
    form = await request.form()
    name = (form.get("name") or "").strip()
    slug = slugify(form.get("slug") or t.slug)
    if not name:
        flash(request, "Team name is required.", "error")
        return redirect(f"/app/teams/{t.id}")
    if slug != t.slug and db.scalar(select(Team.id).where(Team.slug == slug)):
        flash(request, f"The team link '/team/{slug}' is already taken.", "error")
        return redirect(f"/app/teams/{t.id}")
    try:
        t.logo_url = _validate_logo(form.get("logo_url"))
    except ValueError as e:
        flash(request, str(e), "error")
        return redirect(f"/app/teams/{t.id}")
    t.name, t.slug = name[:200], slug
    t.description = (form.get("description") or "").strip() or None
    audit(db, "team.updated", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="team", entity_id=t.id,
          metadata={"name": t.name, "slug": slug})
    db.commit()
    flash(request, "Team saved.")
    return redirect(f"/app/teams/{t.id}")


@router.post("/app/teams/{team_id}/delete")
def delete_team(request: Request, team_id: str, ctx: Ctx = Depends(require_role("admin")), db: Session = Depends(get_db)):
    t = _get_team(db, ctx, team_id)
    n = db.scalar(select(func.count(EventType.id)).where(EventType.team_id == t.id, EventType.deleted_at.is_(None))) or 0
    if n:
        flash(request, f"Move or delete the team's {n} event type(s) before deleting the team.", "error")
        return redirect(f"/app/teams/{t.id}")
    for tm in db.scalars(select(TeamMember).where(TeamMember.team_id == t.id)):
        db.delete(tm)
    audit(db, "team.deleted", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="team", entity_id=t.id,
          metadata={"name": t.name})
    db.delete(t)
    db.commit()
    flash(request, "Team deleted.")
    return redirect("/app/teams?tab=teams")


@router.post("/app/teams/{team_id}/members/add")
async def add_team_member(request: Request, team_id: str, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    t = _get_team(db, ctx, team_id)
    if not _can_manage_team(db, ctx, t):
        raise Forbidden("You can't manage this team's members.")
    form = await request.form()
    uid = form.get("user_id")
    role = form.get("role") if form.get("role") in TEAM_ROLES else "member"
    if role in ("owner", "admin") and not ctx.can("admin"):
        role = "member"
    wm = db.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == ctx.workspace.id,
                                                 WorkspaceMember.user_id == uid, WorkspaceMember.status == "active"))
    if not wm:
        flash(request, "Only active workspace members can join a team. Invite them first.", "error")
        return redirect(f"/app/teams/{t.id}")
    if db.scalar(select(TeamMember.id).where(TeamMember.team_id == t.id, TeamMember.user_id == uid)):
        flash(request, "Already in this team.", "error")
        return redirect(f"/app/teams/{t.id}")
    try:
        priority = max(0, min(10, int(form.get("priority") or 1)))
    except ValueError:
        priority = 1
    db.add(TeamMember(team_id=t.id, user_id=uid, role=role, priority=priority, active=True))
    audit(db, "team.member_added", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="team",
          entity_id=t.id, metadata={"user_id": uid, "role": role})
    db.commit()
    flash(request, "Member added to team.")
    return redirect(f"/app/teams/{t.id}")


@router.post("/app/teams/{team_id}/members/{tmid}/{action}")
async def team_member_action(request: Request, team_id: str, tmid: str, action: str, ctx: Ctx = Depends(get_ctx),
                             db: Session = Depends(get_db)):
    t = _get_team(db, ctx, team_id)
    if not _can_manage_team(db, ctx, t):
        raise Forbidden("You can't manage this team's members.")
    tm = db.get(TeamMember, tmid)
    if not tm or tm.team_id != t.id:
        raise Forbidden("Team member not found.")
    form = await request.form()
    if action == "update":
        role = form.get("role")
        if role in TEAM_ROLES and role != tm.role:
            if (role in ("owner", "admin") or tm.role in ("owner", "admin")) and not ctx.can("admin"):
                flash(request, "Only workspace admins can grant team owner/admin.", "error")
                return redirect(f"/app/teams/{t.id}")
            audit(db, "team.member_role_changed", workspace_id=ctx.workspace.id, actor_id=ctx.user.id,
                  entity_type="team", entity_id=t.id, metadata={"user_id": tm.user_id, "from": tm.role, "to": role})
            tm.role = role
        try:
            tm.priority = max(0, min(10, int(form.get("priority") or tm.priority)))
        except ValueError:
            pass
        tm.active = form.get("active") in ("1", "on", "true")
        flash(request, "Team member updated.")
    elif action == "remove":
        audit(db, "team.member_removed", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="team",
              entity_id=t.id, metadata={"user_id": tm.user_id})
        db.delete(tm)
        flash(request, "Removed from team.")
    else:
        raise Forbidden("Unknown action.")
    db.commit()
    return redirect(f"/app/teams/{t.id}")


# ================================================================ managed event templates (admin)
def _managed_ctx(ctx: Ctx = Depends(require_role("admin"))) -> Ctx:
    if not ctx.feature("managed_events"):
        raise Forbidden("Managed event templates are available on the Teams plan. Upgrade in Settings > Billing.")
    return ctx


def _get_template(db: Session, ctx: Ctx, tid: str) -> EventType:
    t = db.get(EventType, tid)
    if not t or t.workspace_id != ctx.workspace.id or not t.is_managed_template or t.deleted_at:
        raise Forbidden("Template not found.")
    return t


def _location_types() -> dict:
    try:
        from app.routers.event_types import LOCATION_TYPES

        return LOCATION_TYPES
    except Exception:
        return {"google_meet": "Google Meet", "zoom": "Zoom", "phone": "Phone call", "in_person": "In person",
                "custom_url": "Custom link"}


def _apply_template_form(t: EventType, form) -> Optional[str]:
    from app.services.managed import LOCKABLE_SECTIONS

    name = (form.get("name") or "").strip()
    if not name:
        return "Name is required."
    try:
        duration = int(form.get("duration") or 30)
        min_notice = int(form.get("min_notice_minutes") or 0)
        buf_b = int(form.get("buffer_before") or 0)
        buf_a = int(form.get("buffer_after") or 0)
    except ValueError:
        return "Duration, notice and buffers must be numbers."
    if not 5 <= duration <= 1440:
        return "Duration must be between 5 and 1440 minutes."
    t.name, t.duration = name[:200], duration
    t.description = (form.get("description") or "").strip() or None
    t.min_notice_minutes, t.buffer_before, t.buffer_after = max(0, min_notice), max(0, buf_b), max(0, buf_a)
    loc = form.get("location") or "google_meet"
    t.location_json = [{"type": loc, "value": (form.get("location_value") or "").strip()}]
    policy = dict(t.cancellation_policy_json or {})
    policy["policy_text"] = (form.get("policy_text") or "").strip()
    t.cancellation_policy_json = policy
    t.locked_fields_json = [k for k in LOCKABLE_SECTIONS if form.get(f"lock_{k}")]
    return None


@router.get("/app/admin/managed-events")
def managed_list(request: Request, ctx: Ctx = Depends(_managed_ctx), db: Session = Depends(get_db)):
    from app.services.managed import LOCKABLE_SECTIONS

    templates_ = db.scalars(select(EventType).where(EventType.workspace_id == ctx.workspace.id,
                                                    EventType.is_managed_template.is_(True),
                                                    EventType.deleted_at.is_(None)).order_by(EventType.created_at)).all()
    child_counts = dict(db.execute(select(EventType.managed_template_id, func.count(EventType.id)).where(
        EventType.workspace_id == ctx.workspace.id, EventType.managed_template_id.is_not(None),
        EventType.deleted_at.is_(None)).group_by(EventType.managed_template_id)).all())
    return render(request, "app/admin/managed_list.html", ctx, nav="admin", tab="managed", templates_=templates_,
                  child_counts=child_counts, sections=LOCKABLE_SECTIONS, location_types=_location_types(),
                  t=None, loc=None, policy=None)


@router.post("/app/admin/managed-events")
async def managed_create(request: Request, ctx: Ctx = Depends(_managed_ctx), db: Session = Depends(get_db)):
    from app.services.managed import TEMPLATE_SUFFIX

    form = await request.form()
    base = slugify(form.get("slug") or form.get("name") or "managed")
    slug = base + TEMPLATE_SUFFIX
    n = 1
    while db.scalar(select(EventType.id).where(EventType.owner_user_id == ctx.user.id, EventType.slug == slug)):
        n += 1
        slug = f"{base}-{n}{TEMPLATE_SUFFIX}"
    t = EventType(workspace_id=ctx.workspace.id, owner_user_id=ctx.user.id, name="", slug=slug, kind="one_on_one",
                  is_managed_template=True, status="paused", visibility="private", min_notice_minutes=240,
                  cancellation_policy_json={"allow_cancel": True, "allow_reschedule": True, "min_notice_minutes": 0,
                                            "require_reason": False, "host_approval": False, "policy_text": ""},
                  confirmation_json={"message": "", "redirect_url": "", "pass_params": False, "links": []})
    err = _apply_template_form(t, form)
    if err:
        flash(request, err, "error")
        return redirect("/app/admin/managed-events")
    if form.get("team_id"):
        team = db.get(Team, form.get("team_id"))
        t.team_id = team.id if team and team.workspace_id == ctx.workspace.id else None
    db.add(t)
    db.flush()
    audit(db, "managed_template.created", workspace_id=ctx.workspace.id, actor_id=ctx.user.id,
          entity_type="event_type", entity_id=t.id, metadata={"name": t.name, "locked": t.locked_fields_json})
    db.commit()
    flash(request, "Template created. Add questions and assign it to members.")
    return redirect(f"/app/admin/managed-events/{t.id}")


@router.get("/app/admin/managed-events/{tid}")
def managed_detail(request: Request, tid: str, ctx: Ctx = Depends(_managed_ctx), db: Session = Depends(get_db)):
    from app.services.managed import LOCKABLE_SECTIONS, children

    t = _get_template(db, ctx, tid)
    kids = children(db, t)
    users = {u.id: u for u, in db.execute(select(User).join(WorkspaceMember, WorkspaceMember.user_id == User.id).where(
        WorkspaceMember.workspace_id == ctx.workspace.id, WorkspaceMember.status == "active"))}
    assigned = {k.owner_user_id for k in kids}
    return render(request, "app/admin/managed_detail.html", ctx, nav="admin", tab="managed", t=t, kids=kids,
                  users=users, assigned=assigned, sections=LOCKABLE_SECTIONS, location_types=_location_types(),
                  loc=(t.location_json or [{}])[0], policy=t.cancellation_policy_json or {})


@router.post("/app/admin/managed-events/{tid}")
async def managed_update(request: Request, tid: str, ctx: Ctx = Depends(_managed_ctx), db: Session = Depends(get_db)):
    t = _get_template(db, ctx, tid)
    form = await request.form()
    err = _apply_template_form(t, form)
    if err:
        db.rollback()
        flash(request, err, "error")
        return redirect(f"/app/admin/managed-events/{tid}")
    t.version = (t.version or 1) + 1
    audit(db, "managed_template.updated", workspace_id=ctx.workspace.id, actor_id=ctx.user.id,
          entity_type="event_type", entity_id=t.id, metadata={"locked": t.locked_fields_json, "version": t.version})
    db.commit()
    flash(request, "Template saved. Use “Sync to members” to push locked sections.")
    return redirect(f"/app/admin/managed-events/{tid}")


@router.post("/app/admin/managed-events/{tid}/questions")
async def managed_add_question(request: Request, tid: str, ctx: Ctx = Depends(_managed_ctx),
                               db: Session = Depends(get_db)):
    t = _get_template(db, ctx, tid)
    form = await request.form()
    label = (form.get("label") or "").strip()
    if label:
        qtype = form.get("type") if form.get("type") in ("text", "textarea", "phone", "select", "radio", "checkbox",
                                                           "email", "number", "url") else "text"
        opts = [o.strip() for o in (form.get("options") or "").split(",") if o.strip()] or None
        db.add(EventQuestion(event_type_id=t.id, type=qtype, label=label[:300], required=bool(form.get("required")),
                             options_json=opts, sort_order=len(t.questions) + 1))
        t.version = (t.version or 1) + 1
        db.commit()
        flash(request, "Question added.")
    return redirect(f"/app/admin/managed-events/{tid}")


@router.post("/app/admin/managed-events/{tid}/questions/{qid}/delete")
def managed_del_question(request: Request, tid: str, qid: str, ctx: Ctx = Depends(_managed_ctx),
                         db: Session = Depends(get_db)):
    t = _get_template(db, ctx, tid)
    q = db.get(EventQuestion, qid)
    if q and q.event_type_id == t.id:
        db.delete(q)
        t.version = (t.version or 1) + 1
        db.commit()
    return redirect(f"/app/admin/managed-events/{tid}")


@router.post("/app/admin/managed-events/{tid}/assign")
async def managed_assign(request: Request, tid: str, ctx: Ctx = Depends(_managed_ctx), db: Session = Depends(get_db)):
    from app.services.managed import assign

    t = _get_template(db, ctx, tid)
    form = await request.form()
    valid = set(db.scalars(select(WorkspaceMember.user_id).where(WorkspaceMember.workspace_id == ctx.workspace.id,
                                                                WorkspaceMember.status == "active")))
    uids = [u for u in form.getlist("user_ids") if u in valid]
    created = assign(db, t, uids)
    audit(db, "managed_template.assigned", workspace_id=ctx.workspace.id, actor_id=ctx.user.id,
          entity_type="event_type", entity_id=t.id, metadata={"user_ids": [c.owner_user_id for c in created]})
    db.commit()
    flash(request, f"Assigned to {len(created)} member(s).")
    return redirect(f"/app/admin/managed-events/{tid}")


@router.post("/app/admin/managed-events/{tid}/sync")
def managed_sync(request: Request, tid: str, ctx: Ctx = Depends(_managed_ctx), db: Session = Depends(get_db)):
    from app.services.managed import sync

    t = _get_template(db, ctx, tid)
    n = sync(db, t)
    audit(db, "managed_template.synced", workspace_id=ctx.workspace.id, actor_id=ctx.user.id,
          entity_type="event_type", entity_id=t.id, metadata={"children": n, "version": t.version})
    db.commit()
    flash(request, f"Synced locked sections to {n} event type(s).")
    return redirect(f"/app/admin/managed-events/{tid}")


@router.post("/app/admin/managed-events/{tid}/children/{cid}/detach")
def managed_detach(request: Request, tid: str, cid: str, ctx: Ctx = Depends(_managed_ctx),
                   db: Session = Depends(get_db)):
    from app.services.managed import detach

    t = _get_template(db, ctx, tid)
    c = db.get(EventType, cid)
    if c and c.managed_template_id == t.id and c.workspace_id == ctx.workspace.id:
        detach(db, c)
        audit(db, "managed_template.detached", workspace_id=ctx.workspace.id, actor_id=ctx.user.id,
              entity_type="event_type", entity_id=c.id, metadata={"template_id": t.id})
        db.commit()
        flash(request, "Event type detached; the member can now edit every field.")
    return redirect(f"/app/admin/managed-events/{tid}")


@router.post("/app/admin/managed-events/{tid}/delete")
def managed_delete(request: Request, tid: str, ctx: Ctx = Depends(_managed_ctx), db: Session = Depends(get_db)):
    from app.services.managed import children, detach

    t = _get_template(db, ctx, tid)
    for c in children(db, t):
        detach(db, c)
    t.deleted_at = utcnow()
    t.status = "archived"
    audit(db, "managed_template.deleted", workspace_id=ctx.workspace.id, actor_id=ctx.user.id,
          entity_type="event_type", entity_id=t.id, metadata={"name": t.name})
    db.commit()
    flash(request, "Template deleted; existing member event types were kept and unlocked.")
    return redirect("/app/admin/managed-events")
