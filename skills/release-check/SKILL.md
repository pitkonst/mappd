---
name: release-check
description: Verify a MAPP-D change before merging to main or deploying — lint, types, migrations, canary, the three test suites, what CI runs, then the deploy and rollback steps. Use before claiming work is done, before a merge, and before running a deploy.
---

# Release check

Claim "done" only with the output of these commands in hand.

## 1. Fast loop (no services)

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run mappd db lint
uv run mappd data check-canary
uv run pytest -m "not pg and not acceptance"
```

## 2. With Postgres and the stack

```bash
docker compose up -d --wait
uv run pytest -m "pg and not acceptance"
uv run pytest -m acceptance          # the stack scenarios; minutes, needs Docker
```

The Postgres and acceptance suites are not in CI: they are this checklist. A change to a
process's agent also needs its evals (the `eval-loop` skill).

## 3. CI as it will run

CI runs exactly this on the runner:

```bash
uv run pre-commit run --all-files && uv run pytest -m "not pg and not acceptance"
```

## 4. Merge

Commit messages are one imperative line, no body and no trailers (AGENTS.md). Pushes to
`main` run CI and build the images tagged with the commit.

## 5. Deploy and roll back

Deploys are manual: `deploy/deploy.sh` or the Deploy workflow,
one SHA per release; a rollback is a deploy of an older SHA (migrations are expand-only —
the `migration` skill). After a deploy, `mappd doctor` on the server lists stranded
workflows; recover them with `mappd workflow recover` (the `workflow-debug` skill).
Deploying is outward-facing: run it only when a person asked for it.
