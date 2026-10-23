"""The installation dispatcher's service layer: the API and the page call only these."""

import asyncio
import secrets
from typing import Any

import sqlalchemy as sa
from dbos import DBOS, SetWorkflowID
from psycopg.types.json import Jsonb

from mappd import db, ids
from mappd.events import Event, emit

from . import domain, telegram
from .db import DEADLINES, EVENTS, INSTALLERS, JOBS, MESSAGES, PROPOSALS
from .schemas import JobIn, JobOut, Message, ProposalOut, TimelineEntry


class NotFoundError(LookupError):
    """No job has this external_id."""


class DuplicateJobError(ValueError):
    """A job with this external_id exists."""


class OpenJobError(ValueError):
    """The installer still has an open job (one at a time: their messages go to it)."""


class NotHolderError(PermissionError):
    """Only the dispatcher holding the conversation may write in it or release it."""


class UpstreamError(RuntimeError):
    """A confirmed commitment could not be carried out (the system or the gateway failed)."""


class StaleVersionError(Exception):
    """The job moved on, or is not blocked."""


def _token() -> str:
    return secrets.token_urlsafe(16)


async def create_job(job: JobIn) -> JobOut:
    """A job for an installer (created if new) with its arrival deadline; a deep link to link.

    Raises:
        DuplicateJobError: a job with this external_id exists.
        OpenJobError: the installer has a job that is not handed over yet.
    """
    async with db.aconnect() as conn, conn.transaction():
        found = await (
            await db.arun(conn, sa.select(JOBS.c.id).where(JOBS.c.external_id == job.external_id))
        ).fetchone()
        if found is not None:
            raise DuplicateJobError(f"job {job.external_id!r} exists")
        installer = await (
            await db.arun(
                conn, sa.select(INSTALLERS).where(INSTALLERS.c.name == job.installer_name)
            )
        ).fetchone()
        if installer is not None:
            open_job = await (
                await db.arun(
                    conn,
                    sa.select(JOBS.c.external_id).where(
                        JOBS.c.installer_id == installer["id"], JOBS.c.state != "handed_over"
                    ),
                )
            ).fetchone()
            if open_job is not None:
                raise OpenJobError(
                    f"{job.installer_name!r} has an open job {open_job['external_id']!r}"
                )
        if installer is None:
            installer = await (
                await db.arun(
                    conn,
                    sa.insert(INSTALLERS)
                    .values(name=job.installer_name, link_token=_token())
                    .returning(INSTALLERS),
                )
            ).fetchone()
        elif installer["chat_id"] is None and installer["link_token"] is None:
            installer = await (
                await db.arun(
                    conn,
                    sa.update(INSTALLERS)
                    .where(INSTALLERS.c.id == installer["id"])
                    .values(link_token=_token())
                    .returning(INSTALLERS),
                )
            ).fetchone()
        if installer is None:  # pragma: no cover - INSERT ... RETURNING always returns
            raise RuntimeError("the installer row was not written")
        row = await (
            await db.arun(
                conn,
                sa.insert(JOBS)
                .values(
                    external_id=job.external_id,
                    installer_id=installer["id"],
                    site=job.site,
                    skus=Jsonb(job.skus),
                    appointment_at=job.appointment_at,
                )
                .returning(JOBS.c.id),
            )
        ).fetchone()
        if row is None:  # pragma: no cover - INSERT ... RETURNING always returns
            raise RuntimeError("the job row was not written")
        await db.arun(
            conn,
            sa.insert(DEADLINES).values(
                job_id=row["id"], kind="arrival", due_at=domain.arrival_due(job.appointment_at)
            ),
        )
    return await get_job(job.external_id)


def _job_query() -> sa.Select[Any]:
    return sa.select(JOBS, INSTALLERS.c.name.label("installer_name"), INSTALLERS.c.link_token).join(
        INSTALLERS, INSTALLERS.c.id == JOBS.c.installer_id
    )


async def _out(conn: db.AsyncConnection, row: Any, *, messages: bool) -> JobOut:
    overdue = await (
        await db.arun(
            conn,
            sa.select(DEADLINES.c.kind).where(
                DEADLINES.c.job_id == row["id"],
                DEADLINES.c.settled_at.is_(None),
                DEADLINES.c.due_at <= sa.func.now(),
            ),
        )
    ).fetchall()
    history: list[Message] = []
    proposals = await _proposals(conn, int(row["id"]))  # the list marks a pending one
    if messages:
        rows = await (
            await db.arun(
                conn,
                sa.select(MESSAGES).where(MESSAGES.c.job_id == row["id"]).order_by(MESSAGES.c.id),
            )
        ).fetchall()
        history = [Message.model_validate(dict(m)) for m in rows]
    token = row["link_token"]
    return JobOut.model_validate(
        {
            **dict(row),
            "deep_link": domain.deep_link(telegram.bot_name(), token) if token else None,
            "overdue": [o["kind"] for o in overdue],
            "messages": history,
            "proposals": proposals,
        }
    )


async def _proposals(conn: db.AsyncConnection, job_id: int) -> list[ProposalOut]:
    rows = await (
        await db.arun(
            conn,
            sa.select(PROPOSALS)
            .where(PROPOSALS.c.job_id == job_id)
            .order_by(PROPOSALS.c.created_at, PROPOSALS.c.id),
        )
    ).fetchall()
    return [ProposalOut.model_validate({**dict(r), "id": str(r["id"])}) for r in rows]


async def _proposal(external_id: str, proposal_id: str) -> ProposalOut:
    """A proposal of this job.

    Raises:
        NotFoundError: no such job, or the proposal is another job's (or not a UUID).
    """
    job = await get_job(external_id)
    for proposal in job.proposals:
        if proposal.id == proposal_id:
            return proposal
    raise NotFoundError(f"job {external_id!r} has no proposal {proposal_id!r}")


async def confirm(external_id: str, proposal_id: str, operator: str) -> ProposalOut:
    """Run a proposal's commitment once and tell the installer; a repeat answers the result.

    Raises:
        NotFoundError: no such job or proposal.
        StaleVersionError: the proposal was rejected.
    """
    from .workflow import confirm_proposal  # noqa: PLC0415 - the workflow imports the service's peers

    proposal = await _proposal(external_id, proposal_id)
    if proposal.status == "rejected":
        raise StaleVersionError(f"proposal {proposal_id} was rejected")
    with SetWorkflowID(ids.decision("installation.proposal", proposal_id, 1)):
        handle = await DBOS.start_workflow_async(confirm_proposal, proposal_id, operator)
    try:
        result = await handle.get_result()
    except Exception as e:
        now = await _proposal(external_id, proposal_id)
        raise UpstreamError(
            f"confirming proposal {proposal_id} failed ({type(e).__name__}); it is {now.status}"
            " — retry it with `mappd workflow retry`"
        ) from e
    if result == "conflict":
        raise StaleVersionError(f"proposal {proposal_id} is no longer pending")
    return await _proposal(external_id, proposal_id)


async def reject(external_id: str, proposal_id: str, operator: str, note: str) -> ProposalOut:
    """Turn a pending proposal down; nothing runs.

    Raises:
        NotFoundError: no such job or proposal.
        StaleVersionError: the proposal is no longer pending.
    """
    from .workflow import reject_proposal  # noqa: PLC0415 - the workflow imports the service's peers

    await _proposal(external_id, proposal_id)
    if not await reject_proposal(proposal_id, operator, note):
        raise StaleVersionError(f"proposal {proposal_id} is no longer pending")
    return await _proposal(external_id, proposal_id)


async def get_job(external_id: str) -> JobOut:
    """One job with its conversation.

    Raises:
        NotFoundError: no such job.
    """
    async with db.aconnect() as conn:
        row = await (
            await db.arun(conn, _job_query().where(JOBS.c.external_id == external_id))
        ).fetchone()
        if row is None:
            raise NotFoundError(f"no job {external_id!r}")
        return await _out(conn, row, messages=True)


async def list_jobs() -> list[JobOut]:
    """Every job, newest first, without conversations."""
    async with db.aconnect() as conn:
        rows = await (
            await db.arun(conn, _job_query().order_by(JOBS.c.created_at.desc()))
        ).fetchall()
        return [await _out(conn, r, messages=False) for r in rows]


async def unblock(external_id: str, version: int, operator: str) -> JobOut:
    """Return a blocked job to where it stopped and tell the installer, once per decision.

    Raises:
        NotFoundError: no such job.
        StaleVersionError: the job moved on or is not blocked.
    """
    from .workflow import unblock_job  # noqa: PLC0415 - the workflow imports the service's peers

    await get_job(external_id)
    with SetWorkflowID(ids.decision("installation.unblock", external_id, version)):
        handle = await DBOS.start_workflow_async(unblock_job, external_id, version, operator)
    try:
        result = await handle.get_result()
    except Exception:
        # The reply failed after the unblock committed (the outbox holds the send for a
        # person): the job is unblocked all the same, so answer it.
        job = await get_job(external_id)
        if job.state == "blocked" or job.version <= version:
            raise
        return job
    if result == "conflict":
        raise StaleVersionError(f"job {external_id!r} changed or is not blocked")
    return await get_job(external_id)


async def _changed(external_id: str, kind: str, version: int) -> JobOut:
    """Tell the pages a job changed, and return it."""
    await emit(Event.of("installation", external_id, kind, version))
    return await get_job(external_id)


async def take_over(external_id: str, operator: str, version: int) -> JobOut:
    """Hold the conversation: the agent records the installer's messages and stays silent.

    Raises:
        NotFoundError: no such job.
        StaleVersionError: the job moved on, or another dispatcher holds it.
    """
    from .workflow import take_over as hold  # noqa: PLC0415 - the workflow imports the service's peers

    await get_job(external_id)
    new = await hold(external_id, operator, version)
    if new is None:
        raise StaleVersionError(f"job {external_id!r} changed or is held by someone else")
    return await _changed(external_id, "takeover", new)


async def release(external_id: str, operator: str, version: int) -> JobOut:
    """Hand the conversation back to the agent.

    Raises:
        NotFoundError: no such job.
        NotHolderError: `operator` does not hold it.
        StaleVersionError: the job moved on.
    """
    from .workflow import release as hand_back  # noqa: PLC0415 - the workflow imports the service's peers

    job = await get_job(external_id)
    if job.taken_over_by != operator:
        raise NotHolderError(f"{operator!r} does not hold job {external_id!r}")
    new = await hand_back(external_id, operator, version)
    if new is None:
        raise StaleVersionError(f"job {external_id!r} changed")
    return await _changed(external_id, "release", new)


async def send(external_id: str, operator: str, text: str, key: str) -> JobOut:
    """The holder's message to the installer, sent once per idempotency key.

    Raises:
        NotFoundError: no such job.
        NotHolderError: `operator` does not hold the conversation.
    """
    from .workflow import dispatcher_message  # noqa: PLC0415 - the workflow imports the service's peers

    job = await get_job(external_id)
    with SetWorkflowID(ids.from_key("installation.dispatcher_message", key)):
        handle = await DBOS.start_workflow_async(
            dispatcher_message, external_id, operator, text, key
        )
    if await handle.get_result() == "not_holder":
        raise NotHolderError(f"{operator!r} does not hold job {external_id!r}")
    return await _changed(external_id, "message", job.version)


async def advance(external_id: str, operator: str, version: int) -> JobOut:
    """Move the job one step by hand (no photo needed).

    Raises:
        NotFoundError: no such job.
        StaleVersionError: the job moved on, is blocked or is handed over.
    """
    from .workflow import advance_job  # noqa: PLC0415 - the workflow imports the service's peers

    await get_job(external_id)
    new = await advance_job(external_id, operator, version)
    if new is None:
        raise StaleVersionError(f"job {external_id!r} changed or cannot advance")
    return await _changed(external_id, "manual_advance", new)


async def _job_id(conn: db.AsyncConnection, external_id: str) -> int:
    row = await (
        await db.arun(conn, sa.select(JOBS.c.id).where(JOBS.c.external_id == external_id))
    ).fetchone()
    if row is None:
        raise NotFoundError(f"no job {external_id!r}")
    return int(row["id"])


async def timeline(external_id: str) -> list[TimelineEntry]:
    """The job's messages and events merged in the order they happened.

    Raises:
        NotFoundError: no such job.
    """
    async with db.aconnect() as conn:
        job_id = await _job_id(conn, external_id)
        messages = await (
            await db.arun(conn, sa.select(MESSAGES).where(MESSAGES.c.job_id == job_id))
        ).fetchall()
        events = await (
            await db.arun(conn, sa.select(EVENTS).where(EVENTS.c.job_id == job_id))
        ).fetchall()
    # A turn stays together: the message, the events it caused, then the reply to it, even
    # when a faster message arrived in between. Rows of no turn sort by their own time.
    sent_at = {int(m["id"]): m["created_at"] for m in messages}

    def key(at: Any, rank: int, row_id: int, turn: int | None) -> tuple[Any, ...]:
        if turn is not None and turn in sent_at:
            return (sent_at[turn], 0, turn, rank, at, row_id)
        own = 0 if rank != 1 else 1
        return (at, own, row_id, 0, at, row_id)

    rows: list[tuple[tuple[Any, ...], TimelineEntry]] = [
        (
            key(
                m["created_at"], 2 if m["reply_to"] is not None else 0, int(m["id"]), m["reply_to"]
            ),
            TimelineEntry(
                at=m["created_at"],
                kind="message",
                actor=m["author"],
                text=m["text"],
                photo=m["photo_ref"],
                photo_description=m["photo_description"],
            ),
        )
        for m in messages
    ]
    rows += [
        (
            key(e["created_at"], 1, int(e["id"]), e["message_id"]),
            TimelineEntry(
                at=e["created_at"], kind="event", actor=e["actor"], event=e["kind"], data=e["data"]
            ),
        )
        for e in events
    ]
    return [entry for _, entry in sorted(rows, key=lambda r: r[0])]


async def photo(external_id: str, file_id: str) -> bytes:
    """A photo the installer sent on this job, fetched from Telegram (the token stays here).

    Raises:
        NotFoundError: no such job, or no message of this job carried that photo.
    """
    async with db.aconnect() as conn:
        job_id = await _job_id(conn, external_id)
        found = await (
            await db.arun(
                conn,
                sa.select(MESSAGES.c.id).where(
                    MESSAGES.c.job_id == job_id,
                    MESSAGES.c.direction == "in",
                    MESSAGES.c.photo_ref == file_id,
                ),
            )
        ).fetchone()
    if found is None:
        raise NotFoundError(f"job {external_id!r} has no photo {file_id!r}")
    return await asyncio.to_thread(telegram.client().get_file, file_id)
