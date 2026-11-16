from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import func, select

from app.models import EventQuestion, EventType, OutboxEvent, PageView, RoutingForm, RoutingSubmission
from app.services.routing_engine import evaluate, safe_external_url, validate_destination

FALLBACK = {"type": "message", "value": "fallback"}


def _rule(rid, logic, conds, dest="x"):
    return {"id": rid, "logic": logic, "conditions": conds, "destination": {"type": "message", "value": dest}}


@pytest.mark.parametrize("op,value,answer,expected", [
    ("equals", "Enterprise", "enterprise", True),
    ("equals", "Enterprise", "SMB", False),
    ("not_equals", "SMB", "Enterprise", True),
    ("not_equals", "SMB", "smb", False),
    ("contains", "acme", "sales@ACME.com", True),
    ("contains", "acme", "x@y.com", False),
    ("contains", "Zoom", ["Zoom", "Meet"], True),
    ("gt", "100", "250", True),
    ("gt", "100", "50", False),
    ("gt", "100", "abc", False),
    ("lt", "100", "50", True),
    ("lt", "100", "150", False),
    ("empty", None, "", True),
    ("empty", None, None, True),
    ("empty", None, "x", False),
    ("not_empty", None, "x", True),
    ("not_empty", None, [], False),
])
def test_all_operators(op, value, answer, expected):
    rid, _ = evaluate([_rule("r1", "and", [{"field": "f1", "op": op, "value": value}])], FALLBACK, {"f1": answer})
    assert (rid == "r1") is expected


def test_and_or_groups_and_fallback():
    rules = [
        _rule("big", "and", [{"field": "size", "op": "gt", "value": 200}, {"field": "region", "op": "equals", "value": "EU"}], "eu-ent"),
        _rule("any", "or", [{"field": "region", "op": "equals", "value": "US"}, {"field": "budget", "op": "gt", "value": 50000}], "us"),
        _rule("nested", "and", [
            {"field": "product", "op": "not_empty"},
            {"logic": "or", "conditions": [{"field": "size", "op": "lt", "value": 10}, {"field": "email", "op": "contains", "value": ".edu"}]},
        ], "nested"),
    ]
    assert evaluate(rules, FALLBACK, {"size": "500", "region": "EU"}) == ("big", {"type": "message", "value": "eu-ent"})
    assert evaluate(rules, FALLBACK, {"size": "500", "region": "APAC", "budget": "60000"})[0] == "any"
    assert evaluate(rules, FALLBACK, {"size": "50", "region": "US"})[0] == "any"
    assert evaluate(rules, FALLBACK, {"size": "5", "product": "x"})[0] == "nested"
    assert evaluate(rules, FALLBACK, {"size": "50", "product": "x", "email": "a@uni.edu"})[0] == "nested"
    assert evaluate(rules, FALLBACK, {"size": "50", "product": "x", "email": "a@corp.com"}) == (None, FALLBACK)
    assert evaluate([], FALLBACK, {}) == (None, FALLBACK)


def test_url_safety():
    assert safe_external_url("https://example.com/x")
    assert safe_external_url("javascript:alert(1)") is None
    assert safe_external_url("//evil.com") is None
    assert validate_destination({"type": "url", "value": "ftp://x"})
    assert validate_destination({"type": "message", "value": "hi"}) is None


def _setup_form(db, user, ws):
    et = EventType(workspace_id=ws.id, owner_user_id=user.id, name="Sales demo", slug="demo", duration=30)
    db.add(et)
    db.flush()
    db.add(EventQuestion(event_type_id=et.id, label="Company size", type="text", sort_order=1))
    rf = RoutingForm(
        workspace_id=ws.id, owner_user_id=user.id, name="Contact sales", slug="sales",
        form_schema_json=[
            {"id": "f1", "type": "text", "label": "Your name", "required": True},
            {"id": "f2", "type": "email", "label": "Work email", "required": True},
            {"id": "f3", "type": "number", "label": "Company size", "required": False},
            {"id": "f4", "type": "select", "label": "Region", "options": ["EU", "US"], "required": False},
        ],
        rules_json=[{"id": "r_ent", "name": "Enterprise", "logic": "and",
                     "conditions": [{"field": "f3", "op": "gt", "value": "100"}],
                     "destination": {"type": "event_type", "value": et.id}},
                    {"id": "r_bad", "logic": "and", "conditions": [{"field": "f4", "op": "equals", "value": "US"}],
                     "destination": {"type": "url", "value": "https://example.com/us"}}],
        fallback_json={"type": "message", "value": "Thanks, we'll email you."},
    )
    db.add(rf)
    db.commit()
    return et, rf


def test_public_submission_routes_with_prefill(client, db, host):
    user, ws = host
    et, rf = _setup_form(db, user, ws)
    r = client.get(f"/r/{ws.slug}/sales?utm_source=ads")
    assert r.status_code == 200 and "Contact sales" in r.text
    r = client.post(f"/r/{ws.slug}/sales", data={"f1": "Dana Lee", "f2": "dana@corp.com", "f3": "500", "utm_source": "ads"},
                    follow_redirects=False)
    assert r.status_code == 303
    url = urlparse(r.headers["location"])
    q = parse_qs(url.query)
    assert url.path == "/alice/demo"
    assert q["name"] == ["Dana Lee"] and q["email"] == ["dana@corp.com"] and q["a1"] == ["500"] and q["utm_source"] == ["ads"]
    db.rollback()
    db.expire_all()
    sub = db.scalar(select(RoutingSubmission))
    assert q["rsid"] == [sub.id] and sub.matched_rule_id == "r_ent" and sub.answers_json["f3"] == "500"
    ev = db.scalar(select(OutboxEvent).where(OutboxEvent.topic == "routing.submitted"))
    assert ev.payload_json == {"submission_id": sub.id, "form_id": rf.id}
    assert db.scalar(select(func.count(PageView.id)).where(PageView.page == "routing")) == 1
    # url destination
    r = client.post(f"/r/{ws.slug}/sales", data={"f1": "U", "f2": "u@x.com", "f4": "US"}, follow_redirects=False)
    assert r.headers["location"] == "https://example.com/us"
    # fallback message
    r = client.post(f"/r/{ws.slug}/sales", data={"f1": "S", "f2": "s@x.com", "f3": "5"})
    assert r.status_code == 200 and "we&#39;ll email you" in r.text


def test_public_validation_and_honeypot(client, db, host):
    user, ws = host
    _setup_form(db, user, ws)
    r = client.post(f"/r/{ws.slug}/sales", data={"f1": "", "f2": "not-an-email"})
    assert r.status_code == 400 and "required" in r.text
    r = client.post(f"/r/{ws.slug}/sales", data={"f1": "Bot", "f2": "b@x.com", "website": "spam.com"})
    assert r.status_code == 200
    db.rollback()
    db.expire_all()
    assert db.scalar(select(func.count(RoutingSubmission.id))) == 0
    assert client.get(f"/r/{ws.slug}/nope").status_code == 404


def test_preview_does_not_persist(client, db, host, login):
    user, ws = host
    et, rf = _setup_form(db, user, ws)
    login(client, user)
    r = client.post(f"/app/routing/{rf.id}/preview", data={"f1": "T", "f2": "t@x.com", "f3": "1000"})
    assert r.status_code == 200 and "Enterprise" in r.text and "/alice/demo" in r.text
    r = client.post(f"/app/routing/{rf.id}/preview", data={"f1": "T", "f2": "t@x.com", "f3": "1"},
                    headers={"accept": "application/json"})
    assert r.json()["matched_rule_id"] is None
    db.rollback()
    db.expire_all()
    assert db.scalar(select(func.count(RoutingSubmission.id))) == 0
    assert db.scalar(select(func.count(OutboxEvent.id))) == 0


def test_builder_flow_and_csv(client, db, host, login):
    user, ws = host
    et, rf = _setup_form(db, user, ws)
    login(client, user)
    assert client.get("/app/routing").status_code == 200
    assert client.get(f"/app/routing/{rf.id}").status_code == 200
    client.post(f"/app/routing/{rf.id}/questions", data={"label": "Interests", "type": "checkbox", "options": "A, B"})
    client.post(f"/app/routing/{rf.id}/rules", data={"name": "Likes A", "logic": "or", "c1_field": "f5", "c1_op": "contains",
                                                     "c1_value": "A", "dest_type": "url", "dest_url": "javascript:x"})
    db.rollback()
    db.expire_all()
    rf2 = db.get(RoutingForm, rf.id)
    assert rf2.form_schema_json[-1]["id"] == "f5" and len(rf2.rules_json) == 2  # unsafe url rejected
    client.post(f"/app/routing/{rf.id}/rules", data={"name": "Likes A", "logic": "or", "c1_field": "f5", "c1_op": "contains",
                                                     "c1_value": "A", "dest_type": "message", "dest_message": "A fan"})
    # fallback can't be cleared
    client.post(f"/app/routing/{rf.id}/fallback", data={"fb_type": "url", "fb_url": ""})
    db.rollback()
    db.expire_all()
    rf2 = db.get(RoutingForm, rf.id)
    assert len(rf2.rules_json) == 3 and rf2.fallback_json["type"] == "message"
    client.post(f"/r/{ws.slug}/sales", data={"f1": "=cmd", "f2": "c@x.com", "f5": ["A", "B"]})
    r = client.get(f"/app/routing/{rf.id}/submissions")
    assert r.status_code == 200 and "Likes A" in r.text
    r = client.get(f"/app/routing/{rf.id}/submissions.csv")
    assert r.status_code == 200 and "'=cmd" in r.text and "A, B" in r.text
    # create via UI
    r = client.post("/app/routing", data={"name": "Support"}, follow_redirects=False)
    assert r.status_code == 303
    db.rollback()
    db.expire_all()
    new = db.scalar(select(RoutingForm).where(RoutingForm.slug == "support"))
    assert new.fallback_json and new.fallback_json["type"] == "message"


def test_routing_requires_plan(client, db, host, login):
    user, ws = host
    ws.plan_id = "standard"
    db.commit()
    login(client, user)
    assert client.get("/app/routing").status_code == 403


def test_preview_and_public_pages_render(client, db, host, login):
    user, ws = host
    et, rf = _setup_form(db, user, ws)
    login(client, user)
    r = client.get(f"/app/routing/{rf.id}/preview")
    assert r.status_code == 200 and "Test mode" in r.text
    r = client.get(f"/app/routing/{rf.id}")
    assert "Salesforce" in r.text and "salesforce_lookup" in r.text and "Booking page prefill" in r.text
