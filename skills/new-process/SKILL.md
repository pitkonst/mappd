---
name: new-process
description: Add a new business process to MAPP-D by copying the closest demo (support or rfq for pipelines, installation for cases). Use when asked to automate a new process, add a workflow for a new kind of request, or start a folder under processes/.
---

# New process

A process is one folder under `processes/` (layout: copy `processes/support/`). There is no
template generator: copy the closest demo and rename it.

## 1. Pick the shape and the demo

| Shape | When | Copy |
| --- | --- | --- |
| Pipeline | one input in, one decision out, a person approves, an action runs | `processes/support` (text reply) or `processes/rfq` (structured list) |
| Case | long-lived state, many messages, a state machine | `processes/installation` |

## 2. Describe before coding

Write `processes/<name>/process.md` first, following the demo's sections: input, decisions,
**data ownership** (name the system of record), **hard rules** (they become code, never
prompts — AGENTS.md rule 6), actions, evals, API, autonomy.

Then define the evals and their thresholds in `evals/config.yaml` before the agent exists:
metrics against fields of the output schema, plus one for escalation and routing. See the
`eval-loop` and `dataset` skills.

## 3. Copy and rename

1. `cp -r processes/<demo> processes/<name>`; rename every `<demo>` identifier, table name,
   env var (`MAPPD_<DEMO>_*`), route prefix and template folder. `grep -rn <demo>` must come
   back empty.
2. Delete what the new process does not need (mail, Telegram, trust page, mocks).
3. Migrations: rename files to `<name>_0001_initial.py`, set `revision = "<name>_0001"`,
   `branch_labels = ("<name>",)`, `depends_on = "mappd_0001"`. Keep `db.py` in sync — see
   the `migration` skill.
4. Workflow and step names derive from the module (`mappd.durable`), so renaming the folder
   renames them; nothing to edit by hand.

## 4. Declare it on `Process`

In `processes/<name>/__init__.py`:

- `role=` — who may mutate (`operator`, `dispatcher`, `admin`); `public=` — exact paths that
  skip sign-in, only for webhooks that verify their own secret.
- `cases=Cases(table=..., link=..., waiting=..., passing=...)` — so the case appears in
  `/ops` (`src/mappd/ops.py`).
- `webhooks=`, `mailboxes=`, `schedules=`, `autonomy=`, `evals=`, `synth=`, `seed=` as needed.

Register it in `processes/__init__.py` (the `PROCESSES` tuple).

## 5. Check

```bash
uv run mappd db migrate
uv run mappd process list
uv run mappd doctor
uv run mappd process run <name> --input case.json
uv run pytest processes/<name>
```

Every step and endpoint ships with tests (AGENTS.md rule 1); copy the demo's `tests/` and
adapt them. A new process must need **no core change**; if it
does, stop and say so — that is a finding about the core, not a workaround to write.
