"""Contacts (PRD 13): auto-create from bookings, dedupe by normalized email per workspace.

Normalization = strip + lowercase. Provider-specific tricks (gmail dots / +tags) are intentionally
NOT applied: they are different mailboxes on most providers and merging them would leak data.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.events import subscribe
from app.models import Booking, Contact, Invitee, OutboxEvent, RoutingSubmission, utcnow


def normalize_email(email: Optional[str]) -> str:
    return (email or "").strip().lower()


def find_contact(db: Session, workspace_id: str, email: str, include_deleted: bool = False) -> Optional[Contact]:
    q = select(Contact).where(Contact.workspace_id == workspace_id, Contact.email_normalized == normalize_email(email))
    if not include_deleted:
        q = q.where(Contact.deleted_at.is_(None))
    return db.scalar(q)


def upsert_contact(
    db: Session, workspace_id: str, email: str, *, name: str = "", phone: Optional[str] = None,
    company: Optional[str] = None, timezone: Optional[str] = None, source: str = "booking",
    owner_user_id: Optional[str] = None,
) -> Optional[Contact]:
    norm = normalize_email(email)
    if not norm or "@" not in norm:
        return None
    c = find_contact(db, workspace_id, norm, include_deleted=True)
    if c is None:
        c = Contact(workspace_id=workspace_id, email_normalized=norm, name=(name or "").strip()[:200],
                    phone=phone or None, company=company or None, timezone=timezone or None, source=source,
                    owner_user_id=owner_user_id, tags_json=[], custom_json={})
        db.add(c)
        db.flush()
        return c
    if c.deleted_at is not None:  # they booked again -> restore
        c.deleted_at = None
    if name and not c.name:
        c.name = name.strip()[:200]
    if phone:
        c.phone = phone
    if company:
        c.company = company
    if timezone:
        c.timezone = timezone
    if owner_user_id and not c.owner_user_id:
        c.owner_user_id = owner_user_id
    return c


@subscribe("booking.created")
def _contact_from_booking(db: Session, ev: OutboxEvent) -> None:
    booking_id = (ev.payload_json or {}).get("booking_id")
    b = db.get(Booking, booking_id) if booking_id else None
    if not b:
        return
    for inv in b.invitees:
        upsert_contact(db, b.workspace_id, inv.email, name=inv.name, phone=inv.phone, company=inv.company,
                       timezone=inv.timezone, source="booking", owner_user_id=b.host_id)
    if b.routing_submission_id:
        sub = db.get(RoutingSubmission, b.routing_submission_id)
        if sub and sub.workspace_id == b.workspace_id and not sub.booking_id:
            sub.booking_id = b.id
    db.flush()


def meeting_history(db: Session, workspace_id: str, email: str) -> list[Booking]:
    norm = normalize_email(email)
    return list(db.scalars(
        select(Booking).join(Invitee, Invitee.booking_id == Booking.id)
        .where(Booking.workspace_id == workspace_id, func.lower(Invitee.email) == norm)
        .order_by(Booking.start_at.desc())
    ).unique().all()) if norm else []


def search_contacts(db: Session, workspace_id: str, q: str = "", tag: str = "") -> list[Contact]:
    stmt = select(Contact).where(Contact.workspace_id == workspace_id, Contact.deleted_at.is_(None))
    if q:
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(or_(Contact.email_normalized.like(like), Contact.name.ilike(like),
                              Contact.company.ilike(like), Contact.phone.like(like)))
    rows = list(db.scalars(stmt.order_by(Contact.updated_at.desc()).limit(2000)).all())
    if tag:
        t = tag.strip().lower()
        rows = [c for c in rows if t in [x.lower() for x in (c.tags_json or [])]]
    return rows


def all_tags(db: Session, workspace_id: str) -> list[str]:
    tags: set[str] = set()
    for tj in db.scalars(select(Contact.tags_json).where(Contact.workspace_id == workspace_id,
                                                         Contact.deleted_at.is_(None))):
        tags.update(tj or [])
    return sorted(tags, key=str.lower)


def parse_tags(raw: str) -> list[str]:
    out: list[str] = []
    for t in (raw or "").replace(";", ",").split(","):
        t = t.strip()[:40]
        if t and t.lower() not in [x.lower() for x in out]:
            out.append(t)
    return out[:30]


ACTIVE = ("confirmed", "pending", "pending_payment")
COUNTED = ("confirmed", "pending", "pending_payment", "cancelled")  # excludes held/expired/failed/rescheduled


def _emails_with_active_booking(db: Session, workspace_id: str, emails: set[str]) -> set[str]:
    if not emails:
        return set()
    rows = db.scalars(
        select(func.lower(Invitee.email)).join(Booking, Booking.id == Invitee.booking_id)
        .where(Booking.workspace_id == workspace_id, Booking.status.in_(ACTIVE),
               func.lower(Invitee.email).in_(emails))
    ).all()
    return set(rows)


def _submission_email(s: RoutingSubmission) -> Optional[str]:
    return next((normalize_email(v) for v in (s.answers_json or {}).values()
                 if isinstance(v, str) and "@" in v and "." in v.split("@")[-1]), None)


def no_booking_followups(db: Session, workspace_id: str, days: int = 90) -> list[dict]:
    """P3 missed opportunities (PRD 13.1):
      * kind "routing":   routing submissions that captured an email but never converted to a booking;
      * kind "cancelled": invitees whose recent bookings were all cancelled and who have nothing active.
    People with any active/confirmed booking are excluded. Each item: email, kind, submission|booking, contact,
    last_followup (ContactFollowUp or None), at (datetime)."""
    from app.models_crm import ContactFollowUp

    since = utcnow() - dt.timedelta(days=days)
    candidates: dict[str, dict] = {}
    subs = db.scalars(select(RoutingSubmission).where(
        RoutingSubmission.workspace_id == workspace_id, RoutingSubmission.booking_id.is_(None),
        RoutingSubmission.created_at >= since).order_by(RoutingSubmission.created_at.desc()).limit(500)).all()
    for s in subs:
        email = _submission_email(s)
        if email and email not in candidates:
            candidates[email] = {"email": email, "kind": "routing", "submission": s, "booking": None, "at": s.created_at}
    cancelled = db.execute(
        select(Invitee, Booking).join(Booking, Booking.id == Invitee.booking_id)
        .where(Booking.workspace_id == workspace_id, Booking.status == "cancelled", Booking.created_at >= since)
        .order_by(Booking.cancelled_at.desc()).limit(500)
    ).all()
    for inv, b in cancelled:
        email = normalize_email(inv.email)
        if email and "@" in email and email not in candidates:
            candidates[email] = {"email": email, "kind": "cancelled", "submission": None, "booking": b,
                                 "at": b.cancelled_at or b.updated_at}
    booked = _emails_with_active_booking(db, workspace_id, set(candidates))
    contacts = {c.email_normalized: c for c in db.scalars(select(Contact).where(
        Contact.workspace_id == workspace_id, Contact.email_normalized.in_(set(candidates) or {""}),
        Contact.deleted_at.is_(None)))}
    last_fu: dict[str, object] = {}
    for fu in db.scalars(select(ContactFollowUp).where(
            ContactFollowUp.workspace_id == workspace_id,
            ContactFollowUp.email_normalized.in_(set(candidates) or {""})).order_by(ContactFollowUp.created_at)):
        last_fu[fu.email_normalized] = fu
    out = []
    for email, item in candidates.items():
        if email in booked:
            continue
        item["contact"] = contacts.get(email)
        item["last_followup"] = last_fu.get(email)
        out.append(item)
    out.sort(key=lambda i: i["at"] or utcnow(), reverse=True)
    return out


def _rate(n: int, d: int) -> Optional[float]:
    return round(100.0 * n / d, 1) if d else None


def _stats(bookings: list[Booking], now: dt.datetime, et_names: dict) -> dict:
    counted = [b for b in bookings if b.status in COUNTED]
    cancelled = [b for b in counted if b.status == "cancelled"]
    held_past = [b for b in counted if b.status == "confirmed" and b.start_at < now]
    no_shows = [b for b in held_past if b.no_show]
    upcoming = sorted((b for b in counted if b.status in ACTIVE and b.start_at >= now), key=lambda b: b.start_at)
    last = max((b for b in held_past if not b.no_show), key=lambda b: b.start_at, default=None)
    tally: dict[str, int] = {}
    for b in counted:
        if b.status != "cancelled":
            tally[b.event_type_id] = tally.get(b.event_type_id, 0) + 1
    top = sorted(tally.items(), key=lambda kv: (-kv[1], et_names.get(kv[0], "")))[:3]
    return {
        "meetings": len(counted) - len(cancelled), "booked_total": len(counted),
        "completed": len(held_past) - len(no_shows), "cancelled": len(cancelled), "no_shows": len(no_shows),
        "upcoming": len(upcoming), "cancel_rate": _rate(len(cancelled), len(counted)),
        "no_show_rate": _rate(len(no_shows), len(held_past)),
        "last_meeting": last.start_at if last else None, "last_booking_id": last.id if last else None,
        "next_meeting": upcoming[0].start_at if upcoming else None,
        "next_booking_id": upcoming[0].id if upcoming else None,
        "top_event_types": [{"event_type_id": k, "name": et_names.get(k, "Meeting"), "count": v} for k, v in top],
    }


def _et_names(db: Session, ids: set) -> dict:
    from app.models import EventType

    if not ids:
        return {}
    return dict(db.execute(select(EventType.id, EventType.name).where(EventType.id.in_(ids))).all())


def relationship_insights(db: Session, workspace_id: str, email: str) -> dict:
    """Per-contact insights: meeting count, last/next meeting, cancel & no-show rate, top event types."""
    history = meeting_history(db, workspace_id, email)
    return _stats(history, utcnow(), _et_names(db, {b.event_type_id for b in history}))


def company_insights(db: Session, workspace_id: str, limit: int = 200) -> list[dict]:
    """Same metrics aggregated per company (contact.company, else invitee.company, else email domain)."""
    contacts = {c.email_normalized: c for c in db.scalars(select(Contact).where(
        Contact.workspace_id == workspace_id, Contact.deleted_at.is_(None)))}
    rows = db.execute(
        select(Invitee.email, Invitee.company, Booking).join(Booking, Booking.id == Invitee.booking_id)
        .where(Booking.workspace_id == workspace_id).order_by(Booking.start_at.desc()).limit(20000)
    ).all()
    groups: dict[str, dict] = {}
    for email, inv_company, b in rows:
        e = normalize_email(email)
        c = contacts.get(e)
        company = ((c.company if c else None) or inv_company or "").strip()
        if not company:
            continue
        g = groups.setdefault(company.lower(), {"company": company, "bookings": {}, "people": set()})
        g["bookings"][b.id] = b
        g["people"].add(e)
    names = _et_names(db, {b.event_type_id for g in groups.values() for b in g["bookings"].values()})
    now = utcnow()
    out = []
    for g in groups.values():
        st = _stats(list(g["bookings"].values()), now, names)
        out.append({"company": g["company"], "contacts": len(g["people"]), **st})
    out.sort(key=lambda r: (-r["meetings"], r["company"].lower()))
    return out[:limit]
