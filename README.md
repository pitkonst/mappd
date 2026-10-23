# MAPP-D

An open stack for **reliable** business-process automation with LLMs, in the spirit of LAMP:
OSI-licensed components, one `docker compose` command, and three working demo processes to
copy from.

A process is code with a deterministic skeleton; agents work inside its steps; a person approves
what the policy says to approve; and every operator correction becomes an eval case. Two
guarantees sit at the center: **no approved action runs twice**, and **every change to a process
is measured by evals**.

> **Status: pre-alpha.** The core, the CLI and the three demos run and pass their evals and
> acceptance tests on the local stack.

## Quick start: run the demos

You need Docker, [uv](https://docs.astral.sh/uv/) and git. Docker runs only the stack —
Postgres, MLflow, the model gateway and a GreenMail mail server; the app and the mocks run on
your machine from the `mappd` CLI. No model key: the demos run on deterministic mock models, a
mock Telegram Bot API and mock warehouse systems.

```bash
git clone https://github.com/pitkonst/mappd.git && cd mappd
uv sync
cp .env.demo .env
```

`.env.demo` runs everything on mocks: mock models and systems behind the gateway, the
mailboxes on the stack's GreenMail, the Telegram bot polling its mock, and support's webhooks
going to a mock help desk. `.env.example` documents every setting.

Start the stack, then the mocks and the app:

```bash
docker compose up -d --wait            # Postgres, MLflow, agentgateway, GreenMail
uv run mappd db migrate                # the app's tables and DBOS's system schema

uv run mappd mock --all &              # models :9000, warehouse systems :9400, Telegram Bot API :9300, help desk :9200
uv run mappd serve &                   # the app  :8000

uv run mappd process seed support      # the knowledge base, embedded through the gateway
uv run mappd doctor                    # one line per layer, all ok (auth is off)
```

The first `docker compose up` builds the MLflow image, which takes about two minutes; later
starts take seconds.

Then open http://127.0.0.1:8000/support, `/rfq` and `/installation` for the operator queues,
`/ops` for what is waiting or stuck, `/actions` for the outbox (every side effect, and the ones
a person must settle), `/workflows` for every durable workflow, `/docs` for the API, and
http://127.0.0.1:5001 for traces in MLflow. Times on the pages are UTC. `kill %1 %2` stops the mocks and
the app; `docker compose down` stops the stack.

### Support: email in, approved reply out

```bash
uv run mappd mail inject support --from customer@mappd.test \
  --subject "Sensor offline" --body "The sensor in room 3 is offline. How do I reset it?"
```

The mailbox is polled every 30 seconds. The ticket appears at `/support` with a classified
topic, a route and a drafted reply; approve it there, and the threaded reply lands in
`customer@mappd.test` (GreenMail, IMAP on `127.0.0.1:3143`, password `mappd`). A refund request
escalates to sales whatever the model says: that rule is code. The signed webhooks a help desk
would receive are at http://127.0.0.1:9200/deliveries.

### RFQ: a request for quotation becomes priced lines

```bash
uv run mappd mail inject rfq --from buyer@mappd.test --subject "Quote request" \
  --body "Please quote 25 x SEN-T100 temperature sensors and 10 x CTL-H400 controllers."
```

The agent extracts the lines and matches them to the catalog; prices, totals and alternatives
for late delivery are computed in code. An item it cannot match turns the quote into a
clarifying question. Approve the quote at `/rfq`: you may add words to the reply, but its figures
stay as computed. Corrected lines, which re-price the quote, go through the API (`/docs`). The
quote lands in `buyer@mappd.test` (IMAP, password `mappd`).

### Installation: a case over Telegram

A dispatcher creates a job; the installer links their chat and reports progress; the process
moves the job through its states, and a problem on site blocks it until the dispatcher confirms
what the agent proposes.

```bash
RUN=$RANDOM  # each run is a new job, installer and chat: run the demo as often as you like
curl -s localhost:8000/api/v1/installation/jobs -H 'content-type: application/json' -d "{
  \"external_id\": \"job-$RUN\", \"installer_name\": \"Ben $RUN\", \"site\": \"Riverside 4\",
  \"skus\": [\"CTL-H400\"], \"appointment_at\": \"2026-11-02T09:00:00Z\"}"
# → "deep_link": "https://t.me/<bot>?start=<token>"

say() {  # the installer (chat $RUN) sends a message; the mock Bot API numbers the update
  curl -s localhost:9300/test/push -H 'content-type: application/json' \
    -d "{\"message\": {\"chat\": {\"id\": $RUN}, \"text\": \"$1\"}}"
}
say "/start <token>"
say "I have arrived"
say "access granted"
say "The controller is missing from the delivery"
```

Follow the job at `http://127.0.0.1:8000/installation/jobs/job-$RUN`: the timeline of messages
and transitions, the bot's replies (also at http://127.0.0.1:9300/test/sent), and the agent's
proposal to reserve a replacement controller. Type your name in the dispatcher field and
**Confirm** it: the reservation runs once and the installer is told. Confirming does not move
the job; **Unblock** does, back to the step it stopped at (or `POST
/api/v1/installation/jobs/job-$RUN/unblock` with the job's `version`).

Then finish the job. `installed` and `tested` need a photo in the same message, so those two
updates carry one (the mock serves the same picture for any `file_id`) and a caption:

```bash
photo() {  # a photo with a caption
  curl -s localhost:9300/test/push -H 'content-type: application/json' \
    -d "{\"message\": {\"chat\": {\"id\": $RUN}, \"caption\": \"$1\", \"photo\": [{\"file_id\": \"p$RANDOM\", \"file_unique_id\": \"p$RANDOM\", \"width\": 320, \"height\": 200}]}}"
}
say "Found the equipment"
photo "Installed"
photo "Tested, all lights green"
say "Handed over to the customer"
```

The job ends `handed_over`, and each photo appears in the timeline with its description. **Take
over** silences the agent while you write to the installer yourself, and **Release** hands the
conversation back.

Why a fresh `RUN` each time: an update the app has seen is a redelivery and runs once, an
installer with an open job gets no second one (409), and a chat linked to one installer cannot
link another. All three are the process working as intended.

### With real models

Set `OPENROUTER_API_KEY` in `.env`, remove the `MAPPD_GATEWAY_CONFIG` line, and run
`docker compose up -d --wait` again to restart the gateway; `mappd mock llm` is no longer
needed. Models are routes in `deploy/agentgateway/gateway.yaml` (`fast`,
`smart`, `judge`, …) — open-weight models through OpenRouter by default; point a route at
another provider by editing that file. Provider keys live only in the gateway.

## Motivation

A developer who wants to automate a few processes in their company with LLMs hits three walls.
The tooling changes every month. Ready-made platforms trade a fast start for their own
abstractions and licenses that are not open source. And the hard part — evals, exception
handling, people in the loop — is not really supported anywhere.

The seams between the layers have settled: an OpenAI-compatible API for models, MCP for tools,
OpenTelemetry for traces, Postgres for state. That makes a composable stack possible, like LAMP
was. MAPP-D is that stack, with these principles:

- **OSI licenses only**, no open core in the critical path. Every layer is replaceable at a
  standard boundary.
- **Self-hosted**: one server per environment, `docker compose`, a shell script to deploy.
- **Code, not a builder**: thin wrappers over good libraries, so a process is ordinary Python
  you can test, review and diff.
- **Reliability by architecture**: an approval is an event that starts a short durable workflow,
  not the resumption of an agent session — so two concurrent approvals cannot run an action
  twice.
- **Built to be extended by a coding agent**: conventions, a non-interactive CLI, and evals that
  tell the agent whether its change helped.

## Concepts

**The stack.** The name lists roles, not products, so swapping a component never makes it wrong:

| | Role | v1 component |
|---|---|---|
| **M** | Model gateway | [agentgateway](https://github.com/agentgateway/agentgateway) |
| **A** | Agents | [Pydantic AI](https://github.com/pydantic/pydantic-ai) |
| **P** | Persistence | [PostgreSQL](https://www.postgresql.org/) + [pgvector](https://github.com/pgvector/pgvector) |
| **P** | Processes | the demos and your own, with their evals |
| **D** | Durable execution | [DBOS Transact](https://github.com/dbos-inc/dbos-transact-py) |

Traces go to [MLflow](https://github.com/mlflow/mlflow) over OpenTelemetry. The app is one
Python service (FastAPI, HTMX pages, the `mappd` CLI) on one Postgres. It runs on a single node:
DBOS's open-source core has no cross-node recovery (that is its proprietary Conductor), so the
DBOS calls sit behind a small internal interface and moving to another engine is a bounded
change.

**A process is a folder.** `processes/<name>/` holds its schemas, its pure domain logic with the
hard rules, its agents and prompts, a thin workflow, a service layer with the API and the pages
on top, its migrations, and its evals. There is no visual builder and no template generator: a
new process starts as a copy of the closest demo.

**Two shapes.** A *pipeline* (support, RFQ) takes one input to one decision: classify, decide,
draft, approve, act. A *case* (installation) is a state machine that lives for days and many
messages, with an agent interpreting each message into a transition.

**Short durable workflows.** No workflow waits for a person, a timer or a reply. Waiting is a
row in a table; each event — an email, an approval, a Telegram update — starts a new workflow
that runs for seconds. Workflow IDs come from stable keys (a Message-ID, a decision and its
version), so a redelivery or a double click runs once. A crash resumes from the last finished
step.

**Actions run once.** Durable execution alone is at-least-once: a crash after sending re-runs
the send. So every change to the outside world is its own step with an idempotency key, recorded
in an `actions` outbox. A receiver that cannot deduplicate (plain SMTP) gets a send whose outcome
is unknown parked for a person to settle, never sent twice.

**Autonomy is a policy, per category.** A process starts in shadow mode (decides, sends
nothing), then a person approves every decision, then categories move to `auto` one at a time
when the trust dashboard shows they are ready: approved without edits *and* few false escalations.
The policy is a reviewed file, `autonomy.yaml`. Hard rules (refunds always escalate) are code
and override every level.

**Evals are the oracle.** Each process declares metrics and thresholds against its output
schema — exact fields, set F1, an LLM judge, or a simulated dialogue for cases. A labelled dev
set is generated from `world.yaml` (the fictional company of the demos) or imported from real
history; a holdout set stays out of git, marked with a canary that fails CI if it leaks.
Operator corrections become new cases with `mappd feedback sync`.

**API first.** Every action is in `/api/v1` before it is in a page; the HTMX pages call the same
service layer. Integrators get an OpenAPI spec, a JSON Schema per process output, signed
webhooks with redelivery, an SSE stream, idempotency keys and optimistic locking.

## Develop with a coding agent

The repository is set up so a coding agent (Claude Code, Codex, Cursor, Aider, …) can extend it
safely, and a human can follow the same rules.

- **[AGENTS.md](AGENTS.md)** — the hard rules and conventions in one page. `CLAUDE.md` imports
  it.
- **[skills/](skills/README.md)** — step-by-step guides for the common tasks: a new process, the
  eval loop, a side effect, a migration, a dataset, debugging a workflow, the release check.
  Any agent can read them. A test checks that every command and path they name exists.
- **The CLI is the agent's interface.** Every `mappd` command is non-interactive, has `--json`,
  meaningful exit codes and `--help` with examples — the help is the documentation.
  `mappd doctor` checks every layer.
- **Evals are its feedback.** After a change to a prompt, a model or decision logic, the agent
  runs `mappd eval run <name>` and `mappd eval compare <name>`, and reads the regressions and
  the flipped cases.
- **The holdout is fenced off**: `.cursorignore` and `.aiderignore` (for Claude Code, add a
  `Read(**/holdout*.jsonl)` deny rule to your own `.claude/settings.json`); aggregate-only
  holdout runs; a canary check in pre-commit and CI.

Development is the quick start's setup: the stack in Docker, everything else on the host. While
you change code, run the app with `uv run mappd serve --reload`, and the checks:

```bash
uv run pre-commit install            # ruff, mypy, migration lint and the canary check on every commit
uv run pytest -m "not pg and not acceptance"   # the fast loop, no services
uv run pytest                        # with Postgres
uv run pytest -m acceptance          # the app in containers too, on other ports; a few minutes
```

The app runs in a container only for deploys and the acceptance suite
(`compose.app.yaml`).

A good first task to give an agent: *"Add a process for warranty claims, copied from support,
following the process guide."*

## What to read next

| Read | For |
| --- | --- |
| [docs/process-guide.md](docs/process-guide.md) | Building your own process end to end: describe, data, build, evals, deploy, autonomy, changes |
| [AGENTS.md](AGENTS.md) | The rules every change follows |
| `processes/*/process.md` | How each demo decides, and its hard rules |
| [docs/deploy.md](docs/deploy.md) | CI, images, deploying a commit to a server, rollback |
| [docs/auth.md](docs/auth.md) | Sign-in, roles and the auth modes — turn it on before real users |

## License

Apache License 2.0. See [LICENSE](LICENSE).
