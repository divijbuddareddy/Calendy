from sqlalchemy import select

from app.models import AuditLog, EventQuestion, EventType, SentMessage, Team, TeamMember, User, WorkspaceMember
from app.services.bootstrap import add_user_to_workspace


def _fresh(db):
    db.rollback()
    db.expire_all()


def _member(db, ws, email):
    u = db.scalar(select(User).where(User.email == email))
    return db.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == ws.id, WorkspaceMember.user_id == u.id))


def test_members_page_and_invite_flow(client, db, host, login):
    user, ws = host
    login(client, user)
    r = client.get("/app/teams")
    assert r.status_code == 200 and "alice@example.com" in r.text
    r = client.post("/app/teams/invite", data={"email": "Bob@Example.com", "role": "member"}, follow_redirects=False)
    assert r.status_code == 303
    _fresh(db)
    inv = db.scalar(select(WorkspaceMember).where(WorkspaceMember.invite_email == "bob@example.com"))
    assert inv.status == "invited" and inv.invite_token and inv.user_id is None
    msg = db.scalar(select(SentMessage).where(SentMessage.recipient == "bob@example.com"))
    assert f"/invite/{inv.invite_token}" in msg.body
    # duplicate invite is rejected
    client.post("/app/teams/invite", data={"email": "bob@example.com", "role": "member"})
    _fresh(db)
    assert len(db.scalars(select(WorkspaceMember).where(WorkspaceMember.invite_email == "bob@example.com")).all()) == 1
    # resend rotates the token
    old = inv.invite_token
    client.post(f"/app/teams/members/{inv.id}/resend")
    _fresh(db)
    assert db.get(WorkspaceMember, inv.id).invite_token != old
    # revoke deletes the invitation
    inv_id = inv.id
    client.post(f"/app/teams/members/{inv_id}/revoke")
    _fresh(db)
    assert db.get(WorkspaceMember, inv_id) is None
    assert db.scalar(select(AuditLog).where(AuditLog.action == "member.invited"))


def test_invite_domain_restriction(client, db, host, login):
    user, ws = host
    ws.settings_json = {**(ws.settings_json or {}), "approved_domains": ["acme.com"]}
    db.commit()
    login(client, user)
    client.post("/app/teams/invite", data={"email": "eve@evil.com, ok@acme.com", "role": "member"})
    _fresh(db)
    emails = set(db.scalars(select(WorkspaceMember.invite_email).where(WorkspaceMember.workspace_id == ws.id)))
    assert "ok@acme.com" in emails and "eve@evil.com" not in emails


def test_role_matrix(client, db, host, login):
    user, ws = host
    admin = add_user_to_workspace(db, ws, "adm@example.com", "Adm", role="admin")
    mem = add_user_to_workspace(db, ws, "mem@example.com", "Mem", role="member")
    viewer = add_user_to_workspace(db, ws, "view@example.com", "View", role="viewer")
    db.commit()
    mem_m, view_m, adm_m = _member(db, ws, "mem@example.com"), _member(db, ws, "view@example.com"), _member(db, ws, "adm@example.com")

    # member cannot change roles or invite
    login(client, mem)
    assert client.post(f"/app/teams/members/{view_m.id}/role", data={"role": "admin"}).status_code == 403
    assert client.post("/app/teams/invite", data={"email": "x@example.com"}).status_code == 403
    # viewer is read-only
    login(client, viewer)
    assert client.get("/app/teams").status_code == 200
    assert client.post(f"/app/teams/members/{mem_m.id}/deactivate").status_code == 403
    assert client.post("/app/contacts", data={"email": "z@z.com"}).status_code == 403
    # admin can change member->viewer but cannot promote to admin
    login(client, admin)
    client.post(f"/app/teams/members/{mem_m.id}/role", data={"role": "viewer"})
    client.post(f"/app/teams/members/{view_m.id}/role", data={"role": "admin"})
    _fresh(db)
    assert db.get(WorkspaceMember, mem_m.id).role == "viewer"
    assert db.get(WorkspaceMember, view_m.id).role == "viewer"
    # owner can promote; change is audited
    login(client, user)
    client.post(f"/app/teams/members/{view_m.id}/role", data={"role": "admin"})
    _fresh(db)
    assert db.get(WorkspaceMember, view_m.id).role == "admin"
    log = db.scalar(select(AuditLog).where(AuditLog.action == "member.role_changed", AuditLog.entity_id == view_m.id,
                                           AuditLog.actor_id == user.id))
    assert log and log.metadata_json["to"] == "admin"
    # last owner cannot demote themselves
    owner_m = _member(db, ws, "alice@example.com")
    client.post(f"/app/teams/members/{owner_m.id}/role", data={"role": "member"})
    _fresh(db)
    assert db.get(WorkspaceMember, owner_m.id).role == "owner"
    # deactivate / reactivate / remove
    client.post(f"/app/teams/members/{adm_m.id}/deactivate")
    _fresh(db)
    assert db.get(WorkspaceMember, adm_m.id).status == "deactivated"
    client.post(f"/app/teams/members/{adm_m.id}/reactivate")
    _fresh(db)
    assert db.get(WorkspaceMember, adm_m.id).status == "active"
    adm_id = adm_m.id
    client.post(f"/app/teams/members/{adm_id}/remove")
    _fresh(db)
    assert db.get(WorkspaceMember, adm_id) is None


def test_team_crud_and_members(client, db, host, login):
    user, ws = host
    bob = add_user_to_workspace(db, ws, "bob@example.com", "Bob")
    db.commit()
    login(client, user)
    r = client.post("/app/teams/create", data={"name": "Sales Team", "slug": "sales", "description": "Closers",
                                               "logo_url": "https://example.com/logo.png"}, follow_redirects=False)
    assert r.status_code == 303
    _fresh(db)
    team = db.scalar(select(Team).where(Team.slug == "sales"))
    assert team and team.workspace_id == ws.id
    # duplicate slug rejected
    client.post("/app/teams/create", data={"name": "Other", "slug": "sales"})
    _fresh(db)
    assert len(db.scalars(select(Team).where(Team.slug == "sales")).all()) == 1
    # bad logo url rejected
    client.post(f"/app/teams/{team.id}/edit", data={"name": "Sales", "slug": "sales", "logo_url": "javascript:alert(1)"})
    _fresh(db)
    assert db.get(Team, team.id).logo_url == "https://example.com/logo.png"
    client.post(f"/app/teams/{team.id}/members/add", data={"user_id": bob.id, "role": "manager", "priority": "3"})
    _fresh(db)
    tm = db.scalar(select(TeamMember).where(TeamMember.team_id == team.id, TeamMember.user_id == bob.id))
    assert tm.role == "manager" and tm.priority == 3
    client.post(f"/app/teams/{team.id}/members/{tm.id}/update", data={"role": "member", "priority": "5"})
    _fresh(db)
    tm = db.get(TeamMember, tm.id)
    assert tm.priority == 5 and tm.active is False
    db.add(EventType(workspace_id=ws.id, owner_user_id=user.id, team_id=team.id, name="Demo", slug="demo", kind="round_robin"))
    db.commit()
    r = client.get(f"/app/teams/{team.id}")
    assert r.status_code == 200 and "Demo" in r.text and "/team/sales" in r.text and "team_id=" in r.text
    # cannot delete a team that still has event types
    client.post(f"/app/teams/{team.id}/delete")
    _fresh(db)
    assert db.get(Team, team.id) is not None


def test_team_requires_plan(client, db, host, login):
    user, ws = host
    ws.plan_id = "free"
    db.commit()
    login(client, user)
    assert client.post("/app/teams/create", data={"name": "X"}).status_code == 403


def test_managed_templates_assign_and_sync(client, db, host, login):
    user, ws = host
    bob = add_user_to_workspace(db, ws, "bob@example.com", "Bob")
    db.commit()
    login(client, user)
    r = client.post("/app/admin/managed-events", data={
        "name": "Discovery", "slug": "discovery", "duration": "30", "location": "zoom", "description": "Std",
        "lock_duration": "1", "lock_questions": "1", "lock_description": "1"}, follow_redirects=False)
    assert r.status_code == 303
    _fresh(db)
    t = db.scalar(select(EventType).where(EventType.is_managed_template.is_(True)))
    assert set(t.locked_fields_json) == {"duration", "questions", "description"}
    client.post(f"/app/admin/managed-events/{t.id}/questions", data={"label": "Budget?", "type": "text"})
    client.post(f"/app/admin/managed-events/{t.id}/assign", data={"user_ids": [bob.id, user.id]})
    _fresh(db)
    kids = db.scalars(select(EventType).where(EventType.managed_template_id == t.id)).all()
    assert {k.owner_user_id for k in kids} == {bob.id, user.id}
    assert all(k.slug == "discovery" and k.duration == 30 for k in kids)
    assert all([q.label for q in k.questions] == ["Budget?"] for k in kids)
    # change template + sync -> locked fields pushed
    client.post(f"/app/admin/managed-events/{t.id}", data={
        "name": "Discovery", "duration": "45", "location": "zoom", "description": "New text",
        "lock_duration": "1", "lock_questions": "1", "lock_description": "1"})
    client.post(f"/app/admin/managed-events/{t.id}/questions", data={"label": "Team size?", "type": "number"})
    client.post(f"/app/admin/managed-events/{t.id}/sync")
    _fresh(db)
    for k in db.scalars(select(EventType).where(EventType.managed_template_id == t.id)):
        assert k.duration == 45 and k.description == "New text"
        assert sorted(q.label for q in db.scalars(select(EventQuestion).where(EventQuestion.event_type_id == k.id))) == ["Budget?", "Team size?"]
    # members can't reach the admin screen
    login(client, bob)
    assert client.get("/app/admin/managed-events").status_code == 403


def test_admin_pages_render(client, db, host, login):
    user, ws = host
    login(client, user)
    assert client.get("/app/admin/managed-events").status_code == 200
    client.post("/app/admin/managed-events", data={"name": "Onboarding", "duration": "60", "location": "phone"})
    _fresh(db)
    t = db.scalar(select(EventType).where(EventType.is_managed_template.is_(True)))
    r = client.get(f"/app/admin/managed-events/{t.id}")
    assert r.status_code == 200 and "Assign to members" in r.text and "Onboarding" in r.text
    r = client.get("/app/teams?tab=teams")
    assert r.status_code == 200 and "Create a team" in r.text
