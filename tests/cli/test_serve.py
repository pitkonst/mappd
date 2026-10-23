import asyncio
import sys
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
import uvicorn
from typer.testing import CliRunner

from mappd.cli import app
from mappd.cli import serve as serve_mod
from mappd.cli.serve import _serve_all
from mappd.process import MockSpec, register_process

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


@pytest.fixture
def processes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Unique package name per test: imported mock modules stay in sys.modules.
    root = tmp_path / f"procs_{uuid4().hex}"
    monkeypatch.setenv("MAPPD_PROCESSES_DIR", str(root))
    monkeypatch.setattr(sys, "path", list(sys.path))
    return root


@pytest.fixture
def uvicorn_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append({"app": app, **kw}))
    return calls


def test_serve_runs_the_app_factory(uvicorn_calls: list[dict[str, Any]]) -> None:
    r = runner.invoke(app, ["serve", "--port", "8100"])
    assert r.exit_code == 0, r.stderr
    assert uvicorn_calls == [
        {
            "app": "mappd.app:create_app",
            "factory": True,
            "host": "127.0.0.1",
            "port": 8100,
            "reload": False,
        }
    ]


def test_mock_serves_a_process_mock(processes: Path, uvicorn_calls: list[dict[str, Any]]) -> None:
    demo = processes / "demo"
    demo.mkdir(parents=True)
    (demo / "__init__.py").write_text(
        "from fastapi import FastAPI\n"
        "from mappd.process import MockSpec, Process\n\n"
        "process = Process(\n"
        "    name='demo',\n"
        "    mocks={'webhook': MockSpec(app=FastAPI(), port=9200)},\n"
        ")\n"
    )
    register_process(processes, "demo")
    r = runner.invoke(app, ["mock", "webhook"])
    assert r.exit_code == 0, r.stderr
    call = uvicorn_calls[0]
    assert call["port"] == 9200
    assert getattr(call["app"], "title", None) == "FastAPI"


def test_mock_port_option_beats_the_module_default(
    processes: Path, uvicorn_calls: list[dict[str, Any]]
) -> None:
    demo = processes / "demo"
    demo.mkdir(parents=True)
    (demo / "__init__.py").write_text(
        "from fastapi import FastAPI\n"
        "from mappd.process import MockSpec, Process\n\n"
        "process = Process(\n"
        "    name='demo',\n"
        "    mocks={'llm': MockSpec(app=FastAPI(), port=9000)},\n"
        ")\n"
    )
    register_process(processes, "demo")
    r = runner.invoke(app, ["mock", "llm", "--port", "9999"])
    assert r.exit_code == 0, r.stderr
    assert uvicorn_calls[0]["port"] == 9999


def test_mock_without_port_or_default_exits_2(processes: Path) -> None:
    demo = processes / "demo"
    demo.mkdir(parents=True)
    (demo / "__init__.py").write_text(
        "from fastapi import FastAPI\n"
        "from mappd.process import MockSpec, Process\n\n"
        "process = Process(\n"
        "    name='demo',\n"
        "    mocks={'llm': MockSpec(app=FastAPI())},\n"
        ")\n"
    )
    register_process(processes, "demo")
    r = runner.invoke(app, ["mock", "llm"])
    assert r.exit_code == 2
    assert "--port" in r.stderr


def test_mock_unknown_exits_2(processes: Path) -> None:
    r = runner.invoke(app, ["mock", "ghost"])
    assert r.exit_code == 2
    assert "ghost" in r.stderr


def test_mock_spec_requires_a_fastapi_app() -> None:
    with pytest.raises(TypeError, match="FastAPI"):
        MockSpec(app="not-an-app")  # type: ignore[arg-type]


def test_mock_llm_routes_when_several_processes_register_one(
    processes: Path, uvicorn_calls: list[dict[str, Any]]
) -> None:
    for name, claims in (("one", "None"), ("two", "lambda body: 'two' in str(body)")):
        (processes / name).mkdir(parents=True)
        (processes / name / "__init__.py").write_text(
            "from fastapi import FastAPI\n"
            "from mappd.process import MockSpec, Process\n\n"
            f"process = Process(name='{name}',"
            f" mocks={{'llm': MockSpec(app=FastAPI(title='{name}'), port=9000,"
            f" claims={claims})}})\n"
        )
        register_process(processes, name)
    r = runner.invoke(app, ["mock", "llm"])
    assert r.exit_code == 0, r.stderr
    call = uvicorn_calls[0]
    assert call["port"] == 9000
    assert getattr(call["app"], "title", None) is None, "the router, not one process's mock"


def _write_process(root: Path, name: str, mocks: str) -> None:
    (root / name).mkdir(parents=True)
    (root / name / "__init__.py").write_text(
        "from fastapi import FastAPI\n"
        "from mappd.process import MockSpec, Process\n\n"
        f"process = Process(name='{name}', mocks={{{mocks}}})\n"
    )
    register_process(root, name)


def _two_processes(processes: Path) -> None:
    _write_process(
        processes,
        "one",
        "'llm': MockSpec(app=FastAPI(), port=9000), 'webhook': MockSpec(app=FastAPI(), port=9200)",
    )
    _write_process(
        processes,
        "two",
        "'llm': MockSpec(app=FastAPI(), port=9000,"
        " claims=lambda body: 'two' in str(body)),"
        " 'mcp': MockSpec(app=FastAPI(), port=9400)",
    )


@pytest.fixture
def served_all(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, int]]:
    calls: list[tuple[Any, int]] = []

    async def fake(mocks: list[tuple[Any, int]]) -> None:
        calls.extend(mocks)

    monkeypatch.setattr(serve_mod, "_serve_all", fake)
    return calls


def test_mock_all_serves_every_registered_mock_on_one_process(
    processes: Path, served_all: list[tuple[Any, int]]
) -> None:
    _two_processes(processes)
    r = runner.invoke(app, ["mock", "--all"])
    assert r.exit_code == 0, r.stderr
    assert [port for _, port in served_all] == [9000, 9200, 9400]
    llm_app = next(app for _, port in served_all if port == 9000)
    assert getattr(llm_app, "title", None) is None, "the router, not one process's mock"
    assert "mock llm on :9000" in r.stderr


def test_mock_several_names_serve_just_those(
    processes: Path, served_all: list[tuple[Any, int]]
) -> None:
    _two_processes(processes)
    r = runner.invoke(app, ["mock", "webhook", "mcp"])
    assert r.exit_code == 0, r.stderr
    assert [port for _, port in served_all] == [9200, 9400]


def test_mock_needs_a_name_or_all(processes: Path) -> None:
    _two_processes(processes)
    r = runner.invoke(app, ["mock"])
    assert r.exit_code == 2
    assert "--all" in r.stderr


def test_mock_names_and_all_conflict(processes: Path) -> None:
    _two_processes(processes)
    r = runner.invoke(app, ["mock", "llm", "--all"])
    assert r.exit_code == 2


def test_mock_port_option_applies_to_one_mock(processes: Path) -> None:
    _two_processes(processes)
    r = runner.invoke(app, ["mock", "llm", "mcp", "--port", "9999"])
    assert r.exit_code == 2
    assert "--port" in r.stderr


def test_mock_two_mocks_on_one_port_exits_2(processes: Path) -> None:
    _write_process(
        processes,
        "one",
        "'first': MockSpec(app=FastAPI(), port=9100), 'second': MockSpec(app=FastAPI(), port=9100)",
    )
    r = runner.invoke(app, ["mock", "--all"])
    assert r.exit_code == 2
    assert "first" in r.stderr
    assert "second" in r.stderr


def test_serve_all_stops_every_server_when_one_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    servers: list[Any] = []

    class FakeServer:
        def __init__(self, config: uvicorn.Config) -> None:
            self.should_exit = False
            self.quick = len(servers) == 0
            servers.append(self)

        async def serve(self) -> None:
            if self.quick:
                return
            while not self.should_exit:
                await asyncio.sleep(0.01)

    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    asyncio.run(_serve_all([(object(), 1), (object(), 2)]))
    assert [s.should_exit for s in servers] == [True, True]


def test_serve_all_reraises_a_failed_server_after_stopping_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    servers: list[Any] = []

    class FakeServer:
        def __init__(self, config: uvicorn.Config) -> None:
            self.should_exit = False
            self.broken = len(servers) == 0
            servers.append(self)

        async def serve(self) -> None:
            if self.broken:
                raise RuntimeError("bind failed")
            while not self.should_exit:
                await asyncio.sleep(0.01)

    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    with pytest.raises(RuntimeError, match="bind failed"):
        asyncio.run(_serve_all([(object(), 1), (object(), 2)]))
    assert all(s.should_exit for s in servers)
