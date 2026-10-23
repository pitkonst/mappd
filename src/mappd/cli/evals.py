"""mappd eval: run a process over its eval sets, log baselines, compare runs."""

import asyncio
import dataclasses
import json
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from mappd import telemetry
from mappd.cli._common import (
    EXIT_FAILED,
    EXIT_USAGE,
    JsonFlag,
    emit,
    fail,
    load_settings,
    progress,
)
from mappd.data.cache import Cache
from mappd.data.cases import CaseFormatError, read_cases
from mappd.data.llm import Gateway
from mappd.eval.compare import (
    BASELINE_TAG,
    CompareError,
    Comparison,
    baseline_solver,
    compare,
    majority_baseline,
    pick_runs,
)
from mappd.eval.config import EvalConfigError, load_config
from mappd.eval.hook import EvalHook, EvalHookError, load_eval_hook
from mappd.eval.judge import JUDGE_MODEL
from mappd.eval.mlflow import Mlflow, MlflowError, RunRecord
from mappd.eval.runner import EvalReport, run_eval, run_params
from mappd.gateway import GatewayError, list_models

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import TracerProvider

    from mappd.settings import Settings


class SetName(StrEnum):
    """The eval sets."""

    dev = "dev"
    holdout = "holdout"
    feedback = "feedback"


app = typer.Typer(help="Evals of a process.", no_args_is_help=True)
NameArg = Annotated[str, typer.Argument(help="The process, e.g. support.")]
CacheOption = Annotated[
    Path,
    typer.Option("--cache-dir", help="Judge response cache (solver outputs are never cached)."),
]


def routed_models(settings: Settings) -> list[str]:
    """The gateway's model names; exit 1 when the gateway is down."""
    try:
        return list_models(settings)
    except GatewayError as e:
        fail(f"gateway unavailable: {e}", EXIT_FAILED)


def experiment_id(mlflow: Mlflow, name: str) -> str:
    """The process's eval experiment."""
    return mlflow.experiment(f"mappd-eval-{name}")


def start_tracing(settings: Settings, experiment: str) -> TracerProvider:
    """One trace per case, into the eval experiment."""
    return telemetry.setup(dataclasses.replace(settings, mlflow_experiment_id=experiment))


def _lines(report: EvalReport) -> list[str]:
    lines = [f"{report.set}: {report.cases} cases, {report.errored} errored, run {report.run_id}"]
    for m in report.metrics:
        value = "-" if m.value is None else f"{m.value:.3f}"
        sign = "<=" if m.lower_is_better else ">="
        verdict = {True: "pass", False: "FAIL", None: "n/a"}[m.passed]
        lines.append(
            f"  {m.name:<26} {value:>6}  {sign} {m.threshold:<5} {verdict:<4}  ({m.cases} cases)"
        )
    if report.judge_errored:
        lines.append(f"judge unreadable on {report.judge_errored} cases")
    if report.failing:
        lines.append(f"failing cases: {', '.join(report.failing)}")
    if not report.logged:
        lines.append("not logged: MLflow unreachable")
    return lines


def _prepare(name: str, cache_dir: Path) -> tuple[Settings, EvalHook, Mlflow, Gateway]:
    settings = load_settings()
    try:
        hook = load_eval_hook(settings.processes_dir, name)
        cfg_path = hook.config_path or (settings.processes_dir / name / "evals" / "config.yaml")
        load_config(cfg_path)
    except (EvalHookError, EvalConfigError) as e:
        fail(str(e), EXIT_USAGE)
    if JUDGE_MODEL not in routed_models(settings):
        fail(
            f"the gateway does not route {JUDGE_MODEL}; add it to deploy/agentgateway/", EXIT_USAGE
        )
    return settings, hook, Mlflow(settings.mlflow_url), Gateway(settings, Cache(cache_dir))


@app.command("run")
def run_command(
    name: NameArg,
    eval_set: Annotated[
        SetName, typer.Option("--set", help="dev, feedback, or holdout (aggregates only).")
    ] = SetName.dev,
    limit: Annotated[
        int | None, typer.Option("--limit", min=1, help="Only the first N cases (dev only).")
    ] = None,
    concurrency: Annotated[int, typer.Option("--concurrency", min=1, help="Cases in flight.")] = 8,
    cache_dir: CacheOption = Path(".mappd/cache"),
    as_json: JsonFlag = False,
) -> None:
    """Run the process over an eval set, score it, and log the run to MLflow.

    Exit 1 if a metric misses its threshold, more than 10% of cases error, or MLflow is down.
    A holdout run prints and logs aggregates only and refuses --limit.

    Example:
        mappd eval run support --limit 20 --json
    """
    if eval_set is SetName.holdout and limit is not None:
        fail("--limit is refused on the holdout: a partial run must not pass for one", EXIT_USAGE)
    settings, hook, mlflow, gateway = _prepare(name, cache_dir)
    provider = None
    if eval_set is not SetName.holdout:  # holdout runs never trace: text must not reach MLflow
        try:
            provider = start_tracing(settings, experiment_id(mlflow, name))
        except MlflowError:
            provider = None
    try:
        with progress(f"eval {name} {eval_set.value}", as_json) as tick:
            report = asyncio.run(
                run_eval(
                    settings,
                    name,
                    eval_set=eval_set.value,
                    solver=hook.solve,
                    hook=hook,
                    gateway=gateway,
                    mlflow=mlflow,
                    limit=limit,
                    concurrency=concurrency,
                    params=run_params(settings, name),
                    progress=tick,
                )
            )
    except (OSError, CaseFormatError, EvalConfigError) as e:
        fail(f"cannot read the {eval_set.value} set: {e}", EXIT_USAGE)
    finally:
        if provider is not None:
            provider.shutdown()
    emit(report.model_dump(), as_json, _lines(report))
    if report.failed:
        raise typer.Exit(EXIT_FAILED)


@app.command("baseline")
def baseline_command(
    name: NameArg, cache_dir: CacheOption = Path(".mappd/cache"), as_json: JsonFlag = False
) -> None:
    """Log trivial baselines on the dev set: the floor every metric is read against.

    One run per baseline: `majority` (the most common label per field) and each baseline the
    process's evaluate.py lists. Baseline runs are tagged and skipped by `eval compare`.
    Exit 1 if a run could not be logged.

    Example:
        mappd eval baseline support --json
    """
    settings, hook, mlflow, gateway = _prepare(name, cache_dir)
    evals = settings.processes_dir / name / "evals"
    cfg_path = getattr(hook, "config_path", None) or (evals / "config.yaml")
    dev_path = getattr(hook, "dev_path", None) or (evals / "dev.jsonl")
    config = load_config(cfg_path)
    try:
        dev = read_cases(dev_path)
    except (OSError, CaseFormatError) as e:
        fail(f"cannot read the dev set: {e}", EXIT_USAGE)
    baselines = {"majority": majority_baseline(dev, config), **hook.BASELINES}
    reports: dict[str, EvalReport] = {}
    for label, fn in baselines.items():
        with progress(f"baseline {label}", as_json) as tick:
            reports[label] = asyncio.run(
                run_eval(
                    settings,
                    name,
                    eval_set="dev",
                    solver=baseline_solver(fn),
                    hook=hook,
                    gateway=gateway,
                    mlflow=mlflow,
                    run_name=f"baseline {label}",
                    tags={BASELINE_TAG: label},
                    params=run_params(settings, name),
                    progress=tick,
                )
            )
    lines = []
    for label, r in reports.items():
        lines += [f"baseline {label}:", *_lines(r)[1:]]
    emit(
        {"baselines": [{"label": label, **r.model_dump()} for label, r in reports.items()]},
        as_json,
        lines,
    )
    if any(not r.logged for r in reports.values()):
        raise typer.Exit(EXIT_FAILED)


def compare_runs(settings: Settings, name: str, run_ids: list[str], eval_set: str) -> Comparison:
    """Load the two runs and (dev) their case rows, and compare them."""
    mlflow = Mlflow(settings.mlflow_url)
    hook = load_eval_hook(settings.processes_dir, name)
    cfg_path = hook.config_path or (settings.processes_dir / name / "evals" / "config.yaml")
    config = load_config(cfg_path)
    before, after = pick_runs(mlflow, experiment_id(mlflow, name), run_ids, eval_set)

    def rows(run: RunRecord) -> list[dict[str, Any]] | None:
        raw = mlflow.get_artifact(run, "cases.jsonl")
        if raw is None:
            return None
        return [json.loads(line) for line in raw.decode().splitlines() if line]

    if after.tags.get("mappd.set") == "holdout":
        return compare(config, before, after, None, None)
    return compare(config, before, after, rows(before), rows(after))


@app.command("compare")
def compare_command(
    name: NameArg,
    run_ids: Annotated[
        list[str] | None, typer.Argument(help="RUN_A RUN_B (default: the last two runs).")
    ] = None,
    eval_set: Annotated[
        SetName, typer.Option("--set", help="Which set's runs to pick by default.")
    ] = SetName.dev,
    as_json: JsonFlag = False,
) -> None:
    """Compare two eval runs: metric deltas, regressions beyond tolerance, flipped cases (dev).

    Exit 1 on a regression or when MLflow is down, 2 when the runs cannot be found.

    Example:
        mappd eval compare support
    """
    settings = load_settings()
    try:
        result = compare_runs(settings, name, run_ids or [], eval_set.value)
    except MlflowError as e:
        fail(str(e), EXIT_FAILED)
    except (CompareError, EvalConfigError) as e:
        fail(str(e), EXIT_USAGE)
    lines = [f"{result.before} -> {result.after} ({result.set})"]
    for d in result.deltas:
        change = "-" if d.change is None else f"{d.change:+.3f}"
        lines.append(f"  {d.name:<26} {change:>7}{'  REGRESSION' if d.regression else ''}")
    lines += [f"flipped {metric}: {', '.join(ids)}" for metric, ids in result.flipped.items()]
    emit(result.model_dump(), as_json, lines)
    if result.regressions:
        raise typer.Exit(EXIT_FAILED)
