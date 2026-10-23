"""Deterministic stand-in for the installation agents and the vision model, on the shared port.

`claims` tells the core router which requests are the installation process's: the
interpreter's and the resolver's (their prompts' first lines), the vision model's (its
instruction) and control paths under /installation/. Keyword rules decide the interpretation;
the resolver follows its prompt's rules one tool call at a time.
"""

import json
import time
import uuid
from collections import Counter
from typing import Any

from fastapi import Body, FastAPI

app = FastAPI(title="mock-llm-installation")
CALLS: Counter[str] = Counter()
INTERPRETER_MARK = "You read messages from installation technicians"
RESOLVER_MARK = "You resolve problems installation technicians report"
SIMULATOR_MARK = "You play an installation technician"
RESOLVER_REPLY = (
    "Thanks, I have checked what we can do and passed it to your dispatcher,"
    " who will confirm the next step."
)
VISION_MARK = "Describe this photo for a dispatcher"  # the start of vision.PROMPT
DESCRIPTION = "A photo of an installed controller on a wall, its status light green."
EXCEPTIONS = {
    "missing": "missing_parts",
    "broken": "breakage",
    "damaged": "breakage",
    "no access": "no_access",
    "locked": "no_access",
}
PROGRESS = ("arrived", "access", "granted", "found", "installed", "tested", "done", "handed")
# The reply to a step reached, by the step: what is noted and what to send next.
NOTED = {
    "arrived": "Noted, you are on site. Tell me once you have access.",
    "access": "Noted, you have access. Tell me when you have found the equipment.",
    "equipment_found": "Noted, the equipment is there. Send a photo once it is installed.",
    "installed": "Noted, it is installed. Send a photo once it is tested.",
    "tested": "Noted, it is tested. Tell me when you have handed it over.",
    "handed_over": "Noted, handed over. The job is closed, thank you.",
}


def _text(body: dict[str, Any]) -> str:
    parts: list[str] = []
    for m in body.get("messages", []):
        content = m.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts += [p.get("text", "") for p in content if isinstance(p, dict)]
    return "\n".join(parts)


def claims(body: dict[str, Any]) -> bool:
    """Whether a request is the installation process's."""
    if str(body.get("__path__", "")).startswith("/installation/"):
        return True
    text = _text(body)
    marks = (INTERPRETER_MARK, RESOLVER_MARK, SIMULATOR_MARK, VISION_MARK)
    return any(mark in text for mark in marks)


def _message(text: str) -> str:
    for line in _text({"messages": [{"content": text}]}).splitlines():
        if line.startswith("Message:"):
            return line.removeprefix("Message:").strip().lower()
    return text.lower()


def _next_step(text: str) -> str:
    for line in _text({"messages": [{"content": text}]}).splitlines():
        if line.startswith("Next step:"):
            return line.removeprefix("Next step:").strip()
    return ""


def interpret(text: str) -> dict[str, Any]:
    """An interpretation by keywords over the technician's message."""
    message = _message(text)
    for word, kind in EXCEPTIONS.items():
        if word in message:
            return {
                "intent": "exception",
                "reached_next": False,
                "exception": kind,
                "reply": "Thanks, a dispatcher will look into it.",
            }
    if any(word in message for word in PROGRESS):
        return {
            "intent": "progress",
            "reached_next": True,
            "exception": None,
            "reply": NOTED.get(_next_step(text), "Noted, thank you."),
        }
    return {
        "intent": "question",
        "reached_next": False,
        "exception": None,
        "reply": "A dispatcher will answer shortly.",
    }


def _completion(message: dict[str, Any], finish: str = "stop") -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "mock",
        "choices": [{"index": 0, "finish_reason": finish, "message": message}],
        "usage": {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100},
    }


@app.post("/v1/chat/completions")
def chat(body: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008 - FastAPI idiom
    """The vision description, or the interpreter's final answer."""
    text = _text(body)
    if VISION_MARK in text:
        CALLS["vision"] += 1
        return _completion({"role": "assistant", "content": DESCRIPTION})
    if SIMULATOR_MARK in text:
        CALLS["simulate"] += 1
        return _completion({"role": "assistant", "content": planned_message(text)})
    if RESOLVER_MARK in text:
        CALLS["resolve"] += 1
        return resolver_step(body)
    CALLS["interpret"] += 1
    users = [m for m in body.get("messages", []) if m.get("role") == "user"]
    user = str(users[-1].get("content") or "") if users else text
    call = {
        "id": f"call_{uuid.uuid4().hex[:12]}",
        "type": "function",
        "function": {"name": "final_result", "arguments": json.dumps(interpret(user))},
    }
    return _completion({"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls")


def planned_message(text: str) -> str:
    """The simulated technician says the script's line as written (and mentions a photo)."""
    planned, photo = "", False
    for line in text.splitlines():
        if line.startswith("Planned message: "):
            planned = line.removeprefix("Planned message: ").strip()
        elif line.startswith("Photo with this message: "):
            photo = line.endswith("yes")
    return f"{planned} (photo attached)" if photo else planned


def _fields(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(": ")
        if sep and key in {"Job", "Site", "Equipment", "Last step done", "Problem"}:
            out[key] = value.strip()
    return out


def _results(body: dict[str, Any]) -> dict[str, Any]:
    """Tool results so far, by tool name (the run's assistant calls paired with tool messages)."""
    names: dict[str, str] = {}
    out: dict[str, Any] = {}
    for m in body.get("messages", []):
        for call in m.get("tool_calls") or []:
            names[call["id"]] = call["function"]["name"]
        if m.get("role") == "tool" and m.get("tool_call_id") in names:
            try:
                out[names[m["tool_call_id"]]] = json.loads(m.get("content") or "null")
            except ValueError:
                out[names[m["tool_call_id"]]] = m.get("content")
    return out


def _call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    call = {
        "id": f"call_{uuid.uuid4().hex[:12]}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }
    return _completion({"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls")


def resolver_step(body: dict[str, Any]) -> dict[str, Any]:  # noqa: PLR0911 - one per prompt rule
    """The resolver's next move: one tool call, or the final resolution."""
    users = [m for m in body.get("messages", []) if m.get("role") == "user"]
    f = _fields(str(users[0].get("content") or "")) if users else {}
    job, site, problem = f.get("Job", ""), f.get("Site", ""), f.get("Problem", "other")
    sku = (f.get("Equipment", "").split(", ") or [""])[0]
    done = _results(body)
    summary = f"{problem} at {site} ({sku})."
    if problem == "no_access":
        if "site_contact" not in done:
            return _call("site_contact", {"site": site})
        c = done["site_contact"]
        reply = f"Please call {c.get('name')} at {c.get('phone')}. {c.get('access', '')}".strip()
        return _call("final_result", {"summary": summary, "reply": reply, "proposals": []})
    if problem not in {"missing_parts", "breakage"}:
        return _call("final_result", {"summary": summary, "reply": RESOLVER_REPLY, "proposals": []})
    if problem == "breakage" and "open_ticket" not in done:
        return _call("open_ticket", {"job": job, "kind": "breakage", "text": f"{sku} damaged"})
    if "check_stock" not in done:
        return _call("check_stock", {"sku": sku})
    proposals: list[dict[str, Any]] = []
    on_hand = (done["check_stock"] or {}).get("on_hand", 0)
    if on_hand > 0:
        args: dict[str, Any] = {"skus": [sku]}
        reason = f"a replacement {sku}: {on_hand} in stock"
        proposals.append({"tool": "reserve_parts", "arguments": args, "reason": reason})
    elif "delivery_slots" not in done:
        return _call("delivery_slots", {"site": site, "skus": [sku]})
    elif slots := (done["delivery_slots"] or {}).get("slots"):
        args = {"slot": slots[0]}
        reason = f"{sku} is out of stock: deliver it"
        proposals.append({"tool": "book_delivery", "arguments": args, "reason": reason})
    return _call(
        "final_result", {"summary": summary, "reply": RESOLVER_REPLY, "proposals": proposals}
    )


@app.get("/installation/calls")
def calls() -> dict[str, int]:
    """Calls per kind since the last reset."""
    return dict(CALLS)


@app.post("/installation/calls/reset")
def reset() -> dict[str, int]:
    """Forget the counts."""
    CALLS.clear()
    return {}
