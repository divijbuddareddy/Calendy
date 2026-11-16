"""Paid bookings: checkout, success -> finalize, failure -> slot release, reconciliation refund, refunds."""
import datetime as dt
import secrets
import types

import pytest
from sqlalchemy import select

from app.models import (
    Booking,
    EventType,
    InAppNotification,
    Invitee,
    NotificationJob,
    OutboxEvent,
    PaymentAccount,
    PaymentRecord,
    utcnow,
)
from app.services import payments as P


@pytest.fixture
def fake_booking_service(monkeypatch):
    """Stand-in for app.services.booking so these tests only exercise the payment saga."""
    calls = {"finalize": [], "fail": []}

    def finalize_booking(db, booking, now=None):
        calls["finalize"].append(booking.id)
        if getattr(booking, "_fail_finalize", False) or booking.host_notes == "slot-gone":
            raise RuntimeError("SLOT_NO_LONGER_AVAILABLE")
        booking.status = "confirmed"
        booking.hold_expires_at = None
        db.commit()
        return booking

    def fail_pending_booking(db, booking, reason=""):
        calls["fail"].append((booking.id, reason))
        booking.status = "failed"
        booking.hold_expires_at = None
        db.commit()
        return booking

    fake = types.SimpleNamespace(finalize_booking=finalize_booking, fail_pending_booking=fail_pending_booking)
    monkeypatch.setattr(P, "_booking_mod", lambda: fake)
    return calls


_slot = iter(range(1, 10_000))


def paid_booking(db, user, ws, provider="stripe", connect=True, status="pending_payment"):
    db.commit()
    if connect:
        db.add(PaymentAccount(workspace_id=ws.id, user_id=user.id, provider=provider, account_id=f"acct_mock_{provider}"))
    et = EventType(workspace_id=ws.id, owner_user_id=user.id, name="Paid consult", slug="paid-" + secrets.token_hex(3),
                   duration=30, price_minor=4999, currency="GBP", payment_provider=provider)
    db.add(et)
    db.flush()
    start = (utcnow() + dt.timedelta(days=2, hours=next(_slot))).replace(minute=0, second=0, microsecond=0)
    b = Booking(workspace_id=ws.id, event_type_id=et.id, host_id=user.id, start_at=start,
                end_at=start + dt.timedelta(minutes=30), status=status, payment_status="pending",
                manage_token=secrets.token_urlsafe(16), hold_expires_at=utcnow() + dt.timedelta(minutes=5),
                timezone="Europe/London")
    b.invitees.append(Invitee(name="Payer", email="payer@example.com"))
    db.add(b)
    db.commit()
    return b


def topics(db):
    return [e.topic for e in db.scalars(select(OutboxEvent).order_by(OutboxEvent.created_at)).all()]


def test_checkout_record_is_idempotent(db, host, fake_booking_service):
    user, ws = host
    b = paid_booking(db, user, ws)
    r1 = P.get_or_create_checkout(db, b)
    db.commit()
    r2 = P.get_or_create_checkout(db, b)
    assert r1.id == r2.id and r1.status == "pending" and r1.amount_minor == 4999 and r1.currency == "GBP"
    assert r1.provider_payment_id.startswith("mock_stripe_") and r1.checkout_url.startswith(f"/pay/{b.manage_token}/checkout/mock")
    assert len(db.scalars(select(PaymentRecord)).all()) == 1


def test_payment_success_finalizes_booking(db, host, fake_booking_service):
    user, ws = host
    b = paid_booking(db, user, ws)
    rec = P.get_or_create_checkout(db, b)
    db.commit()
    assert P.complete_payment(db, rec) == "confirmed"
    assert fake_booking_service["finalize"] == [b.id]
    db.commit(); db.expire_all()
    rec = db.get(PaymentRecord, rec.id)
    assert rec.status == "succeeded" and db.get(Booking, b.id).payment_status == "paid"
    assert db.get(Booking, b.id).status == "confirmed"
    assert "payment.succeeded" in topics(db)
    # duplicate provider notification is a no-op
    assert P.complete_payment(db, rec) == "already"
    assert fake_booking_service["finalize"] == [b.id] and topics(db).count("payment.succeeded") == 1


def test_payment_failure_releases_slot(db, host, fake_booking_service):
    user, ws = host
    b = paid_booking(db, user, ws)
    rec = P.get_or_create_checkout(db, b)
    db.commit()
    assert P.fail_payment(db, rec, "Card declined") == "failed"
    assert fake_booking_service["fail"] == [(b.id, "Card declined")]
    db.commit(); db.expire_all()
    assert db.get(PaymentRecord, rec.id).status == "failed"
    bk = db.get(Booking, b.id)
    assert bk.status == "failed" and bk.payment_status == "failed"
    assert "payment.failed" in topics(db)
    # a later success for a failed payment does not resurrect anything silently
    P.fail_payment(db, db.get(PaymentRecord, rec.id), "again")
    assert topics(db).count("payment.failed") == 1


def test_success_but_finalize_fails_triggers_refund_and_alert(db, host, fake_booking_service):
    user, ws = host
    b = paid_booking(db, user, ws)
    b.host_notes = "slot-gone"
    db.commit()
    rec = P.get_or_create_checkout(db, b)
    db.commit()
    assert P.complete_payment(db, rec) == "refunded"
    db.commit(); db.expire_all()
    rec = db.get(PaymentRecord, rec.id)
    assert rec.status == "refunded" and rec.refund_status == "refunded"
    assert db.get(Booking, b.id).payment_status == "refunded"
    assert fake_booking_service["fail"] and fake_booking_service["fail"][0][0] == b.id
    assert db.scalar(select(InAppNotification).where(InAppNotification.kind == "payment_alert"))
    assert db.scalar(select(NotificationJob).where(NotificationJob.recipient == user.email, NotificationJob.trigger == "host_alert"))


def test_cancelled_booking_paid_late_is_refunded(db, host, fake_booking_service):
    user, ws = host
    b = paid_booking(db, user, ws, status="cancelled")
    rec = PaymentRecord(workspace_id=ws.id, booking_id=b.id, provider="stripe", amount_minor=4999, currency="GBP",
                        status="pending", provider_payment_id="mock_stripe_late", idempotency_key="checkout:" + b.id)
    db.add(rec)
    db.commit()
    assert P.complete_payment(db, rec) == "refunded"
    assert fake_booking_service["finalize"] == []


def test_refund_endpoint(db, host, client, login, fake_booking_service):
    user, ws = host
    b = paid_booking(db, user, ws)
    rec = P.get_or_create_checkout(db, b)
    db.commit()
    P.complete_payment(db, rec)
    login(client, user)
    r = client.post(f"/app/payments/{b.id}/refund", headers={"Accept": "application/json"})
    assert r.status_code == 200 and r.json()["status"] == "refunded"
    db.commit(); db.expire_all()
    assert db.get(Booking, b.id).payment_status == "refunded"
    r = client.post(f"/app/payments/{b.id}/refund", headers={"Accept": "application/json"})
    assert r.status_code == 400  # nothing left to refund
    detail = client.get(f"/app/payments/{b.id}").json()
    assert detail["payments"][0]["refund_status"] == "refunded"


def test_pay_page_mock_checkout_flow(db, host, client, fake_booking_service):
    user, ws = host
    b = paid_booking(db, user, ws)
    page = client.get(f"/pay/{b.manage_token}")
    assert page.status_code == 200 and "£49.99" in page.text
    r = client.post(f"/pay/{b.manage_token}/start", follow_redirects=False)
    assert r.status_code == 303 and "/checkout/mock" in r.headers["location"]
    mock_page = client.get(r.headers["location"])
    assert mock_page.status_code == 200 and "Simulate card decline" in mock_page.text
    rec = db.scalar(select(PaymentRecord))
    r = client.post(f"/pay/{b.manage_token}/checkout/mock", data={"record": rec.id, "outcome": "success"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/booking/{b.manage_token}/confirmed"
    assert fake_booking_service["finalize"] == [b.id]
    # visiting the pay page again goes straight to confirmation
    assert client.get(f"/pay/{b.manage_token}", follow_redirects=False).headers["location"].endswith("/confirmed")


def test_pay_page_failure_allows_rebook(db, host, client, fake_booking_service):
    user, ws = host
    b = paid_booking(db, user, ws, provider="paypal")
    client.post(f"/pay/{b.manage_token}/start", follow_redirects=False)
    rec = db.scalar(select(PaymentRecord))
    assert rec.provider == "paypal"
    r = client.post(f"/pay/{b.manage_token}/checkout/mock", data={"record": rec.id, "outcome": "fail"},
                    follow_redirects=False)
    assert r.headers["location"] == f"/pay/{b.manage_token}"
    page = client.get(f"/pay/{b.manage_token}")
    assert "Payment not completed" in page.text and "Choose a time again" in page.text and "/alice/" in page.text
    assert fake_booking_service["fail"][0][0] == b.id


def test_stripe_webhook_event_completes_payment(db, host, fake_booking_service):
    user, ws = host
    b = paid_booking(db, user, ws)
    rec = P.get_or_create_checkout(db, b)
    db.commit()
    P.handle_stripe_event(db, {"id": "evt_1", "type": "checkout.session.completed",
                               "data": {"object": {"id": rec.provider_payment_id, "payment_status": "paid",
                                                   "metadata": {"payment_record_id": rec.id}}}})
    assert fake_booking_service["finalize"] == [b.id]
    b2 = paid_booking(db, user, ws, connect=False)
    rec2 = P.get_or_create_checkout(db, b2)
    db.commit()
    P.handle_stripe_event(db, {"id": "evt_2", "type": "checkout.session.expired",
                               "data": {"object": {"id": rec2.provider_payment_id, "metadata": {}}}})
    assert fake_booking_service["fail"][0][0] == b2.id


def test_revenue_summary(db, host, fake_booking_service):
    user, ws = host
    b = paid_booking(db, user, ws)
    rec = P.get_or_create_checkout(db, b)
    db.commit()
    P.complete_payment(db, rec)
    s = P.revenue_summary(db, ws.id)
    assert s["gross_by_currency"] == {"GBP": 4999} and s["by_event_type"][0]["amount_minor"] == 4999


def test_payment_settings_ui(db, host, client, login):
    user, ws = host
    login(client, user)
    assert client.post("/app/integrations/stripe/connect", follow_redirects=False).status_code == 303
    assert client.post("/app/integrations/paypal/connect", follow_redirects=False).status_code == 303
    accts = db.scalars(select(PaymentAccount)).all()
    assert {a.provider for a in accts} == {"stripe", "paypal"} and all("_mock_" in a.account_id for a in accts)
    assert "Test mode" in client.get("/app/integrations").text
