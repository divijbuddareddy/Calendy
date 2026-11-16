"""Operations helpers for workspace owners/admins.

GET /app/ops/backup.sqlite: consistent online snapshot of the SQLite database (sqlite3 backup API).
Only available in non-production environments with a SQLite DATABASE_URL. Production backups
are handled by the managed Postgres service (see docs/OPERATIONS.md). The full-database dump
contains every tenant's data, so it also requires the caller to own their workspace and is audited.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from app.audit import audit
from app.config import settings
from app.db import SessionLocal, engine
from app.deps import Ctx, Forbidden, get_ctx
from app.models import utcnow

router = APIRouter()


def backup_available() -> bool:
    return settings.env != "prod" and engine.dialect.name == "sqlite" and ":memory:" not in str(engine.url)


def sqlite_backup_to(path: str) -> None:
    src = sqlite3.connect(engine.url.database)
    try:
        dst = sqlite3.connect(path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


@router.get("/app/ops/backup.sqlite", include_in_schema=False)
def download_backup(ctx: Ctx = Depends(get_ctx)):
    if not backup_available():
        raise HTTPException(404, "Database download is only available for SQLite in development.")
    if ctx.role != "owner":
        raise Forbidden("Only a workspace owner can download a database backup.")
    fd, path = tempfile.mkstemp(prefix="aal-backup-", suffix=".sqlite")
    os.close(fd)
    sqlite_backup_to(path)
    with SessionLocal() as db:
        audit(db, "ops.db_backup_downloaded", workspace_id=ctx.workspace.id, actor_id=ctx.user.id,
              entity_type="database", entity_id="sqlite")
        db.commit()
    name = f"scheduler-backup-{utcnow().strftime('%Y%m%d-%H%M%S')}.sqlite"
    return FileResponse(path, media_type="application/vnd.sqlite3", filename=name,
                        background=BackgroundTask(os.remove, path))
