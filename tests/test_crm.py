"""CRM integration (mock / Salesforce / HubSpot adapters), CRM-owner routing, routing prefill mapping,
contacts P3 follow-ups and relationship insights."""
import datetime as dt
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import func, select

from app.events import process_outbox, publish
from app.models import (Booking, EventQuestion, EventType, Invitee, RoutingForm, RoutingSubmission, SentMessage, utcnow)
from app.models_crm import ContactFollowUp, CrmActivity, CrmConnection, CrmRecord
from app.security import encrypt, opaque_token
from app.services import contacts as contacts_svc
from app.services import crm
from app.services.bootstrap import add_user_to_workspace


class FakeResp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data

    def json(self):
        return self._data


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("unexpected external HTTP call")

    monkeypatch.setattr(crm, "http_request", boom)


def _owner(db, ws, email="bob@example.com", slug="bob"):
    bob = add_user_to_workspace(db, ws, email, "Bob Owner")
    bob.slug = slug
    et = EventType(workspace_id=ws.id, owner_user_id=bob.id, name="Intro", slug="intro", duration=30)
    db.add(et)
    db.flush()
    db.add(EventQuestion(event_type_id=et.id, label="Team size", type="text", sort_order=1))
    db.commit()
    return bob, et


def _form(db, user, ws, dest, fallback=None):
    rf = RoutingForm(
        workspace_id=ws.id, owner_user_id=user.id, name="Sales", slug="sales",
        form_schema_json=[
            {"id": "f1", "type": "text", "label": "Full legal name", "required": True},
            {"id": "f2", "type": "email", "label": "Work email", "required": True},
            {"id": "f3", "type": "number", "label": "Headcount", "required": False},
        ],
        rules_json=[{"id": "r1", "logic": "and", "conditions": [{"field": "f2", "op": "not_empty"}], "destination": dest}],
        fallback_json=fallback or {"type": "message", "value": "We'll be in touch."},
    )
    db.add(rf)
    db.commit()
    return rf


# ------------------------------------------------------------------ routing: CRM owner lookup
def test_crm_lookup_routes_to_owner_else_fallback(client, db, host):
    user, ws = host
    bob, et = _owner(db, ws)
    crm.connect(db, ws.id, "mock")
    db.add(CrmRecord(workspace_id=ws.id, object_type="lead", email_normalized="dana@corp.com", owner_email="BOB@example.com"))
    db.add(CrmRecord(workspace_id=ws.id, object_type="contact", email_normalized="ext@corp.com", owner_email="stranger@else.com"))
    db.commit()
    _form(db, user, ws, {"type": "salesforce_lookup", "value": "intro"})

    r = client.post(f"/r/{ws.slug}/sales", data={"f1": "Dana", "f2": "Dana@corp.com", "f3": "40"}, follow_redirects=False)
    assert r.status_code == 303
    url = urlparse(r.headers["location"])
    assert url.path == "/bob/intro"
    q = parse_qs(url.query)
    assert q["email"] == ["Dana@corp.com"] and "rsid" in q
    db.rollback()
    db.expire_all()
    sub = db.scalar(select(RoutingSubmission).order_by(RoutingSubmission.created_at.desc()))
    assert sub.destination_json["crm_owner"] == "bob@example.com" and sub.destination_json["crm_fallback"] is False

    # owner is not a workspace member -> fallback; unknown email -> fallback
    for email in ("ext@corp.com", "nobody@corp.com"):
        r = client.post(f"/r/{ws.slug}/sales", data={"f1": "X", "f2": email}, follow_redirects=False)
        assert r.status_code == 200 and "We&#39;ll be in touch." in r.text or "We'll be in touch." in r.text


def test_crm_lookup_without_slug_goes_to_owner_profile_and_survives_crm_outage(client, db, host, monkeypatch):
    user, ws = host
    _owner(db, ws)
    conn = crm.connect(db, ws.id, "salesforce", access_token="tok", instance_url="https://sf.example")
    db.commit()
    _form(db, user, ws, {"type": "crm_lookup", "value": ""})

    def sf(method, url, **kw):
        assert "/services/data/" in url and "Owner.Email" in url
        return FakeResp(200, {"records": [{"Id": "003X", "Owner": {"Email": "bob@example.com"}}]})

    monkeypatch.setattr(crm, "http_request", sf)
    r = client.post(f"/r/{ws.slug}/sales", data={"f1": "D", "f2": "d@corp.com"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/bob"

    monkeypatch.setattr(crm, "http_request", lambda *a, **k: FakeResp(503, {}))
    r = client.post(f"/r/{ws.slug}/sales", data={"f1": "D", "f2": "d@corp.com"}, follow_redirects=False)
    assert r.status_code == 200  # fallback message, never a 500
    db.rollback()
    db.expire_all()
    assert "503" in (db.get(CrmConnection, conn.id).last_error or "")


def test_fallback_cannot_be_crm_lookup(client, db, host, login):
    user, ws = host
    rf = _form(db, user, ws, {"type": "message", "value": "x"})
    login(client, user)
    client.post(f"/app/routing/{rf.id}/fallback", data={"fb_type": "salesforce_lookup", "fb_salesforce_lookup": ""})
    db.rollback()
    db.expire_all()
    assert db.get(RoutingForm, rf.id).fallback_json["type"] == "message"
    client.post(f"/app/routing/{rf.id}/rules", data={"c1_field": "f2", "c1_op": "not_empty",
                                                      "dest_type": "salesforce_lookup", "dest_salesforce_lookup": "intro"})
    db.rollback()
    db.expire_all()
    assert db.get(RoutingForm, rf.id).rules_json[-1]["destination"] == {"type": "salesforce_lookup", "value": "intro"}


# ------------------------------------------------------------------ adapters
def test_salesforce_adapter_lookup_refresh_and_task(db, host, monkeypatch):
    user, ws = host
    monkeypatch.setenv("SALESFORCE_CLIENT_ID", "cid")
    monkeypatch.setenv("SALESFORCE_CLIENT_SECRET", "sec")
    conn = crm.connect(db, ws.id, "salesforce", access_token="old", refresh_token="rt", instance_url="https://sf.example")
    calls = []

    def sf(method, url, **kw):
        calls.append((method, url, kw))
        if url.endswith("/services/oauth2/token"):
            assert kw["data"]["grant_type"] == "refresh_token" and kw["data"]["refresh_token"] == "rt"
            return FakeResp(200, {"access_token": "new", "instance_url": "https://sf.example"})
        if kw["headers"]["Authorization"] == "Bearer old":
            return FakeResp(401, {})
        if method == "GET" and "FROM+Contact" in url:
            return FakeResp(200, {"records": []})
        if method == "GET" and "FROM+Lead" in url:
            return FakeResp(200, {"records": [{"Id": "00Q1", "Owner": {"Email": "Owner@Example.com"}}]})
        if method == "POST" and url.endswith("/sobjects/Task"):
            assert kw["json"]["WhoId"] == "00Q1" and kw["json"]["Subject"] == "Hello"
            return FakeResp(201, {"id": "00T9"})
        raise AssertionError(url)

    monkeypatch.setattr(crm, "http_request", sf)
    assert crm.provider_available("salesforce")
    hit = crm.lookup_owner(db, ws.id, "o'brien@corp.com")
    assert hit == {"record_type": "lead", "record_id": "00Q1", "owner_email": "owner@example.com", "provider": "salesforce"}
    assert "o%5C%27brien%40corp.com" in calls[2][1]  # SOQL-escaped quote
    assert crm.adapter_for(db, conn).push_activity("Hello", "body", "x@corp.com") == "00T9"
    url = crm.SalesforceCrm.authorize_url("st")
    assert "client_id=cid" in url and "state=st" in url


def test_hubspot_adapter_lookup(db, host, monkeypatch):
    user, ws = host
    monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", "pat")
    crm.connect(db, ws.id, "hubspot")

    def hs(method, url, **kw):
        assert kw["headers"]["Authorization"] == "Bearer pat"
        if url.endswith("/crm/v3/objects/contacts/search"):
            return FakeResp(200, {"results": [{"id": "501", "properties": {"hubspot_owner_id": "77"}}]})
        if url.endswith("/crm/v3/owners/77"):
            return FakeResp(200, {"email": "Rep@Example.com"})
        raise AssertionError(url)

    monkeypatch.setattr(crm, "http_request", hs)
    assert crm.lookup_owner(db, ws.id, "a@b.com")["owner_email"] == "rep@example.com"


# ------------------------------------------------------------------ outbox -> CRM activities
def _booking(db, ws, et, host_id, email, status="confirmed", days=2, no_show=False, company=None):
    start = utcnow() + dt.timedelta(days=days)
    b = Booking(workspace_id=ws.id, event_type_id=et.id, host_id=host_id, start_at=start,
                end_at=start + dt.timedelta(minutes=30), status=status, manage_token=opaque_token(), exclusive=False,
                no_show=no_show, cancelled_at=utcnow() if status == "cancelled" else None)
    db.add(b)
    db.flush()
    db.add(Invitee(booking_id=b.id, name="Pat Doe", email=email, company=company))
    db.flush()
    return b


def test_booking_and_routing_events_pushed_to_crm_idempotently(db, host):
    user, ws = host
    bob, et = _owner(db, ws)
    crm.connect(db, ws.id, "mock")
    db.add(CrmRecord(workspace_id=ws.id, email_normalized="pat@acme.com", owner_email="bob@example.com"))
    rf = _form(db, user, ws, {"type": "message", "value": "x"})
    b = _booking(db, ws, et, bob.id, "pat@acme.com")
    sub = RoutingSubmission(workspace_id=ws.id, form_id=rf.id, answers_json={"f1": "Pat", "f2": "pat@acme.com"},
                            destination_json={"type": "message", "value": "x"})
    db.add(sub)
    db.flush()
    publish(db, "booking.created", {"booking_id": b.id}, workspace_id=ws.id)
    ev = publish(db, "routing.submitted", {"submission_id": sub.id, "form_id": rf.id}, workspace_id=ws.id)
    db.commit()
    process_outbox(db)
    acts = db.scalars(select(CrmActivity).order_by(CrmActivity.topic)).all()
    assert [(a.topic, a.status, a.email) for a in acts] == [
        ("booking.created", "sent", "pat@acme.com"), ("routing.submitted", "sent", "pat@acme.com")]
    assert acts[0].subject == "Meeting booked: Intro" and acts[0].external_id.startswith("mock:crr")
    assert "Work email: pat@acme.com" in acts[1].body
    # re-delivery of the same outbox event is a no-op
    crm.push_activity_for_event(db, ev)
    db.commit()
    assert db.scalar(select(func.count(CrmActivity.id))) == 2


def test_failed_push_recorded_and_retried(db, host, monkeypatch):
    user, ws = host
    bob, et = _owner(db, ws)
    crm.connect(db, ws.id, "hubspot", access_token="t")
    b = _booking(db, ws, et, bob.id, "pat@acme.com")
    publish(db, "booking.created", {"booking_id": b.id}, workspace_id=ws.id)
    db.commit()
    monkeypatch.setattr(crm, "http_request", lambda *a, **k: FakeResp(500, {}))
    process_outbox(db)  # does not raise / block other subscribers
    act = db.scalar(select(CrmActivity))
    assert act.status == "failed" and act.attempts == 1

    def ok(method, url, **kw):
        if url.endswith("/search"):
            return FakeResp(200, {"results": []})
        return FakeResp(201, {"id": "note1"})

    monkeypatch.setattr(crm, "http_request", ok)
    assert crm.retry_failed(db) == 1
    assert act.status == "sent" and act.external_id == "note1"


def test_no_crm_connected_means_no_activity(db, host):
    user, ws = host
    bob, et = _owner(db, ws)
    b = _booking(db, ws, et, bob.id, "pat@acme.com")
    publish(db, "booking.created", {"booking_id": b.id}, workspace_id=ws.id)
    db.commit()
    process_outbox(db)
    assert db.scalar(select(func.count(CrmActivity.id))) == 0


# ------------------------------------------------------------------ CRM UI
def test_crm_ui_connect_records_lookup_disconnect(client, db, host, login):
    user, ws = host
    login(client, user)
    r = client.get("/app/contacts/crm")
    assert r.status_code == 200 and "Built-in demo CRM" in r.text
    r = client.post("/app/contacts/crm/connect", data={"provider": "salesforce"}, follow_redirects=False)
    assert db.scalar(select(CrmConnection)) is None  # not configured on this server
    db.rollback()
    db.expire_all()
    client.post("/app/contacts/crm/connect", data={"provider": "mock"})
    client.post("/app/contacts/crm/records", data={"email": "Lead@X.com", "object_type": "lead",
                                                   "owner_email": "alice@example.com", "name": "Lee"})
    rec = db.scalar(select(CrmRecord))
    assert rec.email_normalized == "lead@x.com" and rec.owner_email == "alice@example.com"
    r = client.post("/app/contacts/crm/lookup", data={"email": "lead@x.com"}, headers={"accept": "application/json"})
    assert r.json()["result"]["owner_email"] == "alice@example.com"
    r = client.get("/app/contacts/crm")
    assert "lead@x.com" in r.text and "Disconnect" in r.text
    client.post(f"/app/contacts/crm/records/{rec.id}/delete")
    client.post("/app/contacts/crm/disconnect")
    db.rollback()
    db.expire_all()
    assert db.scalar(select(func.count(CrmRecord.id))) == 0 and db.scalar(select(CrmConnection)) is None


def test_crm_oauth_callback_checks_state(client, db, host, login, monkeypatch):
    user, ws = host
    monkeypatch.setenv("SALESFORCE_CLIENT_ID", "cid")
    monkeypatch.setenv("SALESFORCE_CLIENT_SECRET", "sec")
    login(client, user)
    r = client.post("/app/contacts/crm/connect", data={"provider": "salesforce"}, follow_redirects=False)
    loc = r.headers["location"]
    assert loc.startswith("https://login.salesforce.com/services/oauth2/authorize")
    state = parse_qs(urlparse(loc).query)["state"][0]
    client.get("/app/contacts/crm/salesforce/callback?code=c&state=wrong")
    assert db.scalar(select(CrmConnection)) is None
    db.rollback()
    db.expire_all()
    r = client.post("/app/contacts/crm/connect", data={"provider": "salesforce"}, follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    monkeypatch.setattr(crm, "http_request", lambda m, u, **k: FakeResp(200, {
        "access_token": "AT", "refresh_token": "RT", "instance_url": "https://na1.sf.example", "id": "org/user"}))
    client.get(f"/app/contacts/crm/salesforce/callback?code=c&state={state}")
    db.rollback()
    db.expire_all()
    conn = db.scalar(select(CrmConnection))
    assert conn.provider == "salesforce" and conn.instance_url == "https://na1.sf.example"
    assert conn.access_token_enc != "AT"  # encrypted at rest


# ------------------------------------------------------------------ routing prefill mapping
def test_explicit_prefill_mapping(client, db, host, login):
    user, ws = host
    et = EventType(workspace_id=ws.id, owner_user_id=user.id, name="Demo", slug="demo", duration=30)
    db.add(et)
    db.flush()
    q = EventQuestion(event_type_id=et.id, label="How many seats?", type="text", sort_order=1)
    db.add(q)
    db.commit()
    rf = _form(db, user, ws, {"type": "event_type", "value": et.id})
    # without mapping: label heuristic can't see "Full legal name" as a name field? it contains "name" -> prefilled
    r = client.post(f"/r/{ws.slug}/sales", data={"f1": "Dana", "f2": "d@corp.com", "f3": "12"}, follow_redirects=False)
    q0 = parse_qs(urlparse(r.headers["location"]).query)
    assert "a1" not in q0  # "Headcount" label does not match "How many seats?"
    login(client, user)
    page = client.get(f"/app/routing/{rf.id}")
    assert f'value="q:{q.id}"' in page.text and 'value="label:How many seats?"' in page.text
    r = client.post(f"/app/routing/{rf.id}/prefill", data={"map_f1": "name", "map_f2": "email", "map_f3": f"q:{q.id}"})
    r = client.post(f"/r/{ws.slug}/sales", data={"f1": "Dana", "f2": "d@corp.com", "f3": "12"}, follow_redirects=False)
    q1 = parse_qs(urlparse(r.headers["location"]).query)
    assert q1["name"] == ["Dana"] and q1["email"] == ["d@corp.com"] and q1["a1"] == ["12"]
    # explicit mapping replaces heuristics: unmapped email field is no longer prefilled
    client.post(f"/app/routing/{rf.id}/prefill", data={"map_f3": "label:How many seats?"})
    r = client.post(f"/r/{ws.slug}/sales", data={"f1": "Dana", "f2": "d@corp.com", "f3": "7"}, follow_redirects=False)
    q2 = parse_qs(urlparse(r.headers["location"]).query)
    assert q2["a1"] == ["7"] and "email" not in q2 and "name" not in q2
    # invalid target and duplicate basic target rejected
    client.post(f"/app/routing/{rf.id}/prefill", data={"map_f1": "q:nope"})
    client.post(f"/app/routing/{rf.id}/prefill", data={"map_f1": "email", "map_f2": "email"})
    db.rollback()
    db.expire_all()
    assert crm.prefill_map(db, rf.id) == {"f3": "label:How many seats?"}


# ------------------------------------------------------------------ contacts P3
def test_followups_and_one_click_email(client, db, host, login):
    user, ws = host
    et = EventType(workspace_id=ws.id, owner_user_id=user.id, name="Intro", slug="intro", duration=30)
    db.add(et)
    db.flush()
    rf = _form(db, user, ws, {"type": "message", "value": "x"})
    db.add(RoutingSubmission(workspace_id=ws.id, form_id=rf.id, answers_json={"f2": "lead@corp.com"}))
    db.add(RoutingSubmission(workspace_id=ws.id, form_id=rf.id, answers_json={"f2": "booked@corp.com"}))
    _booking(db, ws, et, user.id, "booked@corp.com")
    _booking(db, ws, et, user.id, "gone@corp.com", status="cancelled")
    _booking(db, ws, et, user.id, "rebooked@corp.com", status="cancelled")
    _booking(db, ws, et, user.id, "rebooked@corp.com", days=5)
    unsub = contacts_svc.upsert_contact(db, ws.id, "lead@corp.com", name="Lena Lead")
    db.commit()
    items = {i["email"]: i["kind"] for i in contacts_svc.no_booking_followups(db, ws.id)}
    assert items == {"lead@corp.com": "routing", "gone@corp.com": "cancelled"}

    login(client, user)
    r = client.get("/app/contacts/follow-ups")
    assert "lead@corp.com" in r.text and "Cancelled, not rebooked" in r.text
    db.rollback()
    db.expire_all()
    r = client.post("/app/contacts/follow-ups/send", data={"email": "lead@corp.com", "event_type_id": et.id})
    db.rollback()
    db.expire_all()
    msg = db.scalar(select(SentMessage).where(SentMessage.recipient == "lead@corp.com"))
    assert msg and "/alice/intro?name=Lena" in msg.body and "Hi Lena" in msg.body
    assert db.scalar(select(ContactFollowUp)).source == "routing"
    assert "Follow up again" in client.get("/app/contacts/follow-ups").text

    unsub.unsubscribed = True
    db.commit()
    client.post("/app/contacts/follow-ups/send", data={"email": "lead@corp.com"})
    db.rollback()
    db.expire_all()
    assert db.scalar(select(func.count(SentMessage.id)).where(SentMessage.recipient == "lead@corp.com")) == 1
    # not an opportunity -> nothing sent
    client.post("/app/contacts/follow-ups/send", data={"email": "booked@corp.com"})
    db.rollback()
    db.expire_all()
    assert db.scalar(select(func.count(SentMessage.id)).where(SentMessage.recipient == "booked@corp.com")) == 0


def test_relationship_insights(client, db, host, login):
    user, ws = host
    et = EventType(workspace_id=ws.id, owner_user_id=user.id, name="Intro", slug="intro", duration=30)
    et2 = EventType(workspace_id=ws.id, owner_user_id=user.id, name="Review", slug="review", duration=60)
    db.add_all([et, et2])
    db.flush()
    e = "pat@acme.com"
    _booking(db, ws, et, user.id, e, days=-10, company="Acme")
    _booking(db, ws, et, user.id, e, days=-5, no_show=True)
    _booking(db, ws, et2, user.id, e, days=-3, status="cancelled")
    nxt = _booking(db, ws, et2, user.id, e, days=4)
    _booking(db, ws, et, user.id, "sam@acme.com", days=-1)
    c = contacts_svc.upsert_contact(db, ws.id, e, name="Pat", company="Acme")
    contacts_svc.upsert_contact(db, ws.id, "sam@acme.com", company="ACME")
    db.commit()
    ins = contacts_svc.relationship_insights(db, ws.id, e)
    assert ins["meetings"] == 3 and ins["cancelled"] == 1 and ins["no_shows"] == 1 and ins["completed"] == 1
    assert ins["cancel_rate"] == 25.0 and ins["no_show_rate"] == 50.0
    assert ins["next_booking_id"] == nxt.id and ins["last_meeting"] is not None
    assert ins["top_event_types"][0] == {"event_type_id": et.id, "name": "Intro", "count": 2}
    comp = contacts_svc.company_insights(db, ws.id)
    assert len(comp) == 1 and comp[0]["contacts"] == 2 and comp[0]["meetings"] == 4

    login(client, user)
    r = client.get(f"/app/contacts/{c.id}")
    assert r.status_code == 200 and "Relationship insights" in r.text and "25%" in r.text
    r = client.get("/app/contacts/insights")
    assert r.status_code == 200 and "Acme" in r.text
    r = client.get("/app/contacts/insights", headers={"accept": "application/json"})
    assert r.json()["companies"][0]["meetings"] == 4


def test_delete_email_data_hook(db, host):
    user, ws = host
    crm.connect(db, ws.id, "mock")
    db.add(CrmRecord(workspace_id=ws.id, email_normalized="pat@acme.com", owner_email="x@y.com"))
    db.add(CrmActivity(workspace_id=ws.id, provider="mock", topic="booking.created", source_event_id="e1",
                       subject="s", email="pat@acme.com", body="secret"))
    db.add(ContactFollowUp(workspace_id=ws.id, email_normalized="pat@acme.com", source="routing"))
    db.commit()
    assert crm.delete_email_data(db, ws.id, "Pat@Acme.com") == {"crm_records": 1, "crm_activities": 1, "follow_ups": 1}
    db.commit()
    assert db.scalar(select(CrmActivity)).body == "[deleted]"
