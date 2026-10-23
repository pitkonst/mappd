# installation

Installation jobs for a distributor and installer of smart-building equipment. A job is a case:
a deterministic state machine in code, driven by the technician's Telegram messages, which an
agent interprets and a vision model describes when they carry photos. When a message blocks the
job, a resolver agent checks the company's systems over MCP and proposes what a dispatcher
confirms.

## Entry point and layout

- `__init__.py` — single entry point (`process = Process(...)`) with its schedules and mocks,
  registered in `processes/__init__.py`.
- `schemas.py`, `db.py`, `domain.py` — the job, the tables, the state machine and deadline
  arithmetic (pure).
- `engine.py` — one message through the case (interpret → `domain.apply` → resolve a block):
  the code the workflow and the eval simulator both run.
- `agents/interpreter/` — reads one message (state, allowed next state, text, photo
  description) into an `Interpretation`. `agents/resolver/` — answers a block with a
  `Resolution` (summary, reply, proposals) after reading the systems. `vision.py` — the photo
  description through the gateway model `vision`.
- `tools.py` — the resolver's view of the systems: no confirmed tools, idempotency keys from
  the turn (`<job>:<turn>:<tool>`), never from the model.
- `telegram.py` (settings, the `send_message` action), `intake.py` (store, enqueue, the queue),
  `workflow.py` (`handle_update`, polling, deadlines, unblock, proposals, takeover).
- `service.py`, `api.py` (`/api/v1/installation`), `pages.py` (`/installation`, the job page
  `/installation/jobs/<id>`; templates in `templates/installation/`).
- `mocks/systems.py` (warehouse, logistics, tickets, contacts over one set of facts),
  `mocks/mock_mcp.py` (their MCP servers, `mappd mock mcp`, port 9400),
  `mocks/mock_telegram.py` (a Bot API stand-in, `mappd mock telegram`, port 9300) and
  `mocks/mock_llm.py` (claims the interpreter's, resolver's, simulator's and vision requests
  behind the shared mock port).
- `sim.py`, `simulator.py`, `agents/simulator/`, `data/synth.py`, `evals/` — the
  dialogue-simulation evals.

## Input

`JobIn` from a dispatcher: `external_id`, `installer_name`, `site`, `skus`, `appointment_at`. A
new installer gets a one-time deep link (`https://t.me/<bot>?start=<token>`); `/start <token>`
links their chat. After that, every Telegram update from the chat is an input.

## State machine

`assigned → arrived → access → equipment_found → installed → tested → handed_over`, one step per
message. A message claiming a later or an earlier step gets a clarifying reply and no
transition.

## Hard rules

- `installed` and `tested` need a photo in the same message; without one the job stays and the
  bot asks for it.
- An exception (missing parts, breakage, no access, other) blocks the job with its reason and
  a dispatcher deadline; only a dispatcher unblocks it, back to the state it was blocked in.
- One open job per installer: a new job for an installer whose job is not handed over is
  refused (409), since their messages go to the open job.
- The agent proposes; `domain.apply` decides. A moved version (an unblock meanwhile) wins over a
  message being applied.

## Channel and ordering

- `MAPPD_INSTALLATION_TELEGRAM_MODE`: `webhook` (production;
  `POST /api/v1/installation/inbound/telegram`, accepted only with the configured
  `X-Telegram-Bot-Api-Secret-Token`), `polling` (development: `installation.poll` every 5 s,
  the offset moves only after the updates are queued) or `off`.
- Each update is stored once (`channel_key` = `update_id`, unique) and handled by the workflow
  `installation.update:<update_id>` on the queue `installation.jobs`, partitioned by chat (an
  installer has one chat and one open job, so linking mid-stream keeps the order), one at a
  time per partition. A redelivered update reaches the same workflow ID and runs once.
- A message that changes nothing keeps the job's version, so a dispatcher's unblock at the
  version they saw applies; when an unblock lands while a message is read, the message is
  re-read against the new state (up to three attempts).
- A photo is described in its own durable step: the journal keeps the text, never the bytes.

## Deadlines

Rows in `installation_deadlines`, checked every minute by `installation.deadlines`: `arrival`
at the appointment + 30 min (settled on arriving; overdue sends one reminder), and
`dispatcher_response` 2 h after a block (settled by the unblock). Each overdue deadline emits
one `overdue` event. The check marks `notified_at` (it acts once); the deadline stays overdue on
the page until arriving or the unblock settles it.

## Actions

`installation.send_message`, key per update (`<update_id>:reply`), deadline or unblock version.
Telegram has no idempotency key, so the outbox prevents duplicates and an uncertain send
escalates to `/actions`.

## Exceptions: the resolver and its proposals

- A message that blocks the job runs the resolver once. It reads the message that stopped the
  job and the conversation, and uses the systems' tools through the gateway's MCP endpoint
  (`MAPPD_INSTALLATION_MCP_URL`, agentgateway multiplexing four servers): `check_stock`,
  `delivery_slots`, `site_contact`, and `open_ticket` (internal, so autonomous).
- `reserve_parts` and `book_delivery` are commitments: the resolver only proposes them
  (`installation_proposals`). `POST .../jobs/<id>/proposals/<pid>/confirm` runs the call once
  (an outbox action keyed by the proposal; the systems honor the key) and tells the installer;
  `.../reject` runs nothing. Confirming does not unblock the job; unblocking withdraws the
  job's pending proposals (status `withdrawn`, a `withdrawn` event), since the block they
  answered is over, so nothing is reserved later for a job that moved on.
- A proposal is its own lock, so confirming or rejecting one takes no `version` and no
  `Idempotency-Key`. Only one `pending → confirmed | rejected` transition wins; the confirm
  workflow's ID is the proposal's, so a double click or a parallel confirm runs the call once.
  The job's `version` covers the job's state and its hold, and deliberately does not move on a
  proposal decision. If it did, an installer message being read at that moment would lose its
  version-guarded update.

## Dispatcher: timeline and take-over

- `GET .../jobs/<id>/timeline` merges the messages (author `installer`, `bot`,
  `dispatcher:<name>`) and the events (`transition`, `blocked`, `unblocked`, `resolution`,
  `tool_call`, `proposal`, `confirmed`, `done`, `failed`, `rejected`, `takeover`, `release`,
  `manual_advance`, `overdue`), each written in the transaction of the change it records.
  A turn stays together: the bot's reply (`messages.reply_to`) and the events a message caused
  (`events.message_id`) follow that message, even when the installer's next message came first.
- `POST .../takeover` and `.../release` (version-guarded; only the holder releases): while a
  dispatcher holds the job, the installer's messages are recorded (photos described) and nothing
  else happens. The holder writes with `POST .../messages` (`Idempotency-Key` required) and may
  `POST .../advance` one step by hand (no photo needed). Deadlines keep running.
- Photos show through `GET .../jobs/<id>/photos/<file_id>` (only that job's photos; the bot
  token stays on the server; 502 when Telegram cannot serve one).

## Events

`progress`, `blocked`, `handed_over`, `unblocked`, `overdue`, `done`/`failed` (a proposal) and
page refreshes (`takeover`, `release`, `message`, `manual_advance`) through `mappd.events`;
`GET /api/v1/installation/events` streams them.

## Evals: dialogue simulation

`mappd data synth installation` plans scenarios from `world.yaml` (sites, depots, delivery
slots, a crew of 12 synthetic technicians with four styles): a job, what the systems know, and
the script — the true order of events, with missing parts, breakage or no access as exceptions.
`mappd eval run installation` plays each scenario: the simulator (gateway model `simulator`)
words one script step per turn, the production engine answers over in-process systems, and the
episode ends at handover, a block, or 16 turns. Metrics in `evals/config.yaml`: final state,
exception kind, no premature transition, timely escalation, proposals match, and a judge for no
unbacked promises. The keyword baseline needs no model.

## Data ownership

MAPP-D owns the job's state and its conversation. Installers and sites are its own records in
the demo; in production the job and installer would come from the field-service system, and
`external_id` is that system's key.

## Not yet verified

Real Telegram and real MCP backends (the demo's systems are the mock). The simulation's
technician is an LLM; real conversations will be messier.
