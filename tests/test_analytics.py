import csv
import datetime as dt
import io
import re

from app.models import Booking, EventType, Invitee, PageView, PaymentRecord, RoutingForm, RoutingSubmission, utcnow
from app.security import opaque_token
from app.services.bootstrap import add_user_to_workspace


def _mk_booking(db, ws, et, host_id, start, status="confirmed", no_show=False, created=None, utm=None, rsid=None, email="i@x.com"):
    b = Booking(
        workspace_id=ws.id, event_type_id=et.id, host_id=host_id, start_at=start, end_at=start + dt.timedelta(minutes=et.duration),
        status=status, no_show=no_show, manage_token=opaque_token(), utm_json=utm, routing_submission_id=rsid,
        exclusive=False,
    )
    if created:
        b.created_at = created
    db.add(b)
    db.flush()
    db.add(Invitee(booking_id=b.id, name="Inv", email=email))
    return b


def _kpis(html):
    return {k: int(v) for k, v in re.findall(r'data-kpi="(\w+)">(\d+)<', html)}


def test_analytics_counts_reconcile(client, db, host, login):
    user, ws = host
    now = utcnow()
    et30 = EventType(workspace_id=ws.id, owner_user_id=user.id, name="Intro call", slug="intro", duration=30)
    et60 = EventType(workspace_id=ws.id, owner_user_id=user.id, name="Deep dive", slug="deep", duration=60, price_minor=5000, currency="USD")
    db.add_all([et30, et60])
    db.flush()
    past = now - dt.timedelta(days=2)
    fut = now + dt.timedelta(days=2)
    rows = [
        _mk_booking(db, ws, et30, user.id, past, created=now - dt.timedelta(days=5)),  # completed
        _mk_booking(db, ws, et30, user.id, past + dt.timedelta(hours=1), created=now - dt.timedelta(days=5),
                    utm={"utm_source": "newsletter", "utm_medium": "email", "utm_campaign": "nov"}),  # completed
        _mk_booking(db, ws, et30, user.id, past + dt.timedelta(hours=2), no_show=True, created=now - dt.timedelta(days=4)),
        _mk_booking(db, ws, et60, user.id, fut, created=now - dt.timedelta(days=1)),  # upcoming
        _mk_booking(db, ws, et60, user.id, fut + dt.timedelta(hours=3), status="cancelled", created=now - dt.timedelta(days=1)),
        _mk_booking(db, ws, et30, user.id, fut + dt.timedelta(hours=5), status="rescheduled", created=now - dt.timedelta(days=1)),
    ]
    # excluded: expired hold, and a booking created 90 days ago
    _mk_booking(db, ws, et30, user.id, fut + dt.timedelta(hours=7), status="expired", created=now - dt.timedelta(days=1))
    _mk_booking(db, ws, et30, user.id, past - dt.timedelta(days=60), created=now - dt.timedelta(days=90))
    db.add(PaymentRecord(workspace_id=ws.id, booking_id=rows[3].id, provider="mock", amount_minor=5000, currency="USD", status="succeeded"))
    for _ in range(12):
        db.add(PageView(workspace_id=ws.id, event_type_id=et30.id, page="event"))
    form = RoutingForm(workspace_id=ws.id, owner_user_id=user.id, name="Qualify", slug="q",
                       rules_json=[{"id": "r1", "name": "Enterprise", "logic": "and", "conditions": [], "destination": {"type": "event_type"}}],
                       fallback_json={"type": "message", "value": "Thanks"})
    db.add(form)
    db.flush()
    s1 = RoutingSubmission(workspace_id=ws.id, form_id=form.id, answers_json={}, matched_rule_id="r1", destination_json={"type": "event_type"})
    s2 = RoutingSubmission(workspace_id=ws.id, form_id=form.id, answers_json={}, destination_json={"type": "message"})
    db.add_all([s1, s2])
    db.flush()
    rows[3].routing_submission_id = s1.id
    db.commit()

    login(client, user)
    r = client.get("/app/analytics")
    assert r.status_code == 200
    k = _kpis(r.text)
    assert k == {"created": 6, "completed": 2, "upcoming": 1, "cancelled": 1, "rescheduled": 1, "no_show": 1}
    assert "newsletter" in r.text and "Qualify" in r.text and "1/2 booked" in r.text
    assert "$50.00" in r.text
    assert "50.0%" in r.text  # 6 bookings / 12 views

    # CSV export reconciles with the KPI rows
    r = client.get("/app/analytics/export.csv")
    data = list(csv.DictReader(io.StringIO(r.text)))
    assert len(data) == 6
    assert sum(1 for d in data if d["outcome"] == "completed") == 2
    assert {d["status"] for d in data} == {"confirmed", "cancelled", "rescheduled"}

    # event type filter
    k = _kpis(client.get("/app/analytics", params={"event_type": et60.id}).text)
    assert k["created"] == 2 and k["cancelled"] == 1 and k["upcoming"] == 1

    # widened date range includes the 90-day-old booking
    start = (now - dt.timedelta(days=100)).date().isoformat()
    k = _kpis(client.get("/app/analytics", params={"start": start}).text)
    assert k["created"] == 7


def test_member_sees_only_own_data(client, db, host, login):
    user, ws = host
    member = add_user_to_workspace(db, ws, "m@example.com", "Mia", role="member")
    et = EventType(workspace_id=ws.id, owner_user_id=user.id, name="Intro", slug="intro", duration=30)
    db.add(et)
    db.flush()
    fut = utcnow() + dt.timedelta(days=1)
    _mk_booking(db, ws, et, user.id, fut)
    _mk_booking(db, ws, et, member.id, fut + dt.timedelta(hours=1))
    _mk_booking(db, ws, et, member.id, fut + dt.timedelta(hours=2))
    db.commit()
    login(client, member)
    k = _kpis(client.get("/app/analytics", params={"host": user.id}).text)  # host param ignored for members
    assert k["created"] == 2
    assert len(client.get("/app/analytics/export.csv").text.strip().splitlines()) == 3
    login(client, user)
    assert _kpis(client.get("/app/analytics").text)["created"] == 3
    assert _kpis(client.get("/app/analytics", params={"host": member.id}).text)["created"] == 2


def test_round_robin_distribution(client, db, host, login):
    user, ws = host
    m2 = add_user_to_workspace(db, ws, "b@example.com", "Ben", role="member")
    et = EventType(workspace_id=ws.id, owner_user_id=user.id, name="Sales RR", slug="rr", duration=30, kind="round_robin")
    db.add(et)
    db.flush()
    fut = utcnow() + dt.timedelta(days=1)
    for i, h in enumerate([user.id, m2.id, m2.id]):
        _mk_booking(db, ws, et, h, fut + dt.timedelta(hours=i))
    db.commit()
    login(client, user)
    r = client.get("/app/analytics")
    assert "Sales RR" in r.text and "Spread 1" in r.text and "Ben" in r.text
