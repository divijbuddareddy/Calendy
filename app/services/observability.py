"""Observability (PRD 23: request IDs + structured logs exist in middleware; this adds Sentry,
worker failure alerts and a deep health check).

    init_observability(app)   - call once from main.create_app() after routers are included.
        * Sentry: initialised when SENTRY_DSN is set AND sentry_sdk is importable; otherwise a no-op.
        * Instruments every worker task (app.worker.TASKS) to record success/failure streaks in
          `worker_task_health`; after WORKER_ALERT_THRESHOLD consecutive failures the platform
          admins are emailed once per failure streak.
        * Registers GET /healthz/deep (DB, outbox backlog, dead letters, worker heartbeat);
          200 when healthy, 503 otherwise.

The worker heartbeat is written by the `heartbeat` periodic task registered below.
"""
from __future__ import annotations

import datetime as dt
import functools
import logging
from typing import Optional

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import IS_TEST, settings
from app.services.ext_settings import ext_settings
from app import worker as _worker
from app.worker import periodic

log = logging.getLogger("observability")
HEARTBEAT = "__heartbeat__"
_state = {"sentry": False, "instrumented": False}


# ------------------------------------------------------------------ sentry
def init_sentry() -> bool:
    dsn = ext_settings().sentry_dsn
    if not dsn:
        return False
    try:
        import sentry_sdk  # type: ignore
    except ImportError:
        log.info("SENTRY_DSN set but sentry_sdk is not installed; error tracking disabled")
        return False
    try:
        sentry_sdk.init(dsn=dsn, environment=settings.env, traces_sample_rate=ext_settings().sentry_traces_sample_rate,
                        send_default_pii=False)
    except Exception as exc:  # noqa: BLE001 - never block startup on telemetry
        log.warning("Sentry init failed: %s", exc)
        return False
    _state["sentry"] = True
    return True


def capture_exception(exc: BaseException) -> None:
    if not _state["sentry"]:
        return
    try:
        import sentry_sdk  # type: ignore

        sentry_sdk.capture_exception(exc)
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ worker health
def _health_row(db: Session, name: str):
    from app.models_commerce import WorkerTaskHealth

    row = db.get(WorkerTaskHealth, name)
    if not row:
        row = WorkerTaskHealth(name=name, consecutive_failures=0)
        db.add(row)
    return row


def _alert_admins(db: Session, name: str, row) -> None:
    from app.models import User
    from app.services.messaging import send_email
    from app.services.notifications import render_email

    admins = db.scalars(select(User).where(User.is_platform_admin.is_(True), User.deleted_at.is_(None))).all()
    title = f"Background task '{name}' is failing"
    msg = (f"The periodic task '{name}' has failed {row.consecutive_failures} times in a row "
           f"(environment: {settings.env}).\n\nLast error: {(row.last_error or '')[:1500]}")
    for a in admins:
        try:
            send_email(db, a.email, f"[{settings.app_name}] {title}",
                       render_email("alert.html", title=title, message=msg, link="/app/admin/health",
                                    link_label="Open system health", recipient_name=a.name))
        except Exception as exc:  # noqa: BLE001
            log.error("could not send worker alert to %s: %s", a.email, exc)
    log.error("worker task %s failed %s times in a row: %s", name, row.consecutive_failures, row.last_error)


def record_task_failure(db: Session, name: str, exc: BaseException, now: Optional[dt.datetime] = None) -> None:
    from app.models import utcnow

    now = now or utcnow()
    row = _health_row(db, name)
    row.consecutive_failures = (row.consecutive_failures or 0) + 1
    row.last_failure_at, row.last_error = now, f"{type(exc).__name__}: {exc}"[:4000]
    threshold = max(1, ext_settings().worker_alert_threshold)
    if row.consecutive_failures >= threshold and not row.alerted_at:
        row.alerted_at = now
        _alert_admins(db, name, row)


def record_task_success(db: Session, name: str, now: Optional[dt.datetime] = None) -> None:
    from app.models import utcnow

    now = now or utcnow()
    row = _health_row(db, name)
    # throttle writes: only when recovering or once a minute
    if row.consecutive_failures or row.alerted_at or not row.last_success_at or \
            (now - row.last_success_at).total_seconds() > 60:
        row.consecutive_failures, row.alerted_at, row.last_success_at = 0, None, now


def _wrap(name: str, fn):
    if getattr(fn, "_observed", False):
        return fn

    @functools.wraps(fn)
    def observed(db, *a, **kw):
        try:
            res = fn(db, *a, **kw)
        except Exception as exc:
            capture_exception(exc)
            try:
                db.rollback()
                record_task_failure(db, name, exc)
                db.commit()
            except Exception:  # noqa: BLE001 - never mask the original failure
                log.exception("could not record failure of task %s", name)
                db.rollback()
            raise
        try:
            record_task_success(db, name)
        except Exception:  # noqa: BLE001
            pass
        return res

    observed._observed = True  # type: ignore[attr-defined]
    return observed


def instrument_worker() -> int:
    """Wrap every registered periodic task (idempotent). Returns number of tasks instrumented."""
    tasks = _worker.TASKS
    for i, (name, fn) in enumerate(list(tasks)):
        tasks[i] = (name, _wrap(name, fn))
    _state["instrumented"] = True
    return len(tasks)


@periodic("heartbeat")
def heartbeat_task(db: Session) -> None:
    from app.models import utcnow

    row = _health_row(db, HEARTBEAT)
    now = utcnow()
    if not row.last_success_at or (now - row.last_success_at).total_seconds() >= 15:
        row.last_success_at = now
    # tasks registered after init (lazy imports) get instrumented too
    if _state["instrumented"] and any(not getattr(fn, "_observed", False) for _, fn in _worker.TASKS):
        instrument_worker()


# ------------------------------------------------------------------ deep health
def deep_health(db: Session) -> tuple[bool, dict]:
    from app.models import OutboxEvent, ProviderWebhookInbox, WebhookDelivery, utcnow
    from app.models_commerce import WorkerTaskHealth

    es = ext_settings()
    now = utcnow()
    checks: dict = {}
    ok = True
    try:
        db.execute(text("SELECT 1"))
        checks["database"] = {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return False, {"database": {"ok": False, "error": type(exc).__name__}}
    stuck_before = now - dt.timedelta(seconds=es.outbox_stuck_seconds)
    pending = db.scalar(select(func.count(OutboxEvent.id)).where(OutboxEvent.processed_at.is_(None))) or 0
    stuck = db.scalar(select(func.count(OutboxEvent.id)).where(
        OutboxEvent.processed_at.is_(None), OutboxEvent.created_at < stuck_before)) or 0
    oldest = db.scalar(select(func.min(OutboxEvent.created_at)).where(OutboxEvent.processed_at.is_(None)))
    outbox_ok = stuck < es.outbox_stuck_threshold
    checks["outbox"] = {"ok": outbox_ok, "pending": pending, "stuck": stuck,
                        "oldest_age_seconds": int((now - oldest).total_seconds()) if oldest else 0}
    ok &= outbox_ok
    dead_wh = db.scalar(select(func.count(WebhookDelivery.id)).where(WebhookDelivery.status == "dead")) or 0
    dead_in = db.scalar(select(func.count(ProviderWebhookInbox.id)).where(ProviderWebhookInbox.status == "dead")) or 0
    checks["dead_letters"] = {"ok": True, "webhook_deliveries": dead_wh, "provider_inbox": dead_in}
    hb = db.get(WorkerTaskHealth, HEARTBEAT)
    worker_expected = settings.worker_enabled and not IS_TEST
    if hb and hb.last_success_at:
        age = int((now - hb.last_success_at).total_seconds())
        hb_ok = age <= es.worker_heartbeat_stale_seconds
        checks["worker"] = {"ok": hb_ok, "heartbeat_age_seconds": age}
    else:
        hb_ok = not worker_expected
        checks["worker"] = {"ok": hb_ok, "heartbeat_age_seconds": None,
                            "note": "no heartbeat yet" if worker_expected else "worker disabled"}
    ok &= hb_ok
    failing = db.scalars(select(WorkerTaskHealth).where(WorkerTaskHealth.consecutive_failures > 0,
                                                        WorkerTaskHealth.name != HEARTBEAT)).all()
    threshold = max(1, es.worker_alert_threshold)
    tasks_ok = all(t.consecutive_failures < threshold for t in failing)
    checks["worker_tasks"] = {"ok": tasks_ok, "failing": {t.name: {"consecutive_failures": t.consecutive_failures,
                                                                   "last_error": (t.last_error or "")[:300]}
                                                          for t in failing}}
    ok &= tasks_ok
    checks["sentry"] = {"enabled": _state["sentry"]}
    return bool(ok), checks


def _deep_route(app) -> None:
    from fastapi.responses import JSONResponse
    from starlette.routing import Route

    from app.db import SessionLocal

    def healthz_deep(request):
        db = SessionLocal()
        try:
            ok, checks = deep_health(db)
        finally:
            db.close()
        return JSONResponse({"ok": ok, "checks": checks}, status_code=200 if ok else 503,
                            headers={"Cache-Control": "no-store"})

    if any(getattr(r, "path", None) == "/healthz/deep" for r in app.router.routes):
        return
    # insert first so the public /{username}/{event} catch-all can never shadow it
    app.router.routes.insert(0, Route("/healthz/deep", healthz_deep, methods=["GET"], include_in_schema=False))


def init_observability(app) -> dict:
    """Idempotent. Returns {"sentry": bool, "tasks_instrumented": int}."""
    sentry = _state["sentry"] or init_sentry()
    n = instrument_worker()
    _deep_route(app)
    return {"sentry": sentry, "tasks_instrumented": n}
