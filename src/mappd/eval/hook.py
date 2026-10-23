"""A process's evaluation specification (`EvalSpec`)."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from mappd.eval.judge import DEFAULT_CRITERIA
from mappd.process import get_process

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from pathlib import Path

    from mappd.data.cases import Case


class EvalHookError(ValueError):
    """The process has no usable EvalSpec."""


class EvalHook(Protocol):
    """Protocol satisfied by `EvalSpec` and test doubles."""

    @property
    def BASELINES(self) -> Mapping[str, Callable[[dict[str, Any]], dict[str, Any]]]:  # noqa: N802
        """Named baseline functions for `mappd eval baseline`."""
        ...

    def solve(self, case_input: dict[str, Any], /) -> Awaitable[dict[str, Any]]:
        """The process output for one case input."""
        ...

    def judge_context(self, case: Case, output: dict[str, Any], /) -> dict[str, str] | None:
        """What the judge sees for one case (or None when there is nothing to judge)."""
        ...


@dataclass(frozen=True)
class EvalSpec:
    """Explicit evaluation specification for a process."""

    solve: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
    judge_context: Callable[[Case, dict[str, Any]], dict[str, str] | None]
    baselines: Mapping[str, Callable[[dict[str, Any]], dict[str, Any]]] = field(
        default_factory=dict
    )
    judge_criteria: tuple[str, ...] = DEFAULT_CRITERIA
    config_path: Path | None = None
    rubric_path: Path | None = None
    dev_path: Path | None = None
    holdout_path: Path | None = None
    canary_path: Path | None = None

    @property
    def BASELINES(self) -> Mapping[str, Callable[[dict[str, Any]], dict[str, Any]]]:  # noqa: N802
        """Uppercase alias for `EvalHook` protocol compatibility."""
        return self.baselines


def load_eval_hook(processes_dir: Path, name: str) -> EvalSpec:
    """Return the `EvalSpec` registered on process `name`."""
    proc = get_process(processes_dir, name)
    if proc is None:
        raise EvalHookError(f"no process {name!r} in {processes_dir}")
    if proc.evals is None:
        raise EvalHookError(f"process {name} has no evals configured")
    return proc.evals
