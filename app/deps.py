"""Shared request dependencies: DB, auth/session, tenant context, roles, templates, CSRF.

Auth model
----------
Session cookie (Starlette SessionMiddleware) holds `user_id`, `workspace_id`, `csrf`.
Every authenticated page/endpoint resolves a `Ctx` (user + workspace + membership role) and
MUST scope queries by `ctx.workspace.id` (tenant isolation, PRD 17.1).

Roles (workspace): owner > admin > member > viewer.   `require_role("admin")` etc.
"""
import datetime as dt
import secrets
from dataclasses import dataclass
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.entitlements import has_feature, plan_of
from app.models import User, Workspace, WorkspaceMember

ROLE_ORDER = {"viewer": 0, "member": 1, "admin": 2, "owner": 3}


class LoginRequired(Exception):
    """Raised for HTML routes when not logged in; handled by redirect to /login."""


class Forbidden(Exception):
    pass


@dataclass
class Ctx:
    user: User
    workspace: Workspace
    member: WorkspaceMember

    @property
    def role(self) -> str:
        return self.member.role

    def can(self, role: str) -> bool:
        return ROLE_ORDER.get(self.role, -1) >= ROLE_ORDER[role]

    def feature(self, name: str) -> bool:
        return has_feature(self.workspace, name)


# ---------------------------------------------------------------- session helpers
def _now_ts() -> int:
    return int(dt.datetime.now(dt.timezone.utc).timestamp())


def login_session(request: Request, user: User, workspace_id: Optional[str] = None):
    request.session.clear()
    request.session["user_id"] = user.id
    if workspace_id:
        request.session["workspace_id"] = workspace_id
    request.session["csrf"] = secrets.token_urlsafe(24)
    # Session versioning: a password change / "sign out other sessions" bumps user.session_epoch,
    # which invalidates every session cookie carrying an older epoch.
    request.session["epoch"] = int(user.session_epoch or 0)
    request.session["iat"] = request.session["seen"] = _now_ts()


def refresh_session_epoch(request: Request, user: User):
    """Keep the *current* session valid after bumping the user's epoch (other sessions are dropped)."""
    request.session["epoch"] = int(user.session_epoch or 0)
    request.session["iat"] = request.session["seen"] = _now_ts()


def bump_session_epoch(user: User) -> int:
    user.session_epoch = int(user.session_epoch or 0) + 1
    return user.session_epoch


def session_valid(request: Request, user: User) -> bool:
    """Epoch match + idle timeout + absolute lifetime (PRD 17.1 secure session management)."""
    sess = request.session
    now = _now_ts()
    if int(sess.get("epoch", 0) or 0) != int(user.session_epoch or 0):
        return False
    iat, seen = sess.get("iat"), sess.get("seen")
    if iat is None or seen is None:  # legacy cookie from before versioning: start the clocks now
        sess["iat"] = sess["seen"] = now
        return True
    if now - int(iat) > settings.session_max_age_minutes * 60:
        return False
    if now - int(seen) > settings.session_idle_minutes * 60:
        return False
    if now - int(seen) > 60:  # avoid re-issuing the cookie on every request
        sess["seen"] = now
    return True


def logout_session(request: Request):
    request.session.clear()


def get_csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(24)
        request.session["csrf"] = tok
    return tok


def _resolve_ctx(request: Request, db: Session) -> Optional[Ctx]:
    uid = request.session.get("user_id")
    if not uid:
        return None
    user = db.get(User, uid)
    if not user or user.deleted_at:
        return None
    if not session_valid(request, user):
        request.session.clear()
        return None
    wsid = request.session.get("workspace_id")
    q = select(WorkspaceMember).where(WorkspaceMember.user_id == user.id, WorkspaceMember.status == "active")
    if wsid:
        member = db.scalar(q.where(WorkspaceMember.workspace_id == wsid))
    else:
        member = None
    if not member:
        member = db.scalar(q.order_by(WorkspaceMember.created_at))
        if not member:
            return None
        request.session["workspace_id"] = member.workspace_id
    ws = db.get(Workspace, member.workspace_id)
    try:  # workspace custom security policy: absolute session max age (app.services.sso)
        from app.services.sso import session_policy_ok

        if not session_policy_ok(request.session, ws):
            request.session.clear()
            return None
    except ImportError:  # pragma: no cover
        pass
    return Ctx(user=user, workspace=ws, member=member)


def optional_ctx(request: Request, db: Session = Depends(get_db)) -> Optional[Ctx]:
    return _resolve_ctx(request, db)


def get_ctx(request: Request, db: Session = Depends(get_db)) -> Ctx:
    """Authenticated HTML/JSON app routes. Unauthenticated -> redirect to /login."""
    ctx = _resolve_ctx(request, db)
    if not ctx:
        raise LoginRequired()
    request.state.ctx = ctx
    return ctx


def require_role(role: str):
    def dep(ctx: Ctx = Depends(get_ctx)) -> Ctx:
        if not ctx.can(role):
            raise Forbidden(f"Requires {role} role")
        return ctx

    return dep


def require_feature(feature: str):
    def dep(ctx: Ctx = Depends(get_ctx)) -> Ctx:
        if not ctx.feature(feature):
            raise Forbidden(f"Your plan does not include '{feature}'. Upgrade in Settings > Billing.")
        return ctx

    return dep


def redirect(url: str, status_code: int = 303) -> RedirectResponse:
    return RedirectResponse(url, status_code=status_code)


def flash(request: Request, message: str, kind: str = "success"):
    request.session.setdefault("_flashes", []).append({"message": message, "kind": kind})


def pop_flashes(request: Request) -> list:
    return request.session.pop("_flashes", []) if "session" in request.scope else []


# ---------------------------------------------------------------- templates
templates = Jinja2Templates(directory="app/templates")


def _fmt_dt(value: Optional[dt.datetime], tz: str = "UTC", fmt: str = "%a %d %b %Y, %H:%M") -> str:
    """Format a naive-UTC datetime in an IANA timezone with abbreviation (e.g. 'Mon 18 Nov 2026, 14:00 GMT')."""
    if not value:
        return ""
    if isinstance(value, str):  # ISO-8601 string (e.g. one-off slot starts stored as UTC ISO)
        try:
            value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if isinstance(value, dt.datetime) and value.tzinfo is not None:
        value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    try:
        z = ZoneInfo(tz or "UTC")
    except Exception:
        z = ZoneInfo("UTC")
    local = value.replace(tzinfo=dt.timezone.utc).astimezone(z)
    return local.strftime(fmt) + " " + (local.tzname() or "")


def _money(amount_minor: Optional[int], currency: Optional[str]) -> str:
    if amount_minor is None:
        return ""
    sym = {"USD": "$", "GBP": "£", "EUR": "€", "INR": "₹"}.get((currency or "").upper(), (currency or "") + " ")
    return f"{sym}{amount_minor / 100:,.2f}"


def _csrf_input(request: Request) -> Markup:
    return Markup(f'<input type="hidden" name="csrf_token" value="{get_csrf_token(request)}">')


templates.env.globals.update(
    app_name=settings.app_name,
    base_url=settings.base_url,
    csrf_token=get_csrf_token,
    csrf_input=_csrf_input,
    get_flashes=pop_flashes,
    plan_of=plan_of,
    has_feature=has_feature,
    now_utc=lambda: dt.datetime.now(dt.timezone.utc).replace(tzinfo=None),
)
templates.env.globals["legal_versions"] = {
    "privacy": settings.privacy_version, "terms": settings.terms_version, "retention": settings.retention_version,
}
templates.env.globals["privacy_version"] = settings.privacy_version
from app import i18n as _i18n  # noqa: E402

_i18n.register(templates.env)
templates.env.filters["dt"] = _fmt_dt
templates.env.filters["money"] = _money


def render(request: Request, template: str, ctx: Optional[Ctx] = None, status_code: int = 200, **context):
    """Render a template; `ctx` (if given) is exposed as `ctx`, plus `user`/`workspace` shortcuts."""
    if ctx is None:
        ctx = getattr(request.state, "ctx", None)
    data = {"ctx": ctx, "user": ctx.user if ctx else None, "workspace": ctx.workspace if ctx else None}
    data.update(context)
    return templates.TemplateResponse(request, template, data, status_code=status_code)
