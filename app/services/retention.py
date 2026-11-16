"""Platform data-retention jobs backing the published policy at /data-retention.

* Deleted accounts: personal fields are anonymised ACCOUNT_PURGE_DAYS after the user deletes
  their account (the soft-delete window allows support to undo mistakes and lets the audit
  trail stay consistent; the row id is kept so audit/booking references don't dangle).
* Data-export payloads (they contain personal data) are dropped EXPORT_TTL_DAYS after creation.
* Invitee PII on old bookings is handled by `analytics.purge_expired_pii` (workspace-configurable),
  notetaker recaps by `notetaker` (`recap_retention`).

Runs at most once an hour from the in-process worker; all steps are idempotent.
"""
from __future__ import annotations

import datetime as dt
import time
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import AuditLog, DataRequest, SentMessage, User, WebhookDelivery, utcnow
from app.worker import periodic

ACCOUNT_PURGE_DAYS = 30
EXPORT_TTL_DAYS = 7
AUDIT_LOG_DAYS = 730
DELIVERY_LOG_DAYS = 90
_RUN_EVERY_SECONDS = 3600
_last_run = 0.0


def purge_deleted_accounts(db: Session, now: Optional[dt.datetime] = None) -> int:
    now = now or utcnow()
    cutoff = now - dt.timedelta(days=ACCOUNT_PURGE_DAYS)
    n = 0
    for u in db.scalars(select(User).where(User.deleted_at.is_not(None), User.deleted_at < cutoff)).all():
        if u.email.endswith("@deleted.invalid"):
            continue
        u.email = f"{u.id}@deleted.invalid"
        u.name = "Deleted user"
        u.password_hash = None
        u.google_sub = None
        u.avatar_url = None
        u.headline = None
        u.slug = None
        u.working_hours_json = None
        u.branding_json = None
        u.notification_prefs_json = None
        u.session_epoch = int(u.session_epoch or 0) + 1
        n += 1
    return n


def expire_exports(db: Session, now: Optional[dt.datetime] = None) -> int:
    now = now or utcnow()
    cutoff = now - dt.timedelta(days=EXPORT_TTL_DAYS)
    n = 0
    for dr in db.scalars(select(DataRequest).where(DataRequest.kind == "export", DataRequest.created_at < cutoff,
                                                   DataRequest.status == "completed")).all():
        dr.result_json = None
        dr.status = "expired"
        n += 1
    return n


def prune_logs(db: Session, now: Optional[dt.datetime] = None) -> dict:
    now = now or utcnow()
    audit_cut = now - dt.timedelta(days=AUDIT_LOG_DAYS)
    log_cut = now - dt.timedelta(days=DELIVERY_LOG_DAYS)
    a = db.execute(delete(AuditLog).where(AuditLog.created_at < audit_cut)).rowcount or 0
    m = db.execute(delete(SentMessage).where(SentMessage.created_at < log_cut)).rowcount or 0
    w = db.execute(delete(WebhookDelivery).where(WebhookDelivery.created_at < log_cut,
                                                 WebhookDelivery.status.in_(("delivered", "dead")))).rowcount or 0
    return {"audit_deleted": a, "messages_deleted": m, "deliveries_deleted": w}


def run_retention(db: Session, now: Optional[dt.datetime] = None) -> dict:
    out = {"accounts_purged": purge_deleted_accounts(db, now), "exports_expired": expire_exports(db, now)}
    out.update(prune_logs(db, now))
    return out


@periodic("platform_retention")
def _retention_task(db: Session):
    global _last_run
    if time.monotonic() - _last_run < _RUN_EVERY_SECONDS and _last_run:
        return
    _last_run = time.monotonic()
    run_retention(db)
