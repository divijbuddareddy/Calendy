"""Transactional notifications, workflow engine, quiet hours, retries, SMS consent."""
import datetime as dt
import secrets

from sqlalchemy import select

from app.events import process_outbox, publish
from app.models import (
    Booking,
    EventType,
    InAppNotification,
    Invitee,
    NotificationJob,
    OutboxEvent,
    SentMessage,
    Workflow,
    utcnow,
)
from app.services import notifications as N
from app.services import workflows as W


def make_booking(db, user, ws, start=None, status="confirmed", phone=None, consent=None):
    db.commit()  # end any read snapshot so this session can write (SQLite WAL)
    et = EventType(workspace_id=ws.id, owner_user_id=user.id, name="Discovery call", slug="disc-" + secrets.token_hex(3),
                   duration=30)
    db.add(et)
    db.flush()
    start = start or (utcnow() + dt.timedelta(days=3)).replace(minute=0, second=0, microsecond=0)
    b = Booking(workspace_id=ws.id, event_type_id=et.id, host_id=user.id, start_at=start,
                end_at=start + dt.timedelta(minutes=30), timezone="America/New_York", status=status,
                manage_token=secrets.token_urlsafe(16), location_json={"type": "phone", "value": "+1 555 0100"})
    b.invitees.append(Invitee(name="Bob Invitee", email="bob@example.com", phone=phone, timezone="America/New_York",
                              consent_json=consent))
    db.add(b)
    db.commit()
    return b


def add_wf(db, ws, user, **kw):
    data = dict(workspace_id=ws.id, owner_user_id=user.id, name="Reminder", trigger="before_event", offset_minutes=60,
                channel="email", audience="invitee", subject_template="Reminder: {event_name}",
                body_template="Hi {invitee_name}, see you at {start_time} ({timezone}). Cancel: {cancel_url}",
                conditions_json={"event_type_ids": []}, active=True)
    data.update(kw)
    wf = Workflow(**data)
    db.add(wf)
    db.commit()
    return wf


def test_booking_created_sends_once_even_if_reprocessed(db, host):
    user, ws = host
    b = make_booking(db, user, ws)
    ev = publish(db, "booking.created", {"booking_id": b.id}, workspace_id=ws.id)
    db.commit()
    process_outbox(db)
    jobs1 = db.scalars(select(NotificationJob).where(NotificationJob.booking_id == b.id)).all()
    assert {j.recipient for j in jobs1} == {"bob@example.com", user.email}
    # simulate an at-least-once redelivery of the same outbox event + a duplicate event
    ev = db.get(OutboxEvent, ev.id)
    ev.processed_at = None
    publish(db, "booking.created", {"booking_id": b.id}, workspace_id=ws.id)
    db.commit()
    process_outbox(db)
    assert len(db.scalars(select(NotificationJob).where(NotificationJob.booking_id == b.id)).all()) == len(jobs1)
    N.send_due(db)
    N.send_due(db)
    sent = db.scalars(select(SentMessage)).all()
    assert len(sent) == 2
    inv_mail = next(m for m in sent if m.recipient == "bob@example.com")
    assert "EST" in inv_mail.body  # invitee sees New York time
    assert any(a["filename"].endswith(".ics") and "BEGIN:VCALENDAR" in a["content"] for a in inv_mail.attachments_json)
    assert "/booking/" in inv_mail.body and "calendar.google.com" in inv_mail.body
    host_mail = next(m for m in sent if m.recipient == user.email)
    assert "GMT" in host_mail.body  # host sees Europe/London time
    assert db.scalar(select(InAppNotification).where(InAppNotification.user_id == user.id,
                                                     InAppNotification.kind == "new_booking"))
    assert all(j.status == "sent" and j.provider_id for j in db.scalars(select(NotificationJob)).all())


def test_host_email_pref_respected(db, host):
    user, ws = host
    user.notification_prefs_json = {"new_booking_email": False}
    db.commit()
    b = make_booking(db, user, ws)
    publish(db, "booking.created", {"booking_id": b.id}, workspace_id=ws.id)
    db.commit()
    process_outbox(db)
    assert {j.recipient for j in db.scalars(select(NotificationJob)).all()} == {"bob@example.com"}
    assert db.scalar(select(InAppNotification).where(InAppNotification.kind == "new_booking"))


def test_cancelled_booking_reminders_cancelled(db, host):
    user, ws = host
    add_wf(db, ws, user)
    add_wf(db, ws, user, name="Thanks", trigger="after_event", offset_minutes=60)
    b = make_booking(db, user, ws)
    publish(db, "booking.created", {"booking_id": b.id}, workspace_id=ws.id)
    db.commit()
    process_outbox(db)
    reminder = db.scalar(select(NotificationJob).where(NotificationJob.trigger == "wf:before_event"))
    assert reminder.status == "scheduled"
    assert reminder.scheduled_at == b.start_at - dt.timedelta(minutes=60)
    b.status, b.cancel_reason, b.cancelled_by = "cancelled", "Conflict came up", "invitee"
    publish(db, "booking.cancelled", {"booking_id": b.id}, workspace_id=ws.id)
    db.commit()
    process_outbox(db)
    db.commit(); db.expire_all()
    assert {j.status for j in db.scalars(select(NotificationJob).where(NotificationJob.trigger.like("wf:%"))).all()} == {"cancelled"}
    cancel_mails = db.scalars(select(NotificationJob).where(NotificationJob.trigger == "booking_cancelled")).all()
    assert {j.recipient for j in cancel_mails} == {"bob@example.com", user.email}
    assert "Conflict came up" in cancel_mails[0].body
    assert "METHOD:CANCEL" in cancel_mails[0].attachments_json[0]["content"]
    # even if a reminder somehow stayed scheduled, the sender refuses to send it
    reminder = db.get(NotificationJob, reminder.id)
    reminder.status, reminder.scheduled_at = "scheduled", utcnow() - dt.timedelta(minutes=1)
    db.commit()
    N.send_due(db)
    assert db.get(NotificationJob, reminder.id).status == "cancelled"
    assert not db.scalar(select(SentMessage).where(SentMessage.subject.like("Reminder%")))


def test_reschedule_cancels_old_and_schedules_new(db, host):
    user, ws = host
    add_wf(db, ws, user)
    old = make_booking(db, user, ws)
    publish(db, "booking.created", {"booking_id": old.id}, workspace_id=ws.id)
    db.commit()
    process_outbox(db)
    new = Booking(workspace_id=ws.id, event_type_id=old.event_type_id, host_id=user.id,
                  start_at=old.start_at + dt.timedelta(days=1), end_at=old.end_at + dt.timedelta(days=1),
                  timezone=old.timezone, status="confirmed", manage_token=secrets.token_urlsafe(16),
                  rescheduled_from_id=old.id)
    new.invitees.append(Invitee(name="Bob Invitee", email="bob@example.com", timezone="America/New_York"))
    db.add(new)
    db.flush()
    old.status = "rescheduled"
    publish(db, "booking.rescheduled", {"booking_id": new.id, "old_booking_id": old.id}, workspace_id=ws.id)
    db.commit()
    process_outbox(db)
    old_rem = db.scalar(select(NotificationJob).where(NotificationJob.booking_id == old.id, NotificationJob.trigger == "wf:before_event"))
    new_rem = db.scalar(select(NotificationJob).where(NotificationJob.booking_id == new.id, NotificationJob.trigger == "wf:before_event"))
    assert old_rem.status == "cancelled" and new_rem.status == "scheduled"
    assert new_rem.scheduled_at == new.start_at - dt.timedelta(minutes=60)
    resched = db.scalars(select(NotificationJob).where(NotificationJob.trigger == "booking_rescheduled")).all()
    assert len(resched) == 2 and len(resched[0].attachments_json) == 2


def test_template_variable_rendering(db, host):
    user, ws = host
    b = make_booking(db, user, ws, start=dt.datetime(2026, 11, 18, 14, 0))
    v = W.booking_variables(db, b, "America/New_York")
    out = W.render_template("Hi {invitee_name}! {event_name} with {host_name} at {start_time} {timezone} "
                            "{location} {unknown_var} {cancel_url}", v)
    assert "Hi Bob Invitee!" in out and "Discovery call with Alice Host" in out
    assert "09:00 EST" in out and "America/New_York" in out and "+1 555 0100" in out
    assert "{unknown_var}" in out and "/booking/" in out
    v_london = W.booking_variables(db, b, "Europe/London")
    assert "14:00 GMT" in v_london["start_time"]
    p = W.preview({"subject_template": "Hello {invitee_name}", "body_template": "<b>{event_name}</b>"})
    assert p["subject"] == "Hello Alex Example" and "&lt;b&gt;" in p["html"]  # user text escaped in HTML email


def test_quiet_hours_deferral(db, host):
    user, ws = host
    ws.settings_json = {"quiet_hours": {"start": "21:00", "end": "08:00", "tz": "Europe/London"}}
    db.commit()
    late = dt.datetime(2026, 11, 18, 22, 30)  # 22:30 London (GMT) -> next day 08:00
    assert W.apply_quiet_hours(late, ws) == dt.datetime(2026, 11, 19, 8, 0)
    early = dt.datetime(2026, 11, 18, 6, 15)
    assert W.apply_quiet_hours(early, ws) == dt.datetime(2026, 11, 18, 8, 0)
    noon = dt.datetime(2026, 11, 18, 12, 0)
    assert W.apply_quiet_hours(noon, ws) == noon
    summer = dt.datetime(2026, 7, 1, 21, 30)  # 22:30 BST -> 08:00 BST = 07:00 UTC
    assert W.apply_quiet_hours(summer, ws) == dt.datetime(2026, 7, 2, 7, 0)
    # integration: an immediate workflow triggered during quiet hours is deferred
    add_wf(db, ws, user, trigger="invitee_created", offset_minutes=0, name="Welcome")
    b = make_booking(db, user, ws)
    jobs = W.schedule_for_booking(db, b, "invitee_created", now=late)
    db.commit()
    assert len(jobs) == 1 and jobs[0].scheduled_at == dt.datetime(2026, 11, 19, 8, 0)


def test_send_retry_backoff_and_failure(db, host, monkeypatch):
    from app.services import messaging

    def boom(*a, **k):
        raise messaging.MessagingError("SMTP down")

    monkeypatch.setattr(messaging, "send_email", boom)
    job = N.create_job(db, dedupe_key="t1", recipient="x@example.com", subject="s", body="b", trigger="test")
    db.commit()
    now = utcnow()
    for i in range(N.MAX_ATTEMPTS):
        assert N.send_job(db, job, now) in ("scheduled", "failed")
        db.commit()
        if job.status == "scheduled":
            assert job.next_attempt_at == now + dt.timedelta(minutes=2 ** i)
    assert job.status == "failed" and job.attempts == N.MAX_ATTEMPTS and "SMTP down" in job.error
    assert N.create_job(db, dedupe_key="t1", recipient="x", subject="s", body="b", trigger="test") is None
    monkeypatch.undo()
    N.retry_job(db, job)
    N.send_job(db, job)
    db.commit()
    assert job.status == "sent"


def test_sms_requires_consent_and_respects_stop(db, host, client):
    user, ws = host
    add_wf(db, ws, user, channel="sms", subject_template="", body_template="Reminder {event_name} at {start_time}")
    no_consent = make_booking(db, user, ws, phone="+447700900001")
    assert W.schedule_for_booking(db, no_consent, "before_event") == []
    ok = make_booking(db, user, ws, start=no_consent.start_at + dt.timedelta(hours=2), phone="+447700900002",
                      consent={"sms": True})
    jobs = W.schedule_for_booking(db, ok, "before_event")
    db.commit()
    assert len(jobs) == 1 and jobs[0].channel == "sms" and jobs[0].recipient == "+447700900002"
    r = client.post("/webhooks/sms/inbound", data={"From": "+447700900002", "Body": "STOP"})
    assert r.status_code == 200 and "unsubscribed" in r.text
    job = db.get(NotificationJob, jobs[0].id)
    job.scheduled_at = utcnow() - dt.timedelta(minutes=1)
    db.commit(); db.expire_all()
    N.send_due(db)
    db.commit(); db.expire_all()
    assert db.get(NotificationJob, job.id).status == "skipped"


def test_no_show_and_routing_workflows(db, host):
    from app.models import RoutingForm, RoutingSubmission

    user, ws = host
    add_wf(db, ws, user, trigger="no_show", offset_minutes=0, name="Missed you",
           subject_template="We missed you", body_template="Rebook: {reschedule_url}")
    add_wf(db, ws, user, trigger="routing_submitted", audience="host", offset_minutes=0, name="Lead",
           subject_template="New lead {invitee_name}", body_template="A form was submitted")
    b = make_booking(db, user, ws)
    b.no_show = True
    publish(db, "booking.no_show", {"booking_id": b.id}, workspace_id=ws.id)
    form = RoutingForm(workspace_id=ws.id, owner_user_id=user.id, name="Qualify", slug="qualify",
                       form_schema_json=[{"id": "f1", "type": "email", "label": "Email"},
                                         {"id": "f2", "type": "text", "label": "Your name"}])
    db.add(form)
    db.flush()
    sub = RoutingSubmission(workspace_id=ws.id, form_id=form.id, answers_json={"f1": "lead@example.com", "f2": "Lea"})
    db.add(sub)
    db.flush()
    publish(db, "routing.submitted", {"submission_id": sub.id}, workspace_id=ws.id)
    db.commit()
    process_outbox(db)
    assert db.scalar(select(NotificationJob).where(NotificationJob.trigger == "wf:no_show")).recipient == "bob@example.com"
    lead = db.scalar(select(NotificationJob).where(NotificationJob.trigger == "wf:routing_submitted"))
    assert lead.recipient == user.email and lead.subject == "New lead Lea"


def test_workflow_ui(db, host, client, login):
    user, ws = host
    login(client, user)
    assert client.get("/app/workflows").status_code == 200
    assert client.post("/app/workflows/templates/reminder_24h", follow_redirects=False).status_code == 303
    wf = db.scalar(select(Workflow))
    assert wf.trigger == "before_event" and wf.offset_minutes == 1440
    page = client.get(f"/app/workflows/{wf.id}")
    assert page.status_code == 200 and "Preview" in page.text
    r = client.post(f"/app/workflows/{wf.id}", data={
        "name": "Day-before", "trigger": "before_event", "offset_value": "1", "offset_unit": "days", "channel": "email",
        "audience": "invitee", "subject_template": "See you {start_time}", "body_template": "Hi {invitee_name}",
        "active": "1"}, follow_redirects=False)
    assert r.status_code == 303
    db.commit(); db.expire_all()
    assert db.get(Workflow, wf.id).name == "Day-before"
    r = client.post("/app/workflows/preview", data={"subject_template": "Hi {invitee_name}", "body_template": "x"})
    assert r.json()["subject"] == "Hi Alex Example"
    client.post(f"/app/workflows/{wf.id}/test", follow_redirects=False)
    db.commit()
    assert db.scalar(select(SentMessage).where(SentMessage.recipient == user.email))
    assert client.post("/app/workflows/quiet-hours", data={"enabled": "1", "start": "20:00", "end": "07:00"},
                       follow_redirects=False).status_code == 303
    assert client.get(f"/app/workflows/{wf.id}/runs").status_code == 200
    assert client.get("/app/workflows/runs").status_code == 200


def test_managed_workflow_locked_for_members(db, host, client, login):
    from app.services.bootstrap import add_user_to_workspace

    user, ws = host
    wf = add_wf(db, ws, user, managed=True, name="Company reminder")
    member = add_user_to_workspace(db, ws, "mem@example.com", "Mem", role="member", password="password123")
    db.commit()
    login(client, member)
    r = client.post(f"/app/workflows/{wf.id}", data={"name": "hacked", "trigger": "before_event", "channel": "email",
                                                      "audience": "invitee", "subject_template": "x", "body_template": "y"})
    assert r.status_code == 403
    db.commit(); db.expire_all()
    assert db.get(Workflow, wf.id).name == "Company reminder"


def test_stop_applies_to_future_bookings_and_start_resubscribes(db, host, client):
    user, ws = host
    add_wf(db, ws, user, channel="sms", subject_template="", body_template="Reminder {event_name}")
    first = make_booking(db, user, ws, phone="+447700900009", consent={"sms": True})
    client.post("/webhooks/sms/inbound", data={"From": "+447700900009", "Body": "stop"})
    # a later booking with fresh consent from the same number is still suppressed (opt-out wins)
    later = make_booking(db, user, ws, start=first.start_at + dt.timedelta(hours=3), phone="+447700900009",
                         consent={"sms": True})
    jobs = W.schedule_for_booking(db, later, "before_event")
    db.commit()
    for j in jobs:
        j.scheduled_at = utcnow() - dt.timedelta(minutes=1)
    db.commit()
    db.expire_all()
    N.send_due(db)
    db.commit(); db.expire_all()
    assert jobs and [db.get(NotificationJob, j.id).status for j in jobs] == ["skipped"] * len(jobs)
    assert not db.scalar(select(SentMessage).where(SentMessage.channel == "sms"))
    r = client.post("/webhooks/sms/inbound", data={"From": "+447700900009", "Body": "START"})
    assert "resubscribed" in r.text
    db.commit(); db.expire_all()
    assert all(not (i.consent_json or {}).get("sms_opt_out")
               for i in db.scalars(select(Invitee).where(Invitee.phone == "+447700900009")).all())


def test_sms_workflows_need_plan_feature(db, host):
    user, ws = host
    ws.plan_id = "free"
    db.commit()
    add_wf(db, ws, user, channel="sms", body_template="x")
    b = make_booking(db, user, ws, phone="+447700900010", consent={"sms": True})
    assert W.schedule_for_booking(db, b, "before_event") == []


def test_job_claimed_by_another_worker_is_not_sent_twice(db, host):
    """Duplicate-send prevention: a stale copy of a job that another worker already attempted is skipped."""
    from app.db import SessionLocal

    user, ws = host
    job = N.create_job(db, dedupe_key="dup-1", recipient="x@example.com", subject="Hi", body="b", trigger="host_alert",
                       workspace_id=ws.id)
    db.commit()
    assert N.create_job(db, dedupe_key="dup-1", recipient="x@example.com", subject="Hi", body="b",
                        trigger="host_alert") is None  # dedupe key
    stale = SessionLocal()
    try:
        stale_job = stale.get(NotificationJob, job.id)  # worker B loads the job ("scheduled", attempts 0)
        stale.expunge(stale_job)
        stale.rollback()
        assert N.send_job(db, job) == "sent"  # worker A sends it
        db.commit()
        stale.add(stale_job)  # B still holds its stale in-memory copy
        assert N.send_job(stale, stale_job) == "sent"  # B notices it was already handled
        stale.commit()
    finally:
        stale.close()
    assert len(db.scalars(select(SentMessage).where(SentMessage.recipient == "x@example.com")).all()) == 1
