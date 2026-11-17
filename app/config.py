"""Application settings (env-driven). Copy .env.example to .env to override."""
import os
from functools import lru_cache
from typing import Optional

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class BlankAsDefaultSettings(BaseSettings):
    """`KEY=` (blank) in .env means "use the default" for non-string fields, instead of failing to
    parse "" as a bool/int (e.g. `DEMO_MODE=` straight from .env.example)."""

    @model_validator(mode="before")
    @classmethod
    def _blank_as_default(cls, data):
        if isinstance(data, dict):
            fields = cls.model_fields
            data = {k: v for k, v in data.items()
                    if not (v == "" and k.lower() in fields and fields[k.lower()].annotation is not str)}
        return data


class Settings(BlankAsDefaultSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "AAL Scheduler"
    base_url: str = "http://127.0.0.1:8000"
    secret_key: str = "dev-secret-change-me-please-0123456789"
    # Fernet key for encrypting OAuth tokens / webhook secrets (derived from secret_key if empty)
    encryption_key: str = ""
    database_url: str = "sqlite:///./scheduler.db"
    env: str = "dev"  # dev | test | prod

    # Booking integrity
    hold_minutes: int = 5
    slot_token_ttl_seconds: int = 900

    # Worker
    worker_enabled: bool = True
    worker_interval_seconds: float = 5.0

    # Email (SMTP optional; otherwise messages are stored in the dev inbox)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    email_from: str = "AAL Scheduler <no-reply@example.com>"

    # Google OAuth / Calendar (optional - mock provider used when absent)
    google_client_id: str = ""
    google_client_secret: str = ""
    # Microsoft Graph (optional)
    microsoft_client_id: str = ""
    microsoft_client_secret: str = ""
    # Zoom (optional)
    zoom_client_id: str = ""
    zoom_client_secret: str = ""
    # Stripe (optional - mock checkout used when absent)
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    # PayPal (optional)
    paypal_client_id: str = ""
    paypal_secret: str = ""
    # SMS (optional)
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from: str = ""
    # Optional LLM for the AI assistant (falls back to deterministic parser)
    anthropic_api_key: str = ""

    # Embeds
    embed_allowed_origins: str = "*"  # comma separated allowlist for frame-ancestors


    # Sessions (PRD 17.1): idle timeout and absolute lifetime, in minutes
    session_idle_minutes: int = 60 * 24 * 3
    session_max_age_minutes: int = 60 * 24 * 14

    # Legal / release gate (PRD 24.3): bump when the published text changes materially
    privacy_version: str = "2026-11-16"
    terms_version: str = "2026-11-16"
    retention_version: str = "2026-11-16"
    company_name: str = "AI Automation Labs"
    privacy_contact_email: str = "privacy@example.com"

    # Observability (optional)
    sentry_dsn: str = ""

    # Demo mode: seeds the demo workspace (demo@example.com / demo1234) at startup if missing and
    # shows the one-click "Try the demo account" button. Defaults to on outside ENV=prod;
    # set DEMO_MODE=true to enable it on a public demo deployment.
    demo_mode: Optional[bool] = None

    @property
    def demo_enabled(self) -> bool:
        return self.demo_mode if self.demo_mode is not None else self.env != "prod"


def normalize_db_url(url: str) -> str:
    """Render/Heroku hand out `postgres://`; SQLAlchemy 2 needs `postgresql+psycopg2://`."""
    if url.startswith("postgres://"):
        return "postgresql+psycopg2://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
settings.database_url = normalize_db_url(settings.database_url)
# On Render, fall back to the service's public URL when BASE_URL wasn't set explicitly.
if os.environ.get("RENDER_EXTERNAL_URL") and not os.environ.get("BASE_URL"):
    settings.base_url = os.environ["RENDER_EXTERNAL_URL"].rstrip("/")
IS_TEST = os.environ.get("SCHEDULER_ENV") == "test" or settings.env == "test"
