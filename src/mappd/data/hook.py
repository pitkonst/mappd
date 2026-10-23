"""A process's synthetic-data specification (`SynthSpec`)."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from mappd.data.writer import SYSTEM
from mappd.process import get_process

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path
    from random import Random

    from pydantic import BaseModel

    from mappd.data.cases import CasePlan, Message, Slot


class HookError(ValueError):
    """The process has no usable SynthSpec."""


class Hook(Protocol):
    """Protocol satisfied by `SynthSpec`."""

    @property
    def MIX(self) -> Mapping[str, float]:  # noqa: N802
        """Outcome share targets."""
        ...

    @property
    def TRAPS(self) -> Mapping[str, str]:  # noqa: N802
        """Trap name to outcome mapping."""
        ...

    @property
    def TRAP_RATE(self) -> float:  # noqa: N802
        """Fraction of cases that carry a trap."""
        ...

    @property
    def ROUNDTRIP_FIELDS(self) -> tuple[str, ...]:  # noqa: N802
        """Fields checked on the round trip."""
        ...

    @property
    def STRICT_FIELDS(self) -> tuple[str, ...]:  # noqa: N802
        """Fields that must match for a case to be kept."""
        ...

    @property
    def CHECK_INSTRUCTIONS(self) -> str:  # noqa: N802
        """Instructions for the round-trip checker model."""
        ...

    def plan_case(self, slot: Slot, rng: Random) -> CasePlan:
        """The plan for one slot: facts for the writer, labels for the evals."""
        ...

    def to_input(self, plan: CasePlan, message: Message) -> dict[str, Any]:
        """The process input for a written case."""
        ...


@dataclass(frozen=True)
class SynthSpec:
    """Explicit synthetic-data specification for a process."""

    mix: Mapping[str, float]
    traps: Mapping[str, str]
    trap_rate: float
    roundtrip_fields: tuple[str, ...]
    strict_fields: tuple[str, ...]
    check_instructions: str
    plan_case: Callable[[Slot, Random], CasePlan]
    to_input: Callable[[CasePlan, Message], dict[str, Any]]
    writer_instructions: str = SYSTEM
    input_model: type[BaseModel] | None = None
    output_dir: Path | None = None

    @property
    def MIX(self) -> Mapping[str, float]:  # noqa: N802
        """Uppercase alias for `Hook` protocol compatibility."""
        return self.mix

    @property
    def TRAPS(self) -> Mapping[str, str]:  # noqa: N802
        """Uppercase alias for `Hook` protocol compatibility."""
        return self.traps

    @property
    def TRAP_RATE(self) -> float:  # noqa: N802
        """Uppercase alias for `Hook` protocol compatibility."""
        return self.trap_rate

    @property
    def ROUNDTRIP_FIELDS(self) -> tuple[str, ...]:  # noqa: N802
        """Uppercase alias for `Hook` protocol compatibility."""
        return self.roundtrip_fields

    @property
    def STRICT_FIELDS(self) -> tuple[str, ...]:  # noqa: N802
        """Uppercase alias for `Hook` protocol compatibility."""
        return self.strict_fields

    @property
    def CHECK_INSTRUCTIONS(self) -> str:  # noqa: N802
        """Uppercase alias for `Hook` protocol compatibility."""
        return self.check_instructions

    @property
    def INPUT_MODEL(self) -> type[BaseModel] | None:  # noqa: N802
        """Uppercase alias for `Hook` protocol compatibility."""
        return self.input_model


def load_hook(processes_dir: Path, name: str) -> SynthSpec:
    """Return the `SynthSpec` registered on process `name`."""
    proc = get_process(processes_dir, name)
    if proc is None:
        raise HookError(f"no process {name!r} in {processes_dir}")
    if proc.synth is None:
        raise HookError(f"process {name} has no synth configured")
    return proc.synth
