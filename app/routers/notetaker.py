"""Notetaker / meeting recaps (PRD 16.2) - /app/recaps and public /recap/{share_token}."""
import datetime as dt
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app import ratelimit
from app.audit import audit
from app.db import get_db
from app.deps import Ctx, Forbidden, flash, redirect, render, require_feature
from app.entitlements import has_feature
from app.models import Booking, BookingHost, Invitee, MeetingRecap, Workspace, utcnow
from app.security import hash_password, opaque_token, verify_password
from app.services import notetaker as svc  # registers @periodic("recap_retention")

router = APIRouter()
feature = require_feature("notetaker")
MAX_UPLOAD = 2 * 1024 * 1024


def _get(db: Session, ctx: Ctx, recap_id: str) -> MeetingRecap:
    r = db.get(MeetingRecap, recap_id)
    if not r or r.workspace_id != ctx.workspace.id or r.deleted_at:
        raise HTTPException(404, "Recap not found")
    if r.owner_user_id != ctx.user.id and not ctx.can("admin"):
        raise Forbidden("Only the recap owner or an admin can open this recap")
    return r


def _writable(ctx: Ctx, r: MeetingRecap):
    if not ctx.can("member") or (r.owner_user_id != ctx.user.id and not ctx.can("admin")):
        raise Forbidden("You cannot modify this recap")


def _my_bookings(db: Session, ctx: Ctx):
    now = utcnow()
    return db.scalars(
        select(Booking).where(
            Booking.workspace_id == ctx.workspace.id, Booking.status == "confirmed",
            Booking.start_at >= now - dt.timedelta(days=14), Booking.start_at <= now + dt.timedelta(days=14),
            or_(Booking.host_id == ctx.user.id,
                Booking.id.in_(select(BookingHost.booking_id).where(BookingHost.user_id == ctx.user.id))),
        ).order_by(Booking.start_at)
    ).all()


@router.get("/app/recaps")
def index(request: Request, q: str = "", ctx: Ctx = Depends(feature), db: Session = Depends(get_db)):
    stmt = select(MeetingRecap).where(MeetingRecap.workspace_id == ctx.workspace.id, MeetingRecap.deleted_at.is_(None))
    if not ctx.can("admin"):
        stmt = stmt.where(MeetingRecap.owner_user_id == ctx.user.id)
    recaps = db.scalars(stmt.order_by(MeetingRecap.created_at.desc())).all()
    recap_by_booking = {r.booking_id: r for r in recaps if r.booking_id}
    bookings = _my_bookings(db, ctx)
    results = svc.search(db, ctx.workspace.id, q, None if ctx.can("admin") else ctx.user.id) if q else None
    return render(request, "app/recaps/index.html", ctx, nav="recaps", recaps=recaps, bookings=bookings,
                  recap_by_booking=recap_by_booking, q=q, results=results)


@router.post("/app/recaps/new")
def create(
    request: Request, booking_id: str = Form(""), title: str = Form(""), consent: str = Form(""),
    meeting_url: str = Form(""), ctx: Ctx = Depends(feature), db: Session = Depends(get_db),
):
    if not ctx.can("member"):
        raise Forbidden("Viewers cannot add a notetaker")
    if consent != "yes":
        flash(request, "Confirm that participants have been notified and consent to recording.", "error")
        return redirect("/app/recaps")
    booking = None
    if booking_id:
        booking = db.get(Booking, booking_id)
        if not booking or booking.workspace_id != ctx.workspace.id:
            raise HTTPException(404, "Booking not found")
        existing = db.scalar(select(MeetingRecap).where(MeetingRecap.booking_id == booking.id,
                                                       MeetingRecap.deleted_at.is_(None)))
        if existing:
            return redirect(f"/app/recaps/{existing.id}")
        if not title:
            snap = booking.snapshot_json or {}
            inv = booking.invitees[0].name if booking.invitees else ""
            title = f"{snap.get('name') or 'Meeting'}{' with ' + inv if inv else ''}"
    r = MeetingRecap(
        workspace_id=ctx.workspace.id, booking_id=booking.id if booking else None, owner_user_id=ctx.user.id,
        title=(title or "Untitled meeting")[:300], consent_confirmed=True,
        recording_status="joining" if booking else "none", retention_until=svc.default_retention(),
    )
    db.add(r)
    db.flush()
    meeting_url = meeting_url.strip()
    if meeting_url and not meeting_url.startswith("https://"):
        meeting_url = ""
    bot = svc.add_bot(db, r, booking, ctx.workspace, ctx.user.name or ctx.user.email, meeting_url or None) if booking else None
    audit(db, "notetaker.added", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="recap", entity_id=r.id,
          metadata={"provider": bot.provider if bot else None, "consent_notices": bot.consent_recipients if bot else 0})
    db.commit()
    if bot and bot.status == "failed":
        flash(request, f"Recap created, but the bot could not join: {bot.last_error}", "error")
    elif bot:
        flash(request, f"{bot.bot_name} added and {bot.consent_recipients} participant(s) emailed a recording notice. "
                       "Start recording when the meeting begins.")
    else:
        flash(request, "Recap created.")
    return redirect(f"/app/recaps/{r.id}")


@router.get("/app/recaps/{recap_id}")
def show(recap_id: str, request: Request, ask: str = "", ctx: Ctx = Depends(feature), db: Session = Depends(get_db)):
    r = _get(db, ctx, recap_id)
    transcript = svc.get_transcript(r)
    answer = svc.ask(transcript, ask) if ask else None
    booking = db.get(Booking, r.booking_id) if r.booking_id else None
    from app.config import settings

    share_url = f"{settings.base_url}/recap/{r.share_token}" if r.share_token else None
    bot = svc.get_bot(db, r)
    actions_text = "\n".join(
        " | ".join([a.get("text", ""), a.get("owner") or "", a.get("due") or ""]) for a in (r.action_items_json or [])
    )
    return render(request, "app/recaps/show.html", ctx, nav="recaps", r=r, transcript=transcript, ask=ask,
                  answer=answer, booking=booking, share_url=share_url, actions_text=actions_text, bot=bot,
                  bot_name=bot.bot_name if bot else svc.bot_name_for(ctx.workspace),
                  retention_days=max(0, (r.retention_until - utcnow()).days) if r.retention_until else None)


@router.post("/app/recaps/{recap_id}/bot")
def bot(recap_id: str, request: Request, action: str = Form(...), ctx: Ctx = Depends(feature),
        db: Session = Depends(get_db)):
    r = _get(db, ctx, recap_id)
    _writable(ctx, r)
    from app.services.recording import RecordingError

    if action not in ("start", "stop", "remove", "sync"):
        raise HTTPException(400, "Unknown action")
    booking = db.get(Booking, r.booking_id) if r.booking_id else None
    b = svc.get_bot(db, r)
    try:
        if action == "start":
            if not r.consent_confirmed:
                flash(request, "Consent confirmation is required before recording.", "error")
                return redirect(f"/app/recaps/{r.id}")
            if b and b.status == "removed":
                flash(request, "The bot has been removed from this meeting.", "error")
                return redirect(f"/app/recaps/{r.id}")
            if r.recording_status in ("none", "joining", "stopped"):
                if not b and booking:  # recap created before bots existed
                    b = svc.add_bot(db, r, booking, ctx.workspace, ctx.user.name or ctx.user.email)
                elif b and not b.consent_sent_at:
                    svc.send_consent_notice(db, r, b, booking, ctx.user.name or ctx.user.email, ctx.workspace)
                svc.bot_action(db, r, b, "start")
                flash(request, "Recording started. The recording indicator is shown to everyone in the meeting"
                               + (f" ({b.consent_recipients} participant(s) were notified by email)." if b else "."))
        elif action == "sync":
            if b:
                svc.sync_bot(db, b)
            flash(request, "Bot status refreshed.", "info")
        else:
            svc.bot_action(db, r, b, action)
            flash(request, "Bot removed from the meeting." if action == "remove" else "Recording stopped.")
    except RecordingError as exc:
        db.rollback()
        flash(request, f"Recording provider error: {exc}", "error")
        return redirect(f"/app/recaps/{r.id}")
    audit(db, f"notetaker.{action}", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="recap", entity_id=r.id)
    db.commit()
    return redirect(f"/app/recaps/{r.id}")


@router.post("/app/recaps/{recap_id}/transcript")
async def upload_transcript(
    recap_id: str, request: Request, text: str = Form(""), file: Optional[UploadFile] = File(None),
    ctx: Ctx = Depends(feature), db: Session = Depends(get_db),
):
    r = _get(db, ctx, recap_id)
    _writable(ctx, r)
    content = text
    if file is not None and file.filename:
        if not file.filename.lower().endswith((".txt", ".vtt")):
            flash(request, "Upload a .txt or .vtt file.", "error")
            return redirect(f"/app/recaps/{r.id}")
        raw = await file.read(MAX_UPLOAD + 1)
        if len(raw) > MAX_UPLOAD:
            flash(request, "Transcript file too large (2 MB max).", "error")
            return redirect(f"/app/recaps/{r.id}")
        content = raw.decode("utf-8", "ignore")
    content = svc.normalize_transcript(content)
    if not content.strip():
        flash(request, "Transcript is empty.", "error")
        return redirect(f"/app/recaps/{r.id}")
    svc.set_transcript(r, content)
    if r.recording_status in ("none", "joining", "recording"):
        r.recording_status = "stopped"
    svc.apply_recap(r)
    db.commit()
    flash(request, "Transcript saved (encrypted) and recap generated.")
    return redirect(f"/app/recaps/{r.id}")


@router.post("/app/recaps/{recap_id}/generate")
def regenerate(recap_id: str, request: Request, ctx: Ctx = Depends(feature), db: Session = Depends(get_db)):
    r = _get(db, ctx, recap_id)
    _writable(ctx, r)
    if not r.transcript:
        flash(request, "Add a transcript first.", "error")
    else:
        svc.apply_recap(r)
        db.commit()
        flash(request, "Recap regenerated.")
    return redirect(f"/app/recaps/{r.id}")


@router.post("/app/recaps/{recap_id}/edit")
def edit(
    recap_id: str, request: Request, title: str = Form(...), summary: str = Form(""), decisions: str = Form(""),
    actions: str = Form(""), retention_days: int = Form(90), ctx: Ctx = Depends(feature), db: Session = Depends(get_db),
):
    r = _get(db, ctx, recap_id)
    _writable(ctx, r)
    r.title = title.strip()[:300] or r.title
    r.summary = summary.strip()
    r.decisions_json = [d.strip() for d in decisions.splitlines() if d.strip()]
    items = []
    for line in actions.splitlines():
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split("|")] + ["", ""]
        items.append({"text": parts[0], "owner": parts[1] or None, "due": parts[2] or None})
    r.action_items_json = items
    r.retention_until = utcnow() + dt.timedelta(days=max(1, min(retention_days, 3650)))
    db.commit()
    flash(request, "Recap updated.")
    return redirect(f"/app/recaps/{r.id}")


@router.post("/app/recaps/{recap_id}/share")
def share(recap_id: str, request: Request, enabled: str = Form("yes"), password: str = Form(""),
          ctx: Ctx = Depends(feature), db: Session = Depends(get_db)):
    r = _get(db, ctx, recap_id)
    _writable(ctx, r)
    if enabled == "yes":
        r.share_token = r.share_token or opaque_token(18)
        r.share_password_hash = hash_password(password) if password else None
        flash(request, "Share link ready" + (" (password protected)." if password else "."))
    else:
        r.share_token = None
        r.share_password_hash = None
        flash(request, "Sharing disabled.", "info")
    audit(db, "recap.share", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="recap", entity_id=r.id,
          metadata={"enabled": enabled == "yes", "password": bool(password)})
    db.commit()
    return redirect(f"/app/recaps/{r.id}")


@router.post("/app/recaps/{recap_id}/delete")
def delete(recap_id: str, request: Request, ctx: Ctx = Depends(feature), db: Session = Depends(get_db)):
    r = _get(db, ctx, recap_id)
    _writable(ctx, r)
    svc.wipe(r)
    audit(db, "recap.deleted", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="recap", entity_id=r.id)
    db.commit()
    flash(request, "Recap and transcript deleted.")
    return redirect("/app/recaps")


# ---------------------------------------------------------------- public share
def _public(db: Session, token: str) -> MeetingRecap:
    r = db.scalar(select(MeetingRecap).where(MeetingRecap.share_token == token, MeetingRecap.deleted_at.is_(None)))
    if not r:
        raise HTTPException(404, "This recap link is invalid or has been disabled.")
    ws = db.get(Workspace, r.workspace_id)
    if not ws or not has_feature(ws, "notetaker"):
        raise HTTPException(404, "This recap is not available.")
    return r


@router.get("/recap/{token}")
def public_recap(token: str, request: Request, db: Session = Depends(get_db)):
    ratelimit.enforce(request, "recap_view", limit=60, window_seconds=60)
    r = _public(db, token)
    unlocked = not r.share_password_hash or request.session.get(f"recap_ok_{r.id}") == r.share_password_hash[-12:]
    return render(request, "recap_public/show.html", r=r if unlocked else None, locked=not unlocked, token=token,
                  title=r.title)


@router.post("/recap/{token}")
def public_recap_unlock(token: str, request: Request, password: str = Form(""), db: Session = Depends(get_db)):
    ratelimit.enforce(request, "recap_unlock", limit=10, window_seconds=300)
    r = _public(db, token)
    if r.share_password_hash and verify_password(password, r.share_password_hash):
        request.session[f"recap_ok_{r.id}"] = r.share_password_hash[-12:]
        return redirect(f"/recap/{token}")
    return render(request, "recap_public/show.html", r=None, locked=True, token=token, title=r.title,
                  error="Incorrect password.", status_code=403)
