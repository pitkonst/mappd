"""Workflow IDs built only from stable unique keys (AGENTS.md, rule 4).

DBOS treats the workflow ID as the identity of a run: starting a workflow with an ID it has
seen returns the old journal's results without executing anything. IDs
therefore come from keys that never repeat (UUID, Message-ID, Telegram update_id, a decision
with its version), never from counters that can restart.
"""

import hashlib
import re
from uuid import UUID

KIND = re.compile(r"[a-z][a-z0-9_.-]*")

type Key = str | UUID


def from_key(kind: str, key: Key) -> str:
    """Workflow ID for the event identified by `key`.

    Example:
        >>> from_key("ticket", "<m-1@example.com>")
        'ticket:<m-1@example.com>'
    """
    return f"{_kind(kind)}:{_part(key, 'key')}"


def decision(kind: str, subject: Key, version: int) -> str:
    """Workflow ID for a decision on `subject` at `version`: retries collapse into one run.

    Example:
        >>> decision("approve", "ticket-m-1", 2)
        'approve:ticket-m-1:v2'
    """
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise ValueError(f"version must be a non-negative int, got {version!r}")
    return f"{_kind(kind)}:{_part(subject, 'subject')}:v{version}"


def mail_case(message_id: str) -> str:
    """A case key for an email: `mail-` and 16 hex digits of the Message-ID's SHA-256.

    Stable (the same mail always gets the same key) and safe in URLs and keys, which a raw
    Message-ID (`<...@...>`, any printable character) is not. Keep the Message-ID itself
    with the case for threading.

    Example:
        >>> mail_case("<m-1@example.com>")
        'mail-573a6b02dad0aac7'
    """
    if not message_id:
        raise ValueError("message_id must not be empty")
    return "mail-" + hashlib.sha256(message_id.encode()).hexdigest()[:16]


def _kind(kind: str) -> str:
    if not KIND.fullmatch(kind):
        raise ValueError(f"kind must match {KIND.pattern}, got {kind!r}")
    return kind


def _part(value: object, name: str) -> str:
    if isinstance(value, UUID):
        return str(value)
    if not isinstance(value, str):
        raise TypeError(
            f"{name} must be a str or UUID, not {type(value).__name__}: "
            "counters can restart and replay an old journal"
        )
    if not value:
        raise ValueError(f"{name} must not be empty")
    return value.replace("%", "%25").replace(":", "%3A")
