"""The case mix: how many cases of each outcome, and which of them are traps."""

import math
from typing import TYPE_CHECKING

from mappd.data.cases import Slot

if TYPE_CHECKING:
    import random
    from collections.abc import Iterable, Mapping

TOLERANCE = 0.001


class MixError(ValueError):
    """A mix that cannot be used."""


def parse_mix(text: str, allowed: Iterable[str]) -> dict[str, float]:
    """`reply=.5,clarify=.2,escalate=.3` as shares that sum to 1."""
    allowed = tuple(allowed)
    mix: dict[str, float] = {}
    for part in text.split(","):
        name, sep, value = part.strip().partition("=")
        if not sep:
            raise MixError(f"expected name=share, got {part!r}")
        if name not in allowed:
            raise MixError(f"unknown outcome {name!r}; allowed: {', '.join(allowed)}")
        try:
            share = float(value)
        except ValueError:
            raise MixError(f"{name}: {value!r} is not a number") from None
        if share < 0:
            raise MixError(f"{name}: negative share {share}")
        mix[name] = share
    if not math.isclose(sum(mix.values()), 1.0, abs_tol=TOLERANCE):
        raise MixError(f"shares must sum to 1, got {sum(mix.values()):.3f}")
    return mix


def make_slots(
    n: int,
    mix: Mapping[str, float],
    traps: Mapping[str, str],
    trap_rate: float,
    rng: random.Random,
) -> list[Slot]:
    """`n` slots with exactly the mix's counts (largest remainder) and the trap rate."""
    raw = {kind: n * share for kind, share in mix.items()}
    counts = {kind: int(value) for kind, value in raw.items()}
    for kind in sorted(raw, key=lambda k: raw[k] - counts[k], reverse=True)[
        : n - sum(counts.values())
    ]:
        counts[kind] += 1
    kinds = [kind for kind, count in counts.items() for _ in range(count)]
    rng.shuffle(kinds)
    slots = [Slot(index=i, kind=kind) for i, kind in enumerate(kinds)]
    wanted = round(n * trap_rate)
    names = sorted(traps)
    candidates = list(range(n))
    rng.shuffle(candidates)
    placed = 0
    turn = 0
    for index in candidates:
        if placed >= wanted or not names:
            break
        for offset in range(len(names)):
            name = names[(turn + offset) % len(names)]
            if traps[name] == slots[index].kind:
                slots[index] = slots[index].model_copy(update={"trap": name})
                placed += 1
                turn = (turn + offset + 1) % len(names)
                break
    return slots
