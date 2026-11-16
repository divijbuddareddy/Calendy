"""Production-safe bootstrap. Safe to run on every deploy (idempotent):

  * creates/updates the schema (create_all + additive column sync) and Postgres constraints
  * creates the first owner account + workspace ONLY if that email doesn't exist yet
    (an existing account is never modified; its password is never overwritten)
  * never creates demo data

Usage:
    SEED_OWNER_EMAIL=you@company.com SEED_OWNER_PASSWORD='...' [SEED_OWNER_NAME='You'] \
    [SEED_WORKSPACE_NAME='Acme'] [SEED_TIMEZONE='Europe/London'] python seed_prod.py
    python seed_prod.py --email you@company.com --password '...' --name You --workspace Acme

Without an email it only migrates the schema (handy as a Render pre-deploy command).
"""
from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import func, select


def run(email: str | None, name: str | None = None, password: str | None = None, workspace_name: str | None = None,
        timezone: str = "UTC") -> dict:
    from app.db import SessionLocal, engine, init_db
    from app.models import User
    from app.services.bootstrap import create_user_with_workspace

    init_db()
    try:
        from app.services.booking import ensure_postgres_constraints

        ensure_postgres_constraints(engine)
    except ImportError:
        pass
    except Exception as exc:  # e.g. btree_gist unavailable: the partial unique index + host locks still protect
        print(f"WARNING: could not install the Postgres overlap constraint: {exc.__class__.__name__}: {exc}",
              file=sys.stderr)
    if not email:
        return {"created": False, "schema": "ok"}
    email = email.strip().lower()
    with SessionLocal() as db:
        existing = db.scalar(select(User).where(func.lower(User.email) == email))
        if existing:
            return {"created": False, "user_id": existing.id}
        if not password or len(password) < 8:
            raise SystemExit("SEED_OWNER_PASSWORD must be at least 8 characters to create the owner account.")
        user, ws = create_user_with_workspace(db, email, name or email.split("@")[0], password=password,
                                              timezone=timezone, verified=True, workspace_name=workspace_name)
        user.onboarding_completed = False  # walk the owner through onboarding on first login
        db.commit()
        return {"created": True, "user_id": user.id, "workspace_id": ws.id}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--email", default=os.environ.get("SEED_OWNER_EMAIL"))
    ap.add_argument("--password", default=os.environ.get("SEED_OWNER_PASSWORD"))
    ap.add_argument("--name", default=os.environ.get("SEED_OWNER_NAME"))
    ap.add_argument("--workspace", default=os.environ.get("SEED_WORKSPACE_NAME"))
    ap.add_argument("--timezone", default=os.environ.get("SEED_TIMEZONE", "UTC"))
    a = ap.parse_args(argv)
    out = run(a.email, a.name, a.password, a.workspace, a.timezone)
    if out.get("created"):
        print(f"Created owner {a.email} (workspace {out['workspace_id']}).")
    elif a.email:
        print(f"{a.email} already exists; nothing changed.")
    else:
        print("Schema is up to date. (Set SEED_OWNER_EMAIL to create the first owner.)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
