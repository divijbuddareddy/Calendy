"""Workflow rule engine (PRD 11.2) - deterministic, no AI.

A workflow = trigger + timing offset + channel + audience + templates + active flag, optionally
restricted to event types (conditions_json.event_type_ids). Matching bookings produce
NotificationJob rows (dedupe key per workflow/booking/trigger/recipient) that the notifications
sender delivers. Quiet hours (Workspace.settings_json.quiet_hours) defer sends.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Booking,
    EventType,
    NotificationJob,
    RoutingForm,
    RoutingSubmission,
    User,
    Workflow,
    Workspace,
    utcnow,
)
from app.security import hash_secret
from app.services import notifications as N

TRIGGERS = {
    "invitee_created": "When an event is booked",
    "before_event": "Before event starts",
    "after_event": "After event ends",
    "invitee_canceled": "When an event is canceled",
    "rescheduled": "When an event is rescheduled",
    "no_show": "When host marks a no-show",
    "routing_submitted": "When a routing form is submitted",
}
TIMED_TRIGGERS = {"before_event", "after_event"}
VARIABLES = [
    "invitee_name", "host_name", "event_name", "start_time", "timezone", "location", "join_link",
    "reschedule_url", "cancel_url",
]
EXTRA_VARIABLES = ["invitee_email", "end_time", "start_date", "duration"]

SAMPLE_VARS = {
    "invitee_name": "Alex Example", "invitee_email": "alex@example.com", "host_name": "Jordan Host",
    "event_name": "Discovery call", "start_time": "Wednesday, 18 November 2026 · 14:00 GMT",
    "start_date": "Wednesday, 18 November 2026", "end_time": "14:30 GMT", "timezone": "Europe/London",
    "location": "Google Meet: https://meet.google.com/abc-defg-hij", "join_link": "https://meet.google.com/abc-defg-hij",
    "reschedule_url": "https://example.com/booking/tok/reschedule", "cancel_url": "https://example.com/booking/tok/cancel",
    "duration": "30",
}

DEFAULT_TEMPLATES = [
    {
        "key": "reminder_24h", "name": "Email reminder – 24 hours before", "trigger": "before_event",
        "offset_minutes": 1440, "channel": "email", "audience": "invitee",
        "subject_template": "Reminder: {event_name} with {host_name} tomorrow",
        "body_template": "Hi {invitee_name},\n\nThis is a friendly reminder that your {event_name} with {host_name} "
                         "is coming up on {start_time} ({timezone}).\n\nWhere: {location}\n\n"
                         "Need to make a change? Reschedule: {reschedule_url}\nCancel: {cancel_url}",
    },
    {
        "key": "reminder_1h", "name": "Email reminder – 1 hour before", "trigger": "before_event",
        "offset_minutes": 60, "channel": "email", "audience": "invitee",
        "subject_template": "Starting soon: {event_name} at {start_time}",
        "body_template": "Hi {invitee_name},\n\n{event_name} with {host_name} starts in one hour ({start_time}).\n\n"
                         "Join: {join_link}\nLocation: {location}",
    },
    {
        "key": "thank_you", "name": "Thank-you follow-up – 1 hour after", "trigger": "after_event",
        "offset_minutes": 60, "channel": "email", "audience": "invitee",
        "subject_template": "Thanks for meeting, {invitee_name}",
        "body_template": "Hi {invitee_name},\n\nThank you for taking the time to meet with {host_name} for {event_name}. "
                         "If you'd like to talk again, just book another time.\n\nBest,\n{host_name}",
    },
    {
        "key": "sms_reminder", "name": "SMS reminder – 1 hour before (requires consent)", "trigger": "before_event",
        "offset_minutes": 60, "channel": "sms", "audience": "invitee", "subject_template": "",
        "body_template": "Reminder: {event_name} with {host_name} at {start_time}. Reply STOP to opt out.",
    },
]

_VAR_RE = re.compile(r"\{(\w+)\}")


def render_template(text: Optional[str], variables: dict) -> str:
    """Replace {known_variable} placeholders; unknown placeholders are left untouched."""
    if not text:
        return ""
    return _VAR_RE.sub(lambda m: str(variables[m.group(1)]) if m.group(1) in variables else m.group(0), text)


def booking_variables(db: Session, booking: Booking, tz: str) -> dict:
    hosts = N.hosts_of(db, booking)
    inv = (booking.invitees or [None])[0]
    links = N.manage_links(booking)
    loc = booking.location_json or {}
    return {
        "invitee_name": inv.name if inv else "",
        "invitee_email": inv.email if inv else "",
        "host_name": ", ".join(h.name for h in hosts) if hosts else "",
        "event_name": N.event_name(db, booking),
        "start_time": N.fmt_local(booking.start_at, tz),
        "start_date": N.fmt_local(booking.start_at, tz, "%A, %d %B %Y"),
        "end_time": N.fmt_local(booking.end_at, tz, "%H:%M"),
        "timezone": tz,
        "location": N.location_text(booking),
        "join_link": loc.get("join_url") or N.location_text(booking),
        "reschedule_url": links.get("reschedule_url", ""),
        "cancel_url": links.get("cancel_url", ""),
        "duration": str(int((booking.end_at - booking.start_at).total_seconds() // 60)),
    }


# ------------------------------------------------------------------ quiet hours
def _parse_hm(v: str) -> dt.time:
    h, m = (v or "0:0").split(":")[:2]
    return dt.time(int(h) % 24, int(m))


def apply_quiet_hours(when_utc: dt.datetime, workspace: Optional[Workspace], tz: str = "UTC") -> dt.datetime:
    """Defer `when_utc` to the end of the workspace quiet window if it falls inside it.
    settings_json.quiet_hours = {"start":"21:00","end":"08:00","tz":optional IANA (default recipient tz)}."""
    qh = ((workspace.settings_json or {}) if workspace else {}).get("quiet_hours") or {}
    if not qh.get("start") or not qh.get("end") or qh.get("enabled") is False:
        return when_utc
    try:
        z = ZoneInfo(qh.get("tz") or tz or "UTC")
    except Exception:
        z = ZoneInfo("UTC")
    start_t, end_t = _parse_hm(qh["start"]), _parse_hm(qh["end"])
    if start_t == end_t:
        return when_utc
    local = when_utc.replace(tzinfo=dt.timezone.utc).astimezone(z)
    t = local.time().replace(tzinfo=None)
    if start_t < end_t:
        inside = start_t <= t < end_t
        end_date = local.date()
    else:  # overnight window e.g. 21:00 -> 08:00
        inside = t >= start_t or t < end_t
        end_date = local.date() + dt.timedelta(days=1) if t >= start_t else local.date()
    if not inside:
        return when_utc
    resume = dt.datetime.combine(end_date, end_t).replace(tzinfo=z)
    return resume.astimezone(dt.timezone.utc).replace(tzinfo=None)


# ------------------------------------------------------------------ matching
def _applies(wf: Workflow, booking: Booking) -> bool:
    ids = (wf.conditions_json or {}).get("event_type_ids") or []
    if ids:
        return booking.event_type_id in ids
    if wf.managed or not wf.owner_user_id:
        return True
    host_ids = {booking.host_id} | {h.user_id for h in (booking.hosts or [])}
    return wf.owner_user_id in host_ids


def matching_workflows(db: Session, workspace_id: str, trigger: str) -> list[Workflow]:
    return list(db.scalars(select(Workflow).where(
        Workflow.workspace_id == workspace_id, Workflow.trigger == trigger, Workflow.active.is_(True))).all())


def _recipients(db: Session, wf: Workflow, booking: Booking) -> list[tuple[str, str]]:
    """[(address, timezone)] for the workflow audience/channel; SMS requires invitee consent."""
    out: list[tuple[str, str]] = []
    if wf.audience == "host":
        for h in N.hosts_of(db, booking):
            if wf.channel == "email":
                out.append((h.email, h.default_timezone or "UTC"))
    elif wf.audience == "custom":
        if wf.custom_recipient:
            out.append((wf.custom_recipient.strip(), booking.timezone or "UTC"))
    else:
        for inv in booking.invitees or []:
            tz = inv.timezone or booking.timezone or "UTC"
            if wf.channel == "sms":
                consent = inv.consent_json or {}
                if inv.phone and consent.get("sms") and not consent.get("sms_opt_out"):
                    out.append((inv.phone, tz))
            else:
                out.append((inv.email, tz))
    return out


def _sms_allowed(db: Session, workspace_id: str) -> bool:
    from app.entitlements import has_feature

    ws = db.get(Workspace, workspace_id)
    return bool(ws and has_feature(ws, "sms"))


def schedule_for_booking(db: Session, booking: Booking, trigger: str, now: Optional[dt.datetime] = None) -> list[NotificationJob]:
    now = now or utcnow()
    if trigger == "invitee_created" and booking.rescheduled_from_id:
        return []
    if trigger in TIMED_TRIGGERS and booking.status != "confirmed":
        return []
    ws = db.get(Workspace, booking.workspace_id)
    jobs = []
    for wf in matching_workflows(db, booking.workspace_id, trigger):
        if not _applies(wf, booking):
            continue
        if wf.channel == "sms" and not _sms_allowed(db, booking.workspace_id):
            continue
        offset = dt.timedelta(minutes=wf.offset_minutes or 0)
        if trigger == "before_event":
            when = booking.start_at - offset
            if when < now:
                continue  # reminder window already passed
        elif trigger == "after_event":
            when = booking.end_at + offset
        else:
            when = now + offset
        for addr, tz in _recipients(db, wf, booking):
            send_at = apply_quiet_hours(when, ws, tz)
            if trigger == "before_event" and send_at >= booking.start_at:
                send_at = when  # never defer a reminder past the meeting itself
            variables = booking_variables(db, booking, tz)
            body = render_template(wf.body_template, variables)
            subject = render_template(wf.subject_template or wf.name, variables)
            if wf.channel == "email":
                body = N.render_email("workflow.html", body_html=N.text_to_html(body), subject=subject,
                                      links=N.manage_links(booking))
            job = N.create_job(
                db, dedupe_key=f"wf:{wf.id}:{booking.id}:{trigger}:{hash_secret(addr.lower())[:16]}",
                recipient=addr, subject=subject if wf.channel == "email" else None, body=body,
                trigger=f"wf:{trigger}", channel=wf.channel, booking_id=booking.id, workspace_id=booking.workspace_id,
                workflow_id=wf.id, scheduled_at=send_at,
            )
            if job:
                jobs.append(job)
    return jobs


def schedule_for_routing(db: Session, ev) -> list[NotificationJob]:
    p = ev.payload_json or {}
    sub = db.get(RoutingSubmission, p.get("submission_id") or p.get("routing_submission_id") or "")
    if not sub:
        return []
    form = db.get(RoutingForm, sub.form_id)
    answers = sub.answers_json or {}
    fields = {f.get("id"): f for f in (form.form_schema_json if form else []) or []}
    email = name = None
    lines = []
    for k, v in answers.items():
        f = fields.get(k, {})
        label = f.get("label") or k
        lines.append(f"{label}: {v}")
        if (f.get("type") == "email" or "email" in label.lower()) and isinstance(v, str) and "@" in v:
            email = email or v
        if "name" in label.lower() and isinstance(v, str):
            name = name or v
    owner = db.get(User, form.owner_user_id) if form else None
    ws = db.get(Workspace, sub.workspace_id)
    variables = dict(SAMPLE_VARS)
    variables.update({
        "invitee_name": name or email or "there", "invitee_email": email or "", "host_name": owner.name if owner else "",
        "event_name": form.name if form else "Routing form", "start_time": N.fmt_local(sub.created_at, "UTC"),
        "timezone": "UTC", "location": "", "join_link": "", "reschedule_url": "", "cancel_url": "",
    })
    jobs = []
    for wf in matching_workflows(db, sub.workspace_id, "routing_submitted"):
        if wf.audience == "host":
            rcpts = [owner.email] if owner else []
        elif wf.audience == "custom":
            rcpts = [wf.custom_recipient] if wf.custom_recipient else []
        else:
            rcpts = [email] if email else []
        if wf.channel != "email":
            continue
        for addr in rcpts:
            body = render_template(wf.body_template, variables) + "\n\n" + "\n".join(lines)
            subject = render_template(wf.subject_template or wf.name, variables)
            html = N.render_email("workflow.html", body_html=N.text_to_html(body), subject=subject, links=None)
            job = N.create_job(
                db, dedupe_key=f"wf:{wf.id}:rs:{sub.id}:{hash_secret(addr.lower())[:16]}", recipient=addr,
                subject=subject, body=html, trigger="wf:routing_submitted", workspace_id=sub.workspace_id,
                workflow_id=wf.id, scheduled_at=apply_quiet_hours(utcnow() + dt.timedelta(minutes=wf.offset_minutes or 0), ws),
            )
            if job:
                jobs.append(job)
    return jobs


# ------------------------------------------------------------------ authoring helpers
def preview(wf_like: dict, variables: Optional[dict] = None) -> dict:
    v = variables or SAMPLE_VARS
    subject = render_template(wf_like.get("subject_template") or wf_like.get("name") or "", v)
    body = render_template(wf_like.get("body_template") or "", v)
    html = N.render_email("workflow.html", body_html=N.text_to_html(body), subject=subject, links=None)
    return {"subject": subject, "body": body, "html": html}


def send_test(db: Session, wf: Workflow, user: User, phone: Optional[str] = None):
    from app.services.messaging import send_email, send_sms

    v = dict(SAMPLE_VARS, host_name=user.name or SAMPLE_VARS["host_name"], timezone=user.default_timezone or "UTC")
    p = preview({"name": wf.name, "subject_template": wf.subject_template, "body_template": wf.body_template}, v)
    if wf.channel == "sms":
        return send_sms(db, phone or f"sms-test:{user.email}", p["body"])
    return send_email(db, user.email, "[Test] " + p["subject"], p["html"])


def can_edit(ctx, wf: Workflow) -> bool:
    if wf.workspace_id != ctx.workspace.id:
        return False
    if wf.managed:
        return ctx.can("admin")
    return ctx.can("admin") or wf.owner_user_id == ctx.user.id


def add_default_template(db: Session, ctx, key: str) -> Optional[Workflow]:
    t = next((t for t in DEFAULT_TEMPLATES if t["key"] == key), None)
    if not t:
        return None
    wf = Workflow(
        workspace_id=ctx.workspace.id, owner_user_id=ctx.user.id, name=t["name"], trigger=t["trigger"],
        offset_minutes=t["offset_minutes"], channel=t["channel"], audience=t["audience"],
        subject_template=t["subject_template"], body_template=t["body_template"], conditions_json={"event_type_ids": []},
        active=True, managed=False,
    )
    db.add(wf)
    db.flush()
    return wf


def event_types_for(db: Session, ctx) -> list[EventType]:
    return list(db.scalars(select(EventType).where(
        EventType.workspace_id == ctx.workspace.id, EventType.deleted_at.is_(None)).order_by(EventType.name)).all())
