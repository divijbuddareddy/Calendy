"""Invitee checkout (/pay/{manage_token}), package redemption, host refunds (/app/payments/...),
plus (via include_router) the payments hub, packages/invoices/links and /app/billing."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import Ctx, flash, get_ctx, redirect, render
from app.models import Booking, BookingHost, EventType, PaymentRecord, User, utcnow
from app.ratelimit import enforce
from app.services import payments as P

router = APIRouter()
# Packages / invoices / payment links (+ the /app/payments hub) and subscription billing. Included
# BEFORE the /app/payments/{booking_id} routes below so their static paths win.
from app.routers.billing import router as _billing_router  # noqa: E402
from app.routers.commerce import router as _commerce_router  # noqa: E402

router.include_router(_commerce_router)
router.include_router(_billing_router)


def _booking_by_token(db: Session, token: str) -> Booking:
    b = db.scalar(select(Booking).where(Booking.manage_token == token))
    if not b:
        raise HTTPException(404, "Booking not found")
    return b


def _context(db: Session, b: Booking) -> dict:
    et = db.get(EventType, b.event_type_id)
    host = db.get(User, b.host_id)
    amount, currency = P.price_of(b, et)
    rebook = f"/{host.slug}/{et.slug}" if host and et and host.slug else "/"
    return {"booking": b, "event_type": et, "host": host, "amount": amount, "currency": currency,
            "record": P.latest_record(db, b.id), "rebook_url": rebook, "tz": b.timezone or "UTC",
            "hide_branding": False}


def _confirmed_url(b: Booking) -> str:
    return f"/booking/{b.manage_token}/confirmed"


def _is_finished(b: Booking) -> bool:
    return b.status == "confirmed" or (b.status == "pending" and b.payment_status == "paid")


@router.get("/pay/{token}", include_in_schema=False)
def pay_page(token: str, request: Request, db: Session = Depends(get_db)):
    b = _booking_by_token(db, token)
    if _is_finished(b):
        return redirect(_confirmed_url(b))
    ctx = _context(db, b)
    if b.status in ("failed", "expired", "cancelled") or b.payment_status in ("failed", "refunded"):
        return render(request, "pay/failed.html", **ctx)
    if b.hold_expires_at and b.hold_expires_at < utcnow() and b.status in ("held", "pending_payment"):
        ctx["expired_hold"] = True
        return render(request, "pay/failed.html", **ctx)
    return render(request, "pay/checkout.html", **ctx)


@router.post("/pay/{token}/start", include_in_schema=False)
def pay_start(token: str, request: Request, db: Session = Depends(get_db)):
    enforce(request, "pay_start", limit=20, window_seconds=60)
    b = _booking_by_token(db, token)
    if _is_finished(b):
        return redirect(_confirmed_url(b))
    if b.status not in ("pending_payment", "held", "pending"):
        return redirect(f"/pay/{token}")
    if b.hold_expires_at and b.hold_expires_at < utcnow():
        return redirect(f"/pay/{token}")
    try:
        rec = P.get_or_create_checkout(db, b, base_url=str(request.base_url))
        db.commit()
    except P.PaymentError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return redirect(f"/pay/{token}")
    except Exception as exc:  # noqa: BLE001 - provider outage: let the invitee retry
        db.rollback()
        flash(request, f"The payment provider is unavailable right now. Please try again. ({exc.__class__.__name__})", "error")
        return redirect(f"/pay/{token}")
    if rec.status == "succeeded":
        return redirect(_confirmed_url(b))
    return redirect(rec.checkout_url or f"/pay/{token}")


@router.get("/pay/{token}/checkout/mock", include_in_schema=False)
def mock_checkout(token: str, request: Request, record: str = "", db: Session = Depends(get_db)):
    b = _booking_by_token(db, token)
    rec = db.get(PaymentRecord, record)
    if not rec or rec.booking_id != b.id or not (rec.provider_payment_id or "").startswith("mock_"):
        raise HTTPException(404, "Checkout session not found")
    if rec.status == "succeeded" or _is_finished(b):
        return redirect(_confirmed_url(b))
    ctx = _context(db, b)
    ctx["record"] = rec
    return render(request, "pay/mock_checkout.html", **ctx)


@router.post("/pay/{token}/checkout/mock", include_in_schema=False)
def mock_checkout_submit(token: str, request: Request, record: str = Form(...), outcome: str = Form("success"),
                         db: Session = Depends(get_db)):
    enforce(request, "pay_mock", limit=20, window_seconds=60)
    b = _booking_by_token(db, token)
    rec = db.get(PaymentRecord, record)
    if not rec or rec.booking_id != b.id or not (rec.provider_payment_id or "").startswith("mock_"):
        raise HTTPException(404, "Checkout session not found")
    if outcome == "success":
        result = P.complete_payment(db, rec, actor="invitee")
        if result in ("confirmed", "already"):
            return redirect(_confirmed_url(b))
        return redirect(f"/pay/{token}")
    P.fail_payment(db, rec, "Card declined (simulated)" if outcome == "fail" else "Checkout cancelled")
    return redirect(f"/pay/{token}")


@router.get("/pay/{token}/return", include_in_schema=False)
def pay_return(token: str, request: Request, record: str = "", db: Session = Depends(get_db)):
    """Return URL from Stripe Checkout / PayPal approval. Status is verified server-side with the provider."""
    b = _booking_by_token(db, token)
    rec = db.get(PaymentRecord, record) if record else P.latest_record(db, b.id)
    if not rec or rec.booking_id != b.id:
        raise HTTPException(404, "Payment not found")
    if rec.status == "succeeded":
        return redirect(_confirmed_url(b) if _is_finished(b) else f"/pay/{token}")
    adapter, account = P.record_adapter(db, rec)
    try:
        status = adapter.capture(rec, account, dict(request.query_params)) if rec.provider == "paypal" else adapter.fetch_status(rec, account)
    except Exception as exc:  # noqa: BLE001
        flash(request, f"We couldn't verify your payment yet ({exc.__class__.__name__}). We'll keep checking.", "warning")
        return render(request, "pay/processing.html", **_context(db, b))
    if status == "succeeded":
        if P.complete_payment(db, rec, actor="invitee") in ("confirmed", "already"):
            return redirect(_confirmed_url(b))
        return redirect(f"/pay/{token}")
    if status == "failed":
        P.fail_payment(db, rec, "Payment was not completed")
        return redirect(f"/pay/{token}")
    return render(request, "pay/processing.html", **_context(db, b))


@router.get("/pay/{token}/cancel", include_in_schema=False)
def pay_cancel(token: str, request: Request, record: str = "", db: Session = Depends(get_db)):
    b = _booking_by_token(db, token)
    rec = db.get(PaymentRecord, record) if record else P.latest_record(db, b.id)
    if rec and rec.booking_id == b.id and rec.status in ("created", "pending"):
        P.fail_payment(db, rec, "Checkout cancelled by invitee")
    return redirect(f"/pay/{token}")


@router.get("/pay/{token}/status", include_in_schema=False)
def pay_status(token: str, db: Session = Depends(get_db)):
    b = _booking_by_token(db, token)
    rec = P.latest_record(db, b.id)
    return {"booking_status": b.status, "payment_status": b.payment_status, "record_status": rec.status if rec else None,
            "confirmed_url": _confirmed_url(b) if _is_finished(b) else None}


# ------------------------------------------------------------------ host side
def _host_booking(db: Session, ctx: Ctx, booking_id: str) -> Booking:
    b = db.get(Booking, booking_id)
    if not b or b.workspace_id != ctx.workspace.id:
        raise HTTPException(404, "Booking not found")
    is_host = b.host_id == ctx.user.id or db.scalar(select(BookingHost.id).where(
        BookingHost.booking_id == b.id, BookingHost.user_id == ctx.user.id))
    if not is_host and not ctx.can("admin"):
        raise HTTPException(403, "Only the host or an admin can refund this booking")
    return b


@router.post("/app/payments/{booking_id}/refund", include_in_schema=False)
def refund(booking_id: str, request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    b = _host_booking(db, ctx, booking_id)
    wants_json = "application/json" in request.headers.get("accept", "")
    try:
        rec = P.refund_booking(db, b, actor_id=ctx.user.id)
    except P.PaymentError as exc:
        if wants_json:
            return JSONResponse({"error": {"code": "REFUND_FAILED", "message": str(exc)}}, status_code=400)
        flash(request, str(exc), "error")
        return redirect(request.headers.get("referer") or f"/app/scheduled/{booking_id}")
    if wants_json:
        return {"ok": True, "payment_id": rec.id, "status": rec.status, "refund_status": rec.refund_status}
    flash(request, f"Refunded {rec.amount_minor / 100:.2f} {rec.currency} via {rec.provider}.")
    return redirect(request.headers.get("referer") or f"/app/scheduled/{booking_id}")


@router.get("/app/payments/{booking_id}", include_in_schema=False)
def payment_detail(booking_id: str, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    b = _host_booking(db, ctx, booking_id)
    recs = db.scalars(select(PaymentRecord).where(PaymentRecord.booking_id == b.id)
                      .order_by(PaymentRecord.created_at.desc())).all()
    return {"booking_id": b.id, "payment_status": b.payment_status, "payments": [
        {"id": r.id, "provider": r.provider, "amount_minor": r.amount_minor, "currency": r.currency,
         "status": r.status, "refund_status": r.refund_status, "provider_payment_id": r.provider_payment_id,
         "created_at": r.created_at.isoformat() + "Z"} for r in recs]}
