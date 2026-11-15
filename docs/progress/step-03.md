# Step 3 report — Authentication, RBAC, API keys, audit log

**Status:** done locally. CI: CI_RESULT
**Branch / PR:** `step-03-auth` → PR_LINK (not merged; merging is your decision)
**Date:** 2026-11-16

## What was built

| Area | Files |
|---|---|
| Permissions | `auth/permissions.py`: permission list, role map (viewer ⊂ responder ⊂ approver ⊂ admin) and API-key scopes |
| Passwords | `auth/passwords.py`: Argon2id, 12-char minimum, a dummy hash check for unknown emails, rehash on login when parameters change |
| Tokens | `auth/tokens.py`: 256-bit tokens, `HMAC(secret_key)` storage, signed CSRF token, `sk_live_<prefix>_<secret>` keys |
| Rules | `auth/service.py`: login, lockout, sessions (12 h idle / 7 day absolute), invites, API keys, user admin, last-admin guard |
| Rate limit | `auth/ratelimit.py`: sliding window, 5 per minute per IP and per email |
| Audit | `security/audit.py`: hash-chained writer (advisory lock), redaction of secret-looking fields, `verify_chain` |
| API | `api/security.py` (`public` / `authenticated` / `require` / `require_scope`, CSRF), `api/errors.py` (RFC 9457) |
| Routers | `auth` (login, logout, me, accept-invite), `users`, `api-keys`, `audit` (list + verify), `/api/v1/meta` |
| CLI | `sentinel create-admin`, `sentinel rbac-docs`, `make create-admin`, `make rbac-docs` |
| Migration | `0002_auth`: `sessions.last_seen_at`, `user_invites`, nullable `password_hash`, readonly can't read password or invite hashes |
| Docs | Generated [`docs/rbac.md`](../rbac.md), security, API, database and setup docs updated, this report |

## Acceptance criteria (roadmap)

| Criterion | Result |
|---|---|
| Anonymous → 401 | ✅ every protected route, in the route × role matrix |
| Wrong role → 403 | ✅ 12 protected routes × 4 roles, all as expected |
| Security tests pass | ✅ CSRF, lockout, no user enumeration, key scopes, audit tamper and deletion detection, rate limit, idle timeout, session revocation |
| Every route declares access; a test fails if one is missing | ✅ `test_every_route_declares_exactly_one_access_rule`, plus a test showing an undeclared route is caught |
| RBAC docs table matches the code | ✅ `docs/rbac.md` is generated, and CI fails if it drifts |
| Create an admin with the CLI, log in with curl, try a forbidden route | ✅ on the live Podman stack (below) |

### Live walkthrough (Podman stack, real curl)
```
1) create-admin (CLI)                         → Created admin admin@example.com
2) anonymous GET /api/v1/users                → 401
3) admin login                                → 200 (cookies set)
4) admin /me                                  → admin, 10 permissions
5) admin POST /users without X-CSRF-Token     → 403
6) admin POST /users with CSRF                → 201 + one-time invite token
7) viewer accepts invite                      → 204
8) viewer login                               → 200
9) viewer GET /api/v1/users                   → 403 problem+json
10) wrong password                            → 401 "Invalid email or password."
11) GET /audit/verify                         → {"ok":true,"checked":8,"total":8}
12) API key on a user route                   → 401
```

### Test totals (local)

| Suite | Result |
|---|---|
| Backend unit (`make test`) | 52 passed |
| Database and API (`make test-db`) | 46 passed |
| Integration | 1 passed |
| All backend (`make test-all`) | **99 passed, coverage 93.9%** (gate 85%) |
| Frontend | 2 passed |
| `make lint` | clean; import-linter `cli → api → auth → security → db → log → config` kept |

### Checking that the tests can actually fail
- **Giving `viewer` the `users:manage` permission:** caught by the role-map test and the RBAC doc check.
- **Disabling the permission check in `require()`:** the matrix test failed immediately ("viewer GET /api/v1/api-keys → 200").

The code was restored after both checks.

## Bugs found and fixed during this step

1. **Connection leak in request handling, from Step 2 code. This was a real production bug.**
   - **Cause:** the DB session dependency used `async for` over an async generator. When a request was rejected after the database had been queried (for example a missing CSRF token), Python left the inner generator suspended, so the session was never rolled back or closed.
   - **Fix:** `session_scope` is now an async context manager.
   - **Regression test:** I added one, and confirmed the old pattern leaks.
2. **Audit list crashed (500) when entries had an IP address.** Postgres returns `IPv4Address` objects. The tests missed it because TestClient's address isn't an IP. The tests now use a realistic client IP (`203.0.113.10`), so the INET path is always exercised.
3. **Login crashed when the client address isn't an IP** (test client, unix sockets). `client_ip()` now validates the address and returns `None` otherwise.
4. **`make up` kept running old code.** podman-compose only recreates a container when its configuration changes, not its image, so the `migrate` container ran the previous build and skipped migration `0002`. `make up` now builds and then forces recreation, and the data volume is kept.
5. **Coverage was undercounted.** SQLAlchemy async runs database code on greenlets, and coverage stopped tracing after each `await`. Coverage now has `concurrency = ["thread", "greenlet"]`. The real figure is 93.9%, not 86%. The Step 2 coverage number was measured without this setting too, so it was also undercounted.
6. **This FastAPI version wraps included routers** (`_IncludedRouter`). A naive route walk saw only 3 of the 16 routes, which would have let the "every route declares access" test pass while checking nothing. The walk now goes through the wrappers. A cross-check test against the OpenAPI schema fails if routes are ever hidden again.

## Changes beyond the written plan (no architecture change)

- **The coverage gate (85%) now applies to the full suite** (`make test-all`, CI). `make test` reports unit coverage only (67.6%), because most auth code is only meaningfully testable against a real database.
- **Small schema additions:** `sessions.last_seen_at` (needed for the 12 h idle timeout) and the `user_invites` table (needed for invites).
- **Re-issuing an invite doubles as password reset** (admin-initiated). There's no email sending yet.
- **`/api/v1/meta`** is implemented (it's in the API catalogue). It returns version and environment only for now.

## Known limitations

- **The rate limiter is in memory.** That's correct for v1's single API process. It would move to Postgres or Caddy if the API ever runs as several processes.
- **In local Podman, every request comes from the network gateway (`10.89.0.5`),** so the per-IP login limit treats all local clients as one. Real client IPs need Caddy with trusted proxy headers (AWS steps). The per-email limit and account lockout are unaffected.
- **The general API limit (300/min per session) isn't implemented.** Only login and invite are rate-limited. P21 is a "Should" item and is planned with Step 32 (security hardening).

## How you verify

```bash
cd "/Users/asadsheikh/Coding/SENTINEL Autonomous AI Site Reliability Engineer"
git checkout step-03-auth
make install
make lint && make test && make test-all
make up                                              # applies migration 0002 automatically
make create-admin email=you@example.com name="You"   # choose a password (12+ chars)
curl -c /tmp/s.jar -X POST localhost:8000/api/v1/auth/login \
     -H 'content-type: application/json' -d '{"email":"you@example.com","password":"YOUR-PASSWORD"}'
curl -b /tmp/s.jar localhost:8000/api/v1/auth/me
curl -b /tmp/s.jar localhost:8000/api/v1/audit/verify
open http://localhost:8000/api/v1/docs               # interactive API docs
make down
```

## Not in this step (by design)

The login page in the web UI (Step 13). Approvals and separation of duties (Step 24). The `credentials` table and GitHub tokens (Step 20). Ingestion endpoints that use API-key scopes (Steps 6 and 10).
