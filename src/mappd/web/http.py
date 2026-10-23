"""HTTP pieces every process API shares: the operator, the idempotency header, the 409 body."""

from typing import Annotated, NoReturn

from fastapi import Header, HTTPException, Response
from pydantic import BaseModel, StringConstraints, TypeAdapter

MAX_KEY = 200

Operator = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
"""Who decided: a name, never blank. Every decision body's `operator` field is one."""

OPERATOR: TypeAdapter[str] = TypeAdapter(Operator)


class Conflict(BaseModel):
    """What a 409 carries: enough to refetch without guessing."""

    detail: str
    current_version: int
    status: str


async def idempotency_key(
    idempotency_key: Annotated[str | None, Header()] = None,
) -> str | None:
    """The optional `Idempotency-Key` header: 1-200 printable ASCII characters."""
    if idempotency_key is None:
        return None
    if not 0 < len(idempotency_key) <= MAX_KEY or not (
        idempotency_key.isascii() and idempotency_key.isprintable()
    ):
        raise HTTPException(422, "Idempotency-Key must be 1-200 printable ASCII characters")
    return idempotency_key


class ConflictError(HTTPException):
    """A 409 whose body is the flat `Conflict` (the app's error handler renders it)."""

    def __init__(self, body: Conflict) -> None:
        super().__init__(409, body.detail)
        self.body = body


def conflict(detail: str, current_version: int, status: str) -> NoReturn:
    """Raise the 409 for a decision on a stale version or a case that moved on."""
    raise ConflictError(Conflict(detail=detail, current_version=current_version, status=status))


DONE = frozenset({"sent", "shadowed", "escalated", "resolved", "repeated"})


def htmx_decided[R: Response](response: R, state: str, kind: str) -> R:
    """Steer htmx after a decision form's answer (the body goes to `#result` by default).

    A decision that went through reloads the page, so it shows the case's new status and no
    stale form. An invalid form replaces the form's own fields (`#fields-<kind>`), so the
    operator corrects them where the errors are, and submits those.
    """
    if state in DONE:
        response.headers["HX-Refresh"] = "true"
    elif state == "invalid":
        response.headers["HX-Retarget"] = f"#fields-{kind}"
        response.headers["HX-Reswap"] = "innerHTML"
    return response
