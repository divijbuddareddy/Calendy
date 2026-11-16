"""Public booking pages + public JSON API (PRD 8, 15.4, 22, acceptance 30/32/33/35)."""
import datetime as dt
import re

from sqlalchemy import func, select

from app.models import (
    Booking,
    CalendarConnection,
    EventQuestion,
    EventType,
    EventTypeHost,
    PageView,
    SentMessage,
    Team,
    WaitlistEntry,
)
from app.services import calendar_facade as cf
from app.services import timeutil as tu


def next_weekday(wd: int, min_days: int = 3) -> dt.date:
    d = tu.utcnow().date() + dt.timedelta(days=min_days)
    while d.weekday() != wd:
        d += dt.timedelta(days=1)
    return d


DAY = next_weekday(2)  # a Wednesday at least 3 days ahead
SLOT_UTC = tu.wall_to_utc(DAY, 10 * 60, "Europe/London")
SLOT_ISO = tu.iso_z(SLOT_UTC)


def make_event(db, user, ws, **kw):
    defaults = dict(workspace_id=ws.id, owner_user_id=user.id, name="Discovery call", slug=kw.pop("slug", "discovery-call"),
                    duration=30, min_notice_minutes=60, max_days_ahead=60, description="Let's talk")
    defaults.update(kw)
    et = EventType(**defaults)
    db.add(et)
    db.commit()
    return et


def post_booking(client, body, key="key-1"):
    return client.post("/api/v1/public/bookings", json=body, headers={"Idempotency-Key": key})


def base_body(**kw):
    body = {"owner_slug": "alice", "event_type_slug": "discovery-call", "start_at": SLOT_ISO, "timezone": "America/New_York",
            "invitee": {"name": "Alex Example", "email": "alex@example.com"}, "answers": []}
    body.update(kw)
    return body


def test_profile_lists_only_public_active_events(client, db, host):
    user, ws = host
    make_event(db, user, ws)
    make_event(db, user, ws, slug="paused-one", name="Paused one", status="paused")
    make_event(db, user, ws, slug="secret-one", name="Secret one", visibility="secret")
    make_event(db, user, ws, slug="oneoff", name="One off", is_one_off=True)
    r = client.get("/alice?utm_source=x")
    assert r.status_code == 200
    assert "Alice Host" in r.text and "Discovery call" in r.text
    assert "Paused one" not in r.text and "Secret one" not in r.text and "One off" not in r.text
    assert db.scalar(select(func.count(PageView.id)).where(PageView.page == "profile")) == 1
    assert client.get("/nobody-here").status_code == 404


def test_booking_page_renders_with_prefill_and_questions(client, db, host):
    user, ws = host
    et = make_event(db, user, ws, durations_json=[30, 60], price_minor=5000, currency="GBP",
                    location_json=[{"type": "zoom"}, {"type": "in_person", "value": "1 Main St"}],
                    cancellation_policy_json={"policy_text": "Cancel 24h ahead please", "min_notice_minutes": 1440})
    db.add_all([
        EventQuestion(event_type_id=et.id, type="text", label="Company name", required=True, sort_order=1, help_text="Your org"),
        EventQuestion(event_type_id=et.id, type="select", label="Topic", options_json=["Sales", "Support"], sort_order=2),
        EventQuestion(event_type_id=et.id, type="consent", label="I agree to terms", required=True, sort_order=3),
    ])
    db.commit()
    r = client.get("/alice/discovery-call?name=Pat&email=pat%40example.com&a1=Acme&a2=Support&utm_campaign=fall&rsid=rs_1")
    assert r.status_code == 200
    t = r.text
    assert 'value="Pat"' in t and 'value="pat@example.com"' in t and 'value="Acme"' in t
    assert '<option value="Support" selected>' in t
    assert "Company name" in t and "Your org" in t and "I agree to terms" in t
    assert "£50.00" in t and "Continue to payment" in t and "Cancel 24h ahead please" in t
    assert 'data-duration="60"' in t and "1 Main St" in t
    assert '"utm_campaign": "fall"' in t and '"rsid": "rs_1"' in t
    assert 'name="website"' in t  # honeypot
    assert "/static/booking.js" in t
    assert r.headers.get("x-frame-options") == "SAMEORIGIN"


def test_embed_mode_headers_and_theme(client, db, host):
    user, ws = host
    make_event(db, user, ws)
    r = client.get("/alice/discovery-call?embed=1&hide_details=1&primary_color=ff0000&radius=4")
    assert r.status_code == 200
    assert "frame-ancestors" in r.headers["content-security-policy"]
    assert "x-frame-options" not in r.headers
    assert "--primary: #e50000" in r.text and 'class="embedded"' in r.text  # #ff0000 darkened to reach 4.5:1 (AA)
    assert "booking-info" not in r.text.split("<style>")[-1].split("</style>")[-1]  # details column hidden


def test_paused_event_shows_unavailable(client, db, host):
    user, ws = host
    make_event(db, user, ws, status="paused")
    r = client.get("/alice/discovery-call")
    assert r.status_code == 404 and "accepting bookings at the moment" in r.text
    r = client.get(f"/api/v1/public/event-types/alice/discovery-call/availability?from={DAY}&to={DAY}")
    assert r.status_code == 404 and r.json()["error"]["code"] == "EVENT_UNAVAILABLE"
    r = post_booking(client, base_body())
    assert r.status_code == 404


def test_invitee_in_other_timezone_books_via_api(client, db, host):
    user, ws = host
    et = make_event(db, user, ws, location_json=[{"type": "custom_url", "value": "https://meet.example.com/x"}])
    q = EventQuestion(event_type_id=et.id, type="text", label="What do you want to discuss?", required=True)
    db.add(q)
    db.commit()
    info = client.get("/api/v1/public/event-types/alice/discovery-call").json()
    assert info["questions"][0]["id"] == q.id and info["locations"][0]["value"] is None  # url hidden before booking
    r = client.get(f"/api/v1/public/event-types/alice/discovery-call/availability?from={DAY}&to={DAY}"
                   f"&timezone=America/New_York")
    assert r.status_code == 200
    data = r.json()
    slots = [s for d in data["days"] for s in d["slots"]]
    slot = next(s for s in slots if s["start_at"] == SLOT_ISO)
    assert re.match(r"^\d\d:\d\d E[SD]T$", slot["label"]) and slot["slot_token"]
    assert data["availability_version"]

    body = base_body(slot_token=slot["slot_token"], answers=[{"question_id": q.id, "value": "Pricing"}],
                     utm={"utm_source": "linkedin", "bogus": "x"}, consent={"privacy_notice_version": "2026-10"})
    r = post_booking(client, body)
    assert r.status_code == 201, r.text
    res = r.json()
    assert res["status"] == "confirmed" and res["start_at"] == SLOT_ISO and res["timezone"] == "America/New_York"
    assert res["manage"]["cancel_url"].endswith("/cancel") and res["manage"]["reschedule_url"].endswith("/reschedule")
    assert res["location"]["type"] == "custom_url"
    b = db.get(Booking, res["booking_id"])
    assert b.utm_json == {"utm_source": "linkedin"} and b.invitees[0].consent_json["privacy_notice_version"] == "2026-10"
    # idempotent replay
    r2 = post_booking(client, body)
    assert r2.status_code == 200 and r2.json()["booking_id"] == res["booking_id"]
    # someone else for the same slot -> 409 envelope
    r3 = post_booking(client, base_body(answers=[{"question_id": q.id, "value": "x"}],
                                        invitee={"name": "B", "email": "b@example.com"}), key="key-2")
    assert r3.status_code == 409
    err = r3.json()["error"]
    assert err["code"] == "SLOT_NO_LONGER_AVAILABLE" and err["message"].startswith("This time was just booked") and err["request_id"]
    # slot vanished from availability
    data = client.get(f"/api/v1/public/event-types/alice/discovery-call/availability?from={DAY}&to={DAY}&timezone=UTC").json()
    assert SLOT_ISO not in [s["start_at"] for d in data["days"] for s in d["slots"]]

    # confirmation page
    page = client.get(res["next_url"])
    assert page.status_code == 200 and "You are scheduled" in page.text and "America/New_York" in page.text
    assert "https://meet.example.com/x" in page.text and "calendar.google.com" in page.text
    assert "aal.event_scheduled" in page.text
    ics = client.get(f"/booking/{res['manage_token']}/ics")
    assert ics.status_code == 200 and ics.headers["content-type"].startswith("text/calendar") and "BEGIN:VEVENT" in ics.text
    manage = client.get(f"/booking/{res['manage_token']}")
    assert manage.status_code == 200 and "Reschedule" in manage.text and "Cancel booking" in manage.text


def test_booking_api_validation(client, db, host):
    user, ws = host
    make_event(db, user, ws)
    r = client.post("/api/v1/public/bookings", json=base_body())
    assert r.status_code == 400 and r.json()["error"]["code"] == "VALIDATION_ERROR"  # missing Idempotency-Key
    r = post_booking(client, base_body(website="http://spam"), key="hp")
    assert r.status_code == 422
    r = post_booking(client, base_body(slot_token="forged"), key="tok")
    assert r.status_code == 409 and r.json()["error"]["code"] == "SLOT_TOKEN_INVALID"
    r = post_booking(client, base_body(invitee={"name": "", "email": "nope"}), key="inv")
    assert r.status_code == 422 and "fields" in r.json()["error"]["details"]
    r = post_booking(client, base_body(timezone="Mars/Olympus"), key="tz")
    assert r.status_code == 422


def test_calendar_error_shows_unavailable_state(client, db, host):
    user, ws = host
    make_event(db, user, ws)
    db.add(CalendarConnection(workspace_id=ws.id, user_id=user.id, provider="google", status="error", last_error="token expired"))
    db.commit()
    r = client.get(f"/api/v1/public/event-types/alice/discovery-call/availability?from={DAY}&to={DAY}")
    assert r.status_code == 503 and r.json()["error"]["code"] == "CALENDAR_UNAVAILABLE"
    r = post_booking(client, base_body())
    assert r.status_code == 503
    assert db.scalar(select(func.count(Booking.id))) == 0


def test_cancel_and_reschedule_via_links(client, db, host, monkeypatch):
    user, ws = host
    make_event(db, user, ws)
    res = post_booking(client, base_body()).json()
    token = res["manage_token"]
    assert client.get(f"/booking/{token}/reschedule").status_code == 200
    new_start = tu.iso_z(SLOT_UTC + dt.timedelta(hours=2))
    # failure keeps original (acceptance #33)
    monkeypatch.setattr(cf, "update_calendar_events", lambda *a, **k: (_ for _ in ()).throw(cf.TransientProviderError("x")))
    r = client.post(f"/api/v1/public/bookings/{token}/reschedule", json={"start_at": new_start})
    assert r.status_code == 502 and r.json()["error"]["code"] == "RESCHEDULE_FAILED"
    assert db.get(Booking, res["booking_id"]).status == "confirmed"
    monkeypatch.undo()
    r = client.post(f"/api/v1/public/bookings/{token}/reschedule", json={"start_at": new_start, "timezone": "Asia/Kolkata"})
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["start_at"] == new_start and new["booking_id"] != res["booking_id"] and new["timezone"] == "Asia/Kolkata"
    old_page = client.get(f"/booking/{token}")
    assert "View the updated booking" in old_page.text
    # cancel the new booking through the HTML form
    r = client.get(f"/booking/{new['manage_token']}/cancel")
    assert r.status_code == 200 and "Reason for cancelling" in r.text
    r = client.post(f"/booking/{new['manage_token']}/cancel", data={"reason": "Changed plans"}, follow_redirects=False)
    assert r.status_code == 303
    db.rollback()  # end the test session's read snapshot
    assert db.get(Booking, new["booking_id"]).status == "cancelled"
    assert "cancelled" in client.get(f"/booking/{new['manage_token']}").text


def test_cancel_via_api_and_expired_link(client, db, host):
    user, ws = host
    make_event(db, user, ws)
    res = post_booking(client, base_body()).json()
    r = client.post(f"/api/v1/public/bookings/{res['manage_token']}/cancel", json={"reason": "no"})
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    b = db.get(Booking, res["booking_id"])
    db.refresh(b)
    b.manage_token_expires_at = tu.utcnow() - dt.timedelta(days=1)
    db.commit()
    assert client.get(f"/booking/{res['manage_token']}").status_code == 410
    assert client.post(f"/api/v1/public/bookings/{res['manage_token']}/cancel", json={}).status_code == 410


def test_confirmation_redirect_passes_only_approved_params(client, db, host):
    user, ws = host
    make_event(db, user, ws, confirmation_json={"redirect_url": "https://example.com/thanks?x=1", "pass_params": True})
    res = post_booking(client, base_body(utm={"utm_source": "ads"})).json()
    r = client.get(res["next_url"], follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("https://example.com/thanks?x=1") and f"booking_id={res['booking_id']}" in loc
    assert "utm_source=ads" in loc and "alex" not in loc


def test_paid_event_redirects_to_payment(client, db, host):
    user, ws = host
    make_event(db, user, ws, price_minor=2500, currency="USD")
    res = post_booking(client, base_body()).json()
    assert res["status"] == "pending_payment" and res["next_url"] == f"/pay/{res['manage_token']}"
    page = client.get(f"/booking/{res['manage_token']}/confirmed")
    assert "Payment required" in page.text and "You are scheduled" not in page.text


def test_email_verification_required(client, db, host):
    user, ws = host
    et = make_event(db, user, ws, requires_email_verification=True)
    r = post_booking(client, base_body())
    assert r.status_code == 422 and r.json()["error"]["code"] == "EMAIL_VERIFICATION_REQUIRED"
    r = client.post("/api/v1/public/verify-email", json={"email": "alex@example.com", "event_type_id": et.id})
    assert r.status_code == 200
    msg = db.scalar(select(SentMessage).where(SentMessage.recipient == "alex@example.com"))
    code = re.search(r"(\d{6})", msg.body).group(1)
    wrong = "000000" if code != "000000" else "111111"
    assert post_booking(client, base_body(verification_code=wrong), key="v1").status_code == 422
    r = post_booking(client, base_body(verification_code=code), key="v2")
    assert r.status_code == 201, r.text


def test_team_pages_and_round_robin_booking(client, db, host):
    from app.services.bootstrap import add_user_to_workspace

    user, ws = host
    bob = add_user_to_workspace(db, ws, "bob@example.com", "Bob", timezone="Europe/London")
    team = Team(workspace_id=ws.id, name="Sales Team", slug="sales")
    db.add(team)
    db.commit()
    et = make_event(db, user, ws, slug="demo", name="Product demo", kind="round_robin", team_id=team.id)
    db.add_all([EventTypeHost(event_type_id=et.id, user_id=user.id, priority=1),
                EventTypeHost(event_type_id=et.id, user_id=bob.id, priority=3)])
    db.commit()
    r = client.get("/team/sales")
    assert r.status_code == 200 and "Sales Team" in r.text and "Product demo" in r.text
    r = client.get("/team/sales/demo")
    assert r.status_code == 200 and "Product demo" in r.text and '"owner_slug": "team:sales"' in r.text
    r = client.get(f"/api/v1/public/event-types/team:sales/demo/availability?from={DAY}&to={DAY}&timezone=Europe/London")
    assert r.status_code == 200 and r.json()["days"]
    r = post_booking(client, base_body(owner_slug="team:sales", event_type_slug="demo"))
    assert r.status_code == 201
    assert db.get(Booking, r.json()["booking_id"]).host_id == bob.id
    assert client.get("/team/nope").status_code == 404


def test_group_waitlist_flow(client, db, host):
    user, ws = host
    make_event(db, user, ws, kind="group", max_invitees=1, waitlist_enabled=True)
    assert post_booking(client, base_body()).status_code == 201
    data = client.get(f"/api/v1/public/event-types/alice/discovery-call/availability?from={DAY}&to={DAY}").json()
    full = [s for d in data["days"] for s in d["slots"] if s["start_at"] == SLOT_ISO][0]
    assert full["full"] and full["seats_remaining"] == 0 and not full["slot_token"]
    r = client.post("/api/v1/public/waitlist", json={"owner_slug": "alice", "event_type_slug": "discovery-call",
                                                     "start_at": SLOT_ISO, "name": "W", "email": "w@example.com"})
    assert r.status_code == 201
    assert db.scalar(select(func.count(WaitlistEntry.id))) == 1


def test_unknown_booking_token_404(client):
    assert client.get("/booking/doesnotexist").status_code == 404
    assert client.get("/api/v1/public/bookings/doesnotexist").status_code == 404
