"""Transactional notifications (PRD 11.1) on top of the outbox + NotificationJob queue.

Flow: outbox subscriber -> NotificationJob rows (unique dedupe_key => a retried event never
creates a second job) -> `notifications` periodic sender delivers due jobs via messaging with
exponential-backoff retries (MAX_ATTEMPTS, error stored) -> SentMessage log / dev inbox.

Pending jobs for a booking are cancelled when it is cancelled/rescheduled, and the sender
re-checks booking state, so a cancelled booking never receives a reminder.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
from typing import Iterable, Optional
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from markupsafe import escape
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.events import subscribe
from app.models import (
    Booking,
    CalendarConnection,
    EventType,
    InAppNotification,
    Invitee,
    NotificationJob,
    User,
    WaitlistEntry,
    utcnow,
)
from app.worker import periodic

log = logging.getLogger("notifications")
MAX_ATTEMPTS = 5
# Notices that must still go out after a booking is cancelled/rescheduled.
POST_CANCEL_TRIGGERS = {
    "booking_cancelled", "booking_rescheduled", "wf:invitee_canceled", "wf:rescheduled", "wf:no_show",
    "cancellation_requested", "payment_refunded", "booking_conflict", "calendar_sync_failed", "host_alert",
}
INACTIVE_STATUSES = ("cancelled", "rescheduled", "expired", "failed")


# ------------------------------------------------------------------ formatting helpers
def fmt_local(value: Optional[dt.datetime], tz: str, fmt: str = "%A, %d %B %Y · %H:%M") -> str:
    if not value:
        return ""
    try:
        z = ZoneInfo(tz or "UTC")
    except Exception:
        z = ZoneInfo("UTC")
    local = value.replace(tzinfo=dt.timezone.utc).astimezone(z)
    return f"{local.strftime(fmt)} {local.tzname() or ''}".strip()


def fmt_range(start: dt.datetime, end: dt.datetime, tz: str) -> str:
    return f"{fmt_local(start, tz)} – {fmt_local(end, tz, '%H:%M')}"


def event_name(db: Session, booking: Booking) -> str:
    snap = booking.snapshot_json or {}
    if snap.get("name"):
        return snap["name"]
    et = db.get(EventType, booking.event_type_id) if booking.event_type_id else None
    return et.name if et else "Meeting"


LOCATION_LABELS = {
    "google_meet": "Google Meet", "zoom": "Zoom", "teams": "Microsoft Teams", "phone": "Phone call",
    "in_person": "In person", "custom_url": "Online", "host_provides": "Host will provide details",
    "invitee_phone": "Host will call you",
}


def location_text(booking: Booking) -> str:
    loc = booking.location_json or {}
    t = loc.get("type")
    if loc.get("join_url"):
        return f"{LOCATION_LABELS.get(t, 'Video')}: {loc['join_url']}"
    if loc.get("host_action_required") or t in ("google_meet", "zoom", "teams"):
        return f"{LOCATION_LABELS.get(t, 'Video')} – {loc.get('fallback') or 'link will be shared by the host'}"
    if t == "invitee_phone":
        return f"Host will call you at {loc.get('value') or 'the number you provided'}"
    label = LOCATION_LABELS.get(t, "")
    val = loc.get("value") or ""
    return f"{label}: {val}" if label and val else (val or label or "Details to follow")


def manage_links(booking: Booking) -> dict:
    try:
        from app.services.booking import manage_urls

        d = manage_urls(booking)
        if d:
            return d
    except Exception:
        pass
    base = settings.base_url.rstrip("/")
    return {
        "manage_url": f"{base}/booking/{booking.manage_token}",
        "cancel_url": f"{base}/booking/{booking.manage_token}/cancel",
        "reschedule_url": f"{base}/booking/{booking.manage_token}/reschedule",
    }


def google_cal_link(db: Session, booking: Booking) -> str:
    try:
        from app.services.ics import google_calendar_url

        return google_calendar_url(booking)
    except Exception:
        q = {
            "action": "TEMPLATE", "text": event_name(db, booking),
            "dates": f"{booking.start_at:%Y%m%dT%H%M%SZ}/{booking.end_at:%Y%m%dT%H%M%SZ}",
            "details": f"Manage: {manage_links(booking)['manage_url']}", "location": location_text(booking),
        }
        return "https://calendar.google.com/calendar/render?" + urlencode(q, quote_via=quote)


def _fallback_ics(db: Session, booking: Booking, method: str) -> str:
    def esc(s: str) -> str:
        return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")

    host = db.get(User, booking.host_id)
    status = "CANCELLED" if method == "CANCEL" else "CONFIRMED"
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//AAL Scheduler//EN", f"METHOD:{method}", "BEGIN:VEVENT",
        f"UID:{booking.id}@aal-scheduler", f"DTSTAMP:{utcnow():%Y%m%dT%H%M%SZ}",
        f"DTSTART:{booking.start_at:%Y%m%dT%H%M%SZ}", f"DTEND:{booking.end_at:%Y%m%dT%H%M%SZ}",
        f"SUMMARY:{esc(event_name(db, booking))}", f"LOCATION:{esc(location_text(booking))}",
        f"ORGANIZER;CN={esc(host.name if host else '')}:mailto:{host.email if host else 'no-reply@example.com'}",
        f"STATUS:{status}", f"SEQUENCE:{booking.version or 1}", "END:VEVENT", "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"


def ics_attachment(db: Session, booking: Booking, method: str = "REQUEST", filename: str = "invite.ics") -> dict:
    content = None
    try:
        from app.services.ics import build_ics

        content = build_ics(booking, method=method)
    except Exception:
        content = None
    if not content:
        content = _fallback_ics(db, booking, method)
    return {"filename": filename, "content": content, "mime": "text/calendar"}


def render_email(template: str, **ctx) -> str:
    from app.deps import templates

    ctx.setdefault("app_name", settings.app_name)
    ctx.setdefault("base_url", settings.base_url.rstrip("/"))
    return templates.env.get_template(f"emails/{template}").render(**ctx)


def text_to_html(text: str) -> str:
    """Escape user-authored workflow text and keep line breaks / bare links clickable."""
    import re

    out = str(escape(text or ""))
    out = re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', out)
    return out.replace("\n", "<br>")


def hosts_of(db: Session, booking: Booking) -> list[User]:
    ids = [booking.host_id] + [h.user_id for h in (booking.hosts or [])]
    out = []
    for uid in dict.fromkeys(i for i in ids if i):
        u = db.get(User, uid)
        if u and not u.deleted_at:
            out.append(u)
    return out


def invitee_recipients(booking: Booking) -> list[tuple[str, str, Optional[Invitee]]]:
    """[(email, timezone, invitee_or_None)] including additional guests."""
    out, seen = [], set()
    for inv in booking.invitees or []:
        tz = inv.timezone or booking.timezone or "UTC"
        for email in [inv.email] + [g for g in (inv.guests_json or []) if isinstance(g, str)]:
            e = (email or "").strip().lower()
            if e and e not in seen:
                seen.add(e)
                out.append((e, tz, inv if email == inv.email else None))
    return out


def pref(user: User, key: str, default: bool = True) -> bool:
    return bool((user.notification_prefs_json or {}).get(key, default))


def booking_context(db: Session, booking: Booking, tz: str) -> dict:
    hosts = hosts_of(db, booking)
    loc = booking.location_json or {}
    return {
        "booking": booking,
        "event_name": event_name(db, booking),
        "host": hosts[0] if hosts else None,
        "hosts": hosts,
        "invitees": list(booking.invitees or []),
        "answers": list(booking.answers or []),
        "when": fmt_range(booking.start_at, booking.end_at, tz),
        "tz": tz,
        "duration": int((booking.end_at - booking.start_at).total_seconds() // 60),
        "location": location_text(booking),
        "join_url": loc.get("join_url"),
        "host_action_required": bool(loc.get("host_action_required")),
        "links": manage_links(booking),
        "gcal_url": google_cal_link(db, booking),
        "app_link": f"{settings.base_url.rstrip('/')}/app/scheduled/{booking.id}",
    }


# ------------------------------------------------------------------ job queue
def dedupe(key: str) -> str:
    return key if len(key) <= 200 else key[:150] + ":" + hashlib.sha256(key.encode()).hexdigest()[:40]


def create_job(
    db: Session, *, dedupe_key: str, recipient: str, subject: Optional[str], body: str, trigger: str,
    channel: str = "email", booking_id: Optional[str] = None, workspace_id: Optional[str] = None,
    workflow_id: Optional[str] = None, attachments: Optional[list] = None, scheduled_at: Optional[dt.datetime] = None,
) -> Optional[NotificationJob]:
    """Insert a job unless one with the same dedupe key exists. Returns the new job or None."""
    key = dedupe(dedupe_key)
    if db.scalar(select(NotificationJob.id).where(NotificationJob.dedupe_key == key)):
        return None
    job = NotificationJob(
        workspace_id=workspace_id, booking_id=booking_id, workflow_id=workflow_id, trigger=trigger, channel=channel,
        recipient=recipient, subject=subject, body=body, attachments_json=attachments or [],
        scheduled_at=scheduled_at or utcnow(), status="scheduled", dedupe_key=key,
    )
    try:
        with db.begin_nested():
            db.add(job)
            db.flush()
    except IntegrityError:
        return None
    return job


def cancel_jobs_for_booking(db: Session, booking_id: str, keep: Iterable[str] = POST_CANCEL_TRIGGERS,
                            reason: str = "Booking cancelled") -> int:
    keep = set(keep)
    n = 0
    for j in db.scalars(select(NotificationJob).where(
            NotificationJob.booking_id == booking_id, NotificationJob.status == "scheduled")).all():
        if j.trigger in keep:
            continue
        j.status, j.error = "cancelled", reason
        n += 1
    db.flush()
    return n


def in_app(db: Session, user_id: str, kind: str, title: str, body: str = "", link: Optional[str] = None) -> None:
    if link and db.scalar(select(InAppNotification.id).where(
            InAppNotification.user_id == user_id, InAppNotification.kind == kind, InAppNotification.link == link,
            InAppNotification.title == title)):
        return
    db.add(InAppNotification(user_id=user_id, kind=kind, title=title[:300], body=body, link=link))


def notify_host_alert(db: Session, user: User, title: str, message: str, link: Optional[str] = None,
                      dedupe_key: Optional[str] = None, workspace_id: Optional[str] = None,
                      booking_id: Optional[str] = None, kind: str = "host_alert") -> None:
    """P0 host alert: in-app + email (used by payments reconciliation, calendar failures, ...)."""
    in_app(db, user.id, kind, title, message, link)
    html = render_email("alert.html", title=title, message=message, link=link, recipient_name=user.name)
    create_job(db, dedupe_key=dedupe_key or f"alert:{kind}:{user.id}:{hashlib.sha1(message.encode()).hexdigest()}",
               recipient=user.email, subject=title, body=html, trigger="host_alert", workspace_id=workspace_id,
               booking_id=booking_id)


# ------------------------------------------------------------------ transactional builders
def queue_booking_confirmed(db: Session, booking: Booking) -> None:
    ics = ics_attachment(db, booking, "REQUEST")
    for email, tz, _inv in invitee_recipients(booking):
        c = booking_context(db, booking, tz)
        html = render_email("booking_confirmed.html", audience="invitee", **c)
        create_job(db, dedupe_key=f"confirm:{booking.id}:{email}", recipient=email,
                   subject=f"Confirmed: {c['event_name']} with {c['host'].name if c['host'] else 'your host'}",
                   body=html, trigger="booking_confirmed", booking_id=booking.id, workspace_id=booking.workspace_id,
                   attachments=[ics])
    for host in hosts_of(db, booking):
        tz = host.default_timezone or "UTC"
        c = booking_context(db, booking, tz)
        inv = c["invitees"][0] if c["invitees"] else None
        title = f"New booking: {inv.name if inv else 'Invitee'} – {c['event_name']}"
        details = f"{c['when']}"
        if inv:
            details += f"\n{inv.name} <{inv.email}>" + (f" · {inv.phone}" if inv.phone else "")
        for a in c["answers"]:
            details += f"\n{a.label}: {a.answer_json}"
        in_app(db, host.id, "new_booking", title, details, f"/app/scheduled/{booking.id}")
        if pref(host, "new_booking_email", True):
            html = render_email("booking_confirmed.html", audience="host", **c)
            create_job(db, dedupe_key=f"confirm:{booking.id}:host:{host.id}", recipient=host.email, subject=title,
                       body=html, trigger="booking_confirmed", booking_id=booking.id,
                       workspace_id=booking.workspace_id, attachments=[ics])


def queue_booking_cancelled(db: Session, booking: Booking) -> None:
    ics = ics_attachment(db, booking, "CANCEL", "cancel.ics")
    by = booking.cancelled_by or "host"
    for email, tz, _ in invitee_recipients(booking):
        c = booking_context(db, booking, tz)
        html = render_email("booking_cancelled.html", audience="invitee", cancelled_by=by, **c)
        create_job(db, dedupe_key=f"cancel:{booking.id}:{email}", recipient=email,
                   subject=f"Cancelled: {c['event_name']}", body=html, trigger="booking_cancelled",
                   booking_id=booking.id, workspace_id=booking.workspace_id, attachments=[ics])
    for host in hosts_of(db, booking):
        c = booking_context(db, booking, host.default_timezone or "UTC")
        inv = c["invitees"][0] if c["invitees"] else None
        title = f"Cancelled: {inv.name if inv else 'Invitee'} – {c['event_name']}"
        in_app(db, host.id, "booking_cancelled", title,
               f"{c['when']}" + (f"\nReason: {booking.cancel_reason}" if booking.cancel_reason else ""),
               f"/app/scheduled/{booking.id}")
        if pref(host, "cancellation_email", True):
            html = render_email("booking_cancelled.html", audience="host", cancelled_by=by, **c)
            create_job(db, dedupe_key=f"cancel:{booking.id}:host:{host.id}", recipient=host.email, subject=title,
                       body=html, trigger="booking_cancelled", booking_id=booking.id,
                       workspace_id=booking.workspace_id, attachments=[ics])


def queue_booking_rescheduled(db: Session, new: Booking, old: Optional[Booking]) -> None:
    atts = [ics_attachment(db, new, "REQUEST")]
    if old:
        atts.append(ics_attachment(db, old, "CANCEL", "cancel-previous.ics"))
    for email, tz, _ in invitee_recipients(new):
        c = booking_context(db, new, tz)
        old_when = fmt_range(old.start_at, old.end_at, tz) if old else ""
        html = render_email("booking_rescheduled.html", audience="invitee", old_when=old_when, old=old, **c)
        create_job(db, dedupe_key=f"resched:{new.id}:{email}", recipient=email,
                   subject=f"Rescheduled: {c['event_name']} – {c['when']}", body=html, trigger="booking_rescheduled",
                   booking_id=new.id, workspace_id=new.workspace_id, attachments=atts)
    for host in hosts_of(db, new):
        tz = host.default_timezone or "UTC"
        c = booking_context(db, new, tz)
        old_when = fmt_range(old.start_at, old.end_at, tz) if old else ""
        inv = c["invitees"][0] if c["invitees"] else None
        title = f"Rescheduled: {inv.name if inv else 'Invitee'} – {c['event_name']}"
        in_app(db, host.id, "booking_rescheduled", title, f"New time: {c['when']}\nWas: {old_when}",
               f"/app/scheduled/{new.id}")
        html = render_email("booking_rescheduled.html", audience="host", old_when=old_when, old=old, **c)
        create_job(db, dedupe_key=f"resched:{new.id}:host:{host.id}", recipient=host.email, subject=title, body=html,
                   trigger="booking_rescheduled", booking_id=new.id, workspace_id=new.workspace_id, attachments=atts)


# ------------------------------------------------------------------ outbox subscribers
def _booking(db: Session, ev) -> Optional[Booking]:
    bid = (ev.payload_json or {}).get("booking_id")
    return db.get(Booking, bid) if bid else None


@subscribe("booking.created")
def on_booking_created(db: Session, ev) -> None:
    from app.services import workflows

    b = _booking(db, ev)
    if not b or b.status in INACTIVE_STATUSES or b.status == "held":
        return
    if b.status == "pending_payment":
        return  # notifications go out once payment succeeds and the booking is finalised
    if not b.rescheduled_from_id:
        queue_booking_confirmed(db, b)
        workflows.schedule_for_booking(db, b, "invitee_created")
    workflows.schedule_for_booking(db, b, "before_event")
    workflows.schedule_for_booking(db, b, "after_event")


@subscribe("booking.cancelled")
def on_booking_cancelled(db: Session, ev) -> None:
    from app.services import workflows

    b = _booking(db, ev)
    if not b:
        return
    cancel_jobs_for_booking(db, b.id)
    queue_booking_cancelled(db, b)
    workflows.schedule_for_booking(db, b, "invitee_canceled")


@subscribe("booking.rescheduled")
def on_booking_rescheduled(db: Session, ev) -> None:
    from app.services import workflows

    p = ev.payload_json or {}
    new = db.get(Booking, p.get("booking_id")) if p.get("booking_id") else None
    old_id = p.get("old_booking_id") or (new.rescheduled_from_id if new else None)
    old = db.get(Booking, old_id) if old_id else None
    if old:
        cancel_jobs_for_booking(db, old.id, reason="Booking rescheduled")
    if not new:
        return
    queue_booking_rescheduled(db, new, old)
    workflows.schedule_for_booking(db, new, "rescheduled")
    workflows.schedule_for_booking(db, new, "before_event")
    workflows.schedule_for_booking(db, new, "after_event")


@subscribe("booking.no_show")
def on_no_show(db: Session, ev) -> None:
    from app.services import workflows

    b = _booking(db, ev)
    if b and (ev.payload_json or {}).get("value", True):
        workflows.schedule_for_booking(db, b, "no_show")


@subscribe("booking.cancellation_requested")
def on_cancellation_requested(db: Session, ev) -> None:
    b = _booking(db, ev)
    if not b:
        return
    for host in hosts_of(db, b):
        c = booking_context(db, b, host.default_timezone or "UTC")
        inv = c["invitees"][0] if c["invitees"] else None
        title = f"Cancellation requested: {inv.name if inv else 'Invitee'} – {c['event_name']}"
        reason = (ev.payload_json or {}).get("reason") or b.cancel_reason or ""
        in_app(db, host.id, "cancellation_requested", title, f"{c['when']}\n{reason}", f"/app/scheduled/{b.id}")
        html = render_email("cancellation_requested.html", reason=reason, **c)
        create_job(db, dedupe_key=f"cancelreq:{b.id}:{host.id}:{ev.id}", recipient=host.email, subject=title,
                   body=html, trigger="cancellation_requested", booking_id=b.id, workspace_id=b.workspace_id)


@subscribe("waitlist.spot_open")
def on_waitlist_spot_open(db: Session, ev) -> None:
    p = ev.payload_json or {}
    ids = p.get("entry_ids") or p.get("waitlist_entry_ids") or ([p["waitlist_entry_id"]] if p.get("waitlist_entry_id") else [])
    entries: list[WaitlistEntry] = []
    if ids:
        entries = [e for e in (db.get(WaitlistEntry, i) for i in ids) if e]
    elif p.get("event_type_id") and p.get("start_at"):
        from app.services.providers.base import parse_dt

        start = parse_dt(p["start_at"]) if isinstance(p["start_at"], str) else p["start_at"]
        entries = list(db.scalars(select(WaitlistEntry).where(
            WaitlistEntry.event_type_id == p["event_type_id"], WaitlistEntry.start_at == start)).all())
    for e in entries:
        et = db.get(EventType, e.event_type_id)
        owner = db.get(User, et.owner_user_id) if et else None
        url = p.get("booking_url")
        if not url and et and owner:
            url = f"{settings.base_url.rstrip('/')}/{owner.slug}/{et.slug}?date={e.start_at:%Y-%m-%d}"
        html = render_email("waitlist_spot_open.html", name=e.name, event_name=et.name if et else "Meeting",
                            when=fmt_local(e.start_at, p.get("timezone") or "UTC"), url=url,
                            host=owner)
        create_job(db, dedupe_key=f"waitlist:{e.id}:{e.start_at:%Y%m%d%H%M}:{ev.id}", recipient=e.email,
                   subject=f"A spot opened up: {et.name if et else 'Meeting'}", body=html, trigger="waitlist_spot_open",
                   workspace_id=e.workspace_id)
        e.notified_at = utcnow()


@subscribe("calendar.sync_failed")
def on_calendar_sync_failed(db: Session, ev) -> None:
    p = ev.payload_json or {}
    user = db.get(User, p.get("user_id")) if p.get("user_id") else None
    if not user:
        conn = db.get(CalendarConnection, p.get("connection_id")) if p.get("connection_id") else None
        user = db.get(User, conn.user_id) if conn else None
    if not user and p.get("booking_id"):
        b = db.get(Booking, p["booking_id"])
        user = db.get(User, b.host_id) if b else None
    if not user:
        return
    provider = {"google": "Google Calendar", "microsoft": "Microsoft Outlook", "mock": "Demo calendar"}.get(
        p.get("provider"), "Calendar")
    title = f"Action required: {provider} sync failed"
    msg = (f"We couldn't sync your {provider}. Until it is reconnected, availability on your booking pages "
           f"cannot be verified and new bookings may not appear on your calendar.\n\nError: {p.get('error') or 'unknown'}")
    link = f"{settings.base_url.rstrip('/')}/app/integrations"
    in_app(db, user.id, "calendar_sync_failed", title, msg, "/app/integrations")
    html = render_email("alert.html", title=title, message=msg, link=link, link_label="Reconnect calendar",
                        recipient_name=user.name)
    bucket = utcnow().strftime("%Y%m%d%H")
    create_job(db, dedupe_key=f"calfail:{p.get('connection_id')}:{p.get('booking_id') or bucket}", recipient=user.email,
               subject=title, body=html, trigger="calendar_sync_failed", workspace_id=ev.workspace_id,
               booking_id=p.get("booking_id"))


@subscribe("booking.conflict")
def on_booking_conflict(db: Session, ev) -> None:
    b = _booking(db, ev)
    if not b:
        return
    detail = (ev.payload_json or {}).get("detail") or b.conflict_detail or "Calendar conflict"
    for host in hosts_of(db, b)[:1]:
        c = booking_context(db, b, host.default_timezone or "UTC")
        html = render_email("alert.html", title=f"Calendar conflict: {c['event_name']} ({c['when']})",
                            message=detail + "\n\nNothing was deleted – review the booking and your calendar.",
                            link=c["app_link"], link_label="Review booking", recipient_name=host.name)
        create_job(db, dedupe_key=f"conflict:{b.id}:{hashlib.sha1(detail.encode()).hexdigest()}",
                   recipient=host.email, subject=f"Calendar conflict: {c['event_name']}", body=html,
                   trigger="booking_conflict", booking_id=b.id, workspace_id=b.workspace_id)


@subscribe("routing.submitted")
def on_routing_submitted(db: Session, ev) -> None:
    from app.services import workflows

    workflows.schedule_for_routing(db, ev)


# ------------------------------------------------------------------ sender
def _sms_opted_out(db: Session, phone: str) -> bool:
    for inv in db.scalars(select(Invitee).where(Invitee.phone == phone)).all():
        if (inv.consent_json or {}).get("sms_opt_out"):
            return True
    return False


def send_job(db: Session, job: NotificationJob, now: Optional[dt.datetime] = None) -> str:
    """Deliver one job (status transitions + retry bookkeeping). Returns the resulting status."""
    from app.services.messaging import MessagingError, send_email, send_sms

    now = now or utcnow()
    if job.status != "scheduled":
        return job.status
    if job.booking_id and job.trigger not in POST_CANCEL_TRIGGERS:
        b = db.get(Booking, job.booking_id)
        if b and b.status in INACTIVE_STATUSES:
            job.status, job.error = "cancelled", f"Booking {b.status}"
            return job.status
    if job.channel == "sms" and _sms_opted_out(db, job.recipient):
        job.status, job.error = "skipped", "Recipient opted out of SMS (STOP)"
        return job.status
    # Atomic claim (duplicate-send prevention across concurrent workers): only the worker whose
    # conditional UPDATE matches the attempt counter it read may send this attempt.
    db.flush()  # persist in-memory edits (e.g. retry_job resetting attempts) before the conditional claim
    old_attempts = job.attempts or 0
    claimed = db.execute(update(NotificationJob).where(
        NotificationJob.id == job.id, NotificationJob.status == "scheduled",
        NotificationJob.attempts == old_attempts).values(attempts=old_attempts + 1)).rowcount
    if not claimed:
        db.refresh(job)
        return job.status
    job.attempts = old_attempts + 1
    try:
        if job.channel == "sms":
            msg = send_sms(db, job.recipient, job.body or "", job_id=job.id)
        else:
            msg = send_email(db, job.recipient, job.subject or "", job.body or "", job.attachments_json or [], job_id=job.id)
        job.status, job.provider_id, job.sent_at, job.error, job.next_attempt_at = "sent", msg.provider_id, now, None, None
        from app.services.usage import record_usage

        record_usage(db, job.workspace_id, "sms_sent" if job.channel == "sms" else "emails_sent")
    except (MessagingError, Exception) as exc:  # noqa: BLE001
        job.error = str(exc)[:2000]
        if job.attempts >= MAX_ATTEMPTS:
            job.status, job.next_attempt_at = "failed", None
        else:
            job.next_attempt_at = now + dt.timedelta(minutes=2 ** (job.attempts - 1))
    return job.status


def send_due(db: Session, now: Optional[dt.datetime] = None, limit: int = 200) -> int:
    now = now or utcnow()
    jobs = db.scalars(
        select(NotificationJob)
        .where(NotificationJob.status == "scheduled", NotificationJob.scheduled_at <= now)
        .order_by(NotificationJob.scheduled_at)
        .limit(limit)
    ).all()
    n = 0
    for job in jobs:
        if job.next_attempt_at and job.next_attempt_at > now:
            continue
        send_job(db, job, now)
        db.commit()
        n += 1
    return n


@periodic("notifications")
def notifications_task(db: Session) -> None:
    send_due(db)


def retry_job(db: Session, job: NotificationJob) -> None:
    job.status, job.next_attempt_at, job.error = "scheduled", None, None
    job.attempts = 0
    if job.scheduled_at > utcnow():
        return
    job.scheduled_at = utcnow()
