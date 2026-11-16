# AAL Scheduler

Calendly-style scheduling & meeting-operations SaaS built from `Calendly_Style_Scheduling_Platform_PRD.pdf`.
Modular monolith: **FastAPI + SQLAlchemy 2 + Jinja2 + vanilla JS**, SQLite by default; the full test suite also passes on **PostgreSQL 16**.
(The PRD suggests Next.js/TypeScript; this machine has no Node.js, so the PRD's "unless an existing
environment dictates otherwise" clause applies. The architecture follows PRD §19 otherwise.)

## Run

```bash
pip install -r requirements.txt
python seed.py          # demo data: demo@example.com / demo1234 (also sam@ / priya@example.com)
run.bat                 # or ./run.sh  ->  http://127.0.0.1:8000
python -m pytest        # full suite on SQLite
```

Everything works offline: calendar, Meet/Zoom/Teams, Stripe/PayPal and SMS have **real adapters**
(enabled by filling `.env`, see `.env.example`) **and** built-in mock modes. All outgoing email/SMS is
viewable at **/dev/inbox**. The background worker (reminders, webhooks, calendar sync, retries) runs
inside the app process every 5 s.

## Where each PRD feature lives

| PRD area | URL(s) | Code |
|---|---|---|
| Auth, verify email, reset, Google sign-in, invites, onboarding (§5, §21) | `/signup` `/login` `/onboarding` `/invite/{t}` | `routers/auth.py` |
| Home dashboard | `/app` | `routers/dashboard.py` |
| Event types: 1:1, group, collective, round robin, multiple durations, one-off, single-use links, questions, policies, permissions, embed code (§6) | `/app/event-types` | `routers/event_types.py` |
| Availability: schedules, multi-range weekly hours, overrides, holidays (GB/US/IN), host-wide limits (§7.1) | `/app/availability` | `routers/availability.py`, `services/holidays.py` |
| Availability engine, DST, buffers, limits, notice/horizon, free/busy exceptions (§7.2, §20) | — | `services/availability.py`, `services/timeutil.py` |
| Atomic booking, holds, idempotency, no double-booking, reschedule saga, cancel policy/approval, no-show, reassign (§7.3–7.4, §8.3) | — | `services/booking.py`, `db.lock_hosts` |
| Public booking page, profile, team pages, confirmation, manage/cancel/reschedule, waitlist, email OTP, prefill/UTM, redirect, embed mode (§8) | `/{user}` `/{user}/{event}` `/team/{slug}` `/booking/{token}` | `routers/public.py`, `static/booking.js` |
| Public JSON booking API (§22) | `/api/v1/public/...` | `routers/public_api.py` |
| Scheduled events: tabs, detail, notes, cancel, reschedule, no-show, approvals, CSV | `/app/scheduled` | `routers/scheduled.py` |
| Calendars (Google, Microsoft, demo), conflict/destination calendars, health, sync & conflict detection; video (Meet/Zoom/Teams); ICS (§9) | `/app/integrations` | `services/providers/*` |
| Transactional notifications + workflows (reminders, follow-ups, SMS, quiet hours, preview, test-send, run history) (§11) | `/app/workflows` | `services/notifications.py`, `services/workflows.py` |
| Payments: Stripe/PayPal, pay-before-confirm, refunds, reconciliation (§12) | `/pay/{token}` | `services/payments.py` |
| Meeting packages (redeem a code at checkout), invoices (draft/sent/paid/void), payment links (§12 P3) | `/app/payments`, `/p/package\|invoice\|link/{t}` | `services/commerce.py`, `routers/commerce.py` |
| Self-service subscription billing (Stripe Billing or mock), seats = active members, cancel/downgrade at period end, usage meters (ACC-005) | `/app/billing` | `services/billing.py`, `services/usage.py` |
| OAuth 2.0 authorization code + PKCE for third-party apps, refresh rotation, revocation (§15.3 P3) | `/oauth/authorize`, `/oauth/token`, `/app/integrations/oauth-apps` | `services/oauth.py`, `routers/oauth.py` |
| Observability: Sentry (optional), worker failure alerts, deep health check (§23) | `/healthz/deep` | `services/observability.py` |
| Teams, members, roles, invites, domain control (§10.1, ACC-004) | `/app/teams` | `routers/teams.py` |
| Routing forms with AND/OR rules, preview, submissions, explicit prefill field mapping, Salesforce/HubSpot/mock CRM owner lookup (§10.5) | `/app/routing`, `/r/{ws}/{form}` | `services/routing_engine.py`, `services/crm.py` |
| Contacts, no-booking follow-ups, relationship insights, CRM connection + activity push (§13) | `/app/contacts`, `/app/contacts/insights`, `/app/contacts/crm` | `services/contacts.py`, `services/crm.py`, `models_crm.py` |
| Analytics + CSV/JSON + UTM + routing/RR/revenue, lead time, weekday×hour heatmap, retention purge (§14) | `/app/analytics` | `services/analytics.py` |
| REST API with API keys, cursor pagination, Idempotency-Key, rate limits (§15) | `/api/v1/*`, docs `/api/docs` | `routers/api_v1.py` |
| Signed outbound webhooks with retries + dead letter; inbound provider webhooks | `/app/integrations/webhooks` | `services/webhooks.py` |
| Embeds: inline, popup widget, popup text, postMessage (§15.4) | `/embed.js`, `/embed/demo` | `static/embed.js` |
| AI scheduling assistant: requests, schedule-from-email, meeting prep, all with approval (§16.1) | `/app/assistant` | `services/assistant.py` |
| Notetaker: Recall.ai-style/mock recording bot, consent notices, encrypted transcripts + retention, summaries, sharing (§16.2) | `/app/recaps` | `services/notetaker.py`, `services/recording.py` |
| Meeting polls (§6.2) | `/app/polls`, `/poll/{t}` | `routers/polls.py` |
| Admin: audit search + CSV/JSON export, security policies, SAML 2.0 SSO (SP-initiated, JIT, enforcement), DNS domain verification, SCIM Users+Groups, data deletion (UI + `DELETE /api/v1/admin/data-deletion`), managed event templates, system health (§17) | `/app/admin`, `/sso/{ws}/login`, `/scim/v2` | `routers/admin.py`, `routers/sso.py`, `services/sso.py`, `services/enterprise.py`, `routers/scim.py`, `services/managed.py` |
| Settings: profile, branding, notifications, password, sessions ("sign out other sessions"), workspace, plan/entitlements, data export/deletion (ACC-005/006) | `/app/settings` | `routers/settings.py`, `entitlements.py` |
| Release gate: privacy notice, terms, data retention policy (versioned; booking consent stores the version) (§24.3) | `/privacy` `/terms` `/data-retention` | `routers/legal.py`, `templates/legal/*`, `services/retention.py` |
| Invitee-page i18n: en, es, fr, de, hi (§8) | `?lang=xx` | `app/i18n.py` |
| Installable PWA + mobile quick actions (§2, P3) | `/manifest.webmanifest` `/sw.js` `/app/m` | `routers/pwa.py`, `static/sw.js`, `templates/app/mobile.html` |
| Browser extension (Chrome/Edge MV3) (§2, P3) | `extension/` | `extension/*`, API in `routers/api_ext.py` (`/api/v1/ext/*`) |
| Cross-tenant isolation tests on every resource class (§23) | — | `tests/test_tenant_isolation.py` |
| Deployment, backups, restore drill, monitoring, runbooks (§17.1, §23) | — | `render.yaml`, `Dockerfile`, `seed_prod.py`, `docs/OPERATIONS.md` |

Cross-cutting: tenant scoping on every query, CSRF, rate limiting, request IDs + structured logs,
transactional outbox (`events.py`), audit log, encrypted OAuth tokens and secrets (`security.py`).

## Booking integrity
`create_booking` locks host rows (`host_locks`) as the first statement of its transaction, re-checks
availability inside the lock, and inserts. A partial unique index on `bookings(host_id, start_at)` for
active exclusive bookings is the DB-level backstop. **On PostgreSQL, also add:**

```sql
CREATE EXTENSION IF NOT EXISTS btree_gist;
ALTER TABLE bookings ADD CONSTRAINT no_overlap EXCLUDE USING gist
  (host_id WITH =, tsrange(start_at, end_at) WITH &&)
  WHERE (exclusive AND status IN ('held','pending','pending_payment','confirmed'));
```

On PostgreSQL, `app.services.booking.ensure_postgres_constraints(engine)` creates the extension and an
equivalent constraint (`bookings_no_overlap`) idempotently at startup; it does nothing on SQLite.

## Deploy (Render)

`render.yaml` is a Render Blueprint: a Python web service (`uvicorn ... --proxy-headers`, health check `/healthz`) plus
a managed PostgreSQL 16 database. `SECRET_KEY` is generated, `ENV=prod`, and `DATABASE_URL` comes from the database
(`postgres://` URLs are normalised to `postgresql+psycopg2://`).

1. Push the repo, then in Render choose **New → Blueprint** and select it.
2. Set `BASE_URL` (or leave it empty to use `RENDER_EXTERNAL_URL`), `SEED_OWNER_EMAIL`, `SEED_OWNER_PASSWORD` and
   `PRIVACY_CONTACT_EMAIL`.
3. Each deploy runs `python seed_prod.py`, which is idempotent: schema sync, Postgres overlap constraint, and the first owner
   only if that email doesn't exist. Run it again any time.

Docker: `docker build -t aal-scheduler . && docker run -p 8000:8000 -e ENV=prod -e SECRET_KEY=... -e DATABASE_URL=... aal-scheduler`.
Backups (Render PITR plus daily backups), the tested restore procedure, monitoring and runbooks are in **docs/OPERATIONS.md**.

## PostgreSQL

The whole suite runs against Postgres with
`TEST_DATABASE_URL=postgresql+psycopg2://user@host:5432/db python -m pytest`. Checked on PostgreSQL 16.2, using the
binaries from the `pgserver` wheel: every test that passes on SQLite also passes on Postgres, except `test_ensure_postgres_constraints`, because that embedded build has no `btree_gist` contrib extension. Render's managed Postgres includes it. Postgres-specific handling:
- `db.lock_hosts` does `INSERT … ON CONFLICT DO NOTHING` and then a row-locking `UPDATE` per host, in sorted order, so
  there are no deadlocks.
- The partial unique index has both `sqlite_where` and `postgresql_where` clauses.
- Datetimes are naive UTC everywhere.
- The engine uses `pool_pre_ping` and `pool_recycle` for managed databases.
- `init_db()` adds missing nullable columns to existing tables. This is a small additive migrator; destructive changes
  still need a real migration.
- At startup, `booking.ensure_postgres_constraints` installs the `btree_gist` exclusion constraint. If the extension is
  missing, it logs a warning and the app keeps running.

## Invitee languages (i18n)

`app/i18n.py` has a message catalog for en, es, fr, de and hi:
- `t(key, locale, **fmt)`: for example `t("book.spots_left", "fr", n=3)`. Missing keys fall back to English, then to the key.
- `get_locale(request, host_locale)`: picks `?lang=` first, then the `lang` cookie (set automatically after `?lang=`),
  then the host's non-English profile language, then `Accept-Language`, then `en`. The result is cached on `request.state.locale`.
- In Jinja: `{{ t('book.schedule_event') }}` uses the template's `locale` variable (or `request.state.locale`).
  `i18n_catalog(locale)` returns the merged dict, for example `|tojson` for `booking.js`. `supported_locales` lists the
  native names for a language switcher.

## Sessions

Session cookies are signed and HTTP-only. They are `Secure` when `BASE_URL` is https, and use `SameSite=Lax`. Each session
carries the user's `session_epoch`. Changing or resetting the password, or **Settings → Security → Sign out other
sessions**, bumps the epoch, which drops every other session immediately. Sessions also expire after
`SESSION_IDLE_MINUTES` (default 3 days) of inactivity and `SESSION_MAX_AGE_MINUTES` (default 14 days) after sign-in.
Login checks `services.sso.sso_required_for_email` first and redirects SSO-enforced domains to the IdP.

## Known limitations
- Real Google, Microsoft, Zoom, Stripe, PayPal, Salesforce and HubSpot adapters are written but have not been tested against live accounts.
- SAML: encrypted assertions, signed AuthnRequests, IdP-initiated login and Single Logout are not supported.
- The Recall.ai notetaker adapter is untested against a live account; only transcripts are stored (encrypted in the database), not audio/video recordings.
- PayPal webhooks are verified server-to-server only when `PAYPAL_WEBHOOK_ID` is set (rejected in prod otherwise).
- Invitee-facing pages (booking, confirmation, manage/cancel/reschedule) are translated into en/es/fr/de/hi via `app/i18n.py`. Host (back-office) pages are English-only.
- The PRD's P3 "assess Exchange / iCloud (CalDAV)" items are research tasks and are not built.
- After deploying: configure an external uptime monitor and `SENTRY_DSN`, and run a first point-in-time-restore drill on Render (see `docs/OPERATIONS.md`).
- The mobile app is an installable PWA, not a native iOS/Android app (PRD 3.2 says a responsive web app is enough). The browser extension isn't published to any store yet: load it unpacked.
