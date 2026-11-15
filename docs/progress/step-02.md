# Step 2 report — Database foundation and migrations

**Status:** done locally. CI: CI_RESULT
**Branch / PR:** `step-02-database` → PR_LINK (not merged; merging is your decision)
**Date:** 2026-11-16

## What was built

| Area | Files |
|---|---|
| Engine and sessions | `backend/src/sentinel/db/engine.py`: async engine (asyncpg), statement timeout, `session_scope` (commit or rollback) |
| IDs | `db/ids.py`: UUIDv7 generator (RFC 9562), monotonic within a process |
| Base model | `db/base.py`: naming conventions, UUIDv7 primary key, timestamp mixins |
| Models | `db/models/identity.py` (`users`, `sessions`, `api_keys`), `db/models/platform.py` (`audit_log`, `jobs`) |
| Repositories | `db/repositories/identity.py`: users, sessions, API keys |
| Migrations | `db/migrations/env.py` (async), `versions/0001_initial.py`, `db/migrate.py` |
| Roles | `scripts/db_roles.sql`, `deploy/compose/initdb/10-sentinel-roles.sh` |
| API | `/readyz` (database check, 503 when down), `api/deps.py` (`DbSession` dependency) |
| CLI | `sentinel db upgrade / downgrade / current / revision / docs` |
| Compose | Role setup on first init, one-shot `migrate` service, API connects as `sentinel_app` |
| Make | `migrate`, `migration`, `db-current`, `db-docs`, `db-shell`, `db-reset`, `test-db`, `test-all` |
| Docs | Generated [`docs/database.md`](../database.md), setup guide §4, this report |

## Acceptance criteria (roadmap)

| Criterion | Result |
|---|---|
| `make migrate` works on a fresh DB | ✅ `make db-reset` → roles created → `migrate` container exited 0 → `make migrate` from the Mac is a no-op → `make db-current` = `0001 (head)` |
| DB tests pass | ✅ locally: 26 passed (`make test-db`). CI: CI_RESULT |
| upgrade → downgrade → upgrade | ✅ `test_upgrade_downgrade_upgrade_cycle` on a fresh database |
| CRUD | ✅ 10 repository tests (users, sessions, API keys, cascades) |
| `audit_log` UPDATE/DELETE denied for the app role | ✅ tested, and also checked by hand with `psql`: `ERROR: permission denied for table audit_log` |
| `docs/database.md` generated | ✅ `make db-docs`. CI fails if it's out of date |

### Test totals (local)

| Suite | Result |
|---|---|
| `make test` (backend unit) | 29 passed, coverage 88.5% (gate 85%) |
| `make test` (frontend) | 2 passed |
| `make test-db` | 26 passed |
| `make test-integration` | 1 passed |
| unit + db combined | coverage 95.6% |
| `make lint` | clean (ruff, mypy --strict, import-linter: `cli → api → db → log → config` kept) |

### Checking that the tests can actually fail
I removed the `REVOKE … ON audit_log FROM sentinel_app` line from the migration and re-ran the suite. The UPDATE and DELETE tests **failed**, as they should. I then restored the line. The tests check the exact error ("permission denied for table audit_log"), so they can't be fooled by the trigger, which raises a different message.

## Design details worth knowing

- **Three roles**, matching the design doc:
  - `sentinel_migrator` owns the schema.
  - `sentinel_app` is what the API uses. It can read and write data but can't create tables, and it can't UPDATE, DELETE or TRUNCATE `audit_log`.
  - `sentinel_readonly` can SELECT, but not from `sessions` or `api_keys`.
- **Models and migrations can't drift.** `test_models_match_migrations` runs Alembic's autogenerate comparison against the migrated database and expects zero differences.
- **`/healthz` vs `/readyz`.** Liveness doesn't touch the database. Readiness returns 503 when Postgres is down, and the API still starts in that case.

## Small additions beyond the written plan (no architecture change)

1. **An audit-log trigger** blocks UPDATE, DELETE and TRUNCATE for *every* role, including the owner. The plan only specified the role grant. This is defence in depth.
2. **`sentinel_readonly` can't read the `sessions` and `api_keys` hash tables.**
3. **A one-shot `migrate` service in compose**, so `make up` gives a ready database with no manual step.
4. **`/readyz`.** My Step 1 code already pointed to it, as coming in Step 2.

## Problems found

- **`make db-reset` deletes the local data volume.** It's required once after this step, because the Step 1 volume was created without the new roles. Nothing important was in it (only the empty Step 1 database).
- **The migrate container printed nothing** at first. Alembic logging wasn't configured when it runs without an ini file. Fixed: `sentinel db …` now logs each migration.

## How you verify

```bash
cd "/Users/asadsheikh/Coding/SENTINEL Autonomous AI Site Reliability Engineer"
git checkout step-02-database
make install
make lint && make test && make test-db
make db-reset                     # once: recreate the dev DB with roles (deletes local data)
make ps                           # postgres healthy, migrate Exited (0), api healthy
curl localhost:8000/readyz        # {"status":"ready","database":"ok"}
make db-current                   # 0001 (head)
make db-shell                     # then try:  \dt   and   \du
make down
```

## Not in this step (by design)

Password hashing, login, RBAC, API-key issuing and the audit hash-chain writer (Step 3). Job claiming and workers (Step 17). `credentials` and every later table arrive with the steps that need them.
