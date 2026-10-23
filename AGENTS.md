# AGENTS.md

Instructions for coding agents working in this repository. Humans: these are the project's rules too.

## Read first

- `docs/process-guide.md` — building a process end to end: describe, data, build, evals, deploy, autonomy, changes.
- `docs/auth.md` — authentication: modes, the policy, the Backend protocol.
- `docs/deploy.md` — CI, images, deploying a commit to a server, rollback.
- `skills/` — step-by-step guides for the common tasks (new process, eval loop, side effects, migrations, datasets, workflow debugging, release check); read the matching `SKILL.md` before starting one.

## Layout

- `src/mappd/` — the core and the `mappd` CLI: `durable` (workflow/step/transaction/action), `actions` (the outbox), `ids`, `db` (migrations, lint), `locking`, `runtime`, `process` and `autonomy`, `eval/` and `data/` (evals, synthetic data), `feedback`, `channels/` and `mail`, `web/` (incl. `web/auth.py`), `ops`, `users`, `telemetry`, `settings`, `mocks/`, `cli/`, `app`.
- `tests/` — package tests; `uv run pytest` from the repo root runs them.
- `deploy/`, `compose.yaml` — the stack (infrastructure only: Postgres, MLflow, agentgateway, GreenMail). `Dockerfile` is the app image; `compose.app.yaml` adds it for deploys and `tests/acceptance/` (run both files together: `-f compose.yaml -f compose.app.yaml`); `compose.deploy.yaml` is the server overlay (built images, `dev`-profiled GreenMail, mail hosts from `.env`). `deploy/deploy.sh` deploys a SHA to a server over SSH; `.github/workflows/deploy.yml` is an example that stays skipped until the repository variable `DEPLOY_ENABLED` is `true`.
- `.env` — every stack parameter lives here (see `.env.example`); compose and the `mappd` CLI both read it.
- `world.yaml` — the curated world of the demos; `mappd data synth` expands it into labelled cases.
- `processes/<name>/` — processes. Start a new one by copying the closest demo (`support` and `rfq` are pipelines, `installation` is a case) and renaming it; register it in `processes/__init__.py` (the layout is that of `processes/support/`).

## Commands

```bash
uv sync                                 # .venv with the package and dev tools
docker compose up -d --wait             # the stack once; then only `mappd` (postgres alone: `... postgres`)
uv run mappd db migrate                 # schema into the dev databases
uv run mappd process seed support       # knowledge base; `mappd mock --all` (or one: `mappd mock llm`), `mappd serve` run locally
uv run mappd action list --status unknown   # the outbox; settle with `mappd action resolve`
uv run mappd user add alice --role operator --password-stdin   # sign-in users
uv run mappd mail status                # mailboxes (GreenMail in the stack); `mappd mail inject support --from … --subject …`
uv run mappd workflow show <id>         # one workflow's steps and outbox rows; `fork <id> --step N`, `resume`, `retry`; the same at /workflows
uv run mappd process autonomy support how_to auto   # edits autonomy.yaml; commit the diff
uv run mappd feedback sync support          # decisions → evals/feedback.jsonl (git-ignored)
uv run pytest -m "not pg and not acceptance"   # fast loop: no Postgres, no stack (plain `not pg` would still run the acceptance suite)
uv run pre-commit install               # lint/format/typecheck on commit
uv run pytest                           # package tests
uv run pytest --cov                     # tests + coverage
uv run ruff check --fix .               # lint
uv run ruff format .                    # format
uv run mypy                             # type check (strict)
uv run pre-commit run --all-files       # lint, format, types, migration lint, canary — everything above except tests
uv run mappd doctor --json              # one check per MAPP-D layer
uv run mappd --help                     # every command has --json and an example
uv run mappd data synth support --seed 42 --n 60 --dry-run   # plan a dataset
uv run mappd eval run support --limit 20   # evals on the dev set, results in MLflow (needs the stack)
uv run mappd eval compare support          # the last two runs: deltas, regressions, flipped cases
uv run mappd eval run rfq                  # the RFQ process: field-comparison evals
uv run mappd eval run installation         # dialogue simulation: a simulated technician per scenario
uv run pytest -m acceptance             # the stack scenarios (docker; a few minutes)
uv run pre-commit run --all-files && uv run pytest -m "not pg and not acceptance"   # what CI runs; no stack needed
MAPPD_DEPLOY_REGISTRY=ghcr.io/<org> deploy/deploy.sh <host> <sha>   # deploy a commit to a server
```

## Hard rules

1. **No code without tests.** Every change ships with tests; nothing reaches production without coverage.
2. **Every side effect is its own durable step.** Anything that changes the outside world (email, external API write, warehouse request) is a separate durable step (`mappd.durable.step`) carrying an idempotency key the receiver honors. For receivers without keys, use the `actions` outbox table. Pydantic AI function tools are **not** DBOS steps: a mutating tool must call a durable step. Use `mappd.actions.action`: it is that step, with the outbox built in. Pass `on_unknown="escalate"` when the receiver cannot deduplicate by key, and raise `OutcomeUnknownError` when a call may have taken effect (a timeout after sending).
3. **Short workflows only.** No workflow waits for a person, a timer or an external reply. Waiting is a row in a table; each event starts a new short workflow.
4. **Workflow IDs come from stable unique keys** (UUID, Message-ID, Telegram `update_id`, decision ID + version). Never from auto-increment counters: DBOS silently replays an old journal on an ID collision. Build them with `mappd.ids.from_key` / `mappd.ids.decision`. DBOS workflows and steps are named "<process>.<function>": declare them with `mappd.durable.workflow` / `.step` / `.transaction` / `.action`, which derive the name from the module; pass `name=` only to keep journal compatibility across a deliberate rename. DBOS registers by name and refuses two functions with one name.
5. **Workflows started from FastAPI are `async def`.**
6. **Hard business rules are code**, not prompts (for example: refunds always escalate).
7. **Every action appears in the API before the UI.** The HTMX UI calls the same service layer as `/api/v1`.
8. **Never read holdout data.** Files named `holdout*.jsonl` are off limits, and `feedback*.jsonl` (real customers' text) is never committed; holdout eval runs report aggregate metrics only. Do not copy examples from them into prompts, code or few-shot sets. Every holdout case carries its process's canary (`processes/<name>/evals/canary.txt`); `mappd data check-canary` fails CI and pre-commit if a canary shows up in a tracked file, so a copied holdout case cannot hide.
9. **Licenses: OSI only.** Do not add a dependency under BSL, SSPL, Elastic, "fair-code" or another source-available license. Check before adding.
10. **No visual builder.** Processes are code.

## Conventions

- Python 3.14 (`requires-python` and `.python-version`); use current language features freely.
- Full type hints; `mypy --strict` must pass. Pydantic models for every process input and output.
- Ruff is the only linter and formatter; config is in `pyproject.toml`. Google-style docstrings on public API.
- Tests mirror the package: `src/mappd/<path>/<mod>.py` → `tests/<path>/test_<mod>.py`. No `__init__.py` in `tests/` (pytest runs with `--import-mode=importlib`); test file basenames are unique. Warnings are errors in tests.
- Dependencies: runtime in `[project].dependencies`, tooling in the `dev` group. Add with `uv add` / `uv add --dev`; commit `uv.lock`. CI runs `uv sync --locked`.
- Commit messages: one simple line, imperative mood (e.g. `Add the RFQ process`); no body, no trailers, no attribution (`Co-Authored-By`, session links, "Generated with").
- Never put real personal data in fixtures, mocks or eval sets — synthetic values only.
- Steps are pure functions with no knowledge of the runtime; `workflow.py` is a thin orchestration layer.
- The acting person on a decision body is a field named `operator`; with auth on, the middleware overwrites it with the signed-in user. A process declares its mutating role and public paths on `Process(role=, public=)`, and where its cases live on `Process(cases=Cases(...))` (waiting and passing statuses, for `/ops`).
- Queries are SQLAlchemy Core expressions run through `mappd.db.run` on the psycopg connection (no ORM `Session`). Each process declares its tables in `processes/<name>/db.py`; keep them in sync with the migrations. Migrations use `op.create_table`/`op.add_column` with explicit `sa.Column`s (never import `db.py` metadata into a migration — it must replay as written); `op.execute` only for DDL Core cannot express (e.g. `CREATE EXTENSION`) and for data backfills written as Core statements on a lightweight `sa.table`.
- App-DB writes inside a workflow are `@durable.transaction` steps (the write and its checkpoint commit together), and each guards its status transition so a replay matches no row. Reads stay plain `@durable.step`s. Durable verbs on sync functions are awaitables — the body runs on a worker thread, so callers `await` them and never reach for `to_thread`. Orchestration helpers that only arrange work (`solve`) stay plain functions: an `agent.run` on an agent built with `DBOSDurability` is already journaled per model call, and steps cannot nest.
- MAPP-D owns process state; external systems own their entities. Name the system of record in `process.md`; keep the agent's decision in one `output` JSONB validated by the process's output model.
- The CLI is non-interactive: flags instead of prompts, `--json` output, meaningful exit codes, detailed `--help` with examples.
- Model calls go through the gateway only; provider keys never appear in app code or config.
- One trace per workflow: HTTP clients that call the gateway are instrumented explicitly and passed to Pydantic AI.
