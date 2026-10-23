"""Deterministic OpenAI-compatible model used in place of a real provider.

Answers /v1/chat/completions (with tool calls) and /v1/embeddings, counts every call, and can
be slowed down so a test can kill the app mid-step. Handlers are sync: FastAPI runs them in a
thread pool, so a delayed request does not block /calls.
"""

import hashlib
import json
import math
import time
import uuid
from collections import Counter
from typing import Any

from fastapi import Body, FastAPI

from processes.support.schemas import EMBED_DIM

app = FastAPI(title="mock-llm")
PORT = 9000
CALLS: Counter[str] = Counter()
DELAY: dict[str, float] = {"draft": 0.0}

REFUND_WORDS = ("refund", "money back", "rückerstattung")
TECH_WORDS = ("sensor", "controller", "camera", "offline", "firmware")


def _text(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts += [p.get("text", "") for p in content if isinstance(p, dict)]
    return "\n".join(parts).lower()


def _ticket_text(messages: list[dict[str, Any]]) -> str:
    users = [m for m in messages if m.get("role") == "user"]
    return _text(users[-1:]) if users else ""


def _tool_call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "mock",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"call_{uuid.uuid4().hex[:12]}",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(args, ensure_ascii=False),
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160},
    }


SAFETY_WORDS = ("smoke", "unsafe", "sparks", "fire")


def _classify(ticket: str) -> dict[str, Any]:
    base: dict[str, Any] = {
        "urgency": "normal",
        "language": "en",
        "safety_risk": False,
        "outcome": "reply",
        "escalate_to": None,
    }
    if any(w in ticket for w in REFUND_WORDS):
        return {
            **base,
            "topic": "refund",
            "route": "commercial",
            "outcome": "escalate",
            "escalate_to": "sales",
        }
    if any(w in ticket for w in SAFETY_WORDS):
        return {
            **base,
            "topic": "malfunction",
            "route": "technical",
            "urgency": "high",
            "safety_risk": True,
            "outcome": "escalate",
            "escalate_to": "service",
        }
    if any(w in ticket for w in TECH_WORDS):
        return {**base, "topic": "malfunction", "route": "technical", "urgency": "high"}
    return {**base, "topic": "how_to", "route": "technical"}


COMMERCIAL_WORDS = (
    "refund",
    "money back",
    "invoice",
    "payment",
    "price",
    "delivery",
    "deliver",
    "ship",
    "warranty",
    "quote",
    "manager",
    "complain",
    "rechnung",
    "lieferung",
    "zahlung",
    "preis",
)
ESCALATE_WORDS = ("refund", "money back", "complain", "manager", "smoke", "unsafe", "custom quote")


def synth_text(messages: list[dict[str, Any]]) -> str:
    """Deterministic 'email' for the synth model: greeting, the facts verbatim, signature."""
    user = str(messages[-1].get("content", ""))
    facts = [line[2:] for line in user.splitlines() if line.startswith("- ")]
    german = "Language: German" in user
    signer = next(
        (
            line.split(":", 1)[1].strip(" .")
            for line in user.splitlines()
            if line.startswith("Sign as:")
        ),
        "A customer",
    )
    greeting, closing = ("Guten Tag,", "Viele Grüße") if german else ("Hello,", "Best regards")
    subject = " ".join((facts[0] if facts else "Question").split()[:6])
    return f"Subject: {subject}\n\n{greeting}\n{' '.join(facts)}\n\n{closing}\n{signer}"


def check_json(messages: list[dict[str, Any]]) -> str:
    """Deterministic checker: keyword rules over the text only (it never sees labels)."""
    text = str(messages[-1].get("content", "")).lower()
    route = "commercial" if any(w in text for w in COMMERCIAL_WORDS) else "technical"
    if any(w in text for w in ESCALATE_WORDS):
        outcome = "escalate"
    elif "ord-" not in text:  # no order context: the planner left the needed fact out
        outcome = "clarify"
    else:
        outcome = "reply"
    if "refund" in text or "money back" in text:
        topic = "refund"
    elif any(w in text for w in ("invoice", "payment", "price", "rechnung")):
        topic = "billing"
    elif any(w in text for w in ("delivery", "deliver", "ship", "lieferung")):
        topic = "delivery"
    elif any(
        w in text for w in ("offline", "smoke", "black", "restart", "false alarm", "too high")
    ):
        topic = "malfunction"
    else:
        topic = "how_to" if route == "technical" else "other"
    return json.dumps(
        {
            "topic": topic,
            "route": route,
            "urgency": "high" if "smoke" in text or "urgent" in text else "normal",
            "language": "de" if "guten tag" in text else "en",
            "outcome": outcome,
        }
    )


def judge_json(messages: list[dict[str, Any]]) -> str:
    """Deterministic judge: a non-empty reply passes every criterion."""
    text = str(messages[-1].get("content", ""))
    reply = text.split("Reply:", 1)[1].split("\n\n", 1)[0].strip() if "Reply:" in text else ""
    ok = bool(reply)
    return json.dumps({"answers": ok, "grounded": ok, "language": ok, "safe": ok, "note": "mock"})


def _message(text: str) -> dict[str, Any]:
    """A plain assistant completion."""
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "mock",
        "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}
        ],
        "usage": {"prompt_tokens": 50, "completion_tokens": 50, "total_tokens": 100},
    }


STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "can",
        "do",
        "does",
        "for",
        "from",
        "has",
        "have",
        "how",
        "i",
        "in",
        "is",
        "it",
        "its",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "please",
        "re",
        "so",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "this",
        "to",
        "under",
        "up",
        "we",
        "what",
        "when",
        "with",
        "you",
        "your",
    ]
)


def _stem(word: str) -> str:
    """A crude stem, enough for a mock: pairing, paired, pairs → pair."""
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:  # noqa: PLR2004 - a root
            return word[: -len(suffix)]
    return word


def _words(text: str) -> set[str]:
    """The stems of a text's content words: hyphens split, stopwords dropped."""
    raw = text.replace("-", " ").replace("/", " ").split()
    words = {w.strip(",.;:!?'\"()[]").lower() for w in raw}
    return {_stem(w) for w in words if w and w not in STOPWORDS}


def _best_article(ticket: str, tool_text: str) -> tuple[str, str, str] | None:
    """The search result sharing the most words with the ticket: (id, title, body).

    search_kb answers `id: title — body` lines; a tie keeps the search's own order.
    """
    articles = []
    for line in tool_text.splitlines():
        head, _, body = line.partition(" — ")
        article_id, _, title = head.partition(": ")
        if article_id.startswith("kb-"):
            articles.append((article_id, title, body))
    if not articles:
        return None
    asked = _words(ticket)
    return max(articles, key=lambda a: len(asked & _words(f"{a[1]} {a[2]}")))


@app.post("/v1/chat/completions")
def chat(body: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008 - FastAPI idiom
    """Classifier: one tool call with the classification. Drafter: search, then answer."""
    messages: list[dict[str, Any]] = body.get("messages", [])
    if body.get("model") == "mock-synth":
        return _message(synth_text(messages))
    if body.get("model") == "mock-check":
        return _message(check_json(messages))
    if body.get("model") == "mock-judge":
        return _message(judge_json(messages))
    tools = {t["function"]["name"] for t in body.get("tools", [])}
    ticket = _ticket_text(messages)
    has_tool_result = any(m.get("role") == "tool" for m in messages)

    if "search_kb" in tools and not has_tool_result:
        CALLS["draft_search"] += 1
        if DELAY["draft"]:
            time.sleep(DELAY["draft"])
        return _tool_call("search_kb", {"query": ticket[:120]})

    if "search_kb" in tools:
        CALLS["draft_final"] += 1
        tool_text = "\n".join(
            str(m.get("content", "")) for m in messages if m.get("role") == "tool"
        )
        article = _best_article(ticket, tool_text)
        if article is None:
            reply, citations = "Thank you for reaching out. We will look into it.", []
        else:
            article_id, title, steps = article
            reply = f"Thank you for reaching out. {title}: {steps}"
            citations = [article_id]
        return _tool_call("final_result", {"reply": reply, "citations": citations})

    CALLS["classify"] += 1
    return _tool_call("final_result", _classify(ticket))


def _embed(text: str, dim: int = EMBED_DIM) -> list[float]:
    # The same content words as _best_article: "device?" is "device", and "how do I" is nothing.
    vector = [0.0] * dim
    for token in sorted(_words(text)):
        h = int(hashlib.sha256(token.encode()).hexdigest(), 16)
        vector[h % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


@app.post("/v1/embeddings")
def embeddings(body: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008 - FastAPI idiom
    """Bag-of-words vectors: similar texts get similar vectors, deterministically."""
    inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
    CALLS["embeddings"] += len(inputs)
    return {
        "object": "list",
        "model": body.get("model", "mock-embed"),
        "data": [
            {"object": "embedding", "index": i, "embedding": _embed(str(t))}
            for i, t in enumerate(inputs)
        ],
        "usage": {"prompt_tokens": 8 * len(inputs), "total_tokens": 8 * len(inputs)},
    }


@app.get("/calls")
def calls() -> dict[str, int]:
    """Calls per kind since the last reset."""
    return dict(CALLS)


@app.post("/calls/reset")
def reset() -> dict[str, int]:
    """Forget the counters."""
    CALLS.clear()
    return {}


@app.post("/delay/{kind}/{seconds}")
def set_delay(kind: str, seconds: float) -> dict[str, float]:
    """Slow down one kind of call (`draft`: the drafter's first request)."""
    DELAY[kind] = seconds
    return DELAY
