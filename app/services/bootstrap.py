"""Shared helpers to provision a host: user + personal workspace + owner membership +
default availability schedule (Mon-Fri 09:00-17:00) + host lock row.
Used by signup, invitations, seed data and tests."""
import re
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AvailabilityRule,
    AvailabilitySchedule,
    HostLock,
    User,
    Workspace,
    WorkspaceMember,
    utcnow,
)
from app.security import hash_password

RESERVED_SLUGS = {
    "app", "api", "login", "logout", "signup", "static", "team", "booking", "embed", "embed.js", "r", "s",
    "poll", "recap", "webhooks", "scim", "dev", "healthz", "auth", "onboarding", "invite", "admin",
    "verify-email", "forgot-password", "reset-password", "d", "e", "pay", "settings", "help", "about",
    # platform pages added for the release gate / PWA / integrations
    "privacy", "terms", "data-retention", "legal", "offline", "icons", "manifest.webmanifest", "sw.js",
    "favicon.ico", "oauth", "sso", "saml", "m", "docs", "status", "pricing", "security",
}


def slugify(value: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return s[:60] or "user"


def unique_user_slug(db: Session, base: str) -> str:
    base = slugify(base)
    if base in RESERVED_SLUGS:
        base = base + "-1"
    slug, n = base, 1
    while db.scalar(select(User.id).where(User.slug == slug)):
        n += 1
        slug = f"{base}-{n}"
    return slug


def unique_workspace_slug(db: Session, base: str) -> str:
    base = slugify(base)
    slug, n = base, 1
    while db.scalar(select(Workspace.id).where(Workspace.slug == slug)):
        n += 1
        slug = f"{base}-{n}"
    return slug


def ensure_host_lock(db: Session, user_id: str):
    if not db.get(HostLock, user_id):
        db.add(HostLock(host_id=user_id, counter=0))


def create_default_schedule(db: Session, user: User, workspace_id: str, tz: Optional[str] = None) -> AvailabilitySchedule:
    sch = AvailabilitySchedule(
        workspace_id=workspace_id, user_id=user.id, name="Working hours", timezone=tz or user.default_timezone, is_default=True
    )
    db.add(sch)
    db.flush()
    for wd in range(5):
        db.add(AvailabilityRule(schedule_id=sch.id, weekday=wd, start_local="09:00", end_local="17:00"))
    return sch


def create_user_with_workspace(
    db: Session,
    email: str,
    name: str,
    password: Optional[str] = None,
    timezone: str = "UTC",
    slug: Optional[str] = None,
    plan_id: str = "teams",  # new workspaces start on a Teams trial so every feature is explorable
    verified: bool = False,
    workspace_name: Optional[str] = None,
) -> tuple[User, Workspace]:
    user = User(
        email=email.strip().lower(),
        name=name,
        password_hash=hash_password(password) if password else None,
        default_timezone=timezone,
        slug=unique_user_slug(db, slug or name or email.split("@")[0]),
        email_verified_at=utcnow() if verified else None,
        notification_prefs_json={"new_booking_email": True, "cancellation_email": True, "daily_agenda": False},
    )
    db.add(user)
    db.flush()
    ws = Workspace(
        name=workspace_name or f"{name or email.split('@')[0]}'s workspace",
        slug=unique_workspace_slug(db, workspace_name or user.slug),
        owner_user_id=user.id,
        plan_id=plan_id,
        settings_json={"retention_days": 365},
    )
    db.add(ws)
    db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="owner", status="active"))
    create_default_schedule(db, user, ws.id, timezone)
    ensure_host_lock(db, user.id)
    db.flush()
    return user, ws


def add_user_to_workspace(
    db: Session, workspace: Workspace, email: str, name: str, role: str = "member", password: Optional[str] = None,
    timezone: str = "UTC",
) -> User:
    """Create (or reuse) a user and make them an active member of an existing workspace."""
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if not user:
        user = User(
            email=email.strip().lower(), name=name, password_hash=hash_password(password) if password else None,
            default_timezone=timezone, slug=unique_user_slug(db, name or email.split("@")[0]), email_verified_at=utcnow(),
        )
        db.add(user)
        db.flush()
        create_default_schedule(db, user, workspace.id, timezone)
        ensure_host_lock(db, user.id)
    if not db.scalar(
        select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace.id, WorkspaceMember.user_id == user.id)
    ):
        db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=role, status="active"))
    db.flush()
    return user
