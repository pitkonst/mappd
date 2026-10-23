---
name: workflow-debug
description: Investigate and repair a stuck, failed or stranded MAPP-D workflow, a case that never moved on, or an outbox action in status unknown or failed. Use when a ticket/request/job is stuck, /ops or doctor reports something, or a workflow is in ERROR.
---

# Workflow debugging

Look before acting: every repair below runs code again, and some of it talks to customers.

## 1. Find it

```bash
uv run mappd doctor                         # stranded workflows; the `cases` line
uv run mappd workflow list --status error
uv run mappd workflow list --stranded
uv run mappd action list --status unknown
uv run mappd action list --status failed
```

In the UI (admin): `/ops` lists stuck cases with links, `/workflows` the workflows, `/actions`
the outbox. Workflow ids come from business keys (`support.ticket:<external_id>`), so a case
id finds its workflow.

## 2. Read it

```bash
uv run mappd workflow show <workflow_id>
```

Steps with kinds (action, transaction, child, step), the error, the outbox rows, and the
MLflow trace link. Find the failing step and the reason before choosing a repair.

## 3. Repair — pick by state

| State | Command | What re-runs |
| --- | --- | --- |
| ERROR | `mappd workflow retry <id>` | the failed step on; finished steps come from the journal |
| PENDING on an old version (stranded) | `mappd workflow recover` | each one after its last finished step, on the running version |
| CANCELLED | `mappd workflow resume <id>` | after its last recorded step, same id |
| finished, needs a re-run | `mappd workflow fork <id> --step N` | steps N on, under a new id |

Before a fork, check `show`'s outbox rows: actions keyed by a business key find their row
`done` and send nothing; actions keyed per workflow (`AUTO`) **send again**.

## 4. Unknown actions

An `unknown` row means an attempt may have taken effect (a timeout after sending) and the
receiver cannot deduplicate. **Check with the receiver first** (the sent folder, the
external system), then:

```bash
uv run mappd action resolve '<key>' --took-effect     # it happened: mark done
uv run mappd action resolve '<key>' --not-sent        # it did not: then retry the workflow
```

## Webhooks

Failing webhook deliveries are redelivered by the `mappd.webhook_redeliver` schedule for
24 hours; leave them alone. One given up (counted in `/ops`) is re-sent by forking the
delivery workflow named in its outbox row's `claimed_by`, from its send step.

## Never

- Never update `dbos.*` tables or an `actions` row by hand.
- Never start a workflow with an id that existed before: DBOS returns the old journal and
  runs nothing (AGENTS.md rule 4).
