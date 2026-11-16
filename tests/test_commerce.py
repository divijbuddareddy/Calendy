"""Meeting packages, invoices, payment links (PRD 12 P3) and self-service subscription billing (ACC-005)."""
import datetime as dt
import json
import re
import secrets
import types

import pytest
from sqlalchemy import select

from app.events import process_outbox
from app.models import (
    Booking,
    EventType,
    Invitee,
    OutboxEvent,
    SentMessage,
    Subscription,
    UsageEvent,
    Workspace,
    WorkspaceMember,
    utcnow,
)
from app.models_commerce import BillingAccount, CommerceCheckout, Invoice, PackagePurchase, PaymentLink
from app.services import billing as B
from app.services import commerce as C
from app.services import payments as P
from app.services.bootstrap import create_user_with_workspace


@pytest.fixture
def fake_finalize(monkeypatch):
    calls = []

    def finalize_booking(db, booking, now=None):
        calls.append(booking.id)
        if booking.host_notes == "slot-gone":
            raise RuntimeError("SLOT_NO_LONGER_AVAILABLE")
        booking.status = "confirmed"
        booking.hold_expires_at = None
        db.commit()
        return booking

    monkeypatch.setattr(P, "_booking_mod", lambda: types.SimpleNamespace(finalize_booking=finalize_booking,
                                                                          fail_pending_booking=lambda *a, **k: None))
    return calls


def _et(db, user, ws, price=5000):
    db.commit()
    et = EventType(workspace_id=ws.id, owner_user_id=user.id, name="Coaching", slug="coach-" + secrets.token_hex(3),
                   duration=60, price_minor=price, currency="USD", payment_provider="stripe")
    db.add(et)
    db.commit()
    return et


_h = iter(range(1, 10_000))


def _pending_booking(db, user, ws, et, notes=None):
    start = (utcnow() + dt.timedelta(days=3, hours=next(_h))).replace(minute=0, second=0, microsecond=0)
    b = Booking(workspace_id=ws.id, event_type_id=et.id, host_id=user.id, start_at=start,
                end_at=start + dt.timedelta(hours=1), status="pending_payment", payment_status="pending",
                manage_token=secrets.token_urlsafe(16), hold_expires_at=utcnow() + dt.timedelta(minutes=5),
                host_notes=notes)
    b.invitees.append(Invitee(name="Client", email="client@example.com"))
    db.add(b)
    db.commit()
    return b


def _last_code(db, email):
    db.commit()
    msg = db.scalars(select(SentMessage).where(SentMessage.recipient == email).order_by(SentMessage.created_at.desc())).all()
    for m in msg:
        found = re.search(r"[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}", m.body or "")
        if found:
            return found.group(0)
    return None


def test_package_purchase_and_redemption(db, host, client, login, fake_finalize):
    user, ws = host
    et = _et(db, user, ws)
    login(client, user)
    r = client.post("/app/payments/packages", data={"event_type_id": et.id, "name": "3 sessions", "sessions": "3",
                                                    "price": "120.00", "currency": "USD"}, follow_redirects=True)
    assert r.status_code == 200 and "3 sessions" in r.text
    from app.models_commerce import PackageProduct

    prod = db.scalar(select(PackageProduct))
    assert prod.price_minor == 12000
    client.cookies.clear()
    page = client.get(f"/p/package/{prod.public_token}")
    assert page.status_code == 200 and "$120.00" in page.text
    r = client.post(f"/p/package/{prod.public_token}", data={"name": "Client", "email": "client@example.com"},
                    follow_redirects=False)
    assert r.status_code == 303 and "/pay/c/" in r.headers["location"] and r.headers["location"].endswith("/mock")
    assert "Simulate card decline" in client.get(r.headers["location"]).text
    r = client.post(r.headers["location"], data={"outcome": "success"}, follow_redirects=True)
    assert "Payment received" in r.text
    code = _last_code(db, "client@example.com")
    assert code
    pur = db.scalar(select(PackagePurchase))
    assert pur.status == "active" and code.replace("-", "") not in (pur.code_hash or "")  # stored hashed

    # redeem on a paid booking's pay page: booking confirmed without payment
    b1 = _pending_booking(db, user, ws, et)
    assert "Have a package code?" in client.get(f"/pay/{b1.manage_token}").text
    r = client.post(f"/pay/{b1.manage_token}/redeem", data={"code": code.lower()}, follow_redirects=False)
    assert r.headers["location"] == f"/booking/{b1.manage_token}/confirmed"
    db.commit(); db.expire_all()
    assert db.get(Booking, b1.id).status == "confirmed" and db.get(Booking, b1.id).payment_status == "paid"
    assert db.get(PackagePurchase, pur.id).sessions_used == 1
    # same booking cannot redeem twice; wrong code rejected; other event type rejected
    assert client.post(f"/pay/{b1.manage_token}/redeem", data={"code": code}, follow_redirects=False).status_code == 303
    b2 = _pending_booking(db, user, ws, et)
    client.post(f"/pay/{b2.manage_token}/redeem", data={"code": "AAAA-BBBB-CCCC"})
    db.commit(); db.expire_all()
    assert db.get(Booking, b2.id).status == "pending_payment"
    other_et = _et(db, user, ws)
    b3 = _pending_booking(db, user, ws, other_et)
    client.post(f"/pay/{b3.manage_token}/redeem", data={"code": code})
    db.commit(); db.expire_all()
    assert db.get(Booking, b3.id).status == "pending_payment"
    # failed finalisation gives the session back
    b4 = _pending_booking(db, user, ws, et, notes="slot-gone")
    client.post(f"/pay/{b4.manage_token}/redeem", data={"code": code})
    db.commit(); db.expire_all()
    assert db.get(PackagePurchase, pur.id).sessions_used == 1
    # use up the package
    for _ in range(2):
        b = _pending_booking(db, user, ws, et)
        client.post(f"/pay/{b.manage_token}/redeem", data={"code": code})
    db.commit(); db.expire_all()
    assert db.get(PackagePurchase, pur.id).status == "exhausted"
    b5 = _pending_booking(db, user, ws, et)
    r = client.post(f"/pay/{b5.manage_token}/redeem", data={"code": code}, follow_redirects=True)
    assert "no sessions left" in r.text
    # cancelling a redeemed booking returns the session
    from app.events import publish

    db.get(Booking, b1.id).status = "cancelled"
    publish(db, "booking.cancelled", {"booking_id": b1.id}, workspace_id=ws.id)
    db.commit()
    process_outbox(db)
    db.commit(); db.expire_all()
    p = db.get(PackagePurchase, pur.id)
    assert p.sessions_used == 2 and p.status == "active"


def test_invoice_lifecycle(db, host, client, login):
    user, ws = host
    login(client, user)
    r = client.post("/app/payments/invoices", data={
        "recipient_email": "payer@example.com", "recipient_name": "Payer Co", "currency": "GBP",
        "item_description": ["Workshop", "Travel"], "item_quantity": ["2", "1"], "item_amount": ["100", "25.50"],
    }, follow_redirects=True)
    assert "saved as draft" in r.text
    inv = db.scalar(select(Invoice))
    assert inv.status == "draft" and inv.amount_minor == 22550 and inv.number == "INV-0001"
    assert client.get(f"/p/invoice/{inv.public_token}").status_code == 404  # drafts aren't public
    client.post(f"/app/payments/invoices/{inv.id}/send")
    db.commit(); db.expire_all()
    inv = db.get(Invoice, inv.id)
    assert inv.status == "sent"
    mail = db.scalar(select(SentMessage).where(SentMessage.recipient == "payer@example.com"))
    assert f"/p/invoice/{inv.public_token}" in mail.body and "INV-0001" in mail.subject
    client.cookies.clear()
    page = client.get(f"/p/invoice/{inv.public_token}")
    assert "£225.50" in page.text and "Pay" in page.text
    r = client.post(f"/p/invoice/{inv.public_token}", follow_redirects=False)
    r = client.post(r.headers["location"], data={"outcome": "success"}, follow_redirects=False)
    assert r.headers["location"].endswith("/done")
    db.commit(); db.expire_all()
    inv = db.get(Invoice, inv.id)
    assert inv.status == "paid" and inv.paid_at
    assert "Paid" in client.get(f"/p/invoice/{inv.public_token}").text
    # paid invoices can't be voided or paid twice
    with pytest.raises(C.CommerceError):
        C.void_invoice(db, inv)
    assert db.scalar(select(OutboxEvent).where(OutboxEvent.topic == "payment.succeeded"))
    # void a second invoice
    login(client, user)
    client.post("/app/payments/invoices", data={"recipient_email": "x@example.com", "currency": "USD", "send_now": "1",
                                                "item_description": ["A"], "item_quantity": ["1"], "item_amount": ["10"]})
    db.commit()
    inv2 = db.scalar(select(Invoice).where(Invoice.number == "INV-0002"))
    assert inv2.status == "sent"
    client.post(f"/app/payments/invoices/{inv2.id}/void")
    db.commit(); db.expire_all()
    assert db.get(Invoice, inv2.id).status == "void"
    client.cookies.clear()
    r = client.post(f"/p/invoice/{inv2.public_token}", follow_redirects=False)
    assert r.headers["location"] == f"/p/invoice/{inv2.public_token}"  # cannot pay a void invoice


def test_payment_link_and_tenant_isolation(db, host, client, login):
    user, ws = host
    other, ows = create_user_with_workspace(db, "eve@example.com", "Eve", password="password123", slug="eve")
    db.commit()
    login(client, user)
    client.post("/app/payments/links", data={"description": "Deposit", "amount": "50", "currency": "EUR"})
    link = db.scalar(select(PaymentLink))
    assert link.amount_minor == 5000
    page = client.get("/app/payments?tab=links").text
    assert f"/p/link/{link.public_token}" in page
    for _ in range(2):
        r = client.post(f"/p/link/{link.public_token}", data={"email": "buyer@example.com"}, follow_redirects=False)
        client.post(r.headers["location"], data={"outcome": "success"})
    db.commit(); db.expire_all()
    assert db.get(PaymentLink, link.id).paid_count == 2
    # decline does not count
    r = client.post(f"/p/link/{link.public_token}", data={}, follow_redirects=False)
    client.post(r.headers["location"], data={"outcome": "fail"})
    db.commit(); db.expire_all()
    assert db.get(PaymentLink, link.id).paid_count == 2
    assert "€100.00" in client.get("/app/payments").text
    client.post(f"/app/payments/links/{link.id}/toggle")
    db.commit(); db.expire_all()
    assert not db.get(PaymentLink, link.id).active
    # another workspace cannot touch it
    login(client, other)
    assert client.post(f"/app/payments/links/{link.id}/toggle").status_code == 404
    assert "Deposit" not in client.get("/app/payments?tab=links").text


def test_stripe_webhook_routes_commerce_checkout(db, host):
    user, ws = host
    db.commit()
    link = C.create_link(db, ws.id, user, "Thing", "10", "USD")
    co = C.start_checkout(db, workspace_id=ws.id, kind="link", ref_id=link.id, amount=1000, currency="USD",
                          title="Thing", owner_user_id=user.id)
    db.commit()
    P.handle_stripe_event(db, {"type": "checkout.session.completed",
                               "data": {"object": {"id": "cs_x", "payment_status": "paid",
                                                   "metadata": {"commerce_checkout_id": co.id}}}})
    db.commit(); db.expire_all()
    assert db.get(CommerceCheckout, co.id).status == "succeeded" and db.get(PaymentLink, link.id).paid_count == 1


# ------------------------------------------------------------------ subscription billing
def test_self_service_subscription_mock(db, host, client, login):
    user, ws = host
    ws.plan_id = "free"
    teammate, _ = create_user_with_workspace(db, "tm@example.com", "Teammate", password="password123", slug="tm")
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=teammate.id, role="member", status="active"))
    db.commit()
    login(client, user)
    page = client.get("/app/billing")
    assert page.status_code == 200 and "Subscribe" in page.text and "Usage" in page.text
    r = client.post("/app/billing/plan", data={"plan": "standard"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/app/billing/checkout/mock?session=mock_bcs_")
    page = client.get(r.headers["location"])
    assert "2 seats" in page.text and "$20.00" in page.text
    session = r.headers["location"].split("session=")[1]
    client.post("/app/billing/checkout/mock", data={"session": session, "outcome": "success"})
    db.commit(); db.expire_all()
    assert db.get(Workspace, ws.id).plan_id == "standard"
    sub = B.current_subscription(db, ws.id)
    assert sub.plan == "standard" and sub.seats == 2 and sub.status == "active" and sub.period_end
    # session can't be replayed
    assert "no longer valid" in client.post("/app/billing/checkout/mock", data={"session": session},
                                            follow_redirects=True).text
    # upgrade applies immediately
    client.post("/app/billing/plan", data={"plan": "teams"})
    db.commit(); db.expire_all()
    assert db.get(Workspace, ws.id).plan_id == "teams"
    # downgrade is scheduled for period end
    client.post("/app/billing/plan", data={"plan": "standard"})
    db.commit(); db.expire_all()
    assert db.get(Workspace, ws.id).plan_id == "teams" and B.account(db, ws.id).scheduled_plan == "standard"
    # seats follow active members
    m = db.scalar(select(WorkspaceMember).where(WorkspaceMember.user_id == teammate.id, WorkspaceMember.workspace_id == ws.id))
    m.status = "deactivated"
    db.commit()
    B.run_billing_cycle(db)
    db.commit(); db.expire_all()
    assert B.current_subscription(db, ws.id).seats == 1
    # period end -> downgrade applied
    sub = B.current_subscription(db, ws.id)
    sub.period_end = utcnow() - dt.timedelta(minutes=1)
    db.commit()
    B.run_billing_cycle(db)
    db.commit(); db.expire_all()
    assert db.get(Workspace, ws.id).plan_id == "standard" and B.current_subscription(db, ws.id).period_end > utcnow()
    # cancel at period end, then resume, then cancel and let it lapse
    client.post("/app/billing/cancel")
    db.commit(); db.expire_all()
    assert B.account(db, ws.id).cancel_at_period_end
    assert "Keep my subscription" in client.get("/app/billing").text
    client.post("/app/billing/resume")
    db.commit(); db.expire_all()
    assert not B.account(db, ws.id).cancel_at_period_end
    client.post("/app/billing/plan", data={"plan": "free"})
    db.commit(); db.expire_all()
    assert B.account(db, ws.id).cancel_at_period_end and db.get(Workspace, ws.id).plan_id == "standard"
    sub = B.current_subscription(db, ws.id)
    sub.period_end = utcnow() - dt.timedelta(minutes=1)
    db.commit()
    B.run_billing_cycle(db)
    db.commit(); db.expire_all()
    assert db.get(Workspace, ws.id).plan_id == "free" and B.current_subscription(db, ws.id).status == "canceled"
    # enterprise is not self-serve; non-owners can't change plans
    assert "contact sales" in client.post("/app/billing/plan", data={"plan": "enterprise"}, follow_redirects=True).text


def test_billing_requires_owner(db, host, client, login):
    user, ws = host
    m = db.scalar(select(WorkspaceMember).where(WorkspaceMember.user_id == user.id))
    m.role = "admin"
    db.commit()
    login(client, user)
    assert client.get("/app/billing").status_code == 200  # admins can view
    assert client.post("/app/billing/plan", data={"plan": "standard"}).status_code == 403
    m.role = "member"
    db.commit()
    assert client.get("/app/billing").status_code == 403


def test_billing_stripe_webhooks(db, host, monkeypatch):
    user, ws = host
    db.commit()
    monkeypatch.setattr(B, "_stripe", lambda method, path, **kw: {"id": "sub_1", "current_period_end": 1900000000})
    P.handle_stripe_event(db, {"type": "checkout.session.completed", "data": {"object": {
        "mode": "subscription", "subscription": "sub_1", "customer": "cus_1", "client_reference_id": ws.id,
        "metadata": {"billing_workspace_id": ws.id, "plan": "standard"}}}})
    db.commit(); db.expire_all()
    assert db.get(Workspace, ws.id).plan_id == "standard"
    acct = db.scalar(select(BillingAccount).where(BillingAccount.workspace_id == ws.id))
    assert acct.customer_id == "cus_1"
    sub = db.scalar(select(Subscription).where(Subscription.provider_subscription_id == "sub_1"))
    assert sub.period_end.year == 2030
    P.handle_stripe_event(db, {"type": "customer.subscription.updated", "data": {"object": {
        "id": "sub_1", "status": "active", "cancel_at_period_end": True, "current_period_end": 1900000000,
        "items": {"data": [{"quantity": 4}]}, "metadata": {"plan": "teams", "billing_workspace_id": ws.id}}}})
    db.commit(); db.expire_all()
    assert db.get(Workspace, ws.id).plan_id == "teams" and db.get(Subscription, sub.id).seats == 4
    assert db.scalar(select(BillingAccount).where(BillingAccount.workspace_id == ws.id)).cancel_at_period_end
    P.handle_stripe_event(db, {"type": "invoice.payment_failed", "data": {"object": {"subscription": "sub_1"}}})
    db.commit(); db.expire_all()
    assert db.get(Subscription, sub.id).status == "past_due"
    P.handle_stripe_event(db, {"type": "customer.subscription.deleted", "data": {"object": {"id": "sub_1"}}})
    db.commit(); db.expire_all()
    assert db.get(Workspace, ws.id).plan_id == "free" and db.get(Subscription, sub.id).status == "canceled"


def test_billing_stripe_checkout_when_keyed(db, host, client, login, monkeypatch):
    from app.config import settings

    user, ws = host
    ws.plan_id = "free"
    db.commit()
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_x")
    seen = {}

    def fake(method, path, **kw):
        seen[(method, path)] = kw
        return {"id": "cs_sub_1", "url": "https://checkout.stripe.com/c/cs_sub_1"}

    monkeypatch.setattr(B, "_stripe", fake)
    login(client, user)
    r = client.post("/app/billing/plan", data={"plan": "standard"}, follow_redirects=False)
    assert r.headers["location"] == "https://checkout.stripe.com/c/cs_sub_1"
    data = seen[("POST", "/checkout/sessions")]["data"]
    assert data["mode"] == "subscription" and data["line_items[0][quantity]"] == 1
    assert data["line_items[0][price_data][recurring][interval]"] == "month"
    assert data["metadata[billing_workspace_id]"] == ws.id


def test_usage_metering(db, host, client, login):
    from app.events import publish
    from app.services.notifications import create_job, send_due
    from app.services.usage import count_usage, usage_summary

    user, ws = host
    et = _et(db, user, ws, price=None)
    b = _pending_booking(db, user, ws, et)
    b.status = "confirmed"
    publish(db, "booking.created", {"booking_id": b.id}, workspace_id=ws.id)
    db.commit()
    process_outbox(db)
    create_job(db, dedupe_key="t1", recipient="a@example.com", subject="s", body="b", trigger="host_alert",
               workspace_id=ws.id)
    create_job(db, dedupe_key="t2", recipient="+1555", subject=None, body="b", trigger="host_alert", channel="sms",
               workspace_id=ws.id)
    db.commit()
    send_due(db)
    count_usage(ws.id, "api_calls", 3)
    login(client, user)
    page = client.get("/app/billing").text
    assert "API calls" in page
    db.commit()
    usage = {u["meter"]: u["period"] for u in usage_summary(db, ws.id)}
    assert usage["bookings_created"] == 1 and usage["api_calls"] == 3 and usage["sms_sent"] == 1
    assert usage["emails_sent"] >= 1
