"""Paid bookings (PRD 12): checkout, success/failure, reconciliation saga, refunds.

State machine (PaymentRecord.status): created -> pending -> succeeded | failed ;
succeeded -> refunded (refund_status mirrors provider) ; refund_pending when an automatic
refund could not be executed (support alerted).

  * complete_payment: mark paid, publish payment.succeeded, then booking.finalize_booking. If the
    booking cannot be finalised (hold expired / slot taken) the saga refunds through the provider
    and alerts the host - money is never kept for a booking that does not exist.
  * fail_payment: publish payment.failed and booking.fail_pending_booking (releases the slot);
    the invitee can rebook.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import audit
from app.config import settings
from app.events import publish
from app.models import Booking, EventType, PaymentAccount, PaymentRecord, User, utcnow
from app.services.providers.payments_mock import MockPaymentProvider, PaymentProviderBase, is_mock_account
from app.worker import periodic

log = logging.getLogger("payments")
PROVIDERS = ("stripe", "paypal")


class PaymentError(Exception):
    pass


# ------------------------------------------------------------------ accounts / adapters
def accounts_for_user(db: Session, user_id: str) -> list[PaymentAccount]:
    return list(db.scalars(select(PaymentAccount).where(
        PaymentAccount.user_id == user_id, PaymentAccount.status == "connected")).all())


def account_for(db: Session, event_type: Optional[EventType], provider: Optional[str] = None) -> Optional[PaymentAccount]:
    if not event_type:
        return None
    provider = provider or event_type.payment_provider or "stripe"
    accts = accounts_for_user(db, event_type.owner_user_id)
    return next((a for a in accts if a.provider == provider), accts[0] if accts else None)


def adapter_for(provider: str, account: Optional[PaymentAccount]) -> PaymentProviderBase:
    if provider == "stripe" and not is_mock_account(account):
        from app.services.providers.payments_stripe import StripeProvider

        if StripeProvider.configured():
            return StripeProvider()
    if provider == "paypal" and not is_mock_account(account):
        from app.services.providers.payments_paypal import PayPalProvider

        if PayPalProvider.configured():
            return PayPalProvider()
    return MockPaymentProvider(provider or "stripe")


def record_adapter(db: Session, record: PaymentRecord) -> tuple[PaymentProviderBase, Optional[PaymentAccount]]:
    booking = db.get(Booking, record.booking_id)
    et = db.get(EventType, booking.event_type_id) if booking else None
    account = account_for(db, et, record.provider)
    if (record.provider_payment_id or "").startswith("mock_"):
        return MockPaymentProvider(record.provider), account
    return adapter_for(record.provider, account), account


def price_of(booking: Booking, et: Optional[EventType]) -> tuple[Optional[int], str]:
    snap = booking.snapshot_json or {}
    amount = snap.get("price_minor") if snap.get("price_minor") is not None else (et.price_minor if et else None)
    currency = (snap.get("currency") or (et.currency if et else None) or "USD").upper()
    return amount, currency


def latest_record(db: Session, booking_id: str) -> Optional[PaymentRecord]:
    return db.scalar(select(PaymentRecord).where(PaymentRecord.booking_id == booking_id)
                     .order_by(PaymentRecord.created_at.desc()))


# ------------------------------------------------------------------ checkout
def get_or_create_checkout(db: Session, booking: Booking, base_url: Optional[str] = None) -> PaymentRecord:
    et = db.get(EventType, booking.event_type_id)
    amount, currency = price_of(booking, et)
    if not amount:
        raise PaymentError("This event type does not require payment")
    key = f"checkout:{booking.id}"
    rec = db.scalar(select(PaymentRecord).where(PaymentRecord.idempotency_key == key))
    if rec and rec.status in ("pending", "created") and rec.checkout_url:
        return rec
    if rec and rec.status not in ("created",):
        return rec
    provider = (et.payment_provider if et else None) or "stripe"
    account = account_for(db, et, provider)
    if not rec:
        rec = PaymentRecord(workspace_id=booking.workspace_id, booking_id=booking.id, provider=provider,
                            amount_minor=amount, currency=currency, status="created", idempotency_key=key)
        db.add(rec)
        db.flush()
    adapter = adapter_for(provider, account)
    base = (base_url or settings.base_url).rstrip("/")
    title = (et.payment_description if et and et.payment_description else None) or (et.name if et else "Meeting")
    sess = adapter.create_checkout(
        rec, booking, account=account, title=title,
        success_url=f"{base}/pay/{booking.manage_token}/return?record={rec.id}",
        cancel_url=f"{base}/pay/{booking.manage_token}/cancel?record={rec.id}",
    )
    rec.provider_payment_id, rec.checkout_url, rec.status = sess.provider_payment_id, sess.checkout_url, "pending"
    booking.payment_status = "pending"
    audit(db, "payment.checkout_created", workspace_id=booking.workspace_id, actor_id="invitee",
          entity_type="booking", entity_id=booking.id, metadata={"payment_id": rec.id, "provider": provider,
                                                                 "mock": adapter.mock})
    db.flush()
    return rec


# ------------------------------------------------------------------ outcomes
def _booking_mod():
    from app.services import booking as booking_service

    return booking_service


def _alert_host(db: Session, booking: Booking, title: str, message: str) -> None:
    from app.services.notifications import notify_host_alert

    host = db.get(User, booking.host_id)
    if host:
        notify_host_alert(db, host, title, message, link=f"/app/scheduled/{booking.id}",
                          dedupe_key=f"payalert:{booking.id}:{title}", workspace_id=booking.workspace_id,
                          booking_id=booking.id, kind="payment_alert")


def complete_payment(db: Session, record: PaymentRecord, actor: str = "provider") -> str:
    """Idempotently mark the payment successful and finalise the booking.
    Returns 'confirmed' | 'already' | 'refunded'."""
    booking = db.get(Booking, record.booking_id)
    if record.status in ("refunded", "refund_pending"):
        return "refunded"
    if record.status == "succeeded" and booking and booking.status == "confirmed":
        return "already"
    first_success = record.status != "succeeded"
    record.status = "succeeded"
    if booking:
        booking.payment_status = "paid"
    if first_success:
        publish(db, "payment.succeeded", {
            "booking_id": record.booking_id, "payment_id": record.id, "provider": record.provider,
            "amount_minor": record.amount_minor, "currency": record.currency,
            "provider_payment_id": record.provider_payment_id,
        }, workspace_id=record.workspace_id)
        audit(db, "payment.succeeded", workspace_id=record.workspace_id, actor_id=actor, entity_type="payment",
              entity_id=record.id)
    db.commit()
    if not booking:
        return reconcile_refund(db, record, None, "Booking not found for successful payment")
    if booking.status == "confirmed":
        return "confirmed"
    if booking.status in ("cancelled", "rescheduled"):
        return reconcile_refund(db, record, booking, f"Booking was {booking.status} when payment completed")
    # held / pending_payment / pending, or expired/failed (finalize tries to re-reserve the slot)
    try:
        _booking_mod().finalize_booking(db, booking)
    except Exception as exc:  # noqa: BLE001 - slot gone / hold expired => saga compensation
        db.rollback()
        log.warning("finalize after payment failed for %s: %s", booking.id, exc)
        booking = db.get(Booking, record.booking_id)
        record = db.get(PaymentRecord, record.id)
        return reconcile_refund(db, record, booking, f"Booking could not be finalised: {exc}")
    db.refresh(booking)
    if booking.status not in ("confirmed", "pending"):
        return reconcile_refund(db, record, booking, f"Booking ended in status {booking.status}")
    return "confirmed"


def reconcile_refund(db: Session, record: PaymentRecord, booking: Optional[Booking], reason: str) -> str:
    """Compensating action: refund through the provider and alert the host/support."""
    adapter, account = record_adapter(db, record)
    try:
        refund_id = adapter.refund(record, account)
        record.status, record.refund_status = "refunded", "refunded"
        msg = (f"A payment of {record.amount_minor / 100:.2f} {record.currency} was automatically refunded "
               f"(refund {refund_id}). Reason: {reason}")
    except Exception as exc:  # noqa: BLE001
        record.status, record.refund_status = "refund_pending", "failed"
        msg = (f"A payment of {record.amount_minor / 100:.2f} {record.currency} succeeded but the booking could not be "
               f"confirmed, and the automatic refund failed ({exc}). Please refund it manually in your "
               f"{record.provider} dashboard. Reason: {reason}")
    if booking:
        booking.payment_status = "refunded" if record.status == "refunded" else "paid"
        if booking.status in ("pending_payment", "held"):
            try:
                _booking_mod().fail_pending_booking(db, booking, "Payment refunded: " + reason)
            except Exception:  # noqa: BLE001
                db.rollback()
                booking = db.get(Booking, record.booking_id)
                record = db.get(PaymentRecord, record.id)
        _alert_host(db, booking, "Payment refunded: booking could not be confirmed", msg)
    audit(db, "payment.auto_refund", workspace_id=record.workspace_id, actor_id="system", entity_type="payment",
          entity_id=record.id, metadata={"reason": reason, "refund_status": record.refund_status})
    db.commit()
    return "refunded"


def fail_payment(db: Session, record: PaymentRecord, reason: str = "Payment failed") -> str:
    if record.status in ("succeeded", "refunded", "refund_pending"):
        return record.status
    booking = db.get(Booking, record.booking_id)
    already_failed = record.status == "failed"
    record.status = "failed"
    if booking:
        booking.payment_status = "failed"
    if not already_failed:
        publish(db, "payment.failed", {
            "booking_id": record.booking_id, "payment_id": record.id, "provider": record.provider,
            "amount_minor": record.amount_minor, "currency": record.currency, "reason": reason,
        }, workspace_id=record.workspace_id)
        audit(db, "payment.failed", workspace_id=record.workspace_id, actor_id="provider", entity_type="payment",
              entity_id=record.id, metadata={"reason": reason})
    db.commit()
    if booking and booking.status in ("pending_payment", "held", "pending"):
        try:
            _booking_mod().fail_pending_booking(db, booking, reason)
        except Exception as exc:  # noqa: BLE001 - make sure the slot is released regardless
            db.rollback()
            log.warning("fail_pending_booking failed: %s", exc)
            booking = db.get(Booking, record.booking_id)
            booking.status = "failed"
            booking.hold_expires_at = None
            db.commit()
    return "failed"


def refund_booking(db: Session, booking: Booking, actor_id: str) -> PaymentRecord:
    rec = db.scalar(select(PaymentRecord).where(
        PaymentRecord.booking_id == booking.id, PaymentRecord.status == "succeeded").order_by(PaymentRecord.created_at.desc()))
    if not rec:
        raise PaymentError("No successful payment to refund for this booking")
    adapter, account = record_adapter(db, rec)
    rec.refund_status = "pending"
    try:
        refund_id = adapter.refund(rec, account)
    except Exception as exc:  # noqa: BLE001
        rec.refund_status = "failed"
        db.commit()
        raise PaymentError(f"Refund failed at {rec.provider}: {exc}") from exc
    rec.status, rec.refund_status = "refunded", "refunded"
    booking.payment_status = "refunded"
    audit(db, "payment.refunded", workspace_id=booking.workspace_id, actor_id=actor_id, entity_type="payment",
          entity_id=rec.id, metadata={"refund_id": refund_id, "amount_minor": rec.amount_minor})
    db.commit()
    return rec


# ------------------------------------------------------------------ provider events
def _record_by_ref(db: Session, record_id: Optional[str], provider_payment_id: Optional[str]) -> Optional[PaymentRecord]:
    if record_id:
        r = db.get(PaymentRecord, record_id)
        if r:
            return r
    if provider_payment_id:
        return db.scalar(select(PaymentRecord).where(PaymentRecord.provider_payment_id == provider_payment_id))
    return None


def handle_stripe_event(db: Session, event: dict) -> None:
    from app.services import billing, commerce

    etype = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    meta = obj.get("metadata") or {}
    if billing.handle_stripe_event(db, event):  # subscription billing (ACC-005)
        return
    if commerce.handle_stripe_object(db, etype, obj):  # packages / invoices / payment links
        return
    rec = _record_by_ref(db, meta.get("payment_record_id") or obj.get("client_reference_id"), obj.get("id"))
    if not rec:
        return
    if etype in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        if obj.get("payment_status", "paid") == "paid":
            complete_payment(db, rec)
    elif etype in ("checkout.session.expired", "checkout.session.async_payment_failed", "payment_intent.payment_failed"):
        fail_payment(db, rec, f"Stripe: {etype}")
    elif etype == "charge.refunded":
        rec.status, rec.refund_status = "refunded", "refunded"
        b = db.get(Booking, rec.booking_id)
        if b:
            b.payment_status = "refunded"
        db.commit()


def handle_paypal_event(db: Session, event: dict) -> None:
    etype = event.get("event_type", "")
    res = event.get("resource") or {}
    rid = res.get("custom_id") or ((res.get("purchase_units") or [{}])[0].get("custom_id"))
    order_id = ((res.get("supplementary_data") or {}).get("related_ids") or {}).get("order_id") or res.get("id")
    rec = _record_by_ref(db, rid, order_id)
    if not rec:
        return
    if etype == "PAYMENT.CAPTURE.COMPLETED":
        complete_payment(db, rec)
    elif etype in ("PAYMENT.CAPTURE.DENIED", "CHECKOUT.ORDER.VOIDED"):
        fail_payment(db, rec, f"PayPal: {etype}")
    elif etype == "PAYMENT.CAPTURE.REFUNDED":
        rec.status, rec.refund_status = "refunded", "refunded"
        db.commit()


@periodic("payment_reconcile")
def payment_reconcile_task(db: Session) -> None:
    """Poll real providers for pending payments whose webhook never arrived."""
    cutoff = utcnow() - dt.timedelta(minutes=2)
    for rec in db.scalars(select(PaymentRecord).where(
            PaymentRecord.status == "pending", PaymentRecord.updated_at < cutoff)).all():
        if (rec.provider_payment_id or "").startswith("mock_"):
            booking = db.get(Booking, rec.booking_id)
            if booking and booking.status in ("expired", "failed", "cancelled"):
                rec.status = "cancelled"
                db.commit()
            continue
        adapter, account = record_adapter(db, rec)
        try:
            status = adapter.fetch_status(rec, account)
        except Exception:  # noqa: BLE001
            continue
        if status == "succeeded":
            complete_payment(db, rec, actor="reconciler")
        elif status == "failed":
            fail_payment(db, rec, "Checkout expired")
        else:
            rec.updated_at = utcnow()
            db.commit()


def revenue_summary(db: Session, workspace_id: str, start: Optional[dt.datetime] = None,
                    end: Optional[dt.datetime] = None) -> dict:
    """Revenue by currency and event type (for analytics)."""
    q = select(PaymentRecord, Booking.event_type_id).join(Booking, Booking.id == PaymentRecord.booking_id).where(
        PaymentRecord.workspace_id == workspace_id, PaymentRecord.status.in_(("succeeded", "refunded")))
    if start:
        q = q.where(PaymentRecord.created_at >= start)
    if end:
        q = q.where(PaymentRecord.created_at < end)
    by_currency: dict = {}
    by_event_type: dict = {}
    refunded: dict = {}
    for rec, et_id in db.execute(q).all():
        if rec.status == "refunded":
            refunded[rec.currency] = refunded.get(rec.currency, 0) + rec.amount_minor
            continue
        by_currency[rec.currency] = by_currency.get(rec.currency, 0) + rec.amount_minor
        k = (et_id, rec.currency)
        by_event_type[k] = by_event_type.get(k, 0) + rec.amount_minor
    return {
        "gross_by_currency": by_currency,
        "refunded_by_currency": refunded,
        "by_event_type": [{"event_type_id": k[0], "currency": k[1], "amount_minor": v} for k, v in by_event_type.items()],
    }
