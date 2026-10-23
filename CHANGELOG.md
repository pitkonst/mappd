# Changelog

All notable changes to the MAPP-D boilerplate. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [0.0.1] - 2026-10-23

The first release: pre-alpha. The core, the CLI and the three demo processes run and pass
their evals and acceptance tests on the local stack.

### Added

- **The stack** in `compose.yaml`: PostgreSQL 17 + pgvector, MLflow (traces over
  OpenTelemetry), agentgateway (every model call goes through it; provider keys live only
  there) and GreenMail. `compose.app.yaml` adds the app image, the migrations and the mocks.
- **Core runtime** (`src/mappd/`): settings from `MAPPD_*` variables and `.env`; workflow IDs
  from stable keys (`mappd.ids`); DBOS workflows, steps and transactions named
  `<process>.<function>` (`mappd.durable`); Alembic migrations for the core and each process
  with an expand-only linter; optimistic locking; one trace per workflow; an app factory that
  discovers processes.
- **Actions run once**: `@action` records its intent in the `actions` outbox before acting;
  `on_unknown="escalate"` parks a send whose outcome is unknown for a person instead of
  repeating it, and `mappd action resolve` settles it.
- **Processes as folders** (`processes/<name>/`), declared with `Process(...)`: routes,
  pages, schedules, mailboxes, webhooks, autonomy, cases, evals and mocks are wired with no
  core change.
- **Three demos to copy from**: `support` (email in, classified ticket, drafted reply,
  approval, threaded reply out; refunds always escalate), `rfq` (requests for quotation into
  priced catalog lines, prices computed in code) and `installation` (a case over Telegram
  with MCP-backed warehouse, logistics, ticket and contact systems).
- **Mail channel**: IMAP polling per mailbox with a cursor, threading, attachments, and the
  `mappd.send_email` action.
- **Integrations**: `/api/v1` before every page (the HTMX pages call the same service
  layer), an OpenAPI spec, a JSON Schema per process output, signed webhooks redelivered from
  the outbox for 24 hours, an SSE stream, idempotency keys.
- **Autonomy per category**: shadow mode, review, then `auto` one category at a time from a
  reviewed `autonomy.yaml`; a trust dashboard; hard rules override every level.
- **Evals**: metrics and thresholds per process (`exact_match`, `set_f1`, `rate`, an LLM
  judge, a simulated dialogue for cases), keyword baselines, runs in MLflow and run-to-run
  comparison with flipped cases.
- **Eval data**: `world.yaml` (the demos' fictional company) expanded into labelled cases
  with a read-back check, imports from real history, a grouped dev/holdout split, holdout
  canaries checked in pre-commit and CI, and operator corrections turned into eval cases.
- **Operations**: `/ops` (cases waiting or stuck, `unknown` and `failed` actions, failing
  webhooks) and `/workflows` (counts, timelines with outbox rows and traces; cancel, resume,
  retry, recover and fork from a step).
- **Authentication**: off by default; `jwt` (built-in users and roles) or `header` (behind
  an SSO proxy); each process declares its mutating role. See `docs/auth.md`.
- **The `mappd` CLI**, non-interactive with `--json` on every command: `doctor`, `serve`,
  `mock`, `db migrate|lint|backup|restore`, `process list|run|seed|shadow|autonomy`,
  `workflow list|show|recover|retry|resume|fork|cancel`, `action list|resolve`,
  `user add|list|passwd|roles|disable|enable`, `mail status|inject`,
  `data synth|import|split|check-canary`, `eval run|baseline|compare`, `feedback sync`,
  `model list|try`.
- **CI and deploys**: GitHub Actions runs pre-commit (ruff, strict mypy, migration lint, the
  canary check) and the tests that need no services; pushes to `main` build
  `mappd-app:<sha12>` and `mappd-mlflow:<sha12>` in GHCR. `deploy/deploy.sh` deploys a SHA to
  a server over SSH; a rollback is a deploy of an older SHA. The GitHub Deploy workflow is an
  example, skipped until the repository variable `DEPLOY_ENABLED` is `true`. See
  `docs/deploy.md`.
- **For coding agents**: `AGENTS.md` (hard rules and conventions), task guides in `skills/`
  checked by a test, `docs/process-guide.md`, and the holdout fenced off with
  `.cursorignore` and `.aiderignore`.

### Known limitations

- No production perimeter yet: TLS, backups on a schedule, alerts.
- Single node: DBOS's open-source core has no cross-node recovery.
