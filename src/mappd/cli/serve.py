"""mappd serve / mappd mock: run the app and the process mocks on the host.

Development needs the containers only for the stack (postgres, mlflow,
agentgateway); the app and its mocks run here, against the same .env.
"""

import asyncio
import inspect
from typing import TYPE_CHECKING, Annotated, Any

import typer
import uvicorn

from mappd.cli._common import EXIT_USAGE, fail, load_settings
from mappd.mocks import llm_router
from mappd.process import get_process, load_processes

if TYPE_CHECKING:
    from mappd.settings import Settings


def serve(
    host: Annotated[str, typer.Option("--host", help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Bind port.")] = 8000,
    reload: Annotated[
        bool, typer.Option("--reload", help="Restart on code changes (dev only).")
    ] = False,
) -> None:
    """Serve the FastAPI app locally (the compose `app` service without docker).

    Example:
        mappd serve --port 8000 --reload
    """
    uvicorn.run("mappd.app:create_app", factory=True, host=host, port=port, reload=reload)


def _resolve(settings: Settings, name: str) -> tuple[Any, int | None]:
    """Resolve mock `name` to its ASGI app and default port.

    Several processes registering a mock of the same name (`llm`) are served by one router
    that sends each request to the mock whose `claims` accepts it.
    """
    key = name.replace("-", "_")
    specs = [
        proc.mocks[k]
        for proc in load_processes(settings.processes_dir)
        for k in dict.fromkeys((name, key))
        if k in proc.mocks
    ]
    if not specs:
        fail(f"no mock {name!r} registered in {settings.processes_dir}", EXIT_USAGE)
    served = llm_router.build(specs) if len(specs) > 1 else specs[0].app
    return served, specs[0].port


# Mocks listen on every interface, unlike the app (127.0.0.1): the gateway container reaches
# them through host.docker.internal, which is not the host's loopback. They hold synthetic
# data only.
MOCK_HOST = "0.0.0.0"  # noqa: S104 - see above


async def _serve_all(mocks: list[tuple[Any, int]]) -> None:
    """Serve every (app, port) pair; one server stopping (or Ctrl-C) stops them all."""
    servers = [
        uvicorn.Server(uvicorn.Config(app, host=MOCK_HOST, port=port)) for app, port in mocks
    ]
    tasks = [asyncio.create_task(server.serve()) for server in servers]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for server in servers:
        server.should_exit = True
    if pending:
        await asyncio.wait(pending)
    for task in done | pending:
        exc = task.exception()
        if exc is not None:
            raise exc


def mock(
    names: Annotated[
        list[str] | None, typer.Argument(help="Mocks to serve, registered on processes.")
    ] = None,
    all_: Annotated[
        bool, typer.Option("--all", help="Serve every mock the processes register.")
    ] = False,
    port: Annotated[
        int | None, typer.Option("--port", help="Bind port (default: the mock's port).")
    ] = None,
) -> None:
    """Serve process mocks locally: several names, or every registered mock with `--all`.

    A process registers its mocks on `Process(mocks={...})` as `MockSpec(app=..., port=...)`.
    When several processes register a mock of the same name (`llm`), one router serves them
    all and sends each request to the mock whose `claims` accepts it. With several mocks each
    keeps its own port in the same process; Ctrl-C stops them all.

    Example:
        mappd mock --all            # every registered mock on its own port
        mappd mock llm mcp          # the llm router and mock-mcp
        mappd mock llm --port 19000  # mock-llm on a custom port
    """
    settings = load_settings()
    if names and all_:
        fail("pass mock names or --all, not both", EXIT_USAGE)
    if all_:
        names = list(
            dict.fromkeys(k for proc in load_processes(settings.processes_dir) for k in proc.mocks)
        )
    if not names:
        fail("name a mock or pass --all", EXIT_USAGE)
    if port is not None and len(names) > 1:
        fail("--port applies to one mock", EXIT_USAGE)
    resolved = {name: _resolve(settings, name) for name in names}
    if len(names) == 1:
        served, default = resolved[names[0]]
        bind = port or default
        if bind is None:
            fail(f"mock {names[0]!r} sets no port; pass --port", EXIT_USAGE)
        uvicorn.run(served, host=MOCK_HOST, port=int(bind))
        return
    mocks = []
    by_port: dict[int, str] = {}
    for name in names:
        served, bind = resolved[name]
        if bind is None:
            fail(f"mock {name!r} sets no port; serve it alone with --port", EXIT_USAGE)
        if bind in by_port:
            fail(f"mocks {by_port[bind]} and {name} both use port {bind}", EXIT_USAGE)
        by_port[bind] = name
        mocks.append((served, bind))
    for name, (_, bind) in zip(names, mocks, strict=True):
        typer.echo(f"mock {name} on :{bind}", err=True)
    asyncio.run(_serve_all(mocks))


async def _maybe_await(result: Any) -> Any:
    """Await `result` when the seed entry point is async."""
    if inspect.isawaitable(result):
        return await result
    return result


def run_seed(settings: Settings, name: str) -> int:
    """Run process `name`'s registered `seed` callable and return its count."""
    proc = get_process(settings.processes_dir, name)
    if proc is None:
        fail(f"no process {name!r} in {settings.processes_dir}", EXIT_USAGE)
    if not callable(proc.seed):
        fail(f"process {name!r} has no seed function configured", EXIT_USAGE)
    result = asyncio.run(_maybe_await(proc.seed()))
    if not isinstance(result, int):
        fail(f"process {name!r} seed() must return the number of seeded rows", EXIT_USAGE)
    return result
