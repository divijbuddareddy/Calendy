"""Self-service subscription billing page (/app/billing) - ACC-005 / roadmap phase 4.
Mounted through routers/payments.py. See services/billing.py for the state machine."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import Ctx, Forbidden, flash, get_ctx, redirect, render
from app.entitlements import PLANS
from app.models import Workspace
from app.services import billing as B
from app.services import usage as U

router = APIRouter(include_in_schema=False)


def _owner(ctx: Ctx) -> None:
    if ctx.role != "owner":
        raise Forbidden("Only the workspace owner can manage billing.")


@router.get("/app/billing")
def billing_page(request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    if not ctx.can("admin"):
        raise Forbidden("Only workspace admins can view billing.")
    U.flush_usage(db)
    db.commit()
    acct = B.account(db, ctx.workspace.id)
    db.commit()
    sub = B.current_subscription(db, ctx.workspace.id)
    seats = B.seat_count(db, ctx.workspace.id)
    return render(request, "app/billing/index.html", ctx, nav="settings", plans=PLANS, order=B.PLAN_ORDER,
                  acct=acct, sub=sub, seats=seats, paid=B.has_paid_subscription(db, ctx.workspace.id),
                  usage=U.usage_summary(db, ctx.workspace.id), period_start=U.period_start(),
                  stripe=B.stripe_enabled(), monthly_total=B.monthly_total, rank=B.rank, is_owner=ctx.role == "owner")


@router.post("/app/billing/plan")
def billing_choose(request: Request, plan: str = Form(...), db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    _owner(ctx)
    ws = db.get(Workspace, ctx.workspace.id)
    try:
        res = B.start_plan_change(db, ws, plan, ctx.user, base_url=str(request.base_url))
    except B.BillingError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return redirect("/app/billing")
    except Exception as exc:  # noqa: BLE001 - Stripe outage
        db.rollback()
        flash(request, f"Billing provider unavailable, please try again ({exc.__class__.__name__}).", "error")
        return redirect("/app/billing")
    label = PLANS.get(plan, {}).get("label", plan)
    if res["action"] == "checkout":
        return redirect(res["url"])
    msg = {"upgraded": f"Upgraded to {label}. New features are available now.",
           "scheduled": f"Your plan will change to {label} at the end of the billing period.",
           "cancel_scheduled": "Your subscription will end at the end of the billing period.",
           "resumed": "Your subscription will continue as before.",
           "downgraded": f"Switched to {label}.",
           "noop": f"You're already on {label}."}[res["action"]]
    flash(request, msg)
    return redirect("/app/billing")


@router.post("/app/billing/cancel")
def billing_cancel(request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    _owner(ctx)
    try:
        B.cancel_at_period_end(db, db.get(Workspace, ctx.workspace.id), ctx.user)
        flash(request, "Your subscription will end at the end of the billing period. You keep paid features until then.")
    except B.BillingError as exc:
        flash(request, str(exc), "error")
    return redirect("/app/billing")


@router.post("/app/billing/resume")
def billing_resume(request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    _owner(ctx)
    B.resume(db, db.get(Workspace, ctx.workspace.id), ctx.user)
    flash(request, "Pending cancellation / downgrade removed.")
    return redirect("/app/billing")


@router.get("/app/billing/checkout/mock")
def billing_mock_checkout(request: Request, session: str = "", db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    _owner(ctx)
    acct = B.account(db, ctx.workspace.id)
    if not session or acct.pending_checkout_id != session:
        flash(request, "This checkout session expired. Choose a plan again.", "error")
        return redirect("/app/billing")
    plan = acct.pending_checkout_plan
    seats = B.seat_count(db, ctx.workspace.id)
    return render(request, "app/billing/mock_checkout.html", ctx, nav="settings", plan=plan, plan_info=PLANS[plan],
                  seats=seats, total=B.monthly_total(plan, seats), session=session)


@router.post("/app/billing/checkout/mock")
def billing_mock_checkout_submit(request: Request, session: str = Form(...), outcome: str = Form("success"),
                                 db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    _owner(ctx)
    ws = db.get(Workspace, ctx.workspace.id)
    if outcome != "success":
        acct = B.account(db, ws.id)
        acct.pending_checkout_id = acct.pending_checkout_plan = None
        db.commit()
        flash(request, "Checkout cancelled - your plan is unchanged.", "warning")
        return redirect("/app/billing")
    try:
        sub = B.complete_mock_checkout(db, ws, session, ctx.user)
    except B.BillingError as exc:
        flash(request, str(exc), "error")
        return redirect("/app/billing")
    flash(request, f"Subscribed to {PLANS[sub.plan]['label']} ({sub.seats} seat{'s' if sub.seats != 1 else ''}).")
    return redirect("/app/billing")


@router.get("/app/billing/return")
def billing_return(request: Request, session_id: str = "", db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    ws = db.get(Workspace, ctx.workspace.id)
    try:
        sub = B.complete_stripe_checkout(db, ws, session_id)
    except Exception as exc:  # noqa: BLE001
        flash(request, f"We couldn't confirm your subscription yet ({exc.__class__.__name__}); it will update "
                       "automatically when Stripe notifies us.", "warning")
        return redirect("/app/billing")
    flash(request, f"Subscribed to {PLANS[sub.plan]['label']}." if sub else
          "Your payment is processing; your plan will update shortly.")
    return redirect("/app/billing")
