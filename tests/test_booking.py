"""Booking integrity tests (PRD 7.3/7.4/8.3/10.3/20.3/24)."""
import datetime as dt
import threading

import pytest
from sqlalchemy import func, select

from app.db import SessionLocal
from app.errors import APIError
from app.models import (
    Booking,
    BookingAnswer,
    BookingEvent,
    BookingHold,
    CalendarEvent,
    EventQuestion,
    EventType,
    EventTypeHost,
    OutboxEvent,
    WaitlistEntry,
)
from app.services import booking as bk
from app.services import calendar_facade as cf
from app.services import ics
from app.services.bootstrap import add_user_to_workspace

T = dt.datetime
NOW = T(2026, 11, 16, 6, 0)
SLOT = T(2026, 11, 18, 10, 0)  # Wed 10:00 London (GMT)
INV = {"name": "Alex Example", "email": "alex@example.com"}


def make_event(db, user, ws, **kw):
    defaults = dict(workspace_id=ws.id, owner_user_id=user.id, name="Intro call", slug=kw.pop("slug", "intro"),
                    duration=30, min_notice_minutes=60, max_days_ahead=60)
    defaults.update(kw)
    et = EventType(**defaults)
    db.add(et)
    db.commit()
    return et


def topics(db, topic):
    return [e.payload_json for e in db.scalars(select(OutboxEvent).where(OutboxEvent.topic == topic)).all()]


def book(db, et, start=SLOT, **kw):
    kw.setdefault("timezone", "Europe/London")
    kw.setdefault("now", NOW)
    return bk.create_booking(db, et, start, dict(kw.pop("invitee", INV)), kw.pop("answers", {}), **kw)


def test_create_booking_confirms_and_snapshots(db, host):
    user, ws = host
    et = make_event(db, user, ws, location_json=[{"type": "zoom"}], buffer_after=10)
    q = EventQuestion(event_type_id=et.id, type="text", label="Company", required=True, sort_order=1)
    db.add(q)
    db.commit()
    b = book(db, et, answers={q.id: "Example Ltd"}, utm={"utm_source": "newsletter"},
             invitee={**INV, "guests": ["g@example.com"]})
    assert b.status == "confirmed"
    assert b.end_at == SLOT + dt.timedelta(minutes=30)
    assert b.manage_token and b.manage_token_expires_at == b.end_at + dt.timedelta(days=30)
    assert b.snapshot_json["name"] == "Intro call" and b.snapshot_json["buffer_after"] == 10
    assert b.utm_json == {"utm_source": "newsletter"}
    assert b.invitees[0].guests_json == ["g@example.com"]
    ans = db.scalar(select(BookingAnswer).where(BookingAnswer.booking_id == b.id))
    assert ans.label == "Company" and ans.answer_json == "Example Ltd"
    assert [h.user_id for h in b.hosts] == [user.id]
    assert db.scalar(select(func.count(CalendarEvent.id)).where(CalendarEvent.booking_id == b.id)) == 1
    assert topics(db, "booking.created") == [{"booking_id": b.id, "actor": "invitee"}]
    # host config changes don't rewrite the booked snapshot
    et.name = "Renamed"
    db.commit()
    assert bk.booking_to_dict(b)["event_type"]["name"] == "Intro call"
    d = bk.booking_to_dict(b)
    assert d["start_at"] == "2026-11-18T10:00:00Z" and d["manage"]["cancel_url"].endswith(f"/booking/{b.manage_token}/cancel")


def test_slot_taken_and_validation_errors(db, host):
    user, ws = host
    et = make_event(db, user, ws)
    q = EventQuestion(event_type_id=et.id, type="email", label="Work email", required=True)
    q2 = EventQuestion(event_type_id=et.id, type="select", label="Topic", options_json=["Sales", "Support"])
    q3 = EventQuestion(event_type_id=et.id, type="consent", label="I agree", required=True)
    q4 = EventQuestion(event_type_id=et.id, type="url", label="Site")
    db.add_all([q, q2, q3, q4])
    db.commit()
    with pytest.raises(APIError) as e:
        book(db, et, answers={q2.id: "Sales", q3.id: True})
    assert e.value.code == "VALIDATION_ERROR" and q.id in e.value.details["fields"]
    with pytest.raises(APIError):
        book(db, et, answers={q.id: "a@b.co", q2.id: "Other", q3.id: True})
    with pytest.raises(APIError):
        book(db, et, answers={q.id: "a@b.co", q3.id: False})
    with pytest.raises(APIError):
        book(db, et, answers={q.id: "a@b.co", q3.id: True, q4.id: "notaurl"})
    with pytest.raises(APIError):
        book(db, et, invitee={"name": "", "email": "bad"}, answers={q.id: "a@b.co", q3.id: True})
    ok = {q.id: "a@b.co", q3.id: True, q4.id: "https://example.com"}
    book(db, et, answers=ok)
    with pytest.raises(APIError) as e:
        book(db, et, answers=ok, invitee={"name": "Other", "email": "o@example.com"})
    assert e.value.code == "SLOT_NO_LONGER_AVAILABLE" and e.value.status_code == 409
    with pytest.raises(APIError) as e:
        book(db, et, start=T(2026, 11, 21, 10, 0), answers=ok)  # Saturday
    assert e.value.code == "SLOT_NO_LONGER_AVAILABLE"


def test_idempotency(db, host):
    user, ws = host
    et = make_event(db, user, ws)
    a = book(db, et, idempotency_key="k1")
    b = book(db, et, idempotency_key="k1")
    assert a.id == b.id
    assert db.scalar(select(func.count(Booking.id))) == 1
    with pytest.raises(APIError) as e:
        book(db, et, start=SLOT + dt.timedelta(hours=1), idempotency_key="k1")
    assert e.value.code == "IDEMPOTENCY_CONFLICT"


def test_concurrent_final_slot_exactly_one_succeeds(db, host):
    user, ws = host
    et = make_event(db, user, ws, custom_rules_json=[{"weekday": 2, "start": "10:00", "end": "10:30"}])
    et_id = et.id
    results, errors = [], []
    barrier = threading.Barrier(6)

    def worker(i):
        s = SessionLocal()
        try:
            e = s.get(EventType, et_id)
            _ = list(e.questions), list(e.hosts)
            barrier.wait()
            b = bk.create_booking(s, e, SLOT, {"name": f"P{i}", "email": f"p{i}@example.com"}, {},
                                  timezone="UTC", now=NOW)
            results.append(b.id)
        except APIError as exc:
            errors.append(exc.code)
        except Exception as exc:  # pragma: no cover - surfaced in assertion
            errors.append(repr(exc))
        finally:
            s.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(results) == 1, (results, errors)
    assert errors == ["SLOT_NO_LONGER_AVAILABLE"] * 5
    db.expire_all()
    assert db.scalar(select(func.count(Booking.id)).where(Booking.status == "confirmed")) == 1


def test_round_robin_availability_strategy(db, host):
    user, ws = host
    bob = add_user_to_workspace(db, ws, "bob@example.com", "Bob", timezone="Europe/London")
    db.commit()
    et = make_event(db, user, ws, kind="round_robin", rr_strategy="availability")
    db.add_all([EventTypeHost(event_type_id=et.id, user_id=user.id, priority=1),
                EventTypeHost(event_type_id=et.id, user_id=bob.id, priority=5)])
    db.commit()
    b1 = book(db, et)
    assert b1.host_id == bob.id and "priority" in b1.assignment_reason
    b2 = book(db, et, invitee={"name": "B", "email": "b@example.com"})  # bob busy at 10:00 -> alice
    assert b2.host_id == user.id
    eth = db.scalar(select(EventTypeHost).where(EventTypeHost.user_id == bob.id))
    assert eth.assignment_count == 1 and eth.last_assigned_at == NOW
    # equal priority -> least recently assigned
    db.scalar(select(EventTypeHost).where(EventTypeHost.user_id == user.id)).priority = 5
    db.commit()
    b3 = book(db, et, start=SLOT + dt.timedelta(hours=2), now=NOW + dt.timedelta(minutes=5))
    assert b3.host_id in (user.id, bob.id)


def test_round_robin_equal_distribution(db, host):
    """Equal distribution balances by bookings inside the balancing window, not lifetime counters."""
    user, ws = host
    bob = add_user_to_workspace(db, ws, "bob@example.com", "Bob", timezone="Europe/London")
    db.commit()
    et = make_event(db, user, ws, kind="round_robin", rr_strategy="equal")
    # stale lifetime counters must not matter
    db.add_all([EventTypeHost(event_type_id=et.id, user_id=user.id, priority=9, assignment_count=0),
                EventTypeHost(event_type_id=et.id, user_id=bob.id, priority=1, assignment_count=50)])
    db.commit()
    # alice already has 2 assignments in the window, bob none
    for i in range(2):
        db.add(Booking(workspace_id=ws.id, event_type_id=et.id, host_id=user.id, start_at=SLOT + dt.timedelta(days=1, hours=i),
                       end_at=SLOT + dt.timedelta(days=1, hours=i, minutes=30), status="confirmed", manage_token=f"rr{i}"))
    db.commit()
    hosts = [book(db, et, start=SLOT + dt.timedelta(hours=i), invitee={"name": "x", "email": f"x{i}@e.com"}).host_id
             for i in range(4)]
    assert hosts[:2] == [bob.id, bob.id]  # bob catches up to 2 first
    assert sorted(hosts[2:]) == sorted([user.id, bob.id])
    assert "last 30 days" in db.scalar(select(Booking).where(Booking.host_id == bob.id)).assignment_reason
    counts = {h.user_id: h.assignment_count for h in db.scalars(select(EventTypeHost)).all()}
    assert counts[bob.id] == 53  # lifetime distribution counter is still persisted


def test_reassign_booking_with_audit(db, host):
    user, ws = host
    bob = add_user_to_workspace(db, ws, "bob@example.com", "Bob", timezone="Europe/London")
    db.commit()
    et = make_event(db, user, ws, kind="round_robin")
    db.add_all([EventTypeHost(event_type_id=et.id, user_id=user.id, priority=5),
                EventTypeHost(event_type_id=et.id, user_id=bob.id, priority=1)])
    db.commit()
    b = book(db, et)
    assert b.host_id == user.id
    bk.reassign_booking(db, b, bob.id, f"user:{user.id}", "vacation")
    assert b.host_id == bob.id and [h.user_id for h in b.hosts] == [bob.id]
    assert "vacation" in b.assignment_reason
    assert db.scalar(select(BookingEvent).where(BookingEvent.booking_id == b.id, BookingEvent.event_type == "reassigned"))


def test_collective_booking_includes_all_hosts(db, host):
    user, ws = host
    bob = add_user_to_workspace(db, ws, "bob@example.com", "Bob", timezone="Europe/London")
    db.commit()
    et = make_event(db, user, ws, kind="collective")
    db.add_all([EventTypeHost(event_type_id=et.id, user_id=user.id), EventTypeHost(event_type_id=et.id, user_id=bob.id)])
    db.commit()
    b = book(db, et)
    assert sorted(h.user_id for h in b.hosts) == sorted([user.id, bob.id])
    # bob now busy at that time for any other event
    other = make_event(db, bob, ws, slug="bob1")
    with pytest.raises(APIError):
        book(db, other, invitee={"name": "Z", "email": "z@example.com"})


def test_cancel_policy_and_host_approval(db, host):
    user, ws = host
    et = make_event(db, user, ws, cancellation_policy_json={"min_notice_minutes": 24 * 60, "require_reason": True})
    b = book(db, et)
    with pytest.raises(APIError) as e:
        bk.cancel_booking(db, b, "invitee", None, now=NOW)
    assert e.value.code == "VALIDATION_ERROR"
    with pytest.raises(APIError) as e:
        bk.cancel_booking(db, b, "invitee", "sick", now=SLOT - dt.timedelta(hours=2))
    assert e.value.code == "POLICY_WINDOW_PASSED"
    bk.cancel_booking(db, b, "invitee", "sick", now=NOW)
    assert b.status == "cancelled" and b.cancelled_by == "invitee" and b.cancel_reason == "sick"
    assert topics(db, "booking.cancelled")[0]["booking_id"] == b.id
    assert db.scalar(select(CalendarEvent).where(CalendarEvent.booking_id == b.id)).sync_status == "deleted"
    # slot is free again
    b2 = book(db, et, invitee={"name": "N", "email": "n@example.com"})
    # host can cancel inside the window
    bk.cancel_booking(db, b2, f"user:{user.id}", None, now=SLOT - dt.timedelta(minutes=5))
    assert b2.cancelled_by == "host"

    et2 = make_event(db, user, ws, slug="approval", cancellation_policy_json={"host_approval": True})
    b3 = book(db, et2, start=SLOT + dt.timedelta(hours=3))
    bk.cancel_booking(db, b3, "invitee", "conflict", now=NOW)
    assert b3.status == "confirmed" and b3.cancellation_requested_at
    assert topics(db, "booking.cancellation_requested")[0]["booking_id"] == b3.id
    bk.decline_cancellation(db, b3, f"user:{user.id}")
    assert b3.cancellation_requested_at is None
    bk.cancel_booking(db, b3, "invitee", "again", now=NOW)
    bk.approve_cancellation(db, b3, f"user:{user.id}", now=NOW)
    assert b3.status == "cancelled"

    et3 = make_event(db, user, ws, slug="nocancel", cancellation_policy_json={"allow_cancel": False})
    b4 = book(db, et3, start=SLOT + dt.timedelta(hours=4))
    with pytest.raises(APIError) as e:
        bk.cancel_booking(db, b4, "invitee", now=NOW)
    assert e.value.status_code == 403


def test_reschedule_success(db, host):
    user, ws = host
    et = make_event(db, user, ws)
    old = book(db, et)
    new = bk.reschedule_booking(db, old, SLOT + dt.timedelta(days=1), "invitee", "moved", now=NOW)
    assert new.id != old.id and new.status == "confirmed" and new.rescheduled_from_id == old.id
    assert old.status == "rescheduled" and old.rescheduled_to_id == new.id
    assert new.invitees[0].email == INV["email"]
    assert new.snapshot_json["_ics"]["uid"] == old.snapshot_json["_ics"]["uid"]
    assert new.snapshot_json["_ics"]["seq"] == 1
    ev = topics(db, "booking.rescheduled")[0]
    assert ev["booking_id"] == new.id and ev["old_booking_id"] == old.id
    # old slot free again
    book(db, et, invitee={"name": "Q", "email": "q@example.com"})


def test_reschedule_failure_preserves_original(db, host, monkeypatch):
    user, ws = host
    et = make_event(db, user, ws)
    old = book(db, et)

    def boom(db_, booking, previous=None):
        raise cf.TransientProviderError("provider down")

    monkeypatch.setattr(cf, "update_calendar_events", boom)
    with pytest.raises(APIError) as e:
        bk.reschedule_booking(db, old, SLOT + dt.timedelta(days=1), "invitee", now=NOW)
    assert e.value.code == "RESCHEDULE_FAILED"
    db.expire_all()
    assert db.get(Booking, old.id).status == "confirmed"
    assert db.scalar(select(func.count(Booking.id))) == 1
    assert topics(db, "booking.rescheduled") == []
    assert db.scalar(select(BookingEvent).where(BookingEvent.event_type == "reschedule_failed"))
    # target slot not reserved by the failed attempt
    book(db, et, start=SLOT + dt.timedelta(days=1), invitee={"name": "Q", "email": "q@example.com"})


def test_reschedule_to_taken_slot_rejected(db, host):
    user, ws = host
    et = make_event(db, user, ws)
    old = book(db, et)
    book(db, et, start=SLOT + dt.timedelta(hours=1), invitee={"name": "Q", "email": "q@example.com"})
    with pytest.raises(APIError) as e:
        bk.reschedule_booking(db, old, SLOT + dt.timedelta(hours=1), "invitee", now=NOW)
    assert e.value.code == "SLOT_NO_LONGER_AVAILABLE"
    assert db.get(Booking, old.id).status == "confirmed"


def test_transient_provider_error_pending_then_retry(db, host, monkeypatch):
    user, ws = host
    et = make_event(db, user, ws)
    real = cf.create_calendar_events

    def flaky(db_, booking):
        raise cf.TransientProviderError("timeout")

    monkeypatch.setattr(cf, "create_calendar_events", flaky)
    b = book(db, et)
    assert b.status == "pending"
    assert topics(db, "booking.created") == []
    # slot stays reserved while pending
    with pytest.raises(APIError):
        book(db, et, invitee={"name": "Q", "email": "q@example.com"})
    monkeypatch.setattr(cf, "create_calendar_events", real)
    bk.retry_pending_calendar(db, now=NOW + dt.timedelta(minutes=5))
    db.refresh(b)
    assert b.status == "confirmed"
    assert len(topics(db, "booking.created")) == 1


def test_permanent_provider_error_falls_back(db, host, monkeypatch):
    user, ws = host
    et = make_event(db, user, ws, location_json=[{"type": "google_meet"}])

    def perm(db_, booking):
        raise cf.PermanentProviderError("conference creation failed")

    monkeypatch.setattr(cf, "create_calendar_events", perm)
    b = book(db, et)
    assert b.status == "confirmed"
    assert b.location_json["host_action_required"] and b.location_json["type"] == "host_provides"
    assert db.scalar(select(BookingEvent).where(BookingEvent.event_type == "host_action_required"))


def test_paid_booking_pending_payment_and_expiry(db, host):
    user, ws = host
    et = make_event(db, user, ws, price_minor=5000, currency="GBP")
    b = book(db, et)
    assert b.status == "pending_payment" and b.payment_status == "pending"
    assert b.hold_expires_at == NOW + dt.timedelta(minutes=15)
    assert topics(db, "booking.created") == []
    assert db.scalar(select(func.count(CalendarEvent.id))) == 0
    # finalize after payment
    bk.finalize_booking(db, b, now=NOW + dt.timedelta(minutes=2))
    assert b.status == "confirmed" and len(topics(db, "booking.created")) == 1
    b2 = book(db, et, start=SLOT + dt.timedelta(hours=1), invitee={"name": "Q", "email": "q@example.com"})
    assert bk.expire_holds(db, now=NOW + dt.timedelta(minutes=16)) == 1
    db.refresh(b2)
    assert b2.status == "expired"
    # expired hold frees the slot
    book(db, et, start=SLOT + dt.timedelta(hours=1), invitee={"name": "R", "email": "r@example.com"},
         now=NOW + dt.timedelta(minutes=17))


def test_fail_pending_booking_releases(db, host):
    user, ws = host
    et = make_event(db, user, ws, price_minor=100, currency="USD")
    b = book(db, et)
    db.add(BookingHold(workspace_id=ws.id, host_id=user.id, booking_id=b.id, start_at=b.start_at, end_at=b.end_at,
                       expires_at=NOW + dt.timedelta(minutes=10)))
    db.commit()
    bk.fail_pending_booking(db, b, "payment failed")
    assert b.status == "failed"
    assert db.scalar(select(BookingHold)).released


def test_single_use_link(db, host):
    user, ws = host
    et = make_event(db, user, ws, single_use=True)
    book(db, et)
    assert et.used_at is not None
    with pytest.raises(APIError) as e:
        book(db, et, start=SLOT + dt.timedelta(hours=1), invitee={"name": "Q", "email": "q@example.com"})
    assert e.value.code == "EVENT_UNAVAILABLE"


def test_paused_event_rejects_booking(db, host):
    user, ws = host
    et = make_event(db, user, ws, status="paused")
    with pytest.raises(APIError) as e:
        book(db, et)
    assert e.value.code == "EVENT_UNAVAILABLE"


def test_group_seats_cancel_releases_and_notifies_waitlist(db, host):
    user, ws = host
    et = make_event(db, user, ws, kind="group", max_invitees=2, waitlist_enabled=True)
    a = book(db, et, invitee={"name": "A", "email": "a@example.com"})
    book(db, et, invitee={"name": "B", "email": "b@example.com"})
    with pytest.raises(APIError):
        book(db, et, invitee={"name": "C", "email": "c@example.com"})
    db.add(WaitlistEntry(workspace_id=ws.id, event_type_id=et.id, start_at=SLOT, name="C", email="c@example.com"))
    db.commit()
    bk.cancel_booking(db, a, "invitee", now=NOW)
    ev = topics(db, "waitlist.spot_open")
    assert ev and ev[0]["event_type_id"] == et.id
    c = book(db, et, invitee={"name": "C", "email": "c@example.com"})
    assert c.status == "confirmed" and not c.exclusive


def test_no_show(db, host):
    user, ws = host
    et = make_event(db, user, ws)
    b = book(db, et)
    bk.mark_no_show(db, b, f"user:{user.id}")
    assert b.no_show and topics(db, "booking.no_show")[0]["booking_id"] == b.id
    bk.mark_no_show(db, b, f"user:{user.id}", False)
    assert not b.no_show


def test_ics_and_calendar_links(db, host):
    user, ws = host
    et = make_event(db, user, ws, location_json=[{"type": "in_person", "value": "1 Main St, London"}])
    b = book(db, et)
    text = ics.build_ics(b)
    assert "BEGIN:VCALENDAR" in text and "METHOD:REQUEST" in text and "DTSTART:20261118T100000Z" in text
    assert f"UID:{b.id}@aal-scheduler" in text and "SEQUENCE:0" in text
    assert "1 Main St\\, London" in text and "\r\n" in text
    cancel = ics.build_ics(b, method="CANCEL")
    assert "METHOD:CANCEL" in cancel and "SEQUENCE:1" in cancel and "STATUS:CANCELLED" in cancel
    url = ics.google_calendar_url(b)
    assert url.startswith("https://calendar.google.com/calendar/render?") and "20261118T100000Z" in url
