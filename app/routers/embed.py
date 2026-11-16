"""Embed loader script (/embed.js) and the embed demo / snippet generator page."""
import hashlib
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.deps import Ctx, get_ctx, render
from app.models import EventType

router = APIRouter(include_in_schema=False)
EMBED_PATH = Path(__file__).resolve().parent.parent / "static" / "embed.js"
_cache: dict = {}


def _script() -> tuple[bytes, str]:
    mtime = EMBED_PATH.stat().st_mtime
    if _cache.get("mtime") != mtime:
        body = EMBED_PATH.read_bytes()
        _cache.update(mtime=mtime, body=body, etag='"' + hashlib.sha256(body).hexdigest()[:20] + '"')
    return _cache["body"], _cache["etag"]


@router.get("/embed.js")
def embed_js(request: Request):
    body, etag = _script()
    headers = {
        "Cache-Control": "public, max-age=86400, stale-while-revalidate=604800",
        "ETag": etag,
        "Access-Control-Allow-Origin": "*",
        "Cross-Origin-Resource-Policy": "cross-origin",
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(body, media_type="application/javascript; charset=utf-8", headers=headers)


@router.get("/embed/demo")
def embed_demo(request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    ets = db.scalars(select(EventType).where(
        EventType.owner_user_id == ctx.user.id, EventType.workspace_id == ctx.workspace.id,
        EventType.deleted_at.is_(None), EventType.status == "active").order_by(EventType.created_at)).all()
    base = settings.base_url.rstrip("/") or str(request.base_url).rstrip("/")
    origin = str(request.base_url).rstrip("/")
    options = [{"label": "Profile page (all event types)", "url": f"{origin}/{ctx.user.slug}"}]
    options += [{"label": et.name, "url": f"{origin}/{ctx.user.slug}/{et.slug}"} for et in ets]
    selected = options[1]["url"] if len(options) > 1 else options[0]["url"]
    branding = (ctx.user.branding_json or {}) if ctx.user else {}
    return render(request, "app/integrations/embed_demo.html", ctx, nav="integrations", tab="embed", options=options,
                  selected=selected, base=base, origin=origin, primary=branding.get("primary") or "#3b47d6",
                  can_hide_branding=ctx.feature("custom_branding"))
