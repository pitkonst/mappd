"""Telegram updates in: store once, enqueue once per update, one at a time per job.

The update's `update_id` is both the stored message's unique key and the workflow ID, so a
redelivery (webhook retry, a poll after a crash) stores nothing and runs nothing new. The DBOS
queue is partitioned by chat: an installer has one chat and one open job, so a chat's messages
run in order (linking mid-stream included) and different chats in parallel.
"""

from typing import Any

import sqlalchemy as sa
from dbos import DBOS, Queue, SetEnqueueOptions, SetWorkflowID
from sqlalchemy.dialects.postgresql import insert as pg_insert

from mappd import db, durable
from mappd.channels.telegram import Update, parse

from . import domain
from .db import INSTALLERS, JOBS, MESSAGES

QUEUE_NAME = "installation.jobs"


async def queue() -> Queue:
    """The jobs queue, one workflow at a time per job (registered on first use, after launch)."""
    found = await DBOS.retrieve_queue_async(QUEUE_NAME)
    if found is not None:
        return found
    return await DBOS.register_queue_async(QUEUE_NAME, partition_concurrency=1)


async def active_job(chat_id: int) -> int | None:
    """The open job of the installer linked to `chat_id`, if any."""
    query = (
        sa.select(JOBS.c.id)
        .join(INSTALLERS, INSTALLERS.c.id == JOBS.c.installer_id)
        .where(INSTALLERS.c.chat_id == chat_id, JOBS.c.state != "handed_over")
        .order_by(JOBS.c.created_at.desc())
        .limit(1)
    )
    async with db.aconnect() as conn:
        row = await (await db.arun(conn, query)).fetchone()
    return int(row["id"]) if row else None


@durable.step
async def store(update: Update, job_id: int | None) -> bool:
    """Record the inbound message once; False when this update was seen before."""
    async with db.aconnect() as conn:
        row = await (
            await db.arun(
                conn,
                pg_insert(MESSAGES)
                .values(
                    job_id=job_id,
                    chat_id=update.chat_id,
                    direction="in",
                    channel_key=str(update.update_id),
                    # a /start link's token is single-use, but it is a secret: never shown
                    text="/start" if update.start_token is not None else update.text,
                    photo_ref=update.photo,
                )
                .on_conflict_do_nothing(index_elements=["channel_key"])
                .returning(MESSAGES.c.id),
            )
        ).fetchone()
    return row is not None


async def enqueue(update: Update, job_id: int | None) -> None:
    """Queue the update's workflow on its chat's partition, stable before and after linking."""
    from .workflow import handle_update  # noqa: PLC0415 - the workflow imports intake

    del job_id  # the job is resolved when the workflow runs, in the chat's order
    with (
        SetWorkflowID(domain.update_workflow_id(update.update_id)),
        SetEnqueueOptions(queue_partition_key=f"chat:{update.chat_id}"),
    ):
        await (await queue()).enqueue_async(handle_update, update.model_dump())


async def receive(raw: dict[str, Any]) -> None:
    """One raw update in: parse, store, enqueue (a repeat collapses onto the same workflow)."""
    update = parse(raw)
    if update is None:
        return
    job_id = await active_job(update.chat_id)
    await store(update, job_id)
    await enqueue(update, job_id)
