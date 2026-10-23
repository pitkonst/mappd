# Authentication

MAPP-D has one integration point for authentication: `mappd.web.auth.Backend`. One pure-ASGI
middleware (`AuthMiddleware`) asks the configured backend who is calling, enforces the path
policy below, and stamps the acting person's name into decision bodies. This document is for
developers wiring MAPP-D into a company's sign-in.

## 1. What auth protects — and what it does not

Authentication covers the app's HTTP surface: the HTMX pages, `/api/v1/*`, the workflow and
actions dashboards. When a person acts, the middleware overwrites the `operator` field of
the decision body with the signed-in name — that is the provenance recorded on the decision.

It does **not** cover: the LLM gateway, MLflow, Postgres, the mocks — those stay on the
internal network behind your perimeter. `MAPPD_AUTH=none` is for
local development only: anyone who can reach the app can act as anyone.

## 2. Modes and environment variables

`MAPPD_AUTH` picks the backend:

| Value | Behaviour |
| --- | --- |
| `none` (default) | Open. No principal, no checks, no stamping. |
| `jwt` | Built-in: users in the `mappd_users` table, HS256 tokens. |
| `header` | Trust `MAPPD_AUTH_HEADER`/`MAPPD_AUTH_ROLES_HEADER` set by a proxy. |
| `pkg.mod:factory` | A custom backend; the app calls `factory(settings)` at startup. |

| Variable | Used by | Meaning |
| --- | --- | --- |
| `MAPPD_AUTH` | all | The mode above. Any other value refuses startup. |
| `MAPPD_AUTH_SECRET` | jwt | HS256 secret, ≥32 chars; required. Rotate = invalidate every session. |
| `MAPPD_AUTH_TOKEN_HOURS` | jwt | Token and cookie lifetime (default 8). Positive number. |
| `MAPPD_AUTH_HEADER` | header | Header carrying the user name (default `X-Forwarded-User`). |
| `MAPPD_AUTH_ROLES_HEADER` | header | Header with comma-separated roles (default `X-Forwarded-Roles`); unknown roles are dropped. |
| `MAPPD_AUTH_COOKIE_SECURE` | jwt | `on` (default): the `mappd_session` cookie is `Secure`. `off` for plain-HTTP dev. |

## 3. The policy

Evaluated in order for every HTTP request (the middleware is transparent to lifespan and
passes websockets through):

1. **Public** — `/health`, `/login`, `/logout`, `/static/*`, `POST /api/v1/auth/token`, and
   each process's declared public paths. No sign-in needed.
2. **Anonymous** — under `/api/`: `401 {"detail": "sign in required"}` with
   `WWW-Authenticate: Bearer`. An htmx request (`HX-Request: true`): 401 with
   `HX-Redirect: /login?next=<path>`. Otherwise `303` to `/login?next=<path>`.
3. **Admin areas** — `/actions`, `/workflows`, `/ops` and their `/api/v1/...` APIs: the
   `admin` role is required (`403` otherwise).
4. **Process paths** — `/<name>` and `/api/v1/<name>` for each registered process. Safe
   methods (GET/HEAD/OPTIONS) need any signed-in principal; mutating methods need the
   process's `role` (`admin` implies every role).
5. **Everything else** (`/`, `/docs`, `/api/v1/auth/me`) — any signed-in principal.

A process declares its requirements on its `Process` object:

```python
process = Process(
    name="installation",
    role="dispatcher",  # who may mutate; default "operator"
    public=("/api/v1/installation/inbound/telegram",),  # exact paths that skip sign-in
)
```

`public` is for inbound webhooks that carry their own credential — a public webhook MUST
verify its own secret: installation's Telegram webhook checks `X-Telegram-Bot-Api-Secret-Token`
against `MAPPD_INSTALLATION_TELEGRAM_SECRET` and fails closed with a 401 when the secret is unset.

## 4. Operator stamping

**Rule: the acting person on a decision body is a field named `operator`.** For a signed-in
principal on a mutating request to a process path, the middleware sets `operator` to the
principal's name — overwriting whatever the client sent. Stamping is fail-closed, an
allowlist:

- **JSON** — classified exactly like FastAPI: a missing/blank content-type, `*/json` or any
  `*+json` media type. The body must parse to a JSON object; the response is re-encoded and
  the content-type normalized to `application/json`. A non-object or unparseable body → 400.
- **`application/x-www-form-urlencoded`** (parameters ignored) — all `operator` pairs are
  dropped and the stamp appended. A raw `;` in the body → 400 (multipart's form parser
  splits on `;` too, so it could hide a pair the stamper never saw; browsers encode `;` as
  `%3B`).
- **Anything else** with a non-empty body — multipart, `text/plain`, octet-stream — → 415.
- An empty body passes through; more than one content-type header → 400.

Models must treat `operator` as an ordinary field (pydantic ignores unknown keys by
default, so models without it are unaffected).

Reserved user names: `auto` (the autonomy operator already used on decision records) and
`system`. Bodies are buffered up to 1 MiB for stamping; larger mutating requests get `413`.

For cookie-authenticated mutating requests the middleware also requires `Origin` (or, if
absent, `Referer`) — when present — to host-match `Host` or the first `X-Forwarded-Host`
value; a mismatch is a 403 cross-site refusal. Bearer credentials skip this check. The
same check guards `POST /login` and `POST /logout` even though they are public — a
cross-site POST to either is login-CSRF or a forced logout.

## 5. The built-in jwt backend

Users live in `mappd_users` (migration `mappd_0003`); passwords are salted scrypt, roles a
JSONB list of `operator`, `dispatcher`, `admin`. Manage them with the CLI:

```bash
echo 's3cret-pass' | mappd user add alice --role operator --password-stdin
mappd user list
echo 'n3w-pass' | mappd user passwd alice --password-stdin
mappd user roles alice --role operator --role admin   # replaces the set
mappd user disable alice    # takes effect on the next request
mappd user enable alice
```

Tokens:

```bash
curl -X POST http://localhost:8000/api/v1/auth/token \
  -H 'content-type: application/json' \
  -d '{"name": "alice", "password": "s3cret-pass"}'
# {"access_token": "...", "token_type": "bearer", "expires_in": 28800}
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/auth/me
```

Browsers sign in at `/login` (name + password → `mappd_session` cookie, HttpOnly,
SameSite=Lax) and sign out at `POST /logout`. The user row is read on every request, so
`disable` and role changes apply immediately — roles come from the table, never from the
token. Rotating `MAPPD_AUTH_SECRET` invalidates every token and session at once.

## 6. Browsers, cookies and SSE

The session cookie is `HttpOnly` + `SameSite=Lax`, and mutating requests pass the Origin
check (section 4) — together they block classic CSRF and cross-site sends. Behind a
reverse proxy the check also accepts the first `X-Forwarded-Host` value, since browsers
see the proxy's host, not the internal `Host`; a cross-site browser request cannot set
that header, so this cannot weaken the check. SSE endpoints
(`/api/v1/*/events`) work through the cookie: `EventSource` sends it like any other
request. There is deliberately no `?token=` query variant — tokens in URLs leak into logs,
browser history and Referer headers.

## 7. The header backend behind a corporate proxy

With `MAPPD_AUTH=header` the app trusts `X-Forwarded-User` and `X-Forwarded-Roles`
(configurable). Deploy behind a proxy that authenticates and **strips these headers from
client requests** — e.g. oauth2-proxy:

```yaml
# oauth2-proxy: pass the authenticated user upstream
set_xauthrequest: true
# then, at the edge (nginx/Caddy), ALWAYS:
#   proxy_set_header X-Forwarded-User $http_x_email;   # never trust the client's
#   proxy_set_header X-Forwarded-Roles "operator";
```

If a client can reach the app without the proxy, it can name any user. Only use this mode
when the network makes that impossible.

Header-authenticated principals are treated like cookie ones (`via_cookie=True`): the
browser is signed in through the proxy's own session cookie, so mutating requests pass
the same Origin check as the jwt cookie — that blocks cross-site forms riding the SSO
session, not just header spoofing.

## 8. Writing your own backend

Implement `mappd.web.auth.Backend` — one async method — and point `MAPPD_AUTH` at a
factory. Sketch: OIDC access tokens verified against the issuer's JWKS:

```python
# mycompany/auth.py
import jwt
from starlette.requests import HTTPConnection
from mappd.web.auth import Principal


class OidcBackend:
    def __init__(self, settings):
        self._jwks = jwt.PyJWKClient("https://sso.example.com/.well-known/jwks.json")

    async def authenticate(self, conn: HTTPConnection) -> Principal | None:
        header = conn.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            return None
        try:
            key = self._jwks.get_signing_key_from_jwt(header[7:])
            claims = jwt.decode(
                header[7:],
                key.key,
                algorithms=["RS256"],
                audience="mappd",
                issuer="https://sso.example.com",
            )
        except jwt.PyJWTError:
            return None
        roles = {r for r in claims.get("roles", []) if r in {"operator", "dispatcher", "admin"}}
        return Principal(name=claims["email"], roles=frozenset(roles))


backend = OidcBackend  # the class is the factory: called as backend(settings)
```

Then `MAPPD_AUTH=mycompany.auth:backend`. Set `Principal.via_cookie=True` when your
credential is a cookie, so the Origin check applies to mutating requests. A failing
factory or a bad import string stops the app at startup with a `SettingsError`.

## 9. Limits of the built-in backend

No rate limiting, lockout, MFA or password reset — `mappd user` is an operator tool, not an
IdP. For production SSO use the `header` backend behind your proxy or a custom backend
(section 8).
