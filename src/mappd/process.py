"""Explicit process definition and registry for MAPP-D."""

import hashlib
import importlib
import inspect
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, get_type_hints

from fastapi import APIRouter, FastAPI
from pydantic import BaseModel
from pydantic_ai import Agent

from mappd.autonomy import Policy
from mappd.autonomy import load as load_policy
from mappd.settings import environment

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
    from datetime import datetime

    from mappd.channels.email import Email
    from mappd.data.hook import SynthSpec
    from mappd.eval.hook import EvalSpec
    from mappd.events import WebhookTarget
    from mappd.feedback import FeedbackSpec
    from mappd.ops import Cases
    from mappd.users import Role

_IMPORT_RE = re.compile(r"^from \.([a-zA-Z_][a-zA-Z0-9_]*) import process as \1$", re.MULTILINE)
_INIT_TEXT_BY_MODULE: dict[str, str] = {}


@dataclass(frozen=True)
class MockSpec:
    """A local development mock service exposed by a process."""

    app: FastAPI
    port: int | None = None
    # For an `llm` mock shared behind one port (mappd.mocks.llm_router): whether a request is
    # this process's. A request without a JSON body is offered as {"__path__": path}. None: the
    # fallback, which takes every request no other mock claims.
    claims: Callable[[dict[str, Any]], bool] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.app, FastAPI):
            raise TypeError("MockSpec.app must be a fastapi.FastAPI application")


@dataclass(frozen=True)
class Schedule:
    """A cron schedule the app applies at startup: DBOS runs `workflow(scheduled_at, context)`.

    `cron` has six fields (seconds first). Schedules are rows in the system database, created
    after launch, so a deploy updates them and a restart does not double them.
    """

    name: str
    workflow: Callable[[datetime, Any], Coroutine[Any, Any, None]]
    cron: str
    context: Any = None  # passed to the workflow on every tick
    when: Callable[[], bool] | None = None  # evaluated when the app starts; None means always on


@dataclass(frozen=True)
class Mailbox:
    """A mailbox a process reads.

    The app polls it when `MAPPD_<NAME>_MAIL_MODE=imap` and hands each new person's email to
    `receive`, which must be idempotent by Message-ID: a poll after a crash reads it again.
    A `ValueError` from `receive` rejects that one message; any other error retries the batch.
    """

    name: str
    receive: Callable[[Email], Awaitable[None]]

    def __post_init__(self) -> None:
        if not self.name.isidentifier():
            raise ValueError(f"mailbox name {self.name!r} is not a Python identifier")


_POLICIES: dict[Path, Policy] = {}


@dataclass(frozen=True)
class Process:
    """A self-contained business process built on MAPP-D."""

    name: str
    router: APIRouter | None = None
    pages: APIRouter | None = None
    webhooks: Sequence[WebhookTarget] = ()
    ingest: Callable[[Any], Awaitable[Any]] | None = None
    migrations_dir: Path | None = None
    seed: Callable[[], int | Awaitable[int]] | None = None
    seeds: str = "rows"  # what `seed` counts, as `mappd process seed` says it
    evals: EvalSpec | None = None
    synth: SynthSpec | None = None
    mocks: Mapping[str, MockSpec] = field(default_factory=dict)
    autonomy: Path | None = None
    categories: tuple[str, ...] = ()
    dashboard: str | None = None
    feedback: FeedbackSpec | None = None
    schedules: Sequence[Schedule] = ()
    mailboxes: Sequence[Mailbox] = ()
    # Where the cases live, for the Operations view (`/ops`): waiting and stuck cases.
    cases: Cases | None = None
    # Auth: the role that may send mutating requests to this process's
    # paths; `public` lists exact paths that skip sign-in (webhooks with their own secret).
    role: Role = "operator"
    public: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.isidentifier():
            raise ValueError(
                f"process name {self.name!r} is not a Python identifier; use snake_case"
            )
        if self.router is not None and not isinstance(self.router, APIRouter):
            raise TypeError(f"process {self.name!r}: router must be a fastapi.APIRouter")
        if self.pages is not None and not isinstance(self.pages, APIRouter):
            raise TypeError(f"process {self.name!r}: pages must be a fastapi.APIRouter")

    def autonomy_path(self) -> Path | None:
        """The process's autonomy policy path (`MAPPD_<NAME>_AUTONOMY` overrides `autonomy`)."""
        if self.autonomy is None:
            return None
        override = environment().get(f"MAPPD_{self.name.upper()}_AUTONOMY", "").strip()
        return Path(override) if override else self.autonomy

    def policy(self) -> Policy:
        """The process's autonomy policy, read once per file (a change ships with a deploy).

        Raises:
            PolicyError: the file is malformed.
        """
        path = self.autonomy_path()
        if path is None:
            return Policy()
        if path not in _POLICIES:
            _POLICIES[path] = load_policy(path)
        return _POLICIES[path]

    def ingest_input_model(self) -> type[BaseModel] | None:
        """Pydantic `BaseModel` type of the first parameter of `ingest`, if any."""
        if not callable(self.ingest):
            return None
        params = list(inspect.signature(self.ingest).parameters)
        if not params:
            return None
        hint = get_type_hints(self.ingest).get(params[0])
        if isinstance(hint, type) and issubclass(hint, BaseModel):
            return hint
        return None

    def prompts_sha(self, processes_dir: Path) -> str:
        """12-char SHA-256 digest of the instructions of all agents in this process's modules."""
        prefix = f"{processes_dir.resolve().name}.{self.name}"
        found: dict[int, Agent[Any, Any]] = {}
        for module_name, module in list(sys.modules.items()):
            if module_name == prefix or module_name.startswith(f"{prefix}."):
                for value in vars(module).values():
                    if isinstance(value, Agent):
                        found[id(value)] = value
        digest = hashlib.sha256()
        for agent in sorted(found.values(), key=lambda a: a.name or ""):
            digest.update((agent.name or "").encode("utf-8"))
            for item in getattr(agent, "_instructions", ()):
                text = getattr(item, "instruction", item)
                digest.update(str(text).encode("utf-8"))
        return digest.hexdigest()[:12]


def load_processes(processes_dir: Path) -> list[Process]:
    """Load the explicit `PROCESSES` sequence from `processes_dir/__init__.py`."""
    root = processes_dir.resolve()
    init_file = root / "__init__.py"
    if not init_file.is_file():
        return []
    if str(root.parent) not in sys.path:
        sys.path.insert(0, str(root.parent))
    text = init_file.read_text(encoding="utf-8")
    importlib.invalidate_caches()
    if root.name in sys.modules:
        if _INIT_TEXT_BY_MODULE.get(root.name) != text:
            module = importlib.reload(sys.modules[root.name])
            _INIT_TEXT_BY_MODULE[root.name] = text
        else:
            module = sys.modules[root.name]
    else:
        module = importlib.import_module(root.name)
        _INIT_TEXT_BY_MODULE[root.name] = text
    raw = getattr(module, "PROCESSES", ())
    if not isinstance(raw, (tuple, list)) or not all(isinstance(p, Process) for p in raw):
        raise TypeError(f"{module.__name__}.PROCESSES must be a tuple or list of mappd.Process")
    seen: set[str] = set()
    for proc in raw:
        if proc.name in seen:
            raise ValueError(f"duplicate process name {proc.name!r} in {module.__name__}.PROCESSES")
        seen.add(proc.name)
    return list(raw)


def get_process(processes_dir: Path, name: str) -> Process | None:
    """Return the registered `Process` named `name`, or `None` if not registered."""
    for proc in load_processes(processes_dir):
        if proc.name == name:
            return proc
    return None


def register_process(processes_dir: Path, name: str) -> Path:
    """Add `name` to `processes_dir/__init__.py`'s explicit `PROCESSES` registry."""
    processes_dir.mkdir(parents=True, exist_ok=True)
    init_file = processes_dir / "__init__.py"
    existing: set[str] = set()
    if init_file.is_file():
        existing.update(_IMPORT_RE.findall(init_file.read_text(encoding="utf-8")))
    existing.add(name)
    names = sorted(existing)
    imports = "\n".join(f"from .{n} import process as {n}" for n in names)
    items = f"({names[0]},)" if len(names) == 1 else f"({', '.join(names)})"
    content = (
        '"""Business processes, explicitly registered for the MAPP-D runtime and CLI."""\n\n'
        "from typing import TYPE_CHECKING\n\n"
        f"{imports}\n\n"
        "if TYPE_CHECKING:\n"
        "    from mappd.process import Process\n\n"
        f"PROCESSES: tuple[Process, ...] = {items}\n"
    )
    init_file.write_text(content, encoding="utf-8")
    return init_file
