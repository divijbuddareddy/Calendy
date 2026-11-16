"""Availability engine tests (PRD 7.1/7.2/20.1/20.2/24.2)."""
import datetime as dt

import pytest
from sqlalchemy import select

from app.models import (
    AvailabilityOverride,
    AvailabilitySchedule,
    Booking,
    BookingHost,
    CalendarConnection,
    ConnectedCalendar,
    EventType,
    EventTypeHost,
    ExternalCalendarEvent,
)
from app.services import availability as av
from app.services import timeutil as tu
from app.services.bootstrap import add_user_to_workspace
from app.services.calendar_facade import CalendarUnavailable

D = dt.date
T = dt.datetime
NOW = T(2026, 11, 16, 6, 0)  # Monday 06:00 UTC (London = GMT)


def make_event(db, user, ws, **kw):
    defaults = dict(workspace_id=ws.id, owner_user_id=user.id, name="Intro call", slug=kw.pop("slug", "intro"),
                    duration=30, min_notice_minutes=240, max_days_ahead=60)
    defaults.update(kw)
    et = EventType(**defaults)
    db.add(et)
    db.commit()
    return et


def add_booking(db, et, host_id, start, minutes=30, status="confirmed", exclusive=True, **snap):
    b = Booking(workspace_id=et.workspace_id, event_type_id=et.id, host_id=host_id, start_at=start,
                end_at=start + dt.timedelta(minutes=minutes), status=status, exclusive=exclusive,
                manage_token=f"tok-{start.isoformat()}-{host_id}-{status}-{id(snap)}", snapshot_json=snap or {})
    db.add(b)
    db.flush()
    db.add(BookingHost(booking_id=b.id, user_id=host_id))
    db.commit()
    return b


def schedule(db, user):
    return db.scalar(select(AvailabilitySchedule).where(AvailabilitySchedule.user_id == user.id))


def starts(slots, tz="Europe/London"):
    return [tu.utc_to_local(s.start, tz).strftime("%a %H:%M") for s in slots]


def test_weekly_rules_min_notice_and_weekend(db, host):
    user, ws = host
    et = make_event(db, user, ws)
    slots = av.get_available_slots(db, et, D(2026, 11, 16), D(2026, 11, 22), "Europe/London", now=NOW)
    by_day = {}
    for s in slots:
        by_day.setdefault(tu.local_date(s.start, "Europe/London"), []).append(s)
    # Monday: min notice 4h from 06:00 -> first slot 10:00
    assert starts(by_day[D(2026, 11, 16)])[0] == "Mon 10:00"
    assert len(by_day[D(2026, 11, 16)]) == 14
    # Tuesday - Friday: 09:00..16:30 = 16 slots
    for d in range(17, 21):
        assert len(by_day[D(2026, 11, d)]) == 16
    assert D(2026, 11, 21) not in by_day and D(2026, 11, 22) not in by_day
    assert all(s.host_ids == [user.id] for s in slots)
    assert all(s.token for s in slots)
    assert av.get_available_days(db, et, D(2026, 11, 16), D(2026, 11, 22), "Europe/London", now=NOW) == [
        D(2026, 11, d) for d in range(16, 21)]


def test_all_weekdays_multiple_ranges_and_interval(db, host):
    user, ws = host
    rules = [{"weekday": wd, "start": "08:00", "end": "09:00"} for wd in range(7)]
    rules += [{"weekday": 2, "start": "13:00", "end": "14:00"}]
    et = make_event(db, user, ws, custom_rules_json=rules, slot_interval=15, min_notice_minutes=0)
    slots = av.get_available_slots(db, et, D(2026, 11, 16), D(2026, 11, 22), "Europe/London", now=NOW)
    days = {tu.local_date(s.start, "Europe/London") for s in slots}
    assert len(days) == 7  # every weekday incl. weekend
    wed = [s for s in slots if tu.local_date(s.start, "Europe/London") == D(2026, 11, 18)]
    assert starts(wed) == ["Wed 08:00", "Wed 08:15", "Wed 08:30", "Wed 13:00", "Wed 13:15", "Wed 13:30"]


def test_date_overrides_holidays_and_custom_hours(db, host):
    user, ws = host
    sch = schedule(db, user)
    db.add(AvailabilityOverride(schedule_id=sch.id, local_date=D(2026, 11, 17), intervals_json=[], kind="day_off"))
    db.add(AvailabilityOverride(schedule_id=sch.id, local_date=D(2026, 11, 18), kind="override",
                                intervals_json=[{"start": "12:00", "end": "13:00"}, {"start": "15:00", "end": "15:30"}]))
    db.add(AvailabilityOverride(schedule_id=sch.id, local_date=D(2026, 11, 19), intervals_json=[], kind="holiday", reason="Holiday"))
    db.add(AvailabilityOverride(schedule_id=sch.id, local_date=D(2026, 11, 21), kind="override",
                                intervals_json=[{"start": "10:00", "end": "11:00"}]))  # Saturday opened
    db.commit()
    et = make_event(db, user, ws)
    slots = av.get_available_slots(db, et, D(2026, 11, 17), D(2026, 11, 21), "Europe/London", now=NOW)
    got = starts(slots)
    assert not any(s.startswith("Tue") or s.startswith("Thu") for s in got)
    assert [s for s in got if s.startswith("Wed")] == ["Wed 12:00", "Wed 12:30", "Wed 15:00"]
    assert [s for s in got if s.startswith("Sat")] == ["Sat 10:00", "Sat 10:30"]


def test_buffers_around_bookings_and_calendar(db, host):
    user, ws = host
    et = make_event(db, user, ws, buffer_before=0, buffer_after=15)
    add_booking(db, et, user.id, T(2026, 11, 18, 10, 0), buffer_after=15)
    slots = starts(av.get_available_slots(db, et, D(2026, 11, 18), D(2026, 11, 18), "Europe/London", now=NOW))
    assert "Wed 10:00" not in slots
    assert "Wed 09:30" not in slots  # new slot's after-buffer would overlap 10:00
    assert "Wed 10:30" not in slots  # existing booking's after-buffer
    assert "Wed 09:00" in slots and "Wed 11:00" in slots


def _mock_calendar(db, user, ws, status="connected"):
    conn = CalendarConnection(workspace_id=ws.id, user_id=user.id, provider="mock", status=status)
    db.add(conn)
    db.flush()
    cal = ConnectedCalendar(connection_id=conn.id, provider_calendar_id="primary", display_name="Main", is_conflict_calendar=True)
    db.add(cal)
    db.flush()
    return conn, cal


def test_calendar_busy_and_freebusy_exceptions(db, host):
    user, ws = host
    _, cal = _mock_calendar(db, user, ws)
    db.add(ExternalCalendarEvent(calendar_id=cal.id, external_id="x1", title="Focus time",
                                 start_at=T(2026, 11, 18, 14, 0), end_at=T(2026, 11, 18, 15, 0)))
    db.add(ExternalCalendarEvent(calendar_id=cal.id, external_id="x2", title="Lunch", transparent=True,
                                 start_at=T(2026, 11, 18, 12, 0), end_at=T(2026, 11, 18, 13, 0)))
    db.commit()
    et = make_event(db, user, ws)
    got = starts(av.get_available_slots(db, et, D(2026, 11, 18), D(2026, 11, 18), "Europe/London", now=NOW))
    assert "Wed 14:00" not in got and "Wed 14:30" not in got
    assert "Wed 12:00" in got  # transparent event does not block
    et.freebusy_exceptions_json = ["focus time"]
    db.commit()
    got = starts(av.get_available_slots(db, et, D(2026, 11, 18), D(2026, 11, 18), "Europe/London", now=NOW))
    assert "Wed 14:00" in got


def test_calendar_in_error_never_shows_unverified_availability(db, host):
    user, ws = host
    _mock_calendar(db, user, ws, status="error")
    db.commit()
    et = make_event(db, user, ws)
    with pytest.raises(CalendarUnavailable):
        av.get_available_slots(db, et, D(2026, 11, 18), D(2026, 11, 18), "Europe/London", now=NOW)


def test_event_limits_and_host_wide_limits(db, host):
    user, ws = host
    et = make_event(db, user, ws, daily_limit=1)
    other = make_event(db, user, ws, slug="other")
    add_booking(db, et, user.id, T(2026, 11, 18, 10, 0))
    days = av.get_available_days(db, et, D(2026, 11, 17), D(2026, 11, 19), "Europe/London", now=NOW)
    assert D(2026, 11, 18) not in days and D(2026, 11, 17) in days
    # other event type unaffected by event limit
    assert D(2026, 11, 18) in av.get_available_days(db, other, D(2026, 11, 18), D(2026, 11, 18), "Europe/London", now=NOW)
    # weekly event limit
    et.daily_limit, et.weekly_limit = None, 1
    db.commit()
    assert av.get_available_days(db, et, D(2026, 11, 16), D(2026, 11, 22), "Europe/London", now=NOW) == []
    assert av.get_available_days(db, et, D(2026, 11, 23), D(2026, 11, 23), "Europe/London", now=NOW) == [D(2026, 11, 23)]
    # host-wide daily limit across all event types
    user.working_hours_json = {"daily_limit": 1}
    db.commit()
    assert D(2026, 11, 18) not in av.get_available_days(db, other, D(2026, 11, 17), D(2026, 11, 19), "Europe/London", now=NOW)
    user.working_hours_json = {"monthly_limit": 1}
    db.commit()
    assert av.get_available_days(db, other, D(2026, 11, 17), D(2026, 11, 30), "Europe/London", now=NOW) == []


def test_min_notice_and_horizon(db, host):
    user, ws = host
    et = make_event(db, user, ws, min_notice_minutes=24 * 60, max_days_ahead=3)
    days = av.get_available_days(db, et, D(2026, 11, 16), D(2026, 11, 30), "Europe/London", now=NOW)
    # now Mon 06:00 + 24h -> Tue 06:00 earliest; horizon Thu 06:00 -> Thu has no slots after 06:00? 09:00 > 06:00 => excluded
    assert days == [D(2026, 11, 17), D(2026, 11, 18)]


def _dst_event(db, user, ws, tz, weekday):
    sch = schedule(db, user)
    sch.timezone = tz
    db.commit()
    return make_event(db, user, ws, custom_rules_json=[{"weekday": weekday, "start": "00:00", "end": "04:00"}],
                      min_notice_minutes=0, max_days_ahead=365)


def test_dst_spring_forward_london(db, host):
    user, ws = host
    et = _dst_event(db, user, ws, "Europe/London", 6)
    slots = av.get_available_slots(db, et, D(2027, 3, 28), D(2027, 3, 28), "Europe/London", now=T(2027, 3, 20))
    labels = [tu.slot_label(s.start, "Europe/London") for s in slots]
    assert labels == ["00:00 GMT", "00:30 GMT", "02:00 BST", "02:30 BST", "03:00 BST", "03:30 BST"]
    assert len({s.start for s in slots}) == len(slots)


def test_dst_fall_back_london_produces_distinct_instants(db, host):
    user, ws = host
    et = _dst_event(db, user, ws, "Europe/London", 6)
    slots = av.get_available_slots(db, et, D(2026, 10, 25), D(2026, 10, 25), "Europe/London", now=T(2026, 10, 20))
    labels = [tu.slot_label(s.start, "Europe/London") for s in slots]
    assert "01:00 BST (UTC+01:00)" in labels and "01:00 GMT (UTC+00:00)" in labels
    assert len(slots) == 10  # 5 elapsed hours
    assert len({s.start for s in slots}) == 10


def test_dst_new_york_both_transitions(db, host):
    user, ws = host
    et = _dst_event(db, user, ws, "America/New_York", 6)
    spring = av.get_available_slots(db, et, D(2027, 3, 14), D(2027, 3, 14), "America/New_York", now=T(2027, 3, 1))
    assert [tu.slot_label(s.start, "America/New_York") for s in spring] == [
        "00:00 EST", "00:30 EST", "01:00 EST", "01:30 EST", "03:00 EDT", "03:30 EDT"]
    fall = av.get_available_slots(db, et, D(2026, 11, 1), D(2026, 11, 1), "America/New_York", now=T(2026, 10, 20))
    fl = [tu.slot_label(s.start, "America/New_York") for s in fall]
    assert "01:30 EDT (UTC-04:00)" in fl and "01:30 EST (UTC-05:00)" in fl
    assert len(fall) == 10


def test_kolkata_host_and_invitee_in_other_zone(db, host):
    user, ws = host
    sch = schedule(db, user)
    sch.timezone = "Asia/Kolkata"
    db.commit()
    et = make_event(db, user, ws, min_notice_minutes=0)
    slots = av.get_available_slots(db, et, D(2026, 11, 18), D(2026, 11, 18), "Asia/Kolkata", now=NOW)
    assert tu.slot_label(slots[0].start, "Asia/Kolkata") == "09:00 IST"
    assert slots[0].start == T(2026, 11, 18, 3, 30)
    # same slots seen from New York (previous evening local time for the first slot)
    ny = av.get_available_slots(db, et, D(2026, 11, 17), D(2026, 11, 18), "America/New_York", now=NOW)
    assert T(2026, 11, 18, 3, 30) in [s.start for s in ny]
    assert tu.slot_label(T(2026, 11, 18, 3, 30), "America/New_York") == "22:30 EST"


def test_group_capacity_and_full_slots(db, host):
    user, ws = host
    et = make_event(db, user, ws, kind="group", max_invitees=2, waitlist_enabled=True)
    s = T(2026, 11, 18, 10, 0)
    add_booking(db, et, user.id, s, exclusive=False)
    slot = [x for x in av.get_available_slots(db, et, D(2026, 11, 18), D(2026, 11, 18), "UTC", now=NOW) if x.start == s][0]
    assert slot.seats_remaining == 1
    add_booking(db, et, user.id, s, exclusive=False, status="confirmed", note="2")
    assert s not in [x.start for x in av.get_available_slots(db, et, D(2026, 11, 18), D(2026, 11, 18), "UTC", now=NOW)]
    full = [x for x in av.get_available_slots(db, et, D(2026, 11, 18), D(2026, 11, 18), "UTC", now=NOW, include_full=True)
            if x.start == s][0]
    assert full.full and full.seats_remaining == 0
    assert av.check_slot(db, et, s, now=NOW) == []


def test_collective_intersects_required_hosts(db, host):
    user, ws = host
    bob = add_user_to_workspace(db, ws, "bob@example.com", "Bob", timezone="Europe/London")
    db.commit()
    bsch = schedule(db, bob)
    from app.models import AvailabilityRule
    for r in db.scalars(select(AvailabilityRule).where(AvailabilityRule.schedule_id == bsch.id)).all():
        r.start_local = "13:00"
    db.commit()
    et = make_event(db, user, ws, kind="collective")
    db.add_all([EventTypeHost(event_type_id=et.id, user_id=user.id, required=True),
                EventTypeHost(event_type_id=et.id, user_id=bob.id, required=True)])
    db.commit()
    add_booking(db, make_event(db, bob, ws, slug="bobs"), bob.id, T(2026, 11, 18, 14, 0))
    got = starts(av.get_available_slots(db, et, D(2026, 11, 18), D(2026, 11, 18), "Europe/London", now=NOW))
    assert got[0] == "Wed 13:00" and "Wed 12:30" not in got
    assert "Wed 14:00" not in got
    s = av.get_available_slots(db, et, D(2026, 11, 18), D(2026, 11, 18), "Europe/London", now=NOW)[0]
    assert sorted(s.host_ids) == sorted([user.id, bob.id])


def test_round_robin_eligible_hosts_and_daily_capacity(db, host):
    user, ws = host
    bob = add_user_to_workspace(db, ws, "bob@example.com", "Bob", timezone="Europe/London")
    db.commit()
    et = make_event(db, user, ws, kind="round_robin")
    db.add_all([EventTypeHost(event_type_id=et.id, user_id=user.id, priority=2),
                EventTypeHost(event_type_id=et.id, user_id=bob.id, priority=1, daily_capacity=1)])
    db.commit()
    s = T(2026, 11, 18, 10, 0)
    assert sorted(av.check_slot(db, et, s, now=NOW)) == sorted([user.id, bob.id])
    add_booking(db, et, bob.id, T(2026, 11, 18, 15, 0))  # bob reaches daily capacity
    assert av.check_slot(db, et, s, now=NOW) == [user.id]
    add_booking(db, make_event(db, user, ws, slug="x"), user.id, s)
    assert av.check_slot(db, et, s, now=NOW) == []
    # inactive host never eligible
    other_day = T(2026, 11, 19, 10, 0)
    db.scalar(select(EventTypeHost).where(EventTypeHost.user_id == bob.id)).active = False
    db.commit()
    assert av.check_slot(db, et, other_day, now=NOW) == [user.id]


def test_one_off_single_use_and_paused(db, host):
    user, ws = host
    et = make_event(db, user, ws, is_one_off=True, one_off_slots_json=["2026-11-21T15:00:00Z", "2026-11-22T16:00:00Z"])
    slots = av.get_available_slots(db, et, D(2026, 11, 16), D(2026, 11, 30), "UTC", now=NOW)
    assert [s.start for s in slots] == [T(2026, 11, 21, 15), T(2026, 11, 22, 16)]  # weekend ok: explicit times
    su = make_event(db, user, ws, slug="su", single_use=True)
    assert av.get_available_slots(db, su, D(2026, 11, 18), D(2026, 11, 18), "UTC", now=NOW)
    su.used_at = NOW
    paused = make_event(db, user, ws, slug="paused", status="paused")
    db.commit()
    assert av.get_available_slots(db, su, D(2026, 11, 18), D(2026, 11, 18), "UTC", now=NOW) == []
    assert av.get_available_slots(db, paused, D(2026, 11, 18), D(2026, 11, 18), "UTC", now=NOW) == []


def test_multiple_durations_and_slot_token(db, host):
    user, ws = host
    et = make_event(db, user, ws, durations_json=[30, 60])
    s60 = av.get_available_slots(db, et, D(2026, 11, 18), D(2026, 11, 18), "UTC", duration=60, now=NOW)
    assert all(s.end - s.start == dt.timedelta(minutes=60) for s in s60)
    assert s60[-1].start == T(2026, 11, 18, 16, 0)
    with pytest.raises(ValueError):
        av.resolve_duration(et, 45)
    assert av.verify_slot_token(s60[0].token, et.id, s60[0].start, 60)
    assert not av.verify_slot_token(s60[0].token, et.id, s60[0].start, 30)
    assert not av.verify_slot_token("garbage", et.id, s60[0].start, 60)


def test_expired_hold_does_not_block(db, host):
    user, ws = host
    et = make_event(db, user, ws)
    b = add_booking(db, et, user.id, T(2026, 11, 18, 10, 0), status="held")
    b.hold_expires_at = NOW - dt.timedelta(minutes=1)
    db.commit()
    assert av.check_slot(db, et, T(2026, 11, 18, 10, 0), now=NOW) == [user.id]
