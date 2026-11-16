"""Calendar provider registry / mock provider / sync / conflict detection / integrations UI."""
import datetime as dt
import secrets

import pytest
from sqlalchemy import select

from app.events import process_outbox
from app.models import (
    Booking,
    CalendarConnection,
    CalendarEvent,
    ConnectedCalendar,
    EventType,
    ExternalCalendarEvent,
    InAppNotification,
    Invitee,
    NotificationJob,
    OutboxEvent,
    utcnow,
)
from app.services.calendar_facade import CalendarUnavailable, get_busy_intervals
from app.services.providers import registry
from app.services.providers import sync as cal_sync


def make_booking(db, user, ws, start=None, minutes=30, status="confirmed", loc=None):
    db.commit()  # end any read snapshot so this session can write (SQLite WAL)
    et = EventType(workspace_id=ws.id, owner_user_id=user.id, name="Intro call", slug="intro-" + secrets.token_hex(3),
                   duration=minutes, location_json=[loc] if loc else None)
    db.add(et)
    db.flush()
    start = start or (utcnow() + dt.timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
    b = Booking(workspace_id=ws.id, event_type_id=et.id, host_id=user.id, start_at=start,
                end_at=start + dt.timedelta(minutes=minutes), timezone="America/New_York", status=status,
                manage_token=secrets.token_urlsafe(16), location_json=dict(loc) if loc else None)
    b.invitees.append(Invitee(name="Bob Invitee", email="bob@example.com", timezone="America/New_York"))
    db.add(b)
    db.commit()
    return b


def cals(db, conn):
    return {c.display_name: c for c in db.scalars(select(ConnectedCalendar).where(ConnectedCalendar.connection_id == conn.id))}


def test_mock_busy_intervals(db, host):
    user, ws = host
    assert get_busy_intervals(db, user.id, utcnow(), utcnow() + dt.timedelta(days=7)) == []
    conn = cal_sync.connect_mock_calendar(db, user, ws.id)
    c = cals(db, conn)
    assert c["Work"].is_write_calendar and not c["Personal"].is_write_calendar
    t0 = (utcnow() + dt.timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    db.add_all([
        ExternalCalendarEvent(calendar_id=c["Personal"].id, external_id="a", title="Dentist", start_at=t0,
                              end_at=t0 + dt.timedelta(hours=1)),
        ExternalCalendarEvent(calendar_id=c["Work"].id, external_id="b", title="Free block", start_at=t0,
                              end_at=t0 + dt.timedelta(hours=1), transparent=True),
        ExternalCalendarEvent(calendar_id=c["Work"].id, external_id="c", title="Our booking", start_at=t0,
                              end_at=t0 + dt.timedelta(hours=1), booking_id="bk_x"),
    ])
    db.commit()
    busy = get_busy_intervals(db, user.id, t0 - dt.timedelta(hours=2), t0 + dt.timedelta(hours=5))
    assert [(b.start, b.end, b.title) for b in busy] == [(t0, t0 + dt.timedelta(hours=1), "Dentist")]
    # outside the window -> nothing
    assert get_busy_intervals(db, user.id, t0 + dt.timedelta(hours=2), t0 + dt.timedelta(hours=5)) == []
    # unchecking the conflict calendar removes its busy time
    c["Personal"].is_conflict_calendar = False
    db.commit()
    assert get_busy_intervals(db, user.id, t0 - dt.timedelta(hours=2), t0 + dt.timedelta(hours=5)) == []


def test_token_expiry_raises_calendar_unavailable_and_notifies(db, host):
    user, ws = host
    conn = cal_sync.connect_mock_calendar(db, user, ws.id)
    db.commit()
    registry.mark_connection_error(db, conn, "token expired")
    db.commit()
    with pytest.raises(CalendarUnavailable):
        get_busy_intervals(db, user.id, utcnow(), utcnow() + dt.timedelta(days=1))
    assert db.scalar(select(OutboxEvent).where(OutboxEvent.topic == "calendar.sync_failed"))
    # marking again does not publish a second event
    registry.mark_connection_error(db, conn, "still expired")
    db.commit()
    assert len(db.scalars(select(OutboxEvent).where(OutboxEvent.topic == "calendar.sync_failed")).all()) == 1
    process_outbox(db)
    assert db.scalar(select(NotificationJob).where(NotificationJob.trigger == "calendar_sync_failed",
                                                   NotificationJob.recipient == user.email))
    assert db.scalar(select(InAppNotification).where(InAppNotification.kind == "calendar_sync_failed"))


def test_create_and_delete_events_idempotent_with_meet_link(db, host):
    user, ws = host
    conn = cal_sync.connect_mock_calendar(db, user, ws.id)
    b = make_booking(db, user, ws, loc={"type": "google_meet"})
    registry.create_calendar_events(db, b)
    registry.create_calendar_events(db, b)  # retry must not duplicate
    db.commit()
    rows = db.scalars(select(CalendarEvent).where(CalendarEvent.booking_id == b.id)).all()
    assert len(rows) == 1 and rows[0].sync_status == "synced" and rows[0].provider == "mock"
    assert rows[0].join_url.startswith("https://meet.google.com/")
    assert b.location_json["join_url"] == rows[0].join_url
    ext = db.scalars(select(ExternalCalendarEvent).where(ExternalCalendarEvent.booking_id == b.id)).all()
    assert len(ext) == 1 and ext[0].calendar_id == cals(db, conn)["Work"].id
    # our own event never counts as external busy time
    assert get_busy_intervals(db, user.id, b.start_at, b.end_at) == []
    registry.delete_calendar_events(db, b)
    registry.delete_calendar_events(db, b)
    db.commit()
    assert db.get(CalendarEvent, rows[0].id).sync_status == "deleted"
    assert not db.scalars(select(ExternalCalendarEvent).where(ExternalCalendarEvent.booking_id == b.id)).all()


def test_no_connection_records_none_provider(db, host):
    user, ws = host
    b = make_booking(db, user, ws)
    registry.create_calendar_events(db, b)
    db.commit()
    row = db.scalar(select(CalendarEvent).where(CalendarEvent.booking_id == b.id))
    assert row.provider == "none" and row.sync_status == "synced"


def test_video_fallback_and_mock_zoom(db, host, client, login):
    user, ws = host
    b = make_booking(db, user, ws, loc={"type": "zoom"})
    registry.create_calendar_events(db, b)
    db.commit()
    assert b.location_json["host_action_required"] is True
    assert db.scalar(select(InAppNotification).where(InAppNotification.kind == "host_action_required"))
    login(client, user)
    r = client.post("/app/integrations/zoom/connect-demo", follow_redirects=False)
    assert r.status_code == 303
    b2 = make_booking(db, user, ws, start=b.start_at + dt.timedelta(hours=3), loc={"type": "zoom"})
    registry.create_calendar_events(db, b2)
    db.commit()
    assert b2.location_json["join_url"].startswith("https://zoom.us/j/")
    assert not b2.location_json.get("host_action_required")
    # zoom connection is not a calendar: availability unaffected
    assert get_busy_intervals(db, user.id, utcnow(), utcnow() + dt.timedelta(days=5)) == []


def test_failed_write_when_connection_broken_then_retry(db, host):
    user, ws = host
    conn = cal_sync.connect_mock_calendar(db, user, ws.id)
    db.commit()
    registry.mark_connection_error(db, conn, "expired")
    b = make_booking(db, user, ws)
    registry.create_calendar_events(db, b)  # must not raise - booking still succeeds
    db.commit()
    row = db.scalar(select(CalendarEvent).where(CalendarEvent.booking_id == b.id))
    assert row.sync_status == "failed"
    conn.status = "connected"
    db.commit()
    registry.retry_failed_events(db, now=utcnow() + dt.timedelta(hours=1))
    db.commit()
    assert db.get(CalendarEvent, row.id).sync_status == "synced"


def test_reschedule_moves_existing_event(db, host):
    user, ws = host
    cal_sync.connect_mock_calendar(db, user, ws.id)
    old = make_booking(db, user, ws)
    registry.create_calendar_events(db, old)
    db.commit()
    new = Booking(workspace_id=ws.id, event_type_id=old.event_type_id, host_id=user.id,
                  start_at=old.start_at + dt.timedelta(days=1), end_at=old.end_at + dt.timedelta(days=1),
                  status="confirmed", manage_token=secrets.token_urlsafe(16), rescheduled_from_id=old.id)
    new.invitees.append(Invitee(name="Bob Invitee", email="bob@example.com"))
    db.add(new)
    old.status = "rescheduled"
    db.commit()
    registry.update_calendar_events(db, new, {"booking_id": old.id})
    db.commit()
    rows = db.scalars(select(CalendarEvent).where(CalendarEvent.booking_id == new.id)).all()
    assert len(rows) == 1
    ext = db.scalars(select(ExternalCalendarEvent).where(ExternalCalendarEvent.external_id == rows[0].external_event_id)).all()
    assert len(ext) == 1 and ext[0].start_at == new.start_at


def test_conflict_detection_flags_never_deletes(db, host):
    user, ws = host
    conn = cal_sync.connect_mock_calendar(db, user, ws.id)
    b = make_booking(db, user, ws)
    registry.create_calendar_events(db, b)
    db.commit()
    assert cal_sync.detect_conflicts(db, conn) == 0
    db.add(ExternalCalendarEvent(calendar_id=cals(db, conn)["Personal"].id, external_id="x", title="Flight",
                                 start_at=b.start_at - dt.timedelta(minutes=15), end_at=b.start_at + dt.timedelta(minutes=15)))
    db.commit()
    assert cal_sync.detect_conflicts(db, conn) == 1
    assert cal_sync.detect_conflicts(db, conn) == 0  # idempotent
    db.commit()
    db.refresh(b)
    assert b.conflict_flag and "Flight" in b.conflict_detail and b.status == "confirmed"
    assert db.scalar(select(OutboxEvent).where(OutboxEvent.topic == "booking.conflict"))
    process_outbox(db)
    assert db.scalar(select(NotificationJob).where(NotificationJob.trigger == "booking_conflict"))


def test_drift_detection_external_delete(db, host):
    user, ws = host
    conn = cal_sync.connect_mock_calendar(db, user, ws.id)
    b = make_booking(db, user, ws)
    registry.create_calendar_events(db, b)
    db.commit()
    ext = db.scalar(select(ExternalCalendarEvent).where(ExternalCalendarEvent.booking_id == b.id))
    db.delete(ext)
    db.commit()
    cal_sync.calendar_sync_task(db, force=True)
    db.refresh(b)
    assert b.conflict_flag and "deleted" in b.conflict_detail and b.status == "confirmed"


def test_integrations_ui_flow(db, host, client, login):
    user, ws = host
    login(client, user)
    assert client.get("/app/integrations").status_code == 200
    r = client.post("/app/integrations/mock/connect", follow_redirects=False)
    assert r.status_code == 303
    conn = db.scalar(select(CalendarConnection).where(CalendarConnection.user_id == user.id))
    page = client.get("/app/integrations")
    assert "Demo calendar" in page.text and "Simulate token expiry" in page.text
    c = cals(db, conn)
    r = client.post("/app/integrations/calendars", data={"conflict": [c["Work"].id], "write_calendar": c["Personal"].id},
                    follow_redirects=False)
    assert r.status_code == 303
    db.commit(); db.expire_all()
    c = cals(db, conn)
    assert c["Personal"].is_write_calendar and not c["Personal"].is_conflict_calendar and c["Work"].is_conflict_calendar
    assert client.get(f"/app/integrations/connections/{conn.id}/events").status_code == 200
    r = client.post(f"/app/integrations/connections/{conn.id}/events", data={
        "calendar_id": c["Work"].id, "title": "Busy", "date": "2030-01-07", "start": "10:00", "end": "11:00"},
        follow_redirects=False)
    assert r.status_code == 303
    assert client.post(f"/app/integrations/connections/{conn.id}/simulate-expiry", follow_redirects=False).status_code == 303
    db.commit(); db.expire_all()
    assert db.get(CalendarConnection, conn.id).status == "error"
    assert "Reconnect required" in client.get("/app/integrations").text
    client.post(f"/app/integrations/connections/{conn.id}/reconnect", follow_redirects=False)
    db.commit(); db.expire_all()
    assert db.get(CalendarConnection, conn.id).status == "connected"
    conn_id = conn.id
    client.post(f"/app/integrations/connections/{conn_id}/disconnect", follow_redirects=False)
    db.commit(); db.expire_all()
    assert db.scalar(select(CalendarConnection).where(CalendarConnection.id == conn_id)) is None


def test_other_users_connection_is_not_accessible(db, host, client, login):
    from app.services.bootstrap import create_user_with_workspace

    user, ws = host
    other, ows = create_user_with_workspace(db, "eve@example.com", "Eve", password="password123", slug="eve")
    conn = cal_sync.connect_mock_calendar(db, other, ows.id)
    db.commit()
    login(client, user)
    assert client.post(f"/app/integrations/connections/{conn.id}/disconnect", follow_redirects=False).status_code == 404


def test_ics_feed(db, host, client):
    from app.services import ics_feed

    user, ws = host
    make_booking(db, user, ws)
    raw = ics_feed.issue(db, user.id)
    db.commit()
    r = client.get(f"/api/v1/calendar-feed/{user.id}/{raw}.ics")
    assert r.status_code == 200 and "BEGIN:VEVENT" in r.text and r.headers["content-type"].startswith("text/calendar")
    assert client.get(f"/api/v1/calendar-feed/{user.id}/wrong.ics").status_code == 404
    from app.models_commerce import IcsFeedToken

    assert raw not in db.scalar(select(IcsFeedToken)).token_hash  # stored hashed


def test_ics_feed_rotate_and_revoke(db, host, client, login):
    import re

    user, ws = host
    make_booking(db, user, ws)
    login(client, user)
    page = client.get("/app/integrations").text
    assert "Create feed URL" in page
    r = client.post("/app/integrations/ics-feed/regenerate", follow_redirects=True)
    old_url = re.search(r'/api/v1/calendar-feed/[^"]+\.ics', r.text).group(0)
    assert client.get(old_url).status_code == 200
    assert old_url not in client.get("/app/integrations").text  # shown once only
    r = client.post("/app/integrations/ics-feed/regenerate", follow_redirects=True)
    new_url = re.search(r'/api/v1/calendar-feed/[^"]+\.ics', r.text).group(0)
    assert new_url != old_url
    assert client.get(old_url).status_code == 404  # rotated -> old URL invalid
    assert client.get(new_url).status_code == 200
    client.post("/app/integrations/ics-feed/revoke")
    assert client.get(new_url).status_code == 404


def test_embed_js_served(client, host, login):
    r = client.get("/embed.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"] and "max-age" in r.headers["cache-control"]
    assert "initInlineWidget" in r.text and "initPopupWidget" in r.text and "aal:event_scheduled" in r.text
    assert client.get("/embed.js", headers={"If-None-Match": r.headers["etag"]}).status_code == 304
    login(client, host[0])
    assert client.get("/embed/demo").status_code == 200


def test_dev_inbox(db, client, host, login):
    from app.services.messaging import send_email

    send_email(db, "alice@example.com", "Hello", "<p>Hi</p>", [{"filename": "invite.ics", "content": "BEGIN:VCALENDAR"}])
    db.commit()
    login(client, host[0])
    r = client.get("/dev/inbox")
    assert r.status_code == 200 and "alice@example.com" in r.text
    from app.models import SentMessage

    m = db.scalar(select(SentMessage))
    assert client.get(f"/dev/inbox/{m.id}/attachments/0").text == "BEGIN:VCALENDAR"
    assert client.post("/dev/run-worker", follow_redirects=False).status_code == 303


def test_dev_inbox_is_tenant_scoped_and_hidden_in_prod(db, client, host, login, monkeypatch):
    from app.config import settings
    from app.models import SentMessage, User
    from app.services.bootstrap import create_user_with_workspace
    from app.services.messaging import send_email, send_sms

    user, ws = host
    b = make_booking(db, user, ws)
    b.invitees[0].phone = "+15550001"
    other, ows = create_user_with_workspace(db, "eve@example.com", "Eve", password="password123", slug="eve")
    db.commit()
    send_email(db, "bob@example.com", "To my invitee", "<p>a</p>")
    send_sms(db, "+15550001", "sms to my invitee")
    secret = send_email(db, "eve@example.com", "Eve secret", "<p>b</p>")
    stranger = send_email(db, "stranger@example.com", "Stranger", "<p>c</p>")
    db.commit()
    login(client, user)
    page = client.get("/dev/inbox").text
    assert "To my invitee" in page and "+15550001" in page
    assert "Eve secret" not in page and "Stranger" not in page
    assert client.get(f"/dev/inbox/{secret.id}/raw").status_code == 404
    assert client.get(f"/dev/inbox?id={stranger.id}").text.count("Stranger") == 0
    client.post("/dev/inbox/clear")
    assert db.get(SentMessage, secret.id) is not None  # other tenants' messages untouched
    # anonymous users are sent to login
    client.cookies.clear()
    assert client.get("/dev/inbox", follow_redirects=False).status_code == 303
    # prod: hidden unless platform admin
    monkeypatch.setattr(settings, "env", "prod")
    login(client, user)
    assert client.get("/dev/inbox").status_code == 404
    u = db.get(User, user.id)
    u.is_platform_admin = True
    db.commit()
    page = client.get("/dev/inbox").text
    assert "Eve secret" in page and "Stranger" in page


# ------------------------------------------------------------------ PRD 9 / 24 confirmations
def test_provider_rate_limit_and_timeout_are_transient_and_backed_off(db, host, monkeypatch):
    import httpx

    from app.services.calendar_facade import TransientProviderError
    from app.services.providers import google
    from app.services.providers.base import raise_for_status

    # timeouts and HTTP 429 / 5xx map to retryable errors
    def timeout(*a, **k):
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(google.httpx, "request", timeout)
    with pytest.raises(TransientProviderError):
        google._request("GET", "https://www.googleapis.com/calendar/v3/x")
    for code in (429, 503):
        with pytest.raises(TransientProviderError):
            raise_for_status(httpx.Response(code, text="slow down"))
    # sync loop backs off exponentially on transient errors, then recovers
    user, ws = host
    conn = cal_sync.connect_mock_calendar(db, user, ws.id)
    conn.last_synced_at = None
    db.commit()
    calls = {"n": 0}

    def rate_limited(db_, conn_, now=None):
        calls["n"] += 1
        raise TransientProviderError("HTTP 429: rate limit exceeded")

    monkeypatch.setattr(cal_sync, "detect_conflicts", rate_limited)
    cal_sync.calendar_sync_task(db)
    db.refresh(conn)
    st = conn.sync_state_json
    assert calls["n"] == 1 and st["consecutive_failures"] == 1 and conn.status == "connected"
    first_wait = dt.datetime.fromisoformat(st["next_attempt_at"]) - utcnow()
    cal_sync.calendar_sync_task(db)  # inside the backoff window: no provider call
    assert calls["n"] == 1
    for _ in range(3):
        conn.sync_state_json = dict(conn.sync_state_json, next_attempt_at=(utcnow() - dt.timedelta(seconds=1)).isoformat())
        db.commit()
        cal_sync.calendar_sync_task(db)
        db.refresh(conn)
    wait = dt.datetime.fromisoformat(conn.sync_state_json["next_attempt_at"]) - utcnow()
    assert calls["n"] == 4 and wait > first_wait * 4  # exponential
    # after 5 consecutive failures the connection is put into the reconnect state
    conn.sync_state_json = dict(conn.sync_state_json, next_attempt_at=None)
    db.commit()
    cal_sync.calendar_sync_task(db)
    db.refresh(conn)
    assert conn.status == "error"
    # recovery clears the backoff
    monkeypatch.undo()
    conn.status = "connected"
    db.commit()
    cal_sync.sync_connection(db, conn)
    db.commit()
    assert "next_attempt_at" not in conn.sync_state_json and conn.sync_state_json["consecutive_failures"] == 0


def test_revoked_refresh_token_shows_reconnect_state(db, host, client, login, monkeypatch):
    import httpx

    from app.services.providers import google
    from app.services.providers.base import set_tokens

    user, ws = host
    conn = CalendarConnection(workspace_id=ws.id, user_id=user.id, provider="google", account_email=user.email,
                              status="connected", expires_at=utcnow() - dt.timedelta(minutes=1))
    set_tokens(conn, {"access_token": "a", "refresh_token": "r"})
    db.add(conn)
    db.commit()
    monkeypatch.setattr(google, "_request", lambda *a, **k: httpx.Response(400, json={"error": "invalid_grant"}))
    cal_sync.sync_connection(db, conn)
    db.commit()
    db.refresh(conn)
    assert conn.status == "error" and "revoked or expired" in conn.last_error
    with pytest.raises(CalendarUnavailable):  # never show unverified availability as safe
        get_busy_intervals(db, user.id, utcnow(), utcnow() + dt.timedelta(days=1))
    login(client, user)
    page = client.get("/app/integrations").text
    assert "Reconnect required" in page and "Action required" in page and "Reconnect" in page
    process_outbox(db)
    assert db.scalar(select(NotificationJob).where(NotificationJob.trigger == "calendar_sync_failed"))


def test_event_moved_outside_app_is_flagged(db, host):
    user, ws = host
    conn = cal_sync.connect_mock_calendar(db, user, ws.id)
    b = make_booking(db, user, ws)
    registry.create_calendar_events(db, b)
    db.commit()
    ext = db.scalar(select(ExternalCalendarEvent).where(ExternalCalendarEvent.booking_id == b.id))
    ext.start_at += dt.timedelta(hours=2)
    ext.end_at += dt.timedelta(hours=2)
    db.commit()
    assert cal_sync.detect_conflicts(db, conn) == 1
    db.commit()
    db.refresh(b)
    assert b.conflict_flag and "moved externally" in b.conflict_detail and b.status == "confirmed"
    assert db.scalar(select(OutboxEvent).where(OutboxEvent.topic == "booking.conflict"))
