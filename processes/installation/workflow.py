"""Orchestration of the installation process: one short workflow per Telegram update.

handle_update runs on the `installation.jobs` queue, one at a time per chat: link a chat, or
load the job → describe the photo → the turn engine (interpret, apply the state machine, resolve
a block) → record the outcome (version-guarded, with its timeline events and proposals) → emit
an event → reply once. poll fetches updates every few
seconds when the bot polls instead of receiving a webhook. Nothing waits: deadlines are rows.
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import httpx2
import sqlalchemy as sa
from psycopg.types.json import Jsonb
from sqlalchemy.dialects.postgresql import insert as pg_insert

from mappd import db, durable, mcp, telemetry
from mappd.channels.telegram import Update
from mappd.events import Event, emit

from . import domain, engine, intake, telegram, vision
from .agents import resolver
from .agents.interpreter import interpret as interpret_message
from .db import DEADLINES, INSTALLERS, JOBS, MESSAGES, POLLING, PROPOSALS, record_event
from .engine import ToolCall
from .schemas import JobRecord, Proposal, Resolution

log = logging.getLogger(__name__)
UNLINKED = "Hello! Please open the link your dispatcher sent to connect this chat to your jobs."
BAD_LINK = "This link is not valid or was already used. Please ask your dispatcher for a new one."
LINKED = (
    "You are linked to your job. Tell me here when you are on site, when you have access, when"
    " you found the equipment, when it is installed and tested (send a photo for each of those"
    " two), and when it is handed over. Report any problem here too."
)
NO_JOB = "You have no open job right now. Your dispatcher will send the next one."
# A dispatcher's change can land while the agent reads a message: reload and re-apply.
ATTEMPTS = 3

# Seams the tests replace: the agents and the vision call.
interpret = interpret_message
resolve = resolver.resolve
mcp_call = mcp.call
describe_photo_text = vision.describe_image
HISTORY = 10  # messages the resolver reads


@durable.workflow
async def handle_update(update: dict[str, Any]) -> str:
    """Apply one technician message to their job and reply once."""
    u = Update.model_validate(update)
    reply_key = f"{u.update_id}:reply"
    if u.start_token is not None:
        linked = await link_chat(u.start_token, u.chat_id, str(u.update_id))
        await telegram.send_message(reply_key, u.chat_id, LINKED if linked else BAD_LINK)
        return "linked" if linked else "bad_link"
    job = await load_job(u.chat_id, str(u.update_id))
    if job is None:
        known = await is_linked(u.chat_id)
        await telegram.send_message(reply_key, u.chat_id, NO_JOB if known else UNLINKED)
        return "no_job" if known else "unlinked"
    description = await describe_photo(u.photo) if u.photo else None
    for attempt in range(ATTEMPTS):
        if attempt:
            job = await load_job(u.chat_id, str(u.update_id))
            if job is None:
                return "conflict"
        if job.taken_over_by is not None:
            # A dispatcher holds the conversation: record the message, say nothing, move nothing.
            await record_inbound(job.id, str(u.update_id), description)
            await _refresh(job.external_id, u.update_id, job.version)
            return "held"
        result = await engine.turn(
            engine.view(job, str(u.update_id)),
            u.text,
            description,
            interpret=interpret,
            resolve=resolve,
        )
        applied = await apply_outcome(
            job.id,
            job.version,
            channel_key=str(u.update_id),
            chat_id=u.chat_id,
            description=description,
            previous=job.state,
            state=result.state,
            blocked_from=result.blocked_from,
            blocked_reason=result.blocked_reason,
            reply=result.reply,
            resolution=result.resolution,
            tool_calls=result.tool_calls,
            now=datetime.now(UTC),
        )
        if applied:
            break
    else:
        return "conflict"
    # The event first: a send that ends unknown or failed must not lose the case's change.
    if not result.event:
        await _refresh(job.external_id, u.update_id, job.version)
    if result.event:
        await emit(
            Event.of(
                "installation",
                job.external_id,
                result.event,
                job.version + 1,
                {"state": result.state, "blocked_reason": result.blocked_reason},
            )
        )
    await telegram.send_message(reply_key, u.chat_id, result.reply)
    return result.state


async def _refresh(external_id: str, update_id: int, version: int) -> None:
    """A message that moved nothing still changes the timeline: tell the job's page."""
    await emit(
        Event(
            id=f"installation:{external_id}:message:{update_id}",
            type="message",
            process="installation",
            case_id=external_id,
            version=version,
            occurred_at=datetime.now(UTC),
            data={},
        )
    )


@durable.step
async def load_job(chat_id: int, current: str | None = None) -> JobRecord | None:
    """The open job of the installer linked to this chat, with its latest messages.

    The history ends before the message being handled (`current`, its channel key): the turn
    engine adds it, and messages that arrived after it wait their turn in the queue.
    """
    job_id = await intake.active_job(chat_id)
    if job_id is None:
        return None
    async with db.aconnect() as conn:
        row = await (await db.arun(conn, sa.select(JOBS).where(JOBS.c.id == job_id))).fetchone()
        if row is None:
            return None
        recent = await (
            await db.arun(
                conn,
                sa.select(MESSAGES.c.author, MESSAGES.c.text)
                .where(
                    MESSAGES.c.job_id == job_id,
                    MESSAGES.c.text.is_not(None),
                    # Only what came before the message being handled: later ones are queued.
                    MESSAGES.c.id
                    < sa.func.coalesce(
                        sa.select(MESSAGES.c.id)
                        .where(MESSAGES.c.channel_key == current)
                        .scalar_subquery(),
                        sa.literal(2**62, sa.BigInteger),  # not stored: no cut-off
                    )
                    if current is not None
                    else sa.true(),
                )
                .order_by(MESSAGES.c.id.desc())
                .limit(HISTORY),
            )
        ).fetchall()
    history = [f"{m['author']}: {m['text']}" for m in reversed(recent)]
    return JobRecord.model_validate({**dict(row), "history": history})


@durable.step
async def is_linked(chat_id: int) -> bool:
    """Whether some installer is linked to this chat."""
    async with db.aconnect() as conn:
        row = await (
            await db.arun(conn, sa.select(INSTALLERS.c.id).where(INSTALLERS.c.chat_id == chat_id))
        ).fetchone()
    return row is not None


@durable.step
def describe_photo(file_id: str) -> str:
    """Download the photo and describe it: the journal keeps the text, never the bytes."""
    return describe_photo_text(telegram.client().get_file(file_id))


@durable.transaction
def link_chat(conn: db.Connection, token: str, chat_id: int, channel_key: str) -> bool:
    """Link a chat to the installer with this one-time token; refuse a used token or chat.

    The `/start` message (`channel_key`) and the confirmation join the installer's open job,
    so its timeline starts where the conversation did.
    """
    other = INSTALLERS.alias("other")
    row = db.run(
        conn,
        sa.update(INSTALLERS)
        .where(
            INSTALLERS.c.link_token == token,
            INSTALLERS.c.chat_id.is_(None),
            ~sa.exists().where(other.c.chat_id == chat_id),
        )
        .values(chat_id=chat_id, link_token=None)
        .returning(INSTALLERS.c.id),
    ).fetchone()
    if row is None:
        return False
    job = db.run(
        conn,
        sa.select(JOBS.c.id)
        .where(JOBS.c.installer_id == row["id"], JOBS.c.state != "handed_over")
        .order_by(JOBS.c.created_at.desc())
        .limit(1),
    ).fetchone()
    if job is not None:
        start = db.run(
            conn,
            sa.update(MESSAGES)
            .where(MESSAGES.c.channel_key == channel_key)
            .values(job_id=job["id"])
            .returning(MESSAGES.c.id),
        ).fetchone()
        reply_to = int(start["id"]) if start is not None else None
        _store_reply(conn, int(job["id"]), chat_id, LINKED, reply_to=reply_to)
    return True


@durable.transaction
def apply_outcome(
    conn: db.Connection,
    job_id: int,
    version: int,
    *,
    channel_key: str,
    chat_id: int,
    description: str | None,
    previous: str,
    state: str,
    blocked_from: str | None,
    blocked_reason: str | None,
    reply: str,
    now: datetime,
    resolution: Resolution | None = None,
    tool_calls: list[ToolCall] | None = None,
) -> bool:
    """Record the message's effect on the job; a moved version (an unblock meanwhile) wins.

    A message that changes nothing (a question, a reply while blocked) keeps the version, so a
    dispatcher's unblock at the version they saw still applies. The timeline gets the change
    (`transition` or `blocked`), the resolution, each system call and each proposal; the
    proposals become rows a dispatcher confirms or rejects.
    """
    moved = state != previous
    changes: dict[str, Any] = (
        {
            "state": state,
            "blocked_from": blocked_from,
            "blocked_reason": blocked_reason,
            "version": JOBS.c.version + 1,
        }
        if moved
        else {}
    )
    changed = db.run(
        conn,
        sa.update(JOBS)
        .where(JOBS.c.id == job_id, JOBS.c.version == version)
        .values(**changes, updated_at=sa.func.now())
        .returning(JOBS.c.id),
    ).fetchone()
    if changed is None:
        return False
    inbound = db.run(
        conn,
        sa.update(MESSAGES)
        .where(MESSAGES.c.channel_key == channel_key)
        .values(job_id=job_id, photo_description=description)
        .returning(MESSAGES.c.id),
    ).fetchone()
    turn = int(inbound["id"]) if inbound is not None else None  # this message's turn
    _store_reply(conn, job_id, chat_id, reply, reply_to=turn)
    if moved:
        kind = "blocked" if state == "blocked" else "transition"
        change = {"from": previous, "to": state, "reason": blocked_reason}
        record_event(conn, job_id, kind, "agent", change, message_id=turn)
    if resolution is not None:
        summary = {"summary": resolution.summary}
        record_event(conn, job_id, "resolution", "agent", summary, message_id=turn)
    for call in tool_calls or []:
        data = call.model_dump(mode="json")
        record_event(conn, job_id, "tool_call", "agent", data, message_id=turn)
    for proposal in resolution.proposals if resolution is not None else []:
        created = db.run(
            conn,
            sa.insert(PROPOSALS)
            .values(
                job_id=job_id,
                tool=proposal.tool,
                arguments=Jsonb(proposal.arguments),
                reason=proposal.reason,
            )
            .returning(PROPOSALS.c.id),
        ).fetchone()
        if created is None:  # pragma: no cover - INSERT ... RETURNING always returns
            raise RuntimeError("the proposal row was not written")
        record_event(
            conn,
            job_id,
            "proposal",
            "agent",
            {"id": str(created["id"]), **proposal.model_dump()},
            message_id=turn,
        )
    if moved and state == "blocked":
        db.run(
            conn,
            sa.insert(DEADLINES).values(
                job_id=job_id, kind="dispatcher_response", due_at=domain.response_due(now)
            ),
        )
    if moved and state == "arrived":
        db.run(
            conn,
            sa.update(DEADLINES)
            .where(
                DEADLINES.c.job_id == job_id,
                DEADLINES.c.kind == "arrival",
                DEADLINES.c.settled_at.is_(None),
            )
            .values(settled_at=now),
        )
    return True


@durable.step
async def read_offset() -> int:
    """The next update_id to ask Telegram for."""
    async with db.aconnect() as conn:
        row = await (
            await db.arun(
                conn, sa.select(POLLING.c.offset).where(POLLING.c.bot == telegram.bot_key())
            )
        ).fetchone()
    return int(row["offset"]) if row else 0


@durable.step
def fetch_updates(offset: int) -> list[dict[str, Any]]:
    """Updates from Telegram at or after `offset`."""
    return telegram.client().get_updates(offset)


@durable.transaction
def save_offset(conn: db.Connection, offset: int) -> None:
    """Move the offset forward (never back: overlapping polls agree)."""
    db.run(
        conn,
        pg_insert(POLLING)
        .values(bot=telegram.bot_key(), offset=offset)
        .on_conflict_do_update(
            index_elements=["bot"], set_={"offset": sa.func.greatest(POLLING.c.offset, offset)}
        ),
    )


async def poll_once() -> int:
    """Fetch, store and enqueue new updates, then advance the offset.

    The offset moves only after the updates are queued: a crash in between re-reads them, and
    their workflow IDs (the update_id) collapse the repeats. Returns how many came.
    """
    offset = await read_offset()
    try:
        updates = await fetch_updates(offset)
    except httpx2.TransportError as e:
        # Telegram out of reach: the next tick asks again. A refusal (TelegramError: a revoked
        # token, a webhook set) is no network blip and still fails the workflow.
        log.warning("installation: Telegram unreachable, poll skipped: %s", e)
        return 0
    for raw in updates:
        await intake.receive(raw)
    ids = [u["update_id"] for u in updates if isinstance(u.get("update_id"), int)]
    if ids:
        await save_offset(max(ids) + 1)
    return len(updates)


@durable.workflow
async def poll(scheduled_at: datetime, context: Any) -> None:
    """The polling schedule's tick (when MAPPD_INSTALLATION_TELEGRAM_MODE=polling)."""
    del scheduled_at, context
    if not await poll_once():
        telemetry.idle()


REMINDER = "Reminder: your appointment has started. Please send a message when you are on site."


@durable.step
async def overdue_deadlines() -> list[dict[str, Any]]:
    """Due deadlines neither resolved nor acted on, with what the reminder and the event need."""
    query = (
        sa.select(
            DEADLINES.c.id,
            DEADLINES.c.kind,
            JOBS.c.external_id,
            JOBS.c.version,
            INSTALLERS.c.chat_id,
        )
        .join(JOBS, JOBS.c.id == DEADLINES.c.job_id)
        .join(INSTALLERS, INSTALLERS.c.id == JOBS.c.installer_id)
        .where(
            DEADLINES.c.settled_at.is_(None),
            DEADLINES.c.notified_at.is_(None),
            DEADLINES.c.due_at <= sa.func.now(),
        )
        .order_by(DEADLINES.c.due_at)
    )
    async with db.aconnect() as conn:
        rows = await (await db.arun(conn, query)).fetchall()
    return [dict(r) for r in rows]


@durable.transaction
def notify_deadline(conn: db.Connection, deadline_id: int) -> bool | None:
    """Mark a deadline acted on; None when another check got there first.

    It stays unsettled (and overdue on the page) until the job resolves it. The timeline gets an
    `overdue` event and, for a missed arrival, the reminder the check is about to send — not
    while the job is blocked (a technician stuck at a locked door is on site). True: remind.
    """
    row = db.run(
        conn,
        sa.update(DEADLINES)
        .where(DEADLINES.c.id == deadline_id, DEADLINES.c.notified_at.is_(None))
        .values(notified_at=sa.func.now())
        .returning(DEADLINES.c.job_id, DEADLINES.c.kind, DEADLINES.c.due_at),
    ).fetchone()
    if row is None:
        return None
    job_id, kind = int(row["job_id"]), str(row["kind"])
    due = row["due_at"].astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    record_event(conn, job_id, "overdue", "system", {"deadline": kind, "was due": due})
    if kind != "arrival":
        return False
    job = db.run(
        conn,
        sa.select(JOBS.c.state, INSTALLERS.c.chat_id)
        .join(INSTALLERS, INSTALLERS.c.id == JOBS.c.installer_id)
        .where(JOBS.c.id == job_id),
    ).fetchone()
    if job is None or job["chat_id"] is None or job["state"] == "blocked":
        return False
    _store_reply(conn, job_id, int(job["chat_id"]), REMINDER)
    return True


def _store_reply(
    conn: db.Connection, job_id: int, chat_id: int, text: str, *, reply_to: int | None = None
) -> None:
    """The bot's message; `reply_to` is the inbound message it answers, if it answers one."""
    db.run(
        conn,
        sa.insert(MESSAGES).values(
            job_id=job_id,
            chat_id=chat_id,
            direction="out",
            text=text,
            author="bot",
            reply_to=reply_to,
        ),
    )


async def check_once() -> int:
    """Act on every overdue deadline once: an event, and for a missed arrival a reminder.

    Returns how many were overdue.
    """
    overdue = await overdue_deadlines()
    for d in overdue:
        remind = await notify_deadline(d["id"])
        if remind is None:
            continue
        ext = str(d["external_id"])
        await emit(
            Event(
                # One event per deadline: a job can have two overdue at one version.
                id=f"installation:{ext}:overdue:{d['id']}",
                type="overdue",
                process="installation",
                case_id=ext,
                version=int(d["version"]),
                occurred_at=datetime.now(UTC),
                data={"kind": d["kind"], "deadline": d["id"]},
            )
        )
        if remind:
            await telegram.send_message(f"deadline:{d['id']}", int(d["chat_id"]), REMINDER)
    return len(overdue)


@durable.workflow
async def check_deadlines(scheduled_at: datetime, context: Any) -> None:
    """The deadline schedule's tick, every minute."""
    del scheduled_at, context
    if not await check_once():
        telemetry.idle()


UNBLOCKED = "Your dispatcher has cleared the issue. Please continue with the job."


@durable.step
async def load_job_by_id(external_id: str) -> dict[str, Any]:
    """The job and its installer's chat."""
    query = (
        sa.select(JOBS.c.id, JOBS.c.version, INSTALLERS.c.chat_id)
        .join(INSTALLERS, INSTALLERS.c.id == JOBS.c.installer_id)
        .where(JOBS.c.external_id == external_id)
    )
    async with db.aconnect() as conn:
        row = await (await db.arun(conn, query)).fetchone()
    if row is None:
        raise LookupError(f"no job {external_id!r}")
    return dict(row)


@durable.transaction
def claim_unblock(conn: db.Connection, job_id: int, version: int, operator: str) -> str | None:
    """Return a blocked job at `version` to where it stopped; the new state, or None.

    The reply to the installer is stored here, with the change, and sent after.
    """
    row = db.run(
        conn,
        sa.update(JOBS)
        .where(
            JOBS.c.id == job_id,
            JOBS.c.version == version,
            JOBS.c.state == "blocked",
            JOBS.c.blocked_from.is_not(None),
        )
        .values(
            state=JOBS.c.blocked_from,
            blocked_from=None,
            blocked_reason=None,
            version=JOBS.c.version + 1,
            updated_at=sa.func.now(),
        )
        .returning(JOBS.c.state),
    ).fetchone()
    if row is None:
        return None
    db.run(
        conn,
        sa.update(DEADLINES)
        .where(
            DEADLINES.c.job_id == job_id,
            DEADLINES.c.kind == "dispatcher_response",
            DEADLINES.c.settled_at.is_(None),
        )
        .values(settled_at=sa.func.now()),
    )
    state = str(row["state"])
    record_event(conn, job_id, "unblocked", f"dispatcher:{operator}", {"state": state})
    # The block is over, so what was proposed to resolve it no longer applies: a part reserved
    # days later for a job that moved on is the mistake to prevent.
    withdrawn = db.run(
        conn,
        sa.update(PROPOSALS)
        .where(PROPOSALS.c.job_id == job_id, PROPOSALS.c.status == "pending")
        .values(status="withdrawn", decided_by=operator, decided_at=sa.func.now())
        .returning(PROPOSALS.c.id, PROPOSALS.c.tool),
    ).fetchall()
    for p in withdrawn:
        data = {"id": str(p["id"]), "tool": p["tool"], "why": "the job was unblocked"}
        record_event(conn, job_id, "withdrawn", f"dispatcher:{operator}", data)
    chat = db.run(
        conn,
        sa.select(INSTALLERS.c.chat_id)
        .join(JOBS, JOBS.c.installer_id == INSTALLERS.c.id)
        .where(JOBS.c.id == job_id),
    ).fetchone()
    if chat is not None and chat["chat_id"] is not None:
        _store_reply(conn, job_id, int(chat["chat_id"]), UNBLOCKED)
    return state


@durable.workflow
async def unblock_job(external_id: str, version: int, operator: str) -> str:
    """A dispatcher clears a block: the job goes back, the installer hears it once."""
    job = await load_job_by_id(external_id)
    state = await claim_unblock(int(job["id"]), version, operator)
    if state is None:
        return "conflict"
    await emit(
        Event.of(
            "installation",
            external_id,
            "unblocked",
            version + 1,
            {"state": state, "operator": operator},
        )
    )
    if job["chat_id"] is not None:
        key = f"unblock:{external_id}:{version}"
        await telegram.send_message(key, int(job["chat_id"]), UNBLOCKED)
    return state


def confirmed_text(tool: str, arguments: dict[str, Any]) -> str:
    """What the installer hears once a dispatcher's confirmed commitment has gone through."""
    if tool == "reserve_parts":
        skus = ", ".join(arguments.get("skus") or [])
        return f"Your dispatcher has reserved the parts for your job: {skus}."
    return f"Your dispatcher has booked a delivery for your job: {arguments.get('slot')}."


@durable.action(per="proposal_id", on_unknown="resend")
def run_tool(
    idempotency_key: str, proposal_id: str, tool: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """The confirmed system call, once per proposal: the systems honor the key."""
    del proposal_id  # the key's subject
    call = {**arguments, "key": idempotency_key}
    # Actions run on a worker thread: the async MCP client gets its own event loop there.
    try:
        return asyncio.run(mcp_call(resolver.mcp_url(), tool, call))
    except mcp.McpToolError as e:
        # The system answered with an error: final, so the proposal ends `failed`, not stuck.
        return {"ok": False, "error": str(e)}


@durable.transaction
def claim_proposal(conn: db.Connection, proposal_id: str, operator: str) -> dict[str, Any] | None:
    """`pending -> confirmed` by `operator`; what the call and the reply need, or None."""
    row = db.run(
        conn,
        sa.update(PROPOSALS)
        .where(PROPOSALS.c.id == proposal_id, PROPOSALS.c.status == "pending")
        .values(status="confirmed", decided_by=operator, decided_at=sa.func.now())
        .returning(PROPOSALS.c.job_id, PROPOSALS.c.tool, PROPOSALS.c.arguments),
    ).fetchone()
    if row is None:
        return None
    job_id = int(row["job_id"])
    record_event(
        conn,
        job_id,
        "confirmed",
        f"dispatcher:{operator}",
        {"id": proposal_id, "tool": row["tool"]},
    )
    job = db.run(
        conn,
        sa.select(JOBS.c.external_id, JOBS.c.version, INSTALLERS.c.chat_id)
        .join(INSTALLERS, INSTALLERS.c.id == JOBS.c.installer_id)
        .where(JOBS.c.id == job_id),
    ).fetchone()
    if job is None:  # pragma: no cover - a proposal's job exists (foreign key)
        raise RuntimeError(f"proposal {proposal_id} has no job")
    return {
        "job_id": job_id,
        "tool": str(row["tool"]),
        "arguments": dict(row["arguments"]),
        "external_id": str(job["external_id"]),
        "version": int(job["version"]),
        "chat_id": job["chat_id"],
    }


@durable.transaction
def finish_proposal(
    conn: db.Connection,
    proposal_id: str,
    job_id: int,
    *,
    result: dict[str, Any],
    chat_id: int | None,
    reply: str | None,
) -> str:
    """`confirmed -> done | failed` with the systems' answer; the reply stored with it."""
    status = "done" if result.get("ok", True) else "failed"
    db.run(
        conn,
        sa.update(PROPOSALS)
        .where(PROPOSALS.c.id == proposal_id, PROPOSALS.c.status == "confirmed")
        .values(status=status, result=Jsonb(result)),
    )
    record_event(conn, job_id, status, "system", {"id": proposal_id, "result": result})
    if status == "done" and chat_id is not None and reply:
        _store_reply(conn, job_id, chat_id, reply)
    return status


@durable.transaction
def reject_proposal(conn: db.Connection, proposal_id: str, operator: str, note: str) -> bool:
    """`pending -> rejected`; nothing runs."""
    row = db.run(
        conn,
        sa.update(PROPOSALS)
        .where(PROPOSALS.c.id == proposal_id, PROPOSALS.c.status == "pending")
        .values(
            status="rejected",
            decided_by=operator,
            decided_at=sa.func.now(),
            result=Jsonb({"note": note}),
        )
        .returning(PROPOSALS.c.job_id),
    ).fetchone()
    if row is None:
        return False
    record_event(
        conn,
        int(row["job_id"]),
        "rejected",
        f"dispatcher:{operator}",
        {"id": proposal_id, "note": note},
    )
    return True


@durable.workflow
async def confirm_proposal(proposal_id: str, operator: str) -> str:
    """A dispatcher confirms a proposal: the system call once, the installer told once."""
    claimed = await claim_proposal(proposal_id, operator)
    if claimed is None:
        return "conflict"
    # Arguments checked per tool; the job is the proposal's own, never the model's.
    arguments = Proposal(tool=claimed["tool"], arguments=claimed["arguments"], reason="").arguments
    result = await run_tool(
        proposal_id, claimed["tool"], {**arguments, "job": claimed["external_id"]}
    )
    chat = claimed["chat_id"]
    reply = confirmed_text(claimed["tool"], arguments)
    status = await finish_proposal(
        proposal_id, claimed["job_id"], result=result, chat_id=chat, reply=reply
    )
    ext = claimed["external_id"]
    await emit(
        Event(
            id=f"installation:{ext}:{status}:{proposal_id}",
            type=status,
            process="installation",
            case_id=ext,
            version=claimed["version"],
            occurred_at=datetime.now(UTC),
            data={"proposal": proposal_id, "tool": claimed["tool"], "operator": operator},
        )
    )
    if status == "done" and chat is not None:
        await telegram.send_message(f"proposal:{proposal_id}", int(chat), reply)
    return status


@durable.transaction
def record_inbound(
    conn: db.Connection, job_id: int, channel_key: str, description: str | None
) -> None:
    """Link a stored message (and its photo's description) to the job, nothing else."""
    db.run(
        conn,
        sa.update(MESSAGES)
        .where(MESSAGES.c.channel_key == channel_key)
        .values(job_id=job_id, photo_description=description),
    )


@durable.transaction
def take_over(conn: db.Connection, external_id: str, operator: str, version: int) -> int | None:
    """A dispatcher holds the conversation from `version`; the new version, or None."""
    row = db.run(
        conn,
        sa.update(JOBS)
        .where(
            JOBS.c.external_id == external_id,
            JOBS.c.version == version,
            JOBS.c.taken_over_by.is_(None),
        )
        .values(taken_over_by=operator, version=JOBS.c.version + 1, updated_at=sa.func.now())
        .returning(JOBS.c.id, JOBS.c.version),
    ).fetchone()
    if row is None:
        return None
    record_event(conn, int(row["id"]), "takeover", f"dispatcher:{operator}")
    return int(row["version"])


@durable.transaction
def release(conn: db.Connection, external_id: str, operator: str, version: int) -> int | None:
    """The holder hands the conversation back to the agent; the new version, or None."""
    row = db.run(
        conn,
        sa.update(JOBS)
        .where(
            JOBS.c.external_id == external_id,
            JOBS.c.version == version,
            JOBS.c.taken_over_by == operator,
        )
        .values(taken_over_by=None, version=JOBS.c.version + 1, updated_at=sa.func.now())
        .returning(JOBS.c.id, JOBS.c.version),
    ).fetchone()
    if row is None:
        return None
    record_event(conn, int(row["id"]), "release", f"dispatcher:{operator}")
    return int(row["version"])


@durable.transaction
def advance_job(conn: db.Connection, external_id: str, operator: str, version: int) -> int | None:
    """Move the job one step by hand at `version` (no photo needed); the new version, or None."""
    current = db.run(
        conn,
        sa.select(JOBS.c.id, JOBS.c.state)
        .where(JOBS.c.external_id == external_id, JOBS.c.version == version)
        .with_for_update(),
    ).fetchone()
    if current is None:
        return None
    try:
        target = domain.advance(current["state"])
    except ValueError:
        return None
    job_id = int(current["id"])
    row = db.run(
        conn,
        sa.update(JOBS)
        .where(JOBS.c.id == job_id, JOBS.c.version == version)
        .values(state=target, version=JOBS.c.version + 1, updated_at=sa.func.now())
        .returning(JOBS.c.version),
    ).fetchone()
    if row is None:  # pragma: no cover - the row is locked FOR UPDATE above
        return None
    record_event(
        conn,
        job_id,
        "manual_advance",
        f"dispatcher:{operator}",
        {"from": current["state"], "to": target},
    )
    if target == "arrived":
        db.run(
            conn,
            sa.update(DEADLINES)
            .where(
                DEADLINES.c.job_id == job_id,
                DEADLINES.c.kind == "arrival",
                DEADLINES.c.settled_at.is_(None),
            )
            .values(settled_at=sa.func.now()),
        )
    return int(row["version"])


@durable.transaction
def store_dispatcher_message(
    conn: db.Connection, external_id: str, operator: str, text: str
) -> int | None:
    """Store the holder's message to the installer; the chat to send it to, or None."""
    row = db.run(
        conn,
        sa.select(JOBS.c.id, INSTALLERS.c.chat_id)
        .join(INSTALLERS, INSTALLERS.c.id == JOBS.c.installer_id)
        .where(JOBS.c.external_id == external_id, JOBS.c.taken_over_by == operator),
    ).fetchone()
    if row is None or row["chat_id"] is None:
        return None
    db.run(
        conn,
        sa.insert(MESSAGES).values(
            job_id=row["id"],
            chat_id=row["chat_id"],
            direction="out",
            text=text,
            author=f"dispatcher:{operator}",
        ),
    )
    return int(row["chat_id"])


@durable.workflow
async def dispatcher_message(external_id: str, operator: str, text: str, key: str) -> str:
    """The holder's message, stored and sent once per idempotency key."""
    chat = await store_dispatcher_message(external_id, operator, text)
    if chat is None:
        return "not_holder"
    await telegram.send_message(f"dispatcher:{key}", chat, text)
    return "sent"
