"""mappd data: synthetic and imported cases, the dev/holdout split, canaries."""

from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from pydantic import BaseModel

from mappd.cli._common import (
    EXIT_FAILED,
    EXIT_USAGE,
    JsonFlag,
    emit,
    fail,
    load_settings,
    progress,
)
from mappd.data import canary as canary_module
from mappd.data import synth as synth_module
from mappd.data.cases import CaseFormatError, read_cases, write_cases
from mappd.data.hook import HookError, load_hook
from mappd.data.importer import RowError, import_rows, parse_map, read_rows
from mappd.data.plan import MixError, parse_mix
from mappd.data.split import split as split_cases
from mappd.data.split import write_split
from mappd.data.world import WorldError
from mappd.process import get_process
from mappd.settings import Settings


class Source(StrEnum):
    """The files `split` may read: never holdout.jsonl, never dev.jsonl."""

    synth = "synth"
    imported = "imported"


app = typer.Typer(help="Eval datasets.", no_args_is_help=True)

OutOption = Annotated[
    Path | None, typer.Option("--out", help="Directory (default processes/NAME/evals).")
]


@app.command()
def synth(
    name: Annotated[str, typer.Argument(help="The process, e.g. support.")],
    seed: Annotated[int, typer.Option("--seed", help="Seed of the world expansion and the plan.")],
    n: Annotated[int, typer.Option("--n", min=1, help="Number of cases to plan.")],
    mix: Annotated[
        str | None, typer.Option("--mix", help="e.g. reply=.5,clarify=.2,escalate=.3")
    ] = None,
    out: OutOption = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Plan and report; no model calls.")
    ] = False,
    cache_dir: Annotated[
        Path, typer.Option("--cache-dir", help="Response cache (keyed by the answering model).")
    ] = Path(".mappd/cache"),
    concurrency: Annotated[
        int,
        typer.Option(
            "--concurrency", min=1, help="Cases in flight at once (writer, then checker)."
        ),
    ] = synth_module.CONCURRENCY,
    as_json: JsonFlag = False,
) -> None:
    """Generate labelled cases from world.yaml.

    The gateway model `synth` writes each case and `check` reads it back; a case is kept
    only when the check recovers its labels. Exit 1 if the gateway fails or more than half
    the cases fail the check.

    Example:
        mappd data synth support --seed 42 --n 300 --json
    """
    settings = load_settings()
    try:
        hook = load_hook(settings.processes_dir, name)
        shares = parse_mix(mix, hook.MIX) if mix else None
        with progress(f"synth {name}", as_json) as tick:
            report = synth_module.run(
                settings,
                name,
                seed=seed,
                n=n,
                mix=shares,
                out_dir=out,
                cache_dir=cache_dir,
                dry_run=dry_run,
                concurrency=concurrency,
                progress=tick,
            )
    except (HookError, MixError, WorldError, synth_module.ConfigError) as e:
        fail(str(e), EXIT_USAGE)
    except synth_module.SynthError as e:
        fail(f"{e} (re-run to resume from the cache)")
    lines = [f"planned {report.planned}"]
    if not dry_run:
        lines += [
            f"kept {report.kept}",
            f"dropped {report.dropped} (traps {report.dropped_traps})",
            f"disagreements {report.disagreements}",
            f"cache {report.cache_hits} hits, {report.cache_misses} misses",
            f"wrote {report.path}",
        ]
    emit(report.model_dump(), as_json, lines)
    if report.failed:
        fail(
            "more than half the cases failed the round trip; check the prompt and the checker model"
        )


@app.command("import")
def import_(
    name: Annotated[str, typer.Argument(help="The process.")],
    source: Annotated[Path, typer.Argument(help="CSV with a header, or JSON lines.")],
    mapping: Annotated[str, typer.Option("--map", help="dst=src,...; bare dst means input.dst")],
    skip_invalid: Annotated[
        bool, typer.Option("--skip-invalid", help="Report bad rows, keep going.")
    ] = False,
    out: OutOption = None,
    as_json: JsonFlag = False,
) -> None:
    """Import real history into processes/NAME/evals/imported.jsonl.

    Example:
        mappd data import support history.csv --map external_id=id,sender=from,subject=title,body=text
    """
    settings = load_settings()
    try:
        input_model = _input_model(settings, name)
        rows = read_rows(source)
        cases, errors = import_rows(
            rows, parse_map(mapping), input_model, skip_invalid=skip_invalid
        )
    except RowError as e:
        fail(str(e))
    except (OSError, ValueError) as e:  # missing file, bad map, no INPUT_MODEL
        fail(str(e), EXIT_USAGE)
    path = (out or _output_dir(settings, name)) / "imported.jsonl"
    write_cases(path, cases)
    emit(
        {"imported": len(cases), "skipped": [str(e) for e in errors], "path": str(path)},
        as_json,
        [f"imported {len(cases)} cases into {path}", *(f"skipped {e}" for e in errors)],
    )


def _output_dir(settings: Settings, name: str) -> Path:
    proc = get_process(settings.processes_dir, name)
    if proc is not None and proc.synth is not None and proc.synth.output_dir is not None:
        return proc.synth.output_dir
    return settings.processes_dir / name / "evals"


def _input_model(settings: Settings, name: str) -> type[BaseModel]:
    """The process input model: from `ingest`'s type annotation or its `SynthSpec`."""
    proc = get_process(settings.processes_dir, name)
    if proc is None:
        raise HookError(f"no process {name!r} in {settings.processes_dir}")
    model = proc.ingest_input_model() or (
        proc.synth.input_model if proc.synth is not None else None
    )
    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        raise HookError(f"process {name!r} must define a typed ingest or synth.input_model")
    return model


@app.command()
def split(
    name: Annotated[str, typer.Argument(help="The process.")],
    holdout: Annotated[float, typer.Option("--holdout", min=0.05, max=0.5, help="Holdout share.")],
    seed: Annotated[int, typer.Option("--seed", help="Seed of the split (not the synth seed).")],
    source: Annotated[
        Source, typer.Option("--from", help="Which generated file to split.")
    ] = Source.synth,
    out: OutOption = None,
    as_json: JsonFlag = False,
) -> None:
    """Split cases into dev.jsonl (committed) and holdout.jsonl (git-ignored).

    Prints counts only.

    Example:
        mappd data split support --holdout 0.2 --seed 7
    """
    settings = load_settings()
    evals = out or _output_dir(settings, name)
    try:
        cases = read_cases(evals / f"{source.value}.jsonl")
    except (OSError, CaseFormatError) as e:
        fail(str(e), EXIT_USAGE)
    token = canary_module.ensure(evals)
    dev, held = split_cases(cases, holdout, seed, token)
    dev_path, holdout_path = write_split(evals, dev, held)
    emit(
        {"dev": len(dev), "holdout": len(held), "dev_path": str(dev_path)},
        as_json,
        [f"dev {len(dev)} -> {dev_path}", f"holdout {len(held)} -> {holdout_path} (git-ignored)"],
    )


@app.command("check-canary")
def check_canary(as_json: JsonFlag = False) -> None:
    """Fail if any process's holdout canary appears in a git-tracked file (CI and pre-commit).

    Example:
        mappd data check-canary
    """
    settings = load_settings()
    hits = canary_module.scan(Path.cwd(), canary_module.canaries(settings.processes_dir))
    emit(
        {"ok": not hits, "hits": [h.model_dump() for h in hits]},
        as_json,
        [f"{h.path}:{h.line}: holdout canary of {h.process}" for h in hits]
        or ["ok: no canary in tracked files"],
    )
    if hits:
        raise typer.Exit(EXIT_FAILED)
