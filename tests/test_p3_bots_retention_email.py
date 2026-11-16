"""Notetaker recording adapters, analytics retention / lead time / heatmap, schedule-from-email and
meeting prep (PRD 14.2, 16.1, 16.2)."""
import datetime as dt

import pytest
from sqlalchemy import select

from app.models import (
    AssistantRequest, Booking, BookingAnswer, BookingHost, Contact, EventType, Invitee, MeetingRecap,
    RoutingForm, RoutingSubmission, SentMessage, Workspace, utcnow,
)
from app.models_notetaker import NotetakerBot
from app.security import decrypt, opaque_token
from app.services import analytics as an
from app.services import assistant as asst
from app.services import notetaker as nt
from app.services import recording


def _et(db, user, ws, slug="intro", duration=30):
    et = EventType(workspace_id=ws.id, owner_user_id=user.id, name="Intro call", slug=slug, duration=duration)
    db.add(et)
    db.flush()
    return et


def _booking(db, user, ws, et, start, email, name="Sarah Connor", created=None, guests=None, join_url=None):
    b = Booking(workspace_id=ws.id, event_type_id=et.id, host_id=user.id, start_at=start,
                end_at=start + dt.timedelta(minutes=30), status="confirmed", manage_token=opaque_token(),
                snapshot_json={"name": et.name}, utm_json={"utm_source": "news", "gclid": "abc"},
                location_json={"type": "zoom", "join_url": join_url} if join_url else None)
    if created:
        b.created_at = created
    db.add(b)
    db.flush()
    db.add(Invitee(booking_id=b.id, name=name, email=email, phone="+441234567890", company="Acme", guests_json=guests))
    db.add(BookingHost(booking_id=b.id, user_id=user.id))
    db.add(BookingAnswer(booking_id=b.id, label="Agenda", answer_json="Pricing review"))
    db.flush()
    return b


@pytest.fixture(autouse=True)
def _reset_adapter():
    yield
    recording.set_adapter(None)


# ---------------------------------------------------------------- recording adapters
class FakeResp:
    def __init__(self, status, data):
        self.status_code, self._data, self.text = status, data, str(data)

    def json(self):
        return self._data


class FakeHttp:
    def __init__(self):
        self.calls = []

    def request(self, method, url, json=None, headers=None):
        self.calls.append((method, url, json, headers))
        if url.endswith("/bot/") and method == "POST":
            return FakeResp(201, {"id": "b-1", "status_changes": [{"code": "joining_call"}]})
        if url.endswith("/transcript/"):
            return FakeResp(200, [{"speaker": "Alice", "words": [{"text": "We"}, {"text": "agreed"}, {"text": "to ship."}]},
                                  {"speaker": "Bob", "words": [{"text": "I will send the deck by Friday."}]}])
        if url.endswith("/bot/b-1/"):
            return FakeResp(200, {"id": "b-1", "status_changes": [{"code": "in_call_recording"}, {"code": "done"}]})
        return FakeResp(200, {})


def test_recall_adapter_requests_and_mapping():
    http = FakeHttp()
    a = recording.RecallAdapter("key123", base_url="https://recall.test/api/v1", client=http)
    st = a.create_bot("https://zoom.us/j/1", "Acme Notetaker", {"recap_id": "r"})
    method, url, body, headers = http.calls[0]
    assert (method, url) == ("POST", "https://recall.test/api/v1/bot/")
    assert body["bot_name"] == "Acme Notetaker" and body["meeting_url"] == "https://zoom.us/j/1"
    assert headers["Authorization"] == "Token key123"
    assert st.bot_id == "b-1" and st.status == "joining"
    assert a.get_bot("b-1").status == "done"
    assert a.fetch_transcript("b-1") == "Alice: We agreed to ship.\nBob: I will send the deck by Friday."
    with pytest.raises(recording.RecordingError):
        a.create_bot(None, "x")
    a.remove_bot("b-1")
    assert http.calls[-1][1].endswith("/bot/b-1/leave_call/")


def test_adapter_selection(monkeypatch):
    monkeypatch.delenv("RECALL_API_KEY", raising=False)
    assert recording.get_adapter().name == "mock"
    monkeypatch.setenv("RECALL_API_KEY", "k")
    assert recording.get_adapter().name == "recall"
    assert recording.get_adapter("mock").name == "mock"


def test_mock_bot_consent_indicator_and_remove(client, db, host, login):
    user, ws = host
    et = _et(db, user, ws)
    b = _booking(db, user, ws, et, utcnow() + dt.timedelta(minutes=10), "guest@acme.com", name="Guest",
                 guests=["cc@acme.com"])
    db.commit()
    login(client, user)
    client.post("/app/recaps/new", data={"booking_id": b.id, "consent": "yes"})
    db.rollback()
    rc = db.scalar(select(MeetingRecap))
    bot = db.scalar(select(NotetakerBot).where(NotetakerBot.recap_id == rc.id))
    assert bot.provider == "mock" and bot.bot_name == f"{ws.name} Notetaker" and bot.status == "in_call"
    # consent notice sent to invitee, guest and host BEFORE recording starts
    rcpts = set(db.scalars(select(SentMessage.recipient).where(SentMessage.subject.like("Recording notice%"))))
    assert {"guest@acme.com", "cc@acme.com", user.email} <= rcpts and bot.consent_recipients == 3
    assert rc.recording_status == "joining"
    page = client.get(f"/app/recaps/{rc.id}").text
    import html as _html

    assert "workspace Notetaker" in page and "Recording notice emailed to 3" in page
    client.post(f"/app/recaps/{rc.id}/bot", data={"action": "start"})
    page = client.get(f"/app/recaps/{rc.id}").text
    assert "&#9679; Recording" in page and "is recording and transcribing" in page
    client.post(f"/app/recaps/{rc.id}/bot", data={"action": "remove"})
    db.rollback()
    assert db.get(NotetakerBot, bot.id).status == "removed"
    assert db.get(MeetingRecap, rc.id).recording_status == "stopped"
    # a removed bot cannot restart recording
    client.post(f"/app/recaps/{rc.id}/bot", data={"action": "start"})
    db.rollback()
    assert db.get(MeetingRecap, rc.id).recording_status == "stopped"


def test_provider_bot_sync_fetches_encrypted_transcript(client, db, host, login):
    user, ws = host
    et = _et(db, user, ws)
    b = _booking(db, user, ws, et, utcnow() - dt.timedelta(minutes=40), "guest@acme.com", join_url="https://zoom.us/j/9")
    db.commit()
    http = FakeHttp()
    recording.set_adapter(recording.RecallAdapter("k", base_url="https://recall.test/api/v1", client=http))
    login(client, user)
    client.post("/app/recaps/new", data={"booking_id": b.id, "consent": "yes"})
    db.rollback()
    bot = db.scalar(select(NotetakerBot))
    assert bot.provider == "recall" and bot.external_bot_id == "b-1" and bot.meeting_url == "https://zoom.us/j/9"
    nt._notetaker_bot_task(db)  # the periodic poller
    db.commit()
    rc = db.get(MeetingRecap, bot.recap_id)
    assert rc.transcript and "agreed" not in rc.transcript
    assert "Bob: I will send the deck" in decrypt(rc.transcript)
    assert rc.recording_status == "processed" and rc.action_items_json
    assert db.get(NotetakerBot, bot.id).transcript_fetched_at is not None


def test_recap_purge_hard_deletes_tombstones(db, host):
    user, ws = host
    rc = MeetingRecap(workspace_id=ws.id, owner_user_id=user.id, title="Old", retention_until=utcnow() - dt.timedelta(days=1))
    nt.set_transcript(rc, "Alice: hi")
    db.add(rc)
    db.commit()
    assert nt.purge_expired(db) == 1
    assert rc.transcript is None and rc.deleted_at
    db.commit()
    nt.purge_expired(db, now=utcnow() + dt.timedelta(days=nt.TOMBSTONE_DAYS + 1))
    db.commit()
    assert db.get(MeetingRecap, rc.id) is None


# ---------------------------------------------------------------- analytics retention / lead time / heatmap
def test_retention_purge_anonymises_but_keeps_counts(db, host):
    user, ws = host
    ws.settings_json = {"retention_days": 30}
    et = _et(db, user, ws)
    now = utcnow()
    old = _booking(db, user, ws, et, now - dt.timedelta(days=60), "old@acme.com", created=now - dt.timedelta(days=70))
    new = _booking(db, user, ws, et, now - dt.timedelta(days=5), "new@acme.com", created=now - dt.timedelta(days=6))
    form = RoutingForm(workspace_id=ws.id, owner_user_id=user.id, name="Q", slug="q")
    db.add(form)
    db.flush()
    sub = RoutingSubmission(workspace_id=ws.id, form_id=form.id, answers_json={"f1": "old@acme.com"})
    sub.created_at = now - dt.timedelta(days=60)
    db.add(sub)
    db.commit()
    f = an.Filters(workspace_id=ws.id, start=now - dt.timedelta(days=100), end=now)
    before = an.compute(db, f)["kpi"]
    assert an.purge_expired_pii(db) == 1
    db.commit()
    inv_old = db.scalar(select(Invitee).where(Invitee.booking_id == old.id))
    assert inv_old.email.endswith("@redacted.invalid") and inv_old.phone is None and inv_old.company is None
    assert db.scalar(select(BookingAnswer).where(BookingAnswer.booking_id == old.id)).answer_json == "[purged]"
    assert db.get(Booking, old.id).utm_json == {"utm_source": "news"}  # attribution kept, click ids dropped
    assert db.scalar(select(Invitee).where(Invitee.booking_id == new.id)).email == "new@acme.com"
    assert db.get(RoutingSubmission, sub.id).answers_json == {"_purged": "[purged]"}
    assert an.compute(db, f)["kpi"] == before  # aggregates unchanged
    assert an.purge_expired_pii(db) == 0  # idempotent
    # default retention 365 days: nothing purged for a fresh workspace
    ws.settings_json = {}
    db.commit()
    assert an.retention_days(db.get(Workspace, ws.id)) == 365


def test_lead_time_and_heatmap(client, db, host, login):
    user, ws = host
    et = _et(db, user, ws)
    now = utcnow()
    # Monday 2026-11-16 10:00 UTC = 10:00 Europe/London
    mon10 = dt.datetime(2026, 11, 16, 10, 0)
    _booking(db, user, ws, et, mon10, "a@x.com", created=mon10 - dt.timedelta(hours=2))
    _booking(db, user, ws, et, mon10 + dt.timedelta(days=7), "b@x.com", created=mon10 - dt.timedelta(hours=1))
    _booking(db, user, ws, et, mon10 + dt.timedelta(days=1, hours=4), "c@x.com", created=mon10 - dt.timedelta(minutes=30))
    db.commit()
    f = an.Filters(workspace_id=ws.id, start=mon10 - dt.timedelta(days=1), end=now + dt.timedelta(days=1), tz="Europe/London")
    d = an.compute(db, f)
    mon = d["heatmap"]["rows"][0]
    assert mon["day"] == "Mon" and mon["cells"][10]["count"] == 2 and mon["cells"][10]["level"] == 4
    assert d["heatmap"]["rows"][1]["cells"][14]["count"] == 1
    assert d["heatmap"]["top"][0] == {"day": "Mon", "hour": "10:00", "count": 2}
    lt = d["lead_time"]
    assert lt["median_hours"] == 28.5 and lt["avg_hours"] is not None
    assert {b["label"]: b["count"] for b in lt["buckets"]}["1-24 hours"] == 1
    assert sum(b["count"] for b in lt["buckets"]) == 3
    login(client, user)
    page = client.get("/app/analytics?start=2026-11-01&end=2026-12-31").text
    assert "Popular time slots" in page and "Booking lead time" in page and 'data-heat="Mon-10"' in page
    js = client.get("/app/analytics/data.json?start=2026-11-01&end=2026-12-31").json()
    assert js["heatmap"]["max"] == 2 and "previous_period" in js and js["retention_days"] == 365
    csv_text = client.get("/app/analytics/export.csv?start=2026-11-01&end=2026-12-31").text
    assert "lead_time_hours" in csv_text.splitlines()[0]


# ---------------------------------------------------------------- schedule from email
THREAD = """From: Sarah Connor <sarah@acme.com>
To: Alice Host <alice@example.com>
Cc: Dana Lee <dana@acme.com>
Subject: Pilot kickoff

Hi Alice,

Could we find 30 minutes next Tuesday afternoon to kick off the pilot?

Thanks,
Sarah

On Fri, 13 Nov 2026 at 09:00, Alice Host <alice@example.com> wrote:
> Happy to chat about a 60 minute workshop sometime.
"""


def test_parse_email_thread():
    info = asst.parse_email_thread(THREAD, "alice@example.com")
    assert info["subject"] == "Pilot kickoff"
    assert info["requester"] == {"name": "Sarah Connor", "email": "sarah@acme.com"}
    assert info["participants"] == [{"name": "Dana Lee", "email": "dana@acme.com"}]
    assert "30 minutes next Tuesday" in info["body"] and "workshop" not in info["body"]
    bare = asst.parse_email_thread("Hey, can we do an hour on Friday?\n\nBest,\nJordan Smith", "alice@example.com")
    assert bare["requester"]["name"] == "Jordan Smith" and bare["requester"]["email"] is None


def test_schedule_from_email_flow(client, db, host, login, monkeypatch):
    user, ws = host
    et = _et(db, user, ws)
    db.commit()
    from app.services import availability

    seen = {}

    def fake_slots(db_, event_type, start_date, end_date, tz, duration=None):
        seen["args"] = (start_date, end_date, duration)
        base = dt.datetime.combine(start_date, dt.time(9, 0))
        return [availability.Slot(start=base + dt.timedelta(hours=h), end=base + dt.timedelta(hours=h, minutes=30))
                for h in range(8)]

    monkeypatch.setattr(availability, "get_available_slots", fake_slots)
    login(client, user)
    r = client.post("/app/assistant/email", data={"thread": THREAD})
    assert r.status_code == 200
    req = db.scalar(select(AssistantRequest).where(AssistantRequest.user_id == user.id))
    p = req.parsed_json
    assert p["source"] == "email" and p["participant"]["email"] == "sarah@acme.com" and p["duration"] == 30
    assert p["time_of_day"] == "afternoon" and seen["args"][2] == 30
    prop = req.proposal_json
    assert prop["slots"] and all(12 <= dt.datetime.fromisoformat(s["start"]).hour + 0 for s in prop["slots"])
    assert prop["draft"]["to"] == "sarah@acme.com" and prop["draft"]["cc"] == ["dana@acme.com"]
    assert prop["draft"]["subject"] == "Re: Pilot kickoff"
    assert "Email: Pilot kickoff" in r.text and "dana@acme.com" in r.text
    assert db.scalar(select(SentMessage).where(SentMessage.recipient == "sarah@acme.com")) is None  # nothing sent yet
    client.post(f"/app/assistant/{req.id}/approve", data={"action": "send", "confirm": "yes", "cc": "dana@acme.com"})
    db.rollback()
    assert db.get(AssistantRequest, req.id).status == "executed"
    sent = set(db.scalars(select(SentMessage.recipient).where(SentMessage.subject == "Re: Pilot kickoff")))
    assert sent == {"sarah@acme.com", "dana@acme.com"}


def test_meeting_prep_brief_and_approval(client, db, host, login):
    user, ws = host
    et = _et(db, user, ws)
    now = utcnow()
    db.add(Contact(workspace_id=ws.id, email_normalized="sarah@acme.com", name="Sarah Connor", tags_json=["vip"],
                   notes="Decision maker"))
    past = _booking(db, user, ws, et, now - dt.timedelta(days=10), "sarah@acme.com")
    past.host_notes = "Wants SSO"
    rc = MeetingRecap(workspace_id=ws.id, owner_user_id=user.id, booking_id=past.id, title="Kickoff", summary="Went well",
                      action_items_json=[{"text": "Send SSO docs", "owner": "Alice", "due": None}])
    db.add(rc)
    nxt = _booking(db, user, ws, et, now + dt.timedelta(days=2), "sarah@acme.com")
    db.commit()
    assert asst.detect_intent("Prep me for my next meeting with Sarah") == "prep"
    brief = asst.meeting_prep(db, user, ws.id, "Prep me for my next meeting with Sarah")["brief"]
    assert brief["booking_id"] == nxt.id and brief["past_meetings"] == 1 and brief["last_notes"] == "Wants SSO"
    assert brief["contact"]["tags"] == ["vip"] and brief["recaps"][0]["action_items"] == ["Send SSO docs"]
    assert brief["answers"][0] == {"q": "Agenda", "a": "Pricing review"}
    login(client, user)
    r = client.post("/app/assistant", data={"prompt": "Prep me for my next meeting with Sarah"})
    assert "Meeting prep" in r.text and "Wants SSO" in r.text and "Send SSO docs" in r.text
    db.rollback()
    req = db.scalar(select(AssistantRequest).where(AssistantRequest.user_id == user.id))
    assert req.status == "draft"
    assert db.scalar(select(SentMessage).where(SentMessage.recipient == "sarah@acme.com")) is None
    client.post(f"/app/assistant/{req.id}/approve", data={"action": "send", "confirm": "yes"})
    db.rollback()
    assert db.get(AssistantRequest, req.id).status == "executed"
    assert db.scalar(select(SentMessage).where(SentMessage.recipient == "sarah@acme.com")) is not None
    none = asst.meeting_prep(db, user, ws.id, "prep for meeting with nobody@else.com")
    assert none["brief"] is None and "No upcoming" in none["answer"]
