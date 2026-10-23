"""DBOS step names come from the code, not from string literals.

DBOS registers steps by name and replays journals by it, so a name is a durable contract:
"<process>.<function>" (AGENTS.md, rule 4). Both parts are already in the code — the
function's module sits in processes/<process>/ — so the decorators that take a name
(`transaction`, `action`) derive it and keep the explicit argument only as the escape
hatch for a deliberate rename that must keep journal compatibility.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

# A process module is at least <package>.<process>.<module>: the second part is the process.
_DEEPEST_MODULE = 3


def step_name(func: Callable[..., object]) -> str:
    """Derive "<process>.<function>" for `func` from its module's package.

    A function in processes/<process>/<module>.py gets the name "<process>.<function>";
    a deliberate rename keeps the old name by passing it to the decorator explicitly.
    """
    parts = getattr(func, "__module__", "").split(".")
    if len(parts) >= _DEEPEST_MODULE and all(part.isidentifier() for part in parts):
        return f"{parts[1]}.{func.__name__}"
    raise TypeError(
        f"{func.__qualname__}: the step name cannot be derived from module "
        f"{func.__module__!r} — pass it to the decorator explicitly"
    )


def agent_name(module: str) -> str:
    """Derive "<process>.<name>" for an object declared in `module` (an agent).

    A module processes/<process>/.../<name>.py yields "<process>.<name>":
    processes/support/agents/classifier/ declares the agent "support.classifier".
    """
    parts = module.split(".")
    if len(parts) >= _DEEPEST_MODULE and all(part.isidentifier() for part in parts):
        return f"{parts[1]}.{parts[-1]}"
    raise TypeError(f"the agent name cannot be derived from module {module!r} — pass it as `name=`")
