"""In-process background worker (stand-in for Redis/BullMQ in the PRD).

Modules register periodic tasks with `@periodic`. Each task gets its own DB session and must
be idempotent. `run_all_once()` runs everything synchronously (used by tests and /dev tools).
"""
import asyncio
import logging
from typing import Callable

from app.config import settings
from app.db import WorkerSessionLocal as SessionLocal

log = logging.getLogger("worker")
TASKS: list[tuple[str, Callable]] = []


def periodic(name: str):
    def deco(fn):
        if name not in [n for n, _ in TASKS]:
            TASKS.append((name, fn))
        return fn

    return deco


@periodic("outbox")
def _outbox_task(db):
    from app.events import process_outbox

    process_outbox(db)


def run_all_once(rounds: int = 2) -> None:
    """Run every task (twice by default so events produced by one task are consumed)."""
    for _ in range(rounds):
        for name, fn in TASKS:
            db = SessionLocal()
            try:
                fn(db)
                db.commit()
            except Exception:
                db.rollback()
                log.exception("worker task %s failed", name)
            finally:
                db.close()


async def worker_loop(stop: asyncio.Event):
    while not stop.is_set():
        try:
            await asyncio.to_thread(run_all_once, 1)
        except Exception:
            log.exception("worker loop error")
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.worker_interval_seconds)
        except asyncio.TimeoutError:
            pass
