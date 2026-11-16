"""Extra env settings for the integrations/payments/observability module (read from the same
.env as app.config, kept separate so config.py stays untouched)."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class ExtSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # PayPal webhook id (Developer dashboard > Webhooks) used by verify-webhook-signature
    paypal_webhook_id: str = ""
    # Optional error tracking
    sentry_dsn: str = ""
    sentry_traces_sample_rate: float = 0.0
    # Worker failure alerts: email platform admins after N consecutive failures of a task
    worker_alert_threshold: int = 3
    # Worker heartbeat considered stale after this many seconds (/healthz/deep)
    worker_heartbeat_stale_seconds: int = 120
    # Outbox backlog older than this many seconds counts as "stuck" for /healthz/deep
    outbox_stuck_seconds: int = 300
    outbox_stuck_threshold: int = 50


@lru_cache
def ext_settings() -> ExtSettings:
    return ExtSettings()
