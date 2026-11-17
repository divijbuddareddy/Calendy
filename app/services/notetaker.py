"""Notetaker / meeting recaps (PRD 16.2).

* Transcripts are stored ENCRYPTED at rest (security.encrypt); only decrypted in memory.
* Recap generation is deterministic (heuristics) with optional Anthropic API refinement.
* The meeting bot goes through a recording-provider adapter (app.services.recording): Recall.ai-style
  when RECALL_API_KEY is set, otherwise the offline simulation. The bot joins as a visible participant
  named "<Workspace> Notetaker"; participants are emailed a recording/consent notice before recording.
  Status: none -> joining -> recording -> stopped -> processed.
* Retention: `retention_until` (default 90 days) purged by the `recap_retention` periodic task
  (transcript/summary wiped immediately; tombstone rows hard-deleted after TOMBSTONE_DAYS).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
from collections import Counter
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Booking, BookingHost, MeetingRecap, User, Workspace, utcnow
from app.models_notetaker import NotetakerBot
from app.security import decrypt, encrypt
from app.services import recording
from app.worker import periodic

log = logging.getLogger("notetaker")
DEFAULT_RETENTION_DAYS = 90
STOPWORDS = set(
    "a an the and or but if then so to of in on at for with from by is are was were be been being it this that these those "
    "i you he she we they me him her us them my your our their its as do does did have has had not no yes ok okay "
    "will would can could should just about what when where who why how which there here also very really um uh like "
    "going get got know think yeah right well".split()
)
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


# ---------------------------------------------------------------- transcript handling
def set_transcript(recap: MeetingRecap, text: Optional[str]) -> None:
    recap.transcript = encrypt(text) if text else None


def get_transcript(recap: MeetingRecap) -> str:
    return decrypt(recap.transcript) or "" if recap.transcript else ""


def normalize_transcript(raw: str) -> str:
    """Accept plain text or WebVTT; strip WEBVTT header, cue numbers and timestamps; '<v Name>' -> 'Name:'."""
    out = []
    for line in (raw or "").replace("\r\n", "\n").split("\n"):
        s = line.strip()
        if not s or s.upper().startswith("WEBVTT") or s.isdigit() or s.startswith("NOTE"):
            continue
        if re.match(r"^(\d{1,2}:)?\d{2}:\d{2}[.,]\d{3}\s*-->", s):
            continue
        m = re.match(r"^<v\s+([^>]+)>(.*?)(</v>)?$", s)
        if m:
            s = f"{m.group(1).strip()}: {m.group(2).strip()}"
        out.append(s)
    return "\n".join(out)


def _lines(text: str) -> list[tuple[Optional[str], str]]:
    res = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^([A-Z][\w .'-]{0,40}?):\s+(.*)$", line)
        res.append((m.group(1).strip(), m.group(2).strip()) if m else (None, line))
    return res


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip()) > 3]


def _words(s: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9']+", s.lower()) if w not in STOPWORDS and len(w) > 2]


def parse_due(text: str, base: dt.date) -> Optional[str]:
    t = text.lower()
    m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", t)
    if m:
        return m.group(1)
    if re.search(r"\b(by |before )?(end of (the )?day|eod|today)\b", t):
        return base.isoformat()
    if "tomorrow" in t:
        return (base + dt.timedelta(days=1)).isoformat()
    if re.search(r"\b(end of (the )?week|eow|this week)\b", t):
        return (base + dt.timedelta(days=(4 - base.weekday()) % 7)).isoformat()
    if "next week" in t:
        return (base + dt.timedelta(days=7 - base.weekday())).isoformat()
    m = re.search(r"\b(?:by|on|before|until)\s+(?:next\s+)?(" + "|".join(WEEKDAYS) + r")\b", t)
    if m:
        wd = WEEKDAYS.index(m.group(1))
        delta = (wd - base.weekday()) % 7 or 7
        return (base + dt.timedelta(days=delta)).isoformat()
    return None


ACTION_RE = re.compile(
    r"\b(action item|todo|to-do|to do:|follow up|follow-up|i will|i'll|we will|we'll|will send|will share|will prepare|"
    r"needs to|need to|is going to|please|can you|could you|assign(?:ed)? to)\b", re.I)
REQUEST_RE = re.compile(r"\b(can|could|would) you\b", re.I)
DECISION_RE = re.compile(r"\b(decided|decision|agreed|we agree|let's go with|lets go with|approved|final answer|we will go|settled on)\b", re.I)


def _owner_for(speaker: Optional[str], sentence: str, participants: list[str]) -> Optional[str]:
    s = sentence.strip()
    m = re.search(r"@(\w+)", s)
    if m:
        return m.group(1)
    m = re.search(r"assign(?:ed)? to\s+([A-Z]\w+)", s)
    if m:
        return m.group(1)
    m = re.match(r"^(?:ok(?:ay)?,?\s+)?([A-Z]\w+)(?:,)?\s+(?:will|can you|could you|needs to|is going to|please)\b", s)
    if m and m.group(1).lower() not in STOPWORDS and m.group(1) not in ("I", "We"):
        return m.group(1)
    m = re.search(r"\b(?:can|could) you\b", s, re.I)
    if m:
        others = [p for p in participants if p != speaker]
        for p in others:
            if p.split()[0].lower() in s.lower():
                return p
        return others[0] if len(others) == 1 else None
    if re.search(r"\b(i will|i'll)\b", s, re.I):
        return speaker
    if re.search(r"\b(we will|we'll)\b", s, re.I):
        return "Team"
    return speaker


def generate_recap(transcript: str, base_date: Optional[dt.date] = None) -> dict:
    """Deterministic summary / decisions / action items extraction."""
    base_date = base_date or utcnow().date()
    lines = _lines(transcript)
    participants = []
    for sp, _ in lines:
        if sp and sp not in participants:
            participants.append(sp)
    sentences: list[tuple[Optional[str], str]] = []
    for sp, text in lines:
        for s in _sentences(text) or [text]:
            sentences.append((sp, s))
    freq = Counter(w for _, s in sentences for w in _words(s))
    topics = [w for w, _ in freq.most_common(6)]
    decisions, actions = [], []
    for sp, s in sentences:
        if DECISION_RE.search(s):
            decisions.append(s if not sp else f"{s} ({sp})")
        elif ACTION_RE.search(s) and (not s.endswith("?") or REQUEST_RE.search(s)):
            text = re.sub(r"^(action item|todo|to-do|to do)\s*[:\-]\s*", "", s, flags=re.I)
            actions.append({"text": text, "owner": _owner_for(sp, s, participants), "due": parse_due(s, base_date)})
    # summary: top-scoring sentences by keyword density, in original order
    scored = []
    for i, (sp, s) in enumerate(sentences):
        ws = _words(s)
        if len(ws) < 3:
            continue
        scored.append((sum(freq[w] for w in ws) / (len(ws) ** 0.5), i, s))
    top = sorted(sorted(scored, reverse=True)[:3], key=lambda x: x[1])
    summary_parts = []
    if participants:
        summary_parts.append(f"Participants: {', '.join(participants)}.")
    if topics:
        summary_parts.append(f"Main topics: {', '.join(topics[:5])}.")
    summary_parts.extend(s for _, _, s in top)
    return {
        "summary": " ".join(summary_parts) or "No content to summarise.",
        "decisions": decisions[:15],
        "action_items": actions[:25],
        "participants": participants,
    }


def llm_recap(transcript: str, timeout: float = 20.0, api_key: Optional[str] = None) -> Optional[dict]:
    from app.services import ai_key

    api_key = api_key if api_key is not None else settings.anthropic_api_key
    if not api_key:
        return None
    try:
        import httpx

        r = httpx.post(
            f"{ai_key.API_BASE}/messages",
            headers=ai_key.headers(api_key),
            json={"model": ai_key.MODEL, "max_tokens": 1200,
                  "system": "Summarise the meeting transcript. Reply with JSON only: {\"summary\": str, \"decisions\": [str], "
                            "\"action_items\": [{\"text\": str, \"owner\": str|null, \"due\": \"YYYY-MM-DD\"|null}]}",
                  "messages": [{"role": "user", "content": transcript[:60000]}]},
            timeout=timeout,
        )
        r.raise_for_status()
        body = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        m = re.search(r"\{.*\}", body, flags=re.S)
        data = json.loads(m.group(0)) if m else None
        if data and isinstance(data.get("summary"), str):
            return data
    except Exception as exc:
        log.warning("LLM recap failed, using heuristics: %s", exc)
    return None


def apply_recap(recap: MeetingRecap, base_date: Optional[dt.date] = None) -> None:
    text = get_transcript(recap)
    from sqlalchemy.orm import object_session

    from app.services.ai_key import effective_key

    key = effective_key(object_session(recap), recap.workspace_id)
    data = llm_recap(text, api_key=key) or generate_recap(text, base_date)
    recap.summary = data.get("summary")
    recap.decisions_json = list(data.get("decisions") or [])
    recap.action_items_json = list(data.get("action_items") or [])
    recap.recording_status = "processed"


# ---------------------------------------------------------------- search / Q&A
def ask(transcript: str, question: str, k: int = 4) -> list[str]:
    """Keyword retrieval: lines of the transcript most relevant to the question."""
    q = set(_words(question))
    if not q:
        return []
    scored = []
    for i, line in enumerate(l for l in transcript.split("\n") if l.strip()):
        lw = _words(line)
        hits = sum(1 for w in lw if w in q or any(w.startswith(x[:5]) for x in q if len(x) >= 5))
        if hits:
            scored.append((hits / (len(set(lw)) ** 0.3 or 1), i, line.strip()))
    top = sorted(scored, reverse=True)[:k]
    return [s for _, _, s in sorted(top, key=lambda x: x[1])]


def search(db: Session, workspace_id: str, query: str, user_id: Optional[str] = None) -> list[dict]:
    q = (query or "").strip().lower()
    if not q:
        return []
    stmt = select(MeetingRecap).where(MeetingRecap.workspace_id == workspace_id, MeetingRecap.deleted_at.is_(None))
    if user_id:
        stmt = stmt.where(MeetingRecap.owner_user_id == user_id)
    results = []
    for r in db.scalars(stmt.order_by(MeetingRecap.created_at.desc())):
        text = get_transcript(r)
        hits = [l.strip() for l in text.split("\n") if q in l.lower()]
        if hits or q in (r.title or "").lower() or q in (r.summary or "").lower():
            results.append({"recap": r, "hits": hits[:5], "count": len(hits)})
    return results


# ---------------------------------------------------------------- retention
def default_retention() -> dt.datetime:
    return utcnow() + dt.timedelta(days=DEFAULT_RETENTION_DAYS)


TOMBSTONE_DAYS = 30


def purge_expired(db: Session, now: Optional[dt.datetime] = None) -> int:
    """Wipe recaps past retention (returns count) and hard-delete old tombstones + their bot rows."""
    now = now or utcnow()
    rows = db.scalars(
        select(MeetingRecap).where(MeetingRecap.retention_until.is_not(None), MeetingRecap.retention_until <= now,
                                   MeetingRecap.deleted_at.is_(None))
    ).all()
    for r in rows:
        wipe(r, now)
    old = db.scalars(select(MeetingRecap).where(
        MeetingRecap.deleted_at.is_not(None), MeetingRecap.deleted_at <= now - dt.timedelta(days=TOMBSTONE_DAYS))).all()
    for r in old:
        for b in db.scalars(select(NotetakerBot).where(NotetakerBot.recap_id == r.id)).all():
            db.delete(b)
        db.delete(r)
    db.flush()
    return len(rows)


def wipe(r: MeetingRecap, now: Optional[dt.datetime] = None) -> None:
    r.transcript = None
    r.summary = None
    r.decisions_json = None
    r.action_items_json = None
    r.share_token = None
    r.share_password_hash = None
    r.deleted_at = now or utcnow()


@periodic("recap_retention")
def _recap_retention_task(db: Session):
    purge_expired(db)


# ---------------------------------------------------------------- meeting bot
ACTIVE_BOT = ("joining", "in_call", "recording", "stopped")


def bot_name_for(ws: Workspace) -> str:
    return f"{(ws.name or 'AAL').strip()[:60]} Notetaker"


def get_bot(db: Session, recap: MeetingRecap) -> Optional[NotetakerBot]:
    return db.scalar(select(NotetakerBot).where(NotetakerBot.recap_id == recap.id))


def meeting_url_for(booking: Optional[Booking]) -> Optional[str]:
    loc = (booking.location_json or {}) if booking else {}
    for key in ("join_url", "value"):
        v = str(loc.get(key) or "")
        if v.startswith("https://"):
            return v
    return None


def participants(db: Session, booking: Optional[Booking]) -> list[tuple[str, str]]:
    """(name, email) for invitees, guests and hosts of a booking (deduplicated)."""
    if not booking:
        return []
    out: dict[str, str] = {}
    for inv in booking.invitees:
        if inv.email and "redacted.invalid" not in inv.email:
            out.setdefault(inv.email.lower(), inv.name or inv.email)
        for g in inv.guests_json or []:
            if isinstance(g, str) and "@" in g:
                out.setdefault(g.lower(), g)
    host_ids = {booking.host_id} | set(db.scalars(select(BookingHost.user_id).where(BookingHost.booking_id == booking.id)))
    for u in db.scalars(select(User).where(User.id.in_(host_ids))).all():
        out.setdefault(u.email.lower(), u.name or u.email)
    return [(n, e) for e, n in out.items()]


def send_consent_notice(db: Session, recap: MeetingRecap, bot: NotetakerBot, booking: Optional[Booking],
                        organiser: str, ws: Workspace) -> int:
    """Email every participant that a visible notetaker will record/transcribe (sent BEFORE recording)."""
    from app.services.messaging import send_email

    n = 0
    for name, email in participants(db, booking):
        send_email(
            db, email, f"Recording notice: {recap.title}",
            f"<p>Hi {name},</p><p>{organiser} has added the {ws.name} notetaker to <strong>{recap.title}</strong>. "
            f"A visible participant named <strong>{bot.bot_name}</strong> will join the call, and the meeting will be "
            f"recorded and transcribed only while its recording indicator is shown. The transcript is stored encrypted "
            f"and deleted after the retention period.</p><p>If you do not consent, reply to the organiser or ask them "
            f"to remove the bot or stop recording at any time.</p>",
        )
        n += 1
    bot.consent_sent_at = utcnow()
    bot.consent_recipients = n
    return n


def add_bot(db: Session, recap: MeetingRecap, booking: Optional[Booking], ws: Workspace, organiser: str,
            meeting_url: Optional[str] = None) -> NotetakerBot:
    """Create the bot row, notify participants, then ask the provider to join the meeting."""
    adapter = recording.get_adapter()
    bot = NotetakerBot(workspace_id=ws.id, recap_id=recap.id, provider=adapter.name, bot_name=bot_name_for(ws),
                       meeting_url=meeting_url or meeting_url_for(booking), status="scheduled")
    db.add(bot)
    db.flush()
    send_consent_notice(db, recap, bot, booking, organiser, ws)
    try:
        st = adapter.create_bot(bot.meeting_url, bot.bot_name, {"recap_id": recap.id, "workspace_id": ws.id})
        bot.external_bot_id = st.bot_id
        bot.status = "joining" if st.status in ("joining", "unknown") else st.status
        bot.status_detail = st.detail[:300] if st.detail else None
        recap.recording_status = "joining"
    except Exception as exc:
        bot.status = "failed"
        bot.last_error = str(exc)[:2000]
        log.warning("notetaker bot create failed: %s", exc)
    return bot


def bot_action(db: Session, recap: MeetingRecap, bot: Optional[NotetakerBot], action: str) -> None:
    """start | stop | remove. Raises recording.RecordingError on provider failure."""
    adapter = recording.get_adapter(bot.provider if bot else "mock")
    ext = bot.external_bot_id if bot else None
    if action == "start":
        if ext:
            adapter.start_recording(ext)
        recap.recording_status = "recording"
        if bot:
            bot.status, bot.recording_started_at = "recording", utcnow()
    elif action == "stop":
        if ext:
            adapter.stop_recording(ext)
        recap.recording_status = "stopped"
        if bot:
            bot.status, bot.recording_stopped_at = "stopped", utcnow()
    elif action == "remove":
        if ext:
            adapter.remove_bot(ext)
        recap.recording_status = "stopped"
        if bot:
            bot.status = "removed"
            bot.recording_stopped_at = bot.recording_stopped_at or utcnow()
    else:
        raise ValueError(action)


def sync_bot(db: Session, bot: NotetakerBot) -> None:
    """Refresh provider status; when the call is done, fetch the transcript (encrypted) and build the recap."""
    recap = db.get(MeetingRecap, bot.recap_id)
    if not recap or recap.deleted_at or not bot.external_bot_id:
        return
    adapter = recording.get_adapter(bot.provider)
    st = adapter.get_bot(bot.external_bot_id)
    if st.status != "unknown":
        if bot.status != "removed":
            bot.status = st.status
        bot.status_detail = (st.detail or "")[:300]
        if st.status == "recording":
            recap.recording_status = "recording"
            bot.recording_started_at = bot.recording_started_at or utcnow()
        elif st.status in ("stopped", "done") and recap.recording_status in ("joining", "recording", "none"):
            recap.recording_status = "stopped"
    if bot.status in ("done", "stopped", "removed") and not bot.transcript_fetched_at:
        text = adapter.fetch_transcript(bot.external_bot_id)
        if text:
            set_transcript(recap, normalize_transcript(text))
            bot.transcript_fetched_at = utcnow()
            apply_recap(recap)


@periodic("notetaker_bots")
def _notetaker_bot_task(db: Session):
    for bot in db.scalars(select(NotetakerBot).where(
        NotetakerBot.provider != "mock", NotetakerBot.transcript_fetched_at.is_(None),
        NotetakerBot.status.in_(ACTIVE_BOT + ("done", "removed")),
        NotetakerBot.created_at >= utcnow() - dt.timedelta(days=3),  # give up polling stale bots
    ).limit(50)).all():
        try:
            sync_bot(db, bot)
        except Exception as exc:
            bot.last_error = str(exc)[:2000]
            log.warning("notetaker bot sync failed: %s", exc)
