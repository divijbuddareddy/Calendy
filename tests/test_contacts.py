import datetime as dt

from sqlalchemy import func, select

from app.events import process_outbox, publish
from app.models import Booking, Contact, EventType, Invitee, RoutingForm, RoutingSubmission, utcnow
from app.security import opaque_token
from app.services.contacts import normalize_email


def _booking(db, ws, et, host_id, email, name="Sam", days=2, **inv):
    start = utcnow() + dt.timedelta(days=days)
    b = Booking(workspace_id=ws.id, event_type_id=et.id, host_id=host_id, start_at=start,
                end_at=start + dt.timedelta(minutes=30), status="confirmed", manage_token=opaque_token(), exclusive=False)
    db.add(b)
    db.flush()
    db.add(Invitee(booking_id=b.id, name=name, email=email, **inv))
    db.flush()
    return b


def _et(db, user, ws):
    et = EventType(workspace_id=ws.id, owner_user_id=user.id, name="Intro", slug="intro", duration=30)
    db.add(et)
    db.flush()
    return et


def test_normalize_email():
    assert normalize_email("  Sam.Smith@Gmail.COM ") == "sam.smith@gmail.com"  # dots kept


def test_contact_dedupe_via_outbox(db, host):
    user, ws = host
    et = _et(db, user, ws)
    b1 = _booking(db, ws, et, user.id, "Sam@Example.com", phone="+44 1", company="Acme", timezone="Europe/London")
    publish(db, "booking.created", {"booking_id": b1.id}, workspace_id=ws.id)
    b2 = _booking(db, ws, et, user.id, " sam@example.COM", name="Samuel", days=3)
    publish(db, "booking.created", {"booking_id": b2.id}, workspace_id=ws.id)
    b3 = _booking(db, ws, et, user.id, "s.am@example.com", days=4)  # different mailbox: not merged
    publish(db, "booking.created", {"booking_id": b3.id}, workspace_id=ws.id)
    db.commit()
    process_outbox(db)
    rows = db.scalars(select(Contact).where(Contact.workspace_id == ws.id)).all()
    assert sorted(c.email_normalized for c in rows) == ["s.am@example.com", "sam@example.com"]
    sam = next(c for c in rows if c.email_normalized == "sam@example.com")
    assert sam.name == "Sam" and sam.company == "Acme" and sam.phone == "+44 1" and sam.timezone == "Europe/London"
    # idempotent on retry
    publish(db, "booking.created", {"booking_id": b1.id}, workspace_id=ws.id)
    db.commit()
    process_outbox(db)
    assert db.scalar(select(func.count(Contact.id))) == 2


def test_routing_conversion_linked_on_booking(db, host):
    user, ws = host
    et = _et(db, user, ws)
    rf = RoutingForm(workspace_id=ws.id, owner_user_id=user.id, name="F", slug="f", fallback_json={"type": "message", "value": "x"})
    db.add(rf)
    db.flush()
    sub = RoutingSubmission(workspace_id=ws.id, form_id=rf.id, answers_json={"f1": "x@y.com"})
    db.add(sub)
    db.flush()
    b = _booking(db, ws, et, user.id, "x@y.com")
    b.routing_submission_id = sub.id
    publish(db, "booking.created", {"booking_id": b.id}, workspace_id=ws.id)
    db.commit()
    process_outbox(db)
    db.rollback()
    db.expire_all()
    assert db.get(RoutingSubmission, sub.id).booking_id == b.id


def test_contacts_ui(client, db, host, login):
    user, ws = host
    et = _et(db, user, ws)
    b = _booking(db, ws, et, user.id, "pat@acme.com", name="Pat")
    publish(db, "booking.created", {"booking_id": b.id}, workspace_id=ws.id)
    db.commit()
    process_outbox(db)
    login(client, user)
    r = client.get("/app/contacts?q=acme")
    assert r.status_code == 200 and "pat@acme.com" in r.text
    c = db.scalar(select(Contact).where(Contact.email_normalized == "pat@acme.com"))
    r = client.get(f"/app/contacts/{c.id}")
    assert r.status_code == 200 and "Intro" in r.text and "Book another meeting" in r.text
    client.post(f"/app/contacts/{c.id}/edit", data={"name": "Pat Q", "email": "pat@acme.com", "tags": "vip, lead",
                                                    "notes": "Likes demos", "company": "Acme"})
    client.post(f"/app/contacts/{c.id}/custom", data={"key": "Plan", "value": "Gold"})
    client.post(f"/app/contacts/{c.id}/unsubscribe")
    db.rollback()
    db.expire_all()
    c = db.get(Contact, c.id)
    assert c.name == "Pat Q" and c.tags_json == ["vip", "lead"] and c.custom_json == {"Plan": "Gold"} and c.unsubscribed
    assert "pat@acme.com" in client.get("/app/contacts?tag=VIP").text
    assert "pat@acme.com" not in client.get("/app/contacts?tag=other").text
    r = client.get(f"/app/contacts/{c.id}/book?event_type_id={et.id}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/alice/intro?name=Pat+Q&email=pat%40acme.com")
    # manual create + dedupe
    client.post("/app/contacts", data={"email": "NEW@x.com", "name": "New"})
    client.post("/app/contacts", data={"email": "new@x.com ", "name": "Dup"})
    db.rollback()
    db.expire_all()
    assert db.scalar(select(func.count(Contact.id)).where(Contact.email_normalized == "new@x.com")) == 1
    r = client.get("/app/contacts/export.csv")
    assert r.status_code == 200 and "pat@acme.com" in r.text and "custom:Plan" in r.text
    client.post(f"/app/contacts/{c.id}/delete")
    db.rollback()
    db.expire_all()
    assert db.get(Contact, c.id).deleted_at is not None
    assert "pat@acme.com" not in client.get("/app/contacts").text
    assert client.get("/app/contacts/follow-ups").status_code == 200


def test_contacts_tenant_isolation(client, db, host, login):
    from app.services.bootstrap import create_user_with_workspace

    user, ws = host
    other, ows = create_user_with_workspace(db, "o@o.com", "Other", slug="other")
    c = Contact(workspace_id=ows.id, email_normalized="secret@o.com", name="Secret")
    db.add(c)
    db.commit()
    login(client, user)
    assert client.get(f"/app/contacts/{c.id}").status_code == 403
    assert "secret@o.com" not in client.get("/app/contacts").text
