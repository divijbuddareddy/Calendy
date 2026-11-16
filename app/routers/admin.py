"""Workspace admin & enterprise security (PRD 17): security settings (approved domains,
retention, quiet hours), audit log search/export, SAML SSO configuration + SCIM token,
organisation data deletion, platform/operator health screen and usage meters.

Managed event templates live in app.routers.teams (/app/admin/managed-events)."""
import csv
import datetime as dt
import io
import re
from typing import Optional
from xml.sax.saxutils import escape as xml_escape

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.audit import audit
from app.config import settings
from app.db import get_db
from app.deps import Ctx, Forbidden, flash, get_ctx, redirect, render, require_role
from app.models import (
    AuditLog,
    Booking,
    BookingAnswer,
    CalendarConnection,
    Contact,
    DataRequest,
    Invitee,
    MeetingPoll,
    NotificationJob,
    OutboxEvent,
    PollVote,
    ProviderWebhookInbox,
    RoutingSubmission,
    SSOConfig,
    UsageEvent,
    User,
    WaitlistEntry,
    WebhookDelivery,
    WebhookSubscription,
    Workspace,
    WorkspaceMember,
    utcnow,
)
from app.ratelimit import client_ip
from app.security import hash_secret, opaque_token
from app.services import sso as sso_svc
from app.services.enterprise import audit_query, audit_row_json, delete_invitee_data  # noqa: F401 (re-export)

router = APIRouter()

HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
MIN_RETENTION_DAYS = 30


def _form_dict(form) -> dict:
    return {k: v for k, v in form.items()}


def _settings(ws: Workspace) -> dict:
    return dict(ws.settings_json or {})


def _save_settings(ws: Workspace, data: dict):
    ws.settings_json = data
    flag_modified(ws, "settings_json")


# --------------------------------------------------------------------------- overview + security
@router.get("/app/admin")
def admin_index(request: Request, ctx: Ctx = Depends(require_role("admin")), db: Session = Depends(get_db)):
    s = _settings(ctx.workspace)
    qh = s.get("quiet_hours") or {}
    sso = db.scalar(select(SSOConfig).where(SSOConfig.workspace_id == ctx.workspace.id))
    recent = db.scalars(
        select(AuditLog).where(AuditLog.workspace_id == ctx.workspace.id).order_by(AuditLog.created_at.desc()).limit(8)
    ).all()
    return render(
        request, "app/admin/index.html", ctx, nav="admin", tab="overview",
        approved_domains=", ".join(s.get("approved_domains") or []), retention_days=s.get("retention_days", 365),
        policy=sso_svc.policy(ctx.workspace), login_methods=sso_svc.LOGIN_METHODS, sso_active=sso_svc.sso_active(ctx.workspace, sso),
        quiet_start=qh.get("start", ""), quiet_end=qh.get("end", ""), sso=sso, recent=recent,
        actors=_actor_map(db, recent),
    )


@router.post("/app/admin/security")
async def admin_security(request: Request, ctx: Ctx = Depends(require_role("admin")), db: Session = Depends(get_db)):
    form = _form_dict(await request.form())
    ws = db.get(Workspace, ctx.workspace.id)
    s = _settings(ws)
    errors = []

    # approved domains (domain control is an Enterprise entitlement)
    raw = (form.get("approved_domains") or "").replace("\n", ",").replace(" ", ",")
    domains = sorted({d.strip().lower().lstrip("@") for d in raw.split(",") if d.strip()})
    if domains != sorted(s.get("approved_domains") or []):
        if not ctx.feature("domain_control"):
            errors.append("Approved-domain restrictions require the Enterprise plan (domain control).")
        else:
            bad = [d for d in domains if not DOMAIN.match(d)]
            if bad:
                errors.append("Invalid domain(s): " + ", ".join(bad))
            else:
                s["approved_domains"] = domains

    # retention
    try:
        days = int(form.get("retention_days") or s.get("retention_days") or 365)
        if days < MIN_RETENTION_DAYS or days > 3650:
            raise ValueError
        s["retention_days"] = days
    except ValueError:
        errors.append(f"Retention must be between {MIN_RETENTION_DAYS} and 3650 days.")

    # quiet hours
    qs, qe = (form.get("quiet_start") or "").strip(), (form.get("quiet_end") or "").strip()
    if qs or qe:
        if not (HHMM.match(qs) and HHMM.match(qe)) or qs == qe:
            errors.append("Quiet hours need a valid start and end time (HH:MM) that differ.")
        else:
            s["quiet_hours"] = {"start": qs, "end": qe, "tz": form.get("quiet_tz") or ctx.user.default_timezone}
    else:
        s.pop("quiet_hours", None)

    # custom security policies (PRD 17.2): stored in settings_json.security_policy, enforced by
    # app.services.sso (login methods / required SSO) and the session check (max age)
    if "policy_present" in form:
        pol = dict(s.get("security_policy") or {})
        raw_age = (form.get("session_max_age_minutes") or "").strip()
        if raw_age:
            try:
                age = int(raw_age)
                if not 5 <= age <= 60 * 24 * 90:
                    raise ValueError
                pol["session_max_age_minutes"] = age
            except ValueError:
                errors.append("Session max age must be between 5 minutes and 90 days (129600 minutes).")
        else:
            pol["session_max_age_minutes"] = None
        methods = [m for m in sso_svc.LOGIN_METHODS if form.get(f"method_{m}")]
        sso_cfg = db.scalar(select(SSOConfig).where(SSOConfig.workspace_id == ws.id))
        sso_on = sso_svc.sso_active(ws, sso_cfg)
        if not methods:
            errors.append("Allow at least one sign-in method.")
        elif methods == ["sso"] and not sso_on:
            errors.append("SAML SSO must be configured and enabled before it can be the only sign-in method.")
        else:
            pol["allowed_login_methods"] = methods
        want_sso = bool(form.get("require_sso"))
        if want_sso and not (sso_on and sso_svc.verified_domains(db, ws.id)):
            errors.append("Requiring SSO needs an enabled SSO configuration and at least one verified domain.")
            want_sso = False
        pol["require_sso"] = want_sso
        pol["jit_provisioning"] = bool(form.get("jit_provisioning"))
        s["security_policy"] = pol

    _save_settings(ws, s)
    sso = db.scalar(select(SSOConfig).where(SSOConfig.workspace_id == ws.id))
    if sso and "approved_domains" in s:
        sso.approved_domains_json = s["approved_domains"]
    audit(
        db, "workspace.security_updated", workspace_id=ws.id, actor_id=ctx.user.id, entity_type="workspace", entity_id=ws.id,
        metadata={"approved_domains": s.get("approved_domains"), "retention_days": s.get("retention_days"),
                  "quiet_hours": s.get("quiet_hours"), "security_policy": s.get("security_policy")},
        ip=client_ip(request),
    )
    db.commit()
    for e in errors:
        flash(request, e, "error")
    if not errors:
        flash(request, "Security settings saved.")
    return redirect("/app/admin")


# --------------------------------------------------------------------------- audit log
def _actor_map(db: Session, rows) -> dict:
    ids = {r.actor_id for r in rows if r.actor_id}
    if not ids:
        return {}
    return {u.id: u.email for u in db.scalars(select(User).where(User.id.in_(ids))).all()}


def _audit_query(db: Session, ctx: Ctx, qp) -> tuple:
    return audit_query(db, ctx.workspace.id, qp)


@router.get("/app/admin/audit")
def audit_page(request: Request, ctx: Ctx = Depends(require_role("admin")), db: Session = Depends(get_db)):
    q, params = _audit_query(db, ctx, request.query_params)
    rows = db.scalars(q.limit(200)).all()
    return render(
        request, "app/admin/audit.html", ctx, nav="admin", tab="audit", rows=rows, params=params,
        actors=_actor_map(db, rows), query=str(request.url.query), can_export=ctx.feature("audit_export"),
    )


@router.get("/app/admin/audit.csv")
def audit_export(request: Request, ctx: Ctx = Depends(require_role("admin")), db: Session = Depends(get_db)):
    from app.services.analytics import csv_safe

    if not ctx.feature("audit_export"):
        raise Forbidden("Your plan does not include 'audit_export'. Upgrade in Settings > Billing.")
    q, _ = _audit_query(db, ctx, request.query_params)
    rows = db.scalars(q.limit(50000)).all()
    actors = _actor_map(db, rows)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["created_at_utc", "action", "actor_id", "actor_email", "entity_type", "entity_id", "ip", "metadata"])
    import json

    for r in rows:
        w.writerow([csv_safe(x) for x in (
            r.created_at.isoformat(), r.action, r.actor_id or "", actors.get(r.actor_id, ""), r.entity_type or "",
            r.entity_id or "", r.ip or "", json.dumps(r.metadata_json or {}, default=str),
        )])
    audit(db, "audit.exported", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, metadata={"rows": len(rows)},
          ip=client_ip(request))
    db.commit()
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="audit_log.csv"'})


@router.get("/app/admin/audit.json")
def audit_export_json(request: Request, ctx: Ctx = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Audit log search as JSON (same filters as the page, incl. free-text `q`); `limit` <= 5000."""
    if not ctx.feature("audit_export"):
        raise Forbidden("Your plan does not include 'audit_export'. Upgrade in Settings > Billing.")
    q, params = _audit_query(db, ctx, request.query_params)
    try:
        limit = max(1, min(5000, int(request.query_params.get("limit") or 1000)))
    except ValueError:
        limit = 1000
    rows = db.scalars(q.limit(limit)).all()
    actors = _actor_map(db, rows)
    audit(db, "audit.exported", workspace_id=ctx.workspace.id, actor_id=ctx.user.id,
          metadata={"rows": len(rows), "format": "json"}, ip=client_ip(request))
    db.commit()
    return JSONResponse({"filters": params, "count": len(rows), "data": [audit_row_json(r, actors) for r in rows]},
                        headers={"Content-Disposition": 'attachment; filename="audit_log.json"'})


# --------------------------------------------------------------------------- SSO / SCIM
def _sp_info(ws: Workspace) -> dict:
    return sso_svc.sp_info(ws)


def _cert_info(pem: Optional[str]) -> Optional[dict]:
    if not pem:
        return None
    from cryptography import x509

    text = pem.strip()
    if "BEGIN CERTIFICATE" not in text:
        body = re.sub(r"\s+", "", text)
        text = "-----BEGIN CERTIFICATE-----\n" + "\n".join(body[i:i + 64] for i in range(0, len(body), 64)) + "\n-----END CERTIFICATE-----"
    try:
        cert = x509.load_pem_x509_certificate(text.encode())
    except Exception:
        return {"valid": False}
    try:
        not_after = cert.not_valid_after_utc.replace(tzinfo=None)
    except AttributeError:  # older cryptography
        not_after = cert.not_valid_after
    return {"valid": True, "subject": cert.subject.rfc4514_string(), "not_after": not_after,
            "expired": not_after < utcnow()}


def _sso_page_ctx(db: Session, ctx: Ctx, sso: Optional[SSOConfig]) -> dict:
    from app.models_enterprise import DomainClaim

    claims = db.scalars(select(DomainClaim).where(DomainClaim.workspace_id == ctx.workspace.id)
                        .order_by(DomainClaim.domain)).all()
    return {
        "sso": sso, "sp": _sp_info(ctx.workspace), "cert": _cert_info(sso.idp_x509_cert if sso else None),
        "sso_ok": ctx.feature("sso"), "scim_ok": ctx.feature("scim"), "domain_ok": ctx.feature("domain_control"),
        "claims": [(c, sso_svc.txt_record(c)) for c in claims],
        "verified": sorted(c.domain for c in claims if c.verified_at),
        "active": sso_svc.sso_active(ctx.workspace, sso),
    }


@router.get("/app/admin/sso")
def sso_page(request: Request, ctx: Ctx = Depends(require_role("admin")), db: Session = Depends(get_db)):
    sso = db.scalar(select(SSOConfig).where(SSOConfig.workspace_id == ctx.workspace.id))
    return render(request, "app/admin/sso.html", ctx, nav="admin", tab="sso", new_token=None,
                  **_sso_page_ctx(db, ctx, sso))


def _get_or_create_sso(db: Session, ws_id: str) -> SSOConfig:
    sso = db.scalar(select(SSOConfig).where(SSOConfig.workspace_id == ws_id))
    if not sso:
        sso = SSOConfig(workspace_id=ws_id)
        db.add(sso)
        db.flush()
    return sso


@router.post("/app/admin/sso")
async def sso_save(request: Request, ctx: Ctx = Depends(require_role("admin")), db: Session = Depends(get_db)):
    if not ctx.feature("sso"):
        raise Forbidden("Your plan does not include 'sso'. Upgrade in Settings > Billing.")
    form = _form_dict(await request.form())
    url = (form.get("idp_sso_url") or "").strip()
    if url and not url.startswith("https://"):
        flash(request, "The IdP SSO URL must use https://", "error")
        return redirect("/app/admin/sso")
    cert = (form.get("idp_x509_cert") or "").strip()
    info = _cert_info(cert)
    if info and not info["valid"]:
        flash(request, "The x509 certificate could not be parsed. Paste the PEM certificate from your IdP.", "error")
        return redirect("/app/admin/sso")
    sso = _get_or_create_sso(db, ctx.workspace.id)
    sso.idp_entity_id = (form.get("idp_entity_id") or "").strip() or None
    sso.idp_sso_url = url or None
    sso.idp_x509_cert = cert or None
    sso.enabled = bool(form.get("enabled")) and bool(sso.idp_entity_id and sso.idp_sso_url and sso.idp_x509_cert)
    want_enforce = bool(form.get("enforce_sso"))
    verified = sso_svc.verified_domains(db, ctx.workspace.id)
    warn = None
    if want_enforce and not sso.enabled:
        warn = "SSO can only be enforced once the configuration is complete and enabled."
    elif want_enforce and not verified:
        warn = "Verify at least one email domain before enforcing SSO (enforcement applies to verified domains)."
    sso.enforce_sso = want_enforce and not warn
    sso.approved_domains_json = _settings(ctx.workspace).get("approved_domains")
    audit(db, "sso.config_updated", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="sso_config",
          entity_id=sso.id, metadata={"idp_entity_id": sso.idp_entity_id, "enabled": sso.enabled,
                                      "enforce_sso": sso.enforce_sso}, ip=client_ip(request))
    db.commit()
    flash(request, "SSO configuration saved." + (f" {warn}" if warn else ""), "warning" if warn else "success")
    return redirect("/app/admin/sso")


@router.post("/app/admin/sso/test")
def sso_test(request: Request, ctx: Ctx = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Start an SP-initiated sign-in as a connection test."""
    sso = db.scalar(select(SSOConfig).where(SSOConfig.workspace_id == ctx.workspace.id))
    if not sso_svc.sso_active(ctx.workspace, sso):
        flash(request, "Complete and enable the SSO configuration first.", "error")
        return redirect("/app/admin/sso")
    return redirect(f"/sso/{ctx.workspace.slug}/login?next=/app/admin/sso")


# --------------------------------------------------------------------------- domain claims
@router.post("/app/admin/domains")
async def domain_claim(request: Request, ctx: Ctx = Depends(require_role("admin")), db: Session = Depends(get_db)):
    if not ctx.feature("domain_control"):
        raise Forbidden("Your plan does not include 'domain_control'. Upgrade in Settings > Billing.")
    form = _form_dict(await request.form())
    domain = (form.get("domain") or "").strip().lower().lstrip("@")
    if not DOMAIN.match(domain):
        flash(request, "Enter a valid domain such as acme.com.", "error")
        return redirect("/app/admin/sso#domains")
    c = sso_svc.claim_domain(db, ctx.workspace.id, domain)
    audit(db, "domain.claimed", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="domain_claim",
          entity_id=c.id, metadata={"domain": domain}, ip=client_ip(request))
    db.commit()
    rec = sso_svc.txt_record(c)
    flash(request, f"Add a TXT record {rec['host']} = {rec['value']}, then click Verify.")
    return redirect("/app/admin/sso#domains")


def _claim(db: Session, ctx: Ctx, cid: str):
    from app.models_enterprise import DomainClaim

    c = db.get(DomainClaim, cid)
    if not c or c.workspace_id != ctx.workspace.id:
        raise Forbidden("Domain claim not found.")
    return c


@router.post("/app/admin/domains/{cid}/verify")
def domain_verify(cid: str, request: Request, ctx: Ctx = Depends(require_role("admin")), db: Session = Depends(get_db)):
    c = _claim(db, ctx, cid)
    ok = sso_svc.verify_domain(db, c)
    if ok:
        ws = db.get(Workspace, ctx.workspace.id)
        s = _settings(ws)
        s["approved_domains"] = sorted(set(s.get("approved_domains") or []) | {c.domain})
        _save_settings(ws, s)
        audit(db, "domain.verified", workspace_id=ws.id, actor_id=ctx.user.id, entity_type="domain_claim",
              entity_id=c.id, metadata={"domain": c.domain}, ip=client_ip(request))
    db.commit()
    flash(request, f"{c.domain} verified." if ok else f"{c.domain}: {c.last_error}", "success" if ok else "error")
    return redirect("/app/admin/sso#domains")


@router.post("/app/admin/domains/{cid}/delete")
def domain_delete(cid: str, request: Request, ctx: Ctx = Depends(require_role("admin")), db: Session = Depends(get_db)):
    c = _claim(db, ctx, cid)
    domain = c.domain
    audit(db, "domain.removed", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="domain_claim",
          entity_id=c.id, metadata={"domain": domain}, ip=client_ip(request))
    db.delete(c)
    db.flush()
    sso = db.scalar(select(SSOConfig).where(SSOConfig.workspace_id == ctx.workspace.id))
    if sso and sso.enforce_sso and not sso_svc.verified_domains(db, ctx.workspace.id):
        sso.enforce_sso = False
    db.commit()
    flash(request, f"Domain {domain} removed.")
    return redirect("/app/admin/sso#domains")


@router.post("/app/admin/scim-token")
def scim_token(request: Request, ctx: Ctx = Depends(require_role("admin")), db: Session = Depends(get_db)):
    if not ctx.feature("scim"):
        raise Forbidden("Your plan does not include 'scim'. Upgrade in Settings > Billing.")
    sso = _get_or_create_sso(db, ctx.workspace.id)
    token = "scim_" + opaque_token(32)
    rotated = bool(sso.scim_token_hash)
    sso.scim_token_hash = hash_secret(token)
    audit(db, "scim.token_rotated" if rotated else "scim.token_created", workspace_id=ctx.workspace.id,
          actor_id=ctx.user.id, entity_type="sso_config", entity_id=sso.id, ip=client_ip(request))
    db.commit()
    return render(request, "app/admin/sso.html", ctx, nav="admin", tab="sso", new_token=token,
                  **_sso_page_ctx(db, ctx, sso))


@router.post("/app/admin/scim-token/revoke")
def scim_token_revoke(request: Request, ctx: Ctx = Depends(require_role("admin")), db: Session = Depends(get_db)):
    sso = db.scalar(select(SSOConfig).where(SSOConfig.workspace_id == ctx.workspace.id))
    if sso and sso.scim_token_hash:
        sso.scim_token_hash = None
        audit(db, "scim.token_revoked", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="sso_config",
              entity_id=sso.id, ip=client_ip(request))
        db.commit()
        flash(request, "SCIM token revoked. Your IdP can no longer provision users.")
    return redirect("/app/admin/sso")


# --------------------------------------------------------------------------- data deletion
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@router.get("/app/admin/data-deletion")
def data_deletion_page(request: Request, ctx: Ctx = Depends(require_role("admin")), db: Session = Depends(get_db)):
    history = db.scalars(
        select(DataRequest).where(DataRequest.workspace_id == ctx.workspace.id, DataRequest.kind == "deletion")
        .order_by(DataRequest.created_at.desc()).limit(20)
    ).all()
    return render(request, "app/admin/data_deletion.html", ctx, nav="admin", tab="data", history=history,
                  allowed=ctx.feature("data_deletion_api"), result=None)


@router.post("/app/admin/data-deletion")
async def data_deletion(request: Request, ctx: Ctx = Depends(require_role("admin")), db: Session = Depends(get_db)):
    if not ctx.feature("data_deletion_api"):
        raise Forbidden("Your plan does not include 'data_deletion_api'. Upgrade in Settings > Billing.")
    form = _form_dict(await request.form())
    email = (form.get("email") or "").strip()
    if not EMAIL_RE.match(email):
        flash(request, "Enter a valid email address.", "error")
        return redirect("/app/admin/data-deletion")
    if (form.get("confirm") or "").strip().lower() != email.lower():
        flash(request, "Type the email address again to confirm deletion.", "error")
        return redirect("/app/admin/data-deletion")
    counts = delete_invitee_data(db, ctx.workspace.id, email, ctx.user.id, client_ip(request))
    db.commit()
    flash(request, "Invitee data deleted: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    return redirect("/app/admin/data-deletion")


@router.post("/app/api/admin/data-deletion")
async def data_deletion_api(request: Request, ctx: Ctx = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """JSON data deletion API: {"email": "..."} -> anonymisation counts."""
    if not ctx.feature("data_deletion_api"):
        raise Forbidden("Your plan does not include 'data_deletion_api'.")
    try:
        body = await request.json()
    except Exception:
        body = {}
    email = str((body or {}).get("email") or "").strip()
    if not EMAIL_RE.match(email):
        return JSONResponse({"error": {"code": "VALIDATION_ERROR", "message": "A valid email is required"}}, status_code=422)
    counts = delete_invitee_data(db, ctx.workspace.id, email, ctx.user.id, client_ip(request))
    db.commit()
    return {"status": "completed", "deleted": counts}


# --------------------------------------------------------------------------- health (platform operator / owner)
def require_operator(ctx: Ctx = Depends(get_ctx)) -> Ctx:
    if not (ctx.user.is_platform_admin or ctx.role == "owner"):
        raise Forbidden("The health screen is available to workspace owners and platform operators.")
    return ctx


def _platform(request: Request, ctx: Ctx) -> bool:
    return bool(ctx.user.is_platform_admin and request.query_params.get("scope") == "platform")


@router.get("/app/admin/health")
def health_page(request: Request, ctx: Ctx = Depends(require_operator), db: Session = Depends(get_db)):
    platform = _platform(request, ctx)
    wsid = ctx.workspace.id

    def scoped(q, col):
        return q if platform else q.where(col == wsid)

    pending_q = scoped(select(func.count(OutboxEvent.id)).where(OutboxEvent.processed_at.is_(None)), OutboxEvent.workspace_id)
    backlog = db.scalar(pending_q) or 0
    oldest = db.scalar(scoped(select(func.min(OutboxEvent.created_at)).where(OutboxEvent.processed_at.is_(None)),
                              OutboxEvent.workspace_id))
    failed_events = db.scalars(
        scoped(select(OutboxEvent).where(OutboxEvent.processed_at.is_(None), OutboxEvent.last_error.is_not(None)),
               OutboxEvent.workspace_id).order_by(OutboxEvent.created_at.desc()).limit(50)
    ).all()
    failed_jobs = db.scalars(
        scoped(select(NotificationJob).where(NotificationJob.status == "failed"), NotificationJob.workspace_id)
        .order_by(NotificationJob.updated_at.desc()).limit(50)
    ).all()
    wq = select(WebhookDelivery).join(WebhookSubscription, WebhookSubscription.id == WebhookDelivery.subscription_id).where(
        WebhookDelivery.status.in_(("dead", "failed"))
    )
    dead_hooks = db.scalars(scoped(wq, WebhookSubscription.workspace_id).order_by(WebhookDelivery.updated_at.desc()).limit(50)).all()
    inbox_dead = []
    if ctx.user.is_platform_admin:  # provider inbox rows are not tenant-attributed
        inbox_dead = db.scalars(
            select(ProviderWebhookInbox).where(ProviderWebhookInbox.status.in_(("failed", "dead")))
            .order_by(ProviderWebhookInbox.created_at.desc()).limit(50)
        ).all()
    cal_errors = db.scalars(
        scoped(select(CalendarConnection).where(CalendarConnection.status == "error"), CalendarConnection.workspace_id).limit(50)
    ).all()
    held = db.scalar(scoped(select(func.count(Booking.id)).where(Booking.status == "held"), Booking.workspace_id)) or 0
    stale_held = db.scalar(scoped(select(func.count(Booking.id)).where(
        Booking.status == "held", Booking.hold_expires_at < utcnow()), Booking.workspace_id)) or 0
    uids = {c.user_id for c in cal_errors}
    users = {u.id: u.email for u in db.scalars(select(User).where(User.id.in_(uids))).all()} if uids else {}
    if platform:
        audit(db, "platform.health_viewed", workspace_id=None, actor_id=ctx.user.id, ip=client_ip(request))
        db.commit()
    return render(
        request, "app/admin/health.html", ctx, nav="admin", tab="health", platform=platform,
        backlog=backlog, oldest=oldest, failed_events=failed_events, failed_jobs=failed_jobs, dead_hooks=dead_hooks,
        inbox_dead=inbox_dead, cal_errors=cal_errors, held=held, stale_held=stale_held, users=users,
    )


def _retry_target(db: Session, ctx: Ctx, kind: str, rid: str):
    platform = ctx.user.is_platform_admin
    wsid = ctx.workspace.id
    if kind == "outbox":
        row = db.get(OutboxEvent, rid)
        if row and (platform or row.workspace_id == wsid):
            row.attempts = 0
            row.last_error = None
            row.processed_at = None
            return row
    elif kind == "notification":
        row = db.get(NotificationJob, rid)
        if row and (platform or row.workspace_id == wsid):
            row.status = "scheduled"
            row.attempts = 0
            row.error = None
            row.scheduled_at = utcnow()
            row.next_attempt_at = utcnow()
            return row
    elif kind == "webhook":
        row = db.get(WebhookDelivery, rid)
        if row:
            sub = db.get(WebhookSubscription, row.subscription_id)
            if sub and (platform or sub.workspace_id == wsid):
                row.status = "pending"
                row.attempts = 0
                row.next_retry_at = utcnow()
                return row
    elif kind == "inbox" and platform:
        row = db.get(ProviderWebhookInbox, rid)
        if row:
            row.status = "received"
            row.attempts = 0
            row.last_error = None
            return row
    return None


@router.post("/app/admin/health/{kind}/{rid}/retry")
def health_retry(kind: str, rid: str, request: Request, ctx: Ctx = Depends(require_operator), db: Session = Depends(get_db)):
    row = _retry_target(db, ctx, kind, rid)
    if not row:
        flash(request, "Item not found.", "error")
    else:
        audit(db, f"health.{kind}_retried", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type=kind,
              entity_id=rid, ip=client_ip(request))
        db.commit()
        flash(request, "Queued for retry. The worker will pick it up shortly.")
    back = "/app/admin/health" + ("?scope=platform" if request.query_params.get("scope") == "platform" else "")
    return redirect(back)


# --------------------------------------------------------------------------- usage meters
@router.get("/app/admin/usage")
def usage_page(request: Request, ctx: Ctx = Depends(require_role("admin")), db: Session = Depends(get_db)):
    since = utcnow() - dt.timedelta(days=30)
    rows30 = db.execute(
        select(UsageEvent.meter, func.sum(UsageEvent.quantity)).where(
            UsageEvent.workspace_id == ctx.workspace.id, UsageEvent.occurred_at >= since
        ).group_by(UsageEvent.meter)
    ).all()
    rows_all = db.execute(
        select(UsageEvent.meter, func.sum(UsageEvent.quantity)).where(UsageEvent.workspace_id == ctx.workspace.id)
        .group_by(UsageEvent.meter)
    ).all()
    m30 = {m: int(q or 0) for m, q in rows30}
    meters = sorted(({"meter": m, "total": int(q or 0), "last30": m30.get(m, 0)} for m, q in rows_all),
                    key=lambda r: -r["last30"])
    mx = max([r["last30"] for r in meters] + [1])
    for r in meters:
        r["pct"] = round(100 * r["last30"] / mx)
    recent = db.scalars(
        select(UsageEvent).where(UsageEvent.workspace_id == ctx.workspace.id).order_by(UsageEvent.occurred_at.desc()).limit(50)
    ).all()
    return render(request, "app/admin/usage.html", ctx, nav="admin", tab="usage", meters=meters, recent=recent)


# --------------------------------------------------------------------------- API-key data deletion API
def _api_writer():
    from app.routers.api_v1 import writer

    return writer


def _api_delete(request: Request, email: str, p, db: Session):
    from app.errors import FORBIDDEN, APIError
    from app.entitlements import has_feature

    if not has_feature(p.workspace, "data_deletion_api"):
        raise APIError(FORBIDDEN, "Your plan does not include the data deletion API", 403)
    member = db.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == p.workspace.id,
                                                     WorkspaceMember.user_id == p.user.id,
                                                     WorkspaceMember.status == "active"))
    if not member or member.role not in ("owner", "admin"):
        raise APIError(FORBIDDEN, "Data deletion requires an API key created by a workspace admin or owner", 403)
    email = (email or "").strip()
    if not EMAIL_RE.match(email):
        return JSONResponse({"error": {"code": "VALIDATION_ERROR", "message": "A valid email is required"}},
                            status_code=422)
    counts = delete_invitee_data(db, p.workspace.id, email, p.user.id, client_ip(request))
    db.commit()
    return {"status": "completed", "email_hash": hash_secret(email.lower()), "deleted": counts}


@router.delete("/api/v1/admin/data-deletion", tags=["Public API v1"])
def api_data_deletion(request: Request, email: str = "", p=Depends(_api_writer()), db: Session = Depends(get_db)):
    """Organisation-wide invitee data deletion (PRD 17.2). API key with `write` scope, created by an
    admin/owner; Enterprise plan. Anonymises bookings' invitee PII, answers, contacts, routing
    submissions, poll votes, waitlist and CRM mirror rows for the email in this workspace."""
    return _api_delete(request, email, p, db)


@router.post("/api/v1/admin/data-deletion", tags=["Public API v1"])
async def api_data_deletion_post(request: Request, p=Depends(_api_writer()), db: Session = Depends(get_db)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    return _api_delete(request, str((body or {}).get("email") or ""), p, db)


@router.delete("/api/v1/data/invitees", tags=["Public API v1"])
def api_delete_invitees(request: Request, email: str = "", p=Depends(_api_writer()), db: Session = Depends(get_db)):
    """Alias of DELETE /api/v1/admin/data-deletion."""
    return _api_delete(request, email, p, db)
