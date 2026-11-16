"""OAuth 2.0 provider endpoints (PRD 15.3 P3) + OAuth app management UI.

Mounted through routers/integrations.py (include_router), so main.py needs no change.

  GET  /.well-known/oauth-authorization-server   metadata (RFC 8414)
  GET  /oauth/authorize                          consent page (login required, PKCE S256 required)
  POST /oauth/authorize                          approve / deny -> redirect with code or error
  POST /oauth/token                              authorization_code | refresh_token grants
  POST /oauth/revoke                             RFC 7009 token revocation
  /app/integrations/oauth-apps                   register apps, rotate secret, delete; revoke authorized apps
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import audit
from app.config import settings
from app.db import get_db
from app.deps import Ctx, Forbidden, flash, get_ctx, redirect, render
from app.models import Workspace, WorkspaceMember
from app.models_commerce import OAuthApp
from app.ratelimit import enforce
from app.services import oauth as O

router = APIRouter(include_in_schema=False)
AUTH_PARAMS = ("response_type", "client_id", "redirect_uri", "scope", "state", "code_challenge",
               "code_challenge_method")


def _no_store(body: dict, status_code: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status_code, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


@router.get("/.well-known/oauth-authorization-server")
def metadata():
    base = settings.base_url.rstrip("/")
    return {
        "issuer": base, "authorization_endpoint": f"{base}/oauth/authorize", "token_endpoint": f"{base}/oauth/token",
        "revocation_endpoint": f"{base}/oauth/revoke", "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"], "scopes_supported": sorted(O.SCOPES),
        "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
    }


def _memberships(db: Session, ctx: Ctx) -> list[Workspace]:
    ids = db.scalars(select(WorkspaceMember.workspace_id).where(
        WorkspaceMember.user_id == ctx.user.id, WorkspaceMember.status == "active")).all()
    return list(db.scalars(select(Workspace).where(Workspace.id.in_(ids or ["-"]))).all())


def _error_response(request: Request, exc: O.OAuthError, req_params: dict, redirect_uri: Optional[str]):
    if exc.redirectable and redirect_uri:
        return RedirectResponse(O.redirect_with(redirect_uri, {"error": exc.error, "error_description": exc.description,
                                                               "state": req_params.get("state")}), status_code=303)
    return render(request, "error.html", status_code=400, title="Authorization request invalid",
                  message=f"{exc.error}: {exc.description}")


@router.get("/oauth/authorize")
def authorize_page(request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    params = {k: request.query_params.get(k) for k in AUTH_PARAMS}
    try:
        req = O.validate_authorize(db, params)
    except O.OAuthError as exc:
        app = O.get_app(db, params.get("client_id"))
        uri = params.get("redirect_uri") if app and params.get("redirect_uri") in (app.redirect_uris or []) else None
        return _error_response(request, exc, params, uri)
    params["redirect_uri"] = req.redirect_uri
    params["scope"] = " ".join(req.scopes)
    return render(request, "app/integrations/oauth_consent.html", ctx, app=req.app, scopes=req.scopes,
                  scope_labels=O.SCOPES, params=params, workspaces=_memberships(db, ctx))


@router.post("/oauth/authorize")
async def authorize_decision(request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    form = await request.form()
    params = {k: form.get(k) for k in AUTH_PARAMS}
    try:
        req = O.validate_authorize(db, params)
    except O.OAuthError as exc:
        return _error_response(request, exc, params, None)
    if form.get("decision") != "allow":
        return RedirectResponse(O.redirect_with(req.redirect_uri, {
            "error": "access_denied", "error_description": "The user denied the request", "state": req.state}),
            status_code=303)
    wsid = form.get("workspace_id") or ctx.workspace.id
    if wsid not in {w.id for w in _memberships(db, ctx)}:
        raise Forbidden("You are not a member of that workspace")
    code = O.issue_code(db, req, ctx.user, wsid)
    audit(db, "oauth.authorized", workspace_id=wsid, actor_id=ctx.user.id, entity_type="oauth_app",
          entity_id=req.app.id, metadata={"scopes": req.scopes, "client_id": req.app.client_id})
    db.commit()
    return RedirectResponse(O.redirect_with(req.redirect_uri, {"code": code, "state": req.state}), status_code=303)


@router.post("/oauth/token")
async def token(request: Request, db: Session = Depends(get_db)):
    enforce(request, "oauth_token", limit=60, window_seconds=60)
    form = {k: str(v) for k, v in (await request.form()).items()}
    try:
        cid, secret = O.client_credentials_from(request.headers.get("authorization"), form)
        app = O.authenticate_client(db, cid, secret)
        grant = form.get("grant_type")
        if grant == "authorization_code":
            body = O.exchange_code(db, app, form.get("code"), form.get("redirect_uri"), form.get("code_verifier"))
        elif grant == "refresh_token":
            body = O.refresh_grant(db, app, form.get("refresh_token"), form.get("scope"))
        else:
            raise O.OAuthError("unsupported_grant_type", "Use authorization_code or refresh_token")
    except O.OAuthError as exc:
        db.rollback()
        headers = {"WWW-Authenticate": "Basic"} if exc.status_code == 401 else {}
        resp = _no_store(exc.body(), exc.status_code)
        resp.headers.update(headers)
        return resp
    db.commit()
    return _no_store(body)


@router.post("/oauth/revoke")
async def revoke(request: Request, db: Session = Depends(get_db)):
    enforce(request, "oauth_revoke", limit=60, window_seconds=60)
    form = {k: str(v) for k, v in (await request.form()).items()}
    try:
        cid, secret = O.client_credentials_from(request.headers.get("authorization"), form)
        app = O.authenticate_client(db, cid, secret)
    except O.OAuthError as exc:
        return _no_store(exc.body(), exc.status_code)
    O.revoke_token(db, app, form.get("token"))
    db.commit()
    return _no_store({})


# ------------------------------------------------------------------ management UI
def _allowed(ctx: Ctx) -> None:
    if not ctx.feature("api"):
        raise Forbidden("OAuth apps require the Standard plan or higher. Upgrade in Settings > Billing.")
    if not ctx.can("admin"):
        raise Forbidden("Only workspace admins can register OAuth apps.")


def _own_app(db: Session, ctx: Ctx, app_id: str) -> OAuthApp:
    app = db.get(OAuthApp, app_id)
    if not app or app.workspace_id != ctx.workspace.id:
        raise HTTPException(404)
    return app


@router.get("/app/integrations/oauth-apps")
def oauth_apps_page(request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    apps = []
    if ctx.feature("api") and ctx.can("admin"):
        apps = db.scalars(select(OAuthApp).where(OAuthApp.workspace_id == ctx.workspace.id, OAuthApp.revoked_at.is_(None))
                          .order_by(OAuthApp.created_at.desc())).all()
    return render(request, "app/integrations/oauth_apps.html", ctx, nav="integrations", tab="oauth", apps=apps,
                  can_manage=ctx.feature("api") and ctx.can("admin"), scope_labels=O.SCOPES,
                  authorized=O.authorized_apps(db, ctx.user.id), new_secret=request.session.pop("_new_oauth_secret", None))


@router.post("/app/integrations/oauth-apps")
async def oauth_apps_create(request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    _allowed(ctx)
    form = await request.form()
    uris = [u.strip() for u in (form.get("redirect_uris") or "").replace(",", "\n").splitlines() if u.strip()]
    try:
        app, secret = O.register_app(db, ctx.workspace.id, ctx.user.id, form.get("name") or "", uris,
                                     form.getlist("scopes") or ["read"], form.get("homepage_url") or None)
    except O.OAuthError as exc:
        db.rollback()
        flash(request, exc.description, "error")
        return redirect("/app/integrations/oauth-apps")
    audit(db, "oauth_app.created", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="oauth_app",
          entity_id=app.id, metadata={"name": app.name, "client_id": app.client_id})
    db.commit()
    request.session["_new_oauth_secret"] = {"app_id": app.id, "client_id": app.client_id, "secret": secret}
    flash(request, "OAuth app registered. Copy the client secret now - it won't be shown again.")
    return redirect("/app/integrations/oauth-apps")


@router.post("/app/integrations/oauth-apps/{app_id}/rotate-secret")
def oauth_apps_rotate(app_id: str, request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    _allowed(ctx)
    app = _own_app(db, ctx, app_id)
    secret = O.rotate_secret(db, app)
    audit(db, "oauth_app.secret_rotated", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="oauth_app",
          entity_id=app.id)
    db.commit()
    request.session["_new_oauth_secret"] = {"app_id": app.id, "client_id": app.client_id, "secret": secret}
    flash(request, "New client secret generated; the old secret no longer works.")
    return redirect("/app/integrations/oauth-apps")


@router.post("/app/integrations/oauth-apps/{app_id}/delete")
def oauth_apps_delete(app_id: str, request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    _allowed(ctx)
    app = _own_app(db, ctx, app_id)
    O.revoke_app(db, app)
    audit(db, "oauth_app.revoked", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="oauth_app",
          entity_id=app.id)
    db.commit()
    flash(request, f"'{app.name}' deleted. All of its tokens were revoked.")
    return redirect("/app/integrations/oauth-apps")


@router.post("/app/integrations/authorized-apps/{app_id}/revoke")
def authorized_app_revoke(app_id: str, request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    n = O.revoke_grants_for_user(db, ctx.user.id, app_id)
    audit(db, "oauth.grant_revoked", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="oauth_app",
          entity_id=app_id, metadata={"tokens": n})
    db.commit()
    flash(request, "Access revoked. The app can no longer use your account.")
    return redirect("/app/integrations/oauth-apps")
