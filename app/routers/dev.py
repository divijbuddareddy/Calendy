"""Developer tools: email/SMS dev inbox and worker trigger.

Hidden (404) when ENV=prod unless the user is a platform admin. The inbox is tenant scoped:
users see only messages for their current workspace's members and its bookings' invitees;
platform admins see everything."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.deps import Ctx, LoginRequired, flash, optional_ctx, redirect, render
from app.models import Booking, Invitee, NotificationJob, SentMessage, User, WorkspaceMember

router = APIRouter(include_in_schema=False)


def dev_access(request: Request, ctx: Optional[Ctx] = Depends(optional_ctx)) -> Ctx:
    """Dev tools need a signed-in user. In prod they are hidden (404) unless platform admin."""
    if settings.env == "prod" and not (ctx and ctx.user.is_platform_admin):
        raise HTTPException(404, "Not found")
    if not ctx:
        raise LoginRequired()
    request.state.ctx = ctx
    return ctx


def _scope_filter(db: Session, ctx: Ctx):
    """Messages visible to this user: platform admins see everything; others only messages
    addressed to the current workspace's members or to invitees (email/phone) of its bookings,
    or produced by a notification job of the workspace."""
    if ctx.user.is_platform_admin:
        return None
    wsid = ctx.workspace.id
    member_emails = select(func.lower(User.email)).join(WorkspaceMember, WorkspaceMember.user_id == User.id).where(
        WorkspaceMember.workspace_id == wsid)
    invite_emails = select(func.lower(WorkspaceMember.invite_email)).where(
        WorkspaceMember.workspace_id == wsid, WorkspaceMember.invite_email.is_not(None))
    inv_emails = select(func.lower(Invitee.email)).join(Booking, Booking.id == Invitee.booking_id).where(
        Booking.workspace_id == wsid)
    inv_phones = select(Invitee.phone).join(Booking, Booking.id == Invitee.booking_id).where(
        Booking.workspace_id == wsid, Invitee.phone.is_not(None))
    ws_jobs = select(NotificationJob.id).where(NotificationJob.workspace_id == wsid)
    rcpt = func.lower(SentMessage.recipient)
    return or_(rcpt.in_(member_emails), rcpt.in_(invite_emails), rcpt.in_(inv_emails),
               SentMessage.recipient.in_(inv_phones), SentMessage.job_id.in_(ws_jobs))


def _scoped(db: Session, ctx: Ctx, query):
    f = _scope_filter(db, ctx)
    return query if f is None else query.where(f)


def _visible_message(db: Session, ctx: Ctx, msg_id: str) -> SentMessage:
    m = db.scalar(_scoped(db, ctx, select(SentMessage).where(SentMessage.id == msg_id)))
    if not m:
        raise HTTPException(404)
    return m


@router.get("/dev/inbox")
def inbox(request: Request, q: str = "", channel: str = "", db: Session = Depends(get_db),
          ctx: Ctx = Depends(dev_access)):
    query = _scoped(db, ctx, select(SentMessage))
    if q:
        like = f"%{q.lower()}%"
        query = query.where(func.lower(SentMessage.recipient).like(like) | func.lower(SentMessage.subject).like(like))
    if channel:
        query = query.where(SentMessage.channel == channel)
    msgs = db.scalars(query.order_by(SentMessage.created_at.desc()).limit(200)).all()
    sel_id = request.query_params.get("id") or (msgs[0].id if msgs else None)
    selected = db.scalar(_scoped(db, ctx, select(SentMessage).where(SentMessage.id == sel_id))) if sel_id else None
    total = db.scalar(_scoped(db, ctx, select(func.count(SentMessage.id)))) or 0
    return render(request, "dev/inbox.html", ctx, msgs=msgs, selected=selected, q=q, channel=channel, total=total,
                  nav="")


@router.get("/dev/inbox/{msg_id}/raw")
def inbox_raw(msg_id: str, db: Session = Depends(get_db), ctx: Ctx = Depends(dev_access)):
    m = _visible_message(db, ctx, msg_id)
    if m.channel == "sms":
        from markupsafe import escape

        return HTMLResponse(f"<pre style='font:15px system-ui;white-space:pre-wrap;padding:16px'>{escape(m.body or '')}</pre>",
                            headers={"Content-Security-Policy": "script-src 'none'"})
    return HTMLResponse(m.body or "", headers={"Content-Security-Policy": "script-src 'none'; frame-ancestors 'self'"})


@router.get("/dev/inbox/{msg_id}/attachments/{idx}")
def inbox_attachment(msg_id: str, idx: int, db: Session = Depends(get_db), ctx: Ctx = Depends(dev_access)):
    m = _visible_message(db, ctx, msg_id)
    atts = m.attachments_json or []
    if idx < 0 or idx >= len(atts):
        raise HTTPException(404)
    a = atts[idx]
    fname = "".join(ch for ch in a.get("filename", "attachment.ics") if ch.isalnum() or ch in "._-") or "attachment"
    return Response(a.get("content", ""), media_type=a.get("mime", "text/calendar"),
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.post("/dev/inbox/clear")
def inbox_clear(request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(dev_access)):
    ids = db.scalars(_scoped(db, ctx, select(SentMessage.id))).all()
    if ids:
        db.execute(delete(SentMessage).where(SentMessage.id.in_(ids)))
    db.commit()
    flash(request, "Dev inbox cleared.")
    return redirect("/dev/inbox")


@router.post("/dev/run-worker")
def run_worker(request: Request, ctx: Ctx = Depends(dev_access)):
    from app.worker import TASKS, run_all_once

    run_all_once()
    flash(request, f"Ran {len(TASKS)} background tasks twice (outbox, notifications, webhooks, calendar sync, ...).")
    return redirect(request.headers.get("referer") or "/dev/inbox")
