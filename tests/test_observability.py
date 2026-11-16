"""PRD 23 observability: Sentry no-op, worker failure alerts, /healthz/deep."""
import datetime as dt

from sqlalchemy import select

from app import worker
from app.main import app
from app.models import OutboxEvent, SentMessage, User, utcnow
from app.models_commerce import WorkerTaskHealth
from app.services import observability as O


def test_init_is_idempotent_and_sentry_noop_without_dsn(monkeypatch):
    from app.services.ext_settings import ext_settings

    monkeypatch.setattr(ext_settings(), "sentry_dsn", "")
    res = O.init_observability(app)
    assert res["sentry"] is False and res["tasks_instrumented"] == len(worker.TASKS)
    O.init_observability(app)
    assert sum(1 for r in app.router.routes if getattr(r, "path", None) == "/healthz/deep") == 1
    assert all(getattr(fn, "_observed", False) for _, fn in worker.TASKS)
    # DSN set but sentry_sdk missing -> still a no-op, never raises
    monkeypatch.setattr(ext_settings(), "sentry_dsn", "https://key@o0.ingest.sentry.io/1")
    import builtins

    real_import = builtins.__import__

    def no_sentry(name, *a, **k):
        if name == "sentry_sdk":
            raise ImportError("not installed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_sentry)
    assert O.init_sentry() is False


def test_repeated_task_failure_alerts_platform_admins_once(db, host, monkeypatch):
    user, ws = host
    admin = db.get(User, user.id)
    admin.is_platform_admin = True
    db.commit()
    state = {"fail": True}

    def flaky(db):
        if state["fail"]:
            raise RuntimeError("provider exploded")

    monkeypatch.setattr(worker, "TASKS", [("flaky", flaky)])
    O.instrument_worker()
    for _ in range(5):
        worker.run_all_once(rounds=1)
    db.commit(); db.expire_all()
    row = db.get(WorkerTaskHealth, "flaky")
    assert row.consecutive_failures == 5 and "provider exploded" in row.last_error and row.alerted_at
    alerts = db.scalars(select(SentMessage).where(SentMessage.recipient == admin.email,
                                                  SentMessage.subject.contains("flaky"))).all()
    assert len(alerts) == 1  # one alert per failure streak
    state["fail"] = False
    worker.run_all_once(rounds=1)
    db.commit(); db.expire_all()
    row = db.get(WorkerTaskHealth, "flaky")
    assert row.consecutive_failures == 0 and row.alerted_at is None and row.last_success_at


def test_healthz_deep(db, client, monkeypatch):
    O.init_observability(app)
    r = client.get("/healthz/deep")
    assert r.status_code == 200 and r.json()["checks"]["database"]["ok"]
    assert r.json()["checks"]["worker"]["ok"]  # worker disabled in tests
    # stuck outbox backlog -> 503
    from app.services.ext_settings import ext_settings

    monkeypatch.setattr(ext_settings(), "outbox_stuck_threshold", 1)
    db.add(OutboxEvent(topic="booking.created", payload_json={}, created_at=utcnow() - dt.timedelta(hours=1)))
    db.commit()
    r = client.get("/healthz/deep")
    assert r.status_code == 503 and r.json()["checks"]["outbox"]["stuck"] == 1
    db.query(OutboxEvent).delete()
    db.commit()
    # stale heartbeat when the worker is expected -> 503
    monkeypatch.setattr(O, "IS_TEST", False)
    monkeypatch.setattr(O.settings, "worker_enabled", True)
    r = client.get("/healthz/deep")
    assert r.status_code == 503 and r.json()["checks"]["worker"]["note"] == "no heartbeat yet"
    O.heartbeat_task(db)
    db.commit()
    assert client.get("/healthz/deep").status_code == 200
    hb = db.get(WorkerTaskHealth, O.HEARTBEAT)
    hb.last_success_at = utcnow() - dt.timedelta(hours=1)
    db.commit()
    assert client.get("/healthz/deep").status_code == 503
