"""Contacts (PRD 13). Auto-created from bookings by app.services.contacts (outbox subscriber).

  GET  /app/contacts                         search (?q=) + tag filter (?tag=)
  POST /app/contacts                         manual create (member+)
  GET  /app/contacts/export.csv              CSV export
  GET  /app/contacts/follow-ups              P3: routing submissions / cancelled-only invitees with no active booking
  POST /app/contacts/follow-ups/send         one-click follow-up email (respects unsubscribe)
  GET  /app/contacts/insights                P3 relationship insights per company (+ per contact on the profile)
  GET  /app/contacts/crm                     CRM connection (mock / Salesforce / HubSpot), mock records, activity log
  POST /app/contacts/crm/connect | /disconnect | /settings | /lookup
  GET  /app/contacts/crm/{provider}/callback OAuth callback (Salesforce / HubSpot)
  POST /app/contacts/crm/records | /records/{id}/delete | /activities/{id}/retry
  GET  /app/contacts/{id}                    profile + meeting history timeline
  POST /app/contacts/{id}/edit | /custom | /custom/delete | /unsubscribe | /delete
  GET  /app/contacts/{id}/book?event_type_id=  open booking page prefilled with name/email
"""
from __future__ import annotations

import csv
import io
import re
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.audit import audit
from app.config import settings
from app.db import get_db
from app.deps import Ctx, Forbidden, flash, get_ctx, redirect, render
from app.security import opaque_token
from app.models import Contact, EventType, RoutingForm, RoutingSubmission, Team, User, utcnow
from app.services import contacts as svc  # registers the booking.created subscriber
from app.services import crm as crm_svc  # registers the CRM activity outbox subscriber
from app.models_crm import ContactFollowUp, CrmActivity, CrmRecord

router = APIRouter()
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _ctx(ctx: Ctx = Depends(get_ctx)) -> Ctx:
    if not ctx.feature("contacts"):
        raise Forbidden("Contacts are not included in your plan.")
    return ctx


def _writer(ctx: Ctx = Depends(_ctx)) -> Ctx:
    if ctx.role == "viewer":
        raise Forbidden("Viewers have read-only access to contacts.")
    return ctx


def _get(db: Session, ctx: Ctx, cid: str) -> Contact:
    c = db.get(Contact, cid)
    if not c or c.workspace_id != ctx.workspace.id or c.deleted_at:
        raise Forbidden("Contact not found.")
    return c


def booking_url(db: Session, et: EventType) -> Optional[str]:
    if et.team_id:
        team = db.get(Team, et.team_id)
        return f"{settings.base_url}/team/{team.slug}/{et.slug}" if team else None
    owner = db.get(User, et.owner_user_id)
    return f"{settings.base_url}/{owner.slug}/{et.slug}" if owner and owner.slug else None


def _bookable_events(db: Session, ctx: Ctx) -> list[EventType]:
    return list(db.scalars(select(EventType).where(
        EventType.workspace_id == ctx.workspace.id, EventType.deleted_at.is_(None), EventType.status == "active",
        EventType.is_managed_template.is_(False), EventType.is_one_off.is_(False), EventType.single_use.is_(False),
        or_(EventType.owner_user_id == ctx.user.id, EventType.team_id.is_not(None))).order_by(EventType.name)).all())


def _csv_cell(v) -> str:
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


# ================================================================ list / create / export
@router.get("/app/contacts")
def contacts_list(request: Request, q: str = "", tag: str = "", ctx: Ctx = Depends(_ctx),
                  db: Session = Depends(get_db)):
    rows = svc.search_contacts(db, ctx.workspace.id, q, tag)
    return render(request, "app/contacts/list.html", ctx, nav="contacts", contacts=rows[:500], total=len(rows), q=q,
                  tag=tag, tags=svc.all_tags(db, ctx.workspace.id))


@router.post("/app/contacts")
async def contacts_create(request: Request, ctx: Ctx = Depends(_writer), db: Session = Depends(get_db)):
    form = await request.form()
    email = svc.normalize_email(form.get("email"))
    if not EMAIL_RE.match(email):
        flash(request, "Enter a valid email address.", "error")
        return redirect("/app/contacts")
    existing = svc.find_contact(db, ctx.workspace.id, email)
    if existing:
        flash(request, "A contact with that email already exists.", "info")
        return redirect(f"/app/contacts/{existing.id}")
    c = svc.upsert_contact(db, ctx.workspace.id, email, name=(form.get("name") or "").strip(),
                           phone=(form.get("phone") or "").strip()[:50] or None,
                           company=(form.get("company") or "").strip()[:200] or None, source="manual",
                           owner_user_id=ctx.user.id)
    c.tags_json = svc.parse_tags(form.get("tags") or "")
    audit(db, "contact.created", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="contact",
          entity_id=c.id)
    db.commit()
    flash(request, "Contact created.")
    return redirect(f"/app/contacts/{c.id}")


@router.get("/app/contacts/export.csv")
def contacts_export(request: Request, q: str = "", tag: str = "", ctx: Ctx = Depends(_ctx),
                    db: Session = Depends(get_db)):
    rows = svc.search_contacts(db, ctx.workspace.id, q, tag)
    keys = sorted({k for c in rows for k in (c.custom_json or {})})
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["email", "name", "phone", "company", "timezone", "tags", "unsubscribed", "source", "notes",
                "created_at_utc"] + [f"custom:{k}" for k in keys])
    for c in rows:
        w.writerow([_csv_cell(x) for x in [c.email_normalized, c.name, c.phone, c.company, c.timezone,
                                           ";".join(c.tags_json or []), "yes" if c.unsubscribed else "no", c.source,
                                           c.notes, c.created_at.isoformat(timespec="seconds")]
                    + [(c.custom_json or {}).get(k, "") for k in keys]])
    audit(db, "contacts.exported", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="contact",
          metadata={"count": len(rows)})
    db.commit()
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="contacts.csv"'})


@router.get("/app/contacts/follow-ups")
def followups(request: Request, ctx: Ctx = Depends(_ctx), db: Session = Depends(get_db)):
    items = svc.no_booking_followups(db, ctx.workspace.id)
    forms = {f.id: f for f in db.scalars(select(RoutingForm).where(RoutingForm.workspace_id == ctx.workspace.id))}
    return render(request, "app/contacts/followups.html", ctx, nav="contacts", items=items, forms=forms,
                  events=_bookable_events(db, ctx))


@router.post("/app/contacts/follow-ups/send")
async def followup_send(request: Request, ctx: Ctx = Depends(_writer), db: Session = Depends(get_db)):
    from html import escape

    from app.services.messaging import send_email

    form = await request.form()
    email = svc.normalize_email(form.get("email"))
    items = {i["email"]: i for i in svc.no_booking_followups(db, ctx.workspace.id)}
    item = items.get(email)
    if not item:
        flash(request, "That person is no longer a follow-up opportunity.", "info")
        return redirect("/app/contacts/follow-ups")
    contact = item.get("contact") or svc.find_contact(db, ctx.workspace.id, email, include_deleted=True)
    if contact and contact.unsubscribed:
        flash(request, "This contact has unsubscribed; no email was sent.", "error")
        return redirect("/app/contacts/follow-ups")
    link = None
    et_id = form.get("event_type_id")
    if et_id:
        et = db.get(EventType, et_id)
        if et and et.workspace_id == ctx.workspace.id and not et.deleted_at:
            link = booking_url(db, et)
    if not link and ctx.user.slug:
        link = f"{settings.base_url}/{ctx.user.slug}"
    if link:
        name = (contact.name if contact else "") or ""
        link += "?" + urlencode({k: v for k, v in {"name": name, "email": email}.items() if v})
    who = escape((contact.name if contact and contact.name else "there").split(" ")[0])
    note = (form.get("message") or "").strip()[:2000]
    reason = ("you recently reached out to us" if item["kind"] == "routing"
              else "your earlier meeting with us was cancelled")
    html = (f"<p>Hi {who},</p><p>I noticed {reason} but we haven't found a time to talk yet.</p>"
            + (f"<p>{escape(note)}</p>" if note else "")
            + (f'<p><a href="{escape(link)}">Pick a time that works for you</a></p>' if link else "")
            + f"<p>Best,<br>{escape(ctx.user.name or ctx.user.email)}</p>")
    msg = send_email(db, email, f"Following up from {ctx.user.name or ctx.workspace.name}", html)
    src = item["submission"].id if item["submission"] else (item["booking"].id if item["booking"] else None)
    db.add(ContactFollowUp(workspace_id=ctx.workspace.id, email_normalized=email, source=item["kind"], source_id=src,
                           sent_by=ctx.user.id, message_id=msg.id))
    audit(db, "contact.followup_sent", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="contact",
          entity_id=contact.id if contact else None, metadata={"kind": item["kind"]})
    db.commit()
    flash(request, f"Follow-up email sent to {email}.")
    return redirect("/app/contacts/follow-ups")


@router.get("/app/contacts/insights")
def insights_page(request: Request, ctx: Ctx = Depends(_ctx), db: Session = Depends(get_db)):
    rows = svc.company_insights(db, ctx.workspace.id)
    if "application/json" in request.headers.get("accept", ""):
        return {"companies": [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()}
                              for r in rows]}
    return render(request, "app/contacts/insights.html", ctx, nav="contacts", rows=rows)


# ================================================================ CRM
def _crm_admin(ctx: Ctx = Depends(_ctx)) -> Ctx:
    if not ctx.can("admin"):
        raise Forbidden("Only workspace admins can manage the CRM connection.")
    return ctx


@router.get("/app/contacts/crm")
def crm_page(request: Request, ctx: Ctx = Depends(_ctx), db: Session = Depends(get_db)):
    conn = crm_svc.get_connection(db, ctx.workspace.id)
    records = db.scalars(select(CrmRecord).where(CrmRecord.workspace_id == ctx.workspace.id)
                         .order_by(CrmRecord.email_normalized).limit(500)).all()
    acts = db.scalars(select(CrmActivity).where(CrmActivity.workspace_id == ctx.workspace.id)
                      .order_by(CrmActivity.created_at.desc()).limit(100)).all()
    return render(request, "app/contacts/crm.html", ctx, nav="contacts", conn=conn, records=records, activities=acts,
                  providers=[(p, crm_svc.PROVIDER_LABELS[p], crm_svc.provider_available(p)) for p in crm_svc.PROVIDERS],
                  is_admin=ctx.can("admin"))


@router.post("/app/contacts/crm/connect")
async def crm_connect(request: Request, ctx: Ctx = Depends(_crm_admin), db: Session = Depends(get_db)):
    form = await request.form()
    provider = form.get("provider")
    if provider not in crm_svc.PROVIDERS or not crm_svc.provider_available(provider):
        flash(request, "That CRM isn't configured on this server (set its client id/secret in .env).", "error")
        return redirect("/app/contacts/crm")
    if provider == "mock":
        crm_svc.connect(db, ctx.workspace.id, "mock", user_id=ctx.user.id)
    elif provider == "hubspot" and crm_svc._env("HUBSPOT_ACCESS_TOKEN") and not crm_svc._env("HUBSPOT_CLIENT_ID"):
        crm_svc.connect(db, ctx.workspace.id, "hubspot", user_id=ctx.user.id, label="HubSpot (private app)")
    else:
        state = opaque_token(16)
        request.session["crm_oauth"] = {"state": state, "provider": provider}
        cls = crm_svc.SalesforceCrm if provider == "salesforce" else crm_svc.HubSpotCrm
        return redirect(cls.authorize_url(state))
    audit(db, "crm.connected", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="crm_connection",
          metadata={"provider": provider})
    db.commit()
    flash(request, f"{crm_svc.PROVIDER_LABELS[provider]} connected.")
    return redirect("/app/contacts/crm")


@router.get("/app/contacts/crm/{provider}/callback")
def crm_callback(request: Request, provider: str, code: str = "", state: str = "", error: str = "",
                 ctx: Ctx = Depends(_crm_admin), db: Session = Depends(get_db)):
    pending = request.session.pop("crm_oauth", None) or {}
    if (error or not code or provider not in ("salesforce", "hubspot") or pending.get("provider") != provider
            or not state or pending.get("state") != state):
        flash(request, "CRM connection was cancelled or could not be verified.", "error")
        return redirect("/app/contacts/crm")
    cls = crm_svc.SalesforceCrm if provider == "salesforce" else crm_svc.HubSpotCrm
    try:
        tok = cls.exchange_code(code)
    except Exception as exc:
        flash(request, f"Could not connect: {exc}", "error")
        return redirect("/app/contacts/crm")
    crm_svc.connect(db, ctx.workspace.id, provider, user_id=ctx.user.id, access_token=tok["access_token"],
                    refresh_token=tok.get("refresh_token"), instance_url=tok.get("instance_url"), label=tok.get("label"))
    audit(db, "crm.connected", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="crm_connection",
          metadata={"provider": provider})
    db.commit()
    flash(request, f"{crm_svc.PROVIDER_LABELS[provider]} connected.")
    return redirect("/app/contacts/crm")


@router.post("/app/contacts/crm/disconnect")
def crm_disconnect(request: Request, ctx: Ctx = Depends(_crm_admin), db: Session = Depends(get_db)):
    if crm_svc.disconnect(db, ctx.workspace.id):
        audit(db, "crm.disconnected", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="crm_connection")
        db.commit()
        flash(request, "CRM disconnected. Stored tokens were deleted.")
    return redirect("/app/contacts/crm")


@router.post("/app/contacts/crm/settings")
async def crm_settings(request: Request, ctx: Ctx = Depends(_crm_admin), db: Session = Depends(get_db)):
    conn = crm_svc.get_connection(db, ctx.workspace.id)
    if conn:
        form = await request.form()
        conn.settings_json = {"push_bookings": bool(form.get("push_bookings")),
                              "push_routing": bool(form.get("push_routing"))}
        db.commit()
        flash(request, "CRM sync settings saved.")
    return redirect("/app/contacts/crm")


@router.post("/app/contacts/crm/lookup")
async def crm_lookup(request: Request, ctx: Ctx = Depends(_ctx), db: Session = Depends(get_db)):
    form = await request.form()
    email = svc.normalize_email(form.get("email"))
    hit = crm_svc.lookup_owner(db, ctx.workspace.id, email) if EMAIL_RE.match(email) else None
    db.commit()
    if "application/json" in request.headers.get("accept", ""):
        return {"email": email, "result": hit}
    flash(request, f"{email}: owner {hit['owner_email']} ({hit['record_type']})" if hit
          else f"No CRM owner found for {email or 'that email'}.", "success" if hit else "info")
    return redirect("/app/contacts/crm")


@router.post("/app/contacts/crm/records")
async def crm_record_save(request: Request, ctx: Ctx = Depends(_writer), db: Session = Depends(get_db)):
    form = await request.form()
    email = svc.normalize_email(form.get("email"))
    owner = svc.normalize_email(form.get("owner_email")) or None
    obj = form.get("object_type") if form.get("object_type") in ("contact", "lead") else "contact"
    if not EMAIL_RE.match(email) or (owner and not EMAIL_RE.match(owner)):
        flash(request, "Enter valid record and owner email addresses.", "error")
        return redirect("/app/contacts/crm")
    rec = None
    if form.get("id"):
        rec = db.get(CrmRecord, form.get("id"))
        if not rec or rec.workspace_id != ctx.workspace.id:
            raise Forbidden("Record not found.")
    clash = db.scalar(select(CrmRecord).where(CrmRecord.workspace_id == ctx.workspace.id, CrmRecord.object_type == obj,
                                              CrmRecord.email_normalized == email))
    if clash and rec and clash.id != rec.id:
        flash(request, "Another record already uses that email.", "error")
        return redirect("/app/contacts/crm")
    rec = rec or clash
    if not rec:
        rec = CrmRecord(workspace_id=ctx.workspace.id)
        db.add(rec)
    rec.object_type, rec.email_normalized, rec.owner_email = obj, email, owner
    rec.name = (form.get("name") or "").strip()[:200]
    rec.company = (form.get("company") or "").strip()[:200] or None
    db.commit()
    flash(request, "CRM record saved.")
    return redirect("/app/contacts/crm")


@router.post("/app/contacts/crm/records/{rid}/delete")
def crm_record_delete(request: Request, rid: str, ctx: Ctx = Depends(_writer), db: Session = Depends(get_db)):
    rec = db.get(CrmRecord, rid)
    if rec and rec.workspace_id == ctx.workspace.id:
        db.delete(rec)
        db.commit()
        flash(request, "CRM record deleted.")
    return redirect("/app/contacts/crm")


@router.post("/app/contacts/crm/activities/{aid}/retry")
def crm_activity_retry(request: Request, aid: str, ctx: Ctx = Depends(_crm_admin), db: Session = Depends(get_db)):
    act = db.get(CrmActivity, aid)
    if not act or act.workspace_id != ctx.workspace.id:
        raise Forbidden("Activity not found.")
    ok = crm_svc.retry_activity(db, act)
    db.commit()
    flash(request, "Activity sent to the CRM." if ok else f"Retry failed: {act.error or 'no CRM connected'}",
          "success" if ok else "error")
    return redirect("/app/contacts/crm")


# ================================================================ profile
@router.get("/app/contacts/{cid}")
def contact_profile(request: Request, cid: str, ctx: Ctx = Depends(_ctx), db: Session = Depends(get_db)):
    c = _get(db, ctx, cid)
    history = svc.meeting_history(db, ctx.workspace.id, c.email_normalized)
    ets = {e.id: e for e in db.scalars(select(EventType).where(EventType.id.in_({b.event_type_id for b in history} or {""})))}
    hosts = {u.id: u for u in db.scalars(select(User).where(User.id.in_({b.host_id for b in history} or {""})))}
    now = utcnow()
    upcoming = [b for b in history if b.start_at >= now and b.status in ("confirmed", "pending", "pending_payment")]
    past = [b for b in history if b not in upcoming]
    subs = [s for s in db.scalars(select(RoutingSubmission).where(RoutingSubmission.workspace_id == ctx.workspace.id)
                                  .order_by(RoutingSubmission.created_at.desc()).limit(1000))
            if c.email_normalized in [svc.normalize_email(v) for v in (s.answers_json or {}).values() if isinstance(v, str)]]
    events = _bookable_events(db, ctx)
    share_url = f"{settings.base_url}/{ctx.user.slug}" if ctx.user.slug else None
    insights = svc.relationship_insights(db, ctx.workspace.id, c.email_normalized)
    crm_owner = crm_svc.lookup_owner(db, ctx.workspace.id, c.email_normalized) if crm_svc.get_connection(
        db, ctx.workspace.id) else None
    return render(request, "app/contacts/profile.html", ctx, nav="contacts", c=c, insights=insights, crm_owner=crm_owner, upcoming=upcoming[::-1], past=past,
                  ets=ets, hosts=hosts, subs=subs[:20], events=events, share_url=share_url,
                  event_urls={e.id: booking_url(db, e) for e in events}, total=len(history),
                  completed=sum(1 for b in past if b.status == "confirmed" and not b.no_show),
                  cancelled=sum(1 for b in history if b.status == "cancelled"))


@router.post("/app/contacts/{cid}/edit")
async def contact_edit(request: Request, cid: str, ctx: Ctx = Depends(_writer), db: Session = Depends(get_db)):
    c = _get(db, ctx, cid)
    form = await request.form()
    email = svc.normalize_email(form.get("email") or c.email_normalized)
    if not EMAIL_RE.match(email):
        flash(request, "Enter a valid email address.", "error")
        return redirect(f"/app/contacts/{c.id}")
    if email != c.email_normalized:
        clash = svc.find_contact(db, ctx.workspace.id, email, include_deleted=True)
        if clash:
            flash(request, "Another contact already uses that email.", "error")
            return redirect(f"/app/contacts/{c.id}")
        c.email_normalized = email
    c.name = (form.get("name") or "").strip()[:200]
    c.phone = (form.get("phone") or "").strip()[:50] or None
    c.company = (form.get("company") or "").strip()[:200] or None
    c.timezone = (form.get("timezone") or "").strip()[:64] or None
    c.notes = (form.get("notes") or "").strip()[:10000] or None
    c.tags_json = svc.parse_tags(form.get("tags") or "")
    audit(db, "contact.updated", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="contact",
          entity_id=c.id)
    db.commit()
    flash(request, "Contact saved.")
    return redirect(f"/app/contacts/{c.id}")


@router.post("/app/contacts/{cid}/custom")
async def contact_custom(request: Request, cid: str, ctx: Ctx = Depends(_writer), db: Session = Depends(get_db)):
    c = _get(db, ctx, cid)
    form = await request.form()
    key = (form.get("key") or "").strip()[:60]
    custom = dict(c.custom_json or {})
    if form.get("remove"):
        custom.pop(form.get("remove"), None)
    elif key:
        if len(custom) >= 50 and key not in custom:
            flash(request, "Up to 50 custom fields per contact.", "error")
            return redirect(f"/app/contacts/{c.id}")
        custom[key] = (form.get("value") or "").strip()[:1000]
    c.custom_json = custom
    db.commit()
    return redirect(f"/app/contacts/{c.id}#custom")


@router.post("/app/contacts/{cid}/unsubscribe")
def contact_unsubscribe(request: Request, cid: str, ctx: Ctx = Depends(_writer), db: Session = Depends(get_db)):
    c = _get(db, ctx, cid)
    c.unsubscribed = not c.unsubscribed
    audit(db, "contact.suppression_changed", workspace_id=ctx.workspace.id, actor_id=ctx.user.id,
          entity_type="contact", entity_id=c.id, metadata={"unsubscribed": c.unsubscribed})
    db.commit()
    flash(request, "Marketing emails suppressed for this contact." if c.unsubscribed else "Contact resubscribed.")
    return redirect(f"/app/contacts/{c.id}")


@router.post("/app/contacts/{cid}/delete")
def contact_delete(request: Request, cid: str, ctx: Ctx = Depends(_writer), db: Session = Depends(get_db)):
    c = _get(db, ctx, cid)
    c.deleted_at = utcnow()
    audit(db, "contact.deleted", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="contact",
          entity_id=c.id)
    db.commit()
    flash(request, "Contact deleted. Meeting history is kept on the bookings.")
    return redirect("/app/contacts")


@router.get("/app/contacts/{cid}/book")
def contact_book(request: Request, cid: str, event_type_id: str, ctx: Ctx = Depends(_ctx),
                 db: Session = Depends(get_db)):
    c = _get(db, ctx, cid)
    et = db.get(EventType, event_type_id)
    if not et or et.workspace_id != ctx.workspace.id or et.deleted_at:
        raise Forbidden("Event type not found.")
    url = booking_url(db, et)
    if not url:
        flash(request, "That event type has no public booking page.", "error")
        return redirect(f"/app/contacts/{c.id}")
    params = {"name": c.name or "", "email": c.email_normalized}
    if c.phone:
        params["phone"] = c.phone
    return redirect(url + "?" + urlencode({k: v for k, v in params.items() if v}))
