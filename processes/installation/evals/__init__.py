"""The installation process's evaluation: dialogue simulation (`mappd eval run installation`)."""

import json
from pathlib import Path
from typing import Any

from mappd.data.cases import Case
from mappd.eval.hook import EvalSpec

from ..simulator import keyword_baseline, solve


def judge_context(case: Case, output: dict[str, Any]) -> dict[str, str]:
    """The conversation, the lookups' results and the proposals, for `no_unbacked_promises`."""
    del case
    lines = []
    for t in output.get("transcript") or []:
        photo = f" [{t['photo']}]" if t.get("photo") else ""
        lines += [f"technician: {t['installer']}{photo}", f"bot: {t['bot']}"]
    calls = [
        f"{c['tool']}({json.dumps(c['arguments'])}) -> {json.dumps(c['result'])}"
        for c in output.get("tool_calls") or []
    ]
    proposals = [
        f"{p['tool']}({json.dumps(p['arguments'])}): {p.get('reason', '')}"
        for p in output.get("proposals") or []
    ]
    return {
        "conversation": "\n".join(lines),
        "tool results": "\n".join(calls) or "(no lookups)",
        "proposals (not confirmed)": "\n".join(proposals) or "(none)",
    }


DIR = Path(__file__).parent

spec = EvalSpec(
    solve=solve,
    judge_context=judge_context,
    baselines={"keywords": keyword_baseline},
    judge_criteria=("no_unbacked_promises",),
    config_path=DIR / "config.yaml",
    rubric_path=DIR / "rubric.md",
    dev_path=DIR / "dev.jsonl",
    holdout_path=DIR / "holdout.jsonl",
    canary_path=DIR / "canary.txt",
)
