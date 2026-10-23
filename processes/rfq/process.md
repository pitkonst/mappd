# rfq

Requests for quotation for a distributor and installer of smart-building equipment. A customer
asks for quantities of products in their own words; the process reads the request, matches each
item to the catalog, prices it, proposes a faster alternative when an item would arrive too late,
and either quotes, asks a clarifying question, or escalates. An operator approves; approval
emails the quote.

## Entry point and layout

- `__init__.py` — single entry point (`process = Process(...)`), registered in `processes/__init__.py`.
- `schemas.py`, `db.py`, `domain.py`, `catalog.py` — the request, the table, the rules, the catalog.
- `agents/extractor/` — the agent that reads the request, with its prompt and `search_catalog` tool.
- `workflow.py`, `service.py`, `api.py` (`/api/v1/rfq`), `pages.py` (`/rfq`, templates in
  `templates/rfq/`), `events.py`, `trust.py`, `feedback.py`, `autonomy.yaml` — the same
  shape as support's, copied from it.
- `data/synth.py` (`SynthSpec`), `evals/` (field-comparison metrics, `config.yaml`, dev set),
  `mocks/mock_llm.py` (claims RFQ's requests behind the shared mock port).

## Input

`RfqIn`: `external_id` (`mail-<hash>` for an email, or another stable key), `sender`, `subject`, `body`.

## Decisions

- Extraction (agent `rfq.extractor`): the requested items, quantities, the catalog SKU when one
  product clearly fits, a needed-by date, whether custom work is asked.
- Everything else is code (`domain.py`): prices and lead times from the catalog, totals in
  `Decimal`, alternatives (same family, arriving in time, closest price), the gate, the draft.

## Hard rules

- Prices come from the catalog, never from the model. So do lead times: a quote reads the
  catalog's `lead_days` and never the depot stock in `world.yaml` (that belongs to the
  installation process's warehouse).
- Custom work (integrations, custom builds) goes to pre-sales.
- A total above `ESCALATE_ABOVE` (€10,000) goes to sales: volume pricing is a person's call.
- The quote's figures come from the template, and an operator cannot type them. An approval's
  `reply` may add words, but each figure line of the quote (items, alternatives, lateness and
  the total) must appear in it unchanged, and no added line may name an amount, a currency or a
  percentage ("50% off", "1102.50", "EUR"); otherwise the approval is refused (422).
  The check reads figures, not intent: a discount written only in words ("half price",
  "we waive the cabinet") passes it, so the approval itself stays the safeguard there. Prices change only
  through corrected `lines`. Leave `reply` out to send the quote as drafted, or as re-priced
  when `lines` is given. A `reply` with `lines` must carry the re-priced figures.

## Data ownership

MAPP-D is the system of record for requests and quotes in the demo; there is no ERP. The quote
goes out by email.

Email: the `rfq` mailbox (`mail.py`, `MAPPD_RFQ_MAIL_MODE=imap` and the `MAPPD_RFQ_IMAP_*`/`SMTP_*`
settings). Each request email becomes a request keyed `mail-<16 hex>` (`mappd.ids.mail_case` of
its Message-ID, as support's); `mail` records the Message-ID (unique per table), the thread ids, the attachments (listed, not read: the agents see the
body only) and `follows`, the request it answers. Automated mail never becomes a request.

## Actions

`mappd.send_email`, key `mappd.send_email:rfq:<external_id>`: one quote per request, threaded
under the request email (`Re:`, `In-Reply-To`, `References`, the request quoted); its Message-ID
is stored as `mail.sent_message_id`. Plain SMTP cannot deduplicate, so an interrupted send stops
at `unknown` for a person: settle it on `/actions`, which also retries the approval (or
`mappd action resolve`, then `mappd workflow retry`). Approval with
corrected `lines` (API only: the page has no list-of-models form widget) re-prices the quote from the catalog before sending.

## Autonomy, decisions, feedback, dashboard

As support's: `autonomy.yaml` with categories = outcomes (`quote`, `clarify`),
escalations always to a person; approve, escalate and resolve record a `DecisionRecord`;
`mappd feedback sync rfq` writes corrected lines into `evals/feedback.jsonl`;
`/rfq/dashboard` and `/api/v1/rfq/trust`.

## Evals

`evals/config.yaml`: outcome and escalation role (exact match), line items F1, SKU F1, totals
check and alternatives (python metrics in `evals/metrics.py`). No judge: the draft is a template.
`mappd eval run rfq`; the keyword baseline is the floor.
