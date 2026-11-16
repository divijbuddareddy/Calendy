# Operations runbook: AAL Scheduler on Render

This covers PRD §17.1 ("backups and tested restore"), §23 (uptime 99.5%, daily backups + PITR, observability) and the §24.3
release gate ("monitoring and backups configured").

## 1. Topology

| Piece | Render resource | Notes |
|---|---|---|
| Web app + background worker | `aal-scheduler` web service (`render.yaml`) | Uvicorn serves FastAPI. The worker (reminders, webhooks, calendar sync, retention) runs in the same process every 5 s. |
| Database | `aal-scheduler-db` managed PostgreSQL 16 | `DATABASE_URL` comes from the database (`postgres://` is normalised to `postgresql+psycopg2://`). |
| Health | `GET /healthz` (process + `SELECT 1`) and `GET /healthz/deep` (worker/outbox/webhook backlog) | Render health check uses `/healthz`. |

**Scaling out.** Every instance runs the worker. The worker's jobs are idempotent and claim rows transactionally, but to keep
email volume predictable run **one** instance. If you need more web capacity, add instances with `WORKER_ENABLED=false`
and keep exactly one instance with `WORKER_ENABLED=true`, for example as a separate Render background worker running
`python -c "import asyncio, app.main; from app.worker import worker_loop; asyncio.run(worker_loop(asyncio.Event()))"`.

## 2. First deploy

1. Push the repo to GitHub or GitLab. In Render choose **New → Blueprint** and pick the repo. Render creates the web
   service and the database from `render.yaml`.
2. When prompted, fill the `sync: false` variables:
   - `BASE_URL`: `https://<service>.onrender.com` or your custom domain. If you leave it empty, `RENDER_EXTERNAL_URL` is used.
   - `SEED_OWNER_EMAIL` / `SEED_OWNER_PASSWORD`: creates the first owner account on the first deploy.
   - `PRIVACY_CONTACT_EMAIL`: shown on `/privacy`, `/terms` and `/data-retention`.
   - Provider keys (Google, Microsoft, Stripe, SMTP and so on) as needed.
3. `preDeployCommand: python seed_prod.py` runs on every deploy. It is idempotent: it syncs the schema (create tables plus
   additive columns), installs the Postgres overlap exclusion constraint (`btree_gist`) and creates the owner only if
   that email doesn't exist yet. It never creates demo data and never changes an existing account. On plans without
   pre-deploy commands, the app runs the same schema sync at startup; run `python seed_prod.py` once from the Render
   shell to create the owner.
4. Check: `curl https://<host>/healthz` returns `{"ok": true, "db": "ok", "env": "prod"}`. Then sign in, connect a
   calendar, publish an event type and book it from a private window.
5. Remove `SEED_OWNER_PASSWORD` from the environment after the first login, and change the password in the app.

`ENV=prod` refuses to boot with the default `SECRET_KEY` (render.yaml generates one) and hides the demo login button.
Session cookies are `Secure` whenever `BASE_URL` is `https://`.

**Docker or another host.** `docker build -t aal-scheduler .` then
`docker run -p 8000:8000 -e ENV=prod -e SECRET_KEY=... -e DATABASE_URL=postgres://... -e BASE_URL=https://... aal-scheduler`.
The image runs `seed_prod.py` then Uvicorn with `--proxy-headers`.

## 3. Backups

**Production (Render PostgreSQL)**

- Use a **paid** Postgres plan (render.yaml uses `basic-256mb`). Paid instances get automatic **daily logical backups**
  and **point-in-time recovery (PITR)**. The PITR window depends on the workspace plan (a few days on Hobby/Professional,
  longer on higher tiers). Free Postgres has no backups and expires, so never use it in production.
- PITR lets you restore to any second inside the window as a **new** database instance. The original stays untouched until
  you switch over.
- **Off-platform copy (recommended).** Each week, and before risky migrations, take a logical dump to storage you control:

  ```bash
  # From any machine with PostgreSQL 16 client tools; take the External Database URL from the Render dashboard
  pg_dump "$EXTERNAL_DATABASE_URL" -Fc --no-owner --no-acl -f aal-$(date +%F).dump
  # upload to S3/GCS/Backblaze with object lock or versioning, keep 30 days
  ```

  The retention policy (`/data-retention`) promises that backups expire. Keep off-platform dumps 30 days at most, or
  update the policy.

**Development (SQLite)**

- Workspace owners can download a consistent snapshot at **Settings → Data & privacy → Download database backup**
  (`/app/ops/backup.sqlite`). It uses the SQLite online-backup API, so it is safe while the app runs. Only available when
  `ENV` isn't `prod` and the database is SQLite. Every download is audited.
- Restore: stop the app, replace `scheduler.db` with the downloaded file, delete `scheduler.db-wal` and `scheduler.db-shm`,
  then start the app again.

## 4. Restore procedure (tested)

### 4a. Point-in-time recovery on Render (data loss or corruption)

1. **Freeze writes.** Suspend the web service (Dashboard → service → *Suspend*) so no new bookings land on the bad database.
2. Dashboard → `aal-scheduler-db` → **Recovery** → choose the timestamp just before the incident → *Restore*. Render creates
   a new instance, for example `aal-scheduler-db-restored`.
3. Check the restored instance using its External URL:
   ```bash
   psql "$RESTORED_URL" -c "select count(*) from bookings; select max(created_at) from bookings; select count(*) from users;"
   ```
4. Point the app at it: change the web service's `DATABASE_URL` to the restored instance's **Internal** URL, or update
   `fromDatabase.name` in `render.yaml`.
5. Resume the service and run the smoke test in §2 step 4. Make sure `/healthz/deep` shows no outbox or webhook backlog.
6. Reconcile anything that happened between the restore point and the freeze:
   - **Payments:** Stripe/PayPal webhooks are retried by the provider, and the `payment_reconcile` worker job
     re-checks pending payments.
   - **Calendars:** the `calendar_sync` job re-reads busy times. Bookings created after the restore point exist only in
     hosts' calendars and invitees' emails, so tell affected hosts.
7. Delete the old instance once you're sure (keep it at least 7 days).

### 4b. Restore from a logical dump (`pg_dump -Fc`)

```bash
createdb -h <host> -U <user> scheduler_restore            # or create a new Render Postgres instance
pg_restore --no-owner --no-acl -h <host> -U <user> -d scheduler_restore aal-YYYY-MM-DD.dump
DATABASE_URL=postgres://<user>:<pw>@<host>/scheduler_restore python seed_prod.py   # idempotent schema sync + constraints
```

Then switch `DATABASE_URL` as in 4a step 4.

### Restore drill record

| Date | Environment | Steps | Result |
|---|---|---|---|
| 2026-11-16 | Local PostgreSQL 16.2 | `seed.py` into `aal_drill` (with a `postgres://` URL) → `pg_dump -Fc` → `pg_restore` into empty `aal_restore` → started the app on the restored DB, `/healthz` | 70 tables, 128 rows: identical per-table row counts; 230/230 indexes; app booted and `/healthz` returned `db: ok` |

Run this drill **every quarter** and after major schema changes. Use a PITR restore on Render to a scratch instance and
add a row to this table. A backup you haven't restored isn't a backup.

## 5. Monitoring and alerting

| Signal | Where | Alert |
|---|---|---|
| Uptime | External monitor (UptimeRobot, Better Stack or Pingdom) on `https://<host>/healthz` every 1 min, from 2+ regions | Page after 2 consecutive failures. 99.5% monthly target ≈ 3.6 h downtime budget |
| Deep health | Same monitor on `/healthz/deep` every 5 min | Notify when it returns 503 (stuck worker, outbox backlog, webhook dead letters) |
| Errors | Sentry: set `SENTRY_DSN` and add `sentry-sdk` to requirements | New issue alert; regression alert |
| Worker failures | Built in: after N consecutive failures of a job, workspace admins get an email and the job shows in **Admin → System health** | Investigate within 1 business day |
| Logs | Render log stream (JSON access logs with `request_id`). Add a log drain (Better Stack, Datadog) for 30-day retention | Search by `request_id` from the error page/API error body |
| Database | Render metrics: CPU, memory, connections, disk | Alert at 80% disk or 80% connections |
| Email deliverability | SMTP provider dashboard (bounces/complaints). SPF, DKIM and DMARC set on the sending domain | Bounce rate > 5% |

## 6. Runbooks

**Site down / health check failing**
1. Render dashboard → service → *Events* and *Logs*. A deploy failure auto-rolls back. If it didn't, use *Rollback* to the
   previous deploy.
2. If `/healthz` reports `db: unreachable`, check the database status page and its connection count. A restart of the
   web service clears leaked connections (the pool uses `pool_pre_ping`).
3. Post a status update if the outage lasts more than 15 minutes.

**Bookings not confirming / emails not sent**
1. **Admin → System health:** look for failing jobs and dead-letter webhooks, then retry from the UI.
2. `/healthz/deep` shows backlog sizes. If the worker is stuck, restart the service (jobs are idempotent).
3. Check the SMTP credentials and the provider dashboard.

**Calendar provider outage or expired tokens**
- Connections move to `reconnect required`, and the booking page stops offering affected hosts' slots instead of showing
  unverified availability (PRD 24.1 #8). Hosts reconnect from **Integrations**. Bookings created while the provider was
  failing are retried by `calendar_retry`.

**Suspected double booking**
- The DB has two guards: host-row locks and a partial unique index. On Postgres there is also the `bookings_no_overlap`
  exclusion constraint. Run
  `select conname from pg_constraint where conname='bookings_no_overlap';`. If it's missing (for example `btree_gist`
  wasn't available), run `python seed_prod.py` and check its warning output.

**Security incident / leaked API key or session**
1. Revoke API keys in **Integrations → API keys** (effective immediately).
2. Affected users: *Settings → Security → Sign out other sessions*, or change the password. Both bump the session epoch,
   which invalidates every other session.
3. Everyone at once: rotating `SECRET_KEY` logs everybody out. It also invalidates **all API keys**, because they are
   stored as keyed hashes, and every outstanding signed link (password resets, slot tokens). If `ENCRYPTION_KEY` is
   unset, the encryption key is derived from `SECRET_KEY`, and stored OAuth tokens and webhook secrets become unreadable.
   So set an explicit `ENCRYPTION_KEY` (a Fernet key from
   `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`) **before** you rotate
   `SECRET_KEY`, and never rotate `ENCRYPTION_KEY` without re-encrypting the stored secrets. For a single user, bumping
   the session epoch (step 2) is enough.
4. Export the audit log from **Admin → Audit log** for the investigation.

**Data subject requests**
- Export or delete own account: *Settings → Data & privacy*. Invitee erasure: **Admin → Data deletion**. The retention jobs
  (`platform_retention`, `analytics_retention`, `recap_retention`) enforce `/data-retention` automatically.

## 7. Release gate checklist (PRD 24.3)

- [x] Double-booking tests (`tests/test_booking.py`), cancellation/reschedule tests, calendar reconciliation tests
- [x] Tenant isolation tests on every resource class (`tests/test_tenant_isolation.py`)
- [x] Timezones verified (DST fixtures in `tests/test_availability.py`; host screens show zone labels)
- [x] Privacy notice and data retention published: `/privacy`, `/terms`, `/data-retention` (versioned; invitee consent stores the version)
- [ ] Monitoring configured: external uptime monitor + Sentry DSN (operator action, §5)
- [ ] Backups configured: paid Render Postgres plan + first PITR drill on Render (operator action, §3–4)
- [x] Full test suite green on SQLite **and** PostgreSQL (`TEST_DATABASE_URL=postgresql+psycopg2://... python -m pytest`)
