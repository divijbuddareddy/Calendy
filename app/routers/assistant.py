"""AI scheduling assistant (PRD 16.1) - /app/assistant.

Every external action (email / booking) needs an explicit "Approve & send" / "Approve & book" POST.
Requests are persisted as AssistantRequest rows: draft -> approved -> executed, or draft -> rejected.
"""
from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import audit
from app.db import get_db
from app.deps import Ctx, Forbidden, flash, redirect, render, require_feature
from app.models import AssistantRequest
from app.services import assistant as svc

router = APIRouter()
feature = require_feature("assistant")

EXAMPLES = [
    "Find 30 minutes next Tuesday with Sarah",
    "Schedule an hour tomorrow afternoon with jordan@acme.com",
    "When did I last meet Sarah?",
    "How many meetings with acme.com this month?",
    "Prep me for my next meeting with Sarah",
]
MAX_THREAD = 20000


def _get(db: Session, ctx: Ctx, req_id: str) -> AssistantRequest:
    r = db.get(AssistantRequest, req_id)
    # least privilege: only the requesting user's own requests in this workspace
    if not r or r.workspace_id != ctx.workspace.id or r.user_id != ctx.user.id:
        raise Forbidden("Assistant request not found")
    return r


def _writer(ctx: Ctx):
    if not ctx.can("member"):
        raise Forbidden("Viewers cannot use the assistant to take actions")


@router.get("/app/assistant")
def index(request: Request, ctx: Ctx = Depends(feature), db: Session = Depends(get_db)):
    recent = db.scalars(
        select(AssistantRequest).where(AssistantRequest.workspace_id == ctx.workspace.id,
                                       AssistantRequest.user_id == ctx.user.id)
        .order_by(AssistantRequest.created_at.desc()).limit(20)
    ).all()
    return render(request, "app/assistant/index.html", ctx, nav="assistant", recent=recent, examples=EXAMPLES,
                  llm_enabled=bool(svc.settings.anthropic_api_key))


@router.post("/app/assistant")
def ask(request: Request, prompt: str = Form(...), ctx: Ctx = Depends(feature), db: Session = Depends(get_db)):
    _writer(ctx)
    prompt = prompt.strip()[:1000]
    if not prompt:
        flash(request, "Type a request first.", "error")
        return redirect("/app/assistant")
    parsed = svc.interpret(db, ctx.user, ctx.workspace.id, prompt)
    req = AssistantRequest(workspace_id=ctx.workspace.id, user_id=ctx.user.id, prompt=prompt, parsed_json=parsed)
    if parsed["intent"] == "history":
        req.proposal_json = {"history": svc.answer_history(db, ctx.user, ctx.workspace.id, prompt)}
        req.status = "executed"  # read-only answer; nothing external happens
        req.executed_at = svc.utcnow()
    elif parsed["intent"] == "prep":
        prep = svc.meeting_prep(db, ctx.user, ctx.workspace.id, prompt)
        req.proposal_json = {"prep": prep}
        if prep.get("brief"):
            # optional agenda email: stays a draft until the user explicitly approves sending it
            req.proposal_json["draft"] = svc.prep_draft(ctx.user, prep["brief"])
        else:
            req.status = "executed"
            req.executed_at = svc.utcnow()
    else:
        req.proposal_json = svc.propose(db, ctx.user, ctx.workspace.id, parsed)
    db.add(req)
    db.commit()
    return redirect(f"/app/assistant/{req.id}")


@router.post("/app/assistant/email")
def from_email(request: Request, thread: str = Form(...), ctx: Ctx = Depends(feature), db: Session = Depends(get_db)):
    """Schedule from email: paste an inbound thread -> extracted request, real slots and a draft reply."""
    _writer(ctx)
    thread = thread.strip()[:MAX_THREAD]
    if not thread:
        flash(request, "Paste an email thread first.", "error")
        return redirect("/app/assistant")
    parsed = svc.interpret_email(db, ctx.user, ctx.workspace.id, thread)
    req = AssistantRequest(workspace_id=ctx.workspace.id, user_id=ctx.user.id, prompt=thread, parsed_json=parsed,
                           proposal_json=svc.propose_email_reply(db, ctx.user, ctx.workspace.id, parsed))
    db.add(req)
    db.commit()
    return redirect(f"/app/assistant/{req.id}")


@router.get("/app/assistant/{req_id}")
def show(req_id: str, request: Request, ctx: Ctx = Depends(feature), db: Session = Depends(get_db)):
    req = _get(db, ctx, req_id)
    return render(request, "app/assistant/show.html", ctx, nav="assistant", req=req,
                  parsed=req.parsed_json or {}, proposal=req.proposal_json or {})


@router.post("/app/assistant/{req_id}/approve")
def approve(
    req_id: str, request: Request, action: str = Form("send"), slot_start: str = Form(""), to: str = Form(""),
    subject: str = Form(""), body: str = Form(""), confirm: str = Form(""), cc: str = Form(None),
    ctx: Ctx = Depends(feature), db: Session = Depends(get_db),
):
    _writer(ctx)
    req = _get(db, ctx, req_id)
    if req.status not in ("draft", "approved"):
        flash(request, f"This request is already {req.status}.", "error")
        return redirect(f"/app/assistant/{req.id}")
    if confirm != "yes":
        flash(request, "Tick the confirmation box to approve this action.", "error")
        return redirect(f"/app/assistant/{req.id}")
    req.status = "approved"
    db.flush()
    html = None
    if body.strip():
        from markupsafe import escape

        html = "".join(f"<p>{escape(p)}</p>" for p in body.strip().split("\n\n"))
    try:
        cc_list = None if cc is None else [c.strip().lower() for c in cc.replace(";", ",").split(",") if c.strip()]
        msg = svc.execute(db, req, ctx.user, action, slot_start or None, to or None, subject or None, html, cc_list)
        audit(db, f"assistant.{action}", workspace_id=ctx.workspace.id, actor_id=ctx.user.id,
              entity_type="assistant_request", entity_id=req.id)
        db.commit()
        flash(request, msg)
    except Exception as exc:
        db.rollback()
        req = _get(db, ctx, req_id)
        req.status = "approved"
        db.commit()
        flash(request, f"Not executed: {exc}", "error")
    return redirect(f"/app/assistant/{req.id}")


@router.post("/app/assistant/{req_id}/reject")
def reject(req_id: str, request: Request, ctx: Ctx = Depends(feature), db: Session = Depends(get_db)):
    _writer(ctx)
    req = _get(db, ctx, req_id)
    if req.status in ("draft", "approved"):
        req.status = "rejected"
        db.commit()
        flash(request, "Suggestion rejected. Nothing was sent.", "info")
    return redirect("/app/assistant")
