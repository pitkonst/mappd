"""`mappd data synth`: plan, write, check and keep synthetic cases for a process."""

import random
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from mappd.data import roundtrip, writer
from mappd.data.cache import Cache
from mappd.data.cases import Case, CasePlan, Message, write_cases
from mappd.data.hook import load_hook
from mappd.data.llm import BACKOFF, Gateway, GatewayError
from mappd.data.plan import make_slots
from mappd.gateway import GatewayError as GatewayModelsError
from mappd.gateway import list_models

if TYPE_CHECKING:
    from collections.abc import Callable

    import httpx2

    from mappd.settings import Settings

# Cases in flight at once: each holds a writer call and then a checker call.
CONCURRENCY = 8

MODELS = (writer.SYNTH_MODEL, roundtrip.CHECK_MODEL)


def _counter(progress: Callable[[int, int], None] | None, total: int) -> Callable[[], None]:
    """A thread-safe `tick()` that reports (done, total); calls progress(0, total) first."""
    done = 0
    lock = threading.Lock()

    def tick() -> None:
        nonlocal done
        if progress is None:
            return
        with lock:
            done += 1
            progress(done, total)

    if progress is not None:
        progress(0, total)
    return tick


def _round_trips(
    plans: list[CasePlan],
    work: Callable[[CasePlan], tuple[Message, dict[str, Any]]],
    concurrency: int,
    progress: Callable[[int, int], None] | None,
) -> list[tuple[Message, dict[str, Any]]]:
    """Run `work` per plan on `concurrency` threads; results stay in plan order."""
    tick = _counter(progress, len(plans))

    def counted(plan: CasePlan) -> tuple[Message, dict[str, Any]]:
        result = work(plan)
        tick()
        return result

    pool = ThreadPoolExecutor(max_workers=max(1, concurrency))
    results: list[tuple[Message, dict[str, Any]]] = []
    try:
        futures = [pool.submit(counted, plan) for plan in plans]
        for done, future in enumerate(futures):
            try:
                results.append(future.result())
            except GatewayError as e:
                raise SynthError(
                    f"gateway failed after {done} of {len(plans)} cases: {e}", done
                ) from e
    except BaseException:
        # Ctrl-C or a failure: drop the queued cases instead of paying for all of them.
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    return results


class ConfigError(ValueError):
    """The gateway does not route the models synthesis needs."""


class SynthError(RuntimeError):
    """The run stopped; `written` cases were done (and cached) before it did."""

    def __init__(self, message: str, written: int) -> None:
        """Record how far the run got."""
        super().__init__(message)
        self.written = written


class Report(BaseModel):
    """What a run planned, kept and dropped."""

    planned: dict[str, int]
    kept: dict[str, int] = {}
    dropped: dict[str, int] = {}
    dropped_traps: dict[str, int] = {}
    disagreements: dict[str, int] = {}
    cache_hits: int = 0
    cache_misses: int = 0
    path: str | None = None
    failed: bool = False


def run(
    settings: Settings,
    name: str,
    *,
    seed: int,
    n: int,
    mix: dict[str, float] | None = None,
    out_dir: Path | None = None,
    cache_dir: Path = Path(".mappd/cache"),
    dry_run: bool = False,
    client: httpx2.Client | None = None,
    check_models: bool = True,
    concurrency: int = CONCURRENCY,
    backoff: float = BACKOFF,
    progress: Callable[[int, int], None] | None = None,
) -> Report:
    """Generate `n` cases for process `name` from `seed`.

    Cases run on `concurrency` threads (each is a writer call, then a checker call) and are
    kept in plan order, so the output does not depend on which call returns first.
    """
    hook = load_hook(settings.processes_dir, name)
    rng = random.Random(seed)
    slots = make_slots(n, mix or dict(hook.MIX), hook.TRAPS, hook.TRAP_RATE, rng)
    plans = [
        hook.plan_case(slot, rng).model_copy(update={"id": f"s{seed}-{slot.index:04d}"})
        for slot in slots
    ]
    report = Report(planned=dict(Counter(s.kind for s in slots)))
    if dry_run:
        return report
    if check_models:
        try:
            routed = list_models(settings)
        except GatewayModelsError as e:
            raise SynthError(f"gateway unavailable: {e}", 0) from e
        missing = [m for m in MODELS if m not in routed]
        if missing:
            raise ConfigError(
                f"the gateway does not route {', '.join(missing)}; add them to deploy/agentgateway/"
            )
    cache = Cache(cache_dir)
    gateway = Gateway(settings, cache, client=client, backoff=backoff)
    kept: Counter[str] = Counter()
    dropped: Counter[str] = Counter()
    dropped_traps: Counter[str] = Counter()
    disagreements: Counter[str] = Counter()
    cases: list[Case] = []

    def round_trip(plan: CasePlan) -> tuple[Message, dict[str, Any]]:
        message = writer.write(gateway, plan, seed, system=hook.writer_instructions)
        got = (
            roundtrip.extract(
                gateway, message, hook.ROUNDTRIP_FIELDS, hook.CHECK_INSTRUCTIONS, seed
            )
            if message.body
            else {}
        )
        return message, got

    results = _round_trips(plans, round_trip, concurrency, progress)
    for plan, (message, got) in zip(plans, results, strict=True):
        verdict = roundtrip.compare(plan.labels, got, hook.ROUNDTRIP_FIELDS, hook.STRICT_FIELDS)
        disagreements.update(verdict.disagreements)
        if not message.body or not verdict.keep:
            dropped[plan.slot.kind] += 1
            if plan.slot.trap:
                dropped_traps[plan.slot.trap] += 1
            continue
        kept[plan.slot.kind] += 1
        cases.append(
            Case(
                id=plan.id,
                input=hook.to_input(plan, message),
                expected=plan.labels,
                meta={
                    "slot": plan.slot.kind,
                    "trap": plan.slot.trap,
                    "persona": plan.customer.persona.id if plan.customer is not None else None,
                    "seed": seed,
                },
            )
        )
    path = (out_dir or hook.output_dir or settings.processes_dir / name / "evals") / "synth.jsonl"
    write_cases(path, cases)
    return report.model_copy(
        update={
            "kept": dict(kept),
            "dropped": dict(dropped),
            "dropped_traps": dict(dropped_traps),
            "disagreements": dict(disagreements),
            "cache_hits": cache.hits,
            "cache_misses": cache.misses,
            "path": str(path),
            "failed": sum(dropped.values()) > len(plans) / 2,
        }
    )
