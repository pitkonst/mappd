---
name: dataset
description: Build or extend a MAPP-D process's labelled eval data — synthetic cases from world.yaml, imported real history, the dev/holdout split and the canary. Use when a process has no eval set yet, needs more or harder cases, or real history should become cases.
---

# Dataset

Labels come from code, text from a model: `world.yaml` defines the fictional company
(catalog, customers, policies, knowledge base), each process's `data/synth.py` expands it
into cases with known answers, and the gateway's `synth` model writes the text while the
`check` model must read the labels back.

## Synthetic cases

1. Plan first, no model calls:

   ```bash
   uv run mappd data synth <name> --seed 42 --n 300 --dry-run
   ```

   Check the mix of outcomes; set it explicitly with `--mix reply=.5,clarify=.2,escalate=.3`
   and include traps (refunds, complaints, questions outside the knowledge base).

2. Generate (needs the stack and the `synth` and `check` routes in the gateway):

   ```bash
   uv run mappd data synth <name> --seed 42 --n 300
   ```

   Responses are cached in `.mappd/cache` by prompt, model and seed, so a re-run is cheap.
   A case is kept only when the checker recovers its labels.

3. Split, with a seed of its own:

   ```bash
   uv run mappd data split <name> --holdout 0.2 --seed 7
   ```

   `dev.jsonl` is committed; `holdout.jsonl` is git-ignored and carries the process's canary
   (`evals/canary.txt`). Cases are grouped by persona, so a persona never crosses the split.

## Real history

```bash
uv run mappd data import <name> history.csv --map external_id=id,sender=from,body=text
uv run mappd data split <name> --from imported --holdout 0.2 --seed 7
```

Real text never goes into fixtures, mocks or committed eval sets (AGENTS.md conventions).

## Rules

- Never read `holdout*.jsonl`, `evals/synth.jsonl` (it contains the holdout before the
  split), `evals/imported.jsonl` or `.mappd/cache`; never copy from them.
- `mappd data check-canary` runs in CI and pre-commit; a hit means holdout text reached a
  tracked file — remove it, do not edit the canary.
- Generate with a model from a different family than the one the process uses to solve.
- Review 20–30 generated cases by hand from the **dev** set before relying on the numbers.
