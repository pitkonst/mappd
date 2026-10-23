# support

First-line support for a distributor and installer of smart-building equipment. A ticket arrives
(API or email); the process classifies it, escalates refunds by rule, drafts a reply from
the knowledge base, and waits for an operator to approve it; approval sends the email.

## Entry point and layout

- `__init__.py` — single entry point (`process = Process(...)`), registered in `processes/__init__.py`.
- `schemas.py`, `db.py`, `domain.py`, `workflow.py` — core process files at the root.
- `service.py` — every operation the API and the pages offer; `api.py` (`/api/v1/support`) and
  `pages.py` (`/support`, templates in `templates/support/`) only translate HTTP to it.
- `events.py` — the events the workflows emit and the webhook target.
- `agents/classifier/` and `agents/drafter/` — each agent in its own folder with its `prompt.md` and tools.
- `data/` — `seed.py` (knowledge base) and `synth.py` (`SynthSpec`).
- `evals/` — `__init__.py` (`EvalSpec`), `config.yaml`, `rubric.md`, `dev.jsonl`, `canary.txt`.

## Input

`TicketIn`: `external_id` (`mail-<hash>` for an email, `tg-<update_id>`) is the stable key: the workflow ID and
the idempotency of redeliveries come from it.

Email: the `support` mailbox (`mail.py`, `MAPPD_SUPPORT_MAIL_MODE=imap` and the
`MAPPD_SUPPORT_IMAP_*`/`SMTP_*` settings). The core
polls it; each customer email becomes a ticket keyed `mail-<16 hex>` (`mappd.ids.mail_case` of its
Message-ID: stable, and safe in URLs and outbox keys), with `mail` recording its Message-ID (unique
per table, so a mail never comes in twice, even one stored under an older key), the
thread ids, the attachments (names, types, sizes; not read) and `follows`, the ticket it answers
(matched by `In-Reply-To`/`References` against a ticket's Message-ID or the reply we sent).
Automated mail (auto-replies, bounces, our own) never becomes a ticket; a mail without a sender
is rejected and logged.

## Decisions

- Classification: topic, route (commercial or technical), urgency, language. Agent `support.classifier` (`agents/classifier/`).
- Draft: reply with knowledge-base citations. Agent `support.drafter`, read-only tool `search_kb` (`agents/drafter/`).
- Approval: an operator's decision on a version of the ticket (optimistic locking, 409 if stale).

## Data ownership

MAPP-D is the system of record for tickets in the demo: there is no external helpdesk. With one, `support_tickets` keeps `external_id` and the ticket as it was decided, and replies go back through actions. The decision is one `output` JSONB (`SupportOutput`); `outcome` and `escalate_to` are generated from it for queries.

## Hard rules

`ALWAYS_ESCALATE` in `domain.py`: refunds always go to a person, without a draft.

## Actions

`mappd.send_email`, key `mappd.send_email:support:<external_id>`: one reply per ticket, over
SMTP from `MAPPD_SUPPORT_MAIL_FROM`. A reply to an email threads under it (`Re:`, `In-Reply-To`,
`References`, the original quoted); its Message-ID comes from the key and is stored as
`mail.sent_message_id`. Plain SMTP cannot deduplicate by key, so an interrupted send stops at
`unknown` (`on_unknown="escalate"`) and the ticket stays `approved`: check the customer's mailbox
or the server log, then settle it on `/actions` (which also retries the approval, so the ticket
finishes) or with `mappd action resolve` followed by `mappd workflow retry`. A rejected recipient (5xx) fails at once, without retries.

## Evals

`evals/config.yaml`: routing and outcome accuracy. `evals/dev.jsonl`: synthetic tickets.

## API and events

| Method and path | Answer |
| --- | --- |
| `POST /api/v1/support/tickets` | 202, the ticket's workflow ID |
| `GET /api/v1/support/tickets?status=&limit=&before=` | a page, newest first; `next` is the cursor |
| `GET /api/v1/support/tickets/{external_id}` | the ticket, 404 |
| `POST /api/v1/support/tickets/{external_id}/approve` | `{"result": "sent", "repeated": bool}`; optional `Idempotency-Key`; 409 with the current version, 422 when a key is reused for another decision, 502 naming the workflow to retry when the send did not complete |
| `GET /api/v1/support/schema` | JSON Schemas of `SupportOutput` and `Decision` |
| `GET /api/v1/support/events` | SSE of the events below (this node only, no replay) |

Events, each with ID `support:<external_id>:<type>:<version>`: `created` (version 0),
`approval_needed` or `escalated` (after the decision is saved), `closed` (after the reply is
sent). Webhooks go to `MAPPD_SUPPORT_WEBHOOK_URL`, signed with `MAPPD_SUPPORT_WEBHOOK_SECRET`;
both empty means none. A delivery is its own workflow (`mappd.deliver_webhook`): a receiver that
keeps refusing ends it in ERROR without touching the ticket, and `mappd workflow retry` resends.
An `unknown` send is settled on `/actions` or with `mappd action resolve`.

## Autonomy

`autonomy.yaml` (reviewed like code; `MAPPD_SUPPORT_AUTONOMY` points tests elsewhere) holds
`shadow`, a level per category (`auto` or `review`, default `review`) and the readiness
thresholds. Edit it with `mappd process shadow support on|off` and
`mappd process autonomy support <category> auto|review`, then commit the diff; the app reads it
at startup. Workflows read it through journaled steps (`support.autonomy_level`,
`support.shadow_mode`), so a replay sees the answer recorded at the time. An `auto` category's
replies and questions go out without approval (a child `approve_and_send` with operator `auto`);
escalations and the hard rules always go to a person. In shadow mode every decision is recorded
and nothing is sent: the ticket ends `shadowed`.

## Operator decisions

Each is recorded in `support_tickets.decision` (`DecisionRecord`): who, the kind, shadow,
whether the reply was edited, the corrected category, outcome and team, and for resolutions
whether the agent could have handled it.

- Approve (`awaiting_approval` → `sent` or `shadowed`): the reply, optionally a corrected category.
- Escalate (`awaiting_approval` → `escalated`): the team; nothing is sent.
- Resolve (`escalated` → `resolved`): `agent_could_handle`, what the agent should have done.

## Feedback

`mappd feedback sync support [--since 30d] [--out PATH]` turns every decision a person made into
an eval case (`fb-<external_id>`, corrections applied) in `evals/feedback.jsonl`, git-ignored and
carrying the canary; `mappd eval run support --set feedback` runs the metrics on it. It is a
developer command: run it on a host with access to the database.

## Dashboard

`/support/dashboard` and `GET /api/v1/support/trust?days=30`: per category, closed without
edits (auto sends and unedited operator sends over decided proposals) next to false
escalations (resolutions marked `agent_could_handle` over all resolutions), and a verdict —
`ready for auto` with the command to run, `keep review`, or `too few cases`. Nothing changes a
level automatically. Token use and cost per trace are in MLflow.
