"""The app: processes registered explicitly in `processes/__init__.py`.

Run with: uvicorn --factory mappd.app:create_app
"""

from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from mappd import events, mail, runtime, web
from mappd.process import Process, load_processes
from mappd.settings import Settings
from mappd.web import api as web_api
from mappd.web import auth as web_auth
from mappd.web import errors as web_errors
from mappd.web import openapi as web_openapi
from mappd.web import ops_api, workflows_api, workflows_pages
from mappd.web import pages as web_pages

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


def discover(processes_dir: Path) -> list[str]:
    """Names of the explicitly registered processes under `processes_dir`, sorted."""
    return sorted(p.name for p in load_processes(processes_dir))


def load(settings: Settings) -> dict[str, Process]:
    """Load every registered process, keyed by process name."""
    return {p.name: p for p in load_processes(settings.processes_dir)}


def create_app(
    settings: Settings | None = None,
    processes: Sequence[Process] | None = None,
) -> FastAPI:
    """Build the FastAPI app with every registered process; DBOS launches in the lifespan."""
    settings = settings or Settings.from_env()
    registered = (
        list(processes) if processes is not None else load_processes(settings.processes_dir)
    )
    schedules = [
        *(s for p in registered for s in p.schedules if s.when is None or s.when()),
        *mail.register(registered),
        *events.register(registered),
    ]
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=runtime.lifespan(settings, schedules=schedules),
        # DBOS workflows are the root spans of our traces; HTTP request spans would
        # sit above them with no inputs/outputs and leave GET noise in MLflow.
        telemetry={"tracing": False},
    )

    @app.get("/health")
    def health() -> dict[str, object]:
        return {"ok": True, "version": settings.app_version}

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> RedirectResponse:
        return RedirectResponse("/static/favicon.svg", status_code=308)

    # Fail fast on a misconfigured MAPPD_AUTH (bad secret, unimportable backend).
    backend = web_auth.backend_from(settings)
    app.state.settings = settings
    app.state.auth_mode = settings.auth
    app.state.auth_backend = backend
    app.state.processes = [p.name for p in registered]
    app.state.dashboards = {p.name: p.dashboard for p in registered if p.dashboard}
    app.state.cases = [(p.name, p.cases) for p in registered if p.cases is not None]
    web_errors.install(app)
    app.mount("/static", StaticFiles(directory=web.STATIC), name="static")
    app.include_router(web_auth.router)
    app.include_router(web_api.router)
    app.include_router(workflows_api.router)
    app.include_router(ops_api.router)
    app.include_router(web_pages.router)
    app.include_router(workflows_pages.router)
    for proc in registered:
        events.log_unconfigured(proc.name, proc.webhooks)
        proc.policy()  # a malformed autonomy.yaml stops the app here, naming the field
        if proc.router is not None:
            app.include_router(proc.router)
        if proc.pages is not None:
            app.include_router(proc.pages)
    app.add_middleware(
        web_auth.AuthMiddleware, backend=backend, mode=settings.auth, processes=registered
    )
    web_openapi.install(app)
    return app
