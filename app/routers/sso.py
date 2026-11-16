"""SAML 2.0 service-provider endpoints (PRD 17.2). Logic lives in app.services.sso.

  GET  /sso/{workspace_slug}/metadata   SP metadata XML
  GET  /sso/{workspace_slug}/login      SP-initiated AuthnRequest (HTTP-Redirect binding)
  POST /sso/{workspace_slug}/acs        Assertion Consumer Service (HTTP-POST binding)
"""
from typing import Optional
from xml.sax.saxutils import escape as xml_escape

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import audit
from app.db import get_db
from app.deps import login_session, redirect, render
from app.models import Workspace
from app.ratelimit import client_ip, enforce
from app.services import sso as sso_svc

router = APIRouter()


def _ws(db: Session, slug: str) -> Optional[Workspace]:
    return db.scalar(select(Workspace).where(Workspace.slug == slug))


def _error(request: Request, message: str, status: int = 400):
    return render(request, "error.html", status_code=status, title="Single sign-on failed", message=message)


def _safe_next(nxt: Optional[str]) -> str:
    if nxt and nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt:
        return nxt
    return "/app"


@router.get("/sso/{workspace_slug}/metadata", include_in_schema=False)
def sp_metadata(workspace_slug: str, db: Session = Depends(get_db)):
    ws = _ws(db, workspace_slug)
    if not ws:
        return Response("Not found", status_code=404)
    sp = sso_svc.sp_info(ws)
    xml = (
        '<?xml version="1.0"?>\n'
        f'<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" entityID="{xml_escape(sp["entity_id"])}">\n'
        '  <md:SPSSODescriptor AuthnRequestsSigned="false" WantAssertionsSigned="true" '
        'protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">\n'
        '    <md:NameIDFormat>urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress</md:NameIDFormat>\n'
        '    <md:AssertionConsumerService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" '
        f'Location="{xml_escape(sp["acs_url"])}" index="0" isDefault="true"/>\n'
        '  </md:SPSSODescriptor>\n</md:EntityDescriptor>\n'
    )
    return Response(xml, media_type="application/samlmetadata+xml")


@router.get("/sso/{workspace_slug}/login", include_in_schema=False)
def sso_login(workspace_slug: str, request: Request, next: str = "", login_hint: str = "",
              db: Session = Depends(get_db)):
    enforce(request, "sso-login", limit=30, window_seconds=60)
    ws = _ws(db, workspace_slug)
    cfg = sso_svc.get_config(db, ws.id) if ws else None
    if not ws or not sso_svc.sso_active(ws, cfg):
        return _error(request, "Single sign-on is not enabled for this workspace.", 404)
    url = sso_svc.build_authn_request(db, ws, cfg, next_url=_safe_next(next), login_hint=login_hint[:320])
    db.commit()
    return redirect(url, status_code=302)


@router.post("/sso/{workspace_slug}/acs", include_in_schema=False)
async def sso_acs(workspace_slug: str, request: Request, db: Session = Depends(get_db)):
    enforce(request, "sso-acs", limit=60, window_seconds=60)
    ws = _ws(db, workspace_slug)
    cfg = sso_svc.get_config(db, ws.id) if ws else None
    if not ws or not sso_svc.sso_active(ws, cfg):
        return _error(request, "Single sign-on is not enabled for this workspace.", 404)
    form = await request.form()
    saml_response = form.get("SAMLResponse") or ""
    if not saml_response:
        return _error(request, "Missing SAMLResponse.")
    ip = client_ip(request)
    try:
        ident = sso_svc.validate_response(db, ws, cfg, str(saml_response))
        user = sso_svc.login_or_provision(db, ws, cfg, ident)
    except sso_svc.SamlError as e:
        db.rollback()
        audit(db, "sso.login_failed", workspace_id=ws.id, actor_id="sso", metadata={"reason": str(e)}, ip=ip)
        db.commit()
        return _error(request, str(e), 403)
    audit(db, "user.login", workspace_id=ws.id, actor_id=user.id, entity_type="user", entity_id=user.id,
          metadata={"via": "saml"}, ip=ip)
    db.commit()
    login_session(request, user, ws.id)
    request.session["auth_method"] = "sso"
    return redirect(_safe_next(ident.get("next")))
