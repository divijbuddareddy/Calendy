"""Platform / release-gate features: legal pages, session versioning, i18n, PWA + mobile quick
actions, extension API, Postgres URL normalisation, retention jobs, SQLite backup."""
import datetime as dt
import secrets

from sqlalchemy import select

from app.models import Booking, DataRequest, EventType, Invitee, User, utcnow
from app.services.bootstrap import create_user_with_workspace


def fresh(db):
    db.commit()
    db.expire_all()


# ------------------------------------------------------------------ release gate: legal pages
def test_legal_pages_published_and_linked(client):
    from app.config import settings

    for path, needle in [("/privacy", "Privacy notice"), ("/terms", "Terms of service"),
                         ("/data-retention", "Data retention policy")]:
        r = client.get(path)
        assert r.status_code == 200 and needle in r.text, path
    assert settings.privacy_version in client.get("/privacy").text
    assert client.get("/legal/versions.json").json()["privacy"] == settings.privacy_version
    landing = client.get("/").text
    assert 'href="/privacy"' in landing and 'href="/data-retention"' in landing
    assert 'href="/privacy"' in client.get("/login").text


def test_app_shell_links_legal_and_pwa(client, host, login):
    login(client, host[0])
    html = client.get("/app").text
    assert 'href="/privacy"' in html and 'rel="manifest"' in html and 'class="bottom-nav"' in html


# ------------------------------------------------------------------ sessions
def test_password_change_signs_out_other_sessions(db, host):
    from fastapi.testclient import TestClient

    from app.main import app

    user, _ = host
    with TestClient(app) as a, TestClient(app) as b:
        for c in (a, b):
            assert c.post("/login", data={"email": user.email, "password": "password123"}).status_code in (200, 303)
            assert c.get("/app", follow_redirects=False).status_code == 200
        r = a.post("/app/settings/security/password", data={"current_password": "password123",
                                                            "new_password": "brandnew123",
                                                            "confirm_password": "brandnew123"})
        assert r.status_code == 200
        assert a.get("/app", follow_redirects=False).status_code == 200  # current session survives
        r = b.get("/app", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")  # other session revoked
        # explicit "sign out other sessions"
        assert b.post("/login", data={"email": user.email, "password": "brandnew123"}).status_code in (200, 303)
        assert b.get("/app", follow_redirects=False).status_code == 200
        a.post("/app/settings/security/sign-out-others")
        assert a.get("/app", follow_redirects=False).status_code == 200
        assert b.get("/app", follow_redirects=False).status_code == 303
    fresh(db)
    assert db.get(User, user.id).session_epoch == 2


def test_session_idle_and_absolute_expiry(monkeypatch, client, host, login):
    from app import deps
    from app.config import settings

    login(client, host[0])
    assert client.get("/app", follow_redirects=False).status_code == 200
    real = deps._now_ts
    monkeypatch.setattr(deps, "_now_ts", lambda: real() + settings.session_idle_minutes * 60 + 120)
    assert client.get("/app", follow_redirects=False).status_code == 303
    monkeypatch.setattr(deps, "_now_ts", real)
    login(client, host[0])
    # absolute lifetime: keep activity fresh but exceed max age
    state = {"t": real()}
    monkeypatch.setattr(deps, "_now_ts", lambda: state["t"])
    login(client, host[0])
    step = settings.session_idle_minutes * 60 - 60
    while state["t"] - real() < settings.session_max_age_minutes * 60 - step:
        state["t"] += step
        assert client.get("/app", follow_redirects=False).status_code == 200
    state["t"] += step
    assert client.get("/app", follow_redirects=False).status_code == 303


def test_reset_password_bumps_epoch(db, host, client):
    from app.routers.auth import reset_token

    user, _ = host
    tok = reset_token(user)
    client.post("/reset-password", data={"token": tok, "password": "another123", "password_confirm": "another123"})
    fresh(db)
    assert db.get(User, user.id).session_epoch == 1


def test_sso_hook_redirects_before_password(monkeypatch, client, host):
    import sys
    import types

    mod = types.ModuleType("app.services.sso")
    mod.sso_required_for_email = lambda db, email: "/sso/start?ws=x" if email.endswith("@example.com") else None
    monkeypatch.setitem(sys.modules, "app.services.sso", mod)
    r = client.post("/login", data={"email": host[0].email, "password": "wrong"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/sso/start")


# ------------------------------------------------------------------ timezones on host screens
def test_one_off_times_shown_in_host_timezone(client, db, host, login):
    user, ws = host
    user = db.get(User, user.id)
    user.default_timezone = "Asia/Kolkata"
    db.commit()
    login(client, user)
    day = (dt.date.today() + dt.timedelta(days=5)).isoformat()
    r = client.post("/app/event-types/one-off", data={"name": "Kickoff", "duration": "30", "location": "zoom",
                                                      "slots": f"{day}T10:00"})
    assert r.status_code == 200
    html = client.get("/app/event-types").text
    assert "10:00 IST" in html and " UTC</div>" not in html


def test_dt_filter_accepts_iso_strings():
    from app.deps import _fmt_dt

    assert _fmt_dt("2026-07-01T12:00:00", "Europe/London", "%H:%M") == "13:00 BST"
    assert _fmt_dt("2026-07-01T12:00:00Z", "America/New_York", "%H:%M") == "08:00 EDT"


# ------------------------------------------------------------------ i18n
def test_i18n_catalog_and_detection():
    from starlette.requests import Request

    from app.i18n import CATALOG, SUPPORTED_LOCALES, catalog, get_locale, parse_accept_language, t

    assert set(SUPPORTED_LOCALES) == {"en", "es", "fr", "de", "hi"}
    for loc in SUPPORTED_LOCALES:
        missing = set(CATALOG["en"]) - set(CATALOG[loc])
        assert not missing, (loc, missing)
    assert t("book.schedule_event", "es") == "Programar evento"
    assert t("book.spots_left", "fr", n=3) == "3 places restantes"
    assert t("book.schedule_event", "pt-BR") == "Schedule event"  # unsupported -> English
    assert t("no.such.key", "de") == "no.such.key"
    assert catalog("hi")["confirm.scheduled"].startswith("आपकी")
    assert parse_accept_language("pt-BR,de;q=0.8,en;q=0.5") == "de"

    def req(qs="", headers=None, cookie=None):
        hdrs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
        if cookie:
            hdrs.append((b"cookie", cookie.encode()))
        return Request({"type": "http", "query_string": qs.encode(), "headers": hdrs, "path": "/"})

    assert get_locale(req("lang=fr", {"Accept-Language": "de"}), "es") == "fr"
    assert get_locale(req(cookie="lang=hi"), "es") == "hi"
    assert get_locale(req(headers={"Accept-Language": "de-DE"}), "es") == "es"  # host's language wins
    assert get_locale(req(headers={"Accept-Language": "de-DE"}), "en") == "de"
    assert get_locale(req(), None) == "en"


def test_i18n_jinja_global():
    from app.deps import templates

    tpl = templates.env.from_string("{{ t('book.schedule_event') }}|{{ t('book.spots_left', n=2) }}|"
                                    "{{ t('confirm.scheduled', 'de') }}")
    assert tpl.render(locale="es") == "Programar evento|Quedan 2 plazas|Ihr Termin ist gebucht"
    assert tpl.render().startswith("Schedule event|")


# ------------------------------------------------------------------ PWA + mobile
def test_pwa_assets(client):
    m = client.get("/manifest.webmanifest")
    assert m.status_code == 200 and m.json()["display"] == "standalone"
    sizes = {i["sizes"] for i in m.json()["icons"]}
    assert {"192x192", "512x512"} <= sizes
    png = client.get("/icons/icon-192.png")
    assert png.status_code == 200 and png.content[:8] == b"\x89PNG\r\n\x1a\n" and png.headers["content-type"] == "image/png"
    assert client.get("/icons/icon-512.png?maskable=1").status_code == 200
    assert client.get("/icons/icon-7.png").status_code == 404
    sw = client.get("/sw.js")
    assert sw.status_code == 200 and "offline" in sw.text and sw.headers.get("service-worker-allowed") == "/"
    assert client.get("/offline").status_code == 200


def _booking(db, user, ws, et, hours=48):
    start = (utcnow() + dt.timedelta(hours=hours)).replace(minute=0, second=0, microsecond=0)
    b = Booking(workspace_id=ws.id, event_type_id=et.id, host_id=user.id, start_at=start,
                end_at=start + dt.timedelta(minutes=30), status="confirmed", manage_token=secrets.token_urlsafe(16),
                timezone="UTC")
    b.invitees.append(Invitee(name="Grace Guest", email="grace@example.com"))
    db.add(b)
    db.commit()
    return b


def test_mobile_quick_actions(client, db, host, login):
    user, ws = host
    et = EventType(workspace_id=ws.id, owner_user_id=user.id, name="Coffee chat", slug="coffee", duration=30)
    db.add(et)
    db.commit()
    b = _booking(db, user, ws, et)
    login(client, user)
    html = client.get("/app/m").text
    assert "Coffee chat" in html and "Grace Guest" in html and "/alice/coffee" in html
    assert ("GMT" in html or "BST" in html)  # time carries its zone label
    day = (dt.date.today() + dt.timedelta(days=3)).isoformat()
    r = client.post("/app/m/one-off", data={"name": "Quick sync", "duration": "20", "slots": f"{day}T09:30"})
    assert r.status_code == 200 and "Your new link" in r.text
    fresh(db)
    oo = db.scalar(select(EventType).where(EventType.name == "Quick sync"))
    assert oo.is_one_off and oo.visibility == "secret" and len(oo.one_off_slots_json) == 1
    r = client.post(f"/app/m/bookings/{b.id}/cancel", data={"reason": "Sick"})
    assert r.status_code == 200
    fresh(db)
    assert db.get(Booking, b.id).status == "cancelled"


# ------------------------------------------------------------------ browser-extension API
def test_extension_api(client, db, host):
    from app.services import apikeys

    user, ws = host
    et = EventType(workspace_id=ws.id, owner_user_id=user.id, name="Intro", slug="intro", duration=30)
    db.add(et)
    db.commit()
    _booking(db, user, ws, et, hours=30)
    _booking(db, user, ws, et, hours=6)
    _, raw = apikeys.create_key(db, ws.id, user.id, "ext", ["read", "write"])
    _, ro = apikeys.create_key(db, ws.id, user.id, "ro", ["read"])
    db.commit()
    h = {"Authorization": f"Bearer {raw}"}
    assert client.get("/api/v1/ext/event-types").status_code == 401
    r = client.get("/api/v1/ext/event-types", headers=h)
    assert r.status_code == 200 and r.json()["data"][0]["url"].endswith("/alice/intro")
    up = client.get("/api/v1/ext/upcoming", headers=h).json()
    assert len(up["data"]) == 2 and up["data"][0]["start_at"] < up["data"][1]["start_at"]
    assert up["data"][0]["invitee"]["name"] == "Grace Guest" and up["timezone"] == "Europe/London"
    r = client.post(f"/api/v1/ext/event-types/{et.id}/single-use-link", headers=h)
    assert r.status_code == 201 and "/alice/su-" in r.json()["url"]
    # single-use clones don't show up as shareable types
    assert len(client.get("/api/v1/ext/event-types", headers=h).json()["data"]) == 1
    assert client.post(f"/api/v1/ext/event-types/{et.id}/single-use-link",
                       headers={"Authorization": f"Bearer {ro}"}).status_code == 403
    # other workspace's key can't touch this event type
    u2, w2 = create_user_with_workspace(db, "eve@example.com", "Eve", password="password123", slug="eve")
    _, k2 = apikeys.create_key(db, w2.id, u2.id, "x", ["read", "write"])
    db.commit()
    h2 = {"Authorization": f"Bearer {k2}"}
    assert client.post(f"/api/v1/ext/event-types/{et.id}/single-use-link", headers=h2).status_code == 404
    assert client.get("/api/v1/ext/upcoming", headers=h2).json()["data"] == []


# ------------------------------------------------------------------ deployment helpers
def test_postgres_url_normalisation():
    from app.config import normalize_db_url

    assert normalize_db_url("postgres://u:p@h:5432/db") == "postgresql+psycopg2://u:p@h:5432/db"
    assert normalize_db_url("postgresql://u@h/db") == "postgresql+psycopg2://u@h/db"
    assert normalize_db_url("postgresql+psycopg2://u@h/db") == "postgresql+psycopg2://u@h/db"
    assert normalize_db_url("sqlite:///./x.db") == "sqlite:///./x.db"


def test_additive_column_sync():
    from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect

    from app import db as dbmod

    eng = create_engine("sqlite://")
    md = MetaData()
    Table("users", md, Column("id", String(40), primary_key=True), Column("email", String(320)))
    md.create_all(eng)
    added = dbmod._add_missing_columns(eng)
    assert "users.session_epoch" in added
    assert "session_epoch" in {c["name"] for c in inspect(eng).get_columns("users")}
    assert dbmod._add_missing_columns(eng) == []  # idempotent


def test_seed_prod_is_idempotent(db):
    import seed_prod

    first = seed_prod.run("owner@example.org", "Owner", "s3cret-pass!", workspace_name="Acme")
    second = seed_prod.run("owner@example.org", "Owner", "s3cret-pass!", workspace_name="Acme")
    assert first["created"] is True and second["created"] is False
    fresh(db)
    assert len(db.scalars(select(User).where(User.email == "owner@example.org")).all()) == 1


def test_retention_jobs(db, host):
    from app.services import retention

    user, ws = host
    u2, _ = create_user_with_workspace(db, "gone@example.com", "Gone", password="password123", slug="gone")
    u2.deleted_at = utcnow() - dt.timedelta(days=retention.ACCOUNT_PURGE_DAYS + 1)
    db.add(DataRequest(user_id=user.id, workspace_id=ws.id, kind="export", status="completed",
                       result_json={"x": 1}, created_at=utcnow() - dt.timedelta(days=30)))
    db.commit()
    out = retention.run_retention(db)
    db.commit()
    assert out["accounts_purged"] == 1 and out["exports_expired"] == 1
    fresh(db)
    g = db.get(User, u2.id)
    assert g.email.endswith("@deleted.invalid") and g.name == "Deleted user" and g.password_hash is None
    assert retention.run_retention(db)["accounts_purged"] == 0


def test_sqlite_backup_download(client, db, host, login):
    from app.routers import ops

    login(client, host[0])
    if not ops.backup_available():
        assert client.get("/app/ops/backup.sqlite").status_code == 404
        return
    r = client.get("/app/ops/backup.sqlite")
    assert r.status_code == 200 and r.content[:16] == b"SQLite format 3\x00"


def test_login_method_policy_blocks_password(monkeypatch, client, host):
    import app.services.sso as sso

    monkeypatch.setattr(sso, "sso_required_for_email", lambda db, email: None)
    monkeypatch.setattr(sso, "login_method_allowed", lambda db, email, method: method != "password")
    r = client.post("/login", data={"email": host[0].email, "password": "password123"}, follow_redirects=False)
    assert r.status_code == 403 and "doesn" in r.text
    monkeypatch.setattr(sso, "login_method_allowed", lambda db, email, method: True)
    r = client.post("/login", data={"email": host[0].email, "password": "password123"}, follow_redirects=False)
    assert r.status_code == 303
