"""Meeting packages, invoices and standalone payment links (PRD 12, P3).

All three are paid through a CommerceCheckout (hosted checkout: Stripe Checkout on the host's
connected account when STRIPE_SECRET_KEY is set and the host connected a real Stripe account,
otherwise the offline mock checkout). Funds go straight to the host, like booking payments.

  * Package: N sessions of one event type bought upfront. On payment a redemption code is
    generated (stored hashed, emailed to the buyer once). An invitee redeems the code on the
    booking's payment page (/pay/{token}) and the booking is confirmed without paying; cancelling
    that booking returns the session to the package.
  * Invoice: draft -> sent (emailed with a pay link) -> paid | void.
  * Payment link: fixed amount + description, payable any number of times.

complete_checkout() is idempotent and is reached from the mock checkout, the Stripe return URL,
the Stripe webhook (metadata.commerce_checkout_id) and the reconciliation task.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import secrets
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.audit import audit
from app.config import settings
from app.events import publish, subscribe
from app.models import Booking, EventType, PaymentAccount, PaymentRecord, User, utcnow
from app.models_commerce import (
    CommerceCheckout,
    Invoice,
    PackageProduct,
    PackagePurchase,
    PackageRedemption,
    PaymentLink,
)
from app.security import constant_time_eq, hash_secret
from app.services.providers.payments_mock import is_mock_account
from app.worker import periodic

log = logging.getLogger("commerce")
CURRENCIES = ("USD", "GBP", "EUR", "INR", "CAD", "AUD")
INVOICE_STATUSES = ("draft", "sent", "paid", "void")


class CommerceError(Exception):
    pass


def _token() -> str:
    return secrets.token_urlsafe(18)


def parse_amount(value) -> int:
    """'49.99' -> 4999 (minor units). Raises CommerceError."""
    s = str(value or "").strip().replace(",", "")
    if not re.fullmatch(r"\d{1,7}(\.\d{1,2})?", s):
        raise CommerceError("Enter an amount like 49.99")
    major, _, minor = s.partition(".")
    amount = int(major) * 100 + int((minor + "00")[:2])
    if amount <= 0:
        raise CommerceError("Amount must be greater than zero")
    return amount


def _currency(c: Optional[str]) -> str:
    c = (c or "USD").upper()
    if c not in CURRENCIES:
        raise CommerceError(f"Unsupported currency {c}")
    return c


def _email(e: Optional[str]) -> str:
    e = (e or "").strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e):
        raise CommerceError("Enter a valid email address")
    return e


def _base(base_url: Optional[str]) -> str:
    return (base_url or settings.base_url).rstrip("/")


def _send(db: Session, workspace_id: str, to: str, subject: str, title: str, message: str,
          link: Optional[str] = None, link_label: Optional[str] = None, name: Optional[str] = None) -> None:
    from app.services.messaging import send_email
    from app.services.notifications import render_email
    from app.services.usage import record_usage

    html = render_email("alert.html", title=title, message=message, link=link, link_label=link_label,
                        recipient_name=name)
    send_email(db, to, subject, html)
    record_usage(db, workspace_id, "emails_sent")


# ------------------------------------------------------------------ packages
def create_package(db: Session, workspace_id: str, owner: User, event_type_id: str, name: str, sessions: int,
                   price, currency: str) -> PackageProduct:
    et = db.get(EventType, event_type_id)
    if not et or et.workspace_id != workspace_id or et.deleted_at:
        raise CommerceError("Choose an event type from this workspace")
    try:
        sessions = int(sessions)
    except (TypeError, ValueError):
        raise CommerceError("Sessions must be a number")
    if not 1 <= sessions <= 100:
        raise CommerceError("A package holds between 1 and 100 sessions")
    p = PackageProduct(workspace_id=workspace_id, owner_user_id=owner.id, event_type_id=et.id,
                       name=(name or f"{sessions} x {et.name}")[:200], sessions=sessions, price_minor=parse_amount(price),
                       currency=_currency(currency), public_token=_token(), active=True)
    db.add(p)
    db.flush()
    return p


def _code_hash(code: str) -> str:
    return hash_secret("pkg-code:" + code.strip().upper().replace("-", ""))


def _new_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def start_package_purchase(db: Session, product: PackageProduct, buyer_name: str, buyer_email: str,
                           base_url: Optional[str] = None) -> CommerceCheckout:
    if not product.active:
        raise CommerceError("This package is no longer available")
    purchase = PackagePurchase(workspace_id=product.workspace_id, product_id=product.id, buyer_name=(buyer_name or "")[:200],
                               buyer_email=_email(buyer_email), sessions_total=product.sessions, status="pending")
    db.add(purchase)
    db.flush()
    return start_checkout(db, workspace_id=product.workspace_id, kind="package", ref_id=purchase.id,
                          amount=product.price_minor, currency=product.currency, title=product.name,
                          owner_user_id=product.owner_user_id, buyer_name=purchase.buyer_name,
                          buyer_email=purchase.buyer_email, base_url=base_url)


def _activate_package(db: Session, purchase: PackagePurchase, base_url: Optional[str] = None) -> Optional[str]:
    if purchase.status != "pending":
        return None
    code = _new_code()
    purchase.code_hash, purchase.code_hint, purchase.status = _code_hash(code), code[-4:], "active"
    product = db.get(PackageProduct, purchase.product_id)
    et = db.get(EventType, product.event_type_id) if product else None
    owner = db.get(User, et.owner_user_id) if et else None
    book_url = f"{_base(base_url)}/{owner.slug}/{et.slug}" if owner and owner.slug and et else None
    _send(db, purchase.workspace_id, purchase.buyer_email, f"Your package: {product.name if product else 'sessions'}",
          f"Your {purchase.sessions_total}-session package is ready",
          f"Thanks for your purchase of {product.name if product else 'a package'}.\n\n"
          f"Your redemption code: {code}\n\n"
          f"Book each session as usual; on the payment step enter this code instead of paying. "
          f"Keep it private - it can be used {purchase.sessions_total} times.",
          link=book_url, link_label="Book a session", name=purchase.buyer_name or None)
    return code


def find_purchase_by_code(db: Session, code: str) -> Optional[PackagePurchase]:
    if not code or len(code) > 40:
        return None
    h = _code_hash(code)
    p = db.scalar(select(PackagePurchase).where(PackagePurchase.code_hash == h))
    return p if p and constant_time_eq(p.code_hash, h) else None


def redeem_package(db: Session, booking: Booking, code: str) -> PackagePurchase:
    """Consume one session for `booking` (status pending_payment/held) and finalise it without payment.
    Commits. Raises CommerceError with a user-facing message."""
    from app.services import payments as P

    if booking.status not in ("pending_payment", "held"):
        raise CommerceError("This booking no longer needs payment")
    if db.scalar(select(PackageRedemption.id).where(PackageRedemption.booking_id == booking.id)):
        raise CommerceError("A package session was already applied to this booking")
    purchase = find_purchase_by_code(db, code)
    product = db.get(PackageProduct, purchase.product_id) if purchase else None
    if not purchase or not product or purchase.workspace_id != booking.workspace_id:
        raise CommerceError("That code is not valid")
    if product.event_type_id != booking.event_type_id:
        raise CommerceError("That package is for a different meeting type")
    if purchase.status != "active" or (purchase.expires_at and purchase.expires_at < utcnow()):
        raise CommerceError("That package has no sessions left" if purchase.status == "exhausted"
                            else "That package is not active")
    # atomic decrement (never oversell a package under concurrency)
    res = db.execute(update(PackagePurchase).where(
        PackagePurchase.id == purchase.id, PackagePurchase.sessions_used < PackagePurchase.sessions_total,
        PackagePurchase.status == "active").values(sessions_used=PackagePurchase.sessions_used + 1))
    if not res.rowcount:
        db.rollback()
        raise CommerceError("That package has no sessions left")
    db.refresh(purchase)
    if purchase.sessions_used >= purchase.sessions_total:
        purchase.status = "exhausted"
    db.add(PackageRedemption(purchase_id=purchase.id, booking_id=booking.id))
    booking.payment_status = "paid"
    snap = dict(booking.snapshot_json or {})
    snap["package_purchase_id"] = purchase.id
    booking.snapshot_json = snap
    for rec in db.scalars(select(PaymentRecord).where(PaymentRecord.booking_id == booking.id,
                                                      PaymentRecord.status.in_(("created", "pending")))).all():
        rec.status = "cancelled"
    audit(db, "package.redeemed", workspace_id=booking.workspace_id, actor_id="invitee", entity_type="booking",
          entity_id=booking.id, metadata={"purchase_id": purchase.id, "used": purchase.sessions_used})
    db.commit()
    pid = purchase.id
    try:
        P._booking_mod().finalize_booking(db, booking)
    except Exception as exc:  # noqa: BLE001 - slot gone: give the session back
        db.rollback()
        _return_session(db, booking.id, payment_status="pending")
        db.commit()
        raise CommerceError(f"Your time slot could not be confirmed ({exc}). Your package session was not used.")
    return db.get(PackagePurchase, pid)


def _return_session(db: Session, booking_id: str, payment_status: str = "refunded") -> bool:
    red = db.scalar(select(PackageRedemption).where(PackageRedemption.booking_id == booking_id))
    if not red:
        return False
    purchase = db.get(PackagePurchase, red.purchase_id)
    db.delete(red)
    if purchase and purchase.sessions_used > 0:
        purchase.sessions_used -= 1
        if purchase.status == "exhausted":
            purchase.status = "active"
    b = db.get(Booking, booking_id)
    if b and b.payment_status == "paid":
        b.payment_status = payment_status
    db.flush()
    return True


@subscribe("booking.cancelled")
def _package_session_back_on_cancel(db: Session, ev) -> None:
    bid = (ev.payload_json or {}).get("booking_id")
    if bid and _return_session(db, bid):
        audit(db, "package.session_returned", workspace_id=ev.workspace_id, actor_id="system", entity_type="booking",
              entity_id=bid)


# ------------------------------------------------------------------ invoices
def create_invoice(db: Session, workspace_id: str, user: User, recipient_email: str, recipient_name: str,
                   items: list[dict], currency: str, description: str = "",
                   due_date: Optional[dt.datetime] = None) -> Invoice:
    lines, total = [], 0
    for it in items:
        desc = (it.get("description") or "").strip()
        if not desc and not it.get("amount"):
            continue
        qty = int(it.get("quantity") or 1)
        if qty < 1 or qty > 1000:
            raise CommerceError("Quantity must be between 1 and 1000")
        unit = parse_amount(it.get("amount"))
        lines.append({"description": desc[:300] or "Item", "quantity": qty, "unit_minor": unit})
        total += unit * qty
    if not lines:
        raise CommerceError("Add at least one line item")
    n = (db.scalar(select(func.count(Invoice.id)).where(Invoice.workspace_id == workspace_id)) or 0) + 1
    inv = Invoice(workspace_id=workspace_id, created_by=user.id, number=f"INV-{n:04d}",
                  recipient_email=_email(recipient_email), recipient_name=(recipient_name or "")[:200],
                  description=(description or "")[:2000], line_items_json=lines, amount_minor=total,
                  currency=_currency(currency), due_date=due_date, status="draft", public_token=_token())
    db.add(inv)
    db.flush()
    return inv


def send_invoice(db: Session, inv: Invoice, base_url: Optional[str] = None) -> None:
    if inv.status not in ("draft", "sent"):
        raise CommerceError(f"A {inv.status} invoice cannot be sent")
    from app.deps import _money

    sender = db.get(User, inv.created_by)
    due = f"\nDue: {inv.due_date:%d %b %Y}" if inv.due_date else ""
    _send(db, inv.workspace_id, inv.recipient_email,
          f"Invoice {inv.number} from {sender.name if sender else settings.app_name}",
          f"Invoice {inv.number}: {_money(inv.amount_minor, inv.currency)}",
          (inv.description + "\n\n" if inv.description else "")
          + "\n".join(f"{l['quantity']} x {l['description']} - {_money(l['unit_minor'] * l['quantity'], inv.currency)}"
                      for l in inv.line_items_json) + due,
          link=f"{_base(base_url)}/p/invoice/{inv.public_token}", link_label="View and pay invoice",
          name=inv.recipient_name or None)
    inv.status, inv.sent_at = "sent", utcnow()
    db.flush()


def void_invoice(db: Session, inv: Invoice) -> None:
    if inv.status == "paid":
        raise CommerceError("A paid invoice cannot be voided; refund it through your payment provider")
    inv.status, inv.voided_at = "void", utcnow()
    db.flush()


# ------------------------------------------------------------------ payment links
def create_link(db: Session, workspace_id: str, user: User, description: str, amount, currency: str) -> PaymentLink:
    description = (description or "").strip()
    if not description:
        raise CommerceError("Add a description")
    link = PaymentLink(workspace_id=workspace_id, created_by=user.id, description=description[:500],
                       amount_minor=parse_amount(amount), currency=_currency(currency), public_token=_token())
    db.add(link)
    db.flush()
    return link


# ------------------------------------------------------------------ checkout
def _stripe_account(db: Session, owner_user_id: str) -> Optional[PaymentAccount]:
    acct = db.scalar(select(PaymentAccount).where(PaymentAccount.user_id == owner_user_id,
                                                  PaymentAccount.provider == "stripe",
                                                  PaymentAccount.status == "connected"))
    return acct


def _use_stripe(db: Session, owner_user_id: str) -> Optional[PaymentAccount]:
    from app.services.providers.payments_stripe import StripeProvider

    acct = _stripe_account(db, owner_user_id)
    if StripeProvider.configured() and acct and not is_mock_account(acct):
        return acct
    return None


def start_checkout(db: Session, *, workspace_id: str, kind: str, ref_id: str, amount: int, currency: str, title: str,
                   owner_user_id: str, buyer_name: str = "", buyer_email: str = "",
                   base_url: Optional[str] = None) -> CommerceCheckout:
    co = CommerceCheckout(workspace_id=workspace_id, kind=kind, ref_id=ref_id, amount_minor=amount, currency=currency,
                          buyer_name=buyer_name or "", buyer_email=buyer_email or "", status="created",
                          access_token=_token(), provider="stripe")
    db.add(co)
    db.flush()
    base = _base(base_url)
    acct = _use_stripe(db, owner_user_id)
    if acct:
        from app.services.providers.payments_stripe import _req

        data = {
            "mode": "payment", "client_reference_id": co.id,
            "success_url": f"{base}/pay/c/{co.access_token}/return?session_id={{CHECKOUT_SESSION_ID}}",
            "cancel_url": f"{base}/pay/c/{co.access_token}/cancel",
            "line_items[0][quantity]": 1, "line_items[0][price_data][currency]": currency.lower(),
            "line_items[0][price_data][unit_amount]": amount,
            "line_items[0][price_data][product_data][name]": title[:250],
            "metadata[commerce_checkout_id]": co.id, "metadata[kind]": kind,
            "payment_intent_data[metadata][commerce_checkout_id]": co.id,
        }
        if buyer_email:
            data["customer_email"] = buyer_email
        s = _req("POST", "/checkout/sessions", account_id=acct.account_id, idem=f"commerce:{co.id}", data=data)
        co.provider_payment_id, co.checkout_url = s["id"], s["url"]
    else:
        co.provider = "mock"
        co.provider_payment_id = f"mock_cco_{secrets.token_hex(10)}"
        co.checkout_url = f"/pay/c/{co.access_token}/mock"
    co.status = "pending"
    db.flush()
    return co


def checkout_title(db: Session, co: CommerceCheckout) -> str:
    if co.kind == "package":
        pur = db.get(PackagePurchase, co.ref_id)
        prod = db.get(PackageProduct, pur.product_id) if pur else None
        return prod.name if prod else "Package"
    if co.kind == "invoice":
        inv = db.get(Invoice, co.ref_id)
        return f"Invoice {inv.number}" if inv else "Invoice"
    link = db.get(PaymentLink, co.ref_id)
    return link.description if link else "Payment"


def _owner_of(db: Session, co: CommerceCheckout) -> Optional[User]:
    if co.kind == "package":
        pur = db.get(PackagePurchase, co.ref_id)
        prod = db.get(PackageProduct, pur.product_id) if pur else None
        return db.get(User, prod.owner_user_id) if prod else None
    obj = db.get(Invoice, co.ref_id) if co.kind == "invoice" else db.get(PaymentLink, co.ref_id)
    return db.get(User, obj.created_by) if obj else None


def complete_checkout(db: Session, co: CommerceCheckout, actor: str = "provider",
                      base_url: Optional[str] = None) -> str:
    """Idempotent success handler. Commits. Returns 'completed' | 'already'."""
    from app.services.notifications import in_app

    if co.status == "succeeded":
        return "already"
    co.status, co.completed_at = "succeeded", utcnow()
    title = checkout_title(db, co)
    if co.kind == "package":
        pur = db.get(PackagePurchase, co.ref_id)
        if pur:
            _activate_package(db, pur, base_url)
    elif co.kind == "invoice":
        inv = db.get(Invoice, co.ref_id)
        if inv and inv.status != "paid":
            inv.status, inv.paid_at = "paid", utcnow()
    elif co.kind == "link":
        link = db.get(PaymentLink, co.ref_id)
        if link:
            link.paid_count = (link.paid_count or 0) + 1
    owner = _owner_of(db, co)
    from app.deps import _money

    if owner:
        in_app(db, owner.id, "commerce_paid", f"Payment received: {title}",
               f"{_money(co.amount_minor, co.currency)} from {co.buyer_email or 'a customer'}", "/app/payments")
    if co.buyer_email:
        _send(db, co.workspace_id, co.buyer_email, f"Receipt: {title}", "Payment received",
              f"We received your payment of {_money(co.amount_minor, co.currency)} for {title}.\n"
              f"Reference: {co.provider_payment_id}", name=co.buyer_name or None)
    publish(db, "payment.succeeded", {"commerce_checkout_id": co.id, "kind": co.kind, "ref_id": co.ref_id,
                                      "amount_minor": co.amount_minor, "currency": co.currency,
                                      "provider": co.provider, "provider_payment_id": co.provider_payment_id},
            workspace_id=co.workspace_id)
    audit(db, f"{co.kind}.paid", workspace_id=co.workspace_id, actor_id=actor, entity_type=co.kind,
          entity_id=co.ref_id, metadata={"checkout_id": co.id, "amount_minor": co.amount_minor})
    db.commit()
    return "completed"


def fail_checkout(db: Session, co: CommerceCheckout, reason: str) -> None:
    if co.status in ("succeeded", "failed"):
        return
    co.status = "failed"
    publish(db, "payment.failed", {"commerce_checkout_id": co.id, "kind": co.kind, "ref_id": co.ref_id,
                                   "amount_minor": co.amount_minor, "currency": co.currency, "reason": reason},
            workspace_id=co.workspace_id)
    db.commit()


def fetch_status(db: Session, co: CommerceCheckout) -> str:
    if co.provider == "mock" or (co.provider_payment_id or "").startswith("mock_"):
        return co.status
    from app.services.providers.payments_stripe import _req

    owner = _owner_of(db, co)
    acct = _stripe_account(db, owner.id) if owner else None
    s = _req("GET", f"/checkout/sessions/{co.provider_payment_id}", account_id=acct.account_id if acct else None)
    if s.get("payment_status") == "paid":
        return "succeeded"
    if s.get("status") == "expired":
        return "failed"
    return "pending"


def handle_stripe_object(db: Session, etype: str, obj: dict) -> bool:
    """Stripe webhook dispatch for commerce checkouts. Returns True if handled."""
    cid = (obj.get("metadata") or {}).get("commerce_checkout_id")
    if not cid:
        return False
    co = db.get(CommerceCheckout, cid)
    if not co:
        return True
    if etype in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        if obj.get("payment_status", "paid") == "paid":
            complete_checkout(db, co)
    elif etype in ("checkout.session.expired", "checkout.session.async_payment_failed"):
        fail_checkout(db, co, f"Stripe: {etype}")
    return True


@periodic("commerce_reconcile")
def commerce_reconcile_task(db: Session) -> None:
    """Poll Stripe for pending checkouts whose webhook never arrived; expire stale mock checkouts."""
    now = utcnow()
    for co in db.scalars(select(CommerceCheckout).where(
            CommerceCheckout.status == "pending", CommerceCheckout.updated_at < now - dt.timedelta(minutes=2))).all():
        if co.provider == "mock":
            if co.created_at < now - dt.timedelta(days=1):
                co.status = "failed"
                db.commit()
            continue
        try:
            st = fetch_status(db, co)
        except Exception:  # noqa: BLE001
            continue
        if st == "succeeded":
            complete_checkout(db, co, actor="reconciler")
        elif st == "failed":
            fail_checkout(db, co, "Checkout expired")
        else:
            co.updated_at = now
            db.commit()


def summary(db: Session, workspace_id: str) -> dict:
    rows = db.execute(select(CommerceCheckout.kind, CommerceCheckout.currency, func.sum(CommerceCheckout.amount_minor))
                      .where(CommerceCheckout.workspace_id == workspace_id, CommerceCheckout.status == "succeeded")
                      .group_by(CommerceCheckout.kind, CommerceCheckout.currency)).all()
    return {"by_kind": [{"kind": k, "currency": c, "amount_minor": int(a or 0)} for k, c, a in rows]}
