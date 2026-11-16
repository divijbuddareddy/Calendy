"""AI assistant, notetaker and meeting poll tests (PRD 16.1, 16.2, 6.2)."""
import datetime as dt

from sqlalchemy import select

from app.models import (
    AssistantRequest, Booking, BookingHost, Contact, EventType, Invitee, MeetingPoll, MeetingRecap, PollVote,
    SentMessage, utcnow,
)
from app.security import decrypt, opaque_token
from app.services import assistant as asst
from app.services import notetaker as nt

MONDAY = dt.date(2026, 11, 16)


class C:  # lightweight contact stand-in for pure parser tests
    def __init__(self, id, name, email):
        self.id, self.name, self.email_normalized = id, name, email


CONTACTS = [C("con_1", "Sarah Connor", "sarah@acme.com"), C("con_2", "Bob Smith", "bob@other.io")]


# ---------------------------------------------------------------- assistant parser
def test_parse_find_30_minutes_next_tuesday_with_sarah():
    p = asst.parse_request("Find 30 minutes next Tuesday with Sarah", MONDAY, CONTACTS, [])
    assert p["intent"] == "schedule"
    assert p["duration"] == 30
    assert p["date_start"] == p["date_end"] == "2026-11-24"  # Tuesday of next calendar week
    assert p["participant"]["email"] == "sarah@acme.com"
    assert p["participant"]["contact_id"] == "con_1"
    assert p["confidence"] >= 0.75
    assert "Sarah" in p["rationale"]


def test_parse_dates_and_time_of_day():
    f = lambda s: asst.parse_request(s, MONDAY, CONTACTS, [])  # noqa: E731
    assert f("meet today")["date_start"] == "2026-11-16"
    t = f("an hour tomorrow afternoon with bob@other.io")
    assert t["date_start"] == "2026-11-17" and t["duration"] == 60 and t["time_of_day"] == "afternoon"
    assert t["window_minutes"] == [12 * 60, 17 * 60]
    assert t["participant"]["contact_id"] == "con_2"
    tw = f("half an hour this week in the morning")
    assert (tw["date_start"], tw["date_end"], tw["duration"], tw["time_of_day"]) == ("2026-11-16", "2026-11-22", 30, "morning")
    nw = f("1.5 hours next week")
    assert (nw["date_start"], nw["date_end"], nw["duration"]) == ("2026-11-23", "2026-11-29", 90)
    assert f("15 min on Friday")["date_start"] == "2026-11-20"
    assert f("call on 2026-12-01")["date_start"] == "2026-12-01"
    d = f("catch up sometime")
    assert d["date_explicit"] is False and d["duration"] is None
    unknown = f("30 minutes with Zed")
    assert unknown["participant"]["contact_id"] is None


def test_parse_event_type_and_history_intent():
    class ET:
        id, name, slug = "evt_1", "Discovery Call", "discovery-call"

    p = asst.parse_request("Book a discovery call with Sarah tomorrow", MONDAY, CONTACTS, [ET])
    assert p["event_type"] == {"id": "evt_1", "name": "Discovery Call"}
    assert asst.parse_request("When did I last meet Sarah?", MONDAY, CONTACTS, [])["intent"] == "history"
    assert asst.parse_request("How many meetings with acme.com this month?", MONDAY, CONTACTS, [])["intent"] == "history"


def _event_type(db, user, ws, slug="intro", **kw):
    et = EventType(workspace_id=ws.id, owner_user_id=user.id, name=kw.pop("name", "Intro call"), slug=slug,
                   duration=kw.pop("duration", 30), **kw)
    db.add(et)
    db.flush()
    return et


def _booking(db, user, ws, et, start, email, name="Sarah Connor", status="confirmed"):
    b = Booking(workspace_id=ws.id, event_type_id=et.id, host_id=user.id, start_at=start,
                end_at=start + dt.timedelta(minutes=30), status=status, manage_token=opaque_token(),
                snapshot_json={"name": et.name})
    db.add(b)
    db.flush()
    db.add(Invitee(booking_id=b.id, name=name, email=email))
    db.add(BookingHost(booking_id=b.id, user_id=user.id))
    db.flush()
    return b


def test_history_questions(db, host):
    user, ws = host
    et = _event_type(db, user, ws)
    db.add(Contact(workspace_id=ws.id, email_normalized="sarah@acme.com", name="Sarah Connor"))
    now = utcnow()
    first_of_month = now.replace(day=1, hour=12, minute=0, second=0, microsecond=0)
    _booking(db, user, ws, et, first_of_month + dt.timedelta(hours=1), "sarah@acme.com")
    _booking(db, user, ws, et, now - dt.timedelta(minutes=90), "sarah@acme.com")
    _booking(db, user, ws, et, now - dt.timedelta(days=400), "old@acme.com", name="Old")
    _booking(db, user, ws, et, now - dt.timedelta(hours=3), "x@acme.com", status="cancelled")
    _booking(db, user, ws, et, now - dt.timedelta(hours=5), "z@else.com", name="Zed")
    db.commit()
    a = asst.answer_history(db, user, ws.id, "How many meetings with acme.com this month?", now=now)
    assert a["count"] == 2  # cancelled and last year's excluded
    a = asst.answer_history(db, user, ws.id, "When did I last meet Sarah?", now=now)
    assert a["answer"].startswith("You last met with Sarah Connor")
    assert len([r for r in a["rows"] if "sarah@acme.com" in r["invitee"]]) == 2


def test_assistant_flow_approve_and_reject(client, db, host, login, monkeypatch):
    user, ws = host
    et = _event_type(db, user, ws)
    db.add(Contact(workspace_id=ws.id, email_normalized="sarah@acme.com", name="Sarah Connor"))
    db.commit()
    from app.services import availability

    calls = {}

    def fake_slots(db_, event_type, start_date, end_date, tz, duration=None):
        calls["args"] = (event_type.id, start_date, end_date, duration)
        base = dt.datetime.combine(start_date, dt.time(10, 0))
        return [availability.Slot(start=base + dt.timedelta(hours=h), end=base + dt.timedelta(hours=h, minutes=30))
                for h in range(5)]

    monkeypatch.setattr(availability, "get_available_slots", fake_slots)
    login(client, user)
    r = client.post("/app/assistant", data={"prompt": "Find 30 minutes next Tuesday with Sarah"})
    assert r.status_code == 200
    req = db.scalar(select(AssistantRequest).where(AssistantRequest.user_id == user.id))
    assert req.status == "draft"
    assert calls["args"][0] == et.id and calls["args"][3] == 30
    assert len(req.proposal_json["slots"]) == 3
    assert req.proposal_json["draft"]["to"] == "sarah@acme.com"
    assert "Approve &amp; send" in r.text
    # nothing sent before approval
    assert db.scalar(select(SentMessage).where(SentMessage.recipient == "sarah@acme.com")) is None
    # approval requires the explicit confirm box
    client.post(f"/app/assistant/{req.id}/approve", data={"action": "send"})
    db.rollback()  # end read snapshot
    assert db.get(AssistantRequest, req.id).status == "draft"
    client.post(f"/app/assistant/{req.id}/approve", data={"action": "send", "confirm": "yes"})
    db.rollback()  # end read snapshot
    assert db.get(AssistantRequest, req.id).status == "executed"
    assert db.scalar(select(SentMessage).where(SentMessage.recipient == "sarah@acme.com")) is not None
    # reject flow
    client.post("/app/assistant", data={"prompt": "30 minutes tomorrow with Sarah"})
    db.rollback()
    req2 = db.scalars(select(AssistantRequest).where(AssistantRequest.status == "draft")).one()
    client.post(f"/app/assistant/{req2.id}/reject")
    db.rollback()  # end read snapshot
    assert db.get(AssistantRequest, req2.id).status == "rejected"
    # history question answered inline
    r = client.post("/app/assistant", data={"prompt": "When did I last meet Sarah?"})
    assert "No past meetings" in r.text


def test_assistant_book_only_proposed_slot(db, host):
    user, ws = host
    et = _event_type(db, user, ws)
    req = AssistantRequest(workspace_id=ws.id, user_id=user.id, prompt="x",
                           parsed_json={"participant": {"name": "S", "email": "s@x.com"}},
                           proposal_json={"slots": [{"start": "2030-01-01T10:00:00"}],
                                          "event_type": {"id": et.id, "duration": 30}})
    db.add(req)
    db.commit()
    import pytest

    with pytest.raises(ValueError):
        asst.execute(db, req, user, "book", "2030-01-01T11:00:00")


def test_assistant_gated_by_plan(client, db, host, login):
    user, ws = host
    ws.plan_id = "free"
    db.commit()
    login(client, user)
    assert client.get("/app/assistant").status_code == 403
    assert client.get("/app/recaps").status_code == 403


# ---------------------------------------------------------------- notetaker
TRANSCRIPT = """WEBVTT

1
00:00:01.000 --> 00:00:04.000
<v Alice>Welcome everyone, let's review the pricing roadmap.</v>

2
00:00:05.000 --> 00:00:09.000
<v Bob>We agreed to launch the new pricing in January.</v>
<v Bob>I will send the pricing deck by Friday.</v>
<v Alice>Carol, can you update the website copy by 2026-12-01?</v>
<v Carol>Sure. The website copy needs a refresh anyway.</v>
"""


def test_recap_heuristics():
    text = nt.normalize_transcript(TRANSCRIPT)
    assert text.splitlines()[0] == "Alice: Welcome everyone, let's review the pricing roadmap."
    rec = nt.generate_recap(text, MONDAY)
    assert any("agreed to launch" in d for d in rec["decisions"])
    items = {a["owner"]: a for a in rec["action_items"]}
    assert items["Bob"]["due"] == "2026-11-20"  # "by Friday"
    assert items["Carol"]["due"] == "2026-12-01"
    assert "Alice" in rec["summary"] and "pricing" in rec["summary"]
    assert any("deck" in line for line in nt.ask(text, "When is the pricing deck coming?"))


def test_recap_encrypted_share_password_and_purge(client, db, host, login):
    user, ws = host
    et = _event_type(db, user, ws)
    b = _booking(db, user, ws, et, utcnow() - dt.timedelta(hours=1), "guest@acme.com", name="Guest")
    db.commit()
    login(client, user)
    # consent is required
    client.post("/app/recaps/new", data={"booking_id": b.id})
    assert db.scalar(select(MeetingRecap)) is None
    client.post("/app/recaps/new", data={"booking_id": b.id, "consent": "yes"})
    db.rollback()
    rc = db.scalar(select(MeetingRecap))
    assert rc.recording_status == "joining" and rc.retention_until > utcnow() + dt.timedelta(days=89)
    client.post(f"/app/recaps/{rc.id}/bot", data={"action": "start"})
    db.rollback()
    assert db.scalar(select(SentMessage).where(SentMessage.recipient == "guest@acme.com")) is not None
    client.post(f"/app/recaps/{rc.id}/bot", data={"action": "stop"})
    plaintext = nt.normalize_transcript(TRANSCRIPT)
    r = client.post(f"/app/recaps/{rc.id}/transcript", files={"file": ("call.vtt", TRANSCRIPT.encode(), "text/vtt")})
    assert r.status_code == 200
    db.rollback()  # end read snapshot
    rc = db.get(MeetingRecap, rc.id)
    assert rc.transcript and "pricing" not in rc.transcript  # encrypted at rest
    assert decrypt(rc.transcript) == plaintext
    assert rc.recording_status == "processed" and rc.action_items_json
    # search across recaps + ask a question
    assert "pricing deck" in client.get("/app/recaps?q=deck").text
    assert "send the pricing deck" in client.get(f"/app/recaps/{rc.id}?ask=deck").text
    # password-protected share
    client.post(f"/app/recaps/{rc.id}/share", data={"enabled": "yes", "password": "s3cret!"})
    db.rollback()  # end read snapshot
    token = db.get(MeetingRecap, rc.id).share_token
    client.cookies.clear()
    page = client.get(f"/recap/{token}")
    assert "password protected" in page.text and "agreed to launch" not in page.text
    bad = client.post(f"/recap/{token}", data={"password": "nope"})
    assert bad.status_code == 403 and "Incorrect password" in bad.text
    ok = client.post(f"/recap/{token}", data={"password": "s3cret!"})
    assert ok.status_code == 200 and "agreed to launch" in ok.text
    # retention purge
    rc = db.get(MeetingRecap, rc.id)
    rc.retention_until = utcnow() - dt.timedelta(minutes=1)
    db.commit()
    assert nt.purge_expired(db) == 1
    db.commit()
    db.rollback()  # end read snapshot
    rc = db.get(MeetingRecap, rc.id)
    assert rc.deleted_at and rc.transcript is None and rc.share_token is None
    assert client.get(f"/recap/{token}").status_code == 404


# ---------------------------------------------------------------- meeting polls
def test_poll_create_vote_tally_confirm(client, db, host, login):
    user, ws = host
    login(client, user)
    t1 = (dt.datetime.now() + dt.timedelta(days=10)).replace(hour=10, minute=0, second=0, microsecond=0)
    t2 = t1 + dt.timedelta(days=1)
    r = client.post("/app/polls", data={"title": "Planning", "duration": "45", "location_type": "zoom",
                                         "times": [t1.strftime("%Y-%m-%dT%H:%M"), t2.strftime("%Y-%m-%dT%H:%M")]})
    assert r.status_code == 200
    db.rollback()
    poll = db.scalar(select(MeetingPoll))
    assert len(poll.options_json) == 2 and poll.duration == 45
    o1, o2 = poll.options_json[0]["id"], poll.options_json[1]["id"]
    client.cookies.clear()
    tok = poll.public_token
    assert client.get(f"/poll/{tok}").status_code == 200
    client.post(f"/poll/{tok}", data={"name": "Ann", "email": "ann@x.com", "options": [o1, o2]})
    client.post(f"/poll/{tok}", data={"name": "Ben", "email": "ben@x.com", "options": [o2]})
    client.post(f"/poll/{tok}", data={"name": "Cat", "email": "cat@x.com", "options": [o1]})
    client.post(f"/poll/{tok}", data={"name": "Cat", "email": "CAT@x.com", "options": [o2]})  # update, not duplicate
    client.post(f"/poll/{tok}", data={"name": "Bot", "email": "bot@x.com", "options": [o1], "website": "spam"})  # honeypot
    assert client.post(f"/poll/{tok}", data={"name": "", "email": "bad", "options": [o1]}).status_code == 400
    db.rollback()
    votes = db.scalars(select(PollVote)).all()
    assert len(votes) == 3
    from app.routers.polls import tally

    db.rollback()  # end read snapshot
    poll = db.get(MeetingPoll, poll.id)
    counts = {t["option"]["id"]: t["count"] for t in tally(poll, votes)}
    assert counts == {o1: 1, o2: 3}
    login(client, user)
    page = client.get(f"/app/polls/{poll.id}")
    assert "Most popular" in page.text
    client.post(f"/app/polls/{poll.id}/confirm", data={"option_id": o2})
    db.rollback()  # end read snapshot
    poll = db.get(MeetingPoll, poll.id)
    assert poll.status == "booked" and poll.chosen_option_id == o2 and poll.booking_id
    b = db.get(Booking, poll.booking_id)
    assert b.start_at.isoformat() == poll.options_json[1]["start_at"]
    assert b.status in ("confirmed", "pending")
    et = db.get(EventType, b.event_type_id)
    assert et.is_one_off and et.visibility == "private"
    for email in ("ann@x.com", "ben@x.com", "cat@x.com"):
        assert db.scalar(select(SentMessage).where(SentMessage.recipient == email,
                                                   SentMessage.subject == "Confirmed: Planning")) is not None
    # voting closed after booking
    client.cookies.clear()
    assert client.post(f"/poll/{tok}", data={"name": "Late", "email": "late@x.com", "options": [o1]}).status_code == 400


def test_poll_viewer_cannot_create(client, db, host, login):
    from app.services.bootstrap import add_user_to_workspace

    _, ws = host
    viewer = add_user_to_workspace(db, ws, "v@example.com", "Viewer", role="viewer")
    db.commit()
    login(client, viewer)
    r = client.post("/app/polls", data={"title": "X", "duration": "30", "times": ["2030-01-01T10:00", "2030-01-02T10:00"]})
    assert r.status_code == 403


def test_ai_pages_render(client, db, host, login):
    user, _ = host
    login(client, user)
    for url in ("/app/assistant", "/app/recaps", "/app/polls"):
        r = client.get(url)
        assert r.status_code == 200, url
