import sys
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from fastapi import APIRouter, HTTPException
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from mappd.app import create_app, discover
from mappd.autonomy import PolicyError
from mappd.process import Process, Schedule, get_process, load_processes, register_process
from mappd.settings import Settings

if TYPE_CHECKING:
    from pathlib import Path

API = """
from fastapi import APIRouter

router = APIRouter()


@router.get("/demo/ping")
def ping() -> dict[str, str]:
    return {"pong": "demo"}
"""


@pytest.fixture
def processes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A uniquely named processes package; sys.path is restored afterwards."""
    root = tmp_path / f"procs_{uuid4().hex}"
    root.mkdir()
    monkeypatch.setattr(sys, "path", list(sys.path))
    return root


def make(
    root: Path, name: str, *, workflow: str = "LOADED = True\n", api: str | None = None
) -> None:
    d = root / name
    d.mkdir()
    (d / "workflow.py").write_text(workflow)
    router_import = ""
    router_arg = ""
    if api is not None:
        (d / "api.py").write_text(api)
        router_import = "from .api import router\n"
        router_arg = ", router=router"
    (d / "__init__.py").write_text(
        "from mappd.process import Process\n"
        "from . import workflow\n"
        f"{router_import}\n"
        f"process = Process(name={name!r}{router_arg})\n"
    )
    register_process(root, name)


def test_discover_lists_explicitly_registered_processes(processes: Path) -> None:
    make(processes, "support")
    make(processes, "rfq")
    (processes / "unregistered").mkdir()
    (processes / "unregistered" / "workflow.py").write_text("LOADED = True\n")
    assert discover(processes) == ["rfq", "support"]
    assert get_process(processes, "support") is not None
    assert get_process(processes, "unregistered") is None


def test_discover_without_a_processes_dir_is_empty(tmp_path: Path) -> None:
    assert discover(tmp_path / "absent") == []
    assert load_processes(tmp_path / "absent") == []


def test_app_serves_health_and_process_routes(processes: Path) -> None:
    make(processes, "demo", api=API)
    make(processes, "quiet")
    client = TestClient(create_app(Settings(processes_dir=processes, app_version="a1b2c3d")))
    assert client.get("/health").json() == {"ok": True, "version": "a1b2c3d"}
    assert client.get("/demo/ping").json() == {"pong": "demo"}
    assert sys.modules[f"{processes.name}.quiet.workflow"].LOADED is True


def test_app_accepts_explicit_process_objects(tmp_path: Path) -> None:
    router = APIRouter()

    @router.get("/custom/ok")
    def ok() -> dict[str, bool]:
        return {"ok": True}

    proc = Process(name="custom", router=router)
    client = TestClient(create_app(Settings(processes_dir=tmp_path / "none"), processes=[proc]))
    assert client.get("/custom/ok").json() == {"ok": True}


def test_process_name_must_be_an_identifier() -> None:
    with pytest.raises(ValueError, match="my-process"):
        Process(name="my-process")


def test_process_router_must_be_an_api_router() -> None:
    with pytest.raises(TypeError, match="router"):
        Process(name="broken", router="not-a-router")  # type: ignore[arg-type]


def test_processes_registry_must_hold_process_instances(processes: Path) -> None:
    (processes / "__init__.py").write_text("PROCESSES = ('not-a-process',)\n")
    with pytest.raises(TypeError, match="PROCESSES"):
        load_processes(processes)


def test_duplicate_process_names_are_rejected(processes: Path) -> None:
    (processes / "__init__.py").write_text(
        "from mappd.process import Process\np = Process(name='dup')\nPROCESSES = (p, p)\n"
    )
    with pytest.raises(ValueError, match="dup"):
        load_processes(processes)


def test_process_prompts_digest_is_computed_from_process_modules(processes: Path) -> None:
    d = processes / "demo"
    d.mkdir()
    (d / "__init__.py").write_text(
        "from pydantic_ai import Agent\n"
        "from pydantic_ai.models.test import TestModel\n"
        "from mappd.process import Process\n"
        "a1 = Agent(TestModel(), name='demo.a', instructions='First instruction.')\n"
        "process = Process(name='demo')\n"
    )
    register_process(processes, "demo")
    proc = get_process(processes, "demo")
    assert proc is not None
    assert len(proc.prompts_sha(processes)) == 12


def test_fastapi_http_tracing_is_disabled_so_workflows_are_trace_roots(processes: Path) -> None:
    api = (
        API
        + '\n@router.post("/demo/act")\ndef act() -> dict[str, bool]:\n    return {"ok": True}\n'
    )
    make(processes, "demo", api=api)
    exporter = InMemorySpanExporter()
    provider = trace.get_tracer_provider()
    if isinstance(provider, trace.ProxyTracerProvider):
        sdk = TracerProvider()
        trace.set_tracer_provider(sdk)
        provider = sdk
    assert isinstance(provider, TracerProvider)
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    client = TestClient(create_app(Settings(processes_dir=processes)))
    exporter.clear()
    assert client.get("/health").status_code == 200
    assert client.get("/demo/ping").status_code == 200
    assert client.post("/demo/act").status_code == 200
    assert exporter.get_finished_spans() == ()


@pytest.mark.usefixtures("no_ops_db")
def test_app_serves_static_index_and_process_pages(tmp_path: Path) -> None:
    pages = APIRouter()

    @pages.get("/demo")
    async def demo() -> dict[str, bool]:
        return {"ok": True}

    settings = Settings(processes_dir=tmp_path)
    app = create_app(settings, processes=[Process(name="demo", pages=pages)])
    client = TestClient(app)  # no `with`: the lifespan (DBOS) does not start
    assert client.get("/demo").json() == {"ok": True}
    assert client.get("/static/htmx.min.js").status_code == 200
    index = client.get("/")
    assert index.status_code == 200
    assert '<a href="/demo">demo</a>' in index.text


def test_pages_must_be_a_router() -> None:
    with pytest.raises(TypeError, match="pages"):
        Process(name="demo", pages=object())  # type: ignore[arg-type]


def test_webhooks_default_to_none() -> None:
    assert Process(name="demo").webhooks == ()


def error_app(tmp_path: Path) -> TestClient:
    pages, api = APIRouter(), APIRouter(prefix="/api/v1/demo")

    @pages.get("/demo/missing")
    async def page_missing() -> None:
        raise HTTPException(404, "no ticket 'x'")

    @pages.get("/demo/db")
    async def page_db() -> None:
        raise psycopg.OperationalError("connection refused")

    @api.get("/missing")
    async def api_missing() -> None:
        raise HTTPException(404, "no ticket 'x'")

    @api.get("/db")
    async def api_db() -> None:
        raise psycopg.OperationalError("connection refused")

    proc = Process(name="demo", router=api, pages=pages)
    return TestClient(create_app(Settings(processes_dir=tmp_path), processes=[proc]))


def test_page_errors_render_the_error_page(tmp_path: Path) -> None:
    client = error_app(tmp_path)
    missing = client.get("/demo/missing")
    assert missing.status_code == 404
    assert missing.headers["content-type"].startswith("text/html")
    assert "no ticket &#39;x&#39;" in missing.text
    down = client.get("/demo/db")
    assert down.status_code == 503
    assert "database is unavailable" in down.text


def test_api_errors_stay_json(tmp_path: Path) -> None:
    client = error_app(tmp_path)
    assert client.get("/api/v1/demo/missing").json() == {"detail": "no ticket 'x'"}
    down = client.get("/api/v1/demo/db")
    assert down.status_code == 503
    assert down.json() == {"detail": "the database is unavailable"}


def test_process_policy_loads_its_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "autonomy.yaml"
    path.write_text("shadow: true\ncategories:\n  how_to: auto\n")
    proc = Process(name="demo", autonomy=path, categories=("how_to", "other"))
    assert proc.policy().shadow is True
    assert proc.policy().level("how_to") == "auto"
    assert Process(name="none").policy().shadow is False
    override = tmp_path / "override.yaml"
    override.write_text("shadow: false\n")
    monkeypatch.setenv("MAPPD_DEMO_AUTONOMY", str(override))
    assert proc.autonomy_path() == override
    assert proc.policy().shadow is False


def test_a_broken_policy_stops_the_app_at_startup(tmp_path: Path) -> None:
    bad = tmp_path / "autonomy.yaml"
    bad.write_text("shadow: maybe\n")
    with pytest.raises(PolicyError, match=r"autonomy\.yaml"):
        create_app(Settings(processes_dir=tmp_path), processes=[Process(name="demo", autonomy=bad)])


def test_create_app_collects_process_schedules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    async def tick(_at: object, _context: object) -> None:
        return None

    def lifespan(settings: Settings, **kwargs: object) -> object:
        seen.update(kwargs)
        return None

    monkeypatch.setattr("mappd.app.runtime.lifespan", lifespan)
    schedule = Schedule(name="demo.tick", workflow=tick, cron="0 * * * * *")
    skipped = Schedule(name="demo.off", workflow=tick, cron="0 * * * * *", when=lambda: False)
    create_app(
        Settings(processes_dir=tmp_path),
        processes=[Process(name="demo", schedules=(schedule, skipped))],
    )
    assert seen["schedules"] == [schedule]


def test_app_serves_the_workflows_api_with_its_settings(tmp_path: Path) -> None:
    settings = Settings(processes_dir=tmp_path, app_name="workflows-app")
    app = create_app(settings, processes=[])
    assert app.state.settings is settings
    paths = set(app.openapi()["paths"])
    assert {"/api/v1/workflows", "/api/v1/workflows/{workflow_id}"} <= paths


@pytest.mark.usefixtures("no_ops_db")
def test_app_defaults_to_open_auth_and_serves_auth_routes(tmp_path: Path) -> None:
    app = create_app(Settings(processes_dir=tmp_path), processes=[])
    assert app.state.auth_mode == "none"
    client = TestClient(app)
    assert client.get("/api/v1/auth/me").json() == {"name": None, "roles": [], "mode": "none"}
    assert client.get("/").status_code == 200, "mode none is a pass-through"


def test_app_fails_fast_on_a_bad_auth_setting(tmp_path: Path) -> None:
    from mappd.settings import SettingsError  # noqa: PLC0415

    with pytest.raises(SettingsError, match="MAPPD_AUTH_SECRET"):
        create_app(Settings(processes_dir=tmp_path, auth="jwt", auth_secret=""))
    with pytest.raises(SettingsError, match="MAPPD_AUTH"):
        create_app(Settings(processes_dir=tmp_path, auth="bogus-mode"))


def test_app_in_header_mode_enforces_sign_in(tmp_path: Path) -> None:
    app = create_app(Settings(processes_dir=tmp_path, auth="header"), processes=[])
    client = TestClient(app)
    assert client.get("/", follow_redirects=False).status_code == 303
    assert client.get("/api/v1/auth/me").status_code == 401
    assert (
        client.get("/api/v1/auth/me", headers={"X-Forwarded-User": "olga"}).json()["name"] == "olga"
    )


def test_the_favicon_is_served() -> None:
    from fastapi.testclient import TestClient  # noqa: PLC0415

    client = TestClient(create_app())
    icon = client.get("/favicon.ico")
    assert icon.status_code == 200
    assert icon.headers["content-type"].startswith("image/svg+xml")
