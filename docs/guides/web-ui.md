# Web UI

A single-page app (React 19, TypeScript, Vite) served by its own Caddy container
on the same origin as the API: <http://127.0.0.1:3000> locally (`make ui`).

| Page | What it shows |
|---|---|
| `/login` | Email and password. A wrong combination says only that; it never says which part was wrong |
| `/incidents` | Active incidents by default, newest first. Filters for status, severity and service live in the URL, so a view can be bookmarked or shared. Refreshes every 10 s, so new incidents appear without a reload |
| `/incidents/:id` | The incident, its signals (with why each one joined), the entities involved, the suspected deployment, and the timeline. Refreshes every 10 s |

## Roles

The UI hides what your role can't do; the API refuses it anyway.

| Role | On an incident |
|---|---|
| Viewer | Read everything. No status, severity or comment controls |
| Responder, approver, admin | Change status (only to the states the state machine allows), set severity, comment |

## Security

| Concern | How it's handled |
|---|---|
| Session | An HttpOnly, SameSite=Strict cookie. The app never sees or stores the session token |
| CSRF | Every non-GET request echoes the readable `sentinel_csrf` cookie in `X-CSRF-Token` |
| Stored XSS | Text from alerts, logs, people and (later) the LLM is only ever rendered as text: no `dangerouslySetInnerHTML`, no HTML Markdown |
| CSP | `default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'`, set by the web Caddy. It's the second line of defence if markup ever slipped through |
| Other headers | `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`. HSTS arrives with TLS (the AWS step) |
| Client IP | Behind the web Caddy, the API takes the client's address from `X-Forwarded-For`, but only when the request carries the internal secret that Caddy adds. A client can't forge either header, so it can't dodge the login rate limit or put another address in the audit log |

## The API client

`frontend/src/api/schema.d.ts` is generated from the API's OpenAPI schema, which is committed as `frontend/src/api/openapi.json`.
- **Never edit them by hand.** After an API change, run `make api-client`.
- **Kept in step:** a backend test fails if `openapi.json` is out of date, and frontend CI fails if `schema.d.ts` doesn't match it.

## Tests

| Command | What |
|---|---|
| `cd frontend && npm test` | Component tests (Vitest, Testing Library). They cover sign-in, the list and its filters, role-aware controls, status changes with the CSRF header, the server's refusals, and stored XSS |
| `make stack-up-fast && make ui && make test-e2e` | Browser tests (Playwright) against the real stack. They create a real admin, viewer and responder, and raise real incidents through Alertmanager. They check that an incident appears live, that a viewer can't change status (in the UI or through the API), that a responder can, and that a stored-XSS payload renders inert under the real CSP |

Locally Playwright uses the installed Chrome. CI installs Chromium. The `ci-e2e` workflow runs the browser tests on every change to the frontend, the backend or the deployment files.
