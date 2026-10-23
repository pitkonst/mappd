"""The RFQ eval hook and config load, and every python metric resolves."""

import importlib
from pathlib import Path

from mappd.eval.config import load_config
from mappd.eval.hook import load_eval_hook

PROCESSES = Path(__file__).parents[2]


def test_config_metrics_resolve() -> None:
    config = load_config(PROCESSES / "rfq" / "evals" / "config.yaml")
    names = {m.name for m in config.metrics}
    assert {"outcome_accuracy", "line_items_f1", "totals_check"} <= names
    for m in config.metrics:
        if m.kind == "python":
            module, _, fn = (m.python or "").partition(":")
            assert callable(getattr(importlib.import_module(module), fn))


def test_hook_loads_with_a_keyword_baseline() -> None:
    hook = load_eval_hook(PROCESSES, "rfq")
    out = hook.baselines["keyword"](
        {"subject": "Quote", "body": "Please quote 12 temperature sensors."}
    )
    assert out["outcome"] in ("quote", "clarify", "escalate")
    assert isinstance(out["lines"], list)
    assert hook.judge_context(None, {}) is None  # type: ignore[arg-type]
