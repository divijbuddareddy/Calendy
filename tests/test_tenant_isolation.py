"""PRD 23 / 24.1 #10 / ACC-002: automated cross-tenant access tests on EVERY resource class.

Workspace A is populated with one row of every tenant-owned resource. User B (owner of a separate
workspace, top plan, so no feature gate hides a leak) then requests every /app/... detail, edit and
delete URL and every /api/v1 resource URL with A's ids. Every request must be refused (403/404, or a
redirect/400 that changes nothing), no response may contain A's private data, and a full snapshot of
A's rows must be byte-for-byte unchanged afterwards.

`test_every_parameterised_route_is_covered` fails when someone adds a new /app or /api/v1 route with
a path parameter without adding it here, so coverage can't silently rot.
"""
import datetime as dt
import secrets

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select

from app.db import Base
from app.models import utcnow
from app.services import apikeys
from app.services.bootstrap import add_user_to_workspace, create_user_with_workspace

import app.main  # noqa: E402,F401  (registers every model module, incl. models_crm / models_commerce)

_MODELS = {m.class_.__name__: m.class_ for m in Base.registry.mappers}
ApiKey = _MODELS["ApiKey"]
AssistantRequest = _MODELS["AssistantRequest"]
AvailabilityOverride = _MODELS["AvailabilityOverride"]
AvailabilitySchedule = _MODELS["AvailabilitySchedule"]
Booking = _MODELS["Booking"]
CalendarConnection = _MODELS["CalendarConnection"]
ConnectedCalendar = _MODELS["ConnectedCalendar"]
Contact = _MODELS["Contact"]
DataRequest = _MODELS["DataRequest"]
EventQuestion = _MODELS["EventQuestion"]
EventType = _MODELS["EventType"]
ExternalCalendarEvent = _MODELS["ExternalCalendarEvent"]
InAppNotification = _MODELS["InAppNotification"]
Invitee = _MODELS["Invitee"]
MeetingPoll = _MODELS["MeetingPoll"]
MeetingRecap = _MODELS["MeetingRecap"]
NotificationJob = _MODELS["NotificationJob"]
OAuthApp = _MODELS["OAuthApp"]
OutboxEvent = _MODELS["OutboxEvent"]
PaymentAccount = _MODELS["PaymentAccount"]
PaymentRecord = _MODELS["PaymentRecord"]
RoutingForm = _MODELS["RoutingForm"]
RoutingSubmission = _MODELS["RoutingSubmission"]
Team = _MODELS["Team"]
TeamMember = _MODELS["TeamMember"]
WebhookDelivery = _MODELS["WebhookDelivery"]
WebhookSubscription = _MODELS["WebhookSubscription"]
Workflow = _MODELS["Workflow"]
Workspace = _MODELS["Workspace"]
WorkspaceMember = _MODELS["WorkspaceMember"]
CrmActivity, CrmRecord = _MODELS["CrmActivity"], _MODELS["CrmRecord"]
Invoice, PackageProduct, PaymentLink = _MODELS["Invoice"], _MODELS["PackageProduct"], _MODELS["PaymentLink"]

SECRET = "ALPHA-PRIVATE-7f3c"  # appears in A's resource names; must never leak into B's responses


def _tok():
    return secrets.token_urlsafe(16)


@pytest.fixture
def world(db):
    a_user, a_ws = create_user_with_workspace(db, "alpha@example.com", "Alpha Owner", password="password123",
                                              slug="alpha", verified=True, workspace_name="Alpha Co")
    b_user, b_ws = create_user_with_workspace(db, "bravo@example.com", "Bravo Owner", password="password123",
                                              slug="bravo", verified=True, workspace_name="Bravo Co")
    for u in (a_user, b_user):
        u.onboarding_completed = True
    a_ws.plan_id = b_ws.plan_id = "enterprise"
    a_mate = add_user_to_workspace(db, a_ws, "alpha-mate@example.com", "Alpha Mate", "member", "password123")
    db.flush()
    A, now = {}, utcnow()
    W = a_ws.id

    et = EventType(workspace_id=W, owner_user_id=a_user.id, name=f"Event {SECRET}", slug="alpha-event", duration=30)
    db.add(et)
    db.flush()
    q = EventQuestion(event_type_id=et.id, label=f"Q {SECRET}", type="text")
    db.add(q)
    managed = EventType(workspace_id=W, owner_user_id=a_user.id, name=f"Managed {SECRET}", slug="alpha-managed",
                        duration=30, is_managed_template=True)
    db.add(managed)
    db.flush()
    child = EventType(workspace_id=W, owner_user_id=a_mate.id, name=f"Child {SECRET}", slug="alpha-child",
                      duration=30, managed_template_id=managed.id)
    db.add(child)
    sch = db.scalar(select(AvailabilitySchedule).where(AvailabilitySchedule.user_id == a_user.id))
    sch.name = f"Hours {SECRET}"
    ov = AvailabilityOverride(schedule_id=sch.id, local_date=(now + dt.timedelta(days=9)).date(), kind="unavailable",
                              intervals_json=[])
    db.add(ov)
    start = (now + dt.timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
    b = Booking(workspace_id=W, event_type_id=et.id, host_id=a_user.id, start_at=start,
                end_at=start + dt.timedelta(minutes=30), status="confirmed", manage_token=_tok(), timezone="UTC",
                host_notes=f"notes {SECRET}")
    b.invitees.append(Invitee(name=f"Invitee {SECRET}", email="invitee-alpha@example.com"))
    db.add(b)
    past = Booking(workspace_id=W, event_type_id=et.id, host_id=a_user.id, start_at=now - dt.timedelta(days=2),
                   end_at=now - dt.timedelta(days=2) + dt.timedelta(minutes=30), status="confirmed",
                   manage_token=_tok(), timezone="UTC", cancellation_requested_at=now)
    past.invitees.append(Invitee(name=f"Past {SECRET}", email="past-alpha@example.com"))
    db.add(past)
    db.flush()
    pay = PaymentRecord(workspace_id=W, booking_id=b.id, provider="mock", amount_minor=5000, currency="GBP",
                        status="succeeded")
    db.add(pay)
    notif = InAppNotification(user_id=a_user.id, kind="booking", title=f"N {SECRET}", workspace_id=W) \
        if "workspace_id" in InAppNotification.__table__.columns else \
        InAppNotification(user_id=a_user.id, kind="booking", title=f"N {SECRET}")
    db.add(notif)
    dr = DataRequest(user_id=a_user.id, workspace_id=W, kind="export", status="completed",
                     result_json={"secret": SECRET}, completed_at=now)
    db.add(dr)
    oapp = OAuthApp(workspace_id=W, created_by=a_user.id, name=f"App {SECRET}", client_id="cid_" + _tok(),
                    client_secret_hash="x" * 64)
    db.add(oapp)
    conn = CalendarConnection(workspace_id=W, user_id=a_user.id, provider="mock", status="connected")
    db.add(conn)
    db.flush()
    cal = ConnectedCalendar(connection_id=conn.id, provider_calendar_id="primary", display_name=f"Cal {SECRET}")
    db.add(cal)
    db.flush()
    xev = ExternalCalendarEvent(calendar_id=cal.id, external_id="x1", title=f"Busy {SECRET}",
                                start_at=start + dt.timedelta(days=1), end_at=start + dt.timedelta(days=1, hours=1))
    db.add(xev)
    pacct = PaymentAccount(workspace_id=W, user_id=a_user.id, provider="stripe", account_id="acct_alpha")
    db.add(pacct)
    from app.security import encrypt

    sub = WebhookSubscription(workspace_id=W, endpoint="https://alpha.example.com/hook",
                              secret_encrypted=encrypt("s3cret"))
    db.add(sub)
    db.flush()
    dlv = WebhookDelivery(subscription_id=sub.id, event_id="evt_alpha", topic="booking.created",
                          payload_json={"secret": SECRET}, status="dead")
    db.add(dlv)
    key_row, a_key = apikeys.create_key(db, W, a_user.id, f"Key {SECRET}", ["read", "write"])
    wf = Workflow(workspace_id=W, name=f"WF {SECRET}", trigger="before_event")
    db.add(wf)
    job = NotificationJob(workspace_id=W, trigger="before_event", recipient="invitee-alpha@example.com",
                          scheduled_at=now, status="failed")
    db.add(job)
    team = Team(workspace_id=W, name=f"Team {SECRET}", slug="alpha-team")
    db.add(team)
    db.flush()
    tm = TeamMember(team_id=team.id, user_id=a_mate.id)
    db.add(tm)
    wm = db.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == W,
                                                 WorkspaceMember.user_id == a_mate.id))
    form = RoutingForm(workspace_id=W, owner_user_id=a_user.id, name=f"Form {SECRET}", slug="alpha-form",
                       form_schema_json=[{"id": "f1", "type": "text", "label": "Name"}],
                       rules_json=[{"id": "r1", "logic": "and", "conditions": [], "destination": {"type": "message", "value": "x"}}])
    db.add(form)
    db.flush()
    rsub = RoutingSubmission(workspace_id=W, form_id=form.id, answers_json={"f1": SECRET})
    db.add(rsub)
    contact = Contact(workspace_id=W, email_normalized="invitee-alpha@example.com", name=f"Contact {SECRET}")
    db.add(contact)
    crm_rec = CrmRecord(workspace_id=W, email_normalized="invitee-alpha@example.com")
    db.add(crm_rec)
    crm_act = CrmActivity(workspace_id=W, provider="hubspot", topic="booking.created", source_event_id="evt_x",
                          subject=f"Act {SECRET}")
    db.add(crm_act)
    outbox = OutboxEvent(workspace_id=W, topic="booking.created", payload_json={"secret": SECRET},
                         processed_at=now) if "workspace_id" in OutboxEvent.__table__.columns else None
    if outbox:
        db.add(outbox)
    areq = AssistantRequest(workspace_id=W, user_id=a_user.id, prompt=f"Prompt {SECRET}")
    db.add(areq)
    recap = MeetingRecap(workspace_id=W, owner_user_id=a_user.id, title=f"Recap {SECRET}")
    db.add(recap)
    poll = MeetingPoll(workspace_id=W, owner_user_id=a_user.id, title=f"Poll {SECRET}", public_token=_tok())
    db.add(poll)
    pkg = PackageProduct(workspace_id=W, owner_user_id=a_user.id, event_type_id=et.id, name=f"Pkg {SECRET}",
                         price_minor=1000, public_token=_tok())
    db.add(pkg)
    inv = Invoice(workspace_id=W, created_by=a_user.id, number="INV-ALPHA-1", recipient_email="x@example.com",
                  amount_minor=1000, public_token=_tok())
    db.add(inv)
    dom = _MODELS["DomainClaim"](workspace_id=W, domain="alpha-private.example", token=_tok())
    db.add(dom)
    plink = PaymentLink(workspace_id=W, created_by=a_user.id, description=f"Link {SECRET}", amount_minor=1000,
                        public_token=_tok())
    db.add(plink)
    db.flush()
    _, b_key = apikeys.create_key(db, b_ws.id, b_user.id, "bravo", ["read", "write"])
    db.commit()

    A.update(user=a_user.id, ws=W, mate=a_mate.id, et=et.id, q=q.id, managed=managed.id, child=child.id, sch=sch.id,
             ov=ov.id, booking=b.id, past=past.id, pay=pay.id, notif=notif.id, dr=dr.id, oapp=oapp.id, conn=conn.id,
             xev=xev.id, pacct=pacct.id, sub=sub.id, dlv=dlv.id, key=key_row.id, wf=wf.id, job=job.id,
             team=team.id, tm=tm.id, wm=wm.id, form=form.id, fq="f1", rule="r1", rsub=rsub.id,
             contact=contact.id, crm_rec=crm_rec.id, crm_act=crm_act.id, outbox=outbox.id if outbox else "none",
             areq=areq.id, recap=recap.id, poll=poll.id, pkg=pkg.id, inv=inv.id, plink=plink.id, dom=dom.id, a_key=a_key)
    return {"A": A, "b_user": b_user, "b_ws": b_ws, "b_key": b_key}


# (method, url template) for every parameterised tenant route. {x} placeholders are A's ids.
APP_ROUTES = [
    ("POST", "/app/notifications/{notif}/read"),
    ("GET", "/app/event-types/{et}/edit"),
    ("POST", "/app/event-types/{et}"),
    ("POST", "/app/event-types/{et}/questions"),
    ("POST", "/app/event-types/{et}/questions/{q}"),
    ("POST", "/app/event-types/{et}/questions/{q}/delete"),
    ("POST", "/app/event-types/{et}/questions/{q}/move"),
    ("POST", "/app/event-types/{et}/duplicate"),
    ("POST", "/app/event-types/{et}/status"),
    ("POST", "/app/event-types/{et}/delete"),
    ("POST", "/app/event-types/{et}/single-use"),
    ("GET", "/app/event-types/{et}/embed"),
    ("POST", "/app/availability/{sch}/weekly"),
    ("POST", "/app/availability/{sch}/default"),
    ("POST", "/app/availability/{sch}/delete"),
    ("POST", "/app/availability/{sch}/overrides"),
    ("POST", "/app/availability/{sch}/overrides/{ov}/delete"),
    ("POST", "/app/availability/{sch}/holidays"),
    ("GET", "/app/scheduled/{booking}"),
    ("POST", "/app/scheduled/{booking}/notes"),
    ("POST", "/app/scheduled/{booking}/cancel"),
    ("GET", "/app/scheduled/{booking}/reschedule"),
    ("POST", "/app/scheduled/{booking}/reschedule"),
    ("POST", "/app/scheduled/{booking}/request-reschedule"),
    ("POST", "/app/scheduled/{past}/no-show"),
    ("POST", "/app/scheduled/{past}/approve-cancel"),
    ("POST", "/app/scheduled/{past}/decline-cancel"),
    ("POST", "/app/scheduled/{booking}/reassign"),
    ("POST", "/app/m/bookings/{booking}/cancel"),
    ("GET", "/app/settings/data/export/{dr}.json"),
    ("POST", "/app/integrations/oauth-apps/{oapp}/rotate-secret"),
    ("POST", "/app/integrations/oauth-apps/{oapp}/delete"),
    ("POST", "/app/integrations/authorized-apps/{oapp}/revoke"),
    ("POST", "/app/integrations/connections/{conn}/disconnect"),
    ("POST", "/app/integrations/connections/{conn}/reconnect"),
    ("POST", "/app/integrations/connections/{conn}/sync"),
    ("POST", "/app/integrations/connections/{conn}/simulate-expiry"),
    ("GET", "/app/integrations/connections/{conn}/events"),
    ("POST", "/app/integrations/connections/{conn}/events"),
    ("POST", "/app/integrations/events/{xev}/delete"),
    ("POST", "/app/integrations/events/{xev}/move"),
    ("POST", "/app/integrations/payments/{pacct}/disconnect"),
    ("POST", "/app/integrations/webhooks/{sub}/toggle"),
    ("POST", "/app/integrations/webhooks/{sub}/delete"),
    ("POST", "/app/integrations/webhooks/{sub}/test"),
    ("POST", "/app/integrations/webhooks/deliveries/{dlv}/retry"),
    ("POST", "/app/integrations/api-keys/{key}/revoke"),
    ("POST", "/app/workflows/jobs/{job}/retry"),
    ("GET", "/app/workflows/{wf}"),
    ("POST", "/app/workflows/{wf}"),
    ("POST", "/app/workflows/{wf}/toggle"),
    ("POST", "/app/workflows/{wf}/delete"),
    ("POST", "/app/workflows/{wf}/test"),
    ("GET", "/app/workflows/{wf}/runs"),
    ("POST", "/app/payments/{booking}/refund"),
    ("GET", "/app/payments/{booking}"),
    ("POST", "/app/payments/packages/{pkg}/toggle"),
    ("GET", "/app/payments/invoices/{inv}"),
    ("POST", "/app/payments/invoices/{inv}/send"),
    ("POST", "/app/payments/invoices/{inv}/void"),
    ("POST", "/app/payments/links/{plink}/toggle"),
    ("POST", "/app/teams/members/{wm}/role"),
    ("POST", "/app/teams/members/{wm}/revoke"),
    ("POST", "/app/teams/members/{wm}/deactivate"),
    ("POST", "/app/teams/members/{wm}/remove"),
    ("GET", "/app/teams/{team}"),
    ("POST", "/app/teams/{team}/edit"),
    ("POST", "/app/teams/{team}/delete"),
    ("POST", "/app/teams/{team}/members/add"),
    ("POST", "/app/teams/{team}/members/{tm}/update"),
    ("POST", "/app/teams/{team}/members/{tm}/remove"),
    ("GET", "/app/admin/managed-events/{managed}"),
    ("POST", "/app/admin/managed-events/{managed}"),
    ("POST", "/app/admin/managed-events/{managed}/questions"),
    ("POST", "/app/admin/managed-events/{managed}/questions/{q}/delete"),
    ("POST", "/app/admin/managed-events/{managed}/assign"),
    ("POST", "/app/admin/managed-events/{managed}/sync"),
    ("POST", "/app/admin/managed-events/{managed}/children/{child}/detach"),
    ("POST", "/app/admin/managed-events/{managed}/delete"),
    ("POST", "/app/admin/domains/{dom}/verify"),
    ("POST", "/app/admin/domains/{dom}/delete"),
    ("POST", "/app/admin/health/notification/{job}/retry"),
    ("POST", "/app/admin/health/webhook/{dlv}/retry"),
    ("POST", "/app/admin/health/outbox/{outbox}/retry"),
    ("GET", "/app/routing/{form}"),
    ("POST", "/app/routing/{form}/prefill"),
    ("POST", "/app/routing/{form}/settings"),
    ("POST", "/app/routing/{form}/delete"),
    ("POST", "/app/routing/{form}/questions"),
    ("POST", "/app/routing/{form}/questions/{fq}/delete"),
    ("POST", "/app/routing/{form}/rules"),
    ("POST", "/app/routing/{form}/rules-json"),
    ("POST", "/app/routing/{form}/rules/{rule}/delete"),
    ("POST", "/app/routing/{form}/fallback"),
    ("GET", "/app/routing/{form}/preview"),
    ("POST", "/app/routing/{form}/preview"),
    ("GET", "/app/routing/{form}/submissions"),
    ("GET", "/app/routing/{form}/submissions.csv"),
    ("POST", "/app/contacts/crm/records/{crm_rec}/delete"),
    ("POST", "/app/contacts/crm/activities/{crm_act}/retry"),
    ("GET", "/app/contacts/{contact}"),
    ("POST", "/app/contacts/{contact}/edit"),
    ("POST", "/app/contacts/{contact}/custom"),
    ("POST", "/app/contacts/{contact}/unsubscribe"),
    ("POST", "/app/contacts/{contact}/delete"),
    ("GET", "/app/contacts/{contact}/book"),
    ("GET", "/app/assistant/{areq}"),
    ("POST", "/app/assistant/{areq}/approve"),
    ("POST", "/app/assistant/{areq}/reject"),
    ("GET", "/app/recaps/{recap}"),
    ("POST", "/app/recaps/{recap}/bot"),
    ("POST", "/app/recaps/{recap}/transcript"),
    ("POST", "/app/recaps/{recap}/generate"),
    ("POST", "/app/recaps/{recap}/edit"),
    ("POST", "/app/recaps/{recap}/share"),
    ("POST", "/app/recaps/{recap}/delete"),
    ("GET", "/app/polls/{poll}"),
    ("POST", "/app/polls/{poll}/status"),
    ("POST", "/app/polls/{poll}/delete"),
    ("POST", "/app/polls/{poll}/confirm"),
]

API_ROUTES = [
    ("GET", "/api/v1/event-types/{et}"),
    ("PATCH", "/api/v1/event-types/{et}"),
    ("GET", "/api/v1/bookings/{booking}"),
    ("POST", "/api/v1/bookings/{booking}/cancel"),
    ("POST", "/api/v1/bookings/{booking}/reschedule"),
    ("POST", "/api/v1/bookings/{past}/no-show"),
    ("DELETE", "/api/v1/webhook-subscriptions/{sub}"),
    ("POST", "/api/v1/ext/event-types/{et}/single-use-link"),
]

# Route templates whose parameters are not tenant resource ids (provider names, template keys,
# signed tokens) — covered by their own tests or by the token check below.
NOT_RESOURCE_ROUTES = {
    "/app/integrations/{provider}/connect",
    "/app/integrations/{provider}/callback",
    "/app/contacts/crm/{provider}/callback",
    "/app/workflows/templates/{key}",
    "/api/v1/calendar-feed/{user_id}/{token}.ics",
}

# Generic templates as registered in the app, for the coverage check.
COVERED_TEMPLATES = {
    "/app/notifications/{nid}/read", "/app/event-types/{event_id}/edit", "/app/event-types/{event_id}",
    "/app/event-types/{event_id}/questions", "/app/event-types/{event_id}/questions/{qid}",
    "/app/event-types/{event_id}/questions/{qid}/delete", "/app/event-types/{event_id}/questions/{qid}/move",
    "/app/event-types/{event_id}/duplicate", "/app/event-types/{event_id}/status", "/app/event-types/{event_id}/delete",
    "/app/event-types/{event_id}/single-use", "/app/event-types/{event_id}/embed", "/app/availability/{sid}/weekly",
    "/app/availability/{sid}/default", "/app/availability/{sid}/delete", "/app/availability/{sid}/overrides",
    "/app/availability/{sid}/overrides/{oid}/delete", "/app/availability/{sid}/holidays",
    "/app/scheduled/{booking_id}", "/app/scheduled/{booking_id}/notes", "/app/scheduled/{booking_id}/cancel",
    "/app/scheduled/{booking_id}/reschedule", "/app/scheduled/{booking_id}/request-reschedule",
    "/app/scheduled/{booking_id}/no-show", "/app/scheduled/{booking_id}/approve-cancel",
    "/app/scheduled/{booking_id}/decline-cancel", "/app/scheduled/{booking_id}/reassign",
    "/app/m/bookings/{booking_id}/cancel", "/app/settings/data/export/{rid}.json",
    "/app/integrations/oauth-apps/{app_id}/rotate-secret", "/app/integrations/oauth-apps/{app_id}/delete",
    "/app/integrations/authorized-apps/{app_id}/revoke", "/app/integrations/connections/{conn_id}/disconnect",
    "/app/integrations/connections/{conn_id}/reconnect", "/app/integrations/connections/{conn_id}/sync",
    "/app/integrations/connections/{conn_id}/simulate-expiry", "/app/integrations/connections/{conn_id}/events",
    "/app/integrations/events/{ev_id}/delete", "/app/integrations/events/{ev_id}/move",
    "/app/integrations/payments/{acct_id}/disconnect", "/app/integrations/webhooks/{sub_id}/toggle",
    "/app/integrations/webhooks/{sub_id}/delete", "/app/integrations/webhooks/{sub_id}/test",
    "/app/integrations/webhooks/deliveries/{delivery_id}/retry", "/app/integrations/api-keys/{key_id}/revoke",
    "/app/workflows/jobs/{job_id}/retry", "/app/workflows/{wf_id}", "/app/workflows/{wf_id}/toggle",
    "/app/workflows/{wf_id}/delete", "/app/workflows/{wf_id}/test", "/app/workflows/{wf_id}/runs",
    "/app/payments/{booking_id}/refund", "/app/payments/{booking_id}", "/app/payments/packages/{pkg_id}/toggle",
    "/app/payments/invoices/{inv_id}", "/app/payments/invoices/{inv_id}/send", "/app/payments/invoices/{inv_id}/void",
    "/app/payments/links/{link_id}/toggle", "/app/teams/members/{mid}/{action}", "/app/teams/{team_id}",
    "/app/teams/{team_id}/edit", "/app/teams/{team_id}/delete", "/app/teams/{team_id}/members/add",
    "/app/teams/{team_id}/members/{tmid}/{action}", "/app/admin/managed-events/{tid}",
    "/app/admin/managed-events/{tid}/questions", "/app/admin/managed-events/{tid}/questions/{qid}/delete",
    "/app/admin/managed-events/{tid}/assign", "/app/admin/managed-events/{tid}/sync",
    "/app/admin/managed-events/{tid}/children/{cid}/detach", "/app/admin/managed-events/{tid}/delete",
    "/app/admin/health/{kind}/{rid}/retry", "/app/admin/domains/{cid}/verify", "/app/admin/domains/{cid}/delete", "/app/routing/{form_id}", "/app/routing/{form_id}/prefill",
    "/app/routing/{form_id}/settings", "/app/routing/{form_id}/delete", "/app/routing/{form_id}/questions",
    "/app/routing/{form_id}/questions/{fid}/{action}", "/app/routing/{form_id}/rules",
    "/app/routing/{form_id}/rules-json", "/app/routing/{form_id}/rules/{rid}/{action}",
    "/app/routing/{form_id}/fallback", "/app/routing/{form_id}/preview", "/app/routing/{form_id}/submissions",
    "/app/routing/{form_id}/submissions.csv", "/app/contacts/crm/records/{rid}/delete",
    "/app/contacts/crm/activities/{aid}/retry", "/app/contacts/{cid}", "/app/contacts/{cid}/edit",
    "/app/contacts/{cid}/custom", "/app/contacts/{cid}/unsubscribe", "/app/contacts/{cid}/delete",
    "/app/contacts/{cid}/book", "/app/assistant/{req_id}", "/app/assistant/{req_id}/approve",
    "/app/assistant/{req_id}/reject", "/app/recaps/{recap_id}", "/app/recaps/{recap_id}/bot",
    "/app/recaps/{recap_id}/transcript", "/app/recaps/{recap_id}/generate", "/app/recaps/{recap_id}/edit",
    "/app/recaps/{recap_id}/share", "/app/recaps/{recap_id}/delete", "/app/polls/{poll_id}",
    "/app/polls/{poll_id}/status", "/app/polls/{poll_id}/delete", "/app/polls/{poll_id}/confirm",
    "/api/v1/ext/event-types/{event_type_id}/single-use-link", "/api/v1/event-types/{event_type_id}",
    "/api/v1/bookings/{booking_id}", "/api/v1/bookings/{booking_id}/cancel", "/api/v1/bookings/{booking_id}/reschedule",
    "/api/v1/bookings/{booking_id}/no-show", "/api/v1/webhook-subscriptions/{sub_id}",
}

# Append-only / bookkeeping tables that B's own (refused) requests may legitimately write to.
IGNORED_TABLES = {"audit_logs", "usage_events", "page_views", "outbox_events", "idempotency_records",
                  "worker_task_health", "sent_messages", "host_locks"}

ALLOWED_STATUS = {400, 403, 404, 405, 409, 422}


def _snapshot(db, skip_ids: set) -> dict:
    db.commit()  # end any open read transaction so we see the latest committed state
    db.expire_all()
    snap = {}
    for table in Base.metadata.sorted_tables:
        if table.name in IGNORED_TABLES:
            continue
        for row in db.execute(table.select()).mappings():
            vals = dict(row)
            if skip_ids & {str(v) for v in vals.values() if v is not None}:
                continue  # rows belonging to B (its session/user/key bookkeeping)
            pk = tuple(vals[c.name] for c in table.primary_key.columns)
            snap[(table.name, pk)] = repr(sorted(vals.items(), key=lambda kv: kv[0]))
    return snap


def _body_for(method, url):
    """Plausible form/JSON bodies so handlers get past validation and reach the tenant check."""
    form = {"name": "Hijacked", "title": "Hijacked", "reason": "x", "notes": "x", "value": "1", "role": "admin",
            "status": "paused", "start_at": (utcnow() + dt.timedelta(days=3)).isoformat(), "date": "2030-01-01",
            "label": "x", "type": "text", "email": "evil@example.com", "user_id": "x", "direction": "up",
            "amount_minor": "100", "mon": "09:00-17:00", "country": "GB", "rules_json": "[]", "transcript": "x",
            "share": "1", "slot": "0", "host_id": "x", "description": "x", "body": "x"}
    return form


def _assert_refused(r, method, url):
    assert SECRET not in r.text, f"{method} {url} leaked workspace A data"
    if r.status_code in ALLOWED_STATUS:
        return
    # Some HTML handlers answer "not found" with a flash + redirect; acceptable only if the redirect
    # does not land on A's resource (the snapshot check proves nothing changed).
    assert r.status_code in (302, 303), f"{method} {url} -> {r.status_code} (expected 403/404)"


def test_app_routes_deny_other_workspace(client, db, world, login):
    A = world["A"]
    login(client, world["b_user"])
    skip = {world["b_user"].id, world["b_ws"].id}
    before = _snapshot(db, skip)
    for method, tpl in APP_ROUTES:
        url = tpl.format(**A)
        r = client.request(method, url, data=_body_for(method, url) if method == "POST" else None,
                           follow_redirects=False)
        _assert_refused(r, method, url)
        if r.status_code in (302, 303):
            loc = r.headers.get("location", "")
            for v in A.values():
                if isinstance(v, str) and len(v) > 6 and v in loc:
                    pytest.fail(f"{method} {url} redirected into A's resource: {loc}")
    after = _snapshot(db, skip)
    changed = [k for k in before if before[k] != after.get(k)]
    assert not changed, f"workspace A rows modified/deleted by workspace B: {changed[:10]}"


def test_api_routes_deny_other_workspace_key(client, db, world):
    A = world["A"]
    h = {"Authorization": f"Bearer {world['b_key']}"}
    skip = {world["b_user"].id, world["b_ws"].id}
    before = _snapshot(db, skip)
    body = {"name": "Hijacked", "reason": "x", "start_at": (utcnow() + dt.timedelta(days=4)).isoformat() + "Z"}
    for method, tpl in API_ROUTES:
        url = tpl.format(**A)
        r = client.request(method, url, headers=h, json=body if method in ("POST", "PATCH") else None)
        assert SECRET not in r.text, f"{method} {url} leaked A's data"
        assert r.status_code in (403, 404), f"{method} {url} -> {r.status_code}"
    # list endpoints only ever return B's own rows
    for url in ("/api/v1/event-types", "/api/v1/bookings", "/api/v1/contacts", "/api/v1/webhook-subscriptions",
                "/api/v1/ext/event-types", "/api/v1/ext/upcoming"):
        r = client.get(url, headers=h)
        assert r.status_code == 200 and SECRET not in r.text and A["ws"] not in r.text, url
    # changing workspace_id in a request body does not switch tenant (PRD 24.1 #10)
    r = client.post("/api/v1/event-types", headers=h,
                    json={"name": "Mine", "duration": 30, "workspace_id": A["ws"], "owner_user_id": A["user"]})
    if r.status_code == 201:
        db.commit()
        db.expire_all()
        et = db.get(EventType, r.json()["id"])
        assert et.workspace_id == world["b_ws"].id and et.owner_user_id == world["b_user"].id
    after = _snapshot(db, skip)
    assert not [k for k in before if before[k] != after.get(k)]


def test_list_pages_never_show_other_workspace(client, world, login):
    login(client, world["b_user"])
    for url in ("/app", "/app/event-types", "/app/scheduled", "/app/scheduled?tab=past", "/app/availability",
                "/app/contacts", "/app/workflows", "/app/routing", "/app/analytics", "/app/integrations",
                "/app/integrations/webhooks", "/app/integrations/api-keys", "/app/teams", "/app/polls",
                "/app/recaps", "/app/assistant", "/app/admin", "/app/admin/audit", "/app/m",
                "/app/scheduled/export.csv", "/app/settings?tab=data"):
        r = client.get(url)
        assert r.status_code in (200, 403, 404), url
        assert SECRET not in r.text, f"{url} shows workspace A data"


def test_workspace_switch_requires_membership(client, world, login):
    login(client, world["b_user"])
    r = client.post("/app/settings/workspace/switch", data={"workspace_id": world["A"]["ws"]}, follow_redirects=False)
    assert r.status_code == 404
    # a forged session workspace_id falls back to B's own membership
    html = client.get("/app/event-types").text
    assert SECRET not in html


def test_api_key_of_a_cannot_be_used_after_b_guesses_prefix(client, world):
    """Keys are hashed; a prefix (which B could see in a screenshot) is not a credential."""
    raw = world["A"]["a_key"]
    r = client.get("/api/v1/me", headers={"Authorization": f"Bearer {raw[:14]}"})
    assert r.status_code == 401


def test_every_parameterised_route_is_covered():
    from app.main import app

    def walk(routes, prefix=""):
        for r in routes:
            if hasattr(r, "original_router"):
                ctx = getattr(r, "include_context", None)
                yield from walk(r.original_router.routes, prefix + (getattr(ctx, "prefix", "") or ""))
            elif hasattr(r, "routes") and not hasattr(r, "methods"):
                yield from walk(r.routes, prefix)
            elif hasattr(r, "methods") and hasattr(r, "path"):
                yield prefix + r.path

    templates = {p for p in walk(app.routes) if "{" in p and (p.startswith("/app/") or p.startswith("/api/v1/"))
                 and not p.startswith("/api/v1/public")}
    missing = templates - COVERED_TEMPLATES - NOT_RESOURCE_ROUTES
    assert not missing, f"Add cross-tenant cases for new routes: {sorted(missing)}"
