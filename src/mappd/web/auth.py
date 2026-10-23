"""Operator authentication: one integration point, several backends.

`AuthMiddleware` is a pure ASGI middleware (not BaseHTTPMiddleware, so SSE streams keep
working). It asks a `Backend` for a `Principal`, stores it in
`scope["state"]["principal"]` (`request.state.principal`), enforces the path policy, and
stamps the signed-in name into `operator` on mutating process requests. Stamping is
fail-closed: only a JSON object or a url-encoded form passes; any other non-empty body
(multipart, text, ...) is refused while a principal is signed in.

Modes (MAPPD_AUTH): `none` — the open behaviour, middleware is a pass-through; `jwt` —
the built-in users-table backend (HS256 bearer token or the `mappd_session` cookie);
`header` — trust a proxy's headers; `pkg.mod:factory` — a custom backend.
"""

import asyncio
import email.message
import importlib
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any, Protocol
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

import jwt
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse, Response

from mappd import db, users
from mappd.settings import Settings, SettingsError
from mappd.users import ROLES, Role, User
from mappd.web.templates import CORE

if TYPE_CHECKING:
    from collections.abc import Sequence

    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from mappd.process import Process

COOKIE = "mappd_session"
MIN_SECRET_LENGTH = 32
MAX_BODY = 1024 * 1024
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
PUBLIC_PATHS = frozenset({"/health", "/login", "/logout", "/api/v1/auth/token", "/favicon.ico"})
ADMIN_PREFIXES = (
    "/actions",
    "/workflows",
    "/ops",
    "/api/v1/actions",
    "/api/v1/workflows",
    "/api/v1/ops",
)


class Principal(BaseModel):
    """Who is signed in; `admin` implies every other role."""

    name: str
    roles: frozenset[Role]
    via_cookie: bool = Field(default=False, exclude=True)

    def has(self, role: Role) -> bool:
        """Whether the principal may act as `role`; admins may act as any role."""
        return "admin" in self.roles or role in self.roles


class Backend(Protocol):
    """THE integration point for a company's own authentication.

    A backend answers one question per request: who is this? `authenticate` gets the
    ASGI connection (headers, cookies, path — no body) and returns a `Principal` or
    None for "anonymous". It must never raise for a bad or missing credential.

    Register it with `MAPPD_AUTH=mypackage.mymodule:factory`: the app calls
    `factory(settings)` once at startup; the callable may be a class (its
    constructor is the factory). Roles it cannot know may be left empty or filled
    from its own store — `Principal.has` still makes `admin` imply all roles.

    For cookie-based credentials set `Principal.via_cookie=True` so the middleware
    applies the same-origin (CSRF) check to mutating requests; bearer tokens skip
    it. The users table (`mappd.users`) is available to backends that want local
    accounts too.
    """

    async def authenticate(self, conn: HTTPConnection) -> Principal | None:
        """Who is calling, or None for anonymous; never raise for a bad credential."""


class NoAuth:
    """MAPPD_AUTH=none: nobody is signed in and nothing is checked (local development)."""

    async def authenticate(self, conn: HTTPConnection) -> Principal | None:
        """Always anonymous."""
        return None


class JwtBackend:
    """The built-in backend: HS256 tokens issued for users in `mappd_users`.

    The token comes from `Authorization: Bearer <t>` first, else the
    `mappd_session` cookie. Claims are `sub`, `iat`, `exp`. The user row is read on
    every request, so a disable or a role change takes effect at once; roles come
    from the table, never from the token.
    """

    def __init__(self, settings: Settings) -> None:
        if len(settings.auth_secret) < MIN_SECRET_LENGTH:
            raise SettingsError(
                "MAPPD_AUTH=jwt requires MAPPD_AUTH_SECRET (at least 32 characters;"
                " generate one with `openssl rand -hex 32`)"
            )
        self._settings = settings

    def issue(self, name: str) -> tuple[str, int]:
        """A signed token for `name` and its lifetime in seconds."""
        seconds = int(self._settings.auth_token_hours * 3600)
        now = datetime.now(UTC)
        token = jwt.encode(
            {"sub": name, "iat": now, "exp": now + timedelta(seconds=seconds)},
            self._settings.auth_secret,
            algorithm="HS256",
        )
        return token, seconds

    def _token(self, conn: HTTPConnection) -> tuple[str | None, bool]:
        """The bearer token, else the cookie; the flag says which credential was used."""
        header = conn.headers.get("authorization", "")
        if header.startswith("Bearer "):
            return header.removeprefix("Bearer ").strip(), False
        cookie = conn.cookies.get(COOKIE)
        return (cookie, True) if cookie else (None, False)

    async def authenticate(self, conn: HTTPConnection) -> Principal | None:
        """Decode the token, then load the user; None on any failure."""
        token, via_cookie = self._token(conn)
        if not token:
            return None
        try:
            claims = jwt.decode(
                token,
                self._settings.auth_secret,
                algorithms=["HS256"],
                options={"require": ["sub", "iat", "exp"]},
            )
        except jwt.PyJWTError:
            return None
        sub = claims["sub"]
        if not isinstance(sub, str) or not sub:
            return None
        async with db.aconnect(self._settings) as c:
            user = await users.aget(c, sub)
        if user is None or user.disabled:
            return None
        return Principal(name=user.name, roles=frozenset(user.roles), via_cookie=via_cookie)


class HeaderBackend:
    """Trusts `auth_header` / `auth_roles_header` (comma-separated; unknown roles dropped).

    Only safe behind a proxy that STRIPS these headers from client requests —
    anyone who can reach the app directly can name any user. The principal counts
    as cookie-authenticated (`via_cookie=True`): behind an SSO proxy the browser
    holds the proxy's session cookie, so mutating requests need the same-origin
    check against login-CSRF and cross-site forms.
    """

    def __init__(self, settings: Settings) -> None:
        self._name_header = settings.auth_header.lower()
        self._roles_header = settings.auth_roles_header.lower()

    async def authenticate(self, conn: HTTPConnection) -> Principal | None:
        """The proxy's headers as a principal; None when the name header is absent."""
        name = conn.headers.get(self._name_header, "").strip()
        if not name:
            return None
        roles = {
            r.strip() for r in conn.headers.get(self._roles_header, "").split(",") if r.strip()
        }
        return Principal(
            name=name, roles=frozenset(r for r in roles if r in ROLES), via_cookie=True
        )


def backend_from(settings: Settings) -> Backend:
    """The backend `settings.auth` names; import errors become `SettingsError` (fail fast)."""
    if settings.auth == "none":
        return NoAuth()
    if settings.auth == "jwt":
        return JwtBackend(settings)
    if settings.auth == "header":
        return HeaderBackend(settings)
    module_name, _, attr = settings.auth.partition(":")
    try:
        factory = getattr(importlib.import_module(module_name), attr)
        backend = factory(settings)
    except (ImportError, AttributeError, TypeError) as e:
        raise SettingsError(f"MAPPD_AUTH={settings.auth!r}: cannot build a backend: {e}") from e
    if not callable(getattr(backend, "authenticate", None)):
        raise SettingsError(
            f"MAPPD_AUTH={settings.auth!r}: factory must return a Backend"
            " (an object with async authenticate(conn))"
        )
    return backend  # type: ignore[no-any-return]


def principal(request: Request | HTTPConnection) -> Principal | None:
    """The signed-in principal, or None (mode none, anonymous, or no middleware)."""
    return getattr(request.state, "principal", None)


def auth_mode(request: Request | HTTPConnection) -> str:
    """The configured MAPPD_AUTH value, for templates (`none` when unset)."""
    return getattr(request.app.state, "auth_mode", "none")


# --- policy ------------------------------------------------------------------

_ADMIN_MESSAGE = "requires role admin"


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(f"{prefix}/")


def _process_of(path: str, names: set[str]) -> str | None:
    """The registered process this path belongs to (`/<name>` or `/api/v1/<name>`)."""
    first, _, second = path.lstrip("/").partition("/")
    if first in names:
        return first
    if first == "api" and second.partition("/")[0] == "v1":
        rest = second.partition("/")[2]
        proc = rest.partition("/")[0]
        if proc in names:
            return proc
    return None


async def _json(
    send: Send, status: int, body: dict[str, Any], headers: dict[str, str] | None = None
) -> None:
    response = JSONResponse(body, status_code=status, headers=headers)
    await response({"type": "http", "method": "GET", "path": "/", "headers": []}, _noreceive, send)


async def _noreceive() -> Message:
    return {"type": "http.request", "body": b"", "more_body": False}


class AuthMiddleware:
    """Pure-ASGI authentication and authorization for the whole app.

    Policy, in order: public paths; anonymous (API 401, htmx 401 + HX-Redirect,
    page 303 to /login); the same-origin check for cookie credentials on mutating
    methods; admin areas; per-process role on mutating methods; everything else
    needs any signed-in principal. A signed-in mutating request to a process path
    gets `operator` stamped into its body — an allowlist: a JSON object (any
    `*/json` or `*+json` type FastAPI would parse, or a missing content-type) or
    a url-encoded form; every other non-empty body is refused while signed in.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        backend: Backend,
        mode: str,
        processes: Sequence[Process],
    ) -> None:
        self.app = app
        self.backend = backend
        self.mode = mode
        self.roles = {p.name: p.role for p in processes}
        self.public = set(PUBLIC_PATHS) | {path for p in processes for path in p.public}

    async def __call__(  # noqa: PLR0911 - the policy is a linear list of gates
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Authenticate, authorize, stamp — in that order — then call the app."""
        if scope["type"] != "http" or self.mode == "none":
            if scope["type"] == "http":
                scope.setdefault("state", {})["principal"] = None
            await self.app(scope, receive, send)
            return

        conn = HTTPConnection(scope)
        path = scope["path"]
        method = scope["method"]

        if path.startswith("/static/"):
            # Assets never need a principal; skipping authenticate avoids a user
            # lookup (a DB query in jwt mode) per file.
            scope.setdefault("state", {})["principal"] = None
            await self.app(scope, receive, send)
            return

        user = await self.backend.authenticate(conn)
        scope.setdefault("state", {})["principal"] = user

        if path in self.public:
            # Login and logout stay reachable anonymously, but a cross-site POST
            # to either is login-CSRF or a forced logout: still origin-check them.
            if (
                method not in SAFE_METHODS
                and path in ("/login", "/logout")
                and not self._same_origin(conn)
            ):
                await _json(send, 403, {"detail": "cross-site request refused"})
                return
            await self.app(scope, receive, send)
            return
        if user is None:
            await self._anonymous(scope, conn, send)
            return
        if user.via_cookie and method not in SAFE_METHODS and not self._same_origin(conn):
            await _json(send, 403, {"detail": "cross-site request refused"})
            return
        is_api = path.startswith("/api/")
        if any(_under(path, p) for p in ADMIN_PREFIXES):
            if not user.has("admin"):
                await self._forbidden(scope, send, is_api, _ADMIN_MESSAGE)
                return
            await self.app(scope, receive, send)
            return
        proc = _process_of(path, set(self.roles))
        if proc is not None and method not in SAFE_METHODS:
            role = self.roles[proc]
            if not user.has(role):
                await self._forbidden(scope, send, is_api, f"requires role {role}")
                return
            replay = await self._stamp(scope, receive, send, user.name)
            if replay is None:  # oversized body already answered
                return
            receive = replay
        await self.app(scope, receive, send)

    async def _anonymous(self, scope: Scope, conn: HTTPConnection, send: Send) -> None:
        path = scope["path"]
        nxt = path + (f"?{scope['query_string'].decode()}" if scope.get("query_string") else "")
        if path.startswith("/api/"):
            await _json(
                send,
                401,
                {"detail": "sign in required"},
                {"WWW-Authenticate": "Bearer"},
            )
            return
        if conn.headers.get("hx-request") == "true":
            await _json(
                send,
                401,
                {"detail": "sign in required"},
                {"HX-Redirect": f"/login?next={quote(path, safe='')}"},
            )
            return
        response = RedirectResponse(f"/login?next={quote(nxt, safe='')}", status_code=303)
        await response(scope, _noreceive, send)

    async def _forbidden(self, scope: Scope, send: Send, is_api: bool, detail: str) -> None:
        if is_api:
            await _json(send, 403, {"detail": detail})
            return
        response = HTMLResponse(f"<h1>403</h1><p>{detail}</p>", status_code=403)
        await response(scope, _noreceive, send)

    def _same_origin(self, conn: HTTPConnection) -> bool:
        """Origin (else Referer) must host-match Host or X-Forwarded-Host; absent: allow."""
        source = conn.headers.get("origin") or conn.headers.get("referer")
        if not source:
            return True
        host = urlsplit(source).netloc
        if not host:
            return False
        # A cross-site browser request cannot set X-Forwarded-Host, so trusting
        # it (the proxy's name for us) cannot weaken the check.
        forwarded = conn.headers.get("x-forwarded-host", "")
        allowed = {conn.headers.get("host", ""), forwarded.split(",")[0].strip()}
        return host in allowed

    async def _stamp(self, scope: Scope, receive: Receive, send: Send, name: str) -> Receive | None:
        """Buffer the body (≤1 MiB), stamp `operator` = `name`, replay it; None: answered.

        Fail-closed: only a JSON object (any media type FastAPI would read as JSON,
        including a missing content-type) or a url-encoded form passes — anything
        else (multipart, text, ...) is a 415, so no parser downstream can read a
        spoofed `operator` the middleware never rewrote.
        """
        body = b""
        more = True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                return None  # the client disconnected; there is nobody left to answer
            body += message.get("body", b"")
            more = bool(message.get("more_body", False))
            if len(body) > MAX_BODY:
                await _json(send, 413, {"detail": "body too large"})
                return None
        content_types = [v for k, v in conn_headers(scope) if k == b"content-type"]
        if len(content_types) > 1:
            await _json(send, 400, {"detail": "one content-type header expected"})
            return None
        if body:
            content_type = content_types[0].decode("latin-1") if content_types else ""
            try:
                body, stamped_type = _stamped_body(body, content_type, name)
            except _StampError as refusal:
                await _json(send, refusal.status, {"detail": refusal.detail})
                return None
            # Re-set the type too: a `+json`/parameter variant must not diverge
            # downstream from what the stamper just rewrote.
            _set_header(scope, b"content-type", stamped_type.encode())
            _set_content_length(scope, len(body))
        sent = False

        async def replay() -> Message:
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        return replay


def conn_headers(scope: Scope) -> list[tuple[bytes, bytes]]:
    """The raw ASGI header list of `scope`."""
    return scope["headers"]  # type: ignore[no-any-return]


def _set_header(scope: Scope, key: bytes, value: bytes) -> None:
    """`key` = `value` as the scope's single header of that name."""
    headers = [(k, v) for k, v in conn_headers(scope) if k != key]
    headers.append((key, value))
    scope["headers"] = headers


def _set_content_length(scope: Scope, length: int) -> None:
    _set_header(scope, b"content-length", str(length).encode())


class _StampError(Exception):
    """A body the stamper will not let through, answered with `status`/`detail`."""

    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail


def _media(content_type: str) -> email.message.Message:
    """The content-type parsed the way FastAPI parses it (email.message)."""
    message = email.message.Message()
    message["content-type"] = content_type
    return message


def _is_json_type(content_type: str) -> bool:
    """Whether FastAPI would read this body as JSON: blank, `*/json` or `*+json`."""
    if not content_type.strip():
        return True
    subtype = _media(content_type).get_content_subtype()
    return subtype == "json" or subtype.endswith("+json")


def _is_form_type(content_type: str) -> bool:
    """Whether this is a url-encoded form (main/subtype, parameters ignored)."""
    return _media(content_type).get_content_type() == "application/x-www-form-urlencoded"


def _stamped_body(body: bytes, content_type: str, name: str) -> tuple[bytes, str]:
    """(`body`, content-type) with `operator` = `name`; `_StampError` when refused."""
    if _is_json_type(content_type):
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError) as e:
            raise _StampError(400, "body must be a JSON object") from e
        if not isinstance(data, dict):
            raise _StampError(400, "body must be a JSON object")
        data["operator"] = name
        return json.dumps(data).encode(), "application/json"
    if _is_form_type(content_type):
        # python-multipart splits fields on ';' as well as '&'; parse_qsl does
        # not. A raw ';' could hide a pair we never saw, so refuse it outright.
        if b";" in body:
            raise _StampError(400, "';' is not a form separator here; percent-encode it")
        pairs = [
            (k, v) for k, v in parse_qsl(body.decode(), keep_blank_values=True) if k != "operator"
        ]
        pairs.append(("operator", name))
        return urlencode(pairs).encode(), "application/x-www-form-urlencoded"
    raise _StampError(415, "send JSON or a url-encoded form")


# --- routes ------------------------------------------------------------------

router = APIRouter(tags=["auth"])


class TokenIn(BaseModel):
    """Credentials for the token endpoint."""

    name: str
    password: str


class TokenOut(BaseModel):
    """An issued bearer token."""

    access_token: str
    token_type: str = "bearer"  # noqa: S105 - an OAuth2 token-type literal, not a password
    expires_in: int


class Me(BaseModel):
    """Who is signed in (name/roles None and empty when anonymous or mode none)."""

    name: str | None
    roles: list[Role]
    mode: str


async def _password_user(settings: Settings, name: str, password: str) -> User | None:
    """users.authenticate off the event loop; shared by the token route and the login form."""

    def check() -> User | None:
        with db.connect(settings) as conn:
            return users.authenticate(conn, name, password)

    return await asyncio.to_thread(check)


def _jwt_backend(request: Request) -> JwtBackend:
    backend = request.app.state.auth_backend
    if not isinstance(backend, JwtBackend):
        raise HTTPException(404, "this app does not issue tokens (MAPPD_AUTH is not 'jwt')")
    return backend


@router.post("/api/v1/auth/token")
async def token(request: Request, credentials: TokenIn) -> TokenOut:
    """Exchange a name and password for a bearer token; 401 on bad credentials."""
    backend = _jwt_backend(request)
    settings: Settings = request.app.state.settings
    user = await _password_user(settings, credentials.name, credentials.password)
    if user is None:
        raise HTTPException(401, "invalid credentials")
    access_token, expires_in = backend.issue(user.name)
    return TokenOut(access_token=access_token, expires_in=expires_in)


@router.get("/api/v1/auth/me")
async def me(request: Request) -> Me:
    """The current principal; null name when anonymous or MAPPD_AUTH=none."""
    user = principal(request)
    return Me(
        name=user.name if user else None,
        roles=sorted(user.roles) if user else [],
        mode=auth_mode(request),
    )


def _next(url: str | None) -> str:
    """`url` only when it is a same-site path (never `//host`)."""
    if url and url.startswith("/") and not url.startswith("//"):
        return url
    return "/"


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    next_url: Annotated[str, Query(alias="next")] = "/",
    error: str = "",
) -> HTMLResponse:
    """The sign-in form (jwt), or an explanation that sign-in happens elsewhere."""
    context = {"mode": auth_mode(request), "next": _next(next_url), "error": error}
    return CORE.TemplateResponse(request, "login.html", context)


@router.post("/login")
async def login(request: Request) -> Response:
    """Check the password, set the session cookie and redirect to `next`."""
    backend = _jwt_backend(request)
    settings: Settings = request.app.state.settings
    data = await request.form()
    name = str(data.get("name", ""))
    nxt = _next(str(data.get("next", "/")))
    user = await _password_user(settings, name, str(data.get("password", "")))
    if user is None:
        context = {"mode": "jwt", "next": nxt, "error": "wrong name or password"}
        return CORE.TemplateResponse(request, "login.html", context, status_code=401)
    access_token, expires_in = backend.issue(user.name)
    response = RedirectResponse(nxt, status_code=303)
    response.set_cookie(
        COOKIE,
        access_token,
        max_age=expires_in,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.auth_cookie_secure,
    )
    return response


@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    """Drop the session cookie and send the browser to the sign-in page (or home)."""
    target = "/login" if auth_mode(request) == "jwt" else "/"
    response = RedirectResponse(target, status_code=303)
    response.delete_cookie(COOKIE, path="/")
    return response
