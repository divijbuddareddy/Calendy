"""Workflow builder UI (PRD 11.2): list, editor with live preview, default templates, test send,
quiet hours, run history with retry."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit import audit
from app.db import get_db
from app.deps import Ctx, Forbidden, flash, redirect, render, require_feature
from app.models import NotificationJob, Workflow, utcnow
from app.services import notifications as N
from app.services import workflows as W

router = APIRouter()
UNITS = {"minutes": 1, "hours": 60, "days": 1440}
feature = require_feature("workflows")


def _wf(db: Session, ctx: Ctx, wf_id: str) -> Workflow:
    wf = db.get(Workflow, wf_id)
    if not wf or wf.workspace_id != ctx.workspace.id:
        raise HTTPException(404, "Workflow not found")
    return wf


def _split_offset(minutes: int) -> tuple[int, str]:
    if minutes and minutes % 1440 == 0:
        return minutes // 1440, "days"
    if minutes and minutes % 60 == 0:
        return minutes // 60, "hours"
    return minutes or 0, "minutes"


def _editor(request: Request, db: Session, ctx: Ctx, wf: Optional[Workflow], error: Optional[str] = None, form=None):
    data = form or {}
    if wf is not None and not form:
        val, unit = _split_offset(wf.offset_minutes)
        data = {"name": wf.name, "trigger": wf.trigger, "offset_value": val, "offset_unit": unit, "channel": wf.channel,
                "audience": wf.audience, "custom_recipient": wf.custom_recipient or "",
                "subject_template": wf.subject_template or "", "body_template": wf.body_template or "",
                "event_type_ids": (wf.conditions_json or {}).get("event_type_ids") or [], "active": wf.active,
                "managed": wf.managed}
    elif not form:
        t = W.DEFAULT_TEMPLATES[0]
        data = {"name": "", "trigger": "before_event", "offset_value": 24, "offset_unit": "hours", "channel": "email",
                "audience": "invitee", "custom_recipient": "", "subject_template": t["subject_template"],
                "body_template": t["body_template"], "event_type_ids": [], "active": True, "managed": False}
    readonly = bool(wf and not W.can_edit(ctx, wf))
    runs = []
    if wf:
        runs = db.scalars(select(NotificationJob).where(NotificationJob.workflow_id == wf.id)
                          .order_by(NotificationJob.created_at.desc()).limit(10)).all()
    return render(request, "app/workflows/edit.html", ctx, nav="workflows", wf=wf, f=data, error=error,
                  triggers=W.TRIGGERS, variables=W.VARIABLES + W.EXTRA_VARIABLES,
                  event_types=W.event_types_for(db, ctx), readonly=readonly, runs=runs,
                  preview=W.preview(data), can_manage=ctx.can("admin") and ctx.feature("managed_workflows"),
                  sms_enabled=ctx.feature("sms"), status_code=400 if error else 200)


async def _parse(request: Request) -> dict:
    form = await request.form()
    try:
        val = max(0, int(form.get("offset_value") or 0))
    except ValueError:
        val = 0
    unit = form.get("offset_unit") if form.get("offset_unit") in UNITS else "minutes"
    return {
        "name": (form.get("name") or "").strip()[:200], "trigger": form.get("trigger") or "",
        "offset_value": val, "offset_unit": unit, "channel": form.get("channel") or "email",
        "audience": form.get("audience") or "invitee", "custom_recipient": (form.get("custom_recipient") or "").strip(),
        "subject_template": (form.get("subject_template") or "")[:500], "body_template": form.get("body_template") or "",
        "event_type_ids": form.getlist("event_type_ids"), "active": bool(form.get("active")),
        "managed": bool(form.get("managed")),
    }


def _validate(ctx: Ctx, f: dict) -> Optional[str]:
    if not f["name"]:
        return "Give the workflow a name."
    if f["trigger"] not in W.TRIGGERS:
        return "Choose a trigger."
    if f["channel"] not in ("email", "sms"):
        return "Invalid channel."
    if f["channel"] == "sms" and not ctx.feature("sms"):
        return "SMS workflows require a plan with SMS."
    if f["audience"] not in ("invitee", "host", "custom"):
        return "Invalid audience."
    if f["audience"] == "custom" and not f["custom_recipient"]:
        return "Enter a custom recipient."
    if f["channel"] == "sms" and f["audience"] == "host":
        return "SMS can be sent to invitees (with consent) or a custom number."
    if f["channel"] == "email" and not f["subject_template"].strip():
        return "Email workflows need a subject."
    if not f["body_template"].strip():
        return "The message body can't be empty."
    if f["managed"] and not (ctx.can("admin") and ctx.feature("managed_workflows")):
        return "Only admins on the Teams plan can create managed workflows."
    return None


def _apply(wf: Workflow, f: dict, ctx: Ctx, db: Session) -> None:
    valid_ids = {et.id for et in W.event_types_for(db, ctx)}
    wf.name, wf.trigger, wf.channel, wf.audience = f["name"], f["trigger"], f["channel"], f["audience"]
    wf.offset_minutes = f["offset_value"] * UNITS[f["offset_unit"]]
    wf.custom_recipient = f["custom_recipient"] or None
    wf.subject_template, wf.body_template = f["subject_template"], f["body_template"]
    wf.conditions_json = {"event_type_ids": [i for i in f["event_type_ids"] if i in valid_ids]}
    wf.active = f["active"]
    wf.managed = f["managed"]


@router.get("/app/workflows", include_in_schema=False)
def workflows_index(request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(feature)):
    wfs = db.scalars(select(Workflow).where(Workflow.workspace_id == ctx.workspace.id)
                     .order_by(Workflow.managed.desc(), Workflow.created_at)).all()
    counts = dict(db.execute(select(NotificationJob.workflow_id, func.count()).where(
        NotificationJob.workspace_id == ctx.workspace.id, NotificationJob.workflow_id.isnot(None),
        NotificationJob.status == "sent").group_by(NotificationJob.workflow_id)).all())
    failed = dict(db.execute(select(NotificationJob.workflow_id, func.count()).where(
        NotificationJob.workspace_id == ctx.workspace.id, NotificationJob.status == "failed",
        NotificationJob.workflow_id.isnot(None)).group_by(NotificationJob.workflow_id)).all())
    qh = (ctx.workspace.settings_json or {}).get("quiet_hours") or {}
    return render(request, "app/workflows/index.html", ctx, nav="workflows", workflows=wfs, sent_counts=counts,
                  failed_counts=failed, templates_=W.DEFAULT_TEMPLATES, triggers=W.TRIGGERS, quiet=qh,
                  can_edit=lambda wf: W.can_edit(ctx, wf), event_types={e.id: e for e in W.event_types_for(db, ctx)})


@router.get("/app/workflows/new", include_in_schema=False)
def workflow_new(request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(feature)):
    return _editor(request, db, ctx, None)


@router.post("/app/workflows", include_in_schema=False)
async def workflow_create(request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(feature)):
    f = await _parse(request)
    err = _validate(ctx, f)
    if err:
        return _editor(request, db, ctx, None, err, f)
    wf = Workflow(workspace_id=ctx.workspace.id, owner_user_id=ctx.user.id)
    _apply(wf, f, ctx, db)
    db.add(wf)
    db.flush()
    audit(db, "workflow.created", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="workflow", entity_id=wf.id)
    db.commit()
    flash(request, f"Workflow '{wf.name}' created.")
    return redirect(f"/app/workflows/{wf.id}")


@router.post("/app/workflows/preview", include_in_schema=False)
async def workflow_preview(request: Request, ctx: Ctx = Depends(feature)):
    f = await _parse(request)
    return JSONResponse(W.preview(f))


@router.post("/app/workflows/templates/{key}", include_in_schema=False)
def workflow_from_template(key: str, request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(feature)):
    t = next((t for t in W.DEFAULT_TEMPLATES if t["key"] == key), None)
    if t and t["channel"] == "sms" and not ctx.feature("sms"):
        raise Forbidden("SMS workflows require a plan with SMS.")
    wf = W.add_default_template(db, ctx, key)
    if not wf:
        raise HTTPException(404)
    audit(db, "workflow.created", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="workflow",
          entity_id=wf.id, metadata={"template": key})
    db.commit()
    flash(request, f"Added '{wf.name}'. It applies to all of your event types – edit it to narrow it down.")
    return redirect("/app/workflows")


@router.post("/app/workflows/quiet-hours", include_in_schema=False)
async def quiet_hours(request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(feature)):
    if not ctx.can("admin"):
        raise Forbidden("Only admins can change workspace quiet hours.")
    form = await request.form()
    settings_json = dict(ctx.workspace.settings_json or {})
    if form.get("enabled"):
        settings_json["quiet_hours"] = {"start": form.get("start") or "21:00", "end": form.get("end") or "08:00",
                                        "tz": (form.get("tz") or "").strip() or None, "enabled": True}
    else:
        settings_json.pop("quiet_hours", None)
    ctx.workspace.settings_json = settings_json
    audit(db, "workspace.quiet_hours_changed", workspace_id=ctx.workspace.id, actor_id=ctx.user.id,
          metadata=settings_json.get("quiet_hours") or {})
    db.commit()
    flash(request, "Quiet hours saved. Workflow messages that fall inside the window are sent when it ends.")
    return redirect("/app/workflows")


@router.get("/app/workflows/runs", include_in_schema=False)
def all_runs(request: Request, status: str = "", db: Session = Depends(get_db), ctx: Ctx = Depends(feature)):
    q = select(NotificationJob).where(NotificationJob.workspace_id == ctx.workspace.id)
    if status:
        q = q.where(NotificationJob.status == status)
    jobs = db.scalars(q.order_by(NotificationJob.created_at.desc()).limit(200)).all()
    wf_names = {w.id: w.name for w in db.scalars(select(Workflow).where(Workflow.workspace_id == ctx.workspace.id)).all()}
    return render(request, "app/workflows/runs.html", ctx, nav="workflows", jobs=jobs, wf=None, wf_names=wf_names,
                  status=status)


@router.post("/app/workflows/jobs/{job_id}/retry", include_in_schema=False)
def retry_job(job_id: str, request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(feature)):
    job = db.get(NotificationJob, job_id)
    if not job or job.workspace_id != ctx.workspace.id:
        raise HTTPException(404)
    N.retry_job(db, job)
    if job.scheduled_at <= utcnow():
        N.send_job(db, job)
    db.commit()
    flash(request, f"Message {job.status}." + (f" Error: {job.error}" if job.error else ""),
          "success" if job.status == "sent" else "warning")
    return redirect(request.headers.get("referer") or "/app/workflows/runs")


@router.get("/app/workflows/{wf_id}", include_in_schema=False)
def workflow_edit(wf_id: str, request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(feature)):
    return _editor(request, db, ctx, _wf(db, ctx, wf_id))


@router.post("/app/workflows/{wf_id}", include_in_schema=False)
async def workflow_update(wf_id: str, request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(feature)):
    wf = _wf(db, ctx, wf_id)
    if not W.can_edit(ctx, wf):
        raise Forbidden("This workflow is managed by your admin and can't be edited.")
    f = await _parse(request)
    if wf.managed and not ctx.can("admin"):
        f["managed"] = True
    err = _validate(ctx, f)
    if err:
        return _editor(request, db, ctx, wf, err, f)
    _apply(wf, f, ctx, db)
    audit(db, "workflow.updated", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="workflow", entity_id=wf.id)
    db.commit()
    flash(request, "Workflow saved. Changes apply to bookings made from now on.")
    return redirect(f"/app/workflows/{wf.id}")


@router.post("/app/workflows/{wf_id}/toggle", include_in_schema=False)
def workflow_toggle(wf_id: str, request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(feature)):
    wf = _wf(db, ctx, wf_id)
    if not W.can_edit(ctx, wf):
        raise Forbidden("This workflow is managed by your admin.")
    wf.active = not wf.active
    if not wf.active:
        for j in db.scalars(select(NotificationJob).where(NotificationJob.workflow_id == wf.id,
                                                          NotificationJob.status == "scheduled")).all():
            j.status, j.error = "cancelled", "Workflow turned off"
    db.commit()
    flash(request, f"Workflow {'turned on' if wf.active else 'turned off; pending messages cancelled'}.")
    return redirect(request.headers.get("referer") or "/app/workflows")


@router.post("/app/workflows/{wf_id}/delete", include_in_schema=False)
def workflow_delete(wf_id: str, request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(feature)):
    wf = _wf(db, ctx, wf_id)
    if not W.can_edit(ctx, wf):
        raise Forbidden("This workflow is managed by your admin.")
    for j in db.scalars(select(NotificationJob).where(NotificationJob.workflow_id == wf.id,
                                                      NotificationJob.status == "scheduled")).all():
        j.status, j.error = "cancelled", "Workflow deleted"
    audit(db, "workflow.deleted", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="workflow", entity_id=wf.id)
    db.delete(wf)
    db.commit()
    flash(request, "Workflow deleted.")
    return redirect("/app/workflows")


@router.post("/app/workflows/{wf_id}/test", include_in_schema=False)
async def workflow_test(wf_id: str, request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(feature)):
    wf = _wf(db, ctx, wf_id)
    form = await request.form()
    try:
        msg = W.send_test(db, wf, ctx.user, phone=(form.get("phone") or "").strip() or None)
        db.commit()
        flash(request, f"Test {'SMS' if wf.channel == 'sms' else 'email'} sent to {msg.recipient}"
                       f"{' – see /dev/inbox' if msg.provider == 'devinbox' else ''}.")
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        flash(request, f"Test send failed: {exc}", "error")
    return redirect(f"/app/workflows/{wf.id}")


@router.get("/app/workflows/{wf_id}/runs", include_in_schema=False)
def workflow_runs(wf_id: str, request: Request, status: str = "", db: Session = Depends(get_db),
                  ctx: Ctx = Depends(feature)):
    wf = _wf(db, ctx, wf_id)
    q = select(NotificationJob).where(NotificationJob.workflow_id == wf.id)
    if status:
        q = q.where(NotificationJob.status == status)
    jobs = db.scalars(q.order_by(NotificationJob.created_at.desc()).limit(200)).all()
    return render(request, "app/workflows/runs.html", ctx, nav="workflows", jobs=jobs, wf=wf,
                  wf_names={wf.id: wf.name}, status=status)
