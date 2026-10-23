"""mappd.durable: the DBOS decorators with names derived from the code."""

from typing import TYPE_CHECKING, Any

import pytest
from dbos._registrations import get_dbos_func_name
from pydantic_ai.capabilities import Caching
from pydantic_ai.durable_exec.dbos import DBOSDurability
from pydantic_ai.models.test import TestModel

from mappd import durable
from mappd.actions import action
from mappd.db.transaction import transaction

if TYPE_CHECKING:
    from collections.abc import Callable


def test_all_four_verbs_are_on_one_surface() -> None:
    assert durable.transaction is transaction
    assert durable.action is action


def _spoof[**P](func: Callable[P, Any], module: str) -> Callable[P, Any]:
    """A function claiming to live in `module`, as if it were part of a process."""
    func.__module__ = module
    return func


def test_workflow_derives_process_and_function() -> None:
    async def run_invoice(case: str) -> str:
        return case

    registered = durable.workflow(_spoof(run_invoice, "processes.billing.flow"))
    assert get_dbos_func_name(registered) == "billing.run_invoice"


def test_a_sync_workflow_is_rejected() -> None:
    def run_invoice(case: str) -> str:
        return case

    with pytest.raises(TypeError, match="async def"):
        durable.workflow(_spoof(run_invoice, "processes.billing.flow"))


def test_step_derives_process_and_function() -> None:
    def check() -> None:
        pass

    registered = durable.step(_spoof(check, "processes.billing.flow"))
    assert get_dbos_func_name(registered) == "billing.check"


def test_name_pins_it_for_journal_compatibility() -> None:
    async def new_name() -> None:
        pass

    registered = durable.workflow(name="billing.old_name")(
        _spoof(new_name, "processes.billing.flow")
    )
    assert get_dbos_func_name(registered) == "billing.old_name"


def test_options_pass_through_to_dbos() -> None:
    def retried() -> None:
        pass

    registered = durable.step(retries_allowed=True, max_attempts=9)(
        _spoof(retried, "processes.billing.flow")
    )
    assert get_dbos_func_name(registered) == "billing.retried"


def test_bare_use_outside_a_process_package_fails_loudly() -> None:
    async def homeless() -> None:
        pass

    with pytest.raises(TypeError, match="explicitly"):
        durable.workflow(_spoof(homeless, "scratchpad"))


def _agent_in(module: str, **options: Any) -> Any:
    """`durable.agent` as if called from `module` (the caller's `__name__`)."""
    return eval(  # noqa: S307 - a fixed string, only for the caller frame's globals
        "durable.agent(TestModel(), **options)",
        {"__name__": module, "durable": durable, "TestModel": TestModel, "options": options},
    )


def _capabilities(instance: Any) -> list[Any]:
    return list(instance._root_capability.capabilities)


def test_agent_is_durable_and_named_by_its_module() -> None:
    built = _agent_in("processes.support.agents.classifier")
    assert built.name == "support.classifier"
    assert sum(isinstance(c, DBOSDurability) for c in _capabilities(built)) == 1


def test_agent_pins_a_name_and_keeps_other_capabilities() -> None:
    built = _agent_in(
        "scratchpad",
        name="support.pinned",
        capabilities=[Caching(), DBOSDurability()],
    )
    assert built.name == "support.pinned"
    caps = _capabilities(built)
    assert sum(isinstance(c, Caching) for c in caps) == 1
    assert sum(isinstance(c, DBOSDurability) for c in caps) == 1


def test_agent_outside_a_process_package_needs_a_name() -> None:
    with pytest.raises(TypeError, match="name="):
        _agent_in("scratchpad")
