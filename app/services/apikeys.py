"""Workspace-scoped API keys (PRD 15.3): shown once, stored as a keyed hash, revocable.
`authenticate` also accepts OAuth 2.0 access tokens (aal_at_...) issued by services.oauth."""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import FORBIDDEN, UNAUTHORIZED, APIError
from app.models import ApiKey, User, Workspace, WorkspaceMember, utcnow
from app.security import constant_time_eq, hash_secret

KEY_PREFIX = "aal_sk_"
VALID_SCOPES = ("read", "write")


@dataclass
class ApiPrincipal:
    key: ApiKey  # or oauth.TokenKeyView for OAuth access tokens (same id/name/prefix/scopes surface)
    workspace: Workspace
    user: User
    kind: str = "api_key"  # api_key | oauth

    @property
    def scopes(self) -> set[str]:
        return set((self.key.scopes or "").split())

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            what = "Access token" if self.kind == "oauth" else "API key"
            raise APIError(FORBIDDEN, f"{what} lacks the '{scope}' scope", 403)


def create_key(db: Session, workspace_id: str, user_id: str, name: str, scopes: list[str]) -> tuple[ApiKey, str]:
    scopes = [s for s in scopes if s in VALID_SCOPES] or ["read"]
    raw = KEY_PREFIX + secrets.token_urlsafe(32)
    row = ApiKey(workspace_id=workspace_id, user_id=user_id, name=(name or "API key")[:200], prefix=raw[:14],
                 key_hash=hash_secret(raw), scopes=" ".join(scopes))
    db.add(row)
    db.flush()
    return row, raw


def revoke_key(db: Session, key: ApiKey) -> None:
    if not key.revoked_at:
        key.revoked_at = utcnow()
    db.flush()


def authenticate(db: Session, authorization: Optional[str]) -> ApiPrincipal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise APIError(UNAUTHORIZED, "Missing API key. Use 'Authorization: Bearer <key>'.", 401)
    raw = authorization[7:].strip()
    from app.services import oauth

    if raw.startswith(oauth.ACCESS_PREFIX):
        view, ws, user = oauth.authenticate_access_token(db, raw)
        return ApiPrincipal(key=view, workspace=ws, user=user, kind="oauth")
    if not raw.startswith(KEY_PREFIX):
        raise APIError(UNAUTHORIZED, "Invalid API key", 401)
    h = hash_secret(raw)
    key = db.scalar(select(ApiKey).where(ApiKey.key_hash == h))
    if not key or not constant_time_eq(key.key_hash, h):
        raise APIError(UNAUTHORIZED, "Invalid API key", 401)
    if key.revoked_at:
        raise APIError(UNAUTHORIZED, "API key has been revoked", 401)
    ws = db.get(Workspace, key.workspace_id)
    user = db.get(User, key.user_id)
    if not ws or ws.status != "active" or not user or user.deleted_at:
        raise APIError(UNAUTHORIZED, "API key is no longer valid", 401)
    member = db.scalar(select(WorkspaceMember).where(
        WorkspaceMember.workspace_id == ws.id, WorkspaceMember.user_id == user.id, WorkspaceMember.status == "active"))
    if not member:
        raise APIError(UNAUTHORIZED, "API key owner is no longer a member of this workspace", 401)
    now = utcnow()
    if not key.last_used_at or (now - key.last_used_at).total_seconds() > 60:
        key.last_used_at = now
        db.commit()
    return ApiPrincipal(key=key, workspace=ws, user=user)
