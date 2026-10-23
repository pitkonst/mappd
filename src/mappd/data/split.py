"""Dev/holdout split: grouped by persona, so a holdout persona never appears in dev."""

import random
from collections import Counter, defaultdict
from typing import TYPE_CHECKING

from mappd.data.cases import Case, write_cases

if TYPE_CHECKING:
    from pathlib import Path


def _stratum(case: Case) -> tuple[str, str]:
    return str(case.expected.get("outcome")), str(case.meta.get("trap"))


def split(
    cases: list[Case], holdout: float, seed: int, canary: str
) -> tuple[list[Case], list[Case]]:
    """Whole personas go to the holdout until it holds `holdout` of the cases.

    Greedy: each step adds the persona that keeps the holdout's mix of outcomes and traps closest
    to the whole set's, so rare strata are not left to chance. Ties go to the seeded order.
    """
    groups: dict[str, list[Case]] = defaultdict(list)
    for case in cases:
        groups[str(case.meta.get("persona", case.id))].append(case)
    remaining = sorted(groups)
    random.Random(seed).shuffle(remaining)
    overall = Counter(_stratum(c) for c in cases)
    target = round(len(cases) * holdout)
    held_counts: Counter[tuple[str, str]] = Counter()
    held_size = 0
    chosen: list[str] = []

    def distance(counts: Counter[tuple[str, str]], size: int) -> float:
        return sum(abs(counts[s] / size - overall[s] / len(cases)) for s in overall)

    while held_size < target and remaining:
        best = min(
            remaining,
            key=lambda p: distance(
                held_counts + Counter(_stratum(c) for c in groups[p]), held_size + len(groups[p])
            ),
        )
        remaining.remove(best)
        chosen.append(best)
        held_counts.update(_stratum(c) for c in groups[best])
        held_size += len(groups[best])
    held = [
        c.model_copy(update={"meta": {**c.meta, "canary": canary}})
        for p in chosen
        for c in groups[p]
    ]
    dev = [c for p in remaining for c in groups[p]]
    return dev, held


def write_split(evals_dir: Path, dev: list[Case], holdout: list[Case]) -> tuple[Path, Path]:
    """dev.jsonl (committed) and holdout.jsonl (git-ignored)."""
    dev_path, holdout_path = evals_dir / "dev.jsonl", evals_dir / "holdout.jsonl"
    write_cases(dev_path, dev)
    write_cases(holdout_path, holdout)
    return dev_path, holdout_path
