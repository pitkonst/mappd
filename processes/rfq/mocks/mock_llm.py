"""Deterministic stand-in for the models the RFQ process calls, behind the shared mock port.

`claims` tells the core router (mappd.mocks.llm_router) which requests are RFQ's: the
extractor (it offers the `search_catalog` tool), the synth writer and checker (their
instructions name quotes and `lines_key`), and control paths under /rfq/. Answers come from
keyword rules over the text, never from labels.
"""

import json
import re
import time
import uuid
from collections import Counter
from typing import Any

from fastapi import Body, FastAPI

from processes.rfq import catalog

app = FastAPI(title="mock-llm-rfq")
CALLS: Counter[str] = Counter()
WRITER_MARK = "asks Helio Building Systems, a smart-building equipment distributor, for a quote"
WORD_QUANTITIES = {"two dozen": 24, "a dozen": 12, "a pair of": 2, "a hundred": 100}
DESC = r"([^\n,;.]+?)(?=\s+and\s|[\n,;.]|$)"  # up to the next item
ITEM = re.compile(r"(\d+)\s*x\s*" + DESC)
# Words that end an item's name: "10 x CO2 sensors delivered within 7 days" asks for sensors.
TAIL = re.compile(r"\s+(?:delivered|within|by|needed|before|until|asap)\b.*$", re.IGNORECASE)
WITHIN = re.compile(r"within (\d+) days")
GERMAN = ("guten tag", "bitte", "angebot", "viele grüße")


def _texts(body: dict[str, Any]) -> str:
    return "\n".join(str(m.get("content") or "") for m in body.get("messages", []))


def claims(body: dict[str, Any]) -> bool:
    """Whether a request is the RFQ process's."""
    if str(body.get("__path__", "")).startswith("/rfq/"):
        return True
    tools = {t.get("function", {}).get("name") for t in body.get("tools", [])}
    text = _texts(body)
    return "search_catalog" in tools or WRITER_MARK in text or "lines_key" in text


def _user(body: dict[str, Any]) -> str:
    users = [m for m in body.get("messages", []) if m.get("role") == "user"]
    return str(users[-1].get("content") or "") if users else ""


def read(text: str) -> dict[str, Any]:
    """An extraction from the text: "<n> x <name>" items and quantities in words."""
    lines: list[dict[str, Any]] = []
    found = [(int(q), desc) for q, desc in ITEM.findall(text)]
    for words, qty in WORD_QUANTITIES.items():
        for match in re.finditer(rf"{words} " + DESC, text, re.IGNORECASE):
            found.append((qty, match.group(1)))
    for qty, raw in found:
        desc = TAIL.sub("", raw).strip()
        hits = catalog.search(desc, limit=1)
        lines.append(
            {
                "requested": desc,
                "quantity": qty,
                "sku": hits[0].sku if hits else None,
                "confidence": "high" if hits else "low",
            }
        )
    within = WITHIN.search(text.lower())
    lower = text.lower()
    return {
        "lines": lines,
        "needed_by_days": int(within.group(1)) if within else None,
        "custom_request": "integrat" in lower or "custom" in lower,
        "language": "de" if any(w in lower for w in GERMAN) else "en",
    }


def _completion(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "mock",
        "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140},
    }


def _tool_call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    call = {
        "id": f"call_{uuid.uuid4().hex[:12]}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
    }
    reply = _completion({"role": "assistant", "content": None, "tool_calls": [call]})
    reply["choices"][0]["finish_reason"] = "tool_calls"
    return reply


def _write(user: str) -> str:
    facts = [line.lstrip("- ").strip() for line in user.splitlines() if line.startswith("- ")]
    german = "Language: German" in user
    greeting, closing = ("Guten Tag,", "Viele Grüße") if german else ("Hello,", "Best regards")
    return f"Subject: Request for quotation\n\n{greeting}\n" + "\n".join(facts) + f"\n\n{closing}"


def _check(user: str) -> str:
    extraction = read(user)
    key = ";".join(
        sorted(f"{line['sku'] or '?'}x{line['quantity'] or '?'}" for line in extraction["lines"])
    )
    if extraction["custom_request"]:
        outcome = "escalate"
    elif not extraction["lines"] or any(not line["sku"] for line in extraction["lines"]):
        outcome = "clarify"
    else:
        outcome = "quote"
    return json.dumps({"outcome": outcome, "language": extraction["language"], "lines_key": key})


@app.post("/v1/chat/completions")
def chat(body: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008 - FastAPI idiom
    """Writer, checker, or the extractor's final answer."""
    text = _texts(body)
    if WRITER_MARK in text:
        CALLS["synth"] += 1
        return _completion({"role": "assistant", "content": _write(_user(body))})
    if "lines_key" in text and not body.get("tools"):
        CALLS["check"] += 1
        return _completion({"role": "assistant", "content": _check(_user(body))})
    CALLS["extract"] += 1
    return _tool_call("final_result", read(_user(body)))


@app.get("/rfq/calls")
def calls() -> dict[str, int]:
    """Calls per kind since the last reset."""
    return dict(CALLS)


@app.post("/rfq/calls/reset")
def reset() -> dict[str, int]:
    """Forget the counts."""
    CALLS.clear()
    return {}
