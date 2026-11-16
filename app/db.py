"""Database engine/session. SQLite by default (dev), PostgreSQL-ready via DATABASE_URL.

Booking integrity
-----------------
Use `lock_hosts(db, host_ids)` as the FIRST statement of a write transaction that reserves
time for those hosts. It upserts/updates a row per host in `host_locks`:
  * SQLite (WAL): the first write takes the database write lock, so concurrent bookers are
    serialised and the transaction's read snapshot starts after the lock is acquired.
  * PostgreSQL: the UPDATE takes a row lock per host (sorted order => no deadlocks).
Then re-check conflicts and insert the booking/hold, then commit. A partial unique index on
bookings(host_id, start_at) for exclusive active bookings is a second DB-level guard; on
Postgres add the recommended tstzrange exclusion constraint (see README).
"""
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import normalize_db_url, settings


class Base(DeclarativeBase):
    pass


def _make_engine(url: str):
    url = normalize_db_url(url)
    kwargs = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    else:
        # Managed Postgres (Render etc.) drops idle connections; validate before use.
        kwargs.update(pool_pre_ping=True, pool_size=5, max_overflow=10, pool_recycle=1800)
    eng = create_engine(url, future=True, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(eng, "connect")
        def _sqlite_pragmas(dbapi_conn, _):
            # Let SQLAlchemy emit BEGIN itself (pysqlite recipe) so transactions are real.
            dbapi_conn.isolation_level = None
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            if ":memory:" not in url:
                cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

        @event.listens_for(eng, "begin")
        def _do_begin(conn):
            # Background-worker sessions use BEGIN IMMEDIATE: they read-then-write, and a deferred
            # read txn that later upgrades to write fails instantly with "database is locked"
            # under concurrency instead of waiting on busy_timeout.
            if conn.get_execution_options().get("sqlite_immediate"):
                conn.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                conn.exec_driver_sql("BEGIN")

    return eng


engine = _make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
# Write-oriented sessions (background worker): lock at BEGIN on SQLite; no-op elsewhere.
WorkerSessionLocal = sessionmaker(
    bind=engine.execution_options(sqlite_immediate=True), autoflush=False, expire_on_commit=False, future=True
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope():
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def lock_hosts(db: Session, host_ids) -> None:
    """Acquire write locks for the given host ids. Call as the first statement of a transaction
    (call db.commit() beforehand if the session may have an open read transaction)."""
    pg = db.get_bind().dialect.name == "postgresql"
    for hid in sorted(set(host_ids)):
        if pg:
            # Make sure the row exists without racing a concurrent first-time insert, then row-lock it.
            db.execute(text("INSERT INTO host_locks (host_id, counter) VALUES (:h, 0) ON CONFLICT (host_id) DO NOTHING"),
                       {"h": hid})
        res = db.execute(
            text("UPDATE host_locks SET counter = counter + 1 WHERE host_id = :h"), {"h": hid}
        )
        if res.rowcount == 0:
            db.execute(text("INSERT INTO host_locks (host_id, counter) VALUES (:h, 1)"), {"h": hid})


def _add_missing_columns(bind) -> list[str]:
    """Tiny additive "migration": ALTER TABLE ... ADD COLUMN for model columns missing from existing
    tables (create_all only creates missing tables). Only nullable/defaulted additive changes are
    supported; anything destructive needs a real migration."""
    from sqlalchemy import inspect

    added = []
    insp = inspect(bind)
    existing_tables = set(insp.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have or col.primary_key:
                continue
            ctype = col.type.compile(dialect=bind.dialect)
            ddl = f'ALTER TABLE {table.name} ADD COLUMN {col.name} {ctype}'
            with bind.begin() as conn:
                conn.exec_driver_sql(ddl)
            added.append(f"{table.name}.{col.name}")
    return added


def init_db():
    from app import models  # noqa: F401  (register models)

    Base.metadata.create_all(bind=engine)
    try:
        _add_missing_columns(engine)
    except Exception:  # pragma: no cover - never block startup on the convenience migrator
        import logging

        logging.getLogger("app.db").exception("additive column sync failed")
