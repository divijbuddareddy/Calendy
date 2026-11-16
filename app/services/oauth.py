"""OAuth 2.0 authorization-code flow for third-party apps (PRD 15.3, P3) - OAuth 2.1 style:

  * Apps are registered per developer workspace (/app/integrations/oauth-apps). client_id is
    public; the client secret is shown once and stored as a keyed hash.
  * PKCE (S256) is REQUIRED on every authorization request.
  * /oauth/authorize shows a consent page; approval issues a single-use code (10 min, hashed).
  * POST /oauth/token: grant_type=authorization_code | refresh_token. Client auth by HTTP Basic
    or client_secret in the form body. Access tokens (1 h) and refresh tokens (60 d) are hashed;
    refresh rotates the pair. Re-use of a spent code or rotated refresh token revokes the
    whole grant family (theft detection).
  * POST /oauth/revoke (RFC 7009) and user-side revocation of authorized apps.
  * Access tokens authenticate /api/v1 exactly like API keys, limited to the granted scopes and
    to the workspace the user chose at consent time.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import secrets
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode, urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import User, Workspace, WorkspaceMember, utcnow
from app.models_commerce import OAuthApp, OAuthAuthorizationCode, OAuthToken
from app.security import constant_time_eq, hash_secret, new_id

SCOPES = {"read": "Read your event types, availability, bookings, contacts and analytics",
          "write": "Create, cancel and reschedule bookings, edit event types and manage webhooks"}
ACCESS_PREFIX = "aal_at_"
REFRESH_PREFIX = "aal_rt_"
SECRET_PREFIX = "aal_cs_"
CODE_TTL = dt.timedelta(minutes=10)
ACCESS_TTL = dt.timedelta(hours=1)
REFRESH_TTL = dt.timedelta(days=60)


class OAuthError(Exception):
    """RFC 6749 error. `redirectable` errors are sent back to the client's redirect_uri."""

    def __init__(self, error: str, description: str = "", status_code: int = 400, redirectable: bool = False):
        self.error, self.description, self.status_code, self.redirectable = error, description, status_code, redirectable
        super().__init__(f"{error}: {description}")

    def body(self) -> dict:
        return {"error": self.error, "error_description": self.description}


def _h(kind: str, raw: str) -> str:
    return hash_secret(f"oauth:{kind}:{raw}")


def parse_scopes(scope: Optional[str]) -> list[str]:
    return sorted({s for s in (scope or "").replace(",", " ").split() if s})


# ------------------------------------------------------------------ app registration
def validate_redirect_uri(uri: str) -> str:
    uri = (uri or "").strip()
    p = urlparse(uri)
    if not p.scheme or not p.netloc or p.fragment:
        raise OAuthError("invalid_request", f"Invalid redirect URI: {uri!r}")
    local = p.hostname in ("localhost", "127.0.0.1", "::1")
    if p.scheme != "https" and not (p.scheme == "http" and (local or settings.env != "prod")):
        raise OAuthError("invalid_request", "Redirect URIs must use HTTPS (http is allowed for localhost only)")
    return uri


def register_app(db: Session, workspace_id: str, user_id: str, name: str, redirect_uris: list[str],
                 scopes: list[str], homepage_url: Optional[str] = None) -> tuple[OAuthApp, str]:
    uris = [validate_redirect_uri(u) for u in redirect_uris if u.strip()]
    if not uris:
        raise OAuthError("invalid_request", "At least one redirect URI is required")
    scopes = [s for s in scopes if s in SCOPES] or ["read"]
    secret = SECRET_PREFIX + secrets.token_urlsafe(32)
    app = OAuthApp(workspace_id=workspace_id, created_by=user_id, name=(name or "OAuth app")[:200],
                   homepage_url=(homepage_url or None), client_id="aal_ci_" + secrets.token_urlsafe(18),
                   client_secret_hash=_h("secret", secret), redirect_uris=uris, scopes=" ".join(sorted(scopes)))
    db.add(app)
    db.flush()
    return app, secret


def rotate_secret(db: Session, app: OAuthApp) -> str:
    secret = SECRET_PREFIX + secrets.token_urlsafe(32)
    app.client_secret_hash = _h("secret", secret)
    db.flush()
    return secret


def revoke_app(db: Session, app: OAuthApp) -> None:
    now = utcnow()
    app.revoked_at = app.revoked_at or now
    for t in db.scalars(select(OAuthToken).where(OAuthToken.app_id == app.id, OAuthToken.revoked_at.is_(None))).all():
        t.revoked_at = now
    db.flush()


def get_app(db: Session, client_id: Optional[str]) -> Optional[OAuthApp]:
    if not client_id:
        return None
    app = db.scalar(select(OAuthApp).where(OAuthApp.client_id == client_id))
    return app if app and not app.revoked_at else None


def authenticate_client(db: Session, client_id: Optional[str], client_secret: Optional[str]) -> OAuthApp:
    app = get_app(db, client_id)
    if not app or not client_secret or not constant_time_eq(app.client_secret_hash, _h("secret", client_secret)):
        raise OAuthError("invalid_client", "Client authentication failed", 401)
    return app


def client_credentials_from(authorization: Optional[str], form: dict) -> tuple[Optional[str], Optional[str]]:
    if authorization and authorization.lower().startswith("basic "):
        try:
            cid, _, sec = base64.b64decode(authorization[6:].strip()).decode().partition(":")
            from urllib.parse import unquote

            return unquote(cid), unquote(sec)
        except Exception:  # noqa: BLE001
            raise OAuthError("invalid_client", "Malformed Basic credentials", 401)
    return form.get("client_id"), form.get("client_secret")


# ------------------------------------------------------------------ authorize
@dataclass
class AuthorizeRequest:
    app: OAuthApp
    redirect_uri: str
    scopes: list[str]
    state: str
    code_challenge: str
    code_challenge_method: str


def validate_authorize(db: Session, params: dict) -> AuthorizeRequest:
    app = get_app(db, params.get("client_id"))
    if not app:
        raise OAuthError("invalid_client", "Unknown or revoked client_id")
    redirect_uri = params.get("redirect_uri") or (app.redirect_uris[0] if len(app.redirect_uris or []) == 1 else "")
    if redirect_uri not in (app.redirect_uris or []):
        raise OAuthError("invalid_request", "redirect_uri does not match a registered redirect URI")
    # From here on errors can be returned to the (verified) redirect URI.
    state = params.get("state") or ""
    if params.get("response_type") != "code":
        raise OAuthError("unsupported_response_type", "Only response_type=code is supported", redirectable=True)
    challenge = params.get("code_challenge") or ""
    method = params.get("code_challenge_method") or "plain"
    if not challenge:
        raise OAuthError("invalid_request", "PKCE code_challenge is required", redirectable=True)
    if method != "S256":
        raise OAuthError("invalid_request", "code_challenge_method must be S256", redirectable=True)
    if not (43 <= len(challenge) <= 128):
        raise OAuthError("invalid_request", "Invalid code_challenge", redirectable=True)
    requested = parse_scopes(params.get("scope")) or parse_scopes(app.scopes)
    allowed = set(parse_scopes(app.scopes))
    if any(s not in SCOPES for s in requested) or not set(requested) <= allowed:
        raise OAuthError("invalid_scope", f"Requested scope exceeds what this app may request ({app.scopes})",
                         redirectable=True)
    return AuthorizeRequest(app, redirect_uri, requested, state, challenge, method)


def redirect_with(uri: str, params: dict) -> str:
    sep = "&" if "?" in uri else "?"
    return uri + sep + urlencode({k: v for k, v in params.items() if v not in (None, "")})


def issue_code(db: Session, req: AuthorizeRequest, user: User, workspace_id: str) -> str:
    raw = secrets.token_urlsafe(32)
    db.add(OAuthAuthorizationCode(
        code_hash=_h("code", raw), app_id=req.app.id, user_id=user.id, workspace_id=workspace_id,
        redirect_uri=req.redirect_uri, scopes=" ".join(req.scopes), code_challenge=req.code_challenge,
        code_challenge_method=req.code_challenge_method, expires_at=utcnow() + CODE_TTL))
    db.flush()
    return raw


def pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


# ------------------------------------------------------------------ tokens
def _issue_tokens(db: Session, app: OAuthApp, user_id: str, workspace_id: str, scopes: str,
                  family_id: Optional[str] = None) -> dict:
    access = ACCESS_PREFIX + secrets.token_urlsafe(32)
    refresh = REFRESH_PREFIX + secrets.token_urlsafe(32)
    now = utcnow()
    tok = OAuthToken(app_id=app.id, user_id=user_id, workspace_id=workspace_id, scopes=scopes,
                     access_token_hash=_h("access", access), refresh_token_hash=_h("refresh", refresh),
                     prefix=access[:14], access_expires_at=now + ACCESS_TTL, refresh_expires_at=now + REFRESH_TTL,
                     family_id=family_id or new_id("fam"))
    db.add(tok)
    db.flush()
    return {"access_token": access, "token_type": "Bearer", "expires_in": int(ACCESS_TTL.total_seconds()),
            "refresh_token": refresh, "scope": scopes, "workspace_id": workspace_id}


def _revoke_family(db: Session, family_id: Optional[str]) -> None:
    if not family_id:
        return
    now = utcnow()
    for t in db.scalars(select(OAuthToken).where(OAuthToken.family_id == family_id)).all():
        t.revoked_at = t.revoked_at or now
    db.flush()


def exchange_code(db: Session, app: OAuthApp, code: Optional[str], redirect_uri: Optional[str],
                  code_verifier: Optional[str]) -> dict:
    if not code:
        raise OAuthError("invalid_request", "code is required")
    row = db.scalar(select(OAuthAuthorizationCode).where(OAuthAuthorizationCode.code_hash == _h("code", code)))
    if not row or row.app_id != app.id:
        raise OAuthError("invalid_grant", "Invalid authorization code")
    if row.used_at:
        _revoke_family(db, row.id)  # code replay: revoke everything issued from it
        db.commit()
        raise OAuthError("invalid_grant", "Authorization code already used")
    if row.expires_at < utcnow():
        raise OAuthError("invalid_grant", "Authorization code expired")
    if (redirect_uri or row.redirect_uri) != row.redirect_uri:
        raise OAuthError("invalid_grant", "redirect_uri mismatch")
    if not code_verifier or not (43 <= len(code_verifier) <= 128) or not constant_time_eq(
            pkce_challenge(code_verifier), row.code_challenge):
        raise OAuthError("invalid_grant", "PKCE verification failed")
    row.used_at = utcnow()
    return _issue_tokens(db, app, row.user_id, row.workspace_id, row.scopes, family_id=row.id)


def refresh_grant(db: Session, app: OAuthApp, refresh_token: Optional[str], scope: Optional[str] = None) -> dict:
    if not refresh_token:
        raise OAuthError("invalid_request", "refresh_token is required")
    tok = db.scalar(select(OAuthToken).where(OAuthToken.refresh_token_hash == _h("refresh", refresh_token)))
    if not tok or tok.app_id != app.id:
        raise OAuthError("invalid_grant", "Invalid refresh token")
    if tok.revoked_at:
        _revoke_family(db, tok.family_id)  # rotated token re-used: assume theft
        db.commit()
        raise OAuthError("invalid_grant", "Refresh token has been revoked")
    if tok.refresh_expires_at and tok.refresh_expires_at < utcnow():
        raise OAuthError("invalid_grant", "Refresh token expired")
    scopes = tok.scopes
    if scope:
        req = parse_scopes(scope)
        if not set(req) <= set(parse_scopes(tok.scopes)):
            raise OAuthError("invalid_scope", "Cannot widen scopes on refresh")
        scopes = " ".join(req)
    tok.revoked_at = utcnow()
    return _issue_tokens(db, app, tok.user_id, tok.workspace_id, scopes, family_id=tok.family_id)


def revoke_token(db: Session, app: OAuthApp, token: Optional[str]) -> None:
    """RFC 7009: revoking either token of a grant revokes the grant family. Unknown tokens are OK."""
    if not token:
        return
    tok = db.scalar(select(OAuthToken).where(
        (OAuthToken.access_token_hash == _h("access", token)) | (OAuthToken.refresh_token_hash == _h("refresh", token))))
    if tok and tok.app_id == app.id:
        _revoke_family(db, tok.family_id)


def revoke_grants_for_user(db: Session, user_id: str, app_id: str) -> int:
    now, n = utcnow(), 0
    for t in db.scalars(select(OAuthToken).where(OAuthToken.user_id == user_id, OAuthToken.app_id == app_id,
                                                 OAuthToken.revoked_at.is_(None))).all():
        t.revoked_at, n = now, n + 1
    db.flush()
    return n


def authorized_apps(db: Session, user_id: str) -> list[dict]:
    rows = db.scalars(select(OAuthToken).where(OAuthToken.user_id == user_id, OAuthToken.revoked_at.is_(None))).all()
    out: dict[str, dict] = {}
    now = utcnow()
    for t in rows:
        if t.refresh_expires_at and t.refresh_expires_at < now:
            continue
        app = db.get(OAuthApp, t.app_id)
        if not app or app.revoked_at:
            continue
        d = out.setdefault(app.id, {"app": app, "scopes": set(), "workspace_id": t.workspace_id,
                                    "last_used_at": None, "created_at": t.created_at})
        d["scopes"] |= set(parse_scopes(t.scopes))
        if t.last_used_at and (not d["last_used_at"] or t.last_used_at > d["last_used_at"]):
            d["last_used_at"] = t.last_used_at
    return list(out.values())


# ------------------------------------------------------------------ resource-server side
@dataclass
class TokenKeyView:
    """Duck-types models.ApiKey for ApiPrincipal (id/name/prefix/scopes)."""

    id: str
    name: str
    prefix: str
    scopes: str


def authenticate_access_token(db: Session, raw: str):
    """Returns (TokenKeyView, Workspace, User) or raises APIError 401."""
    from app.errors import UNAUTHORIZED, APIError

    tok = db.scalar(select(OAuthToken).where(OAuthToken.access_token_hash == _h("access", raw)))
    if not tok or tok.revoked_at:
        raise APIError(UNAUTHORIZED, "Invalid or revoked access token", 401)
    now = utcnow()
    if tok.access_expires_at < now:
        raise APIError(UNAUTHORIZED, "Access token expired; use your refresh token", 401)
    app = db.get(OAuthApp, tok.app_id)
    ws = db.get(Workspace, tok.workspace_id)
    user = db.get(User, tok.user_id)
    if not app or app.revoked_at or not ws or ws.status != "active" or not user or user.deleted_at:
        raise APIError(UNAUTHORIZED, "Access token is no longer valid", 401)
    member = db.scalar(select(WorkspaceMember).where(
        WorkspaceMember.workspace_id == ws.id, WorkspaceMember.user_id == user.id, WorkspaceMember.status == "active"))
    if not member:
        raise APIError(UNAUTHORIZED, "The authorizing user is no longer a member of this workspace", 401)
    if not tok.last_used_at or (now - tok.last_used_at).total_seconds() > 60:
        tok.last_used_at = now
        db.commit()
    return TokenKeyView(id=tok.id, name=f"OAuth: {app.name}", prefix=tok.prefix, scopes=tok.scopes), ws, user
