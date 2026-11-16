import datetime as dt
import secrets

import pytest
from sqlalchemy import select

from app.models import (
    AuditLog,
    AvailabilityOverride,
    AvailabilityRule,
    AvailabilitySchedule,
    Booking,
    BookingEvent,
    CalendarConnection,
    DataRequest,
    EventType,
    InAppNotification,
    Invitee,
    SentMessage,
    Subscription,
    User,
    Workspace,
    utcnow,
)
from app.services.bootstrap import add_user_to_workspace, create_user_with_workspace
from app.services.holidays import holidays_for


def fresh(db):
    db.rollback()
    db.expire_all()


def make_event(db, user, ws, **kw):
    ev = EventType(workspace_id=ws.id, owner_user_id=user.id, name=kw.pop("name", "Intro"),
                   slug=kw.pop("slug", "intro-" + secrets.token_hex(3)), duration=30, **kw)
    db.add(ev)
    db.flush()
    return ev


def make_booking(db, ev, host_id, start, status="confirmed", name="Ivy Invitee", email="ivy@example.com", **kw):
    b = Booking(workspace_id=ev.workspace_id, event_type_id=ev.id, host_id=host_id, start_at=start,
                end_at=start + dt.timedelta(minutes=30), status=status, manage_token=secrets.token_urlsafe(16),
                timezone="America/New_York", **kw)
    b.invitees.append(Invitee(name=name, email=email, timezone="America/New_York"))
    db.add(b)
    db.flush()
    return b


def _sched(db, user):
    return db.scalar(select(AvailabilitySchedule).where(AvailabilitySchedule.user_id == user.id,
                                                        AvailabilitySchedule.is_default.is_(True)))


# ------------------------------------------------------------------ availability
def test_availability_weekly_save_and_reload(client, db, host, login):
    user, _ = host
    login(client, user)
    assert client.get("/app/availability").status_code == 200
    sch = _sched(db, user)
    r = client.post(f"/app/availability/{sch.id}/weekly", data={
        "name": "Main hours", "timezone": "Asia/Kolkata",
        "d0_start": ["09:00", "13:00"], "d0_end": ["12:00", "17:00"],
        "d3_start": "10:00", "d3_end": "23:59"}, follow_redirects=False)
    assert r.status_code == 303
    fresh(db)
    sch = db.get(AvailabilitySchedule, sch.id)
    rules = db.scalars(select(AvailabilityRule).where(AvailabilityRule.schedule_id == sch.id)
                       .order_by(AvailabilityRule.weekday, AvailabilityRule.start_local)).all()
    assert sch.name == "Main hours" and sch.timezone == "Asia/Kolkata"
    assert [(r.weekday, r.start_local, r.end_local) for r in rules] == [
        (0, "09:00", "12:00"), (0, "13:00", "17:00"), (3, "10:00", "24:00")]
    page = client.get(f"/app/availability?schedule={sch.id}").text
    assert 'value="13:00"' in page and "Main hours" in page
    # overlap rejected -> rules unchanged
    client.post(f"/app/availability/{sch.id}/weekly", data={"timezone": "UTC", "d1_start": ["09:00", "10:00"],
                                                            "d1_end": ["11:00", "12:00"]})
    # start after end rejected
    client.post(f"/app/availability/{sch.id}/weekly", data={"timezone": "UTC", "d1_start": "15:00", "d1_end": "09:00"})
    fresh(db)
    assert db.scalar(select(AvailabilityRule).where(AvailabilityRule.schedule_id == sch.id,
                                                    AvailabilityRule.weekday == 1)) is None
    assert db.get(AvailabilitySchedule, sch.id).timezone == "Asia/Kolkata"


def test_schedules_overrides_holidays_limits(client, db, host, login):
    user, ws = host
    login(client, user)
    r = client.post("/app/availability/schedules", data={"name": "Evenings"}, follow_redirects=False)
    new_id = r.headers["location"].split("schedule=")[1]
    fresh(db)
    assert db.get(AvailabilitySchedule, new_id).name == "Evenings"
    client.post(f"/app/availability/{new_id}/default")
    fresh(db)
    assert db.get(AvailabilitySchedule, new_id).is_default
    old = db.scalar(select(AvailabilitySchedule).where(AvailabilitySchedule.user_id == user.id,
                                                       AvailabilitySchedule.id != new_id))
    assert not old.is_default
    # cannot delete default
    client.post(f"/app/availability/{new_id}/delete")
    fresh(db)
    assert db.get(AvailabilitySchedule, new_id) is not None

    day = dt.date.today() + dt.timedelta(days=10)
    client.post(f"/app/availability/{new_id}/overrides", data={"date": day.isoformat(), "mode": "custom",
                                                               "start": ["10:00", "14:00"], "end": ["11:00", "15:00"],
                                                               "reason": "Dentist"})
    client.post(f"/app/availability/{new_id}/overrides", data={
        "date": (day + dt.timedelta(days=1)).isoformat(), "end_date": (day + dt.timedelta(days=3)).isoformat(),
        "mode": "unavailable", "kind": "day_off", "reason": "Vacation"})
    fresh(db)
    ovs = db.scalars(select(AvailabilityOverride).where(AvailabilityOverride.schedule_id == new_id)
                     .order_by(AvailabilityOverride.local_date)).all()
    assert len(ovs) == 4
    assert ovs[0].intervals_json == [{"start": "10:00", "end": "11:00"}, {"start": "14:00", "end": "15:00"}]
    assert all(o.kind == "day_off" and o.intervals_json == [] for o in ovs[1:])
    client.post(f"/app/availability/{new_id}/overrides/{ovs[0].id}/delete")

    # holidays
    upcoming = [d for d, _ in holidays_for("GB") if d > dt.date.today()][:2]
    assert client.get(f"/app/availability?schedule={new_id}&country=GB").status_code == 200
    client.post(f"/app/availability/{new_id}/holidays", data={"country": "GB", "dates": [d.isoformat() for d in upcoming]})
    fresh(db)
    hol = db.scalars(select(AvailabilityOverride).where(AvailabilityOverride.schedule_id == new_id,
                                                        AvailabilityOverride.kind == "holiday")).all()
    assert {h.local_date for h in hol} == set(upcoming)
    assert db.get(AvailabilitySchedule, new_id).holiday_country == "GB"
    client.post(f"/app/availability/{new_id}/holidays", data={"country": "GB", "dates": [upcoming[0].isoformat()]})
    fresh(db)
    assert len(db.scalars(select(AvailabilityOverride).where(AvailabilityOverride.schedule_id == new_id,
                                                             AvailabilityOverride.kind == "holiday")).all()) == 1

    # host-wide limits
    client.post("/app/availability/limits", data={"daily_limit": "5", "weekly_limit": "", "monthly_limit": "40"})
    fresh(db)
    wh = db.get(User, user.id).working_hours_json
    assert wh["daily_limit"] == 5 and wh["weekly_limit"] is None and wh["monthly_limit"] == 40

    # delete non-default schedule; event types fall back
    client.post(f"/app/availability/{old.id}/default")
    fresh(db)
    ev = make_event(db, user, ws, schedule_id=new_id)
    db.commit()
    client.post(f"/app/availability/{new_id}/delete")
    fresh(db)
    assert db.get(AvailabilitySchedule, new_id) is None and db.get(EventType, ev.id).schedule_id is None


def test_cross_tenant_schedule_denied(client, db, host, login):
    user_a, _ = host
    user_b, _ = create_user_with_workspace(db, "b@example.com", "B", password="password123", verified=True)
    db.commit()
    sch_a = _sched(db, user_a)
    login(client, user_b)
    r = client.post(f"/app/availability/{sch_a.id}/weekly", data={"timezone": "UTC"})
    assert r.status_code == 404
    assert client.post(f"/app/availability/{sch_a.id}/delete").status_code == 404


# ------------------------------------------------------------------ scheduled events
def test_scheduled_list_detail_and_export(client, db, host, login):
    user, ws = host
    ev = make_event(db, user, ws, name="Discovery")
    soon = utcnow() + dt.timedelta(days=1)
    b1 = make_booking(db, ev, user.id, soon.replace(second=0, microsecond=0), conflict_flag=True,
                      conflict_detail="Overlaps 'Dentist'")
    b2 = make_booking(db, ev, user.id, utcnow() - dt.timedelta(days=2), name="Past Pat", email="pat@example.com")
    b3 = make_booking(db, ev, user.id, utcnow() + dt.timedelta(days=3), status="cancelled", name="Cal Cancel",
                      email="cal@example.com", cancelled_by="invitee")
    b4 = make_booking(db, ev, user.id, utcnow() + dt.timedelta(days=4), name="Req Rita", email="rita@example.com",
                      cancellation_requested_at=utcnow())
    db.add(BookingEvent(booking_id=b1.id, actor="invitee", event_type="created", payload_json={}))
    db.commit()
    login(client, user)
    up = client.get("/app/scheduled").text
    assert "Ivy Invitee" in up and "Conflict" in up and "Past Pat" not in up
    assert "Past Pat" in client.get("/app/scheduled?tab=past").text
    assert "Cal Cancel" in client.get("/app/scheduled?tab=cancelled").text
    assert "Req Rita" in client.get("/app/scheduled?tab=pending").text
    filt = client.get("/app/scheduled?tab=upcoming&q=rita").text
    assert "Req Rita" in filt and "Ivy Invitee" not in filt
    d = client.get(f"/app/scheduled/{b1.id}")
    assert d.status_code == 200 and "Overlaps" in d.text and "Created" in d.text
    r = client.post(f"/app/scheduled/{b1.id}/notes", data={"host_notes": "Bring slides"}, follow_redirects=False)
    assert r.status_code == 303
    fresh(db)
    assert db.get(Booking, b1.id).host_notes == "Bring slides"
    csv_resp = client.get("/app/scheduled/export.csv?tab=upcoming")
    assert csv_resp.status_code == 200 and "text/csv" in csv_resp.headers["content-type"]
    assert "ivy@example.com" in csv_resp.text and "pat@example.com" not in csv_resp.text
    # request reschedule emails the invitee
    client.post(f"/app/scheduled/{b1.id}/request-reschedule", data={"message": "Sorry!"})
    fresh(db)
    assert db.scalar(select(SentMessage).where(SentMessage.recipient == "ivy@example.com"))
    assert db.scalar(select(BookingEvent).where(BookingEvent.booking_id == b1.id,
                                                BookingEvent.event_type == "reschedule_requested"))
    # reschedule page renders even if availability service is still being built
    assert client.get(f"/app/scheduled/{b1.id}/reschedule").status_code == 200


def test_scheduled_actions_via_booking_service(client, db, host, login):
    pytest.importorskip("app.services.booking")
    user, ws = host
    ev = make_event(db, user, ws)
    b = make_booking(db, ev, user.id, utcnow() - dt.timedelta(hours=2))
    db.commit()
    login(client, user)
    client.post(f"/app/scheduled/{b.id}/no-show", data={"value": "1"})
    fresh(db)
    assert db.get(Booking, b.id).no_show is True
    client.post(f"/app/scheduled/{b.id}/no-show", data={"value": "0"})
    fresh(db)
    fb = make_booking(db, ev, user.id, utcnow() + dt.timedelta(days=2), email="fut@example.com")
    db.commit()
    client.post(f"/app/scheduled/{fb.id}/cancel", data={"reason": "Conflict came up"})
    fresh(db)
    assert db.get(Booking, b.id).no_show is False
    fb = db.get(Booking, fb.id)
    assert fb.status == "cancelled" and fb.cancelled_by == "host" and fb.cancel_reason == "Conflict came up"


def test_member_sees_only_own_bookings_and_cross_tenant_denied(client, db, host, login):
    owner, ws = host
    member = add_user_to_workspace(db, ws, "mem@example.com", "Member", password="password123")
    ev = make_event(db, owner, ws)
    b_owner = make_booking(db, ev, owner.id, utcnow() + dt.timedelta(days=1), name="Owner Guest")
    other, ws_b = create_user_with_workspace(db, "other@example.com", "Other", password="password123", verified=True)
    db.commit()
    login(client, member)
    assert "Owner Guest" not in client.get("/app/scheduled").text
    assert client.get(f"/app/scheduled/{b_owner.id}").status_code == 404
    login(client, other)
    assert client.get(f"/app/scheduled/{b_owner.id}").status_code == 404
    assert client.post(f"/app/scheduled/{b_owner.id}/cancel", data={"reason": "x"}).status_code == 404
    assert client.post(f"/app/scheduled/{b_owner.id}/notes", data={"host_notes": "x"}).status_code == 404
    fresh(db)
    assert db.get(Booking, b_owner.id).status == "confirmed" and db.get(Booking, b_owner.id).host_notes is None
    login(client, owner)
    assert client.get(f"/app/scheduled/{b_owner.id}").status_code == 200


# ------------------------------------------------------------------ dashboard
def test_home_dashboard(client, db, host, login):
    user, ws = host
    ev = make_event(db, user, ws, name="Coffee chat")
    make_booking(db, ev, user.id, utcnow() + dt.timedelta(days=2), name="Dana Dash")
    make_booking(db, ev, user.id, utcnow() + dt.timedelta(days=3), name="Req R", cancellation_requested_at=utcnow())
    db.add(CalendarConnection(workspace_id=ws.id, user_id=user.id, provider="google", status="error",
                              last_error="invalid_grant", account_email="alice@gmail.com"))
    n = InAppNotification(user_id=user.id, kind="booking", title="New booking from Dana", link="/app/scheduled")
    db.add(n)
    db.commit()
    login(client, user)
    page = client.get("/app")
    assert page.status_code == 200
    t = page.text
    assert "Dana Dash" in t and "Coffee chat" in t and "Reconnect" in t and "New booking from Dana" in t
    assert "Cancellation request" in t
    r = client.post(f"/app/notifications/{n.id}/read", follow_redirects=False)
    assert r.headers["location"] == "/app/scheduled"
    fresh(db)
    assert db.get(InAppNotification, n.id).read_at is not None


def test_unverified_banner(client, db, login):
    user, _ = create_user_with_workspace(db, "unv@example.com", "Unverified", password="password123")
    db.commit()
    login(client, user)
    assert "Verify your email" in client.get("/app").text
    client.post("/verify-email/resend")
    fresh(db)
    assert db.scalar(select(SentMessage).where(SentMessage.recipient == "unv@example.com"))


# ------------------------------------------------------------------ settings
def test_settings_save(client, db, host, login):
    user, ws = host
    create_user_with_workspace(db, "taken@example.com", "Taken", slug="takenslug")
    db.commit()
    login(client, user)
    for tab in ["profile", "branding", "notifications", "security", "workspace", "billing", "data"]:
        assert client.get(f"/app/settings?tab={tab}").status_code == 200, tab
    client.post("/app/settings/profile", data={"name": "Alice H", "slug": "alice-h", "timezone": "Asia/Tokyo",
                                               "locale": "fr", "headline": "Hello", "avatar_url": "https://x.test/a.png",
                                               "work_start": "08:00", "work_end": "16:00"})
    fresh(db)
    u = db.get(User, user.id)
    assert (u.name, u.slug, u.default_timezone, u.locale, u.headline) == ("Alice H", "alice-h", "Asia/Tokyo", "fr", "Hello")
    assert u.working_hours_json["start"] == "08:00"
    client.post("/app/settings/profile", data={"name": "Alice H", "slug": "takenslug", "timezone": "Asia/Tokyo"})
    client.post("/app/settings/profile", data={"name": "Alice H", "slug": "api", "timezone": "Asia/Tokyo"})
    fresh(db)
    assert db.get(User, user.id).slug == "alice-h"
    client.post("/app/settings/branding", data={"primary": "#ff0000", "hide_branding": "1"})
    client.post("/app/settings/notifications", data={"new_booking_email": "1"})
    fresh(db)
    u = db.get(User, user.id)
    assert u.branding_json["primary"] == "#ff0000" and u.branding_json["hide_branding"] is True
    assert u.notification_prefs_json["new_booking_email"] and not u.notification_prefs_json["cancellation_email"]
    # password change
    client.post("/app/settings/security/password", data={"current_password": "wrong", "new_password": "newpass123",
                                                          "confirm_password": "newpass123"})
    client.post("/app/settings/security/password", data={"current_password": "password123", "new_password": "newpass123",
                                                          "confirm_password": "newpass123"})
    from app.security import verify_password

    fresh(db)
    assert verify_password("newpass123", db.get(User, user.id).password_hash)
    # workspace + billing
    client.post("/app/settings/workspace", data={"name": "Acme", "slug": "acme"})
    client.post("/app/settings/billing/plan", data={"plan": "standard"})
    fresh(db)
    w = db.get(Workspace, ws.id)
    assert w.name == "Acme" and w.plan_id == "standard"
    sub = db.scalar(select(Subscription).where(Subscription.workspace_id == ws.id))
    assert sub.plan == "standard" and sub.seats == 1
    assert db.scalar(select(AuditLog).where(AuditLog.action == "billing.plan_changed"))
    # free plan blocks hiding branding
    client.post("/app/settings/billing/plan", data={"plan": "free"})
    client.post("/app/settings/branding", data={"primary": "#00ff00", "hide_branding": "1"})
    fresh(db)
    assert db.get(User, user.id).branding_json["primary"] == "#ff0000"


def test_member_cannot_change_plan_or_workspace(client, db, host, login):
    _, ws = host
    m = add_user_to_workspace(db, ws, "m@example.com", "M", password="password123")
    db.commit()
    login(client, m)
    assert client.post("/app/settings/billing/plan", data={"plan": "free"}).status_code == 403
    assert client.post("/app/settings/workspace", data={"name": "x", "slug": "x"}).status_code == 403


def test_data_export_and_account_deletion(client, db, host, login):
    user, ws = host
    ev = make_event(db, user, ws, name="Exported event")
    make_booking(db, ev, user.id, utcnow() + dt.timedelta(days=1), email="exp@example.com")
    db.commit()
    login(client, user)
    r = client.post("/app/settings/data/export", follow_redirects=False)
    assert r.status_code == 303
    fresh(db)
    dr = db.scalar(select(DataRequest).where(DataRequest.user_id == user.id, DataRequest.kind == "export"))
    assert dr.status == "completed"
    dl = client.get(f"/app/settings/data/export/{dr.id}.json")
    assert dl.status_code == 200 and "attachment" in dl.headers["content-disposition"]
    data = dl.json()
    assert data["user"]["email"] == "alice@example.com"
    assert data["event_types"][0]["name"] == "Exported event"
    assert data["bookings_hosted"][0]["invitees"][0]["email"] == "exp@example.com"
    assert db.scalar(select(AuditLog).where(AuditLog.action == "data.export_requested"))
    # other users can't download it
    other, _ = create_user_with_workspace(db, "o@example.com", "O", password="password123", verified=True)
    db.commit()
    login(client, other)
    assert client.get(f"/app/settings/data/export/{dr.id}.json").status_code == 404
    # deletion requires confirmation
    login(client, user)
    client.post("/app/settings/data/delete", data={"confirm_email": "wrong@example.com", "password": "password123"})
    fresh(db)
    assert db.get(User, user.id).deleted_at is None
    r = client.post("/app/settings/data/delete", data={"confirm_email": "alice@example.com", "password": "password123"},
                    follow_redirects=False)
    assert r.status_code == 303
    fresh(db)
    assert db.get(User, user.id).deleted_at is not None
    assert db.get(EventType, ev.id).status == "paused"
    assert db.scalar(select(DataRequest).where(DataRequest.user_id == user.id, DataRequest.kind == "deletion"))
    assert db.scalar(select(AuditLog).where(AuditLog.action == "user.deleted"))
    assert client.get("/app", follow_redirects=False).status_code == 303
    r = client.post("/login", data={"email": "alice@example.com", "password": "password123"})
    assert r.status_code == 400
