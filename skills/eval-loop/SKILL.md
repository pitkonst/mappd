---
name: eval-loop
description: Change a prompt, a model or an agent in a MAPP-D process and measure the effect with evals. Use when editing anything under processes/*/agents/, a model route, or decision logic, and when asked whether a change made a process better or worse.
---

# Eval loop

Evals are the oracle: a change to an agent is done when the evals say so, not when the code
reads well. Needs the stack (`docker compose up -d --wait`) for
MLflow and the gateway.

## The loop

1. **Know the floor.** Once per process, if no baseline is logged:

   ```bash
   uv run mappd eval baseline <name>
   ```

2. **Measure before.** Run the dev set on the current code:

   ```bash
   uv run mappd eval run <name>              # --limit 20 for a quick look
   ```

3. **Make one change.** A prompt (`processes/<name>/agents/<agent>/prompt.md`), a model, or
   decision code. One change per run, so a delta has one cause.

4. **Measure after and compare:**

   ```bash
   uv run mappd eval run <name>
   uv run mappd eval compare <name>          # the last two runs
   ```

   Read the metric deltas, the regressions beyond tolerance (exit 1), and the **flipped
   cases** — the cases that changed verdict. Fix the cause, not the case.

5. Repeat 3–4 until every metric in `evals/config.yaml` meets its threshold.

## Holdout: once, at the end

```bash
uv run mappd eval run <name> --set holdout
```

Aggregates only, no cases. **Never read `holdout*.jsonl`**, and never copy text from it,
from `evals/synth.jsonl` or from `.mappd/cache` into a prompt, a few-shot example or code
(AGENTS.md rule 8). Running the holdout repeatedly to steer a change is the same leak by
other means: run it once per finished change.

## Operator feedback

`uv run mappd feedback sync <name>` turns operator decisions into `evals/feedback.jsonl`
(real customers' text: git-ignored, never committed); run them with
`uv run mappd eval run <name> --set feedback`.

## What not to do

- Do not lower a threshold to pass; a threshold change is a decision for a person, with the
  reason in the commit.
- Do not edit `dev.jsonl` labels to match the model; if a label is wrong, say so and fix it
  in its own commit.
- Hard business rules stay code (AGENTS.md rule 6); do not move them into a prompt to make a
  metric move.
