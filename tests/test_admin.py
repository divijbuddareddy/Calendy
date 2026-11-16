import datetime as dt

from sqlalchemy import select

from app.audit import audit
from app.models import (
    AuditLog,
    Booking,
    BookingAnswer,
    Contact,
    EventType,
    Invitee,
    NotificationJob,
    OutboxEvent,
    SSOConfig,
    User,
    Workspace,
    WorkspaceMember,
    utcnow,
)
from app.security import hash_secret
from app.services.bootstrap import add_user_to_workspace


def _enterprise(db, ws):
    w = db.get(Workspace, ws.id)
    w.plan_id = "enterprise"
    db.commit()


def test_role_matrix_admin_pages(client, db, host, login):
    user, ws = host
    member = add_user_to_workspace(db, ws, "mem@example.com", "Mem", role="member")
    viewer = add_user_to_workspace(db, ws, "view@example.com", "View", role="viewer")
    admin = add_user_to_workspace(db, ws, "adm@example.com", "Adm", role="admin")
    db.commit()
    for u in (member, viewer):
        login(client, u)
        assert client.get("/app/admin").status_code == 403
        assert client.get("/app/admin/audit").status_code == 403
        assert client.post("/app/admin/security", data={"retention_days": "100"}).status_code == 403
        assert client.get("/app/admin/health").status_code == 403
    login(client, admin)
    assert client.get("/app/admin").status_code == 200
    assert client.get("/app/admin/health").status_code == 403  # owner / platform operator only
    login(client, user)
    r = client.get("/app/admin")
    assert r.status_code == 200 and "Security policies" in r.text
    assert client.get("/app/admin/health").status_code == 200
    assert client.get("/app/admin/usage").status_code == 200


def test_security_settings_save(client, db, host, login):
    user, ws = host
    login(client, user)
    # teams plan: no domain control
    client.post("/app/admin/security", data={"approved_domains": "acme.com", "retention_days": "400",
                                              "quiet_start": "21:00", "quiet_end": "07:30"})
    db.rollback(); db.expire_all()
    s = db.get(Workspace, ws.id).settings_json
    assert s["retention_days"] == 400
    assert s["quiet_hours"]["start"] == "21:00" and s["quiet_hours"]["end"] == "07:30"
    assert "approved_domains" not in s
    _enterprise(db, ws)
    client.post("/app/admin/security", data={"approved_domains": "Acme.com, @example.org", "retention_days": "10"})
    db.rollback(); db.expire_all()
    s = db.get(Workspace, ws.id).settings_json
    assert s["approved_domains"] == ["acme.com", "example.org"]
    assert s["retention_days"] == 400  # 10 rejected (< 30)
    assert "quiet_hours" not in s
    assert db.scalar(select(AuditLog).where(AuditLog.action == "workspace.security_updated"))


def test_audit_search_and_export_gated(client, db, host, login):
    user, ws = host
    audit(db, "member.role_changed", workspace_id=ws.id, actor_id=user.id, entity_type="workspace_member", entity_id="wm_1")
    audit(db, "event_type.updated", workspace_id=ws.id, actor_id=user.id, entity_type="event_type", entity_id="evt_1")
    audit(db, "member.role_changed", workspace_id="ws_other", actor_id="x")
    db.commit()
    login(client, user)
    r = client.get("/app/admin/audit", params={"action": "role_changed"})
    assert r.status_code == 200 and "<code>member.role_changed</code>" in r.text and "<code>event_type.updated</code>" not in r.text
    r = client.get("/app/admin/audit", params={"actor": "alice@example.com", "entity": "event_type"})
    assert "<code>event_type.updated</code>" in r.text and "<code>member.role_changed</code>" not in r.text
    assert client.get("/app/admin/audit.csv").status_code == 403  # teams plan lacks audit_export
    _enterprise(db, ws)
    r = client.get("/app/admin/audit.csv", params={"action": "role"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().splitlines()
    assert lines[0].startswith("created_at_utc,action")
    assert len(lines) == 2 and "wm_1" in lines[1]  # other workspace's row excluded


def test_sso_config_and_scim_token(client, db, host, login):
    user, ws = host
    login(client, user)
    assert client.post("/app/admin/sso", data={"idp_entity_id": "x"}).status_code == 403
    _enterprise(db, ws)
    r = client.post("/app/admin/sso", data={"idp_entity_id": "https://idp.example.com", "idp_sso_url": "http://insecure"},
                    follow_redirects=False)
    assert r.status_code == 303
    db.rollback(); db.expire_all()
    assert db.scalar(select(SSOConfig).where(SSOConfig.workspace_id == ws.id)) is None
    client.post("/app/admin/sso", data={"idp_entity_id": "https://idp.example.com", "idp_sso_url": "https://idp.example.com/sso",
                                         "enabled": "1", "enforce_sso": "1"})
    db.rollback(); db.expire_all()
    cfg = db.scalar(select(SSOConfig).where(SSOConfig.workspace_id == ws.id))
    assert cfg.idp_sso_url == "https://idp.example.com/sso"
    assert cfg.enforce_sso is False and cfg.enabled is False  # no cert -> not ready; cannot be enforced
    page = client.get("/app/admin/sso").text
    assert f"/sso/{ws.slug}/acs" in page and f"/sso/{ws.slug}/login" in page
    meta = client.get(f"/sso/{ws.slug}/metadata")
    assert meta.status_code == 200 and "AssertionConsumerService" in meta.text
    r = client.post("/app/admin/scim-token")
    assert r.status_code == 200 and "scim_" in r.text
    db.rollback(); db.expire_all()
    cfg = db.scalar(select(SSOConfig).where(SSOConfig.workspace_id == ws.id))
    assert cfg.scim_token_hash and "scim_" not in cfg.scim_token_hash


def _scim_setup(db, ws):
    _enterprise(db, ws)
    db.add(SSOConfig(workspace_id=ws.id, scim_token_hash=hash_secret("scim_good")))
    db.commit()
    return {"Authorization": "Bearer scim_good"}


def test_scim_lifecycle(client, db, host):
    user, ws = host
    h = _scim_setup(db, ws)
    assert client.get("/scim/v2/Users").status_code == 401
    assert client.get("/scim/v2/Users", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/scim/v2/ServiceProviderConfig").json()["patch"]["supported"] is True

    r = client.post("/scim/v2/Users", headers=h, json={
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"], "userName": "Bob@Example.com",
        "name": {"givenName": "Bob", "familyName": "Smith"}, "active": True})
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    assert r.json()["userName"] == "bob@example.com" and r.json()["active"] is True
    assert client.post("/scim/v2/Users", headers=h, json={"userName": "bob@example.com"}).status_code == 409

    r = client.get("/scim/v2/Users", headers=h, params={"filter": 'userName eq "bob@example.com"'})
    assert r.json()["totalResults"] == 1 and r.json()["Resources"][0]["id"] == uid
    r = client.get("/scim/v2/Users", headers=h, params={"filter": 'userName eq "nobody@example.com"'})
    assert r.json()["totalResults"] == 0
    assert client.get(f"/scim/v2/Users/{uid}", headers=h).json()["name"]["formatted"] == "Bob Smith"

    r = client.patch(f"/scim/v2/Users/{uid}", headers=h, json={
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
        "Operations": [{"op": "replace", "path": "active", "value": False}]})
    assert r.status_code == 200 and r.json()["active"] is False
    db.rollback(); db.expire_all()
    m = db.scalar(select(WorkspaceMember).where(WorkspaceMember.user_id == uid, WorkspaceMember.workspace_id == ws.id))
    assert m.status == "deactivated"
    # Okta-style patch without path
    r = client.patch(f"/scim/v2/Users/{uid}", headers=h, json={"Operations": [{"op": "Replace", "value": {"active": True}}]})
    assert r.json()["active"] is True
    # owner protected
    assert client.patch(f"/scim/v2/Users/{user.id}", headers=h,
                        json={"Operations": [{"op": "replace", "path": "active", "value": False}]}).status_code == 400

    assert client.delete(f"/scim/v2/Users/{uid}", headers=h).status_code == 204
    assert client.get(f"/scim/v2/Users/{uid}", headers=h).status_code == 404
    db.rollback(); db.expire_all()
    assert db.scalar(select(AuditLog).where(AuditLog.action == "scim.user_deprovisioned"))


def test_scim_token_scoped_to_workspace(client, db, host):
    user, ws = host
    h = _scim_setup(db, ws)
    from app.services.bootstrap import create_user_with_workspace

    other, ows = create_user_with_workspace(db, "o@other.com", "Other", plan_id="enterprise")
    db.commit()
    assert client.get(f"/scim/v2/Users/{other.id}", headers=h).status_code == 404
    ids = [r["id"] for r in client.get("/scim/v2/Users", headers=h).json()["Resources"]]
    assert ids == [user.id]


def _booking(db, ws, user, email, start=None):
    et = db.scalar(select(EventType).where(EventType.workspace_id == ws.id))
    if not et:
        et = EventType(workspace_id=ws.id, owner_user_id=user.id, name="Intro", slug="intro", duration=30)
        db.add(et)
        db.flush()
    start = start or (utcnow() + dt.timedelta(days=2))
    b = Booking(workspace_id=ws.id, event_type_id=et.id, host_id=user.id, start_at=start,
                end_at=start + dt.timedelta(minutes=30), status="confirmed", manage_token=hash_secret(str(start) + email))
    db.add(b)
    db.flush()
    db.add(Invitee(booking_id=b.id, name="Carol", email=email, phone="+44123", company="Acme"))
    db.add(BookingAnswer(booking_id=b.id, label="Notes", answer_json="secret plans"))
    return b


def test_data_deletion(client, db, host, login):
    user, ws = host
    b = _booking(db, ws, user, "Carol@Example.com")
    keep = _booking(db, ws, user, "dave@example.com", start=utcnow() + dt.timedelta(days=3))
    db.add(Contact(workspace_id=ws.id, email_normalized="carol@example.com", name="Carol", phone="1"))
    db.commit()
    login(client, user)
    assert client.post("/app/admin/data-deletion", data={"email": "carol@example.com", "confirm": "carol@example.com"}).status_code == 403
    _enterprise(db, ws)
    client.post("/app/admin/data-deletion", data={"email": "carol@example.com", "confirm": "nope@example.com"})
    db.rollback(); db.expire_all()
    assert db.scalar(select(Invitee).where(Invitee.booking_id == b.id)).name == "Carol"
    client.post("/app/admin/data-deletion", data={"email": "carol@example.com", "confirm": "CAROL@example.com"})
    db.rollback(); db.expire_all()
    inv = db.scalar(select(Invitee).where(Invitee.booking_id == b.id))
    assert inv.name == "Deleted" and inv.email.endswith("@redacted.invalid") and inv.phone is None and inv.company is None
    assert db.scalar(select(BookingAnswer).where(BookingAnswer.booking_id == b.id)).answer_json == "[deleted]"
    assert db.scalar(select(Invitee).where(Invitee.booking_id == keep.id)).name == "Carol"  # different email untouched
    c = db.scalar(select(Contact).where(Contact.workspace_id == ws.id))
    assert c.deleted_at is not None and c.name == "Deleted" and c.email_normalized != "carol@example.com"
    log = db.scalar(select(AuditLog).where(AuditLog.action == "data.invitee_deleted"))
    assert log and "carol" not in str(log.metadata_json).lower() and log.metadata_json["invitees"] == 1
    r = client.post("/app/api/admin/data-deletion", json={"email": "dave@example.com"})
    assert r.status_code == 200 and r.json()["deleted"]["invitees"] == 1


def test_health_and_retry(client, db, host, login):
    user, ws = host
    ev = OutboxEvent(topic="booking.created", payload_json={}, workspace_id=ws.id, attempts=8, last_error="boom")
    other_ev = OutboxEvent(topic="booking.created", payload_json={}, workspace_id="ws_other", attempts=8, last_error="x")
    job = NotificationJob(workspace_id=ws.id, trigger="invitee_created", recipient="a@b.com", scheduled_at=utcnow(),
                          status="failed", attempts=5, error="smtp down")
    db.add_all([ev, other_ev, job])
    db.commit()
    login(client, user)
    r = client.get("/app/admin/health")
    assert r.status_code == 200 and "boom" in r.text and "smtp down" in r.text
    client.post(f"/app/admin/health/outbox/{ev.id}/retry")
    client.post(f"/app/admin/health/notification/{job.id}/retry")
    client.post(f"/app/admin/health/outbox/{other_ev.id}/retry")  # other tenant: ignored
    db.rollback(); db.expire_all()
    assert db.get(OutboxEvent, ev.id).attempts == 0 and db.get(OutboxEvent, ev.id).last_error is None
    assert db.get(NotificationJob, job.id).status == "scheduled"
    assert db.get(OutboxEvent, other_ev.id).attempts == 8
    # platform operator (non-owner) can see platform view
    op = add_user_to_workspace(db, ws, "ops@example.com", "Ops", role="member")
    db.get(User, op.id).is_platform_admin = True
    db.commit()
    login(client, op)
    r = client.get("/app/admin/health?scope=platform")
    assert r.status_code == 200 and "Platform-wide" in r.text
    client.post(f"/app/admin/health/outbox/{other_ev.id}/retry?scope=platform")
    db.rollback(); db.expire_all()
    assert db.get(OutboxEvent, other_ev.id).attempts == 0
