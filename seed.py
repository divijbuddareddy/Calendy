"""Seed a demo workspace so every feature can be explored immediately.

    python seed.py            # creates demo data in DATABASE_URL (default ./scheduler.db)

Login: demo@example.com / demo1234   (teammates: sam@example.com, priya@example.com / demo1234)
"""
import datetime as dt
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.main import app  # noqa: F401  (imports routers -> registers outbox subscribers / periodic tasks)
from app.models import (
    EventQuestion,
    EventType,
    EventTypeHost,
    RoutingForm,
    Team,
    TeamMember,
    User,
    utcnow,
)
from app.services.bootstrap import add_user_to_workspace, create_user_with_workspace


def main():
    init_db()
    db = SessionLocal()
    if db.scalar(select(User).where(User.email == "demo@example.com")):
        print("Demo data already present. Login: demo@example.com / demo1234")
        return
    tz = "Europe/London"
    user, ws = create_user_with_workspace(
        db, "demo@example.com", "Demo Host", password="demo1234", timezone=tz, slug="demo", verified=True,
        workspace_name="Demo Co",
    )
    user.onboarding_completed = True
    user.headline = "Product consultant · happy to chat"
    sam = add_user_to_workspace(db, ws, "sam@example.com", "Sam Rivera", "admin", "demo1234", "America/New_York")
    priya = add_user_to_workspace(db, ws, "priya@example.com", "Priya Shah", "member", "demo1234", "Asia/Kolkata")
    db.flush()

    # Demo calendar with a busy block
    from app.models import ExternalCalendarEvent
    from app.services.providers import sync as cal_sync
    from app.services.providers import registry

    conn = cal_sync.connect_mock_calendar(db, user, ws.id)
    personal = next(c for c in registry.calendars_of(db, conn) if c.display_name == "Personal")
    z = ZoneInfo(tz)
    tomorrow = (dt.datetime.now(z) + dt.timedelta(days=1)).date()
    s = dt.datetime.combine(tomorrow, dt.time(12, 0), z).astimezone(dt.timezone.utc).replace(tzinfo=None)
    db.add(ExternalCalendarEvent(calendar_id=personal.id, external_id="seed-lunch", title="Lunch with Sam",
                                 start_at=s, end_at=s + dt.timedelta(hours=1)))

    disc = EventType(
        workspace_id=ws.id, owner_user_id=user.id, name="30-minute discovery call", slug="discovery-call",
        description="Discuss requirements and next steps.", duration=30, durations_json=[15, 30, 60],
        location_json=[{"type": "google_meet", "value": ""}], min_notice_minutes=240, max_days_ahead=30,
        buffer_after=15, color="#3b47d6",
        cancellation_policy_json={"allow_cancel": True, "allow_reschedule": True, "min_notice_minutes": 60,
                                  "policy_text": "Please cancel at least 1 hour before."},
    )
    db.add(disc)
    db.flush()
    db.add_all([
        EventQuestion(event_type_id=disc.id, type="company", label="Company", required=False, sort_order=1),
        EventQuestion(event_type_id=disc.id, type="select", label="Topic", required=True, sort_order=2,
                      options_json=["Pricing", "Integration", "Other"]),
        EventQuestion(event_type_id=disc.id, type="consent", label="I agree to the privacy notice", required=True,
                      sort_order=3),
    ])
    webinar = EventType(workspace_id=ws.id, owner_user_id=user.id, name="Group onboarding webinar", slug="webinar",
                        kind="group", duration=60, max_invitees=10, min_notice_minutes=60, waitlist_enabled=True,
                        location_json=[{"type": "zoom", "value": ""}], color="#12795a")
    paid = EventType(workspace_id=ws.id, owner_user_id=user.id, name="Paid strategy session", slug="strategy",
                     duration=60, price_minor=5000, currency="GBP", payment_provider="stripe",
                     location_json=[{"type": "custom_url", "value": "https://example.com/room"}], color="#b42318")
    db.add_all([webinar, paid])

    team = Team(workspace_id=ws.id, name="Sales", slug="sales", description="Talk to our sales team")
    db.add(team)
    db.flush()
    for i, u in enumerate([user, sam, priya]):
        db.add(TeamMember(team_id=team.id, user_id=u.id, role="admin" if u is user else "member", priority=3 - i))
    rr = EventType(workspace_id=ws.id, owner_user_id=user.id, team_id=team.id, name="Sales demo", slug="sales-demo",
                   kind="round_robin", rr_strategy="equal", duration=30, min_notice_minutes=60,
                   location_json=[{"type": "google_meet", "value": ""}], visibility="team")
    coll = EventType(workspace_id=ws.id, owner_user_id=user.id, team_id=team.id, name="Panel interview",
                     slug="panel", kind="collective", duration=45, min_notice_minutes=60,
                     location_json=[{"type": "google_meet", "value": ""}], visibility="team")
    db.add_all([rr, coll])
    db.flush()
    for et in (rr, coll):
        for i, u in enumerate([user, sam, priya] if et is rr else [user, sam]):
            db.add(EventTypeHost(event_type_id=et.id, user_id=u.id, priority=3 - i, required=True))

    db.add(RoutingForm(
        workspace_id=ws.id, owner_user_id=user.id, name="Contact sales", slug="contact-sales",
        form_schema_json=[
            {"id": "name", "type": "text", "label": "Name", "required": True},
            {"id": "email", "type": "email", "label": "Email", "required": True},
            {"id": "size", "type": "number", "label": "Company size", "required": True},
        ],
        rules_json=[{"id": "r1", "logic": "and", "conditions": [{"field": "size", "op": "gt", "value": "50"}],
                     "destination": {"type": "event_type", "value": rr.id}}],
        fallback_json={"type": "event_type", "value": disc.id},
    ))
    db.commit()

    # A few real bookings through the booking engine
    from app.services.availability import get_available_slots
    from app.services.booking import cancel_booking, create_booking

    today = dt.date.today()
    slots = get_available_slots(db, disc, today, today + dt.timedelta(days=14), tz)
    q = {q.label: q.id for q in disc.questions}
    people = [("Alex Example", "alex@example.com"), ("Jordan Lee", "jordan@acme.com"), ("Taylor Kim", "taylor@acme.com")]
    made = []
    for i, (n, e) in enumerate(people):
        if len(slots) > i * 5:
            b = create_booking(db, disc, slots[i * 5].start, {"name": n, "email": e, "timezone": "America/New_York"},
                               {q["Topic"]: "Pricing", q["I agree to the privacy notice"]: True},
                               timezone="America/New_York", utm={"utm_source": "newsletter"})
            made.append(b)
    if made:
        cancel_booking(db, made[-1], "invitee", reason="Something came up")
    from app.worker import run_all_once

    run_all_once()
    db.close()
    print("Seeded. Login at http://127.0.0.1:8000/login  ->  demo@example.com / demo1234")
    print("Public booking page: http://127.0.0.1:8000/demo")


if __name__ == "__main__":
    main()
