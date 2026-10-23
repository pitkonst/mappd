---
name: side-effect
description: Write code that changes the outside world (send an email or message, call an external API that writes, reserve stock, post a webhook) as a durable, idempotent MAPP-D action. Use when a workflow or an agent tool must act on another system.
---

# Side effects

DBOS runs a step **at least once**: a crash after the effect but before the checkpoint runs
it again. So every effect is its own durable step with an idempotency key, recorded in the
`actions` outbox (AGENTS.md rule 2; `src/mappd/actions.py` explains the three transactions).

## Write the action

```python
from mappd import durable
from mappd.actions import OutcomeUnknownError, PermanentError

@durable.action(per="order_id", on_unknown="escalate")
def reserve_stock(idempotency_key: str, order_id: str, sku: str, qty: int) -> dict[str, Any]:
    """Reserve stock once per order."""
    try:
        reply = warehouse.reserve(sku, qty, key=idempotency_key)
    except TimeoutError as e:  # sent, answer lost: it may have happened
        raise OutcomeUnknownError(str(e)) from e
    except Rejected as e:  # will never succeed
        raise PermanentError(str(e)) from e
    return {"reservation": reply.id}
```

- The first parameter is always `idempotency_key`; pass it on to receivers that honor one.
- Return a JSON object; it is stored and returned on every replay.
- Callers `await` it from a workflow.

## Choose the key

| Key | Deduplicates | Use for |
| --- | --- | --- |
| `per="param"` / `per(entity)` | one entity across every workflow | "one reply per ticket", "one reservation per order" |
| a business key string | exactly what you name | a key the receiver already knows |
| `AUTO` | one step inside one workflow | effects that may legitimately repeat in a new workflow |

A fork re-runs steps (`mappd workflow fork`): an `AUTO` action sends again, a business-keyed
one finds its row `done`. Prefer a business key.

## Choose `on_unknown`

- `"resend"` — the receiver deduplicates by the key you pass: an interrupted attempt is
  simply sent again.
- `"escalate"` — the receiver cannot deduplicate (plain SMTP, Telegram): an interrupted
  attempt becomes `unknown` and waits for a person (`mappd action resolve`).

## Rules that bite

- An agent's function tool is **not** a step. A mutating tool must call an action.
- Never act inside a `@durable.transaction` or a plain step; never hold a DB transaction
  open across the call (the action already commits its claim first).
- App-DB writes inside a workflow are `@durable.transaction` steps that guard their status
  transition (`WHERE status = 'approved'`), so a replay matches no row.
- In a pipeline the agent *proposes* actions in its output; the next workflow step runs them.
- Hard rules (refunds always escalate) are checked in code before the action, never left
  to a prompt.

## Test it

Copy the patterns in `tests/test_actions.py`: done once, replay returns the stored result,
a crash mid-call becomes `unknown` under `escalate`, a different payload on the same key is
refused. An acceptance scenario for a crash after sending lives in
`tests/acceptance/test_mail_stack.py`.
