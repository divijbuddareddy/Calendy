"""Public REST API: API keys, scopes, tenant isolation, cursor pagination, idempotency."""
import datetime as dt
import secrets

import pytest
from sqlalchemy import select

from app.models import ApiKey, Booking, Contact, EventType, Invitee, WebhookSubscription, utcnow
from app.services import apikeys
from app.services.bootstrap import create_user_with_workspace


def key_for(db, user, ws, scopes=("read", "write")):
    db.commit()
    row, raw = apikeys.create_key(db, ws.id, user.id, "test", list(scopes))
    db.commit()
    return {"Authorization": f"Bearer {raw}"}, row


def make_et(db, user, ws, name="Intro"):
    et = EventType(workspace_id=ws.id, owner_user_id=user.id, name=name, slug=name.lower() + secrets.token_hex(2), duration=30)
    db.add(et)
    db.commit()
    return et


def make_booking(db, user, ws, et, i=0, created=None):
    start = (utcnow() + dt.timedelta(days=3, hours=i)).replace(minute=0, second=0, microsecond=0)
    b = Booking(workspace_id=ws.id, event_type_id=et.id, host_id=user.id, start_at=start,
                end_at=start + dt.timedelta(minutes=30), status="confirmed", manage_token=secrets.token_urlsafe(16),
                timezone="UTC")
    if created:
        b.created_at = created
    b.invitees.append(Invitee(name=f"Guest {i}", email=f"g{i}@example.com"))
    db.add(b)
    db.commit()
    return b


@pytest.fixture
def other(db):
    u, w = create_user_with_workspace(db, "mallory@example.com", "Mallory", password="password123", slug="mallory")
    db.commit()
    return u, w


def test_auth_and_revocation(db, host, client):
    user, ws = host
    assert client.get("/api/v1/me").status_code == 401
    r = client.get("/api/v1/me", headers={"Authorization": "Bearer aal_sk_nope"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHORIZED" and "request_id" in r.json()["error"]
    h, row = key_for(db, user, ws)
    assert row.key_hash != h["Authorization"][7:] and len(row.prefix) < 20  # only a hash is stored
    r = client.get("/api/v1/me", headers=h)
    assert r.status_code == 200 and r.json()["workspace"]["id"] == ws.id and r.json()["user"]["email"] == user.email
    db.commit(); db.expire_all()
    assert db.get(ApiKey, row.id).last_used_at is not None
    apikeys.revoke_key(db, db.get(ApiKey, row.id))
    db.commit()
    r = client.get("/api/v1/me", headers=h)
    assert r.status_code == 401 and "revoked" in r.json()["error"]["message"]


def test_read_only_scope(db, host, client):
    user, ws = host
    h, _ = key_for(db, user, ws, scopes=("read",))
    assert client.get("/api/v1/event-types", headers=h).status_code == 200
    r = client.post("/api/v1/event-types", headers=h, json={"name": "Nope"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN"


def test_cross_tenant_access_denied(db, host, other, client):
    user, ws = host
    ouser, ows = other
    oet = make_et(db, ouser, ows, "Secret")
    ob = make_booking(db, ouser, ows, oet)
    db.add(Contact(workspace_id=ows.id, email_normalized="hidden@example.com", name="Hidden"))
    db.commit()
    h, _ = key_for(db, user, ws)
    assert client.get(f"/api/v1/event-types/{oet.id}", headers=h).status_code == 404
    assert client.patch(f"/api/v1/event-types/{oet.id}", headers=h, json={"name": "pwned"}).status_code == 404
    assert client.get(f"/api/v1/bookings/{ob.id}", headers=h).status_code == 404
    assert client.post(f"/api/v1/bookings/{ob.id}/cancel", headers=h, json={}).status_code == 404
    # smuggling another workspace id in params/body never widens scope
    r = client.get(f"/api/v1/bookings?workspace_id={ows.id}", headers=h)
    assert r.status_code == 200 and r.json()["data"] == []
    assert client.get(f"/api/v1/contacts?workspace_id={ows.id}", headers=h).json()["data"] == []
    r = client.post("/api/v1/event-types", headers=h, json={"name": "Mine", "workspace_id": ows.id})
    assert r.status_code == 201
    db.commit()
    assert db.get(EventType, r.json()["id"]).workspace_id == ws.id
    r = client.get(f"/api/v1/availability?event_type_id={oet.id}&from=2026-11-20&to=2026-11-21", headers=h)
    assert r.status_code == 404


def test_event_types_crud(db, host, client):
    user, ws = host
    h, _ = key_for(db, user, ws)
    r = client.post("/api/v1/event-types", headers=h, json={"name": "Strategy Session", "duration": 45,
                                                            "locations": [{"type": "zoom"}], "price_minor": 5000,
                                                            "currency": "usd"})
    assert r.status_code == 201, r.text
    et = r.json()
    assert et["slug"] == "strategy-session" and et["duration"] == 45 and et["currency"] == "USD"
    assert et["booking_url"].endswith("/alice/strategy-session")
    assert client.post("/api/v1/event-types", headers=h, json={"name": "Strategy Session"}).status_code == 409
    r = client.patch(f"/api/v1/event-types/{et['id']}", headers=h, json={"duration": 60, "status": "paused"})
    assert r.status_code == 200 and r.json()["duration"] == 60 and r.json()["status"] == "paused"
    assert client.get(f"/api/v1/event-types/{et['id']}", headers=h).json()["name"] == "Strategy Session"
    lst = client.get("/api/v1/event-types", headers=h).json()
    assert [x["id"] for x in lst["data"]] == [et["id"]] and lst["next_cursor"] is None
    r = client.patch(f"/api/v1/event-types/{et['id']}", headers=h, json={"duration": 1})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_cursor_pagination_stable(db, host, client):
    user, ws = host
    et = make_et(db, user, ws)
    same = utcnow().replace(microsecond=0)
    ids = {make_booking(db, user, ws, et, i, created=same if i < 4 else same + dt.timedelta(seconds=i)).id for i in range(7)}
    h, _ = key_for(db, user, ws)
    seen, cursor, pages = [], None, 0
    while True:
        url = "/api/v1/bookings?limit=3" + (f"&cursor={cursor}" if cursor else "")
        body = client.get(url, headers=h).json()
        seen += [b["id"] for b in body["data"]]
        pages += 1
        cursor = body["next_cursor"]
        if not cursor:
            break
    assert pages == 3 and len(seen) == 7 and set(seen) == ids  # no duplicates / gaps even with equal created_at
    assert client.get("/api/v1/bookings?cursor=%%%", headers=h).status_code == 400
    first = client.get("/api/v1/bookings?limit=1&invitee_email=g6@example.com", headers=h).json()
    assert len(first["data"]) == 1 and first["data"][0]["invitees"][0]["email"] == "g6@example.com"


def test_create_booking_idempotency(db, host, client, monkeypatch):
    booking_mod = pytest.importorskip("app.services.booking")
    user, ws = host
    et = make_et(db, user, ws)
    calls = []

    def fake_create(db_, event_type, start_utc, invitee, answers, *, timezone, duration=None, idempotency_key=None,
                    utm=None, source="api", actor="api", **kw):
        calls.append(idempotency_key)
        b = Booking(workspace_id=event_type.workspace_id, event_type_id=event_type.id, host_id=event_type.owner_user_id,
                    start_at=start_utc, end_at=start_utc + dt.timedelta(minutes=30), status="confirmed",
                    manage_token=secrets.token_urlsafe(16), timezone=timezone, source=source,
                    idempotency_key=idempotency_key)
        b.invitees.append(Invitee(name=invitee["name"], email=invitee["email"]))
        db_.add(b)
        db_.commit()
        return b

    monkeypatch.setattr(booking_mod, "create_booking", fake_create)
    h, _ = key_for(db, user, ws)
    body = {"event_type_id": et.id, "start_at": "2030-01-08T14:00:00Z", "timezone": "Europe/London",
            "invitee": {"name": "Alex", "email": "alex@example.com"}}
    assert client.post("/api/v1/bookings", headers=h, json=body).status_code == 400  # header required
    r1 = client.post("/api/v1/bookings", headers={**h, "Idempotency-Key": "k-1"}, json=body)
    assert r1.status_code == 201, r1.text
    r2 = client.post("/api/v1/bookings", headers={**h, "Idempotency-Key": "k-1"}, json=body)
    assert r2.status_code == 201 and r2.json()["id"] == r1.json()["id"] and r2.headers.get("Idempotent-Replayed") == "true"
    assert len(calls) == 1 and calls[0].endswith(":k-1")
    r3 = client.post("/api/v1/bookings", headers={**h, "Idempotency-Key": "k-1"}, json={**body, "start_at": "2030-01-08T15:00:00Z"})
    assert r3.status_code == 409 and r3.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert r1.json()["manage"]["cancel_url"].endswith("/cancel")
    assert client.get(f"/api/v1/bookings/{r1.json()['id']}", headers=h).json()["status"] == "confirmed"


def test_cancel_and_reschedule_delegate_to_booking_service(db, host, client, monkeypatch):
    booking_mod = pytest.importorskip("app.services.booking")
    user, ws = host
    et = make_et(db, user, ws)
    b = make_booking(db, user, ws, et)
    seen = {}

    def fake_cancel(db_, booking, actor, reason=None, **kw):
        seen["cancel"] = (actor, reason)
        booking.status, booking.cancel_reason = "cancelled", reason
        db_.commit()
        return booking

    monkeypatch.setattr(booking_mod, "cancel_booking", fake_cancel)
    h, _ = key_for(db, user, ws)
    r = client.post(f"/api/v1/bookings/{b.id}/cancel", headers=h, json={"reason": "No longer needed"})
    assert r.status_code == 200 and r.json()["status"] == "cancelled" and seen["cancel"] == ("api", "No longer needed")
    r = client.post(f"/api/v1/bookings/{b.id}/reschedule", headers=h, json={"start_at": "2030-01-01T10:00:00Z"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "INVALID_STATE"


def test_contacts_webhooks_and_analytics(db, host, client):
    user, ws = host
    et = make_et(db, user, ws)
    make_booking(db, user, ws, et)
    db.add(Contact(workspace_id=ws.id, email_normalized="c1@example.com", name="C One"))
    db.commit()
    h, _ = key_for(db, user, ws)
    assert client.get("/api/v1/contacts", headers=h).json()["data"][0]["email"] == "c1@example.com"
    r = client.post("/api/v1/webhook-subscriptions", headers=h,
                    json={"endpoint": "https://hooks.example.com/a", "events": ["booking.created"]})
    assert r.status_code == 201 and r.json()["secret"].startswith("whsec_")
    sid = r.json()["id"]
    lst = client.get("/api/v1/webhook-subscriptions", headers=h).json()["data"]
    assert [s["id"] for s in lst] == [sid] and "secret" not in lst[0]
    assert client.delete(f"/api/v1/webhook-subscriptions/{sid}", headers=h).status_code == 204
    db.commit()
    assert db.scalar(select(WebhookSubscription)) is None
    s = client.get("/api/v1/analytics/summary", headers=h).json()
    assert s["bookings"]["created"] == 1 and s["bookings"]["upcoming"] == 1 and s["by_event_type"][0]["count"] == 1


def test_plan_without_api_feature(db, host, client):
    user, ws = host
    ws.plan_id = "free"
    db.commit()
    h, _ = key_for(db, user, ws)
    assert client.get("/api/v1/me", headers=h).status_code == 403


def test_api_keys_ui(db, host, client, login):
    user, ws = host
    login(client, user)
    assert client.get("/app/integrations/api-keys").status_code == 200
    r = client.post("/app/integrations/api-keys", data={"name": "CRM", "scopes": ["read"]}, follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/app/integrations/api-keys").text
    assert "aal_sk_" in page
    raw = page.split("aal_sk_", 1)[1].split("<", 1)[0]
    assert client.get("/api/v1/me", headers={"Authorization": "Bearer aal_sk_" + raw}).status_code == 200
    key = db.scalar(select(ApiKey))
    client.post(f"/app/integrations/api-keys/{key.id}/revoke", follow_redirects=False)
    assert client.get("/api/v1/me", headers={"Authorization": "Bearer aal_sk_" + raw}).status_code == 401
