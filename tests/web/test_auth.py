"""mappd.web.auth: the middleware policy, the backends and the auth routes.

Policy tests run on the header backend (no database). The jwt backend reads
`mappd_users` on every request, so its tests are pg.
"""

import sys
from collections.abc import AsyncIterable  # noqa: TC003 - FastAPI evaluates the hint
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import jwt
import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.testclient import TestClient
from starlette.requests import HTTPConnection

from mappd import users
from mappd.app import create_app
from mappd.db import connect
from mappd.process import Process
from mappd.settings import Settings, SettingsError
from mappd.web import auth
from mappd.web.auth import HeaderBackend, JwtBackend, Principal, backend_from

if TYPE_CHECKING:
    from pathlib import Path

SECRET = "s" * 40

demo = APIRouter()
install = APIRouter()


@demo.get("/api/v1/demo/items")
def items() -> dict[str, bool]:
    return {"ok": True}


@demo.post("/api/v1/demo/items")
async def make_item(request: Request) -> dict[str, object]:
    data: dict[str, object] = await request.json()
    return {**data, "content-type": request.headers.get("content-type", "")}


@demo.post("/demo/form")
async def form_post(request: Request) -> dict[str, object]:
    return {k: v for k, v in (await request.form()).multi_items() if isinstance(v, str)}


@demo.post("/demo/upload")
async def upload(request: Request) -> dict[str, object]:
    data = await request.form()
    out: dict[str, object] = {k: v for k, v in data.multi_items() if isinstance(v, str)}
    await data.close()
    return out


@demo.get("/api/v1/demo/stream", response_class=EventSourceResponse)
async def stream() -> AsyncIterable[ServerSentEvent]:
    yield ServerSentEvent(data={"n": 1})


@demo.post("/api/v1/demo/open")
def open_route() -> dict[str, bool]:
    return {"ok": True}


@demo.get("/demo")
def page() -> dict[str, bool]:
    return {"ok": True}


@install.post("/api/v1/install/act")
def act() -> dict[str, bool]:
    return {"ok": True}


DEMO = Process(name="demo", router=demo, role="operator", public=("/api/v1/demo/open",))
INSTALL = Process(name="install", router=install, role="dispatcher")


def app_for(tmp_path: Path, mode: str = "header", **kwargs: object) -> FastAPI:
    settings = Settings(processes_dir=tmp_path, auth=mode, **kwargs)  # type: ignore[arg-type]
    return create_app(settings, processes=[DEMO, INSTALL])


def user(name: str, *roles: str) -> dict[str, str]:
    return {"X-Forwarded-User": name, "X-Forwarded-Roles": ",".join(roles)}


def test_mode_none_passes_everything_and_stamps_nothing(tmp_path: Path) -> None:
    client = TestClient(app_for(tmp_path, "none"))
    assert client.get("/").status_code == 200
    assert client.get("/api/v1/auth/me").json() == {"name": None, "roles": [], "mode": "none"}
    r = client.post("/demo/form", data={"operator": "mallory"})
    assert r.status_code == 200
    assert r.json()["operator"] == "mallory", "mode none never rewrites a body"
    up = client.post("/demo/upload", files={"operator": (None, "mallory")})
    assert up.json()["operator"] == "mallory", "mode none never refuses a body type"


def test_anonymous_api_gets_401_with_a_bearer_challenge(tmp_path: Path) -> None:
    client = TestClient(app_for(tmp_path))
    r = client.get("/api/v1/demo/items")
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"] == "Bearer"
    assert r.json() == {"detail": "sign in required"}


def test_anonymous_page_redirects_to_login_with_next(tmp_path: Path) -> None:
    client = TestClient(app_for(tmp_path))
    r = client.get("/demo", params={"tab": "all"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=%2Fdemo%3Ftab%3Dall"


def test_anonymous_htmx_gets_401_and_a_redirect_header(tmp_path: Path) -> None:
    client = TestClient(app_for(tmp_path))
    r = client.get("/demo", headers={"HX-Request": "true"})
    assert r.status_code == 401
    assert r.headers["HX-Redirect"] == "/login?next=%2Fdemo"


def test_public_paths_stay_open(tmp_path: Path) -> None:
    client = TestClient(app_for(tmp_path))
    assert client.get("/health").status_code == 200
    assert client.post("/api/v1/demo/open").status_code == 200, "declared Process.public"
    assert client.get("/static/mappd.css").status_code == 200
    login = client.get("/login")
    assert login.status_code == 200
    assert "handled elsewhere" in login.text, "header mode explains sign-in is external"


def test_any_signed_in_principal_reads(tmp_path: Path) -> None:
    client = TestClient(app_for(tmp_path))
    assert client.get("/api/v1/demo/items", headers=user("olga", "dispatcher")).status_code == 200
    assert client.get("/demo", headers=user("olga")).status_code == 200, "no role still reads"
    assert client.get("/", headers=user("olga")).status_code == 200


def test_the_process_role_gates_mutations(tmp_path: Path) -> None:
    client = TestClient(app_for(tmp_path))
    ok = client.post("/api/v1/demo/items", json={"x": 1}, headers=user("olga", "operator"))
    assert ok.status_code == 200
    assert ok.json()["operator"] == "olga", "the middleware stamps the signed-in name"
    wrong = client.post("/api/v1/demo/items", json={"x": 1}, headers=user("dan", "dispatcher"))
    assert wrong.status_code == 403
    assert wrong.json() == {"detail": "requires role operator"}
    dispatcher = client.post("/api/v1/install/act", headers=user("dan", "dispatcher"))
    assert dispatcher.status_code == 200
    assert client.post("/api/v1/install/act", headers=user("olga", "operator")).status_code == 403


def test_admin_only_areas_and_admin_passing(tmp_path: Path) -> None:
    client = TestClient(app_for(tmp_path))
    api = client.get("/api/v1/workflows", headers=user("olga", "operator"))
    assert api.status_code == 403
    assert api.json() == {"detail": "requires role admin"}
    page = client.get("/actions", headers=user("olga", "operator"))
    assert page.status_code == 403
    assert page.headers["content-type"].startswith("text/html")
    for path in ("/ops", "/api/v1/ops"):
        assert client.get(path, headers=user("olga", "operator")).status_code == 403, path
    admin = user("root", "admin")
    assert client.post("/api/v1/demo/items", json={}, headers=admin).status_code == 200
    # admin reaches the route: any answer but 401/403 came from the handler itself
    assert client.get("/api/v1/workflows", headers=admin).status_code not in (401, 403)


def test_header_backend_drops_unknown_roles(tmp_path: Path) -> None:
    client = TestClient(app_for(tmp_path))
    r = client.get("/api/v1/auth/me", headers=user("olga", "operator,superuser, admin "))
    assert r.json()["roles"] == ["admin", "operator"]


def test_anonymous_cannot_mutate_even_public_shaped_paths(tmp_path: Path) -> None:
    client = TestClient(app_for(tmp_path))
    assert client.post("/api/v1/demo/items", json={}).status_code == 401


def test_stamping_overwrites_a_spoofed_operator_in_json(tmp_path: Path) -> None:
    client = TestClient(app_for(tmp_path))
    r = client.post(
        "/api/v1/demo/items", json={"operator": "mallory"}, headers=user("olga", "operator")
    )
    assert r.json()["operator"] == "olga"


def test_stamping_overwrites_a_spoofed_operator_in_a_form(tmp_path: Path) -> None:
    client = TestClient(app_for(tmp_path))
    r = client.post("/demo/form", data={"operator": "mallory"}, headers=user("olga", "operator"))
    assert r.json()["operator"] == "olga"


def test_stamping_adds_operator_when_missing(tmp_path: Path) -> None:
    client = TestClient(app_for(tmp_path))
    r = client.post("/demo/form", data={"note": "hi"}, headers=user("olga", "operator"))
    assert r.json() == {"note": "hi", "operator": "olga"}


def test_multipart_and_other_bodies_are_refused(tmp_path: Path) -> None:
    """Signed in, a body the stamper cannot rewrite never reaches the route (fail-closed)."""
    client = TestClient(app_for(tmp_path))
    r = client.post(
        "/demo/upload",
        files={"f": ("a.txt", b"data"), "operator": (None, "mallory")},
        headers=user("olga", "operator"),
    )
    assert r.status_code == 415
    assert r.json() == {"detail": "send JSON or a url-encoded form"}
    text = client.post(
        "/api/v1/demo/items",
        content=b"operator=victim",
        headers={**user("olga", "operator"), "content-type": "text/plain"},
    )
    assert text.status_code == 415


def test_json_family_types_are_stamped_and_normalized(tmp_path: Path) -> None:
    """FastAPI parses `*/json` and `*+json` as JSON, so the stamper must too."""
    client = TestClient(app_for(tmp_path))
    for ctype in ("application/vnd.review+json", "text/json"):
        r = client.post(
            "/api/v1/demo/items",
            content=b'{"operator": "mallory"}',
            headers={**user("olga", "operator"), "content-type": ctype},
        )
        assert r.status_code == 200, ctype
        assert r.json()["operator"] == "olga"
        assert r.json()["content-type"] == "application/json", ctype


def test_a_missing_content_type_is_json_and_stamped(tmp_path: Path) -> None:
    """FastAPI reads a bodyless content-type as JSON; the stamper must not pass it."""
    client = TestClient(app_for(tmp_path))
    r = client.post(
        "/api/v1/demo/items",
        content=b'{"operator": "mallory"}',
        headers=user("olga", "operator"),
    )
    assert r.status_code == 200
    assert r.json()["operator"] == "olga"


def test_a_json_family_body_that_is_no_object_is_400(tmp_path: Path) -> None:
    client = TestClient(app_for(tmp_path))
    for body in (b"[1, 2]", b"not json", b'"just a string"'):
        r = client.post(
            "/api/v1/demo/items",
            content=body,
            headers={**user("olga", "operator"), "content-type": "application/x.x+json"},
        )
        assert r.status_code == 400, body
        assert r.json() == {"detail": "body must be a JSON object"}


def test_a_semicolon_form_separator_is_refused(tmp_path: Path) -> None:
    """python-multipart splits fields on ';' too; a raw one could hide a pair."""
    client = TestClient(app_for(tmp_path))
    r = client.post(
        "/demo/form",
        content=b"note=x;operator=victim",
        headers={**user("olga", "operator"), "content-type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 400
    ok = client.post(
        "/demo/form",
        content=b"note=a%3Bb&operator=victim",
        headers={**user("olga", "operator"), "content-type": "application/x-www-form-urlencoded"},
    )
    assert ok.status_code == 200
    assert ok.json() == {"note": "a;b", "operator": "olga"}


def test_two_content_type_headers_are_400(tmp_path: Path) -> None:
    client = TestClient(app_for(tmp_path))
    r = client.post(
        "/api/v1/demo/items",
        content=b"{}",
        headers=[
            ("x-forwarded-user", "olga"),
            ("x-forwarded-roles", "operator"),
            ("content-type", "application/json"),
            ("content-type", "text/json"),
        ],
    )
    assert r.status_code == 400
    assert r.json() == {"detail": "one content-type header expected"}


def test_an_empty_body_needs_no_stamp(tmp_path: Path) -> None:
    client = TestClient(app_for(tmp_path))
    assert client.post("/api/v1/install/act", headers=user("dan", "dispatcher")).status_code == 200


def test_real_process_routes_never_see_a_spoofed_operator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two confirmed bypasses through the real routers: refuse or stamp to the signer."""
    from processes.installation import api as install_api  # noqa: PLC0415
    from processes.installation import service as install_service  # noqa: PLC0415
    from processes.support import api as support_api  # noqa: PLC0415
    from processes.support import service as support_service  # noqa: PLC0415

    released: list[tuple[str, str, int]] = []

    async def release(external_id: str, operator: str, version: int) -> object:
        released.append((external_id, operator, version))
        raise install_service.NotFoundError("no job")

    escalated: list[object] = []

    async def escalate(
        external_id: str, decision: object, *, idempotency_key: str | None = None
    ) -> object:
        escalated.append(decision)
        raise support_service.NotFoundError("no ticket")

    monkeypatch.setattr(install_service, "release", release)
    monkeypatch.setattr(support_service, "escalate", escalate)
    app = create_app(
        Settings(processes_dir=tmp_path, auth="header"),
        processes=[
            Process(name="support", router=support_api.router, role="operator"),
            Process(name="installation", router=install_api.router, role="dispatcher"),
        ],
    )
    client = TestClient(app)

    # Confirmed bypass 1: a `+json` media type reached installation's release un-stamped.
    r = client.post(
        "/api/v1/installation/jobs/j-1/release",
        content=b'{"operator": "victim", "version": 0}',
        headers={**user("dan", "dispatcher"), "content-type": "application/vnd.review+json"},
    )
    assert r.status_code == 404
    assert released == [("j-1", "dan", 0)], "release heard the signed-in name, never victim"

    # Confirmed bypass 2: multipart reached support's escalate un-stamped.
    r = client.post(
        "/api/v1/support/tickets/t-1/escalate",
        files={"operator": (None, "victim")},
        headers=user("olga", "operator"),
    )
    assert r.status_code == 415
    assert escalated == [], "a multipart escalate never reaches the service"

    # The same decision as plain JSON is stamped as usual.
    r = client.post(
        "/api/v1/support/tickets/t-1/escalate",
        json={"version": 0, "operator": "victim", "escalate_to": "billing"},
        headers=user("olga", "operator"),
    )
    assert r.status_code == 404
    assert escalated[0].operator == "olga"  # type: ignore[attr-defined]


def test_an_anonymous_telegram_update_is_refused_even_public(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Public skips sign-in, but the webhook still demands its own secret."""
    from processes.installation import api, intake  # noqa: PLC0415

    async def receive(update: object) -> None:
        return None

    monkeypatch.setattr(intake, "receive", receive)
    monkeypatch.setenv("MAPPD_INSTALLATION_TELEGRAM_SECRET", "the-secret")
    proc = Process(
        name="installation",
        router=api.router,
        public=("/api/v1/installation/inbound/telegram",),
    )
    client = TestClient(create_app(Settings(processes_dir=tmp_path, auth="header"), [proc]))
    update = {"update_id": 1, "message": {"chat": {"id": 2}, "text": "hi"}}
    url = "/api/v1/installation/inbound/telegram"
    assert client.post(url, json=update).status_code == 401
    bad = client.post(url, json=update, headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"})
    assert bad.status_code == 401
    ok = client.post(url, json=update, headers={"X-Telegram-Bot-Api-Secret-Token": "the-secret"})
    assert ok.status_code == 200


def test_a_body_over_1_mib_is_413(tmp_path: Path) -> None:
    client = TestClient(app_for(tmp_path))
    r = client.post(
        "/api/v1/demo/items",
        content=b" " * (1024 * 1024 + 1),
        headers={**user("olga", "operator"), "content-type": "application/json"},
    )
    assert r.status_code == 413


def test_sse_streams_through_the_middleware(tmp_path: Path) -> None:
    client = TestClient(app_for(tmp_path))
    r = client.get("/api/v1/demo/stream", headers=user("olga"))
    assert r.status_code == 200
    assert "data:" in r.text


def test_a_support_style_body_without_operator_still_validates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stamping adds a key TicketIn lacks; pydantic ignores it (AGENTS.md rule: no hack)."""
    from processes.support import api, service  # noqa: PLC0415

    async def ingested(ticket: object) -> str:
        return "wf-1"

    monkeypatch.setattr(service, "ingest", ingested)
    settings = Settings(processes_dir=tmp_path, auth="header")
    proc = Process(name="support", router=api.router, role="operator")
    client = TestClient(create_app(settings, processes=[proc]))
    assert client.post("/api/v1/support/tickets", json={}).status_code == 401
    r = client.post(
        "/api/v1/support/tickets",
        json={"external_id": "m-1", "sender": "a@b.c", "subject": "s", "body": "b"},
        headers=user("olga", "operator"),
    )
    assert r.status_code == 202, r.text


# --- CSRF: cookie credentials get an origin check, bearer does not -----------


class _CookieBackend:
    """A backend whose credentials always count as cookie-based."""

    def __init__(self, via_cookie: bool) -> None:
        self.via_cookie = via_cookie

    async def authenticate(self, conn: HTTPConnection) -> Principal | None:
        name = conn.headers.get("x-user")
        if not name:
            return None
        return Principal(name=name, roles=frozenset({"operator"}), via_cookie=self.via_cookie)


def _cookie_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, via_cookie: bool) -> TestClient:
    monkeypatch.setattr(auth, "backend_from", lambda _s: _CookieBackend(via_cookie))
    return TestClient(app_for(tmp_path, "jwt", auth_secret=SECRET))


def test_cookie_requests_with_a_foreign_origin_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _cookie_app(tmp_path, monkeypatch, via_cookie=True)
    r = client.post(
        "/demo/form",
        data={"note": "x"},
        headers={**user("olga"), "x-user": "olga", "Origin": "https://evil.example"},
    )
    assert r.status_code == 403
    assert r.json() == {"detail": "cross-site request refused"}


def test_cookie_requests_from_the_same_origin_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _cookie_app(tmp_path, monkeypatch, via_cookie=True)
    r = client.post(
        "/demo/form",
        data={"note": "x"},
        headers={"x-user": "olga", "Origin": "http://testserver"},
    )
    assert r.status_code == 200
    assert client.post("/demo/form", data={}, headers={"x-user": "olga"}).status_code == 200


def test_bearer_requests_skip_the_origin_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _cookie_app(tmp_path, monkeypatch, via_cookie=False)
    r = client.post(
        "/demo/form",
        data={"note": "x"},
        headers={"x-user": "olga", "Origin": "https://evil.example"},
    )
    assert r.status_code == 200


def test_header_principals_are_origin_checked_like_cookies(tmp_path: Path) -> None:
    """Behind an SSO proxy the browser holds the proxy's cookie: same check applies."""
    client = TestClient(app_for(tmp_path))
    foreign = client.post(
        "/api/v1/demo/items",
        json={},
        headers={**user("olga", "operator"), "Origin": "https://evil.example"},
    )
    assert foreign.status_code == 403
    assert foreign.json() == {"detail": "cross-site request refused"}
    same = client.post(
        "/api/v1/demo/items",
        json={},
        headers={**user("olga", "operator"), "Origin": "http://testserver"},
    )
    assert same.status_code == 200


def test_the_origin_check_honours_x_forwarded_host(tmp_path: Path) -> None:
    """Behind a proxy Host is internal; X-Forwarded-Host (first value) counts too."""
    client = TestClient(app_for(tmp_path))
    r = client.post(
        "/api/v1/demo/items",
        json={},
        headers={
            **user("olga", "operator"),
            "X-Forwarded-Host": "app.example.com, other.example.com",
            "Origin": "https://app.example.com",
        },
    )
    assert r.status_code == 200
    bad = client.post(
        "/api/v1/demo/items",
        json={},
        headers={
            **user("olga", "operator"),
            "X-Forwarded-Host": "app.example.com",
            "Origin": "https://elsewhere.example",
        },
    )
    assert bad.status_code == 403


def test_login_and_logout_posts_are_origin_checked(tmp_path: Path) -> None:
    """Public, but a cross-site POST to either is login-CSRF or a forced logout."""
    client = TestClient(app_for(tmp_path))
    for path in ("/login", "/logout"):
        r = client.post(path, data={}, headers={"Origin": "https://evil.example"})
        assert r.status_code == 403, path
        assert r.json() == {"detail": "cross-site request refused"}
    # No Origin at all still reaches the routes (header mode has no token issuer).
    assert client.post("/login", data={}).status_code == 404
    assert client.post("/logout", follow_redirects=False).status_code == 303


class _RecordingBackend:
    """Counts authenticate calls; always anonymous."""

    def __init__(self) -> None:
        self.calls = 0

    async def authenticate(self, conn: HTTPConnection) -> Principal | None:
        self.calls += 1
        return None


def test_static_assets_never_reach_the_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _RecordingBackend()
    monkeypatch.setattr(auth, "backend_from", lambda _s: backend)
    client = TestClient(app_for(tmp_path, "jwt", auth_secret=SECRET))
    assert client.get("/static/mappd.css").status_code == 200
    assert backend.calls == 0, "a DB lookup per asset is not acceptable"
    client.get("/api/v1/demo/items")
    assert backend.calls == 1


def test_a_disconnect_mid_body_stops_the_request(tmp_path: Path) -> None:
    """A non-http.request message while buffering means: gone; nobody to answer."""
    import asyncio  # noqa: PLC0415

    # These annotate the fake ASGI app: evaluated at def time, so a real import.
    from starlette.types import Message, Receive, Scope, Send  # noqa: PLC0415, TC002

    called = False

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal called
        called = True

    middleware = auth.AuthMiddleware(
        inner, backend=HeaderBackend(Settings()), mode="header", processes=[DEMO]
    )
    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/demo/items",
        "headers": [(b"x-forwarded-user", b"olga"), (b"x-forwarded-roles", b"operator")],
        "state": {},
    }

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    asyncio.run(middleware(scope, receive, send))
    assert not called
    assert sent == []


# --- construction ------------------------------------------------------------


def test_unknown_auth_modes_stop_the_app(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="MAPPD_AUTH"):
        backend_from(Settings(processes_dir=tmp_path, auth="kerberos"))


def test_jwt_needs_a_secret(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="MAPPD_AUTH_SECRET"):
        app_for(tmp_path, "jwt")
    with pytest.raises(SettingsError, match="MAPPD_AUTH_SECRET"):
        JwtBackend(Settings(processes_dir=tmp_path, auth="jwt", auth_secret="short"))  # noqa: S106


def test_a_custom_backend_import_string_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = tmp_path / "acme_auth.py"
    module.write_text(
        "from mappd.web.auth import HeaderBackend\n"
        "def backend(settings):\n"
        "    return HeaderBackend(settings)\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        client = TestClient(app_for(tmp_path, "acme_auth:backend"))
        assert client.get("/api/v1/demo/items").status_code == 401
        assert client.get("/api/v1/demo/items", headers=user("olga")).status_code == 200
    finally:
        sys.modules.pop("acme_auth", None)


def test_a_broken_import_string_fails_fast(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="MAPPD_AUTH"):
        app_for(tmp_path, "no_such_module_anywhere:backend")


def test_header_backend_parses_roles(tmp_path: Path) -> None:
    backend = HeaderBackend(Settings(auth_header="X-U", auth_roles_header="X-R"))
    conn = HTTPConnection(
        {"type": "http", "headers": [(b"x-u", b"olga"), (b"x-r", b"operator,bogus")]}
    )
    import asyncio  # noqa: PLC0415

    p = asyncio.run(backend.authenticate(conn))
    assert p is not None
    assert p.roles == {"operator"}


def test_principal_admin_implies_every_role() -> None:
    p = Principal(name="root", roles=frozenset({"admin"}))
    assert p.has("admin")
    assert p.has("operator")
    assert p.has("dispatcher")
    q = Principal(name="olga", roles=frozenset({"operator"}))
    assert q.has("operator")
    assert not q.has("admin")
    assert not q.has("dispatcher")


def test_the_token_route_only_exists_in_jwt_mode(tmp_path: Path) -> None:
    client = TestClient(app_for(tmp_path))
    r = client.post("/api/v1/auth/token", json={"name": "x", "password": "y"})
    assert r.status_code == 404


# --- jwt mode (reads mappd_users): pg -----------------------------------------


def jwt_client(settings: Settings) -> TestClient:
    app = create_app(settings, processes=[DEMO, INSTALL])
    return TestClient(app)


@pytest.fixture
def jwt_settings(migrated: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("MAPPD_DATABASE_URL", migrated.database_url)
    with connect(migrated) as conn:
        conn.execute("DELETE FROM mappd_users WHERE name LIKE 'jwt-%'")
        users.add(conn, "jwt-olga", "secret-123", ("operator",))
        users.add(conn, "jwt-dan", "secret-123", ("dispatcher",))
        users.add(conn, "jwt-root", "secret-123", ("admin",))
        users.add(conn, "jwt-off", "secret-123", ("admin",))
        users.set_disabled(conn, "jwt-off", True)
    return replace(migrated, auth="jwt", auth_secret=SECRET, auth_cookie_secure=True)


def _issue(settings: Settings, name: str) -> str:
    token, _ = JwtBackend(settings).issue(name)
    return token


def _expired(name: str) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {"sub": name, "iat": now - timedelta(hours=2), "exp": now - timedelta(hours=1)},
        SECRET,
        algorithm="HS256",
    )


def test_the_token_endpoint_issues_bearer_tokens(jwt_settings: Settings) -> None:
    client = jwt_client(jwt_settings)
    bad = client.post("/api/v1/auth/token", json={"name": "jwt-olga", "password": "nope-nope"})
    assert bad.status_code == 401
    ghost = client.post("/api/v1/auth/token", json={"name": "jwt-ghost", "password": "secret-123"})
    assert ghost.status_code == 401
    r = client.post("/api/v1/auth/token", json={"name": "jwt-olga", "password": "secret-123"})
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer"  # noqa: S105
    assert body["expires_in"] == 8 * 3600
    assert (
        client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
        ).json()["name"]
        == "jwt-olga"
    )


def test_bearer_tokens_work(jwt_settings: Settings) -> None:
    client = jwt_client(jwt_settings)
    headers = {"Authorization": f"Bearer {_issue(jwt_settings, 'jwt-olga')}"}
    assert client.get("/api/v1/demo/items", headers=headers).status_code == 200
    assert client.get("/api/v1/auth/me", headers=headers).json()["roles"] == ["operator"]


def test_cookies_work(jwt_settings: Settings) -> None:
    client = jwt_client(jwt_settings)
    cookie = {"cookie": f"{auth.COOKIE}={_issue(jwt_settings, 'jwt-olga')}"}
    assert client.get("/api/v1/demo/items", headers=cookie).status_code == 200
    foreign = client.post(
        "/api/v1/demo/items",
        json={},
        headers={**cookie, "Origin": "https://evil.example"},
    )
    assert foreign.status_code == 403, "a cookie credential is origin-checked"
    same = client.post(
        "/api/v1/demo/items",
        json={},
        headers={**cookie, "Origin": "http://testserver"},
    )
    assert same.status_code == 200


def test_expired_and_forged_tokens_are_rejected(jwt_settings: Settings) -> None:
    client = jwt_client(jwt_settings)
    expired = {"Authorization": f"Bearer {_expired('jwt-olga')}"}
    assert client.get("/api/v1/demo/items", headers=expired).status_code == 401
    forged = jwt.encode(
        {
            "sub": "jwt-olga",
            "iat": datetime.now(UTC),
            "exp": datetime.now(UTC) + timedelta(hours=1),
        },
        "another-secret-key-that-is-long-enough",
        algorithm="HS256",
    )
    assert (
        client.get("/api/v1/demo/items", headers={"Authorization": f"Bearer {forged}"}).status_code
        == 401
    )
    garbage = {"Authorization": "Bearer not-a-token"}
    assert client.get("/api/v1/demo/items", headers=garbage).status_code == 401


def test_disabled_and_unknown_users_cannot_use_tokens(jwt_settings: Settings) -> None:
    client = jwt_client(jwt_settings)
    off = {"Authorization": f"Bearer {_issue(jwt_settings, 'jwt-off')}"}
    assert client.get("/api/v1/demo/items", headers=off).status_code == 401
    ghost = {"Authorization": f"Bearer {_issue(jwt_settings, 'jwt-ghost')}"}
    assert client.get("/api/v1/demo/items", headers=ghost).status_code == 401


def test_roles_come_from_the_table_not_the_token(jwt_settings: Settings) -> None:
    """Disabling or promoting takes effect on the very next request."""
    client = jwt_client(jwt_settings)
    headers = {"Authorization": f"Bearer {_issue(jwt_settings, 'jwt-olga')}"}
    assert client.get("/api/v1/actions", headers=headers).status_code == 403
    with connect(jwt_settings) as conn:
        users.set_roles(conn, "jwt-olga", ("admin",))
        users.set_disabled(conn, "jwt-dan", True)
    assert client.get("/api/v1/actions", headers=headers).status_code == 200
    dan = {"Authorization": f"Bearer {_issue(jwt_settings, 'jwt-dan')}"}
    assert client.get("/api/v1/demo/items", headers=dan).status_code == 401


def test_login_sets_a_session_cookie_and_redirects(jwt_settings: Settings) -> None:
    client = jwt_client(jwt_settings)
    bad = client.post(
        "/login", data={"name": "jwt-olga", "password": "wrong"}, follow_redirects=False
    )
    assert bad.status_code == 401
    assert "wrong name or password" in bad.text
    r = client.post(
        "/login",
        data={"name": "jwt-olga", "password": "secret-123", "next": "/demo"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/demo"
    cookie = r.headers["set-cookie"]
    for flag in ("mappd_session=", "HttpOnly", "SameSite=lax", "Secure", "Max-Age=", "Path=/"):
        assert flag in cookie, flag


def test_login_rejects_an_off_site_next(jwt_settings: Settings) -> None:
    client = jwt_client(jwt_settings)
    for bad_next in ("https://evil.example", "//evil.example", "evil"):
        r = client.post(
            "/login",
            data={"name": "jwt-olga", "password": "secret-123", "next": bad_next},
            follow_redirects=False,
        )
        assert r.headers["location"] == "/", bad_next


def test_logout_clears_the_cookie(jwt_settings: Settings) -> None:
    client = jwt_client(jwt_settings)
    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    assert "mappd_session=" in r.headers["set-cookie"]


def test_support_ticket_accepts_the_stamped_operator(
    jwt_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TicketIn has no `operator`; stamping into its body is harmless (extra is ignored)."""
    from processes.support import api, service  # noqa: PLC0415

    seen: list[object] = []

    async def ingested(ticket: object) -> str:
        seen.append(ticket)
        return "wf-1"

    monkeypatch.setattr(service, "ingest", ingested)
    proc = Process(name="support", router=api.router, role="operator")
    app = create_app(jwt_settings, processes=[proc])
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {_issue(jwt_settings, 'jwt-olga')}"}
    r = client.post(
        "/api/v1/support/tickets",
        json={"external_id": "m-1", "sender": "a@b.c", "subject": "s", "body": "b"},
        headers=headers,
    )
    assert r.status_code == 202, r.text
