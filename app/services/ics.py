"""iCalendar (RFC 5545) invites + "add to calendar" links.

    build_ics(booking, method="REQUEST"|"CANCEL", event_name=None, organizer_email=None) -> str
    google_calendar_url(booking, event_name=None) -> str
    outlook_calendar_url(booking, event_name=None) -> str

UID is stable across a reschedule chain (stored in booking.snapshot_json["_ics"]["uid"] and copied
to the replacement booking); SEQUENCE increments on every reschedule and once more on cancel.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional
from urllib.parse import urlencode

from sqlalchemy.orm import object_session

from app.config import settings
from app.models import User


def _esc(value: Optional[str]) -> str:
    v = str(value or "")
    return v.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n")


def _fold(line: str) -> str:
    """Fold lines longer than 75 octets (RFC 5545 3.1)."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > (75 if not parts else 74):
            parts.append(cur.decode("utf-8"))
            cur = b""
        cur += b
    parts.append(cur.decode("utf-8"))
    return "\r\n ".join(parts)


def _ts(value: dt.datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%SZ")


def _ics_meta(booking) -> tuple[str, int]:
    meta = (booking.snapshot_json or {}).get("_ics") or {}
    return meta.get("uid") or f"{booking.id}@aal-scheduler", int(meta.get("seq") or 0)


def location_text(booking) -> str:
    loc = booking.location_json or {}
    if loc.get("join_url"):
        return loc["join_url"]
    t = loc.get("type")
    v = loc.get("value")
    labels = {
        "google_meet": "Google Meet (link to follow)",
        "zoom": "Zoom (link to follow)",
        "teams": "Microsoft Teams (link to follow)",
        "phone": f"Phone call: {v}" if v else "Phone call",
        "invitee_phone": f"Host will call {v}" if v else "Host will call you",
        "in_person": v or "In person",
        "custom_url": v or "Online",
        "host_provides": loc.get("note") or "Host will provide details",
    }
    return labels.get(t, v or "")


def _event_name(booking, event_name: Optional[str]) -> str:
    return event_name or (booking.snapshot_json or {}).get("name") or "Meeting"


def build_ics(booking, method: str = "REQUEST", event_name: Optional[str] = None,
              organizer_email: Optional[str] = None) -> str:
    method = "CANCEL" if method.upper() == "CANCEL" else ("PUBLISH" if method.upper() == "PUBLISH" else "REQUEST")
    uid, seq = _ics_meta(booking)
    if method == "CANCEL":
        seq += 1
    db = object_session(booking)
    host = db.get(User, booking.host_id) if db else None
    org_email = organizer_email or (host.email if host else "no-reply@example.com")
    org_name = host.name if host else settings.app_name
    name = _event_name(booking, event_name)
    base = settings.base_url.rstrip("/")
    desc = [f"{name}"]
    if (booking.snapshot_json or {}).get("description"):
        desc.append(booking.snapshot_json["description"])
    desc.append(f"Location: {location_text(booking)}")
    desc.append(f"Manage this booking: {base}/booking/{booking.manage_token}")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//AAL Scheduler//EN",
        "CALSCALE:GREGORIAN",
        f"METHOD:{method}",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"SEQUENCE:{seq}",
        f"DTSTAMP:{_ts(dt.datetime.now(dt.timezone.utc).replace(tzinfo=None))}",
        f"DTSTART:{_ts(booking.start_at)}",
        f"DTEND:{_ts(booking.end_at)}",
        f"SUMMARY:{_esc(name)}",
        f"DESCRIPTION:{_esc(chr(10).join(desc))}",
        f"LOCATION:{_esc(location_text(booking))}",
        f"ORGANIZER;CN={_esc(org_name)}:mailto:{org_email}",
    ]
    for inv in getattr(booking, "invitees", []) or []:
        lines.append(f"ATTENDEE;CN={_esc(inv.name)};ROLE=REQ-PARTICIPANT;PARTSTAT=ACCEPTED;RSVP=FALSE:mailto:{inv.email}")
        for g in inv.guests_json or []:
            lines.append(f"ATTENDEE;ROLE=OPT-PARTICIPANT;RSVP=TRUE:mailto:{g}")
    if (booking.location_json or {}).get("join_url"):
        lines.append(f"URL:{booking.location_json['join_url']}")
    lines.append("STATUS:CANCELLED" if method == "CANCEL" else "STATUS:CONFIRMED")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def google_calendar_url(booking, event_name: Optional[str] = None) -> str:
    name = _event_name(booking, event_name)
    base = settings.base_url.rstrip("/")
    params = {
        "action": "TEMPLATE",
        "text": name,
        "dates": f"{_ts(booking.start_at)}/{_ts(booking.end_at)}",
        "details": f"Manage this booking: {base}/booking/{booking.manage_token}",
        "location": location_text(booking),
    }
    return "https://calendar.google.com/calendar/render?" + urlencode(params)


def outlook_calendar_url(booking, event_name: Optional[str] = None) -> str:
    params = {
        "path": "/calendar/action/compose",
        "rru": "addevent",
        "subject": _event_name(booking, event_name),
        "startdt": booking.start_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "enddt": booking.end_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "location": location_text(booking),
    }
    return "https://outlook.live.com/calendar/0/deeplink/compose?" + urlencode(params)
