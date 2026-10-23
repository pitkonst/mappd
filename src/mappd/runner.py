"""One run of a process from the command line, inside its own trace (`mappd process run`).

A process registers `ingest` (its intake callable, typed with a Pydantic input model) on `Process`.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from dbos import DBOS, SetWorkflowID
from dbos._registrations import get_dbos_func_name
from opentelemetry import trace
from pydantic import BaseModel, ValidationError

from mappd import ids
from mappd.process import get_process

if TYPE_CHECKING:
    from mappd.settings import Settings

type Entry = Callable[[Any], Awaitable[Any]]


class RunError(ValueError):
    """The process or its input cannot be used."""


@dataclass(frozen=True)
class RunResult:
    """What one run produced."""

    workflow_id: str
    output: Any
    trace_id: str


def prepare(settings: Settings, name: str, data: object) -> tuple[Entry, BaseModel]:
    """Load process `name` and validate `data` against its `ingest` input model."""
    proc = get_process(settings.processes_dir, name)
    if proc is None:
        raise RunError(f"no process {name!r} in {settings.processes_dir}")
    ingest = proc.ingest
    model = proc.ingest_input_model()
    if not callable(ingest) or model is None:
        raise RunError(
            f"process {name!r} must define ingest (a callable typed with its Pydantic input model)"
        )
    try:
        case = model.model_validate(data)
    except ValidationError as e:
        raise RunError(f"input does not match {model.__name__}: {e}") from e
    return ingest, case


async def run_once(name: str, entry: Entry, case: BaseModel) -> RunResult:
    """Run the process's `ingest` inside a span named after the process and wait for completion."""
    tracer = trace.get_tracer("mappd.cli")
    with tracer.start_as_current_span(f"mappd process run {name}") as span:
        if get_dbos_func_name(entry) is not None:
            workflow_id = ids.from_key(f"{name}.run", uuid4())
            with SetWorkflowID(workflow_id):
                output = await entry(case)
        else:
            workflow_id = str(await entry(case))
            handle: Any = await DBOS.retrieve_workflow_async(workflow_id)
            output = await handle.get_result()
        trace_id = format(span.get_span_context().trace_id, "032x")
    return RunResult(workflow_id=workflow_id, output=output, trace_id=trace_id)
