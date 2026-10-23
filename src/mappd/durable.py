"""The four durable verbs on one surface: `workflow`, `step`, `transaction`, `action`.

`transaction` (`mappd.db.transaction`) and `action` (`mappd.actions`) are re-exported
here so a process needs only `from mappd import durable`.

Each is a named durable boundary (AGENTS.md, rule 4): the name comes from the code —
"<process>.<function>" via `mappd.naming.step_name` — not from a string literal that can
drift or be typoed. `name=` pins a name for journal compatibility across a deliberate
rename; every other option passes through to the DBOS decorators unchanged.

`step`, `transaction` and `action` on a sync function return a coroutine: the body runs
on a worker thread, so awaiting a durable step never blocks the event loop and callers
never reach for `asyncio.to_thread`. Steps declared `async def` pass through unchanged.

`agent` builds a Pydantic AI agent with DBOS durability always on — every model call
`agent.run` makes is a journaled step, so nothing on this surface can run unjournaled.

Action idempotency keys scale with intent: `AUTO` deduplicates a step inside its
workflow, `per(entity)` — or `@action(per="param")`, which takes the entity from a named
argument — deduplicates one entity across workflows, a plain string is a business key.
"""

import asyncio
import inspect
from functools import wraps
from typing import TYPE_CHECKING, Any, cast, overload

from dbos import DBOS
from pydantic_ai import Agent
from pydantic_ai.durable_exec.dbos import DBOSDurability

from mappd.actions import AUTO, action, per
from mappd.db.transaction import transaction
from mappd.naming import agent_name, step_name

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Sequence

    from pydantic_ai.capabilities import AgentCapability
    from pydantic_ai.models import Model

__all__ = ["AUTO", "action", "agent", "per", "step", "transaction", "workflow"]


def agent[OutT](
    model: Model | str | None = None,
    /,
    *,
    name: str | None = None,
    capabilities: Sequence[AgentCapability[Any]] | None = None,
    output_type: type[OutT] | None = None,
    **options: Any,
) -> Agent[Any, OutT]:
    """A Pydantic AI agent with `DBOSDurability` always on.

    Every model request `agent.run` makes is a journaled DBOS step, which is why the
    callsite helper that runs agents stays a plain function. The agent is named
    "<process>.<module>" — derived from the caller's module — unless `name=` pins it.
    A `DBOSDurability` in `capabilities` is kept; every other option passes through.
    """
    if name is None:
        frame = inspect.currentframe()
        caller = frame.f_back if frame is not None else None
        module = caller.f_globals.get("__name__", "") if caller is not None else ""
        name = agent_name(module)
    caps: list[AgentCapability[Any]] = [
        c for c in capabilities or [] if not isinstance(c, DBOSDurability)
    ]
    caps.append(DBOSDurability())
    if output_type is not None:
        options["output_type"] = output_type
    return Agent(model, name=name, capabilities=caps, **options)


@overload
def workflow[**P, R](func: Callable[P, R], /) -> Callable[P, R]: ...


@overload
def workflow[**P, R](
    func: None = None, /, *, name: str | None = None, **options: Any
) -> Callable[[Callable[P, R]], Callable[P, R]]: ...


def workflow[**P, R](
    func: Callable[P, R] | None = None, /, *, name: str | None = None, **options: Any
) -> Callable[P, R] | Callable[[Callable[P, R]], Callable[P, R]]:
    """A DBOS workflow named "<process>.<function>" unless `name=` pins it.

    Workflows are `async def` (AGENTS.md, rule 5): every durable step is awaitable,
    so a sync workflow could not call one.
    """

    def decorate(fn: Callable[P, R]) -> Callable[P, R]:
        if not inspect.iscoroutinefunction(fn):
            raise TypeError(f"{fn.__qualname__}: a durable workflow must be `async def`")
        return DBOS.workflow(name=name or step_name(fn), **options)(cast("Callable[P, R]", fn))

    return decorate(func) if func is not None else decorate


@overload
def step[**P, R](
    func: Callable[P, Coroutine[Any, Any, R]], /
) -> Callable[P, Coroutine[Any, Any, R]]: ...


@overload
def step[**P, R](func: Callable[P, R], /) -> Callable[P, Coroutine[Any, Any, R]]: ...


@overload
def step[**P, R](
    func: None = None, /, *, name: str | None = None, **options: Any
) -> Callable[[Callable[P, R]], Callable[P, Coroutine[Any, Any, R]]]: ...


def step[**P, R](
    func: Callable[P, R] | None = None, /, *, name: str | None = None, **options: Any
) -> (
    Callable[P, Coroutine[Any, Any, R]]
    | Callable[[Callable[P, R]], Callable[P, Coroutine[Any, Any, R]]]
):
    """A DBOS step named "<process>.<function>" unless `name=` pins it.

    A sync function becomes an awaitable step whose body runs on a worker thread;
    an `async def` function passes through.
    """

    def decorate(fn: Callable[P, R]) -> Callable[P, Coroutine[Any, Any, R]]:
        if inspect.iscoroutinefunction(fn):
            return DBOS.step(name=name or step_name(fn), **options)(fn)

        @wraps(fn)
        async def offloaded(*args: P.args, **kwargs: P.kwargs) -> R:
            return await asyncio.to_thread(fn, *args, **kwargs)

        return DBOS.step(name=name or step_name(fn), **options)(offloaded)

    return decorate(func) if func is not None else decorate
