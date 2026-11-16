"""Integrations hub: calendars (Google / Microsoft / demo), video (Zoom / Meet / Teams), payments
(Stripe / PayPal), outbound webhooks, API keys, ICS subscription feed."""
import datetime as dt
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

# Importing these modules registers their @subscribe / @periodic handlers.
from app.services import notifications as _notifications  # noqa: F401
from app.services import payments as payments_service
from app.services import webhooks as webhooks_service
from app.services import workflows as _workflows  # noqa: F401
from app.services import usage as _usage  # noqa: F401  (usage meters: subscribers + flush task)
from app.services import observability as _observability  # noqa: F401  (worker heartbeat task)
from app.services.providers import registry
from app.services.providers import sync as cal_sync

from app.audit import audit
from app.config import settings
from app.db import get_db
from app.deps import Ctx, Forbidden, flash, get_ctx, redirect, render
from app.entitlements import limit as plan_limit
from app.models import (
    ApiKey,
    Booking,
    BookingHost,
    CalendarConnection,
    CalendarEvent,
    ConnectedCalendar,
    ExternalCalendarEvent,
    PaymentAccount,
    PaymentRecord,
    User,
    WebhookDelivery,
    WebhookSubscription,
    utcnow,
)
from app.security import hash_secret, sign, unsign
from app.services import apikeys
from app.services import ics_feed as ics_feed_service
from app.services.providers.base import get_tokens, set_tokens
from app.services.providers.mock import new_mock_account_id

router = APIRouter()
# OAuth 2.0 provider (+ management UI) lives in its own module; mounted here so main.py is unchanged.
from app.routers.oauth import router as _oauth_router  # noqa: E402

router.include_router(_oauth_router)
OAUTH_SALT = "oauth-integration"
PROVIDER_LABELS = {"google": "Google Calendar", "microsoft": "Microsoft Outlook / 365", "mock": "Demo calendar",
                   "zoom": "Zoom"}


# ------------------------------------------------------------------ helpers
def _conn(db: Session, ctx: Ctx, conn_id: str) -> CalendarConnection:
    c = db.get(CalendarConnection, conn_id)
    if not c or c.user_id != ctx.user.id or c.workspace_id != ctx.workspace.id:
        raise HTTPException(404, "Connection not found")
    return c


def _redirect_uri(request: Request, provider: str) -> str:
    base = settings.base_url.rstrip("/") if settings.base_url else str(request.base_url).rstrip("/")
    return f"{base}/app/integrations/{provider}/callback"


def _calendar_limit_ok(db: Session, ctx: Ctx) -> bool:
    lim = plan_limit(ctx.workspace, "max_calendars")
    if lim is None:
        return True
    n = db.scalar(select(func.count(CalendarConnection.id)).where(
        CalendarConnection.user_id == ctx.user.id, CalendarConnection.provider.in_(registry.CALENDAR_PROVIDERS))) or 0
    return n < lim




def _tabs(active: str) -> dict:
    return {"nav": "integrations", "tab": active}


# ------------------------------------------------------------------ overview
@router.get("/app/integrations", include_in_schema=False)
def integrations_home(request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    conns = registry.calendar_connections(db, ctx.user.id)
    rows = []
    for c in conns:
        cals = registry.calendars_of(db, c)
        busy_count = 0
        if cals:
            busy_count = db.scalar(select(func.count(ExternalCalendarEvent.id)).where(
                ExternalCalendarEvent.calendar_id.in_([x.id for x in cals]), ExternalCalendarEvent.end_at > utcnow(),
                ExternalCalendarEvent.booking_id.is_(None))) or 0
        rows.append({"conn": c, "cals": cals, "label": PROVIDER_LABELS.get(c.provider, c.provider),
                     "busy_count": busy_count, "scopes": (c.scopes or "").split()})
    zoom = registry.video_connection(db, ctx.user.id, "zoom")
    accounts = db.scalars(select(PaymentAccount).where(
        PaymentAccount.user_id == ctx.user.id, PaymentAccount.workspace_id == ctx.workspace.id)).all()
    payments = db.scalars(select(PaymentRecord).where(PaymentRecord.workspace_id == ctx.workspace.id)
                          .order_by(PaymentRecord.created_at.desc()).limit(15)).all()
    failed_events = db.scalar(select(func.count(CalendarEvent.id)).where(
        CalendarEvent.user_id == ctx.user.id, CalendarEvent.sync_status == "failed")) or 0
    feed = ics_feed_service.current(db, ctx.user.id)
    new_feed_raw = request.session.pop("_new_ics_feed", None)
    feed_url = ics_feed_service.feed_url(ctx.user.id, new_feed_raw) if new_feed_raw else None
    return render(
        request, "app/integrations/index.html", ctx, **_tabs("calendars"),
        rows=rows, zoom=zoom, accounts=accounts, payments=payments, failed_events=failed_events, feed_url=feed_url, feed=feed,
        google_configured=registry.calendar_adapter("google").is_configured(),
        microsoft_configured=registry.calendar_adapter("microsoft").is_configured(),
        zoom_configured=bool(settings.zoom_client_id and settings.zoom_client_secret),
        stripe_configured=bool(settings.stripe_secret_key),
        paypal_configured=bool(settings.paypal_client_id and settings.paypal_secret),
        revenue=payments_service.revenue_summary(db, ctx.workspace.id),
        provider_labels=PROVIDER_LABELS, is_mock_zoom=bool(zoom and (zoom.sync_state_json or {}).get("mock")),
    )


# ------------------------------------------------------------------ demo calendar
@router.post("/app/integrations/mock/connect", include_in_schema=False)
def connect_mock(request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    if not _calendar_limit_ok(db, ctx):
        flash(request, "Your plan's calendar connection limit is reached. Upgrade to connect more calendars.", "error")
        return redirect("/app/integrations")
    conn = cal_sync.connect_mock_calendar(db, ctx.user, ctx.workspace.id)
    # Seed one realistic busy block tomorrow 12:00-13:00 in the host's timezone.
    personal = next(c for c in registry.calendars_of(db, conn) if c.display_name == "Personal")
    tz = ZoneInfo(ctx.user.default_timezone or "UTC")
    tomorrow = (dt.datetime.now(tz) + dt.timedelta(days=1)).date()
    start = dt.datetime.combine(tomorrow, dt.time(12, 0), tz).astimezone(dt.timezone.utc).replace(tzinfo=None)
    db.add(ExternalCalendarEvent(calendar_id=personal.id, external_id=f"seed-{conn.id}", title="Lunch with Sam",
                                 start_at=start, end_at=start + dt.timedelta(hours=1)))
    audit(db, "calendar.connected", workspace_id=ctx.workspace.id, actor_id=ctx.user.id,
          entity_type="calendar_connection", entity_id=conn.id, metadata={"provider": "mock"})
    db.commit()
    flash(request, "Demo calendar connected with 'Work' and 'Personal' calendars. Add busy events to test conflicts.")
    return redirect("/app/integrations")


# ------------------------------------------------------------------ OAuth (google / microsoft / zoom)
@router.get("/app/integrations/{provider}/connect", include_in_schema=False)
def oauth_connect(provider: str, request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    if provider not in ("google", "microsoft", "zoom"):
        raise HTTPException(404)
    if provider == "zoom":
        from app.services.providers.video import ZoomProvider

        adapter = ZoomProvider()
    else:
        adapter = registry.calendar_adapter(provider)
        if not _calendar_limit_ok(db, ctx):
            flash(request, "Your plan's calendar connection limit is reached. Upgrade to connect more calendars.", "error")
            return redirect("/app/integrations")
    if not adapter.is_configured():
        if provider == "zoom":
            return _connect_mock_zoom(request, db, ctx)
        flash(request, f"{PROVIDER_LABELS[provider]} OAuth isn't configured on this server "
                       f"({provider.upper()}_CLIENT_ID / _SECRET). Use the demo calendar to try everything offline.", "warning")
        return redirect("/app/integrations")
    state = sign({"uid": ctx.user.id, "ws": ctx.workspace.id, "p": provider, "reconnect": request.query_params.get("reconnect")}, OAUTH_SALT)
    return redirect(adapter.authorize_url(state, _redirect_uri(request, provider)), 302)


def _connect_mock_zoom(request: Request, db: Session, ctx: Ctx):
    existing = registry.video_connection(db, ctx.user.id, "zoom")
    if existing:
        existing.status, existing.last_error = "connected", None
    else:
        db.add(CalendarConnection(workspace_id=ctx.workspace.id, user_id=ctx.user.id, provider="zoom",
                                  account_email=ctx.user.email, scopes="meeting:write user:read", status="connected",
                                  sync_state_json={"mock": True, "kind": "video"}))
    audit(db, "video.connected", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, metadata={"provider": "zoom", "mock": True})
    db.commit()
    flash(request, "Zoom (demo mode) connected. Event types with a Zoom location now get auto-generated meeting links.")
    return redirect("/app/integrations")


@router.post("/app/integrations/zoom/connect-demo", include_in_schema=False)
def zoom_demo(request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    return _connect_mock_zoom(request, db, ctx)


@router.get("/app/integrations/{provider}/callback", include_in_schema=False)
def oauth_callback(provider: str, request: Request, code: str = "", state: str = "", error: str = "",
                   db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    data = unsign(state, OAUTH_SALT, max_age=900)
    if not data or data.get("uid") != ctx.user.id or data.get("p") != provider:
        flash(request, "The connection request expired or was invalid. Please try again.", "error")
        return redirect("/app/integrations")
    if error or not code:
        flash(request, f"Connection cancelled: {error or 'no authorization code'}", "warning")
        return redirect("/app/integrations")
    if provider == "zoom":
        from app.services.providers.video import ZoomProvider

        adapter = ZoomProvider()
    else:
        adapter = registry.calendar_adapter(provider)
    try:
        tokens = adapter.exchange_code(code, _redirect_uri(request, provider))
    except Exception as exc:  # noqa: BLE001
        flash(request, f"Could not complete the connection: {exc}", "error")
        return redirect("/app/integrations")
    email = tokens.pop("account_email", None)
    conn = db.scalar(select(CalendarConnection).where(
        CalendarConnection.user_id == ctx.user.id, CalendarConnection.provider == provider,
        CalendarConnection.account_email == email)) if email else None
    if conn is None:
        conn = CalendarConnection(workspace_id=ctx.workspace.id, user_id=ctx.user.id, provider=provider,
                                  account_email=email, sync_state_json={"kind": "video"} if provider == "zoom" else {})
        db.add(conn)
    old = get_tokens(conn)
    if not tokens.get("refresh_token") and old.get("refresh_token"):
        tokens["refresh_token"] = old["refresh_token"]
    set_tokens(conn, tokens)
    conn.scopes = tokens.get("scopes")
    conn.status, conn.last_error = "connected", None
    db.flush()
    if provider != "zoom":
        try:
            infos = adapter.list_calendars(db, conn)
        except Exception as exc:  # noqa: BLE001
            infos = []
            conn.last_error = f"Could not list calendars: {exc}"
        existing = {c.provider_calendar_id: c for c in registry.calendars_of(db, conn)}
        has_write = registry.write_target(db, ctx.user.id)[1] is not None
        for info in infos:
            if info.provider_calendar_id in existing:
                existing[info.provider_calendar_id].display_name = info.display_name
                continue
            make_write = info.primary and info.can_write and not has_write
            has_write = has_write or make_write
            db.add(ConnectedCalendar(connection_id=conn.id, provider_calendar_id=info.provider_calendar_id,
                                     display_name=info.display_name, is_conflict_calendar=info.primary,
                                     is_write_calendar=make_write))
        db.flush()
        cal_sync.sync_connection(db, conn)
    audit(db, "calendar.connected" if provider != "zoom" else "video.connected", workspace_id=ctx.workspace.id,
          actor_id=ctx.user.id, entity_type="calendar_connection", entity_id=conn.id, metadata={"provider": provider})
    db.commit()
    flash(request, f"{PROVIDER_LABELS.get(provider, provider)} connected{(' as ' + email) if email else ''}.")
    return redirect("/app/integrations")


# ------------------------------------------------------------------ connection management
@router.post("/app/integrations/calendars", include_in_schema=False)
async def save_calendar_settings(request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    form = await request.form()
    write_id = form.get("write_calendar")
    conflict_ids = set(form.getlist("conflict"))
    for conn in registry.calendar_connections(db, ctx.user.id):
        for cal in registry.calendars_of(db, conn):
            cal.is_conflict_calendar = cal.id in conflict_ids
            cal.is_write_calendar = cal.id == write_id
        registry.invalidate_busy_cache(conn.id)
    audit(db, "calendar.settings_changed", workspace_id=ctx.workspace.id, actor_id=ctx.user.id,
          metadata={"write_calendar": write_id, "conflict_calendars": sorted(conflict_ids)})
    db.commit()
    flash(request, "Calendar settings saved. Availability now checks the selected calendars.")
    return redirect("/app/integrations")


@router.post("/app/integrations/connections/{conn_id}/disconnect", include_in_schema=False)
def disconnect(conn_id: str, request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    conn = _conn(db, ctx, conn_id)
    label = PROVIDER_LABELS.get(conn.provider, conn.provider)
    try:
        if conn.provider == "zoom":
            from app.services.providers.video import ZoomProvider

            ZoomProvider().revoke(db, conn)
        else:
            adapter = registry.calendar_adapter(conn.provider)
            if adapter and conn.provider != "mock":
                adapter.revoke(db, conn)
    except Exception:  # noqa: BLE001 - revoke is best effort
        pass
    registry.invalidate_busy_cache(conn.id)
    cal_ids = [cal.id for cal in registry.calendars_of(db, conn)]
    if cal_ids:
        db.query(ExternalCalendarEvent).filter(ExternalCalendarEvent.calendar_id.in_(cal_ids)).delete(synchronize_session=False)
        db.query(ConnectedCalendar).filter(ConnectedCalendar.id.in_(cal_ids)).delete(synchronize_session=False)
    audit(db, "calendar.disconnected" if conn.provider != "zoom" else "video.disconnected", workspace_id=ctx.workspace.id,
          actor_id=ctx.user.id, entity_type="calendar_connection", entity_id=conn.id, metadata={"provider": conn.provider})
    db.delete(conn)
    db.commit()
    flash(request, f"{label} disconnected. Its events no longer block your availability and new bookings won't be "
                   f"written to it. Existing calendar events were left in place.")
    return redirect("/app/integrations")


@router.post("/app/integrations/connections/{conn_id}/reconnect", include_in_schema=False)
def reconnect(conn_id: str, request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    conn = _conn(db, ctx, conn_id)
    if conn.provider == "mock" or (conn.sync_state_json or {}).get("mock"):
        conn.status, conn.last_error = "connected", None
        cal_sync.sync_connection(db, conn)
        registry.retry_failed_events(db, now=utcnow() + dt.timedelta(days=1))
        audit(db, "calendar.reconnected", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_id=conn.id)
        db.commit()
        flash(request, f"{PROVIDER_LABELS.get(conn.provider, conn.provider)} reconnected.")
        return redirect("/app/integrations")
    return redirect(f"/app/integrations/{conn.provider}/connect?reconnect={conn.id}")


@router.post("/app/integrations/connections/{conn_id}/sync", include_in_schema=False)
def sync_now(conn_id: str, request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    conn = _conn(db, ctx, conn_id)
    if conn.status != "connected":
        flash(request, "Reconnect this calendar before syncing.", "error")
        return redirect("/app/integrations")
    cal_sync.sync_connection(db, conn)
    registry.retry_failed_events(db, now=utcnow() + dt.timedelta(days=1))
    db.commit()
    if conn.status == "connected" and not conn.last_error:
        flash(request, "Calendar synced.")
    else:
        flash(request, f"Sync problem: {conn.last_error}", "error")
    return redirect(request.headers.get("referer") or "/app/integrations")


@router.post("/app/integrations/connections/{conn_id}/simulate-expiry", include_in_schema=False)
def simulate_expiry(conn_id: str, request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    conn = _conn(db, ctx, conn_id)
    if conn.provider != "mock" and not (conn.sync_state_json or {}).get("mock"):
        raise HTTPException(400, "Only demo connections can simulate token expiry")
    registry.mark_connection_error(db, conn, "Simulated: OAuth refresh token expired or was revoked")
    db.commit()
    flash(request, "Token expiry simulated. Booking pages now refuse to show unverified availability until you reconnect.", "warning")
    return redirect("/app/integrations")


# ------------------------------------------------------------------ demo calendar event editor
@router.get("/app/integrations/connections/{conn_id}/events", include_in_schema=False)
def mock_events(conn_id: str, request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    conn = _conn(db, ctx, conn_id)
    cals = registry.calendars_of(db, conn)
    cal_map = {c.id: c for c in cals}
    events = db.scalars(select(ExternalCalendarEvent).where(
        ExternalCalendarEvent.calendar_id.in_(list(cal_map) or ["-"]),
        ExternalCalendarEvent.end_at > utcnow() - dt.timedelta(days=1)).order_by(ExternalCalendarEvent.start_at)).all()
    tz = ctx.user.default_timezone or "UTC"
    today = dt.datetime.now(ZoneInfo(tz)).date() + dt.timedelta(days=1)
    return render(request, "app/integrations/mock_calendar.html", ctx, **_tabs("calendars"), conn=conn, cals=cals,
                  cal_map=cal_map, events=events, tz=tz, default_date=today.isoformat(),
                  label=PROVIDER_LABELS.get(conn.provider, conn.provider))


@router.post("/app/integrations/connections/{conn_id}/events", include_in_schema=False)
def add_mock_event(conn_id: str, request: Request, calendar_id: str = Form(...), title: str = Form("Busy"),
                   date: str = Form(...), start: str = Form(...), end: str = Form(...), transparent: str = Form(""),
                   db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    conn = _conn(db, ctx, conn_id)
    if conn.provider != "mock":
        raise HTTPException(400, "Events can only be added to the demo calendar here")
    cal = db.get(ConnectedCalendar, calendar_id)
    if not cal or cal.connection_id != conn.id:
        raise HTTPException(404)
    tz = ZoneInfo(ctx.user.default_timezone or "UTC")
    try:
        d = dt.date.fromisoformat(date)
        s = dt.datetime.combine(d, dt.time.fromisoformat(start), tz).astimezone(dt.timezone.utc).replace(tzinfo=None)
        e = dt.datetime.combine(d, dt.time.fromisoformat(end), tz).astimezone(dt.timezone.utc).replace(tzinfo=None)
    except ValueError:
        flash(request, "Invalid date or time", "error")
        return redirect(f"/app/integrations/connections/{conn_id}/events")
    if e <= s:
        e += dt.timedelta(days=1)
    from app.security import new_id

    db.add(ExternalCalendarEvent(calendar_id=cal.id, external_id=new_id("ext"), title=(title or "Busy")[:500],
                                 start_at=s, end_at=e, transparent=bool(transparent)))
    db.flush()
    n = cal_sync.detect_conflicts(db, conn)
    db.commit()
    flash(request, "Busy event added." + (f" {n} existing booking(s) now flagged as conflicting." if n else ""))
    return redirect(f"/app/integrations/connections/{conn_id}/events")


def _mock_event(db: Session, ctx: Ctx, ev_id: str) -> tuple[ExternalCalendarEvent, CalendarConnection]:
    ev = db.get(ExternalCalendarEvent, ev_id)
    if not ev:
        raise HTTPException(404)
    cal = db.get(ConnectedCalendar, ev.calendar_id)
    conn = _conn(db, ctx, cal.connection_id)
    if conn.provider != "mock":
        raise HTTPException(400)
    return ev, conn


@router.post("/app/integrations/events/{ev_id}/delete", include_in_schema=False)
def delete_mock_event(ev_id: str, request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    ev, conn = _mock_event(db, ctx, ev_id)
    was_booking = bool(ev.booking_id)
    db.delete(ev)
    db.flush()
    n = cal_sync.detect_conflicts(db, conn) if was_booking else 0
    db.commit()
    flash(request, "Simulated an external delete of the booking's calendar event - the booking was flagged, not cancelled."
          if was_booking and n else "Event deleted.")
    return redirect(f"/app/integrations/connections/{conn.id}/events")


@router.post("/app/integrations/events/{ev_id}/move", include_in_schema=False)
def move_mock_event(ev_id: str, request: Request, minutes: int = Form(60), db: Session = Depends(get_db),
                    ctx: Ctx = Depends(get_ctx)):
    ev, conn = _mock_event(db, ctx, ev_id)
    ev.start_at += dt.timedelta(minutes=minutes)
    ev.end_at += dt.timedelta(minutes=minutes)
    db.flush()
    n = cal_sync.detect_conflicts(db, conn)
    db.commit()
    flash(request, f"Moved event by {minutes} minutes externally." + (f" {n} booking(s) flagged." if n else ""))
    return redirect(f"/app/integrations/connections/{conn.id}/events")


# ------------------------------------------------------------------ payments accounts
@router.post("/app/integrations/stripe/connect", include_in_schema=False)
def stripe_connect(request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    if not ctx.feature("payments"):
        raise Forbidden("Payments require the Standard plan or higher.")
    if settings.stripe_secret_key:
        from app.services.providers.payments_stripe import StripeProvider

        sp = StripeProvider()
        try:
            acct = db.scalar(select(PaymentAccount).where(PaymentAccount.user_id == ctx.user.id,
                                                          PaymentAccount.provider == "stripe"))
            if not acct or "_mock_" in acct.account_id:
                acct_id = sp.create_connected_account(ctx.user.email, idem=f"acct:{ctx.user.id}")
                acct = PaymentAccount(workspace_id=ctx.workspace.id, user_id=ctx.user.id, provider="stripe",
                                      account_id=acct_id, status="pending")
                db.add(acct)
                db.commit()
            base = settings.base_url.rstrip("/")
            url = sp.onboarding_link(acct.account_id, f"{base}/app/integrations", f"{base}/app/integrations/stripe/return")
            return redirect(url, 302)
        except Exception as exc:  # noqa: BLE001
            flash(request, f"Stripe connection failed: {exc}", "error")
            return redirect("/app/integrations#payments")
    acct = PaymentAccount(workspace_id=ctx.workspace.id, user_id=ctx.user.id, provider="stripe",
                          account_id=new_mock_account_id("acct"), status="connected")
    db.add(acct)
    audit(db, "payments.connected", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, metadata={"provider": "stripe", "mock": True})
    db.commit()
    flash(request, "Stripe (test mode) connected. Paid event types will use a simulated hosted checkout.")
    return redirect("/app/integrations#payments")


@router.get("/app/integrations/stripe/return", include_in_schema=False)
def stripe_return(request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    acct = db.scalar(select(PaymentAccount).where(PaymentAccount.user_id == ctx.user.id, PaymentAccount.provider == "stripe"))
    if not acct:
        return redirect("/app/integrations#payments")
    try:
        from app.services.providers.payments_stripe import StripeProvider

        ready = StripeProvider().account_ready(acct.account_id)
    except Exception:  # noqa: BLE001
        ready = False
    acct.status = "connected" if ready else "pending"
    audit(db, "payments.connected", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, metadata={"provider": "stripe"})
    db.commit()
    flash(request, "Stripe connected." if ready else "Stripe onboarding isn't complete yet - finish it to accept payments.",
          "success" if ready else "warning")
    return redirect("/app/integrations#payments")


@router.post("/app/integrations/paypal/connect", include_in_schema=False)
def paypal_connect(request: Request, paypal_account: str = Form(""), db: Session = Depends(get_db),
                   ctx: Ctx = Depends(get_ctx)):
    if not ctx.feature("payments"):
        raise Forbidden("Payments require the Standard plan or higher.")
    configured = bool(settings.paypal_client_id and settings.paypal_secret)
    account_id = paypal_account.strip() if configured and paypal_account.strip() else new_mock_account_id("paypal")
    db.add(PaymentAccount(workspace_id=ctx.workspace.id, user_id=ctx.user.id, provider="paypal", account_id=account_id,
                          status="connected"))
    audit(db, "payments.connected", workspace_id=ctx.workspace.id, actor_id=ctx.user.id,
          metadata={"provider": "paypal", "mock": not configured})
    db.commit()
    flash(request, "PayPal connected." if configured else "PayPal (test mode) connected.")
    return redirect("/app/integrations#payments")


@router.post("/app/integrations/payments/{acct_id}/disconnect", include_in_schema=False)
def payments_disconnect(acct_id: str, request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    acct = db.get(PaymentAccount, acct_id)
    if not acct or acct.user_id != ctx.user.id:
        raise HTTPException(404)
    audit(db, "payments.disconnected", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, metadata={"provider": acct.provider})
    db.delete(acct)
    db.commit()
    flash(request, "Payment account disconnected. Paid event types can't take new bookings until a provider is connected.")
    return redirect("/app/integrations#payments")


# ------------------------------------------------------------------ webhooks UI
def _webhooks_allowed(ctx: Ctx):
    if not ctx.feature("webhooks"):
        raise Forbidden("Webhooks require the Standard plan or higher. Upgrade in Settings > Billing.")
    if not ctx.can("admin"):
        raise Forbidden("Only workspace admins can manage webhooks.")


@router.get("/app/integrations/webhooks", include_in_schema=False)
def webhooks_page(request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    _webhooks_allowed(ctx)
    subs = db.scalars(select(WebhookSubscription).where(WebhookSubscription.workspace_id == ctx.workspace.id)
                      .order_by(WebhookSubscription.created_at.desc())).all()
    sub_ids = [s.id for s in subs] or ["-"]
    status = request.query_params.get("status") or ""
    q = select(WebhookDelivery).where(WebhookDelivery.subscription_id.in_(sub_ids))
    if status:
        q = q.where(WebhookDelivery.status == status)
    deliveries = db.scalars(q.order_by(WebhookDelivery.created_at.desc()).limit(50)).all()
    return render(request, "app/integrations/webhooks.html", ctx, **_tabs("webhooks"), subs=subs,
                  deliveries=deliveries, sub_map={s.id: s for s in subs}, events=webhooks_service.WEBHOOK_EVENTS,
                  new_secret=request.session.pop("_new_webhook_secret", None), status=status)


@router.post("/app/integrations/webhooks", include_in_schema=False)
async def webhooks_create(request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    _webhooks_allowed(ctx)
    form = await request.form()
    try:
        sub, secret = webhooks_service.create_subscription(db, ctx.workspace.id, form.get("endpoint", ""),
                                                           form.getlist("events"))
    except webhooks_service.InvalidEndpoint as exc:
        flash(request, str(exc), "error")
        return redirect("/app/integrations/webhooks")
    audit(db, "webhook.created", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="webhook",
          entity_id=sub.id, metadata={"endpoint": sub.endpoint, "events": sub.events})
    db.commit()
    request.session["_new_webhook_secret"] = {"id": sub.id, "secret": secret}
    flash(request, "Webhook subscription created. Copy the signing secret now - it won't be shown again.")
    return redirect("/app/integrations/webhooks")


def _sub(db: Session, ctx: Ctx, sub_id: str) -> WebhookSubscription:
    s = db.get(WebhookSubscription, sub_id)
    if not s or s.workspace_id != ctx.workspace.id:
        raise HTTPException(404)
    return s


@router.post("/app/integrations/webhooks/{sub_id}/toggle", include_in_schema=False)
def webhooks_toggle(sub_id: str, request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    _webhooks_allowed(ctx)
    s = _sub(db, ctx, sub_id)
    s.active = not s.active
    audit(db, "webhook.updated", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_id=s.id, metadata={"active": s.active})
    db.commit()
    flash(request, "Subscription " + ("enabled." if s.active else "paused."))
    return redirect("/app/integrations/webhooks")


@router.post("/app/integrations/webhooks/{sub_id}/delete", include_in_schema=False)
def webhooks_delete(sub_id: str, request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    _webhooks_allowed(ctx)
    s = _sub(db, ctx, sub_id)
    db.query(WebhookDelivery).filter(WebhookDelivery.subscription_id == s.id).delete()
    audit(db, "webhook.deleted", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_id=s.id)
    db.delete(s)
    db.commit()
    flash(request, "Webhook subscription deleted.")
    return redirect("/app/integrations/webhooks")


@router.post("/app/integrations/webhooks/{sub_id}/test", include_in_schema=False)
def webhooks_test(sub_id: str, request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    _webhooks_allowed(ctx)
    s = _sub(db, ctx, sub_id)
    d = webhooks_service.send_test_event(db, s)
    db.commit()
    flash(request, f"Test event sent: {d.status}" + (f" (HTTP {d.response_code})" if d.response_code else
                                                     f" - {(d.response_body or '')[:120]}"),
          "success" if d.status == "delivered" else "warning")
    return redirect("/app/integrations/webhooks")


@router.post("/app/integrations/webhooks/deliveries/{delivery_id}/retry", include_in_schema=False)
def webhooks_retry(delivery_id: str, request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    _webhooks_allowed(ctx)
    d = db.get(WebhookDelivery, delivery_id)
    if not d:
        raise HTTPException(404)
    _sub(db, ctx, d.subscription_id)
    webhooks_service.retry_delivery(db, d)
    webhooks_service.deliver(db, d)
    db.commit()
    flash(request, f"Delivery retried: {d.status}", "success" if d.status == "delivered" else "warning")
    return redirect("/app/integrations/webhooks")


# ------------------------------------------------------------------ API keys UI
def _api_allowed(ctx: Ctx):
    if not ctx.feature("api"):
        raise Forbidden("The REST API requires the Standard plan or higher. Upgrade in Settings > Billing.")
    if not ctx.can("admin"):
        raise Forbidden("Only workspace admins can manage API keys.")


@router.get("/app/integrations/api-keys", include_in_schema=False)
def api_keys_page(request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    _api_allowed(ctx)
    keys = db.scalars(select(ApiKey).where(ApiKey.workspace_id == ctx.workspace.id).order_by(ApiKey.created_at.desc())).all()
    owners = {u.id: u for u in db.scalars(select(User).where(User.id.in_([k.user_id for k in keys] or ["-"]))).all()}
    return render(request, "app/integrations/api_keys.html", ctx, **_tabs("api"), keys=keys, owners=owners,
                  new_key=request.session.pop("_new_api_key", None))


@router.post("/app/integrations/api-keys", include_in_schema=False)
async def api_keys_create(request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    _api_allowed(ctx)
    form = await request.form()
    key, raw = apikeys.create_key(db, ctx.workspace.id, ctx.user.id, form.get("name") or "API key",
                                  form.getlist("scopes") or ["read"])
    audit(db, "api_key.created", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="api_key",
          entity_id=key.id, metadata={"name": key.name, "scopes": key.scopes, "prefix": key.prefix})
    db.commit()
    request.session["_new_api_key"] = {"id": key.id, "key": raw}
    flash(request, "API key created. Copy it now - it won't be shown again.")
    return redirect("/app/integrations/api-keys")


@router.post("/app/integrations/api-keys/{key_id}/revoke", include_in_schema=False)
def api_keys_revoke(key_id: str, request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    _api_allowed(ctx)
    key = db.get(ApiKey, key_id)
    if not key or key.workspace_id != ctx.workspace.id:
        raise HTTPException(404)
    apikeys.revoke_key(db, key)
    audit(db, "api_key.revoked", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="api_key", entity_id=key.id)
    db.commit()
    flash(request, f"API key '{key.name}' revoked. Requests using it are now rejected.")
    return redirect("/app/integrations/api-keys")


# ------------------------------------------------------------------ ICS subscription feed
@router.post("/app/integrations/ics-feed/regenerate", include_in_schema=False)
def ics_feed_regenerate(request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    had = ics_feed_service.current(db, ctx.user.id)
    was_active = bool(had and not had.revoked_at)
    raw = ics_feed_service.issue(db, ctx.user.id)
    audit(db, "ics_feed.rotated" if had else "ics_feed.created", workspace_id=ctx.workspace.id, actor_id=ctx.user.id,
          entity_type="user", entity_id=ctx.user.id)
    db.commit()
    request.session["_new_ics_feed"] = raw
    flash(request, "New calendar feed URL created. Copy it now - it won't be shown again."
          + (" The previous URL no longer works." if was_active else ""))
    return redirect("/app/integrations#ics-h")


@router.post("/app/integrations/ics-feed/revoke", include_in_schema=False)
def ics_feed_revoke(request: Request, db: Session = Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    if ics_feed_service.revoke(db, ctx.user.id):
        audit(db, "ics_feed.revoked", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="user",
              entity_id=ctx.user.id)
        db.commit()
        flash(request, "Calendar feed disabled. Apps subscribed to the old URL will stop updating.")
    return redirect("/app/integrations#ics-h")


def _ics_escape(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


@router.get("/api/v1/calendar-feed/{user_id}/{token}.ics", include_in_schema=False)
def ics_feed(user_id: str, token: str, db: Session = Depends(get_db)):
    from app.services.notifications import event_name, location_text, manage_links

    user = ics_feed_service.verify(db, user_id, token)
    if not user:
        raise HTTPException(404, "Feed not found")
    sub = select(BookingHost.booking_id).where(BookingHost.user_id == user_id)
    bookings = db.scalars(select(Booking).where(
        (Booking.host_id == user_id) | Booking.id.in_(sub), Booking.status.in_(("confirmed", "pending")),
        Booking.end_at > utcnow() - dt.timedelta(days=30)).order_by(Booking.start_at).limit(1000)).all()
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//AAL Scheduler//Feed//EN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", f"X-WR-CALNAME:{_ics_escape(settings.app_name + ' - ' + (user.name or user.email))}"]
    now = utcnow()
    for b in bookings:
        inv = ", ".join(i.name for i in b.invitees)
        lines += ["BEGIN:VEVENT", f"UID:{b.id}@aal-scheduler", f"DTSTAMP:{now:%Y%m%dT%H%M%SZ}",
                  f"DTSTART:{b.start_at:%Y%m%dT%H%M%SZ}", f"DTEND:{b.end_at:%Y%m%dT%H%M%SZ}",
                  f"SUMMARY:{_ics_escape(event_name(db, b) + (' with ' + inv if inv else ''))}",
                  f"LOCATION:{_ics_escape(location_text(b))}",
                  f"DESCRIPTION:{_ics_escape('Manage: ' + manage_links(b)['manage_url'])}",
                  f"STATUS:{'CONFIRMED' if b.status == 'confirmed' else 'TENTATIVE'}", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return Response("\r\n".join(lines) + "\r\n", media_type="text/calendar; charset=utf-8",
                    headers={"Cache-Control": "private, max-age=300"})
