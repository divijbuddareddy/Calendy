"""Shared test fixtures. Each test session uses a fresh temp SQLite file DB.

Fixtures:
  db           - SQLAlchemy session
  client       - TestClient (CSRF disabled in test env)
  host         - (user, workspace) with default Mon-Fri 09-17 schedule, tz Europe/London, slug "alice"
  login(client, user) - helper fixture to log a user in via the test-only session endpoint
"""
import os
import tempfile

_tmpdir = tempfile.mkdtemp(prefix="aal_test_")
os.environ["SCHEDULER_ENV"] = "test"
os.environ["ENV"] = "test"
# TEST_DATABASE_URL=postgresql+psycopg2://... runs the whole suite against PostgreSQL (see README).
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{_tmpdir}/test.db"
os.environ["WORKER_ENABLED"] = "false"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, SessionLocal, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.services.bootstrap import create_user_with_workspace  # noqa: E402

init_db()


@app.post("/__test__/login/{user_id}", include_in_schema=False)
def _test_login(user_id: str, request: __import__("fastapi").Request):
    from app.deps import login_session
    from app.models import User

    with SessionLocal() as s:
        u = s.get(User, user_id)
        login_session(request, u)
    return {"ok": True}


@pytest.fixture(autouse=True)
def _clean_db():
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def host(db):
    user, ws = create_user_with_workspace(
        db, "alice@example.com", "Alice Host", password="password123", timezone="Europe/London", slug="alice", verified=True
    )
    user.onboarding_completed = True
    db.commit()
    return user, ws


@pytest.fixture
def login():
    def _login(client, user):
        r = client.post(f"/__test__/login/{user.id}")
        assert r.status_code == 200

    return _login
