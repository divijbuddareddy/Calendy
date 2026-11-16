"""AI scheduling assistant (PRD 16.1).

Design
------
* `parse_request(text, today, contacts, event_types)` is a deterministic natural-language parser that
  extracts intent, duration, date range, time-of-day window, participant (matched to Contacts) and
  event type. It returns a confidence score and a short human-readable rationale (never chain of thought).
* If `settings.anthropic_api_key` is set, `llm_extract()` asks the Anthropic Messages API for the same
  JSON fields; any error falls back to the deterministic parser (the parser always runs).
* `propose()` fetches slots ONLY via `app.services.availability.get_available_slots`, so suggestions can
  never fall outside availability / buffers / limits, and drafts an email. Nothing is sent or booked until
  the user explicitly approves (`execute()`).
* `answer_history()` answers meeting-history questions from the user's own bookings/contacts only
  (least privilege: bookings where the user is a host, inside the user's workspace).
* Schedule from email: `parse_email_thread()` + `interpret_email()` extract the requester, other
  participants, duration and window from a pasted inbound thread; `propose_email_reply()` finds slots
  through the same availability service and drafts a "Re:" reply. Sending / booking needs approval.
* Meeting prep: `meeting_prep()` builds a read-only brief for the next meeting (contact, history,
  intake answers, last notes, recap action items); an optional agenda email needs approval.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Booking, BookingHost, Contact, EventType, Invitee, User, utcnow

log = logging.getLogger("assistant")

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
WD_ABBR = {"mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3, "fri": 4, "sat": 5, "sun": 6}
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
TIME_WINDOWS = {"morning": (8 * 60, 12 * 60), "afternoon": (12 * 60, 17 * 60), "evening": (17 * 60, 21 * 60)}
_STOP = r"(?:\s+(?:on|next|this|tomorrow|today|for|at|in|about|around|during|before|after|sometime|some|to|re|regarding|morning|afternoon|evening)\b|[,.?!;]|$)"


def _tz(name: Optional[str]) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def local_today(tz: str) -> dt.date:
    return dt.datetime.now(_tz(tz)).date()


# ---------------------------------------------------------------- parser pieces
def parse_duration(text: str) -> Optional[int]:
    t = text.lower()
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:-\s*)?(hours?|hrs?|h)\b", t)
    if m:
        return int(round(float(m.group(1)) * 60))
    m = re.search(r"(\d+)\s*(?:-\s*)?(minutes?|mins?|m)\b", t)
    if m:
        return int(m.group(1))
    if re.search(r"\bhalf(?: an)? hour\b", t):
        return 30
    if re.search(r"\bquarter(?: of an)? hour\b", t):
        return 15
    if re.search(r"\b(an|one) hour\b", t):
        return 60
    return None


def parse_date_range(text: str, today: dt.date) -> tuple[dt.date, dt.date, bool, str]:
    """Return (start, end, explicit, description)."""
    t = text.lower()
    week_start = today - dt.timedelta(days=today.weekday())
    if "day after tomorrow" in t:
        d = today + dt.timedelta(days=2)
        return d, d, True, "day after tomorrow"
    if re.search(r"\btomorrow\b", t):
        d = today + dt.timedelta(days=1)
        return d, d, True, "tomorrow"
    if re.search(r"\btoday\b", t):
        return today, today, True, "today"
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t)
    if m:
        try:
            d = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return d, d, True, d.isoformat()
        except ValueError:
            pass
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(MONTHS) + r")[a-z]*\b", t) or re.search(
        r"\b(" + "|".join(MONTHS) + r")[a-z]*\s+(\d{1,2})(?:st|nd|rd|th)?\b", t
    )
    if m:
        a, b = m.group(1), m.group(2)
        day, mon = (int(a), b) if a.isdigit() else (int(b), a)
        try:
            d = dt.date(today.year, MONTHS.index(mon[:3]) + 1, day)
            if d < today:
                d = d.replace(year=today.year + 1)
            return d, d, True, d.strftime("%a %d %b")
        except ValueError:
            pass
    wd_names = "|".join(WEEKDAYS + list(WD_ABBR))
    m = re.search(r"\b(next|this|on|coming)?\s*(" + wd_names + r")\b", t)
    if m:
        word = m.group(2)
        wd = WEEKDAYS.index(word) if word in WEEKDAYS else WD_ABBR[word]
        if m.group(1) == "next":
            # "next <weekday>" = that weekday in next calendar week (Mon-start)
            d = week_start + dt.timedelta(days=7 + wd)
            return d, d, True, f"next {WEEKDAYS[wd].title()}"
        d = today + dt.timedelta(days=(wd - today.weekday()) % 7)
        return d, d, True, WEEKDAYS[wd].title()
    if re.search(r"\bnext week\b", t):
        s = week_start + dt.timedelta(days=7)
        return s, s + dt.timedelta(days=6), True, "next week"
    if re.search(r"\bthis week\b", t):
        return today, week_start + dt.timedelta(days=6), True, "this week"
    if re.search(r"\bnext month\b", t):
        first = (today.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
        last = (first + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1)
        return first, last, True, "next month"
    if re.search(r"\bthis month\b", t):
        last = (today.replace(day=1) + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1)
        return today, last, True, "this month"
    return today, today + dt.timedelta(days=7), False, "the next 7 days"


def _clock(h: str, mi: Optional[str], ap: Optional[str]) -> int:
    hour = int(h) % 12 if ap else int(h)
    if ap == "pm":
        hour += 12
    return hour * 60 + int(mi or 0)


def parse_time_window(text: str) -> tuple[Optional[str], Optional[tuple[int, int]]]:
    t = text.lower()
    for name, win in TIME_WINDOWS.items():
        if re.search(rf"\b{name}s?\b", t):
            return name, win
    m = re.search(r"\bafter\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", t)
    if m:
        return f"after {m.group(0)[6:].strip()}", (_clock(m.group(1), m.group(2), m.group(3)), 24 * 60)
    m = re.search(r"\bbefore\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", t)
    if m:
        return f"before {m.group(0)[7:].strip()}", (0, _clock(m.group(1), m.group(2), m.group(3)))
    if re.search(r"\blunch\b", t):
        return "lunchtime", (11 * 60 + 30, 14 * 60)
    return None, None


def extract_participant(text: str) -> tuple[Optional[str], Optional[str]]:
    """Return (name_or_none, email_or_none) mentioned after 'with' / any email."""
    em = re.search(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text)
    email = em.group(0).lower() if em else None
    m = re.search(r"\bwith\s+(.+?)" + _STOP, text, flags=re.IGNORECASE)
    name = None
    if m:
        cand = m.group(1).strip()
        if cand and "@" not in cand and cand.lower() not in ("me", "us", "them", "him", "her"):
            name = cand
    return name, email


def match_contacts(contacts: list, name: Optional[str], email: Optional[str]) -> list:
    if email:
        hits = [c for c in contacts if c.email_normalized == email]
        if hits:
            return hits
    if not name:
        return []
    n = name.lower().strip()
    exact = [c for c in contacts if (c.name or "").lower() == n]
    if exact:
        return exact
    first = n.split()[0]
    hits = [c for c in contacts if (c.name or "").lower().startswith(n) or (c.name or "").lower().split()[:1] == [first]]
    if not hits:
        hits = [c for c in contacts if n in (c.name or "").lower() or n in c.email_normalized]
    return hits


def detect_intent(text: str) -> str:
    t = text.lower().strip()
    if re.search(r"\b(prep|prepare|briefing|brief me)\b", t) or re.search(r"\bget ready for\b", t):
        return "prep"
    if re.match(r"^(when|how many|how often|who|what|did i|have i|list|show)\b", t) or re.search(
        r"\b(last met|last meet|last meeting|meeting history|met with|how many)\b", t
    ):
        return "history"
    return "schedule"


def parse_request(text: str, today: dt.date, contacts: list, event_types: list) -> dict:
    """Deterministic parser. Pure function (no DB); returns a JSON-serialisable dict."""
    reasons = []
    conf = 0.45
    intent = detect_intent(text)
    duration = parse_duration(text)
    if duration:
        reasons.append(f"duration {duration} min stated")
        conf += 0.15
    start, end, explicit, desc = parse_date_range(text, today)
    reasons.append(f"date range: {desc}" + ("" if explicit else " (default)"))
    if explicit:
        conf += 0.15
    tod_label, window = parse_time_window(text)
    if tod_label:
        reasons.append(f"time of day: {tod_label}")
    pname, pemail = extract_participant(text)
    participant = None
    alternatives = []
    if pname or pemail:
        hits = match_contacts(contacts, pname, pemail)
        if hits:
            c = hits[0]
            participant = {"name": c.name or c.email_normalized, "email": c.email_normalized, "contact_id": c.id}
            alternatives = [{"name": h.name, "email": h.email_normalized, "contact_id": h.id} for h in hits[1:5]]
            reasons.append(f"matched contact {participant['name']} <{participant['email']}>"
                           + (f" ({len(hits)} possible matches)" if len(hits) > 1 else ""))
            conf += 0.2 if len(hits) == 1 else 0.05
        else:
            participant = {"name": pname or pemail, "email": pemail, "contact_id": None}
            reasons.append(f"participant '{pname or pemail}' not found in contacts")
            conf -= 0.1 if not pemail else 0
    event_type = None
    tl = text.lower()
    for et in sorted(event_types, key=lambda e: -len(e.name or "")):
        if (et.name and et.name.lower() in tl) or (et.slug and et.slug.lower() in tl):
            event_type = {"id": et.id, "name": et.name}
            reasons.append(f"event type '{et.name}' mentioned")
            conf += 0.05
            break
    return {
        "intent": intent,
        "duration": duration,
        "date_start": start.isoformat(),
        "date_end": end.isoformat(),
        "date_explicit": explicit,
        "time_of_day": tod_label,
        "window_minutes": list(window) if window else None,
        "participant": participant,
        "alternatives": alternatives,
        "event_type": event_type,
        "confidence": round(max(0.05, min(conf, 0.95)), 2),
        "rationale": "; ".join(reasons),
        "parser": "deterministic",
    }


# ---------------------------------------------------------------- optional LLM extraction
def llm_extract(text: str, today: dt.date, timeout: float = 8.0) -> Optional[dict]:
    """Ask Claude to extract the same fields. Returns None when not configured or on any error."""
    if not settings.anthropic_api_key:
        return None
    try:
        import httpx

        system = (
            "Extract scheduling intent as strict JSON with keys: intent ('schedule'|'history'), "
            "duration_minutes (int|null), date_start (YYYY-MM-DD|null), date_end (YYYY-MM-DD|null), "
            "time_of_day ('morning'|'afternoon'|'evening'|null), participant_name (str|null), "
            "participant_email (str|null), event_type (str|null). 'next <weekday>' means that weekday in next "
            f"calendar week. Today is {today.isoformat()} ({today.strftime('%A')}). Reply with JSON only."
        )
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": settings.anthropic_api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-sonnet-5-5", "max_tokens": 300, "system": system,
                  "messages": [{"role": "user", "content": text[:2000]}]},
            timeout=timeout,
        )
        r.raise_for_status()
        body = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        m = re.search(r"\{.*\}", body, flags=re.S)
        return json.loads(m.group(0)) if m else None
    except Exception as exc:  # fall back silently to deterministic parser
        log.warning("LLM extraction failed, using deterministic parser: %s", exc)
        return None


def merge_llm(parsed: dict, llm: Optional[dict], contacts: list) -> dict:
    if not llm:
        return parsed
    out = dict(parsed)
    try:
        if llm.get("intent") in ("schedule", "history"):
            out["intent"] = llm["intent"]
        if isinstance(llm.get("duration_minutes"), int) and 5 <= llm["duration_minutes"] <= 480:
            out["duration"] = llm["duration_minutes"]
        for k in ("date_start", "date_end"):
            if llm.get(k):
                dt.date.fromisoformat(llm[k])
                out[k] = llm[k]
        if llm.get("time_of_day") in TIME_WINDOWS:
            out["time_of_day"] = llm["time_of_day"]
            out["window_minutes"] = list(TIME_WINDOWS[llm["time_of_day"]])
        if (llm.get("participant_name") or llm.get("participant_email")) and not (
            parsed.get("participant") and parsed["participant"].get("contact_id")
        ):
            hits = match_contacts(contacts, llm.get("participant_name"), (llm.get("participant_email") or "").lower() or None)
            if hits:
                out["participant"] = {"name": hits[0].name, "email": hits[0].email_normalized, "contact_id": hits[0].id}
        out["parser"] = "llm+deterministic"
        out["rationale"] = parsed["rationale"] + "; refined by language model"
    except Exception:
        return parsed
    return out


# ---------------------------------------------------------------- DB-backed operations
def workspace_contacts(db: Session, workspace_id: str) -> list:
    return list(db.scalars(select(Contact).where(Contact.workspace_id == workspace_id, Contact.deleted_at.is_(None))))


def user_event_types(db: Session, workspace_id: str, user_id: str) -> list:
    return list(db.scalars(
        select(EventType).where(
            EventType.workspace_id == workspace_id, EventType.owner_user_id == user_id, EventType.deleted_at.is_(None),
            EventType.status == "active", EventType.is_managed_template.is_(False), EventType.is_one_off.is_(False),
        ).order_by(EventType.created_at)
    ))


def interpret(db: Session, user: User, workspace_id: str, text: str, today: Optional[dt.date] = None) -> dict:
    today = today or local_today(user.default_timezone)
    contacts = workspace_contacts(db, workspace_id)
    ets = user_event_types(db, workspace_id, user.id)
    parsed = parse_request(text, today, contacts, ets)
    return merge_llm(parsed, llm_extract(text, today), contacts)


def choose_event_type(db: Session, user: User, workspace_id: str, parsed: dict) -> Optional[EventType]:
    ets = user_event_types(db, workspace_id, user.id)
    if parsed.get("event_type"):
        for e in ets:
            if e.id == parsed["event_type"]["id"]:
                return e
    dur = parsed.get("duration")
    if dur:
        for e in ets:
            if e.duration == dur or dur in (e.durations_json or []):
                return e
    return ets[0] if ets else None


def _pick_spread(slots: list, n: int = 3) -> list:
    if len(slots) <= n:
        return slots
    # prefer distinct days, then spread across the list
    chosen, days = [], set()
    for s in slots:
        if s.start.date() not in days:
            chosen.append(s)
            days.add(s.start.date())
        if len(chosen) == n:
            return chosen
    step = max(1, len(slots) // n)
    for s in slots[::step]:
        if s not in chosen:
            chosen.append(s)
        if len(chosen) == n:
            break
    return sorted(chosen, key=lambda s: s.start)


def propose(db: Session, user: User, workspace_id: str, parsed: dict) -> dict:
    """Fetch real availability and build the action preview + draft email. Never sends anything."""
    from app.deps import _fmt_dt

    tz = user.default_timezone or "UTC"
    et = choose_event_type(db, user, workspace_id, parsed)
    proposal: dict = {"slots": [], "event_type": None, "booking_link": None, "warnings": [], "draft": None}
    if not et:
        proposal["warnings"].append("You have no active event types. Create one so the assistant can offer times.")
        return proposal
    duration = parsed.get("duration") or et.duration
    if duration != et.duration and duration not in (et.durations_json or []):
        proposal["warnings"].append(
            f"'{et.name}' does not offer {duration} minutes; using its default {et.duration} minutes.")
        duration = et.duration
    proposal["event_type"] = {"id": et.id, "name": et.name, "duration": duration}
    owner = db.get(User, et.owner_user_id)
    link = f"{settings.base_url}/{owner.slug}/{et.slug}"
    p = parsed.get("participant") or {}
    if p.get("name") or p.get("email"):
        from urllib.parse import urlencode

        q = {k: v for k, v in (("name", p.get("name")), ("email", p.get("email"))) if v}
        link += "?" + urlencode(q)
    proposal["booking_link"] = link
    try:
        from app.services.availability import get_available_slots

        start = dt.date.fromisoformat(parsed["date_start"])
        end = dt.date.fromisoformat(parsed["date_end"])
        slots = get_available_slots(db, et, start, end, tz, duration=duration)
    except (ImportError, AttributeError) as exc:
        proposal["warnings"].append(f"Availability service is not available yet ({exc.__class__.__name__}).")
        slots = []
    except Exception as exc:
        proposal["warnings"].append(f"Could not compute availability: {exc}")
        slots = []
    window = parsed.get("window_minutes")
    if window:
        z = _tz(tz)

        def in_window(s):
            loc = s.start.replace(tzinfo=dt.timezone.utc).astimezone(z)
            mins = loc.hour * 60 + loc.minute
            return window[0] <= mins and mins + duration <= window[1]

        filtered = [s for s in slots if in_window(s)]
        if slots and not filtered:
            proposal["warnings"].append(f"No free times in the {parsed.get('time_of_day')}; showing other times that day.")
        else:
            slots = filtered
    picks = _pick_spread(list(slots), 3)
    proposal["slots"] = [
        {"start": s.start.isoformat(), "end": (s.start + dt.timedelta(minutes=duration)).isoformat(),
         "label": _fmt_dt(s.start, tz)} for s in picks
    ]
    if not picks:
        proposal["warnings"].append("No available times in that range. Try a wider date range.")
    name = (p.get("name") or "there").split()[0]
    lines = "".join(f"<li>{s['label']}</li>" for s in proposal["slots"])
    body = (
        f"<p>Hi {name},</p><p>Would any of these times work for a {duration}-minute {et.name}?</p>"
        f"<ul>{lines or '<li>(no times found)</li>'}</ul>"
        f"<p>Or pick any time that suits you here: <a href=\"{link}\">{link}</a></p>"
        f"<p>Best,<br>{user.name or user.email}</p>"
    )
    proposal["draft"] = {"to": p.get("email"), "subject": f"{et.name}: a few times that work", "html": body}
    return proposal


def execute(db: Session, req, user: User, action: str, slot_start: Optional[str] = None,
            to: Optional[str] = None, subject: Optional[str] = None, html: Optional[str] = None,
            cc: Optional[list] = None) -> str:
    """Execute an APPROVED assistant request. action = 'send' (email draft) | 'book' (book a proposed slot).
    Returns a human message. Raises ValueError on invalid input."""
    from app.services.messaging import send_email

    proposal = req.proposal_json or {}
    draft = proposal.get("draft") or {}
    if action == "send":
        to = (to or draft.get("to") or "").strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", to):
            raise ValueError("A valid recipient email is required to send the draft.")
        subj = subject or draft.get("subject") or "Meeting times"
        body = html or draft.get("html") or ""
        send_email(db, to, subj, body)
        cc = [c for c in (cc if cc is not None else draft.get("cc") or [])
              if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", c or "") and c != to]
        for c in cc[:10]:
            send_email(db, c, subj, body)
        msg = f"Email sent to {to}" + (f" (cc {', '.join(cc[:10])})." if cc else ".")
    elif action == "book":
        allowed = {s["start"] for s in proposal.get("slots", [])}
        if not slot_start or slot_start not in allowed:
            raise ValueError("Choose one of the proposed times (the assistant never books outside availability).")
        p = (req.parsed_json or {}).get("participant") or {}
        email = (to or p.get("email") or "").strip().lower()
        if not email:
            raise ValueError("The participant's email is required to book.")
        et = db.get(EventType, (proposal.get("event_type") or {}).get("id"))
        if not et or et.workspace_id != req.workspace_id:
            raise ValueError("Event type no longer available.")
        from app.services.booking import create_booking  # lazy

        guests = [g for g in (draft.get("cc") or []) if g != email][:10]
        b = create_booking(
            db, et, dt.datetime.fromisoformat(slot_start),
            {"name": p.get("name") or email.split("@")[0], "email": email, "timezone": user.default_timezone,
             "guests": guests},
            {}, timezone=user.default_timezone, duration=(proposal.get("event_type") or {}).get("duration"),
            idempotency_key=f"assistant:{req.id}:{slot_start}", source="assistant", actor=f"user:{user.id}",
        )
        proposal = dict(proposal)
        proposal["booking_id"] = getattr(b, "id", None)
        req.proposal_json = proposal
        msg = "Meeting booked and confirmation sent."
    else:
        raise ValueError("Unknown action")
    req.status = "executed"
    req.executed_at = utcnow()
    return msg


# ---------------------------------------------------------------- meeting history Q&A
def _my_bookings_query(workspace_id: str, user_id: str):
    return (
        select(Booking, Invitee)
        .join(Invitee, Invitee.booking_id == Booking.id)
        .where(
            Booking.workspace_id == workspace_id,
            Booking.status == "confirmed",
            or_(Booking.host_id == user_id,
                Booking.id.in_(select(BookingHost.booking_id).where(BookingHost.user_id == user_id))),
        )
    )


def _period(text: str, today: dt.date) -> tuple[Optional[dt.date], Optional[dt.date], str]:
    t = text.lower()
    if "this month" in t:
        return today.replace(day=1), (today.replace(day=1) + dt.timedelta(days=32)).replace(day=1), "this month"
    if "last month" in t:
        first_this = today.replace(day=1)
        first_last = (first_this - dt.timedelta(days=1)).replace(day=1)
        return first_last, first_this, "last month"
    if "this week" in t:
        s = today - dt.timedelta(days=today.weekday())
        return s, s + dt.timedelta(days=7), "this week"
    if "this year" in t:
        return dt.date(today.year, 1, 1), dt.date(today.year + 1, 1, 1), "this year"
    return None, None, "all time"


def answer_history(db: Session, user: User, workspace_id: str, text: str, now: Optional[dt.datetime] = None) -> dict:
    """Answer meeting-history questions from the user's own bookings. Returns {answer, rows, target}."""
    from app.deps import _fmt_dt

    now = now or utcnow()
    tz = user.default_timezone or "UTC"
    today = now.replace(tzinfo=dt.timezone.utc).astimezone(_tz(tz)).date()
    t = text.lower()
    # target: email, domain, or a name
    target_label, cond = None, None
    em = re.search(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", t)
    dom = re.search(r"\b((?:[a-z0-9-]+\.)+[a-z]{2,})\b", t) if not em else None
    if em:
        target_label = em.group(0)
        cond = lambda inv: inv.email.lower() == target_label  # noqa: E731
    elif dom and not re.fullmatch(r"\d+(\.\d+)*", dom.group(1)):
        d = dom.group(1)
        target_label = d
        cond = lambda inv: inv.email.lower().endswith("@" + d) or inv.email.lower().endswith("." + d)  # noqa: E731
    else:
        m = re.search(r"\b(?:with|meet|met|meeting)\s+(.+?)" + _STOP, text, flags=re.IGNORECASE)
        name = m.group(1).strip() if m else None
        if name and name.lower() not in ("me", "anyone", "everyone"):
            contacts = match_contacts(workspace_contacts(db, workspace_id), name, None)
            emails = {c.email_normalized for c in contacts}
            n = name.lower()
            target_label = contacts[0].name if len(contacts) == 1 else name
            cond = lambda inv: inv.email.lower() in emails or n in (inv.name or "").lower()  # noqa: E731
    p_start, p_end, p_label = _period(t, today)
    rows = []
    for b, inv in db.execute(_my_bookings_query(workspace_id, user.id).order_by(Booking.start_at)).all():
        if cond and not cond(inv):
            continue
        if p_start:
            loc = b.start_at.replace(tzinfo=dt.timezone.utc).astimezone(_tz(tz)).date()
            if not (p_start <= loc < p_end):
                continue
        rows.append((b, inv))
    who = f" with {target_label}" if target_label else ""
    past = [r for r in rows if r[0].start_at <= now]
    future = [r for r in rows if r[0].start_at > now]

    def label(r):
        b, inv = r
        et = db.get(EventType, b.event_type_id)
        return {"booking_id": b.id, "when": _fmt_dt(b.start_at, tz), "invitee": f"{inv.name} <{inv.email}>",
                "event": et.name if et else ""}

    if re.search(r"\bhow (many|often)\b", t):
        answer = f"You have {len(rows)} meeting{'s' if len(rows) != 1 else ''}{who} ({p_label})"
        if future and past:
            answer += f": {len(past)} past and {len(future)} upcoming"
        answer += "."
        shown = rows
    elif re.search(r"\b(next|upcoming)\b", t):
        if future:
            answer = f"Your next meeting{who} is {label(future[0])['when']} ({label(future[0])['event']})."
        else:
            answer = f"No upcoming meetings{who}."
        shown = future[:5]
    else:  # "when did I last meet", generic history
        if past:
            answer = f"You last met{who} on {label(past[-1])['when']} ({label(past[-1])['event']})."
        else:
            answer = f"No past meetings{who} found."
        if future:
            answer += f" Next one: {label(future[0])['when']}."
        shown = list(reversed(past))[:5] + future[:3]
    return {"answer": answer, "rows": [label(r) for r in shown[:10]], "target": target_label, "count": len(rows)}


# ---------------------------------------------------------------- schedule from email
EMAIL_RE_ANY = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_HEADER_RE = re.compile(r"^(from|to|cc|subject|date|sent|reply-to)\s*:\s*(.*)$", re.I)
_QUOTE_START = re.compile(
    r"^(on\s.+wrote:\s*$|-{2,}\s*original message\s*-{2,}|-{2,}\s*forwarded message\s*-{2,}|_{5,}|>)", re.I)
_SIGNOFF = re.compile(r"^(thanks|thank you|best|regards|cheers|kind regards|best regards|many thanks)\b[,!.]?\s*$", re.I)


def _addresses(value: str) -> list[tuple[Optional[str], str]]:
    """Parse 'Name <a@b.com>, c@d.com' into [(name, email)]."""
    out = []
    for part in re.split(r"[,;](?![^<]*>)", value or ""):
        em = EMAIL_RE_ANY.search(part)
        if not em:
            continue
        name = part[: em.start()].replace("<", "").replace('"', "").strip() or None
        out.append((name, em.group(0).lower()))
    return out


def parse_email_thread(text: str, user_email: Optional[str] = None) -> dict:
    """Split a pasted inbound email thread into headers + the latest message body.

    Accepts a raw thread ("From: ...\\nTo: ...\\nSubject: ...\\n\\nbody\\n\\nOn ... wrote:\\n> older").
    The latest message is the text before the first quote marker. Pure function."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    headers: dict[str, str] = {}
    i = 0
    # leading header block (stop at first blank line after headers or first non-header line)
    while i < len(lines):
        ln = lines[i].strip()
        m = _HEADER_RE.match(ln)
        if m and m.group(1).lower() not in headers:
            headers[m.group(1).lower()] = m.group(2).strip()
            i += 1
            continue
        if not ln and headers:
            i += 1
        break
    body_lines = []
    for ln in lines[i:]:
        s = ln.strip()
        if _QUOTE_START.match(s) or (_HEADER_RE.match(s) and s.lower().startswith("from:") and body_lines):
            break
        body_lines.append(ln.rstrip())
    body = "\n".join(body_lines).strip()
    me = (user_email or "").lower()
    sender = next(iter(_addresses(headers.get("from", "")) or _addresses(headers.get("reply-to", ""))), (None, None))
    if sender[1] == me:  # the user pasted their own outgoing mail: requester is the first recipient
        sender = next(iter(_addresses(headers.get("to", ""))), (None, None))
    others = []
    for key in ("to", "cc"):
        for n, e in _addresses(headers.get(key, "")):
            if e not in (me, sender[1]) and e not in [o["email"] for o in others]:
                others.append({"name": n, "email": e})
    # emails mentioned in the body ("please include dana@acme.com")
    for e in EMAIL_RE_ANY.findall(body):
        e = e.lower()
        if e not in (me, sender[1]) and e not in [o["email"] for o in others]:
            others.append({"name": None, "email": e})
    name = sender[0]
    if not name and body_lines:  # signature line after a sign-off
        for j, ln in enumerate(body_lines):
            if _SIGNOFF.match(ln.strip()) and j + 1 < len(body_lines) and body_lines[j + 1].strip():
                cand = body_lines[j + 1].strip()
                if len(cand.split()) <= 4 and not EMAIL_RE_ANY.search(cand):
                    name = cand
                break
    subject = headers.get("subject") or ""
    return {
        "subject": subject,
        "requester": {"name": name or (sender[1].split("@")[0].title() if sender[1] else None), "email": sender[1]},
        "participants": others[:10],
        "body": body[:4000],
        "headers": {k: v[:300] for k, v in headers.items()},
    }


def interpret_email(db: Session, user: User, workspace_id: str, thread: str, today: Optional[dt.date] = None) -> dict:
    """Extract the scheduling request from an email thread -> parsed dict (same shape as interpret())."""
    today = today or local_today(user.default_timezone)
    info = parse_email_thread(thread, user.email)
    contacts = workspace_contacts(db, workspace_id)
    ets = user_event_types(db, workspace_id, user.id)
    text = f"{info['subject']}\n{info['body']}"
    parsed = parse_request(text, today, contacts, ets)
    parsed = merge_llm(parsed, llm_extract(text, today), contacts)
    parsed["intent"] = "schedule"  # an inbound meeting request is always a scheduling task
    req = info["requester"]
    if req.get("email"):
        hits = match_contacts(contacts, None, req["email"])
        parsed["participant"] = {"name": (hits[0].name if hits and hits[0].name else req.get("name")) or req["email"],
                                 "email": req["email"], "contact_id": hits[0].id if hits else None}
        parsed["alternatives"] = []
        parsed["rationale"] += f"; requester {req['email']} taken from the email's From header"
        parsed["confidence"] = round(min(0.95, parsed["confidence"] + 0.1), 2)
    parsed["source"] = "email"
    parsed["email"] = info
    return parsed


def propose_email_reply(db: Session, user: User, workspace_id: str, parsed: dict) -> dict:
    """Find slots (availability service only) and draft a reply to the thread. Sends nothing."""
    proposal = propose(db, user, workspace_id, parsed)
    info = parsed.get("email") or {}
    p = parsed.get("participant") or {}
    subj = info.get("subject") or (proposal.get("event_type") or {}).get("name") or "Meeting"
    if not subj.lower().startswith("re:"):
        subj = "Re: " + subj
    first = (p.get("name") or "there").split()[0]
    dur = (proposal.get("event_type") or {}).get("duration")
    lines = "".join(f"<li>{s['label']}</li>" for s in proposal.get("slots", []))
    if proposal.get("slots"):
        middle = (f"<p>Thanks for reaching out - happy to meet{f' for {dur} minutes' if dur else ''}. "
                  f"Here are a few times that work on my side:</p><ul>{lines}</ul>"
                  "<p>Reply with the one that suits you best, or pick any open time here: ")
    else:
        middle = "<p>Thanks for reaching out. I couldn't find a free time in that window - please pick any open time here: "
    link = proposal.get("booking_link") or ""
    html = (f"<p>Hi {first},</p>{middle}<a href=\"{link}\">{link}</a></p>"
            f"<p>Best,<br>{user.name or user.email}</p>")
    if proposal.get("draft") is not None or link:
        proposal["draft"] = {"to": p.get("email"), "cc": [o["email"] for o in info.get("participants", [])],
                             "subject": subj[:200], "html": html}
    proposal["reply_to_thread"] = True
    return proposal


# ---------------------------------------------------------------- meeting prep
PREP_RE = re.compile(r"\b(prep|prepare|preparation|brief|briefing|context|get ready|agenda)\b", re.I)


def meeting_prep(db: Session, user: User, workspace_id: str, text: str, now: Optional[dt.datetime] = None) -> dict:
    """Read-only brief for the user's next meeting (optionally with a named person/email).
    Uses only bookings the user hosts, contacts in the workspace and the user's own recaps."""
    from app.deps import _fmt_dt
    from app.models import BookingAnswer, MeetingRecap

    now = now or utcnow()
    tz = user.default_timezone or "UTC"
    contacts = workspace_contacts(db, workspace_id)
    em = EMAIL_RE_ANY.search(text)
    pname, _ = extract_participant(text)
    if not pname:
        m = re.search(r"\b(?:for|about)\s+(?:my\s+)?(?:next\s+)?(?:meeting|call)?\s*(?:with\s+)?([A-Z][\w'-]+(?:\s+[A-Z][\w'-]+)?)",
                      text)
        pname = m.group(1) if m else None
    hits = match_contacts(contacts, pname, em.group(0).lower() if em else None) if (pname or em) else []
    emails = {c.email_normalized for c in hits} | ({em.group(0).lower()} if em else set())
    rows = db.execute(_my_bookings_query(workspace_id, user.id).order_by(Booking.start_at)).all()
    if emails or pname:
        n = (pname or "").lower()
        rows = [(b, i) for b, i in rows if i.email.lower() in emails or (n and n in (i.name or "").lower())]
    upcoming = [(b, i) for b, i in rows if b.start_at > now]
    if not upcoming:
        who = f" with {hits[0].name if hits else (pname or (em.group(0) if em else ''))}" if (pname or em) else ""
        return {"answer": f"No upcoming meetings{who} to prepare for.", "brief": None}
    b, inv = upcoming[0]
    et = db.get(EventType, b.event_type_id)
    contact = next((c for c in contacts if c.email_normalized == inv.email.lower()), None)
    past = [(pb, pi) for pb, pi in rows if pb.start_at <= now and pi.email.lower() == inv.email.lower()]
    answers = [{"q": a.label, "a": a.answer_json if isinstance(a.answer_json, str) else json.dumps(a.answer_json)}
               for a in db.scalars(select(BookingAnswer).where(BookingAnswer.booking_id == b.id)).all()]
    recap_rows = []
    if past:
        for r in db.scalars(select(MeetingRecap).where(
            MeetingRecap.workspace_id == workspace_id, MeetingRecap.owner_user_id == user.id,
            MeetingRecap.deleted_at.is_(None), MeetingRecap.booking_id.in_([pb.id for pb, _ in past]),
        ).order_by(MeetingRecap.created_at.desc()).limit(3)).all():
            recap_rows.append({"title": r.title, "summary": (r.summary or "")[:600],
                               "action_items": [a.get("text") for a in (r.action_items_json or [])][:5]})
    last = past[-1][0] if past else None
    brief = {
        "booking_id": b.id,
        "when": _fmt_dt(b.start_at, tz),
        "event": et.name if et else "",
        "invitee": {"name": inv.name, "email": inv.email, "company": inv.company or (contact.company if contact else None)},
        "contact": {"tags": (contact.tags_json or []) if contact else [], "notes": (contact.notes or "")[:600] if contact else "",
                    "timezone": (contact.timezone if contact else None) or inv.timezone},
        "past_meetings": len(past),
        "last_meeting": _fmt_dt(last.start_at, tz) if last else None,
        "last_notes": (last.host_notes or "")[:600] if last else "",
        "answers": answers[:10],
        "recaps": recap_rows,
        "location": (b.location_json or {}).get("join_url") or (b.location_json or {}).get("value"),
    }
    answer = (f"Next: {brief['event']} with {inv.name} on {brief['when']}. "
              + (f"You've met {len(past)} time{'s' if len(past) != 1 else ''} before (last {brief['last_meeting']})."
                 if past else "This is your first meeting with them."))
    return {"answer": answer, "brief": brief}


def prep_draft(user: User, brief: dict) -> dict:
    """Optional agenda email to the invitee - only sent after explicit approval."""
    inv = brief["invitee"]
    first = (inv.get("name") or "there").split()[0]
    items = "".join(f"<li>{a}</li>" for r in brief.get("recaps", []) for a in r.get("action_items", [])[:3])
    html = (f"<p>Hi {first},</p><p>Looking forward to our {brief['event']} on {brief['when']}.</p>"
            + (f"<p>Following up on open items from last time:</p><ul>{items}</ul>" if items else "")
            + "<p>Is there anything specific you'd like to cover?</p>"
            + f"<p>Best,<br>{user.name or user.email}</p>")
    return {"to": inv.get("email"), "subject": f"Agenda for our {brief['event']}", "html": html}
