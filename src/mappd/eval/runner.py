"""Run a process over an eval set, score it, log it to MLflow."""

import asyncio
import contextlib
import hashlib
import json
import subprocess
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from opentelemetry import trace
from pydantic import BaseModel

from mappd import telemetry
from mappd.data.cases import Case, read_cases
from mappd.eval import metrics as scoring
from mappd.eval.config import load_config
from mappd.eval.judge import DEFAULT_CRITERIA, judge
from mappd.eval.mlflow import MlflowError, TraceRecord
from mappd.process import get_process

if TYPE_CHECKING:
    from pathlib import Path

    from mappd.data.llm import Gateway
    from mappd.eval.config import Metric
    from mappd.eval.hook import EvalHook
    from mappd.eval.mlflow import Mlflow, RunRecord
    from mappd.settings import Settings

type EvalSet = Literal["dev", "holdout", "feedback"]
type Solver = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
ERRORED_LIMIT = 0.10
tracer = trace.get_tracer("mappd.eval")


class MetricResult(BaseModel):
    """One metric's aggregate."""

    name: str
    value: float | None
    threshold: float
    lower_is_better: bool
    passed: bool | None
    cases: int


class CaseResult(BaseModel):
    """One case's output and scores (dev only; never logged for holdout)."""

    id: str
    expected: dict[str, Any]
    output: dict[str, Any] | None
    error: str | None
    scores: dict[str, float | None]
    judge_note: str | None
    trace_id: str | None = None


class EvalReport(BaseModel):
    """What `mappd eval run` prints."""

    run_id: str | None
    set: EvalSet
    cases: int
    errored: int
    judge_errored: int
    metrics: list[MetricResult]
    failing: list[str]
    logged: bool
    failed: bool


def _git(*args: str) -> str:
    try:
        return subprocess.run(  # noqa: S603 - fixed argv
            ["git", *args],  # noqa: S607 - git from PATH
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
    except OSError:
        return ""


def run_params(settings: Settings, name: str) -> dict[str, str]:
    """Git SHA and dirty flag, a hash of the process agents' prompts, the gateway config file."""
    proc = get_process(settings.processes_dir, name)
    prompts_sha = (
        proc.prompts_sha(settings.processes_dir)
        if proc is not None
        else hashlib.sha256().hexdigest()[:12]
    )
    return {
        "git_sha": _git("rev-parse", "--short", "HEAD") or "unknown",
        "git_dirty": str(bool(_git("status", "--porcelain"))).lower(),
        "prompts_sha": prompts_sha,
        "gateway_config": str(settings.gateway_config),
    }


class _Case:
    """Everything one case needs: the metrics, the solver, the judge."""

    def __init__(
        self, metrics: list[Metric], solver: Solver, hook: EvalHook, gateway: Gateway, rubric: str
    ) -> None:
        self.metrics = metrics
        self.solver = solver
        self.hook = hook
        self.gateway = gateway
        self.rubric = rubric

    async def run(self, case: Case) -> tuple[CaseResult, bool]:
        """Solve, judge and score one case; the bool says the judge could not be read."""
        try:
            output = await self.solver(case.input)
        except Exception as e:
            scores: dict[str, float | None] = {m.name: None for m in self.metrics}
            error = f"{type(e).__name__}: {e}"
            return CaseResult(
                id=case.id,
                expected=case.expected,
                output=None,
                error=error,
                scores=scores,
                judge_note=None,
            ), False
        judged, note, judge_error = await self._judge(case, output)
        scores = {m.name: scoring.score_case(m, case, output, judged) for m in self.metrics}
        return CaseResult(
            id=case.id,
            expected=case.expected,
            output=output,
            error=None,
            scores=scores,
            judge_note=note,
        ), judge_error

    async def _judge(
        self, case: Case, output: dict[str, Any]
    ) -> tuple[bool | None, str | None, bool]:
        if not any(m.kind == "judge" and scoring.in_scope(m, case) for m in self.metrics):
            return None, None, False
        try:
            context = self.hook.judge_context(case, output)
            if context is None or ("reply" in context and not str(context["reply"]).strip()):
                return False, "no reply drafted", False
            criteria: tuple[str, ...] = getattr(self.hook, "judge_criteria", DEFAULT_CRITERIA)
            verdict = await asyncio.to_thread(
                judge, self.gateway, self.rubric, context, criteria=criteria
            )
        except Exception:
            verdict = None
        if verdict is None:
            return None, None, True
        return verdict.passed, verdict.note, False

    def _traced_case(
        self,
        case: Case,
        run_id: str,
        res: CaseResult,
        start_ms: int,
        trace_id: str | None,
        *,
        model_id: str | None,
    ) -> tuple[CaseResult, TraceRecord | None]:
        duration_ms = max(int(time.time() * 1000) - start_ms, 1)
        user = str(case.input.get("sender") or "") or None
        session = str(case.input.get("external_id") or case.id)
        tags = {"mappd.eval.case_id": case.id, **({"mappd.eval.run_id": run_id} if run_id else {})}
        out = res.output if res.output is not None else {"error": res.error}
        record = (
            TraceRecord(
                trace_id=trace_id,
                name=f"eval.case {case.id}",
                start_ms=start_ms,
                duration_ms=duration_ms,
                state="ERROR" if res.error else "OK",
                request_preview=json.dumps(case.input, ensure_ascii=False),
                response_preview=json.dumps(out, ensure_ascii=False),
                user=user,
                session=session,
                tags=tags,
                model_id=model_id,
            )
            if trace_id
            else None
        )
        return res.model_copy(update={"trace_id": trace_id}), record

    async def run_traced(
        self, case: Case, run_id: str, model_id: str | None
    ) -> tuple[CaseResult, bool, TraceRecord | None]:
        """Run one dev case inside an `eval.case` span and build its `TraceRecord`."""
        start_ms = int(time.time() * 1000)
        tags = {"mappd.eval.case_id": case.id, **({"mappd.eval.run_id": run_id} if run_id else {})}
        with tracer.start_as_current_span("eval.case", attributes=tags) as span:
            telemetry.annotate_trace(
                name=f"eval.case {case.id}",
                inputs=case.input,
                user=str(case.input.get("sender") or "") or None,
                session=str(case.input.get("external_id") or case.id),
                span_type="EVALUATOR",
                tags=tags,
            )
            res, j_err = await self.run(case)
            telemetry.annotate_trace(
                outputs=res.output if res.output is not None else {"error": res.error}
            )
            ctx = span.get_span_context()
            trace_id = f"tr-{ctx.trace_id:032x}" if ctx.is_valid else None
        res, record = self._traced_case(case, run_id, res, start_ms, trace_id, model_id=model_id)
        return res, j_err, record


def _misses(metrics: list[Metric], result: CaseResult) -> bool:
    """Whether the case scored worse than perfect on any metric, in that metric's direction."""
    for metric in metrics:
        score = result.scores[metric.name]
        if score is not None and (score > 0.0 if metric.lower_is_better else score < 1.0):
            return True
    return False


def _start(
    mlflow: Mlflow, name: str, eval_set: EvalSet, run_name: str | None, tags: Mapping[str, str]
) -> tuple[str | None, RunRecord | None]:
    try:
        experiment = mlflow.experiment(f"mappd-eval-{name}")
        stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
        run = mlflow.start_run(
            experiment, run_name or f"{eval_set} {stamp}", {"mappd.set": eval_set, **tags}
        )
        return experiment, run
    except MlflowError:
        return None, None


def _logged_model(
    mlflow: Mlflow,
    experiment: str | None,
    run: RunRecord | None,
    name: str,
    settings: Settings,
    *,
    params: Mapping[str, str] | None,
) -> str | None:
    """The Versions tab's entry for this process at this code/prompt version, if MLflow is up."""
    if run is None or experiment is None:
        return None
    with contextlib.suppress(MlflowError):
        return mlflow.logged_model(
            experiment,
            name,
            settings.app_version,
            {"app_version": settings.app_version, **(params or {})},
        )
    return None


def _flush_eval_traces(
    mlflow: Mlflow, experiment: str | None, run_id: str, trace_records: list[TraceRecord]
) -> None:
    if experiment is None or not trace_records:
        return
    provider = trace.get_tracer_provider()
    if hasattr(provider, "force_flush"):
        provider.force_flush()
    for record in trace_records:
        with contextlib.suppress(MlflowError):
            mlflow.register_trace(experiment, run_id, record)
    with contextlib.suppress(MlflowError):
        mlflow.link_traces(run_id, [t.trace_id for t in trace_records])


def _counter(progress: Callable[[int, int], None] | None, total: int) -> Callable[[], None]:
    """A `tick()` reporting (done, total); calls progress(0, total) first. Single-threaded."""
    done = 0

    def tick() -> None:
        nonlocal done
        done += 1
        if progress is not None:
            progress(done, total)

    if progress is not None:
        progress(0, total)
    return tick


async def _run_cases(
    cases: list[Case],
    worker: _Case,
    *,
    eval_set: EvalSet,
    run_id: str,
    model_id: str | None,
    concurrency: int,
    progress: Callable[[int, int], None] | None,
    mlflow: Mlflow,
    run: RunRecord | None,
) -> list[tuple[CaseResult, bool, TraceRecord | None]]:
    """Run every case under the concurrency gate; progress ticks as each finishes."""
    gate = asyncio.Semaphore(concurrency)
    tick = _counter(progress, len(cases))

    async def bounded(case: Case) -> tuple[CaseResult, bool, TraceRecord | None]:
        async with gate:
            out: tuple[CaseResult, bool, TraceRecord | None]
            if eval_set == "holdout":  # no spans: holdout text never reaches MLflow
                res, j_err = await worker.run(case)
                out = (res, j_err, None)
            else:
                out = await worker.run_traced(case, run_id, model_id)
        tick()
        return out

    try:
        return await asyncio.gather(*(bounded(c) for c in cases))
    except BaseException:
        if run is not None:
            with contextlib.suppress(MlflowError):
                mlflow.finish(run.run_id, "FAILED")
        raise


def _metric_results(
    metrics: list[Metric], results: list[CaseResult], too_many_errors: bool
) -> list[MetricResult]:
    out = []
    for metric in metrics:
        scores = [r.scores[metric.name] for r in results]
        value = scoring.aggregate(metric, scores)
        out.append(
            MetricResult(
                name=metric.name,
                value=value,
                threshold=metric.threshold,
                lower_is_better=metric.lower_is_better,
                passed=None if too_many_errors else scoring.passes(metric, value),
                cases=sum(1 for s in scores if s is not None),
            )
        )
    return out


def _eval_paths(
    settings: Settings, name: str, eval_set: EvalSet, hook: EvalHook
) -> tuple[Path, Path, Path]:
    fallback = settings.processes_dir / name / "evals"
    cfg_path: Path = getattr(hook, "config_path", None) or (fallback / "config.yaml")
    rub_path: Path = getattr(hook, "rubric_path", None) or (fallback / "rubric.md")
    set_attr = {"dev": "dev_path", "holdout": "holdout_path"}.get(eval_set)
    hooked: Path | None = getattr(hook, set_attr, None) if set_attr else None
    cases_path: Path = hooked or (fallback / f"{eval_set}.jsonl")
    return cfg_path, rub_path, cases_path


async def run_eval(
    settings: Settings,
    name: str,
    *,
    eval_set: EvalSet,
    solver: Solver,
    hook: EvalHook,
    gateway: Gateway,
    mlflow: Mlflow,
    limit: int | None = None,
    concurrency: int = 8,
    run_name: str | None = None,
    tags: Mapping[str, str] | None = None,
    params: Mapping[str, str] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> EvalReport:
    """Run `solver` over the set, score every metric, log to MLflow (aggregates for holdout)."""
    if eval_set == "holdout" and limit is not None:
        raise ValueError("--limit is refused on the holdout: a partial run must not pass for one")
    cfg_path, rub_path, cases_path = _eval_paths(settings, name, eval_set, hook)
    config = load_config(cfg_path)
    rubric = rub_path.read_text(encoding="utf-8") if rub_path.is_file() else ""
    cases = read_cases(cases_path)[: limit or None]

    experiment, run = _start(mlflow, name, eval_set, run_name, tags or {})
    run_id = run.run_id if run else ""
    model_id = _logged_model(mlflow, experiment, run, name, settings, params=params)
    worker = _Case(config.metrics, solver, hook, gateway, rubric)
    results = await _run_cases(
        cases,
        worker,
        eval_set=eval_set,
        run_id=run_id,
        model_id=model_id,
        concurrency=concurrency,
        progress=progress,
        mlflow=mlflow,
        run=run,
    )
    case_results = [r for r, _, _ in results]
    errored = sum(1 for r in case_results if r.error)
    judge_errored = sum(1 for _, j, _ in results if j)
    trace_records = [t for _, _, t in results if t is not None]
    judge_metrics = [m for m in config.metrics if m.kind == "judge"]
    judged_cases = sum(
        1
        for case, result in zip(cases, case_results, strict=True)
        if result.output is not None and any(scoring.in_scope(m, case) for m in judge_metrics)
    )
    judge_broken = bool(judged_cases) and judge_errored / judged_cases > ERRORED_LIMIT
    too_many_errors = (bool(cases) and errored / len(cases) > ERRORED_LIMIT) or judge_broken
    metric_results = _metric_results(config.metrics, case_results, too_many_errors)
    below = any(m.passed is False for m in metric_results)

    failing: list[str] = []
    if eval_set == "dev":
        failing = sorted(r.id for r in case_results if r.error or _misses(config.metrics, r))

    logged = run is not None
    if run is not None:
        try:
            values = {m.name: m.value for m in metric_results if m.value is not None}
            counts = {
                "cases": float(len(cases)),
                "errored": float(errored),
                "judge_errored": float(judge_errored),
            }
            model = {"model_id": model_id} if model_id else {}
            mlflow.log(
                run.run_id, {"set": eval_set, **model, **(params or {})}, {**values, **counts}
            )
            if eval_set == "dev":
                lines = "".join(r.model_dump_json() + "\n" for r in case_results)
                mlflow.put_artifact(run, "cases.jsonl", lines.encode())
                _flush_eval_traces(mlflow, experiment, run.run_id, trace_records)
            # FAILED marks an incomplete run that compare must skip; below-threshold is a result.
            mlflow.finish(run.run_id, "FAILED" if too_many_errors else "FINISHED")
        except MlflowError:
            logged = False

    return EvalReport(
        run_id=run.run_id if run else None,
        set=eval_set,
        cases=len(cases),
        errored=errored,
        judge_errored=judge_errored,
        metrics=metric_results,
        failing=failing,
        logged=logged,
        failed=too_many_errors or not logged or below,
    )
