"""processes/<name>/evals/config.yaml: the metrics, their thresholds and tolerances."""

from typing import TYPE_CHECKING, Literal

import yaml
from pydantic import BaseModel, ValidationError, model_validator

if TYPE_CHECKING:
    from pathlib import Path

type Kind = Literal["exact_match", "set_f1", "judge", "rate", "python"]
DEFAULT_TOLERANCE = 0.03
# Validator messages that start with one of these name the field they are about.
FIELDS_NAMED_IN_MESSAGES = ("field", "equals", "python")


class EvalConfigError(ValueError):
    """config.yaml cannot be used; the message names the file and the field."""


class Metric(BaseModel):
    """One metric over the cases its `when` selects."""

    name: str
    kind: Kind
    field: str | None = None
    output: str | None = None
    when: dict[str, str | list[str]] = {}
    equals: str | None = None
    python: str | None = None
    threshold: float
    tolerance: float = DEFAULT_TOLERANCE
    lower_is_better: bool = False

    @model_validator(mode="after")
    def _consistent(self) -> Metric:
        if self.field is not None and not self.field.startswith("expected."):
            raise ValueError("field: must start with 'expected.'")
        if self.kind in {"exact_match", "set_f1"} and self.field is None:
            raise ValueError("field: required for exact_match and set_f1 metrics")
        if self.kind == "rate":
            if self.equals is None:
                raise ValueError("equals: a rate metric needs the output value it counts")
            if self.output is None and self.field is None:
                raise ValueError("field: a rate metric needs 'output' or 'field'")
        if self.kind == "python" and not (self.python and ":" in self.python):
            raise ValueError("python: a python metric needs 'module:function'")
        return self

    def output_path(self) -> str:
        """The dotted path into the process output."""
        if self.output:
            return self.output
        return (self.field or "").removeprefix("expected.")


class EvalConfig(BaseModel):
    """All metrics of a process."""

    metrics: list[Metric]

    @model_validator(mode="after")
    def _unique(self) -> EvalConfig:
        seen: set[str] = set()
        for metric in self.metrics:
            if metric.name in seen:
                raise ValueError(f"duplicate metric {metric.name}")
            seen.add(metric.name)
        return self


def _where(loc: tuple[int | str, ...], message: str) -> str:
    path = ""
    for part in loc:
        path += f"[{part}]" if isinstance(part, int) else (f".{part}" if path else str(part))
    named = message.split(":", 1)[0]
    if named in FIELDS_NAMED_IN_MESSAGES:
        path += f".{named}"
    return path


def load_config(path: Path) -> EvalConfig:
    """Read and validate a process's eval config.

    Raises:
        EvalConfigError: missing, not YAML, or invalid; names the file and the field.
    """
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as e:
        raise EvalConfigError(f"{path}: {e}") from None
    try:
        return EvalConfig.model_validate(data)
    except ValidationError as e:
        first = e.errors()[0]
        message = str(first["msg"]).removeprefix("Value error, ")
        raise EvalConfigError(
            f"{path}: {_where(tuple(first['loc']), message)}: {message}"
        ) from None
