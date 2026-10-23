"""Feedback: operator decisions become eval cases.

`mappd feedback sync <name>` asks the process for its decided records, maps each to a Case,
and merges them by id into processes/<name>/evals/feedback.jsonl — git-ignored, because the
text is real customers' (AGENTS.md: synthetic data only in tracked files). Every case carries
the process canary, so a case copied into a tracked file fails `mappd data check-canary`.
"""

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from mappd.data.canary import ensure
from mappd.data.cases import Case, read_cases, write_cases

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from datetime import datetime

    from mappd.process import Process

FILE = "feedback.jsonl"


@dataclass(frozen=True)
class FeedbackSpec:
    """How a process turns its decided records into eval cases."""

    collect: Callable[[datetime], Awaitable[list[Any]]]
    to_case: Callable[[Any], Case | None]


class SyncReport(BaseModel):
    """What a sync did."""

    new: int
    updated: int
    unchanged: int
    edited: int
    path: str


def merge(path: Path, cases: Sequence[Case]) -> SyncReport:
    """Merge `cases` into `path` by id: append new, replace changed, keep the rest."""
    existing = {c.id: c for c in read_cases(path)} if path.is_file() else {}
    new = updated = unchanged = 0
    for case in cases:
        before = existing.get(case.id)
        if before is None:
            new += 1
        elif before == case:
            unchanged += 1
        else:
            updated += 1
        existing[case.id] = case
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".feedback-", suffix=".tmp")
    os.close(fd)
    tmp = Path(name)
    try:
        write_cases(tmp, list(existing.values()))
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
    edited = sum(1 for c in cases if c.meta.get("edited"))
    return SyncReport(new=new, updated=updated, unchanged=unchanged, edited=edited, path=str(path))


async def sync(
    process: Process, evals_dir: Path, since: datetime, *, out: Path | None = None
) -> SyncReport:
    """Collect the process's decided records since `since` and merge them as cases.

    `out` defaults to `evals_dir/feedback.jsonl`; the canary always comes from `evals_dir`.

    Raises:
        ValueError: the process declares no feedback.
    """
    if process.feedback is None:
        raise ValueError(f"process {process.name!r} declares no feedback")
    canary = ensure(evals_dir)
    cases = []
    for record in await process.feedback.collect(since):
        case = process.feedback.to_case(record)
        if case is not None:
            cases.append(case.model_copy(update={"meta": {**case.meta, "canary": canary}}))
    return merge(out or evals_dir / FILE, cases)
