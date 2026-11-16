"""Payments hub UI (/app/payments): booking payments, meeting packages, invoices, payment links
(PRD 12 P3), plus their public pages and the commerce checkout pages.

Mounted through routers/payments.py (include_router, before its /app/payments/{booking_id} routes).
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import audit
from app.db import get_db
from app.deps import Ctx, Forbidden, flash, get_ctx, redirect, render
from app.models import EventType, PaymentRecord, User
from app.models_commerce import CommerceCheckout, Invoice, PackageProduct, PackagePurchase, PaymentLink
from app.ratelimit import enforce
from app.services import commerce as C
from app.services import payments as P

router = APIRouter(include_in_schema=False)


def _need_payments(ctx: Ctx) -> None:
    if not ctx.feature("payments"):
        raise Forbidden("Payments require the Standard plan or higher. Upgrade in Settings > Billing.")


def _ws_obj(db: Session, ctx: Ctx, model, obj_id: str):
    o = db.get(model, obj_id)
    if not o or o.workspace_id != ctx.workspace.id:
        raise HTTPException(404)
    return o


# ------------------------------------------------------------------ host UI
@router.get("/app/payments")
def payments_home(request: Request, tab: str = "overview", db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    wsid = ctx.workspace.id
    data = {"tab": tab if tab in ("overview", "packages", "invoices", "links") else "overview",
            "enabled": ctx.feature("payments"), "currencies": C.CURRENCIES}
    data["records"] = db.scalars(select(PaymentRecord).where(PaymentRecord.workspace_id == wsid)
                                 .order_by(PaymentRecord.created_at.desc()).limit(25)).all()
    data["revenue"] = P.revenue_summary(db, wsid)
    data["commerce"] = C.summary(db, wsid)
    data["checkouts"] = db.scalars(select(CommerceCheckout).where(CommerceCheckout.workspace_id == wsid,
                                                                 CommerceCheckout.status == "succeeded")
                                   .order_by(CommerceCheckout.created_at.desc()).limit(25)).all()
    data["packages"] = db.scalars(select(PackageProduct).where(PackageProduct.workspace_id == wsid)
                                  .order_by(PackageProduct.created_at.desc())).all()
    data["purchases"] = db.scalars(select(PackagePurchase).where(PackagePurchase.workspace_id == wsid,
                                                                PackagePurchase.status != "pending")
                                   .order_by(PackagePurchase.created_at.desc()).limit(50)).all()
    data["product_names"] = {p.id: p.name for p in data["packages"]}
    data["invoices"] = db.scalars(select(Invoice).where(Invoice.workspace_id == wsid)
                                  .order_by(Invoice.created_at.desc())).all()
    data["links"] = db.scalars(select(PaymentLink).where(PaymentLink.workspace_id == wsid)
                               .order_by(PaymentLink.created_at.desc())).all()
    data["event_types"] = db.scalars(select(EventType).where(EventType.workspace_id == wsid,
                                                            EventType.deleted_at.is_(None)).order_by(EventType.name)).all()
    data["et_names"] = {e.id: e.name for e in data["event_types"]}
    return render(request, "app/payments/index.html", ctx, nav="payments", **data)


@router.post("/app/payments/packages")
def package_create(request: Request, event_type_id: str = Form(...), name: str = Form(""), sessions: str = Form("5"),
                   price: str = Form(...), currency: str = Form("USD"), db: Session = Depends(get_db),
                   ctx: Ctx = Depends(get_ctx)):
    _need_payments(ctx)
    try:
        p = C.create_package(db, ctx.workspace.id, ctx.user, event_type_id, name, sessions, price, currency)
    except C.CommerceError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return redirect("/app/payments?tab=packages")
    audit(db, "package.created", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="package", entity_id=p.id)
    db.commit()
    flash(request, f"Package '{p.name}' created. Share its link so clients can buy it.")
    return redirect("/app/payments?tab=packages")


@router.post("/app/payments/packages/{pkg_id}/toggle")
def package_toggle(pkg_id: str, request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    _need_payments(ctx)
    p = _ws_obj(db, ctx, PackageProduct, pkg_id)
    p.active = not p.active
    db.commit()
    flash(request, f"Package {'enabled' if p.active else 'disabled'}.")
    return redirect("/app/payments?tab=packages")


@router.post("/app/payments/invoices")
async def invoice_create(request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    _need_payments(ctx)
    form = await request.form()
    items = [{"description": d, "quantity": q, "amount": a} for d, q, a in
             zip(form.getlist("item_description"), form.getlist("item_quantity"), form.getlist("item_amount"))]
    due = None
    if form.get("due_date"):
        try:
            due = dt.datetime.fromisoformat(str(form.get("due_date")))
        except ValueError:
            pass
    try:
        inv = C.create_invoice(db, ctx.workspace.id, ctx.user, form.get("recipient_email"), form.get("recipient_name"),
                               items, form.get("currency") or "USD", form.get("description") or "", due)
        if form.get("send_now"):
            C.send_invoice(db, inv, base_url=str(request.base_url))
    except C.CommerceError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return redirect("/app/payments?tab=invoices")
    audit(db, "invoice.created", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="invoice",
          entity_id=inv.id, metadata={"amount_minor": inv.amount_minor, "status": inv.status})
    db.commit()
    flash(request, f"Invoice {inv.number} {'sent' if inv.status == 'sent' else 'saved as draft'}.")
    return redirect("/app/payments?tab=invoices")


@router.get("/app/payments/invoices/{inv_id}")
def invoice_view(inv_id: str, request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    inv = _ws_obj(db, ctx, Invoice, inv_id)
    return render(request, "pay/invoice.html", ctx, invoice=inv, host=db.get(User, inv.created_by), public=False)


@router.post("/app/payments/invoices/{inv_id}/send")
def invoice_send(inv_id: str, request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    _need_payments(ctx)
    inv = _ws_obj(db, ctx, Invoice, inv_id)
    try:
        C.send_invoice(db, inv, base_url=str(request.base_url))
    except C.CommerceError as exc:
        flash(request, str(exc), "error")
        return redirect("/app/payments?tab=invoices")
    audit(db, "invoice.sent", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="invoice", entity_id=inv.id)
    db.commit()
    flash(request, f"Invoice {inv.number} emailed to {inv.recipient_email}.")
    return redirect("/app/payments?tab=invoices")


@router.post("/app/payments/invoices/{inv_id}/void")
def invoice_void(inv_id: str, request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    _need_payments(ctx)
    inv = _ws_obj(db, ctx, Invoice, inv_id)
    try:
        C.void_invoice(db, inv)
    except C.CommerceError as exc:
        flash(request, str(exc), "error")
        return redirect("/app/payments?tab=invoices")
    audit(db, "invoice.voided", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="invoice", entity_id=inv.id)
    db.commit()
    flash(request, f"Invoice {inv.number} voided.")
    return redirect("/app/payments?tab=invoices")


@router.post("/app/payments/links")
def link_create(request: Request, description: str = Form(...), amount: str = Form(...), currency: str = Form("USD"),
                db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    _need_payments(ctx)
    try:
        link = C.create_link(db, ctx.workspace.id, ctx.user, description, amount, currency)
    except C.CommerceError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return redirect("/app/payments?tab=links")
    audit(db, "payment_link.created", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="payment_link",
          entity_id=link.id)
    db.commit()
    flash(request, "Payment link created.")
    return redirect("/app/payments?tab=links")


@router.post("/app/payments/links/{link_id}/toggle")
def link_toggle(link_id: str, request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    _need_payments(ctx)
    link = _ws_obj(db, ctx, PaymentLink, link_id)
    link.active = not link.active
    db.commit()
    flash(request, f"Payment link {'enabled' if link.active else 'disabled'}.")
    return redirect("/app/payments?tab=links")


# ------------------------------------------------------------------ public pages
def _by_token(db: Session, model, token: str):
    o = db.scalar(select(model).where(model.public_token == token))
    if not o:
        raise HTTPException(404, "Not found")
    return o


@router.get("/p/package/{token}")
def package_public(token: str, request: Request, db: Session = Depends(get_db)):
    p = _by_token(db, PackageProduct, token)
    et = db.get(EventType, p.event_type_id)
    return render(request, "pay/commerce_public.html", kind="package", obj=p, event_type=et,
                  host=db.get(User, p.owner_user_id), available=p.active, amount=p.price_minor, currency=p.currency)


@router.post("/p/package/{token}")
def package_buy(token: str, request: Request, name: str = Form(""), email: str = Form(...), db: Session = Depends(get_db)):
    enforce(request, "commerce_buy", limit=20, window_seconds=60)
    p = _by_token(db, PackageProduct, token)
    try:
        co = C.start_package_purchase(db, p, name, email, base_url=str(request.base_url))
        db.commit()
    except C.CommerceError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return redirect(f"/p/package/{token}")
    except Exception as exc:  # noqa: BLE001 - provider outage
        db.rollback()
        flash(request, f"The payment provider is unavailable right now. Please try again. ({exc.__class__.__name__})", "error")
        return redirect(f"/p/package/{token}")
    return redirect(co.checkout_url)


@router.get("/p/invoice/{token}")
def invoice_public(token: str, request: Request, db: Session = Depends(get_db)):
    inv = _by_token(db, Invoice, token)
    if inv.status == "draft":
        raise HTTPException(404, "Not found")
    return render(request, "pay/invoice.html", invoice=inv, host=db.get(User, inv.created_by), public=True)


@router.post("/p/invoice/{token}")
def invoice_pay(token: str, request: Request, db: Session = Depends(get_db)):
    enforce(request, "commerce_buy", limit=20, window_seconds=60)
    inv = _by_token(db, Invoice, token)
    if inv.status != "sent":
        flash(request, "This invoice can't be paid." if inv.status != "paid" else "This invoice is already paid.", "error")
        return redirect(f"/p/invoice/{token}")
    try:
        co = C.start_checkout(db, workspace_id=inv.workspace_id, kind="invoice", ref_id=inv.id, amount=inv.amount_minor,
                              currency=inv.currency, title=f"Invoice {inv.number}", owner_user_id=inv.created_by,
                              buyer_name=inv.recipient_name, buyer_email=inv.recipient_email,
                              base_url=str(request.base_url))
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        flash(request, f"The payment provider is unavailable right now. Please try again. ({exc.__class__.__name__})", "error")
        return redirect(f"/p/invoice/{token}")
    return redirect(co.checkout_url)


@router.get("/p/link/{token}")
def link_public(token: str, request: Request, db: Session = Depends(get_db)):
    link = _by_token(db, PaymentLink, token)
    return render(request, "pay/commerce_public.html", kind="link", obj=link, event_type=None,
                  host=db.get(User, link.created_by), available=link.active, amount=link.amount_minor,
                  currency=link.currency)


@router.post("/p/link/{token}")
def link_pay(token: str, request: Request, name: str = Form(""), email: str = Form(""), db: Session = Depends(get_db)):
    enforce(request, "commerce_buy", limit=20, window_seconds=60)
    link = _by_token(db, PaymentLink, token)
    if not link.active:
        flash(request, "This payment link is no longer active.", "error")
        return redirect(f"/p/link/{token}")
    try:
        if email:
            email = C._email(email)
        co = C.start_checkout(db, workspace_id=link.workspace_id, kind="link", ref_id=link.id, amount=link.amount_minor,
                              currency=link.currency, title=link.description, owner_user_id=link.created_by,
                              buyer_name=name, buyer_email=email, base_url=str(request.base_url))
        db.commit()
    except C.CommerceError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return redirect(f"/p/link/{token}")
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        flash(request, f"The payment provider is unavailable right now. Please try again. ({exc.__class__.__name__})", "error")
        return redirect(f"/p/link/{token}")
    return redirect(co.checkout_url)


# ------------------------------------------------------------------ commerce checkout pages
def _checkout(db: Session, access: str) -> CommerceCheckout:
    co = db.scalar(select(CommerceCheckout).where(CommerceCheckout.access_token == access))
    if not co:
        raise HTTPException(404, "Checkout not found")
    return co


def _back_url(db: Session, co: CommerceCheckout) -> str:
    if co.kind == "package":
        pur = db.get(PackagePurchase, co.ref_id)
        prod = db.get(PackageProduct, pur.product_id) if pur else None
        return f"/p/package/{prod.public_token}" if prod else "/"
    if co.kind == "invoice":
        inv = db.get(Invoice, co.ref_id)
        return f"/p/invoice/{inv.public_token}" if inv else "/"
    link = db.get(PaymentLink, co.ref_id)
    return f"/p/link/{link.public_token}" if link else "/"


@router.get("/pay/c/{access}/mock")
def commerce_mock(access: str, request: Request, db: Session = Depends(get_db)):
    co = _checkout(db, access)
    if co.provider != "mock":
        raise HTTPException(404, "Checkout not found")
    if co.status == "succeeded":
        return redirect(f"/pay/c/{access}/done")
    return render(request, "pay/commerce_mock.html", co=co, title=C.checkout_title(db, co))


@router.post("/pay/c/{access}/mock")
def commerce_mock_submit(access: str, request: Request, outcome: str = Form("success"), db: Session = Depends(get_db)):
    enforce(request, "pay_mock", limit=20, window_seconds=60)
    co = _checkout(db, access)
    if co.provider != "mock":
        raise HTTPException(404, "Checkout not found")
    if outcome == "success":
        C.complete_checkout(db, co, actor="buyer", base_url=str(request.base_url))
        return redirect(f"/pay/c/{access}/done")
    C.fail_checkout(db, co, "Card declined (simulated)" if outcome == "fail" else "Checkout cancelled")
    flash(request, "Payment was not completed. You can try again.", "error")
    return redirect(_back_url(db, co))


@router.get("/pay/c/{access}/return")
def commerce_return(access: str, request: Request, db: Session = Depends(get_db)):
    co = _checkout(db, access)
    if co.status != "succeeded":
        try:
            st = C.fetch_status(db, co)
        except Exception:  # noqa: BLE001
            st = "pending"
        if st == "succeeded":
            C.complete_checkout(db, co, actor="buyer", base_url=str(request.base_url))
        elif st == "failed":
            C.fail_checkout(db, co, "Payment was not completed")
            flash(request, "Payment was not completed. You can try again.", "error")
            return redirect(_back_url(db, co))
    return redirect(f"/pay/c/{access}/done")


@router.get("/pay/c/{access}/cancel")
def commerce_cancel(access: str, request: Request, db: Session = Depends(get_db)):
    co = _checkout(db, access)
    C.fail_checkout(db, co, "Checkout cancelled")
    return redirect(_back_url(db, co))


@router.get("/pay/c/{access}/done")
def commerce_done(access: str, request: Request, db: Session = Depends(get_db)):
    co = _checkout(db, access)
    return render(request, "pay/commerce_done.html", co=co, title=C.checkout_title(db, co))


# ------------------------------------------------------------------ package redemption on a booking's pay page
@router.post("/pay/{token}/redeem")
def redeem(token: str, request: Request, code: str = Form(...), db: Session = Depends(get_db)):
    from app.models import Booking

    enforce(request, "pkg_redeem", limit=10, window_seconds=60)
    b = db.scalar(select(Booking).where(Booking.manage_token == token))
    if not b:
        raise HTTPException(404, "Booking not found")
    try:
        purchase = C.redeem_package(db, b, code)
    except C.CommerceError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return redirect(f"/pay/{token}")
    left = purchase.sessions_total - purchase.sessions_used
    flash(request, f"Package session applied - {left} session{'s' if left != 1 else ''} left.")
    return redirect(f"/booking/{token}/confirmed")
