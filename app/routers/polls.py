"""Meeting polls (PRD 6.2, P3) - /app/polls and public /poll/{public_token}.

Host proposes candidate times; invitees vote (multiple options); host confirms the winner, which creates a
hidden one-off event type + booking and emails every voter the final time.
"""
import datetime as dt
import logging
import re
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import ratelimit
from app.audit import audit
from app.db import get_db
from app.deps import Ctx, Forbidden, _fmt_dt, flash, redirect, render, require_feature
from app.entitlements import has_feature
from app.events import publish
from app.models import (
    Booking, BookingHost, EventType, Invitee, MeetingPoll, PollVote, User, Workspace, utcnow,
)
from app.security import opaque_token
from app.services.bootstrap import slugify

router = APIRouter()
feature = require_feature("polls")
log = logging.getLogger("polls")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
LOCATION_TYPES = [("google_meet", "Google Meet"), ("zoom", "Zoom"), ("teams", "Microsoft Teams"), ("phone", "Phone call"),
                  ("in_person", "In person"), ("custom_url", "Custom link"), ("host_provides", "Details to follow")]


def _z(name):
    try:
        return ZoneInfo(name or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def local_to_utc(value: str, tz: str) -> dt.datetime:
    local = dt.datetime.fromisoformat(value.strip())
    return local.replace(tzinfo=_z(tz)).astimezone(dt.timezone.utc).replace(tzinfo=None)


def tally(poll: MeetingPoll, votes: list) -> list[dict]:
    """Per option: vote count + voter names, sorted in option order. Pure function."""
    out = []
    for o in poll.options_json or []:
        voters = [v for v in votes if o["id"] in (v.option_ids_json or [])]
        out.append({"option": o, "count": len(voters), "voters": [v.name for v in voters],
                    "start": dt.datetime.fromisoformat(o["start_at"])})
    best = max((t["count"] for t in out), default=0)
    for t in out:
        t["best"] = best > 0 and t["count"] == best
    return out


def _get(db: Session, ctx: Ctx, poll_id: str) -> MeetingPoll:
    p = db.get(MeetingPoll, poll_id)
    if not p or p.workspace_id != ctx.workspace.id:
        raise HTTPException(404, "Poll not found")
    if p.owner_user_id != ctx.user.id and not ctx.can("admin"):
        raise Forbidden("Only the poll owner or an admin can manage this poll")
    return p


def _writer(ctx: Ctx):
    if not ctx.can("member"):
        raise Forbidden("Viewers cannot create or change polls")


@router.get("/app/polls")
def index(request: Request, ctx: Ctx = Depends(feature), db: Session = Depends(get_db)):
    stmt = select(MeetingPoll).where(MeetingPoll.workspace_id == ctx.workspace.id)
    if not ctx.can("admin"):
        stmt = stmt.where(MeetingPoll.owner_user_id == ctx.user.id)
    polls = db.scalars(stmt.order_by(MeetingPoll.created_at.desc())).all()
    counts = dict(db.execute(
        select(PollVote.poll_id, func.count()).where(PollVote.poll_id.in_([p.id for p in polls] or [""]))
        .group_by(PollVote.poll_id)).all())
    return render(request, "app/polls/index.html", ctx, nav="polls", polls=polls, counts=counts,
                  location_types=LOCATION_TYPES)


@router.post("/app/polls")
async def create(request: Request, ctx: Ctx = Depends(feature), db: Session = Depends(get_db)):
    _writer(ctx)
    form = await request.form()
    title = (form.get("title") or "").strip()[:300]
    try:
        duration = int(form.get("duration") or 30)
    except ValueError:
        duration = 0
    tz = ctx.user.default_timezone or "UTC"
    starts = []
    for raw in form.getlist("times"):
        if raw and str(raw).strip():
            try:
                starts.append(local_to_utc(str(raw), tz))
            except ValueError:
                flash(request, f"Invalid date/time: {raw}", "error")
                return redirect("/app/polls")
    starts = sorted(set(s for s in starts if s > utcnow()))
    if not title or not (5 <= duration <= 720):
        flash(request, "Give the poll a title and a duration between 5 and 720 minutes.", "error")
        return redirect("/app/polls")
    if len(starts) < 2:
        flash(request, "Add at least two future candidate times.", "error")
        return redirect("/app/polls")
    loc_type = form.get("location_type") or "host_provides"
    loc = {"type": loc_type if loc_type in dict(LOCATION_TYPES) else "host_provides",
           "value": (form.get("location_value") or "").strip()[:500]}
    poll = MeetingPoll(
        workspace_id=ctx.workspace.id, owner_user_id=ctx.user.id, title=title,
        description=(form.get("description") or "").strip()[:3000] or None, duration=duration, location_json=loc,
        options_json=[{"id": f"o{i + 1}", "start_at": s.isoformat()} for i, s in enumerate(starts[:20])],
        public_token=opaque_token(16),
    )
    db.add(poll)
    db.commit()
    flash(request, "Poll created. Share the link with participants.")
    return redirect(f"/app/polls/{poll.id}")


@router.get("/app/polls/{poll_id}")
def show(poll_id: str, request: Request, ctx: Ctx = Depends(feature), db: Session = Depends(get_db)):
    poll = _get(db, ctx, poll_id)
    votes = db.scalars(select(PollVote).where(PollVote.poll_id == poll.id).order_by(PollVote.created_at)).all()
    from app.config import settings

    return render(request, "app/polls/show.html", ctx, nav="polls", poll=poll, votes=votes, tallies=tally(poll, votes),
                  public_url=f"{settings.base_url}/poll/{poll.public_token}", location_types=dict(LOCATION_TYPES))


@router.post("/app/polls/{poll_id}/status")
def set_status(poll_id: str, request: Request, status: str = Form(...), ctx: Ctx = Depends(feature),
               db: Session = Depends(get_db)):
    _writer(ctx)
    poll = _get(db, ctx, poll_id)
    if poll.status == "booked":
        flash(request, "This poll is already booked.", "error")
    elif status in ("open", "closed"):
        poll.status = status
        db.commit()
        flash(request, "Voting closed." if status == "closed" else "Voting reopened.")
    return redirect(f"/app/polls/{poll.id}")


@router.post("/app/polls/{poll_id}/delete")
def delete(poll_id: str, request: Request, ctx: Ctx = Depends(feature), db: Session = Depends(get_db)):
    _writer(ctx)
    poll = _get(db, ctx, poll_id)
    db.delete(poll)
    db.commit()
    flash(request, "Poll deleted.")
    return redirect("/app/polls")


def _poll_event_type(db: Session, poll: MeetingPoll, start: dt.datetime) -> EventType:
    slug = slugify(f"poll-{poll.public_token[:8]}")
    et = db.scalar(select(EventType).where(EventType.owner_user_id == poll.owner_user_id, EventType.slug == slug))
    if not et:
        et = EventType(
            workspace_id=poll.workspace_id, owner_user_id=poll.owner_user_id, name=poll.title[:200], slug=slug,
            description=poll.description, duration=poll.duration, location_json=[poll.location_json] if poll.location_json else None,
            visibility="private", is_one_off=True, min_notice_minutes=0, max_days_ahead=3650,
        )
        db.add(et)
    et.one_off_slots_json = [start.isoformat()]
    db.flush()
    return et


def confirm_poll(db: Session, poll: MeetingPoll, option_id: str, vote_id: Optional[str], actor: str) -> Booking:
    """Create the booking for the chosen option and email all voters. Caller commits."""
    opt = next((o for o in poll.options_json or [] if o["id"] == option_id), None)
    if not opt:
        raise ValueError("Unknown option")
    votes = db.scalars(select(PollVote).where(PollVote.poll_id == poll.id).order_by(PollVote.created_at)).all()
    if not votes:
        raise ValueError("Nobody has voted yet.")
    chosen = next((v for v in votes if v.id == vote_id), None) if vote_id else None
    chosen = chosen or next((v for v in votes if option_id in (v.option_ids_json or [])), None) or votes[0]
    start = dt.datetime.fromisoformat(opt["start_at"])
    owner = db.get(User, poll.owner_user_id)
    et = _poll_event_type(db, poll, start)
    guests = sorted({v.email for v in votes if v.id != chosen.id})
    invitee = {"name": chosen.name, "email": chosen.email, "phone": None, "company": None,
               "timezone": owner.default_timezone, "guests": guests[:10]}
    db.commit()  # persist the one-off event type; booking transactions lock hosts as their first statement
    from app.errors import APIError
    from app.services.booking import create_booking

    try:
        # Goes through the full availability + double-booking checks; never bypass them.
        booking = create_booking(
            db, et, start, invitee, {}, timezone=owner.default_timezone, duration=poll.duration,
            idempotency_key=f"poll:{poll.id}:{option_id}", source="poll", actor=actor,
        )
    except APIError as exc:
        db.rollback()
        log.info("poll %s option %s could not be booked: %s", poll.id, option_id, exc.message)
        raise ValueError(f"That time can no longer be booked ({exc.message}). Pick another option.") from exc
    et.used_at = utcnow()
    poll.status = "booked"
    poll.chosen_option_id = option_id
    poll.booking_id = booking.id
    from app.services.messaging import send_email

    loc = poll.location_json or {}
    loc_txt = loc.get("value") or dict(LOCATION_TYPES).get(loc.get("type"), "")
    for v in votes:
        when = _fmt_dt(start, owner.default_timezone)
        send_email(db, v.email, f"Confirmed: {poll.title}",
                   f"<p>Hi {v.name},</p><p>{owner.name or owner.email} picked a time for <strong>{poll.title}</strong>:</p>"
                   f"<p><strong>{when}</strong> ({poll.duration} min)</p>"
                   + (f"<p>Location: {loc_txt}</p>" if loc_txt else "")
                   + "<p>Thanks for voting!</p>")
    db.flush()
    return booking


@router.post("/app/polls/{poll_id}/confirm")
def confirm(poll_id: str, request: Request, option_id: str = Form(...), vote_id: str = Form(""),
            ctx: Ctx = Depends(feature), db: Session = Depends(get_db)):
    _writer(ctx)
    poll = _get(db, ctx, poll_id)
    if poll.status == "booked":
        flash(request, "This poll has already been booked.", "error")
        return redirect(f"/app/polls/{poll.id}")
    try:
        b = confirm_poll(db, poll, option_id, vote_id or None, f"user:{ctx.user.id}")
        audit(db, "poll.booked", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="poll",
              entity_id=poll.id, metadata={"booking_id": b.id, "option_id": option_id})
        db.commit()
        flash(request, "Meeting booked and all voters notified.")
    except (ValueError, IntegrityError) as exc:
        db.rollback()
        msg = "That time conflicts with an existing booking." if isinstance(exc, IntegrityError) else str(exc)
        flash(request, msg, "error")
    return redirect(f"/app/polls/{poll_id}")


# ---------------------------------------------------------------- public voting
def _public(db: Session, token: str) -> MeetingPoll:
    p = db.scalar(select(MeetingPoll).where(MeetingPoll.public_token == token))
    if not p:
        raise HTTPException(404, "This poll link is invalid.")
    ws = db.get(Workspace, p.workspace_id)
    if not ws or not has_feature(ws, "polls"):
        raise HTTPException(404, "This poll is not available.")
    return p


def _public_ctx(db: Session, poll: MeetingPoll):
    owner = db.get(User, poll.owner_user_id)
    votes = db.scalars(select(PollVote).where(PollVote.poll_id == poll.id)).all()
    return {"poll": poll, "owner": owner, "tallies": tally(poll, votes), "host_tz": owner.default_timezone,
            "location_types": dict(LOCATION_TYPES), "title": poll.title, "vote_count": len(votes)}


@router.get("/poll/{token}")
def public_poll(token: str, request: Request, db: Session = Depends(get_db)):
    ratelimit.enforce(request, "poll_view", limit=60, window_seconds=60)
    poll = _public(db, token)
    return render(request, "poll_public/show.html", **_public_ctx(db, poll),
                  voted=request.session.get(f"poll_voted_{poll.id}"))


@router.post("/poll/{token}")
async def public_vote(token: str, request: Request, db: Session = Depends(get_db)):
    ratelimit.enforce(request, "poll_vote", limit=10, window_seconds=60)
    poll = _public(db, token)
    form = await request.form()
    if (form.get("website") or "").strip():  # honeypot: silently accept, store nothing
        return redirect(f"/poll/{token}")
    data = _public_ctx(db, poll)
    name = (form.get("name") or "").strip()[:200]
    email = (form.get("email") or "").strip().lower()[:320]
    valid_ids = {o["id"] for o in poll.options_json or []}
    picked = [i for i in form.getlist("options") if i in valid_ids]
    error = None
    if poll.status != "open":
        error = "Voting is closed for this poll."
    elif not name or not EMAIL_RE.match(email):
        error = "Please enter your name and a valid email."
    elif not picked:
        error = "Select at least one time that works for you."
    if error:
        return render(request, "poll_public/show.html", **data, error=error, form_name=name, form_email=email,
                      status_code=400)
    vote = db.scalar(select(PollVote).where(PollVote.poll_id == poll.id, func.lower(PollVote.email) == email))
    if vote:
        vote.name, vote.option_ids_json = name, picked
    else:
        db.add(PollVote(poll_id=poll.id, name=name, email=email, option_ids_json=picked))
    db.commit()
    request.session[f"poll_voted_{poll.id}"] = True
    return redirect(f"/poll/{token}?voted=1")
