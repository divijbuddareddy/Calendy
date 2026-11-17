"""Anthropic (Claude) API key management.

Each workspace can store its own Anthropic API key from Settings -> AI. The key is encrypted at rest
(`security.encrypt`) inside `Workspace.settings_json["anthropic_api_key_enc"]`. When a workspace has no
key, the server-wide `ANTHROPIC_API_KEY` from the environment is used as a fallback. With neither, the AI
features fall back to the built-in deterministic parsers, so everything still works offline.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.models import Workspace
from app.security import decrypt, encrypt

log = logging.getLogger(__name__)

SETTINGS_FIELD = "anthropic_api_key_enc"
API_BASE = "https://api.anthropic.com/v1"
API_VERSION = "2023-06-01"
MODEL = "claude-sonnet-5-5"


def workspace_key(ws: Optional[Workspace]) -> Optional[str]:
    """The key saved on this workspace (decrypted), or None."""
    if not ws:
        return None
    enc = (ws.settings_json or {}).get(SETTINGS_FIELD)
    return decrypt(enc) if enc else None


def effective_key(db: Optional[Session], workspace_id: Optional[str]) -> str:
    """Key to use for a workspace: its own key first, then the server-wide env key, else ''."""
    if db is not None and workspace_id:
        key = workspace_key(db.get(Workspace, workspace_id))
        if key:
            return key
    return settings.anthropic_api_key or ""


def key_source(ws: Optional[Workspace]) -> str:
    """'workspace' | 'server' | 'none' - for display."""
    if workspace_key(ws):
        return "workspace"
    return "server" if settings.anthropic_api_key else "none"


def mask(key: Optional[str]) -> str:
    if not key:
        return ""
    return f"{key[:7]}…{key[-4:]}" if len(key) > 14 else "…" + key[-4:]


def set_workspace_key(ws: Workspace, key: Optional[str]) -> None:
    data = dict(ws.settings_json or {})
    if key:
        data[SETTINGS_FIELD] = encrypt(key.strip())
    else:
        data.pop(SETTINGS_FIELD, None)
    ws.settings_json = data
    flag_modified(ws, "settings_json")


def headers(key: str) -> dict:
    return {"x-api-key": key, "anthropic-version": API_VERSION, "content-type": "application/json"}


def verify_key(key: str, timeout: float = 10.0) -> tuple[bool, str]:
    """Check a key against the Anthropic API (lists models; uses no tokens). Returns (ok, message)."""
    key = (key or "").strip()
    if not key:
        return False, "Enter an API key."
    try:
        import httpx

        r = httpx.get(f"{API_BASE}/models", headers=headers(key), params={"limit": 1}, timeout=timeout)
    except Exception as exc:  # network error: can't verify, but don't block saving
        log.warning("Could not reach Anthropic API to verify key: %s", exc)
        return True, "Saved, but the key could not be verified (the Anthropic API was unreachable)."
    if r.status_code == 200:
        return True, "API key verified and saved. AI features are now on."
    if r.status_code in (401, 403):
        return False, "Anthropic rejected this API key. Check that you copied it correctly."
    return True, f"Saved, but verification returned HTTP {r.status_code}; the key will be tried on the next AI request."
