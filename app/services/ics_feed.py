"""Private ICS subscription feed tokens (PRD 9.2 "rotate/revoke securely").

The raw token is only shown once (right after it is generated); only a keyed hash is stored.
Regenerating replaces the hash, so the previous URL stops working immediately; revoking
disables the feed until a new URL is generated.
"""
from __future__ import annotations

import secrets
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import User, utcnow
from app.models_commerce import IcsFeedToken
from app.security import constant_time_eq, hash_secret


def _hash(raw: str) -> str:
    return hash_secret("ics-feed:" + raw)


def feed_url(user_id: str, raw: str, base_url: Optional[str] = None) -> str:
    return f"{(base_url or settings.base_url).rstrip('/')}/api/v1/calendar-feed/{user_id}/{raw}.ics"


def current(db: Session, user_id: str) -> Optional[IcsFeedToken]:
    return db.scalar(select(IcsFeedToken).where(IcsFeedToken.user_id == user_id))


def issue(db: Session, user_id: str) -> str:
    """Create or rotate the user's feed token. Returns the raw token (show once)."""
    raw = secrets.token_urlsafe(24)
    row = current(db, user_id)
    if not row:
        row = IcsFeedToken(user_id=user_id, token_hash=_hash(raw))
        db.add(row)
    row.token_hash, row.hint, row.revoked_at, row.last_used_at = _hash(raw), raw[-4:], None, None
    db.flush()
    return raw


def revoke(db: Session, user_id: str) -> bool:
    row = current(db, user_id)
    if not row or row.revoked_at:
        return False
    row.revoked_at = utcnow()
    # replace the hash so even a race with the old value cannot match
    row.token_hash = _hash("revoked:" + secrets.token_urlsafe(16))
    db.flush()
    return True


def verify(db: Session, user_id: str, raw: str) -> Optional[User]:
    row = current(db, user_id)
    if not row or row.revoked_at or not raw:
        return None
    if not constant_time_eq(row.token_hash, _hash(raw)):
        return None
    user = db.get(User, user_id)
    if not user or user.deleted_at:
        return None
    now = utcnow()
    if not row.last_used_at or (now - row.last_used_at).total_seconds() > 300:
        row.last_used_at = now
        db.commit()
    return user
