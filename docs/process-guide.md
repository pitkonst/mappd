# Developer guide: a process from first idea to production

This guide walks one new process through its whole life: describe it, build its eval set, write
it, measure it, ship it, widen its autonomy, and change it later. It ties together the rules in
`AGENTS.md` and the task checklists in `skills/`; when a step has
a skill, the skill is the short version of the same step.

The running example is `warranty`: a customer emails a warranty claim, the process checks the
serial number and the purchase date against the rules, drafts an answer, and an operator
approves it before it goes out. Replace the name with yours throughout.

## 0. Before you start

You need the dev loop from `README.md` working:

```bash
uv sync
cp .env.example .env                  # then set OPENROUTER_API_KEY (evals need real models)
docker compose up -d --wait           # Postgres, MLflow, agentgateway, GreenMail
uv run mappd db migrate
uv run mappd doctor                   # every line ok or off
```

Two ideas carry the rest of the guide:

- **A process is a deterministic skeleton with agents inside its steps.** Code decides what
  happens and in which order; an agent only fills in what needs language (classify, extract,
  draft). Money, refunds and other hard rules are code (`AGENTS.md`, rule 6).
- **Evals are the oracle.** A process is done when its metrics meet their thresholds, not when
  the code reads well. You define the metrics before the agent exists.

## 1. The lifecycle at a glance

| Phase | You produce | Commands |
| --- | --- | --- |
| Describe | `process.md`, `evals/config.yaml` | — |
| Data | `evals/dev.jsonl` (committed), `evals/holdout.jsonl` (not) | `mappd data synth`, `data import`, `data split` |
| Build | the process folder, migrations, tests | `mappd db migrate`, `process run`, `pytest` |
| Measure | metrics in MLflow | `mappd eval baseline`, `eval run`, `eval compare` |
| Ship | a commit on `main`, an image, a deploy | the release check, `deploy/deploy.sh` |
| Roll out | shadow → approve all → autonomy per category | `mappd process shadow`, `process autonomy`, `feedback sync` |
| Change | the same loop, with journal and schema care | section 9 |

## 2. Describe the process

Pick the shape first; it decides which demo you copy.

| Shape | When | Copy |
| --- | --- | --- |
| Pipeline | one input in, one decision out, a person approves, an action runs | `support` (a text reply) or `rfq` (a structured list) |
| Case | long-lived state, many messages over days, a state machine | `installation` |

`warranty` is a pipeline with a text reply, so it starts from `support`.

Write `processes/warranty/process.md` before any code, with the demo's sections:

- **Input** — what arrives and its stable key (a Message-ID, an external ticket ID). The key
  becomes the workflow ID; it must never repeat for a different input (`AGENTS.md`, rule 4).
- **Decisions** — the outcomes (`approve_claim`, `reject_claim`, `clarify`, `escalate`) and who
  each escalation goes to.
- **Data ownership** — the system of record for each entity. MAPP-D owns the process state
  (status, version, the agent's output, the decision); the ERP owns the order and the device.
  Keep the external ID and a snapshot of the input, never a live mirror.
- **Hard rules** — each one becomes a line of code in `domain.py`, never a sentence in a
  prompt. Example: a claim above 1,000 EUR always escalates to sales.
- **Actions** — every change to the outside world (the reply email, a replacement order).
- **Evals** — the metrics and why each threshold is where it is.
- **API and events, autonomy** — what integrators see; which categories may ever go `auto`.

Then write `processes/warranty/evals/config.yaml`. Metrics point at fields of the output
schema and the case's expected labels; `when` restricts a metric to some cases. Copy the shape
from `processes/support/evals/config.yaml`:

```yaml
metrics:
  - name: outcome_accuracy
    kind: exact_match
    field: expected.outcome
    threshold: 0.85
  - name: escalation_role_accuracy      # every process measures routing, not only answers
    kind: exact_match
    field: expected.escalate_to
    when: {outcome: escalate}
    threshold: 0.8
  - name: false_escalation_rate         # the counter-metric: over-caution is not free
    kind: rate
    output: outcome
    equals: escalate
    when: {outcome: [approve_claim, reject_claim, clarify]}
    threshold: 0.15
    lower_is_better: true
  - name: judge_pass_rate               # free text: an LLM judge with evals/rubric.md
    kind: judge
    when: {outcome: [approve_claim, reject_claim, clarify]}
    threshold: 0.7
```

The kinds in use today are `exact_match`, `set_f1`, `rate` and `judge`; a custom metric is a
Python function (see `processes/rfq/evals/metrics.py`).

## 3. Build the eval set

Skill: `skills/dataset/SKILL.md`.

Labels come from code, text from a model. `world.yaml` holds the fictional company (catalog,
customers, policies, knowledge base); your `data/synth.py` plans each case in code — the
correct outcome first, then the facts the message must state and omit — and the gateway's
`synth` model writes the text without seeing the labels. A second model (`check`) reads the
text back; a case is kept only when it recovers the labels. If your process needs facts the
world lacks (warranty periods, serial-number formats), add them to `world.yaml` first.

```bash
uv run mappd data synth warranty --seed 42 --n 300 --dry-run   # the plan, no model calls
uv run mappd data synth warranty --seed 42 --n 300             # needs the stack and a provider key
uv run mappd data split warranty --holdout 0.2 --seed 7
```

Set the mix of outcomes explicitly and include traps — the cases that look like one outcome and
are another (a claim that never says "warranty", a claim just past the period).

With real history instead:

```bash
uv run mappd data import warranty history.csv --map external_id=id,sender=from,body=text
uv run mappd data split warranty --from imported --holdout 0.2 --seed 7
```

Rules that apply from here on:

- `dev.jsonl` is committed. `holdout.jsonl` is git-ignored and carries the process's canary.
- Never open `holdout*.jsonl`, `evals/synth.jsonl`, `evals/imported.jsonl` or `.mappd/cache`,
  and never copy text from them into a prompt, a few-shot example or code (`AGENTS.md`,
  rule 8). `mappd data check-canary` fails CI when a holdout case leaks into a tracked file.
- Real customers' text never goes into fixtures, mocks or committed eval sets.
- Read 20–30 dev cases by hand before trusting any number.
- Keep a copy of the holdout set outside the repository: it is not in git, and regenerating it
  costs model calls and changes the set.

## 4. Build the process

Skill: `skills/new-process/SKILL.md`.

### 4.1 Copy and rename

```bash
cp -r processes/support processes/warranty
rm -f processes/warranty/evals/{canary.txt,dev.jsonl,holdout.jsonl,synth.jsonl}
```

Delete the copied `canary.txt` before you split (section 3): `mappd data split` creates a new
one when none exists, and a shared canary would hide which process leaked.

Rename every `support` identifier, table, route prefix, template folder, env var
(`MAPPD_SUPPORT_*`) and test file (test basenames must be unique across the repository:
`test_warranty_*.py`). `grep -rn support processes/warranty` must come back empty. Delete what
`warranty` does not need — the knowledge base, the mail intake, the trust page — and the tests
that go with it.

Workflow and step names derive from the module (`mappd.durable`), so renaming the folder
renames them; there is nothing to edit by hand.

### 4.2 The files, in the order you write them

| File | What goes in it | Rules |
| --- | --- | --- |
| `schemas.py` | Pydantic models: the input (`ClaimIn`), the agent's output (`WarrantyOutput`), the decision bodies | Every input and output is a model. The output model is also the approval form and the JSON Schema integrators get |
| `domain.py` | Pure functions: `decide` with the hard rules first, workflow IDs, decision records | No database, no DBOS, no HTTP. This is where `ALWAYS_ESCALATE` and the amount limit live |
| `agents/<agent>/` | `prompt.md` and `__init__.py` building the agent with `durable.agent(gateway.model("fast"), output_type=...)` | Models are gateway routes (`fast`, `smart`, …), never provider names or keys |
| `db.py` + `migrations/` | The tables and their Alembic migrations, kept in sync | `skills/migration/SKILL.md`; first migration `warranty_0001` with `branch_labels = ("warranty",)` and `depends_on = "mappd_0001"` |
| `workflow.py` | Thin orchestration: load → solve → save → (approval) → act | Short workflows only; side effects are actions; writes are guarded transactions |
| `service.py` | Every operation the API and the pages offer | The only place both call (`AGENTS.md`, rule 7) |
| `api.py`, `pages.py`, `templates/warranty/` | HTTP adapters over the service | Routes under `/api/v1/warranty` and `/warranty` — the auth middleware applies the process's role only under those prefixes |
| `events.py` | The webhook target and the event builder | URL and secret come from env vars the target names |
| `evals/__init__.py`, `data/synth.py`, `data/seed.py` | The `EvalSpec`, the `SynthSpec`, base data | `solve` in the eval spec is the same function the workflow calls |
| `__init__.py` | The `Process(...)` declaration | Section 4.4 |

### 4.3 The workflow rules that bite

Read `workflow.py` of the demo you copied before changing it; each rule below is visible there.

- **Two short workflows, never a wait.** `process_claim` runs from intake to the draft and ends
  with the claim `awaiting_approval`. The operator's decision starts `approve_and_send`. Nothing
  sleeps, polls or waits for a person inside a workflow (`AGENTS.md`, rule 3). Timers are rows
  checked by a scheduled workflow (`Process(schedules=...)`).
- **Workflow IDs from stable keys.** `ids.from_key("warranty.claim", external_id)` for intake;
  `ids.decision("warranty.approve", external_id, version)` for a decision, so a double click and a
  retry collapse into one run (`AGENTS.md`, rule 4).
- **Every side effect is an action.** The reply email goes through `mappd.mail.send`; your own
  effects use `@durable.action` with a business key (`per="claim_id"`) and
  `on_unknown="escalate"` when the receiver cannot deduplicate. A Pydantic AI function tool is
  not a step: a mutating tool must call an action. Skill: `skills/side-effect/SKILL.md`.
- **Writes are guarded transactions.** Each app-DB write inside a workflow is a
  `@durable.transaction` whose `WHERE` names the status it moves from
  (`WHERE status = 'approved'`), so a replay or a fork matches no row.
- **Optimistic locking on decisions.** The decision carries the version the operator saw;
  `update_if_version` refuses a stale one, and the API answers 409.
- **Workflows are `async def`; sync durable steps are awaited.** The body runs on a worker
  thread; never call `asyncio.to_thread` yourself.

### 4.4 Declare and register it

`processes/warranty/__init__.py` declares everything the core needs; the core then wires the
routes, schedules, mailboxes, webhooks, `/ops` and the CLI with no core change:

```python
process = Process(
    name="warranty",
    router=router,
    pages=page_router,
    role="operator",  # who may decide
    webhooks=WEBHOOKS,
    autonomy=Path(__file__).parent / "autonomy.yaml",
    cases=Cases(
        table=CLAIMS,
        link="/warranty/claims/{key}",
        waiting=("awaiting_approval", "escalated"),
        passing={"new": timedelta(minutes=10), "approved": timedelta(minutes=10)},
    ),
    categories=(...),  # what autonomy is granted per
    ingest=ingest,
    migrations_dir=Path(__file__).parent / "migrations",
    evals=eval_spec,
    synth=synth_spec,
    feedback=feedback.spec,
    mailboxes=(MAILBOX,),  # only if it reads email
    mocks={"llm": MockSpec(app=mock_llm.app, port=mock_llm.PORT, claims=...)},
)
```

Add it to `PROCESSES` in `processes/__init__.py`. Start `autonomy.yaml` with every category at
`review` and `shadow: true` (section 8).

If the new process needs a change in `src/mappd/`, stop and record it as a finding about the
core in its own commit; a process copied from a demo is meant to need none.

### 4.5 Tests

Every change ships with tests (`AGENTS.md`, rule 1). Adapt the demo's `tests/` folder: domain
rules (pure, fast), schemas, the service against Postgres (`pg` marker), the API, the pages,
migrations against `db.py`, and the eval and synth specs. Add an acceptance scenario in
`tests/acceptance/` when the process has a path the unit tests cannot reach (a crash after
sending, a duplicate delivery).

## 5. Run it locally

```bash
uv run mappd db migrate                                  # the new tables
uv run mappd process list                                # warranty is registered
uv run mappd doctor                                      # agents resolve, policy loads
uv run mappd process run warranty --input case.json      # one case, prints the trace id
uv run mappd serve                                       # the app on the host: /warranty, /docs
uv run pytest processes/warranty
```

`process run` executes one case now and prints its MLflow trace. `mappd serve` gives you the
queue, the approval form generated from your output model, and the API at `/docs`. Without a
provider key, run the model mock on the host — `uv run mappd mock llm`, with
`MAPPD_GATEWAY_CONFIG=gateway.mocks.yaml` and
`MAPPD_MOCK_LLM_URL=http://host.docker.internal:9000/v1` in `.env` — and give your process an
`llm` entry in `Process(mocks=...)` that answers its agents' requests.

## 6. Measure: the eval loop

Skill: `skills/eval-loop/SKILL.md`. Needs the stack and real models.

```bash
uv run mappd eval baseline warranty          # the floor: a keyword baseline, once
uv run mappd eval run warranty --limit 20    # a quick look
uv run mappd eval run warranty               # the dev set; results go to MLflow
```

Then iterate, one change per run so a delta has one cause:

```bash
$EDITOR processes/warranty/agents/classifier/prompt.md
uv run mappd eval run warranty
uv run mappd eval compare warranty           # deltas, regressions beyond tolerance, flipped cases
```

`eval run` exits 1 when a metric misses its threshold or more than 10 % of cases error;
`eval compare` exits 1 on a regression. Read the flipped cases and fix the cause, not the case.
Open the failing traces in MLflow to see what the agent saw.

When the dev set passes, run the holdout **once** for the finished change:

```bash
uv run mappd eval run warranty --set holdout     # aggregates only
```

Do not lower a threshold or edit a dev label to pass. Either is a decision for a person, made
in its own commit with the reason.

## 7. Ship

### 7.1 Release check

Skill: `skills/release-check/SKILL.md`. Run all of it before merging:

```bash
uv run pre-commit run --all-files     # ruff, mypy, migration lint, canary
uv run pytest -m "not pg and not acceptance"
uv run pytest -m "pg and not acceptance"
uv run pytest -m acceptance
```

CI runs the first two lines; the Postgres and acceptance suites are yours to run. Commit messages are one imperative line (`Add the warranty process`).

### 7.2 Configuration the server needs

The app's environment is an explicit list, not a pass-through. For each env var your process
reads, add a line to the `x-app` environment in `compose.app.yaml`, for example:

```yaml
    MAPPD_WARRANTY_WEBHOOK_URL: ${MAPPD_WARRANTY_WEBHOOK_URL:-}
    MAPPD_WARRANTY_WEBHOOK_SECRET: ${MAPPD_WARRANTY_WEBHOOK_SECRET:-}
```

A mailbox also needs its `MAPPD_WARRANTY_{IMAP,SMTP}_*` lines in `compose.app.yaml` and its
host, port and TLS lines in the `x-mail` block of `compose.deploy.yaml` (the dev file points at
GreenMail). Document each variable in `.env.example`, and set the real values in the server's
`/opt/mappd/.env` by hand — the deploy never writes that file.

If an agent needs a model route the gateway lacks, add it to both
`deploy/agentgateway/gateway.yaml` and `gateway.mocks.yaml`.

### 7.3 Deploy

A push to `main` runs CI and builds `mappd-app:<sha12>` and `mappd-mlflow:<sha12>`. Deploy
that SHA to staging, then to prod:

```bash
MAPPD_DEPLOY_REGISTRY=ghcr.io/<org> deploy/deploy.sh deploy@staging <sha>
```

The GitHub Deploy workflow does the same from Actions once the repository variable
`DEPLOY_ENABLED` is `true` (`docs/deploy.md`).

The deploy uploads the compose files of that SHA, pulls the images, runs the migrations, starts
the app and runs `mappd doctor`. Your new tables arrive with it: migrations are expand-only, so
the previous version keeps running on the new schema and a rollback is a deploy of the older SHA.

After the deploy, check on the server (the CLI runs inside the app container):

- `ssh deploy@staging docker exec mappd-app-1 mappd doctor` — every layer ok; `stranded`
  workflows are recovered with `ssh deploy@staging docker exec mappd-app-1 mappd workflow
  recover` (runbook: `docs/deploy.md`).
- `/ops` — the new process appears with its waiting and stuck cases.
- One real input end to end on staging, in shadow mode.

Before prod handles real customers, set `MAPPD_AUTH=jwt` (or `header` behind an SSO proxy) and
create the users (`mappd user add … --role operator`). Doctor's `auth` line says
`off` until you do. The app binds to `127.0.0.1`; putting it behind TLS is not built yet.

## 8. Roll out: from shadow to autonomy

The rollout is per process and per category, and every level change is a reviewed commit to
`autonomy.yaml` that ships with a deploy.

1. **Shadow.** `shadow: true`: the process decides and records, and sends nothing. Compare its
   decisions with what people did.

   ```bash
   uv run mappd process shadow warranty on      # edits autonomy.yaml; commit the diff
   ```

2. **Approve everything.** `shadow: false`, every category `review`. Operators approve, correct
   or escalate each draft; each correction is recorded on the decision.
3. **Turn decisions into evals.** Operator edits become eval cases:

   ```bash
   uv run mappd feedback sync warranty          # → evals/feedback.jsonl (real text: git-ignored)
   uv run mappd eval run warranty --set feedback
   ```

   Run the feedback set with every later change, next to the dev set.
4. **Widen autonomy per category.** The trust dashboard (`/<process>/dashboard`, if the process
   has one) shows, per category over 30 days, the share approved without edits next to the share
   of false escalations, and a readiness verdict from the `ready:` block of `autonomy.yaml`.
   Only when both are healthy:

   ```bash
   uv run mappd process autonomy warranty how_to_claim auto
   ```

   Escalations and hard rules go to a person whatever the level.

## 9. Change a process that is live

Each kind of change has its own risk.

**A prompt or a model.** The eval loop of section 6, then the feedback set, then the holdout
once. A model is a gateway route: change `gateway.yaml`, not the agent.

**A hard rule or decision code.** Change `domain.py` with a unit test for the new rule, then run
the evals: a rule change moves the metrics too.

**The output schema.** Old rows hold the old output as JSONB and are read back through the new
model. Add fields with defaults; never remove or retype a field in the same deploy. The approval
form and the published JSON Schema change with the model, so tell integrators.

**The database.** Expand only: add tables, nullable columns and indexes. A drop, rename or type
change takes two deploys — expand and move the readers now, contract later with
`contract = True`. `mappd db lint` refuses the rest. Skill: `skills/migration/SKILL.md`.

**A workflow.** DBOS replays a workflow by step order, and `mappd workflow recover`,
`retry` and `fork` copy finished steps into the running version by position. So:

- Add new steps after the existing ones; do not reorder or remove steps that run before an
  action. The support workflow keeps its claim as the first step for this reason.
- Renaming a step or workflow function renames its DBOS name; pass `name=` with the old name to
  keep in-flight and recorded workflows replayable.
- Keep the arguments of a workflow compatible: recorded workflows are retried with their stored
  inputs.

Because workflows are short, almost nothing is in flight at a deploy. After deploying a workflow
change, check `/workflows?status=stranded` and recover what is there.

**An idempotency key.** Never change the key of an existing action: a different key for the same
effect sends it again.

## 10. When something goes wrong

Skill: `skills/workflow-debug/SKILL.md`.

| Symptom | Look at | Fix |
| --- | --- | --- |
| A case does not move | `/ops`, `mappd workflow show <id>` | Retry the failed step: `mappd workflow retry <id>` |
| A send is `unknown` | `/actions`, `mappd action list --status unknown` | Check with the receiver, then settle it on `/actions` (it retries the workflow too), or `mappd action resolve` + `mappd workflow retry` |
| Workflows `stranded` after a deploy | `mappd doctor`, `/workflows?status=stranded` | `mappd workflow recover` |
| A metric dropped | `mappd eval compare`, MLflow traces | The eval loop; never the threshold |
| A webhook receiver missed events | `/ops` (redelivering, given up) | Fix the receiver; redelivery runs for 24 hours |

## Where the rules live

- `AGENTS.md` — the hard rules and conventions, short.
- `skills/` — one checklist per task named in this guide.
- `processes/support`, `processes/rfq`, `processes/installation` — the working examples.
