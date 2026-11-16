"""Self-service subscription billing (ACC-005, roadmap phase 4 "self-service paid plans operate").

Plans/prices come from entitlements.PLANS (price_minor = per seat per month, USD).
Seats = active workspace members (re-synced by the `billing` periodic task).

  choose plan -> start_plan_change():
      no paid subscription yet  -> hosted checkout (Stripe Billing Checkout, mode=subscription,
                                   when STRIPE_SECRET_KEY is set; otherwise the mock checkout page)
      upgrade of a paid plan    -> applied immediately (Stripe: subscription item price swap, prorated)
      downgrade to a paid plan  -> scheduled for period end (BillingAccount.scheduled_plan)
      downgrade to Free         -> cancel at period end
  complete checkout -> Subscription row (models.Subscription) + Workspace.plan_id updated.
  Stripe webhooks (checkout.session.completed mode=subscription, customer.subscription.updated/
  deleted, invoice.payment_failed/paid) are routed here from payments.handle_stripe_event.
  The `billing` periodic task applies period-end changes (mock renewals, scheduled downgrades,
  cancellations) and syncs the seat count.
"""
from __future__ import annotations

import datetime as dt
import logging
import secrets
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit import audit
from app.config import settings
from app.entitlements import PLANS
from app.models import Subscription, User, Workspace, WorkspaceMember, utcnow
from app.models_commerce import BillingAccount
from app.worker import periodic

log = logging.getLogger("billing")
PLAN_ORDER = ["free", "standard", "teams", "enterprise"]
PERIOD = dt.timedelta(days=30)
ACTIVE_STATUSES = ("active", "trialing", "past_due")


class BillingError(Exception):
    pass


def stripe_enabled() -> bool:
    return bool(settings.stripe_secret_key)


def rank(plan: str) -> int:
    return PLAN_ORDER.index(plan) if plan in PLAN_ORDER else 0


def self_serve_plans() -> list[str]:
    return [p for p in PLAN_ORDER if p in PLANS and PLANS[p].get("price_minor") is not None]


def seat_count(db: Session, workspace_id: str) -> int:
    return max(1, db.scalar(select(func.count(WorkspaceMember.id)).where(
        WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.status == "active",
        WorkspaceMember.user_id.is_not(None))) or 1)


def account(db: Session, workspace_id: str) -> BillingAccount:
    acct = db.scalar(select(BillingAccount).where(BillingAccount.workspace_id == workspace_id))
    if not acct:
        acct = BillingAccount(workspace_id=workspace_id, provider="stripe" if stripe_enabled() else "mock")
        db.add(acct)
        db.flush()
    return acct


def current_subscription(db: Session, workspace_id: str) -> Optional[Subscription]:
    acct = db.scalar(select(BillingAccount).where(BillingAccount.workspace_id == workspace_id))
    if acct and acct.subscription_id:
        sub = db.get(Subscription, acct.subscription_id)
        if sub:
            return sub
    return None


def has_paid_subscription(db: Session, workspace_id: str) -> bool:
    sub = current_subscription(db, workspace_id)
    return bool(sub and sub.status in ACTIVE_STATUSES and sub.plan != "free")


def monthly_total(plan: str, seats: int) -> int:
    return (PLANS.get(plan, {}).get("price_minor") or 0) * seats


def _stripe(method: str, path: str, **kw):
    from app.services.providers.payments_stripe import _req

    return _req(method, path, **kw)


def _price_data(prefix: str, plan: str) -> dict:
    p = PLANS[plan]
    return {
        f"{prefix}[price_data][currency]": "usd",
        f"{prefix}[price_data][unit_amount]": p["price_minor"],
        f"{prefix}[price_data][recurring][interval]": "month",
        f"{prefix}[price_data][product_data][name]": f"{settings.app_name} {p['label']} (per seat)",
    }


# ------------------------------------------------------------------ plan changes
def start_plan_change(db: Session, ws: Workspace, plan: str, actor: User, base_url: Optional[str] = None) -> dict:
    """Returns {"action": "checkout", "url"} | {"action": "upgraded"|"scheduled"|"cancel_scheduled"|"noop"|"resumed"}.
    Commits."""
    if plan not in PLANS:
        raise BillingError("Unknown plan")
    if PLANS[plan].get("price_minor") is None:
        raise BillingError(f"The {PLANS[plan]['label']} plan is sold by our team - contact sales.")
    acct = account(db, ws.id)
    sub = current_subscription(db, ws.id)
    paid = has_paid_subscription(db, ws.id)
    base = (base_url or settings.base_url).rstrip("/")
    if not paid:
        if plan == "free":
            if ws.plan_id != "free":
                old = ws.plan_id
                ws.plan_id = "free"  # leaving a trial / manual plan
                audit(db, "billing.plan_changed", workspace_id=ws.id, actor_id=actor.id, entity_type="workspace",
                      entity_id=ws.id, metadata={"from": old, "to": "free"})
                db.commit()
                return {"action": "downgraded"}
            return {"action": "noop"}
        seats = seat_count(db, ws.id)
        if stripe_enabled():
            data = {"mode": "subscription", "client_reference_id": ws.id,
                    "success_url": f"{base}/app/billing/return?session_id={{CHECKOUT_SESSION_ID}}",
                    "cancel_url": f"{base}/app/billing?checkout=cancelled",
                    "line_items[0][quantity]": seats, **_price_data("line_items[0]", plan),
                    "metadata[billing_workspace_id]": ws.id, "metadata[plan]": plan,
                    "subscription_data[metadata][billing_workspace_id]": ws.id,
                    "subscription_data[metadata][plan]": plan}
            if acct.customer_id:
                data["customer"] = acct.customer_id
            else:
                data["customer_email"] = actor.email
            s = _stripe("POST", "/checkout/sessions", data=data, idem=f"billing:{ws.id}:{plan}:{secrets.token_hex(4)}")
            acct.provider, acct.pending_checkout_id, acct.pending_checkout_plan = "stripe", s["id"], plan
            url = s["url"]
        else:
            sid = "mock_bcs_" + secrets.token_hex(10)
            acct.provider, acct.pending_checkout_id, acct.pending_checkout_plan = "mock", sid, plan
            url = f"/app/billing/checkout/mock?session={sid}"
        audit(db, "billing.checkout_started", workspace_id=ws.id, actor_id=actor.id, entity_type="workspace",
              entity_id=ws.id, metadata={"plan": plan, "seats": seats})
        db.commit()
        return {"action": "checkout", "url": url}

    # existing paid subscription
    if plan == sub.plan:
        if acct.cancel_at_period_end or acct.scheduled_plan:
            return resume(db, ws, actor)
        return {"action": "noop"}
    if plan == "free":
        return cancel_at_period_end(db, ws, actor)
    if rank(plan) > rank(sub.plan):
        if acct.provider == "stripe" and sub.provider_subscription_id and stripe_enabled():
            s = _stripe("GET", f"/subscriptions/{sub.provider_subscription_id}")
            item = s["items"]["data"][0]["id"]
            _stripe("POST", f"/subscriptions/{sub.provider_subscription_id}", data={
                "items[0][id]": item, **_price_data("items[0]", plan), "items[0][quantity]": seat_count(db, ws.id),
                "proration_behavior": "create_prorations", "cancel_at_period_end": "false",
                "metadata[plan]": plan, "metadata[billing_workspace_id]": ws.id})
        old = sub.plan
        sub.plan, ws.plan_id = plan, plan
        acct.scheduled_plan, acct.cancel_at_period_end = None, False
        audit(db, "billing.plan_changed", workspace_id=ws.id, actor_id=actor.id, entity_type="workspace",
              entity_id=ws.id, metadata={"from": old, "to": plan, "when": "immediate"})
        db.commit()
        return {"action": "upgraded"}
    acct.scheduled_plan, acct.cancel_at_period_end = plan, False
    audit(db, "billing.downgrade_scheduled", workspace_id=ws.id, actor_id=actor.id, entity_type="workspace",
          entity_id=ws.id, metadata={"from": sub.plan, "to": plan, "at": sub.period_end.isoformat() if sub.period_end else None})
    db.commit()
    return {"action": "scheduled"}


def cancel_at_period_end(db: Session, ws: Workspace, actor: User) -> dict:
    sub = current_subscription(db, ws.id)
    if not sub or sub.status not in ACTIVE_STATUSES:
        raise BillingError("There is no active subscription to cancel")
    acct = account(db, ws.id)
    if acct.provider == "stripe" and sub.provider_subscription_id and stripe_enabled():
        _stripe("POST", f"/subscriptions/{sub.provider_subscription_id}", data={"cancel_at_period_end": "true"})
    acct.cancel_at_period_end, acct.scheduled_plan = True, None
    audit(db, "billing.cancel_scheduled", workspace_id=ws.id, actor_id=actor.id, entity_type="workspace",
          entity_id=ws.id, metadata={"at": sub.period_end.isoformat() if sub.period_end else None})
    db.commit()
    return {"action": "cancel_scheduled"}


def resume(db: Session, ws: Workspace, actor: User) -> dict:
    sub = current_subscription(db, ws.id)
    acct = account(db, ws.id)
    if acct.provider == "stripe" and sub and sub.provider_subscription_id and stripe_enabled() and acct.cancel_at_period_end:
        _stripe("POST", f"/subscriptions/{sub.provider_subscription_id}", data={"cancel_at_period_end": "false"})
    acct.cancel_at_period_end, acct.scheduled_plan = False, None
    audit(db, "billing.resumed", workspace_id=ws.id, actor_id=actor.id, entity_type="workspace", entity_id=ws.id)
    db.commit()
    return {"action": "resumed"}


def activate(db: Session, ws: Workspace, plan: str, *, provider_subscription_id: str, customer_id: Optional[str] = None,
             period_end: Optional[dt.datetime] = None, seats: Optional[int] = None, actor: str = "system") -> Subscription:
    """Idempotently create/refresh the Subscription and switch the workspace plan. Caller commits."""
    acct = account(db, ws.id)
    sub = db.scalar(select(Subscription).where(Subscription.provider_subscription_id == provider_subscription_id))
    if not sub:
        sub = Subscription(workspace_id=ws.id, provider_subscription_id=provider_subscription_id, plan=plan)
        db.add(sub)
    sub.plan, sub.status = plan, "active"
    sub.seats = seats or seat_count(db, ws.id)
    sub.period_end = period_end or (utcnow() + PERIOD)
    db.flush()
    old = ws.plan_id
    ws.plan_id = plan
    acct.subscription_id = sub.id
    acct.customer_id = customer_id or acct.customer_id
    acct.pending_checkout_id = acct.pending_checkout_plan = None
    acct.cancel_at_period_end, acct.scheduled_plan = False, None
    if old != plan:
        audit(db, "billing.plan_changed", workspace_id=ws.id, actor_id=actor, entity_type="workspace",
              entity_id=ws.id, metadata={"from": old, "to": plan, "subscription": provider_subscription_id})
    db.flush()
    return sub


def complete_mock_checkout(db: Session, ws: Workspace, session_id: str, actor: User) -> Subscription:
    acct = account(db, ws.id)
    if not session_id or acct.pending_checkout_id != session_id or not acct.pending_checkout_plan:
        raise BillingError("This checkout session is no longer valid")
    sub = activate(db, ws, acct.pending_checkout_plan, provider_subscription_id="mock_sub_" + secrets.token_hex(8),
                   customer_id=acct.customer_id or "mock_cus_" + secrets.token_hex(6), actor=actor.id)
    db.commit()
    return sub


def _ts(v) -> Optional[dt.datetime]:
    return dt.datetime.fromtimestamp(int(v), dt.timezone.utc).replace(tzinfo=None) if v else None


def complete_stripe_checkout(db: Session, ws: Workspace, session_id: str) -> Optional[Subscription]:
    s = _stripe("GET", f"/checkout/sessions/{session_id}")
    if (s.get("metadata") or {}).get("billing_workspace_id") != ws.id:
        raise BillingError("Checkout session does not belong to this workspace")
    if s.get("status") != "complete" or not s.get("subscription"):
        return None
    sub_obj = _stripe("GET", f"/subscriptions/{s['subscription']}")
    sub = activate(db, ws, (s.get("metadata") or {}).get("plan") or "standard", provider_subscription_id=sub_obj["id"],
                   customer_id=s.get("customer"), period_end=_ts(sub_obj.get("current_period_end")))
    db.commit()
    return sub


# ------------------------------------------------------------------ webhooks
def handle_stripe_event(db: Session, event: dict) -> bool:
    """Returns True when the event was a billing event (handled here)."""
    etype = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    meta = obj.get("metadata") or {}
    wsid = meta.get("billing_workspace_id")
    if etype == "checkout.session.completed" and obj.get("mode") == "subscription":
        ws = db.get(Workspace, wsid or obj.get("client_reference_id") or "")
        if ws and obj.get("subscription"):
            period_end = None
            try:
                period_end = _ts(_stripe("GET", f"/subscriptions/{obj['subscription']}").get("current_period_end"))
            except Exception:  # noqa: BLE001 - the subscription.updated event will carry it
                pass
            activate(db, ws, meta.get("plan") or "standard", provider_subscription_id=obj["subscription"],
                     customer_id=obj.get("customer"), period_end=period_end)
            db.commit()
        return True
    if etype.startswith("customer.subscription."):
        sub = db.scalar(select(Subscription).where(Subscription.provider_subscription_id == obj.get("id")))
        ws = db.get(Workspace, sub.workspace_id) if sub else db.get(Workspace, wsid or "")
        if not ws:
            return True
        acct = account(db, ws.id)
        if etype == "customer.subscription.deleted" or obj.get("status") in ("canceled", "incomplete_expired"):
            if sub:
                sub.status = "canceled"
            if not sub or acct.subscription_id == sub.id:
                ws.plan_id = "free"
            acct.cancel_at_period_end = False
            audit(db, "billing.subscription_ended", workspace_id=ws.id, actor_id="stripe", entity_type="workspace",
                  entity_id=ws.id)
        else:
            if not sub:
                sub = activate(db, ws, meta.get("plan") or "standard", provider_subscription_id=obj["id"],
                               customer_id=obj.get("customer"), period_end=_ts(obj.get("current_period_end")))
            sub.status = obj.get("status") or sub.status
            sub.period_end = _ts(obj.get("current_period_end")) or sub.period_end
            qty = ((obj.get("items") or {}).get("data") or [{}])[0].get("quantity")
            if qty:
                sub.seats = int(qty)
            if meta.get("plan") in PLANS and sub.status in ACTIVE_STATUSES:
                sub.plan = ws.plan_id = meta["plan"]
            acct.cancel_at_period_end = bool(obj.get("cancel_at_period_end"))
        db.commit()
        return True
    if etype in ("invoice.payment_failed", "invoice.paid"):
        sub = db.scalar(select(Subscription).where(Subscription.provider_subscription_id == obj.get("subscription")))
        if sub:
            if etype == "invoice.payment_failed":
                sub.status = "past_due"
                _alert_owner(db, sub.workspace_id, "Payment for your subscription failed",
                             "We couldn't charge your card for this billing period. Update your payment method to "
                             "keep your plan's features.")
            elif sub.status == "past_due":
                sub.status = "active"
            db.commit()
        return True
    return False


def _alert_owner(db: Session, workspace_id: str, title: str, message: str) -> None:
    from app.services.notifications import notify_host_alert

    ws = db.get(Workspace, workspace_id)
    owner = db.get(User, ws.owner_user_id) if ws else None
    if owner:
        notify_host_alert(db, owner, title, message, link="/app/billing", workspace_id=workspace_id,
                          kind="billing_alert", dedupe_key=f"billing:{workspace_id}:{title}:{utcnow():%Y%m%d}")


# ------------------------------------------------------------------ periodic: period end + seats
def run_billing_cycle(db: Session, now: Optional[dt.datetime] = None) -> int:
    now = now or utcnow()
    changed = 0
    for acct in db.scalars(select(BillingAccount).where(BillingAccount.subscription_id.is_not(None))).all():
        sub = db.get(Subscription, acct.subscription_id)
        ws = db.get(Workspace, acct.workspace_id)
        if not sub or not ws or sub.status not in ACTIVE_STATUSES:
            continue
        # seat sync (seat count = active members)
        seats = seat_count(db, ws.id)
        if seats != sub.seats:
            if acct.provider == "stripe" and sub.provider_subscription_id and stripe_enabled():
                try:
                    s = _stripe("GET", f"/subscriptions/{sub.provider_subscription_id}")
                    _stripe("POST", f"/subscriptions/{sub.provider_subscription_id}", data={
                        "items[0][id]": s["items"]["data"][0]["id"], "items[0][quantity]": seats,
                        "proration_behavior": "create_prorations"})
                except Exception as exc:  # noqa: BLE001 - retried next cycle
                    log.warning("seat sync failed for %s: %s", ws.id, exc)
                    continue
            audit(db, "billing.seats_changed", workspace_id=ws.id, actor_id="system", entity_type="workspace",
                  entity_id=ws.id, metadata={"from": sub.seats, "to": seats})
            sub.seats = seats
            acct.last_seat_sync_at = now
            changed += 1
        if not sub.period_end or sub.period_end > now:
            continue
        # period end reached
        if acct.cancel_at_period_end:
            if acct.provider == "stripe" and stripe_enabled():
                continue  # Stripe sends customer.subscription.deleted
            sub.status, ws.plan_id, acct.cancel_at_period_end = "canceled", "free", False
            audit(db, "billing.subscription_ended", workspace_id=ws.id, actor_id="system", entity_type="workspace",
                  entity_id=ws.id)
        elif acct.scheduled_plan:
            new = acct.scheduled_plan
            if acct.provider == "stripe" and sub.provider_subscription_id and stripe_enabled():
                try:
                    s = _stripe("GET", f"/subscriptions/{sub.provider_subscription_id}")
                    _stripe("POST", f"/subscriptions/{sub.provider_subscription_id}", data={
                        "items[0][id]": s["items"]["data"][0]["id"], **_price_data("items[0]", new),
                        "proration_behavior": "none", "metadata[plan]": new})
                except Exception as exc:  # noqa: BLE001
                    log.warning("scheduled downgrade failed for %s: %s", ws.id, exc)
                    continue
            audit(db, "billing.plan_changed", workspace_id=ws.id, actor_id="system", entity_type="workspace",
                  entity_id=ws.id, metadata={"from": sub.plan, "to": new, "when": "period_end"})
            sub.plan, ws.plan_id, acct.scheduled_plan = new, new, None
            sub.period_end = sub.period_end + PERIOD
        elif acct.provider != "stripe" or not stripe_enabled():
            sub.period_end = sub.period_end + PERIOD  # mock renewal
        else:
            continue
        changed += 1
    db.flush()
    return changed


@periodic("billing")
def billing_task(db: Session) -> None:
    run_billing_cycle(db)
