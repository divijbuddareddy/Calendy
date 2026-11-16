import datetime as dt

from sqlalchemy import select

from app.models import AuditLog, EventQuestion, EventType, EventTypeHost, Workspace, WorkspaceMember
from app.services.bootstrap import add_user_to_workspace, create_user_with_workspace


def fresh(db):
    db.rollback()
    db.expire_all()


def _create(client, name="Intro Call", **extra):
    data = {"name": name, "duration": "30", "kind": "one_on_one", "location": "google_meet", **extra}
    r = client.post("/app/event-types", data=data, follow_redirects=False)
    assert r.status_code == 303, r.text
    return r.headers["location"].split("/")[3]


def test_event_type_crud(client, db, host, login):
    user, ws = host
    login(client, user)
    eid = _create(client)
    fresh(db)
    ev = db.get(EventType, eid)
    assert ev.slug == "intro-call" and ev.owner_user_id == user.id and ev.version == 1
    assert client.get("/app/event-types").status_code == 200
    for s in ["basics", "location", "availability", "questions", "confirmation", "policy", "payment", "permissions"]:
        assert client.get(f"/app/event-types/{eid}/edit?section={s}").status_code == 200, s

    # basics with multiple durations
    r = client.post(f"/app/event-types/{eid}", data={"section": "basics", "name": "Discovery", "slug": "discovery",
                                                    "duration": "30", "kind": "one_on_one", "color": "#112233",
                                                    "multiple_durations": "1", "durations": "15, 60"},
                    follow_redirects=False)
    assert r.status_code == 303
    fresh(db)
    ev = db.get(EventType, eid)
    assert ev.name == "Discovery" and ev.slug == "discovery" and ev.durations_json == [15, 30, 60] and ev.version == 2

    # location: multiple options + validation
    client.post(f"/app/event-types/{eid}", data={"section": "location", "loc_type": ["zoom", "in_person"],
                                                "loc_value": ["", "1 Main St"]})
    # availability: custom rules
    client.post(f"/app/event-types/{eid}", data={
        "section": "availability", "availability_source": "custom", "custom_0": "09:00-12:00, 13:00-17:00",
        "custom_2": "10:00-11:00", "slot_interval": "15", "min_notice_value": "4", "min_notice_unit": "hours",
        "max_days_ahead": "30", "buffer_before": "0", "buffer_after": "15", "daily_limit": "3"})
    # overlapping custom rules rejected
    client.post(f"/app/event-types/{eid}", data={"section": "availability", "availability_source": "custom",
                                                "custom_1": "09:00-12:00, 11:00-13:00", "max_days_ahead": "30"})
    # confirmation + policy + payment + permissions
    client.post(f"/app/event-types/{eid}", data={"section": "confirmation", "confirmation_message": "Thanks!",
                                                "redirect_url": "", "links": "Agenda | https://example.com/a",
                                                "calendar_event_title": "{event_name} / {invitee_name} / {host_name}"})
    r = client.post(f"/app/event-types/{eid}", data={"section": "confirmation", "calendar_event_title": "{bogus}"},
                    follow_redirects=False)
    client.post(f"/app/event-types/{eid}", data={"section": "policy", "allow_cancel": "1", "require_reason": "1",
                                                "host_approval": "1", "policy_min_notice": "120", "policy_text": "24h"})
    client.post(f"/app/event-types/{eid}", data={"section": "payment", "payment_enabled": "1", "price": "50",
                                                "currency": "GBP", "payment_provider": "stripe"})
    client.post(f"/app/event-types/{eid}", data={"section": "permissions", "visibility": "secret"})
    fresh(db)
    ev = db.get(EventType, eid)
    assert ev.location_json == [{"type": "zoom", "value": ""}, {"type": "in_person", "value": "1 Main St"}]
    assert {"weekday": 0, "start": "13:00", "end": "17:00"} in ev.custom_rules_json
    assert ev.min_notice_minutes == 240 and ev.max_days_ahead == 30 and ev.buffer_after == 15 and ev.daily_limit == 3
    assert ev.slot_interval == 15
    assert ev.confirmation_json["links"] == [{"label": "Agenda", "url": "https://example.com/a"}]
    assert "{host_name}" in ev.calendar_event_title
    assert ev.cancellation_policy_json["host_approval"] and ev.cancellation_policy_json["min_notice_minutes"] == 120
    assert ev.price_minor == 5000 and ev.currency == "GBP"
    assert ev.visibility == "secret"
    assert db.scalar(select(AuditLog).where(AuditLog.action == "event_type.updated", AuditLog.entity_id == eid))

    # questions add / reorder / edit / delete
    client.post(f"/app/event-types/{eid}/questions", data={"type": "text", "label": "Company?", "required": "1"})
    client.post(f"/app/event-types/{eid}/questions", data={"type": "select", "label": "Topic", "options": "A\nB"})
    r = client.post(f"/app/event-types/{eid}/questions", data={"type": "radio", "label": "No options"})
    fresh(db)
    qs = db.scalars(select(EventQuestion).where(EventQuestion.event_type_id == eid).order_by(EventQuestion.sort_order)).all()
    assert [q.label for q in qs] == ["Company?", "Topic"] and qs[1].options_json == ["A", "B"]
    client.post(f"/app/event-types/{eid}/questions/{qs[1].id}/move?dir=up")
    client.post(f"/app/event-types/{eid}/questions/{qs[0].id}", data={"type": "consent", "label": "I agree",
                                                                       "required": "1"})
    client.post(f"/app/event-types/{eid}/questions/{qs[1].id}/delete")
    fresh(db)
    qs = db.scalars(select(EventQuestion).where(EventQuestion.event_type_id == eid).order_by(EventQuestion.sort_order)).all()
    assert [(q.label, q.type) for q in qs] == [("I agree", "consent")]

    # slug collision rejected
    eid2 = _create(client, name="Other")
    client.post(f"/app/event-types/{eid2}", data={"section": "basics", "name": "Other", "slug": "discovery",
                                                 "duration": "30", "kind": "one_on_one", "color": "#112233"})
    fresh(db)
    assert db.get(EventType, eid2).slug == "other"

    # delete (soft)
    r = client.post(f"/app/event-types/{eid2}/delete", follow_redirects=False)
    assert r.status_code == 303
    fresh(db)
    assert db.get(EventType, eid2).deleted_at is not None
    assert client.get(f"/app/event-types/{eid2}/edit").status_code == 404
    assert db.scalar(select(AuditLog).where(AuditLog.action == "event_type.deleted"))


def test_duplicate_pause_archive(client, db, host, login):
    user, _ = host
    login(client, user)
    eid = _create(client)
    client.post(f"/app/event-types/{eid}/questions", data={"type": "text", "label": "Why?"})
    r = client.post(f"/app/event-types/{eid}/duplicate", follow_redirects=False)
    dup_id = r.headers["location"].split("/")[3]
    fresh(db)
    dup = db.get(EventType, dup_id)
    assert dup.id != eid and dup.slug == "intro-call-copy" and dup.name.endswith("(copy)")
    assert [q.label for q in dup.questions] == ["Why?"]
    client.post(f"/app/event-types/{eid}/status", data={"action": "pause"})
    fresh(db)
    assert db.get(EventType, eid).status == "paused"
    client.post(f"/app/event-types/{eid}/status", data={"action": "activate"})
    client.post(f"/app/event-types/{dup_id}/status", data={"action": "archive"})
    fresh(db)
    assert db.get(EventType, eid).status == "active" and db.get(EventType, dup_id).status == "archived"
    assert "Intro Call (copy)" in client.get("/app/event-types?tab=archived").text


def test_plan_limits_and_gating(client, db, host, login):
    user, ws = host
    ws = db.get(Workspace, ws.id)
    ws.plan_id = "free"
    db.commit()
    login(client, user)
    _create(client)
    r = client.post("/app/event-types", data={"name": "Second", "duration": "30", "kind": "one_on_one"})
    assert r.status_code == 400 and "allows 1 active" in r.text
    r = client.post("/app/event-types", data={"name": "Group", "duration": "30", "kind": "group"})
    assert r.status_code == 400
    assert client.get("/app/event-types/one-off/new").status_code == 403


def test_locked_field_rejected_for_member(client, db, host, login):
    owner, ws = host
    member = add_user_to_workspace(db, ws, "mem@example.com", "Mem", role="member", password="password123")
    ev = EventType(workspace_id=ws.id, owner_user_id=member.id, name="Managed", slug="managed", duration=30,
                   locked_fields_json=["duration", "location"], managed_template_id="evt_tpl")
    db.add(ev)
    db.commit()
    login(client, member)
    page = client.get(f"/app/event-types/{ev.id}/edit")
    assert page.status_code == 200 and "Locked" in page.text
    r = client.post(f"/app/event-types/{ev.id}", data={"section": "basics", "name": "Managed", "slug": "managed",
                                                      "duration": "60", "kind": "one_on_one", "color": "#112233"})
    assert r.status_code == 403
    # unlocked field still editable when locked input is omitted (disabled)
    r = client.post(f"/app/event-types/{ev.id}", data={"section": "basics", "name": "Renamed", "slug": "managed",
                                                      "kind": "one_on_one", "color": "#112233"}, follow_redirects=False)
    assert r.status_code == 303
    r = client.post(f"/app/event-types/{ev.id}", data={"section": "location", "loc_type": "zoom", "loc_value": ""})
    assert r.status_code == 403
    fresh(db)
    ev = db.get(EventType, ev.id)
    assert ev.duration == 30 and ev.name == "Renamed"
    # admins may change locked fields
    login(client, owner)
    client.post(f"/app/event-types/{ev.id}", data={"section": "basics", "name": "Renamed", "slug": "managed",
                                                  "duration": "45", "kind": "one_on_one", "color": "#112233"})
    fresh(db)
    assert db.get(EventType, ev.id).duration == 45


def test_one_off_and_single_use(client, db, host, login):
    user, _ = host
    login(client, user)
    future = (dt.datetime.utcnow() + dt.timedelta(days=3)).replace(hour=10, minute=0, second=0, microsecond=0)
    r = client.post("/app/event-types/one-off", data={
        "name": "Kickoff", "duration": "45", "location": "zoom",
        "slots": [future.strftime("%Y-%m-%dT%H:%M"), (future + dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")]},
        follow_redirects=False)
    assert r.status_code == 303
    fresh(db)
    oo = db.scalar(select(EventType).where(EventType.is_one_off.is_(True)))
    assert oo.visibility == "secret" and len(oo.one_off_slots_json) == 2 and oo.duration == 45
    # Europe/London is UTC+0 in winter / +1 in summer: stored value is UTC
    stored = dt.datetime.fromisoformat(oo.one_off_slots_json[0])
    assert abs((stored - future).total_seconds()) <= 3600
    r = client.post("/app/event-types/one-off", data={"name": "Past", "slots": ["2020-01-01T10:00"]})
    assert r.status_code == 400

    eid = _create(client)
    r = client.post(f"/app/event-types/{eid}/single-use", follow_redirects=False)
    assert r.status_code == 303
    r = client.post(f"/app/event-types/{eid}/single-use", headers={"accept": "application/json"})
    assert r.json()["url"].split("/")[-1].startswith("su-")
    fresh(db)
    su = db.scalars(select(EventType).where(EventType.single_use.is_(True))).all()
    assert len(su) == 2 and all(e.visibility == "secret" and e.slug.startswith("su-") for e in su)
    page = client.get("/app/event-types").text
    assert "Unused" in page and "Kickoff" in page


def test_hosts_section_for_round_robin(client, db, host, login):
    owner, ws = host
    m = add_user_to_workspace(db, ws, "rr@example.com", "RR Host", password="password123")
    db.commit()
    login(client, owner)
    eid = _create(client, name="Sales demo", kind="round_robin")
    r = client.post(f"/app/event-types/{eid}", data={
        "section": "hosts", "host_ids": [owner.id, m.id], f"active_{owner.id}": "1", f"active_{m.id}": "1",
        f"priority_{owner.id}": "2", f"priority_{m.id}": "1", f"capacity_{m.id}": "4", "rr_strategy": "equal"},
        follow_redirects=False)
    assert r.status_code == 303
    fresh(db)
    hosts = {h.user_id: h for h in db.scalars(select(EventTypeHost).where(EventTypeHost.event_type_id == eid))}
    assert set(hosts) == {owner.id, m.id} and hosts[m.id].daily_capacity == 4 and hosts[owner.id].priority == 2
    assert db.get(EventType, eid).rr_strategy == "equal"


def test_cross_tenant_event_type_denied(client, db, host, login):
    user_a, ws_a = host
    user_b, ws_b = create_user_with_workspace(db, "bob@example.com", "Bob", password="password123", verified=True)
    ev = EventType(workspace_id=ws_a.id, owner_user_id=user_a.id, name="A's", slug="a-event", duration=30)
    db.add(ev)
    db.commit()
    login(client, user_b)
    assert client.get(f"/app/event-types/{ev.id}/edit").status_code == 404
    r = client.post(f"/app/event-types/{ev.id}", data={"section": "basics", "name": "pwned", "slug": "x",
                                                      "duration": "30", "kind": "one_on_one", "color": "#000000"})
    assert r.status_code == 404
    assert client.post(f"/app/event-types/{ev.id}/delete").status_code == 404
    assert client.post(f"/app/event-types/{ev.id}/duplicate").status_code == 404
    assert client.get(f"/app/event-types/{ev.id}/embed").status_code == 404
    assert "A&#39;s" not in client.get("/app/event-types").text
    fresh(db)
    assert db.get(EventType, ev.id).name == "A's" and db.get(EventType, ev.id).deleted_at is None


def test_member_cannot_edit_others_event(client, db, host, login):
    owner, ws = host
    member = add_user_to_workspace(db, ws, "m2@example.com", "M2", role="member", password="password123")
    ev = EventType(workspace_id=ws.id, owner_user_id=owner.id, name="Owner's", slug="owners", duration=30)
    db.add(ev)
    db.commit()
    login(client, member)
    assert client.get(f"/app/event-types/{ev.id}/edit").status_code == 404
    ev2 = db.get(EventType, ev.id)
    ev2.shared_with_json = [member.id]
    db.commit()
    assert client.get(f"/app/event-types/{ev.id}/edit").status_code == 200
