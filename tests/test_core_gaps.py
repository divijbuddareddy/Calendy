"""Scheduling-core gap closure (PRD 6.3, 7.1-7.5, 8.1-8.2, 10.2-10.4, 20.1, 22, 24).

Round-robin balancing window, holiday calendars, team custom rules, stale slot detection,
re-checks inside the host lock, PostgreSQL exclusion constraint, DST labels and the no-JS booking flow.
"""
import datetime as dt
import re
import threading
from urllib.parse import quote_plus

import pytest
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.errors import APIError
from app.models import (
    AvailabilityOverride,
    AvailabilitySchedule,
    Booking,
    BookingHost,
    EventQuestion,
    EventType,
    EventTypeHost,
)
from app.services import availability as av
from app.services import booking as bk
from app.services import timeutil as tu
from app.services.bootstrap import add_user_to_workspace

D = dt.date
T = dt.datetime
NOW = T(2026, 11, 16, 6, 0)  # Monday
SLOT = T(2026, 11, 18, 10, 0)  # Wednesday 10:00 London (GMT)
INV = {"name": "Alex Example", "email": "alex@example.com"}


def make_event(db, user, ws, **kw):
    defaults = dict(workspace_id=ws.id, owner_user_id=user.id, name="Intro call", slug=kw.pop("slug", "intro"),
                    duration=30, min_notice_minutes=60, max_days_ahead=60)
    defaults.update(kw)
    et = EventType(**defaults)
    db.add(et)
    db.commit()
    return et


def book(db, et, start=SLOT, **kw):
    kw.setdefault("timezone", "Europe/London")
    kw.setdefault("now", NOW)
    return bk.create_booking(db, et, start, dict(kw.pop("invitee", INV)), kw.pop("answers", {}), **kw)


def schedule(db, user):
    return db.scalar(select(AvailabilitySchedule).where(AvailabilitySchedule.user_id == user.id))


def add_booking(db, et, host_id, start, minutes=30, exclusive=True, created_at=None, status="confirmed"):
    b = Booking(workspace_id=et.workspace_id, event_type_id=et.id, host_id=host_id, start_at=start,
                end_at=start + dt.timedelta(minutes=minutes), status=status, exclusive=exclusive,
                manage_token=f"t-{start.isoformat()}-{host_id}-{created_at}-{exclusive}-{status}")
    if created_at:
        b.created_at = created_at
    db.add(b)
    db.flush()
    db.add(BookingHost(booking_id=b.id, user_id=host_id))
    db.commit()
    return b


def two_hosts(db, host, kind, bob_tz="Europe/London", **kw):
    user, ws = host
    bob = add_user_to_workspace(db, ws, "bob@example.com", "Bob", timezone=bob_tz)
    db.commit()
    schedule(db, bob).timezone = bob_tz
    et = make_event(db, user, ws, kind=kind, **kw)
    db.add_all([EventTypeHost(event_type_id=et.id, user_id=user.id, priority=1),
                EventTypeHost(event_type_id=et.id, user_id=bob.id, priority=1)])
    db.commit()
    return user, ws, bob, et


# ------------------------------------------------------------------ 10.3 round robin
def test_rr_equal_distribution_counts_only_the_balancing_window(db, host):
    user, ws, bob, et = two_hosts(db, host, "round_robin", rr_strategy="equal")
    # alice: 3 assignments 45 days ago (outside the default 30-day window), bob: 1 recent one
    for i in range(3):
        add_booking(db, et, user.id, T(2026, 10, 1, 10 + i), created_at=NOW - dt.timedelta(days=45))
    add_booking(db, et, bob.id, T(2026, 11, 10, 10), created_at=NOW - dt.timedelta(days=5))
    assert bk.rr_window_counts(db, et, [user.id, bob.id], NOW) == {user.id: 0, bob.id: 1}
    b = book(db, et)
    assert b.host_id == user.id and "last 30 days" in b.assignment_reason
    # a 60-day window (per event type, kept in confirmation_json) sees alice's older assignments
    et.confirmation_json = {"rr_balancing_window_days": 60}
    db.commit()
    assert bk.rr_balancing_window_days(et) == 60
    b2 = book(db, et, start=SLOT + dt.timedelta(hours=2), invitee={"name": "B", "email": "b@example.com"})
    assert b2.host_id == bob.id and "last 60 days" in b2.assignment_reason


def test_rr_cancelled_bookings_do_not_count(db, host):
    user, ws, bob, et = two_hosts(db, host, "round_robin", rr_strategy="equal")
    add_booking(db, et, user.id, T(2026, 11, 10, 10), created_at=NOW - dt.timedelta(days=2), status="cancelled")
    add_booking(db, et, bob.id, T(2026, 11, 11, 10), created_at=NOW - dt.timedelta(days=2))
    assert book(db, et).host_id == user.id


def test_rr_maximise_availability_priority_then_least_recent(db, host):
    user, ws, bob, et = two_hosts(db, host, "round_robin", rr_strategy="availability")
    rows = {h.user_id: h for h in et.hosts}
    rows[user.id].last_assigned_at = NOW - dt.timedelta(hours=1)
    rows[bob.id].last_assigned_at = NOW - dt.timedelta(days=3)
    db.commit()
    assert bk.choose_round_robin_host(db, et, [user.id, bob.id], NOW)[0] == bob.id  # least recently assigned
    rows[user.id].priority = 5
    db.commit()
    host_id, reason = bk.choose_round_robin_host(db, et, [user.id, bob.id], NOW)
    assert host_id == user.id and "priority 5" in reason


# ------------------------------------------------------------------ 7.1 holiday calendars
def test_holiday_country_blocks_public_holidays_and_override_wins(db, host):
    user, ws = host
    sch = schedule(db, user)
    et = make_event(db, user, ws, min_notice_minutes=0)
    xmas = D(2026, 12, 25)  # Friday
    before = av.get_available_slots(db, et, xmas, xmas, "Europe/London", now=NOW)
    assert before
    sch.holiday_country = "GB"
    db.commit()
    assert av.get_available_slots(db, et, xmas, xmas, "Europe/London", now=NOW) == []
    assert av.get_available_slots(db, et, D(2026, 12, 24), D(2026, 12, 24), "Europe/London", now=NOW)
    # Boxing day substitute (28 Dec) also blocked; US holidays don't apply to a GB schedule
    assert av.get_available_slots(db, et, D(2026, 12, 28), D(2026, 12, 28), "Europe/London", now=NOW) == []
    assert av.get_available_slots(db, et, D(2026, 11, 26), D(2026, 11, 26), "Europe/London", now=NOW)
    # an explicit override row for the date wins over the holiday calendar
    db.add(AvailabilityOverride(schedule_id=sch.id, local_date=xmas, intervals_json=[{"start": "10:00", "end": "11:00"}]))
    db.commit()
    got = av.get_available_slots(db, et, xmas, xmas, "Europe/London", now=NOW)
    assert [tu.slot_label(s.start, "Europe/London") for s in got] == ["10:00 GMT", "10:30 GMT"]


# ------------------------------------------------------------------ team events + custom rules
def test_event_custom_rules_apply_to_every_host_in_their_own_timezone(db, host):
    user, ws, bob, et = two_hosts(db, host, "round_robin", bob_tz="America/New_York",
                                  custom_rules_json=[{"weekday": 2, "start": "08:00", "end": "09:00"}], min_notice_minutes=0)
    slots = av.get_available_slots(db, et, D(2026, 11, 18), D(2026, 11, 18), "UTC", now=NOW)
    by_start = {tu.utc_to_local(s.start, "UTC").strftime("%H:%M"): s.host_ids for s in slots}
    # alice: 08:00-09:00 London (= UTC); bob: 08:00-09:00 New York (= 13:00-14:00 UTC)
    assert by_start == {"08:00": [user.id], "08:30": [user.id], "13:00": [bob.id], "13:30": [bob.id]}
    # without custom rules each host falls back to their own default schedule
    et.custom_rules_json = None
    db.commit()
    slots = av.get_available_slots(db, et, D(2026, 11, 18), D(2026, 11, 18), "UTC", now=NOW)
    assert any(s.host_ids == [bob.id] and s.start.hour >= 17 for s in slots)  # bob works until 17:00 EST = 22:00 UTC


# ------------------------------------------------------------------ 7.5 stale slots / config changes
def test_host_edits_availability_after_page_load_is_detected(db, host):
    user, ws = host
    et = make_event(db, user, ws)
    before = av.get_available_slots(db, et, D(2026, 11, 18), D(2026, 11, 18), "Europe/London", now=NOW)
    slot = next(s for s in before if s.start == SLOT)
    assert av.verify_slot_token(slot.token, et.id, SLOT, 30)
    v1 = av.availability_version(before)
    db.add(AvailabilityOverride(schedule_id=schedule(db, user).id, local_date=D(2026, 11, 18), intervals_json=[],
                                kind="day_off"))
    db.commit()
    after = av.get_available_slots(db, et, D(2026, 11, 18), D(2026, 11, 18), "Europe/London", now=NOW)
    assert after == [] and av.availability_version(after) != v1
    with pytest.raises(APIError) as exc:
        book(db, et)
    assert exc.value.code == "SLOT_NO_LONGER_AVAILABLE" and exc.value.status_code == 409
    assert exc.value.details["reason"] == "availability_changed"
    assert "availability changed" in exc.value.message


def test_slot_taken_reports_booked_reason(db, host):
    user, ws = host
    et = make_event(db, user, ws)
    book(db, et)
    with pytest.raises(APIError) as exc:
        book(db, et, invitee={"name": "B", "email": "b@example.com"})
    assert exc.value.details["reason"] == "booked" and exc.value.message == bk.SLOT_MSG


def test_slot_token_is_short_lived_and_bound_to_the_slot(db, host, monkeypatch):
    user, ws = host
    et = make_event(db, user, ws)
    tok = av.slot_token(et.id, SLOT, 30)
    assert av.verify_slot_token(tok, et.id, SLOT, 30)
    assert not av.verify_slot_token(tok, et.id, SLOT + dt.timedelta(minutes=30), 30)
    assert not av.verify_slot_token(tok, et.id, SLOT, 60)
    monkeypatch.setattr(settings, "slot_token_ttl_seconds", -1)
    assert not av.verify_slot_token(tok, et.id, SLOT, 30)


def test_duration_change_after_booking_keeps_snapshot(db, host):
    user, ws = host
    et = make_event(db, user, ws, min_notice_minutes=0, slot_interval=30)
    b = book(db, et)
    et.duration = 60
    et.version = (et.version or 1) + 1
    db.commit()
    db.refresh(b)
    assert b.end_at == SLOT + dt.timedelta(minutes=30)
    assert b.snapshot_json["duration"] == 30 and b.snapshot_json["event_version"] == 1
    assert bk.booking_to_dict(b)["end_at"] == "2026-11-18T10:30:00Z"
    starts = [s.start for s in av.get_available_slots(db, et, D(2026, 11, 18), D(2026, 11, 18), "UTC", now=NOW)]
    assert T(2026, 11, 18, 10, 30) in starts  # 60-min slot right after the unchanged 30-min booking
    assert T(2026, 11, 18, 9, 30) not in starts and SLOT not in starts


def test_expired_unswept_hold_does_not_block_a_new_booking(db, host):
    user, ws = host
    et = make_event(db, user, ws)
    stale = add_booking(db, et, user.id, SLOT, status="held")
    stale.hold_expires_at = NOW - dt.timedelta(minutes=1)
    db.commit()
    b = book(db, et)
    assert b.status == "confirmed"
    db.refresh(stale)
    assert stale.status == "expired"


# ------------------------------------------------------------------ 7.5 re-checks inside the lock
def _inject_after_lock(monkeypatch, fn):
    real = bk.lock_hosts

    def wrapped(db, ids):
        real(db, ids)
        fn(db)

    monkeypatch.setattr(bk, "lock_hosts", wrapped)


def test_collective_host_busy_during_final_confirmation(db, host, monkeypatch):
    user, ws, bob, et = two_hosts(db, host, "collective")
    assert sorted(av.check_slot(db, et, SLOT, now=NOW)) == sorted([user.id, bob.id])  # page showed the slot
    other = make_event(db, bob, ws, slug="bob-other")

    def bob_gets_busy(s):  # committed by a concurrent writer just before our lock was granted
        b = Booking(workspace_id=ws.id, event_type_id=other.id, host_id=bob.id, start_at=SLOT,
                    end_at=SLOT + dt.timedelta(minutes=30), status="confirmed", manage_token="race-bob")
        s.add(b)
        s.flush()

    _inject_after_lock(monkeypatch, bob_gets_busy)
    with pytest.raises(APIError) as exc:
        book(db, et)
    assert exc.value.code == "SLOT_NO_LONGER_AVAILABLE" and exc.value.details["reason"] == "booked"
    assert db.scalar(select(Booking).where(Booking.event_type_id == et.id)) is None


def test_group_reaches_capacity_while_submitting(db, host, monkeypatch):
    user, ws = host
    et = make_event(db, user, ws, kind="group", max_invitees=2)
    add_booking(db, et, user.id, SLOT, exclusive=False)
    slot = next(s for s in av.get_available_slots(db, et, D(2026, 11, 18), D(2026, 11, 18), "UTC", now=NOW)
                if s.start == SLOT)
    assert slot.seats_remaining == 1

    def last_seat_taken(s):
        b = Booking(workspace_id=ws.id, event_type_id=et.id, host_id=user.id, start_at=SLOT,
                    end_at=SLOT + dt.timedelta(minutes=30), status="confirmed", exclusive=False, manage_token="race-seat")
        s.add(b)
        s.flush()

    _inject_after_lock(monkeypatch, last_seat_taken)
    with pytest.raises(APIError) as exc:
        book(db, et)
    assert exc.value.details["reason"] == "capacity" and exc.value.message == bk.FULL_MSG


def test_group_last_seat_concurrent_exactly_one(db, host):
    user, ws = host
    et = make_event(db, user, ws, kind="group", max_invitees=3)
    add_booking(db, et, user.id, SLOT, exclusive=False)
    add_booking(db, et, user.id, SLOT, exclusive=False, created_at=NOW)
    et_id = et.id
    ok, errs = [], []
    barrier = threading.Barrier(4)

    def worker(i):
        s = SessionLocal()
        try:
            e = s.get(EventType, et_id)
            _ = list(e.questions), list(e.hosts)
            barrier.wait()
            ok.append(bk.create_booking(s, e, SLOT, {"name": f"P{i}", "email": f"p{i}@example.com"}, {},
                                        timezone="UTC", now=NOW).id)
        except APIError as exc:
            errs.append(exc.code)
        finally:
            s.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(ok) == 1 and errs == ["SLOT_NO_LONGER_AVAILABLE"] * 3


# ------------------------------------------------------------------ 7.4 PostgreSQL backstop
class _FakeConn:
    def __init__(self, log):
        self.log = log

    def execute(self, stmt, params=None):
        self.log.append(str(stmt))
        return self

    def first(self):
        return None


class _FakeEngine:
    def __init__(self, name):
        self.dialect = type("D", (), {"name": name})()
        self.log = []

    def begin(self):
        engine = self

        class _Ctx:
            def __enter__(self):
                return _FakeConn(engine.log)

            def __exit__(self, *a):
                return False

        return _Ctx()


def test_ensure_postgres_constraints():
    from app.db import engine

    assert bk.ensure_postgres_constraints(engine) is False  # SQLite: no-op
    pg = _FakeEngine("postgresql")
    assert bk.ensure_postgres_constraints(pg) is True
    sql = "\n".join(pg.log)
    assert "CREATE EXTENSION IF NOT EXISTS btree_gist" in sql
    assert "EXCLUDE USING gist" in sql and "tsrange(start_at, end_at) WITH &&" in sql
    assert "status IN ('held', 'pending', 'pending_payment', 'confirmed')" in sql


# ------------------------------------------------------------------ 20.1 fall-back DST labels
def test_fall_back_duplicate_times_have_unambiguous_offset_labels(client, db, host):
    user, ws = host
    sch = schedule(db, user)
    sch.timezone = "America/New_York"
    db.commit()
    et = make_event(db, user, ws, custom_rules_json=[{"weekday": 6, "start": "00:00", "end": "04:00"}],
                    min_notice_minutes=0, max_days_ahead=3650)
    slots = av.get_available_slots(db, et, D(2026, 11, 1), D(2026, 11, 1), "America/New_York", now=T(2026, 10, 20))
    labels = [s.to_dict("America/New_York")["label"] for s in slots]
    assert "01:00 EDT (UTC-04:00)" in labels and "01:00 EST (UTC-05:00)" in labels
    assert "00:30 EDT" in labels  # non-ambiguous times stay short
    dup = [s.to_dict("America/New_York")["label_12h"] for s in slots if "(UTC" in s.to_dict("America/New_York")["label"]]
    assert len(dup) == len(set(dup)) == 4


# ------------------------------------------------------------------ 8.1/8.2 booking page without JavaScript
def _next_weekday(wd, min_days=3):
    d = tu.utcnow().date() + dt.timedelta(days=min_days)
    while d.weekday() != wd:
        d += dt.timedelta(days=1)
    return d


DAY = _next_weekday(2)
SLOT_ISO = tu.iso_z(tu.wall_to_utc(DAY, 10 * 60, "Europe/London"))


def _public_event(db, host, **kw):
    user, ws = host
    defaults = dict(name="Discovery call", slug="discovery-call", min_notice_minutes=60)
    defaults.update(kw)
    return make_event(db, user, ws, **defaults)


def _pick(client, et_path, **params):
    qs = "&".join(f"{k}={quote_plus(v)}" for k, v in params.items())
    return client.get(f"{et_path}?{qs}")


def test_nojs_month_day_and_slot_selection(client, db, host):
    et = _public_event(db, host)
    r = _pick(client, "/alice/discovery-call", tz="Europe/London", month=DAY.strftime("%Y-%m"))
    assert r.status_code == 200
    assert "times available" in r.text and "no times available" in r.text
    assert 'id="cal-grid" role="group"' in r.text and "Please enable JavaScript" not in r.text
    r = _pick(client, "/alice/discovery-call", tz="Europe/London", date=DAY.isoformat())
    assert quote_plus(SLOT_ISO) in r.text and re.search(r"10:00 (GMT|BST)", r.text)
    r = _pick(client, "/alice/discovery-call", tz="America/New_York", date=DAY.isoformat())
    assert re.search(r"0[45]:00 E[SD]T", r.text)
    r = _pick(client, "/alice/discovery-call", tz="Europe/London", date=DAY.isoformat(), slot=SLOT_ISO)
    assert f'action="/book/{et.id}"' in r.text and 'name="slot_token"' in r.text
    assert re.search(r'<div id="step-pick"\s+hidden', r.text) and '"selected_slot": {' in r.text


def _details(client, **extra):
    r = _pick(client, "/alice/discovery-call", tz="Europe/London", date=DAY.isoformat(), slot=SLOT_ISO, **extra)
    token = re.search(r'name="slot_token" id="f-slot-token" value="([^"]+)"', r.text).group(1)
    idem = re.search(r'name="idempotency_key" id="f-idem" value="([^"]+)"', r.text).group(1)
    return {"start_at": SLOT_ISO, "slot_token": token, "tz": "Europe/London", "duration": "30", "idempotency_key": idem}


def test_nojs_form_post_books_and_redirects(client, db, host):
    et = _public_event(db, host)
    q = EventQuestion(event_type_id=et.id, type="checkbox", label="Topics", options_json=["A", "B"], sort_order=1)
    c = EventQuestion(event_type_id=et.id, type="consent", label="I agree", required=True, sort_order=2)
    db.add_all([q, c])
    db.commit()
    form = _details(client, utm_source="news")
    # validation error -> re-rendered form keeps values and shows the message
    bad = client.post(f"/book/{et.id}", data={**form, "email": "pat@example.com", f"q-{q.id}": ["A", "B"]},
                      follow_redirects=False)
    assert bad.status_code == 422
    assert "Name is required" in bad.text and 'value="pat@example.com"' in bad.text
    assert 'aria-invalid="true"' in bad.text
    data = {**form, "name": "Pat", "email": "pat@example.com", f"q-{q.id}": ["A", "B"], f"q-{c.id}": "on",
            "utm_source": "news"}
    r = client.post(f"/book/{et.id}", data=data, follow_redirects=False)
    assert r.status_code == 303, r.text
    b = db.scalar(select(Booking).where(Booking.event_type_id == et.id))
    assert r.headers["location"] == f"/booking/{b.manage_token}/confirmed"
    assert b.status == "confirmed" and b.timezone == "Europe/London" and b.utm_json == {"utm_source": "news"}
    assert {a.question_id: a.answer_json for a in b.answers}[q.id] == ["A", "B"]
    # double submit (same idempotency key) does not double book
    again = client.post(f"/book/{et.id}", data=data, follow_redirects=False)
    assert again.status_code == 303 and again.headers["location"] == r.headers["location"]


def test_nojs_stale_slot_redirects_to_picker_with_message(client, db, host):
    user, ws = host
    et = _public_event(db, host)
    form = _details(client)
    db.add(AvailabilityOverride(schedule_id=schedule(db, user).id, local_date=DAY, intervals_json=[], kind="day_off"))
    db.commit()
    r = client.post(f"/book/{et.id}", data={**form, "name": "Pat", "email": "pat@example.com"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/alice/discovery-call?")
    page = client.get(r.headers["location"])
    assert "availability changed" in page.text
    # expired / forged slot token
    r = client.post(f"/book/{et.id}", data={**form, "slot_token": "nope", "name": "Pat", "email": "pat@example.com"},
                    follow_redirects=False)
    assert r.status_code == 303 and "expired" in client.get(r.headers["location"]).text


def test_nojs_reschedule_post(client, db, host):
    et = _public_event(db, host)
    form = _details(client)
    r = client.post(f"/book/{et.id}", data={**form, "name": "Pat", "email": "pat@example.com"}, follow_redirects=False)
    b = db.scalar(select(Booking).where(Booking.event_type_id == et.id))
    new_iso = tu.iso_z(tu.wall_to_utc(DAY, 11 * 60, "Europe/London"))
    page = client.get(f"/booking/{b.manage_token}/reschedule?tz=Europe/London&date={DAY.isoformat()}&slot={quote_plus(new_iso)}")
    assert page.status_code == 200 and f'action="/booking/{b.manage_token}/reschedule"' in page.text
    token = re.search(r'name="slot_token" id="f-slot-token" value="([^"]+)"', page.text).group(1)
    r = client.post(f"/booking/{b.manage_token}/reschedule", data={"start_at": new_iso, "slot_token": token,
                                                                   "tz": "Europe/London", "reason": "clash"},
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    assert "/confirmed" in r.headers["location"], client.get(r.headers["location"]).text[-3000:]
    db.rollback()  # end the read snapshot
    new = db.scalar(select(Booking).where(Booking.rescheduled_from_id == b.id))
    assert new and new.status == "confirmed" and tu.iso_z(new.start_at) == new_iso
    assert r.headers["location"] == f"/booking/{new.manage_token}/confirmed"
