import re

from sqlalchemy import select

from app.models import AuditLog, SentMessage, User, WorkspaceMember
from app.services.bootstrap import create_user_with_workspace
from app.services.hostutil import is_host_public


def _last_link(db, email, path):
    db.rollback(); db.expire_all()
    msg = db.scalars(select(SentMessage).where(SentMessage.recipient == email).order_by(SentMessage.created_at.desc())).first()
    assert msg is not None
    m = re.search(rf"https?://[^\"'<\s]+{re.escape(path)}\?token=[^\"'<\s]+", msg.body)
    assert m, msg.body
    return m.group(0).split(path, 1)[1].replace("&amp;", "&")


def test_signup_verify_login_logout(client, db):
    r = client.post("/signup", data={"name": "Bob Builder", "email": "Bob@Example.com", "password": "supersecret",
                                     "timezone": "America/New_York"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/onboarding"
    user = db.scalar(select(User).where(User.email == "bob@example.com"))
    assert user and user.default_timezone == "America/New_York" and user.email_verified_at is None
    assert not is_host_public(user)
    assert db.scalar(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)).role == "owner"
    # logged in -> onboarding renders
    assert client.get("/onboarding").status_code == 200
    # verify
    qs = _last_link(db, "bob@example.com", "/verify-email")
    r = client.get("/verify-email" + qs, follow_redirects=False)
    assert r.status_code == 303
    db.rollback(); db.expire_all()
    assert db.get(User, user.id).email_verified_at is not None
    assert is_host_public(db.get(User, user.id))
    # logout
    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/app", follow_redirects=False).headers["location"].startswith("/login")
    # bad login -> generic error
    r = client.post("/login", data={"email": "bob@example.com", "password": "wrong"})
    assert r.status_code == 400 and "Invalid email or password" in r.text
    r = client.post("/login", data={"email": "nobody@example.com", "password": "wrong"})
    assert r.status_code == 400 and "Invalid email or password" in r.text
    # good login (onboarding not completed yet)
    r = client.post("/login", data={"email": "bob@example.com", "password": "supersecret"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/onboarding"
    db.rollback()
    assert db.scalar(select(AuditLog).where(AuditLog.action == "user.login", AuditLog.actor_id == user.id))


def test_signup_validation_and_duplicate(client, host):
    r = client.post("/signup", data={"name": "X", "email": "alice@example.com", "password": "password123"})
    assert r.status_code == 400 and "already exists" in r.text
    r = client.post("/signup", data={"name": "X", "email": "x@example.com", "password": "short"})
    assert r.status_code == 400


def test_invalid_verify_token(client):
    r = client.get("/verify-email?token=garbage")
    assert r.status_code == 400


def test_password_reset(client, db, host):
    user, _ = host
    r = client.post("/forgot-password", data={"email": "alice@example.com"})
    assert r.status_code == 200 and "If an account exists" in r.text
    # unknown email -> same response, no mail
    r = client.post("/forgot-password", data={"email": "ghost@example.com"})
    assert r.status_code == 200 and "If an account exists" in r.text
    qs = _last_link(db, "alice@example.com", "/reset-password")
    assert client.get("/reset-password" + qs).status_code == 200
    token = qs.split("token=", 1)[1]
    r = client.post("/reset-password", data={"token": token, "password": "newpassword1", "password_confirm": "nope"})
    assert r.status_code == 400
    r = client.post("/reset-password", data={"token": token, "password": "newpassword1",
                                             "password_confirm": "newpassword1"}, follow_redirects=False)
    assert r.status_code == 303
    # token is single-use
    assert client.get("/reset-password" + qs).status_code == 400
    r = client.post("/login", data={"email": "alice@example.com", "password": "newpassword1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app"
    r2 = client.post("/login", data={"email": "alice@example.com", "password": "password123"})
    assert r2.status_code == 400


def test_onboarding_flow_and_slug_rules(client, db, login):
    other, _ = create_user_with_workspace(db, "taken@example.com", "Taken", password="password123", slug="taken")
    user, _ = create_user_with_workspace(db, "new@example.com", "New Person", password="password123")
    db.commit()
    login(client, user)
    # collision rejected
    client.post("/onboarding", data={"step": "profile", "slug": "taken", "timezone": "Europe/Paris"})
    db.rollback(); db.expire_all()
    assert db.get(User, user.id).slug != "taken"
    # reserved rejected
    client.post("/onboarding", data={"step": "profile", "slug": "login", "timezone": "Europe/Paris"})
    db.rollback(); db.expire_all()
    assert db.get(User, user.id).slug != "login"
    r = client.post("/onboarding", data={"step": "profile", "slug": "newbie", "timezone": "Europe/Paris",
                                         "headline": "Hi"}, follow_redirects=False)
    assert r.headers["location"] == "/onboarding?step=calendar"
    assert client.get("/onboarding?step=calendar").status_code == 200
    r = client.post("/onboarding", data={"step": "availability", "days": ["0", "2"], "start": "10:00", "end": "16:00"},
                    follow_redirects=False)
    assert r.headers["location"] == "/onboarding?step=event"
    r = client.post("/onboarding", data={"step": "event", "name": "Intro call", "duration": "30",
                                         "location": "google_meet"}, follow_redirects=False)
    assert r.headers["location"] == "/onboarding?step=done"
    page = client.get("/onboarding?step=done")
    assert "/newbie/intro-call" in page.text
    client.post("/onboarding", data={"step": "done"})
    db.rollback(); db.expire_all()
    u = db.get(User, user.id)
    assert u.slug == "newbie" and u.default_timezone == "Europe/Paris" and u.onboarding_completed


def test_invitation_acceptance(client, db, host):
    _, ws = host
    m = WorkspaceMember(workspace_id=ws.id, role="member", status="invited", invite_email="carol@example.com",
                        invite_token="tok-123")
    db.add(m)
    db.commit()
    assert client.get("/invite/tok-123").status_code == 200
    assert client.get("/invite/nope").status_code == 404
    r = client.post("/invite/tok-123", data={"name": "Carol", "password": "password123", "timezone": "UTC"},
                    follow_redirects=False)
    assert r.status_code == 303
    db.rollback(); db.expire_all()
    m = db.get(WorkspaceMember, m.id)
    carol = db.scalar(select(User).where(User.email == "carol@example.com"))
    assert m.status == "active" and m.user_id == carol.id and m.invite_token is None
    assert carol.email_verified_at is not None


def test_google_button_hidden_without_config(client):
    r = client.get("/login")
    assert "Continue with Google" not in r.text
    r = client.get("/auth/google/start", follow_redirects=False)
    assert r.headers["location"] == "/login"
