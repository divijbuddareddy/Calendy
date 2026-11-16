"""Authentication (ACC-001/003): signup, email verification, login/logout, password reset,
Google OAuth sign-in, invitation acceptance, and the onboarding wizard."""
from __future__ import annotations

import re
import secrets
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import ratelimit
from app.audit import audit
from app.config import settings
from app.db import get_db
from app.deps import Ctx, flash, get_ctx, login_session, logout_session, optional_ctx, redirect, render
from app.deps import templates as _templates

# One-click demo login on the login page (hidden in production).
_templates.env.globals["demo_enabled"] = settings.env != "prod"
from app.models import (
    AvailabilityRule,
    AvailabilitySchedule,
    EventType,
    User,
    Workspace,
    WorkspaceMember,
    utcnow,
)
from app.security import hash_password, sign, unsign, verify_password
from app.services.bootstrap import (
    RESERVED_SLUGS,
    create_default_schedule,
    create_user_with_workspace,
    ensure_host_lock,
    slugify,
    unique_user_slug,
)
from app.services.hostutil import all_timezones, event_public_url, valid_tz

router = APIRouter()

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
VERIFY_SALT = "verify-email"
RESET_SALT = "reset-password"
OAUTH_SALT = "google-oauth-state"
VERIFY_MAX_AGE = 3 * 24 * 3600
RESET_MAX_AGE = 3600


def _safe_next(nxt: Optional[str]) -> str:
    if nxt and nxt.startswith("/") and not nxt.startswith("//"):
        return nxt
    return "/app"


def _base() -> str:
    return settings.base_url.rstrip("/")


def _email_shell(title: str, body: str, cta_label: str, cta_url: str) -> str:
    return (
        f'<div style="font-family:system-ui,sans-serif;max-width:520px;margin:auto;color:#1a1f36">'
        f"<h2>{title}</h2><p>{body}</p>"
        f'<p><a href="{cta_url}" style="display:inline-block;background:#3b47d6;color:#fff;padding:10px 18px;'
        f'border-radius:6px;text-decoration:none;font-weight:600">{cta_label}</a></p>'
        f'<p style="color:#5b6380;font-size:13px">Or paste this link into your browser:<br>{cta_url}</p>'
        f'<p style="color:#5b6380;font-size:13px">— {settings.app_name}</p></div>'
    )


def verification_link(user: User) -> str:
    return f"{_base()}/verify-email?token={sign({'uid': user.id, 'email': user.email}, VERIFY_SALT)}"


def send_verification_email(db: Session, user: User):
    from app.services.messaging import send_email

    link = verification_link(user)
    send_email(
        db, user.email, f"Verify your email for {settings.app_name}",
        _email_shell("Confirm your email", "Verify your email address to activate your public booking links.",
                     "Verify email", link),
    )


def reset_token(user: User) -> str:
    # Including a fingerprint of the current hash makes the link single-use (it changes after reset).
    return sign({"uid": user.id, "ph": (user.password_hash or "none")[-12:]}, RESET_SALT)


# ================================================================ signup
@router.get("/signup")
def signup_page(request: Request, ctx: Optional[Ctx] = Depends(optional_ctx)):
    if ctx:
        return redirect("/app")
    return render(request, "auth/signup.html", google_enabled=bool(settings.google_client_id), form={}, error=None)


@router.post("/signup")
async def signup(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    name = (form.get("name") or "").strip()
    email = (form.get("email") or "").strip().lower()
    password = form.get("password") or ""
    tz = (form.get("timezone") or "UTC").strip()
    data = {"name": name, "email": email, "timezone": tz}
    error = None
    if not ratelimit.check(f"signup:{ratelimit.client_ip(request)}", 10, 3600):
        error = "Too many sign-up attempts. Please try again later."
    elif not name:
        error = "Please enter your name."
    elif not EMAIL_RE.match(email):
        error = "Please enter a valid email address."
    elif len(password) < 8:
        error = "Password must be at least 8 characters."
    elif db.scalar(select(User.id).where(func.lower(User.email) == email)):
        error = "An account with this email already exists. Try logging in."
    if error:
        return render(request, "auth/signup.html", google_enabled=bool(settings.google_client_id), form=data,
                      error=error, status_code=400)
    if not valid_tz(tz):
        tz = "UTC"
    user, ws = create_user_with_workspace(db, email, name, password=password, timezone=tz)
    send_verification_email(db, user)
    audit(db, "user.signup", workspace_id=ws.id, actor_id=user.id, entity_type="user", entity_id=user.id,
          ip=ratelimit.client_ip(request))
    db.commit()
    login_session(request, user, ws.id)
    flash(request, "Welcome! We sent a verification link to your email.")
    return redirect("/onboarding")


# ================================================================ email verification
@router.get("/verify-email")
def verify_email(request: Request, token: str = "", db: Session = Depends(get_db)):
    data = unsign(token, VERIFY_SALT, max_age=VERIFY_MAX_AGE)
    user = db.get(User, data["uid"]) if isinstance(data, dict) else None
    if not user or user.email != data.get("email"):
        return render(request, "auth/message.html", title="Link invalid or expired",
                      message="This verification link is invalid or has expired. Log in to request a new one.",
                      status_code=400)
    if not user.email_verified_at:
        user.email_verified_at = utcnow()
        audit(db, "user.email_verified", actor_id=user.id, entity_type="user", entity_id=user.id)
        db.commit()
    flash(request, "Email verified — your booking links are now live.")
    return redirect("/app" if request.session.get("user_id") == user.id else "/login")


@router.post("/verify-email/resend")
def resend_verification(request: Request, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    if ctx.user.email_verified_at:
        flash(request, "Your email is already verified.", "info")
    elif not ratelimit.check(f"resend-verify:{ctx.user.id}", 5, 3600):
        flash(request, "Too many requests. Please try again later.", "error")
    else:
        user = db.get(User, ctx.user.id)
        send_verification_email(db, user)
        db.commit()
        flash(request, f"Verification email sent to {user.email}.")
    return redirect(_safe_next(request.headers.get("referer", "").replace(_base(), "") or "/app"))


# ================================================================ login / logout
@router.get("/login")
def login_page(request: Request, next: str = "", ctx: Optional[Ctx] = Depends(optional_ctx)):
    if ctx:
        return redirect(_safe_next(next))
    return render(request, "auth/login.html", google_enabled=bool(settings.google_client_id), next=next, email="",
                  error=None)


def _login_method_allowed(db: Session, email: str, method: str) -> bool:
    """Workspace security policy (allowed login methods / SSO enforcement); optional module."""
    if not email:
        return True
    try:
        from app.services.sso import login_method_allowed
    except ImportError:
        return True
    try:
        return bool(login_method_allowed(db, email, method))
    except Exception:
        import logging

        logging.getLogger("app.auth").exception("login_method_allowed failed")
        return True


def _sso_redirect(db: Session, email: str, nxt: str = "") -> Optional[str]:
    """Enterprise SSO enforcement: if the email's domain requires SSO, send the user to the IdP
    instead of checking a password. The SSO module is optional (lazy import)."""
    if not email:
        return None
    try:
        from app.services.sso import sso_required_for_email
    except ImportError:
        return None
    try:
        url = sso_required_for_email(db, email)
    except Exception:  # never let an SSO misconfiguration lock out password login silently
        import logging

        logging.getLogger("app.auth").exception("sso_required_for_email failed")
        return None
    if url and nxt and "next=" not in url:
        url += ("&" if "?" in url else "?") + urlencode({"next": _safe_next(nxt)})
    return url or None


@router.post("/login")
async def login(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    password = form.get("password") or ""
    nxt = form.get("next") or ""
    ip = ratelimit.client_ip(request)
    if not (ratelimit.check(f"login-ip:{ip}", 20, 300) and ratelimit.check(f"login-email:{email}", 10, 300)):
        return render(request, "auth/login.html", google_enabled=bool(settings.google_client_id), next=nxt,
                      email=email, error="Too many attempts. Please wait a few minutes and try again.", status_code=429)
    sso_url = _sso_redirect(db, email, nxt)
    if sso_url:
        return redirect(sso_url)
    if not _login_method_allowed(db, email, "password"):
        return render(request, "auth/login.html", google_enabled=bool(settings.google_client_id), next=nxt,
                      email=email, error="Your organisation doesn't allow password sign-in. Use single sign-on "
                                         "or the sign-in method your admin enabled.", status_code=403)
    user = db.scalar(select(User).where(func.lower(User.email) == email))
    if not user or user.deleted_at or not verify_password(password, user.password_hash):
        return render(request, "auth/login.html", google_enabled=bool(settings.google_client_id), next=nxt,
                      email=email, error="Invalid email or password.", status_code=400)
    login_session(request, user)
    audit(db, "user.login", actor_id=user.id, entity_type="user", entity_id=user.id, ip=ip)
    db.commit()
    if not user.onboarding_completed:
        return redirect("/onboarding")
    return redirect(_safe_next(nxt))


@router.post("/logout")
def logout(request: Request):
    logout_session(request)
    return redirect("/login")


# ================================================================ password reset
@router.get("/forgot-password")
def forgot_page(request: Request):
    return render(request, "auth/forgot.html", sent=False)


@router.post("/forgot-password")
async def forgot(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    if ratelimit.check(f"forgot:{ratelimit.client_ip(request)}", 5, 900):
        user = db.scalar(select(User).where(func.lower(User.email) == email, User.deleted_at.is_(None)))
        if user:
            from app.services.messaging import send_email

            link = f"{_base()}/reset-password?token={reset_token(user)}"
            send_email(db, user.email, f"Reset your {settings.app_name} password",
                       _email_shell("Reset your password", "Use the button below to choose a new password. "
                                    "This link expires in 1 hour. If you didn't ask for this, ignore this email.",
                                    "Reset password", link))
            db.commit()
    # Same response whether or not the account exists (no account enumeration).
    return render(request, "auth/forgot.html", sent=True)


def _reset_user(db: Session, token: str) -> Optional[User]:
    data = unsign(token, RESET_SALT, max_age=RESET_MAX_AGE)
    if not isinstance(data, dict):
        return None
    user = db.get(User, data.get("uid"))
    if not user or user.deleted_at or (user.password_hash or "none")[-12:] != data.get("ph"):
        return None
    return user


@router.get("/reset-password")
def reset_page(request: Request, token: str = "", db: Session = Depends(get_db)):
    if not _reset_user(db, token):
        return render(request, "auth/message.html", title="Link invalid or expired",
                      message="This password reset link is invalid, already used or expired.", status_code=400)
    return render(request, "auth/reset.html", token=token, error=None)


@router.post("/reset-password")
async def reset_password(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    token = form.get("token") or ""
    user = _reset_user(db, token)
    if not user:
        return render(request, "auth/message.html", title="Link invalid or expired",
                      message="This password reset link is invalid, already used or expired.", status_code=400)
    pw, pw2 = form.get("password") or "", form.get("password_confirm") or ""
    if len(pw) < 8 or pw != pw2:
        return render(request, "auth/reset.html", token=token, status_code=400,
                      error="Passwords must match and be at least 8 characters.")
    user.password_hash = hash_password(pw)
    user.session_epoch = int(user.session_epoch or 0) + 1  # sign out every existing session
    if not user.email_verified_at:
        user.email_verified_at = utcnow()  # they proved control of the inbox
    audit(db, "user.password_reset", actor_id=user.id, entity_type="user", entity_id=user.id,
          ip=ratelimit.client_ip(request))
    db.commit()
    flash(request, "Password updated. Please log in.")
    return redirect("/login")


# ================================================================ Google OAuth sign-in
@router.get("/auth/google/start")
def google_start(request: Request, next: str = ""):
    if not settings.google_client_id:
        flash(request, "Google sign-in is not configured.", "error")
        return redirect("/login")
    nonce = secrets.token_urlsafe(16)
    request.session["oauth_nonce"] = nonce
    state = sign({"n": nonce, "next": _safe_next(next)}, OAUTH_SALT)
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": f"{_base()}/auth/google/callback",
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
    }
    return redirect("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params), 302)


@router.get("/auth/google/callback")
def google_callback(request: Request, code: str = "", state: str = "", error: str = "", db: Session = Depends(get_db)):
    data = unsign(state, OAUTH_SALT, max_age=600)
    nonce = request.session.pop("oauth_nonce", None)
    if error or not code or not isinstance(data, dict) or not nonce or data.get("n") != nonce:
        flash(request, "Google sign-in failed or was cancelled.", "error")
        return redirect("/login")
    import httpx

    try:
        tok = httpx.post("https://oauth2.googleapis.com/token", timeout=15, data={
            "code": code, "client_id": settings.google_client_id, "client_secret": settings.google_client_secret,
            "redirect_uri": f"{_base()}/auth/google/callback", "grant_type": "authorization_code",
        })
        tok.raise_for_status()
        info = httpx.get("https://openidconnect.googleapis.com/v1/userinfo", timeout=15,
                         headers={"Authorization": f"Bearer {tok.json()['access_token']}"})
        info.raise_for_status()
        profile = info.json()
    except Exception:
        flash(request, "Could not complete Google sign-in. Please try again.", "error")
        return redirect("/login")
    sub, email = profile.get("sub"), (profile.get("email") or "").lower()
    if not sub or not email or not profile.get("email_verified"):
        flash(request, "Your Google account email is not verified.", "error")
        return redirect("/login")
    if not _login_method_allowed(db, email, "google"):
        flash(request, "Your organisation doesn't allow Google sign-in. Use single sign-on instead.", "error")
        return redirect("/login")
    user = db.scalar(select(User).where(User.google_sub == sub)) or db.scalar(
        select(User).where(func.lower(User.email) == email))
    if user and user.deleted_at:
        flash(request, "This account has been deleted.", "error")
        return redirect("/login")
    if not user:
        user, _ws = create_user_with_workspace(db, email, profile.get("name") or email.split("@")[0], verified=True)
        user.avatar_url = profile.get("picture")
        audit(db, "user.signup", actor_id=user.id, entity_type="user", entity_id=user.id, metadata={"via": "google"})
    user.google_sub = sub
    if not user.email_verified_at:
        user.email_verified_at = utcnow()
    audit(db, "user.login", actor_id=user.id, entity_type="user", entity_id=user.id, metadata={"via": "google"})
    db.commit()
    login_session(request, user)
    return redirect("/onboarding" if not user.onboarding_completed else data.get("next") or "/app")


# ================================================================ invitations
def _invite(db: Session, token: str) -> Optional[WorkspaceMember]:
    if not token:
        return None
    return db.scalar(select(WorkspaceMember).where(WorkspaceMember.invite_token == token,
                                                   WorkspaceMember.status == "invited"))


@router.get("/invite/{token}")
def invite_page(request: Request, token: str, db: Session = Depends(get_db)):
    m = _invite(db, token)
    if not m:
        return render(request, "auth/message.html", title="Invitation not found",
                      message="This invitation is invalid, revoked or has already been accepted.", status_code=404)
    ws = db.get(Workspace, m.workspace_id)
    email = (m.invite_email or "").lower()
    current = db.get(User, request.session.get("user_id")) if request.session.get("user_id") else None
    existing = db.scalar(select(User).where(func.lower(User.email) == email, User.deleted_at.is_(None))) if email else None
    return render(request, "auth/invite.html", member=m, ws=ws, email=email, current=current, existing=existing,
                  token=token, error=None)


@router.post("/invite/{token}")
async def invite_accept(request: Request, token: str, db: Session = Depends(get_db)):
    m = _invite(db, token)
    if not m:
        return render(request, "auth/message.html", title="Invitation not found",
                      message="This invitation is invalid, revoked or has already been accepted.", status_code=404)
    form = await request.form()
    email = (m.invite_email or "").lower()
    uid = request.session.get("user_id")
    current = db.get(User, uid) if uid else None
    if current and not current.deleted_at and (not email or current.email.lower() == email):
        user = current
    else:
        existing = db.scalar(select(User).where(func.lower(User.email) == email)) if email else None
        if existing:
            flash(request, "Log in with the invited email to accept the invitation.", "info")
            return redirect(f"/login?next=/invite/{token}")
        name = (form.get("name") or "").strip()
        password = form.get("password") or ""
        tz = form.get("timezone") or "UTC"
        if not name or len(password) < 8 or not email:
            ws = db.get(Workspace, m.workspace_id)
            return render(request, "auth/invite.html", member=m, ws=ws, email=email, current=None, existing=None,
                          token=token, error="Enter your name and a password of at least 8 characters.",
                          status_code=400)
        user = User(email=email, name=name, password_hash=hash_password(password),
                    default_timezone=tz if valid_tz(tz) else "UTC", slug=unique_user_slug(db, name),
                    email_verified_at=utcnow(),  # the invite link proves inbox ownership
                    notification_prefs_json={"new_booking_email": True, "cancellation_email": True})
        db.add(user)
        db.flush()
        create_default_schedule(db, user, m.workspace_id, user.default_timezone)
        ensure_host_lock(db, user.id)
    dup = db.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == m.workspace_id,
                                                  WorkspaceMember.user_id == user.id))
    if dup and dup.id != m.id:
        dup.status = "active"
        db.delete(m)
    else:
        m.user_id = user.id
        m.status = "active"
        m.invite_token = None
        if not db.scalar(select(AvailabilitySchedule.id).where(AvailabilitySchedule.user_id == user.id,
                                                              AvailabilitySchedule.workspace_id == m.workspace_id)):
            create_default_schedule(db, user, m.workspace_id, user.default_timezone)
    audit(db, "member.invite_accepted", workspace_id=m.workspace_id, actor_id=user.id, entity_type="workspace_member",
          entity_id=m.id)
    db.commit()
    login_session(request, user, m.workspace_id)
    flash(request, "You've joined the workspace.")
    return redirect("/onboarding" if not user.onboarding_completed else "/app")


# ================================================================ onboarding wizard
STEPS = ["profile", "calendar", "availability", "event", "done"]


def validate_user_slug(db: Session, slug: str, user_id: str) -> Optional[str]:
    if not slug or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,58}[a-z0-9])?", slug):
        return "Use 1–60 lowercase letters, numbers or hyphens."
    if slug in RESERVED_SLUGS:
        return f"'{slug}' is reserved. Please choose another link."
    if db.scalar(select(User.id).where(User.slug == slug, User.id != user_id)):
        return f"'{slug}' is already taken. Please choose another link."
    return None


def _default_schedule(db: Session, ctx: Ctx) -> AvailabilitySchedule:
    sch = db.scalar(select(AvailabilitySchedule).where(
        AvailabilitySchedule.user_id == ctx.user.id, AvailabilitySchedule.workspace_id == ctx.workspace.id
    ).order_by(AvailabilitySchedule.is_default.desc(), AvailabilitySchedule.created_at))
    if not sch:
        sch = create_default_schedule(db, ctx.user, ctx.workspace.id)
    return sch


@router.get("/onboarding")
def onboarding(request: Request, step: str = "profile", ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    step = step if step in STEPS else "profile"
    sch = _default_schedule(db, ctx)
    rules = db.scalars(select(AvailabilityRule).where(AvailabilityRule.schedule_id == sch.id)).all()
    days = sorted({r.weekday for r in rules})
    first = rules[0] if rules else None
    ev = db.scalar(select(EventType).where(EventType.owner_user_id == ctx.user.id, EventType.deleted_at.is_(None),
                                           EventType.is_one_off.is_(False), EventType.single_use.is_(False))
                   .order_by(EventType.created_at))
    from app.models import CalendarConnection

    conns = db.scalars(select(CalendarConnection).where(CalendarConnection.user_id == ctx.user.id)).all()
    return render(request, "auth/onboarding.html", ctx, step=step, steps=STEPS, timezones=all_timezones(),
                  days=days or [0, 1, 2, 3, 4], start=first.start_local if first else "09:00",
                  end=first.end_local if first else "17:00", event=ev,
                  event_url=event_public_url(db, ev) if ev else None, connections=conns, error=None,
                  profile_url=f"{_base()}/{ctx.user.slug}")


@router.post("/onboarding")
async def onboarding_save(request: Request, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    form = await request.form()
    step = form.get("step") or "profile"
    user = db.get(User, ctx.user.id)
    if step == "profile":
        slug = slugify(form.get("slug") or "") if form.get("slug") else ""
        err = validate_user_slug(db, slug, user.id)
        tz = form.get("timezone") or user.default_timezone
        if err or not valid_tz(tz):
            flash(request, err or "Unknown timezone.", "error")
            return redirect("/onboarding?step=profile", 303)
        user.slug, user.default_timezone = slug, tz
        user.headline = (form.get("headline") or "").strip()[:300] or None
        if form.get("name"):
            user.name = form.get("name").strip()[:200]
        sch = _default_schedule(db, ctx)
        sch.timezone = tz
        db.commit()
        return redirect("/onboarding?step=calendar")
    if step == "availability":
        days = sorted({int(d) for d in form.getlist("days") if str(d).isdigit() and 0 <= int(d) <= 6})
        start, end = form.get("start") or "09:00", form.get("end") or "17:00"
        from app.services.hostutil import parse_ranges

        ranges, err = parse_ranges([(start, end)])
        if err or not days:
            flash(request, err or "Pick at least one day.", "error")
            return redirect("/onboarding?step=availability")
        sch = _default_schedule(db, ctx)
        for r in db.scalars(select(AvailabilityRule).where(AvailabilityRule.schedule_id == sch.id)).all():
            db.delete(r)
        for d in days:
            db.add(AvailabilityRule(schedule_id=sch.id, weekday=d, start_local=ranges[0]["start"],
                                    end_local=ranges[0]["end"]))
        db.commit()
        return redirect("/onboarding?step=event")
    if step == "event":
        from app.routers.event_types import create_event_type_basic

        name = (form.get("name") or "").strip() or "30 minute meeting"
        try:
            duration = max(5, min(720, int(form.get("duration") or 30)))
        except ValueError:
            duration = 30
        loc = form.get("location") or "google_meet"
        ev, err = create_event_type_basic(db, ctx, name=name, duration=duration, kind="one_on_one",
                                          locations=[{"type": loc, "value": (form.get("location_value") or "").strip()}])
        if err:
            flash(request, err, "error")
            return redirect("/onboarding?step=event")
        db.commit()
        return redirect("/onboarding?step=done")
    if step == "done":
        user.onboarding_completed = True
        db.commit()
        return redirect("/app")
    return redirect("/onboarding")
