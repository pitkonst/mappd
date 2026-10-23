"""The support process's evaluation specification (`mappd eval run support`)."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import cache
from pathlib import Path
from typing import Any

from mappd.data.cases import Case
from mappd.eval.hook import EvalSpec

from ..data import seed
from ..schemas import TicketIn
from ..workflow import solve as solve_ticket

NO_ARTICLES = "(none: the reply should ask for the missing fact)"
COMMERCIAL = ("refund", "money back", "invoice", "payment", "price", "deliver", "ship", "quote")
REFUND = ("refund", "money back", "rückerstattung")
GERMAN = ("guten tag", "hallo", " ich ", "sehr geehrte")


async def solve(input: dict[str, Any]) -> dict[str, Any]:  # noqa: A002 - the hook's API
    """The support decision for one case input."""
    output = await solve_ticket(TicketIn.model_validate(input))
    return output.model_dump()


def judge_context(case: Case, output: dict[str, Any]) -> dict[str, str]:
    """The email, the expected outcome, the reply and the articles it is judged against.

    Those are the expected articles and the ones the reply cites: a step from a cited article
    is grounded, whether it was the right article to cite is `citations_f1`'s question.
    """
    bodies = _articles()
    draft = output.get("draft") or {}
    ids = dict.fromkeys([*(case.expected.get("citations") or []), *(draft.get("citations") or [])])
    cited = [bodies[c] for c in ids if c in bodies]
    return {
        "email": f"Subject: {case.input.get('subject', '')}\n\n{case.input.get('body', '')}",
        "expected outcome": str(case.expected.get("outcome", "")),
        "reply": str(draft.get("reply", "")),
        "articles": "\n".join(cited) or NO_ARTICLES,
    }


def keyword_baseline(input: dict[str, Any]) -> dict[str, Any]:  # noqa: A002 - the hook's API
    """A floor: commercial words route commercial, refund words escalate to sales."""
    text = f" {input.get('subject', '')} {input.get('body', '')} ".lower()
    refund = any(w in text for w in REFUND)
    return {
        "topic": "refund" if refund else "other",
        "route": "commercial" if any(w in text for w in COMMERCIAL) else "technical",
        "urgency": "normal",
        "language": "de" if any(w in text for w in GERMAN) else "en",
        "outcome": "escalate" if refund else "reply",
        "escalate_to": "sales" if refund else None,
        "draft": None,
    }


@cache
def _articles() -> dict[str, str]:
    async def collect() -> dict[str, str]:
        return {a.id: f"{a.id}: {a.title}. {a.body}" async for a in seed.load_articles()}

    with ThreadPoolExecutor(1) as pool:
        return pool.submit(asyncio.run, collect()).result()


BASELINES = {"keywords": keyword_baseline}
DIR = Path(__file__).parent

spec = EvalSpec(
    solve=solve,
    judge_context=judge_context,
    baselines=BASELINES,
    config_path=DIR / "config.yaml",
    rubric_path=DIR / "rubric.md",
    dev_path=DIR / "dev.jsonl",
    holdout_path=DIR / "holdout.jsonl",
    canary_path=DIR / "canary.txt",
)
