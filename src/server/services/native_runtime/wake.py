"""A notice a native turn takes in while it runs.

The PTC lane reads the steers the server sends into a live turn between the
model's calls. A native CLI turn has no such step: the graph hands the prompt
to the CLI, and the CLI owns the turn until it ends. The notice is therefore
taken here, between the events the CLI streams, and the turn that opens next
carries it on the same CLI session.

The rows are the ones the PTC lane takes (the outbox's ``steer`` jobs), so a
notice is read once, whichever lane reads it. A failed read leaves the notice
parked: the turn's ending settles it as the next turn's input, so it is never
lost.
"""

from __future__ import annotations

import contextvars
import logging

logger = logging.getLogger(__name__)

#: How many notices one turn takes in at a time.
_TAKE_LIMIT = 10

#: Job ids this task is holding and has not accepted. The hold is in Postgres;
#: this map is the execution context of the worker running the turn, so it
#: can accept or give the rows back. Another worker does not read it.
_held_notices: contextvars.ContextVar[dict[str, list[str]] | None] = (
    contextvars.ContextVar("native_notice_holds", default=None)
)


def _hold_map() -> dict[str, list[str]]:
    current = _held_notices.get()
    if current is None:
        current = {}
        _held_notices.set(current)
    return current


def _key(thread_id: str, run_id: str) -> str:
    return f"{thread_id}:{run_id}"


async def take_native_notice(thread_id: str, run_id: str) -> str:
    """The notice sent into this run while it was running, or ``""``.

    The rows are held, not confirmed. Confirming here would acknowledge the
    notice before the replacement turn starts, so a cancel or a restart
    failure would drop it. ``accept_native_notice`` confirms them once that
    turn's state is being checkpointed. ``release_native_notice`` gives them
    back when that turn does not start.
    """
    if not thread_id or not run_id:
        return ""
    from src.server.database.runs import outbox as outbox_db

    try:
        jobs = await outbox_db.list_takeable_jobs(
            thread_id, ["steer"], run_id=run_id, limit=_TAKE_LIMIT
        )
        if not jobs:
            return ""
        ids = [str(job["hook_outbox_id"]) for job in jobs]
        held = set(await outbox_db.hold_jobs(ids, owner=("run", run_id)))
    except Exception:  # noqa: BLE001 - a failed read leaves the notice parked
        logger.warning(
            "[NATIVE_WAKE] Taking a notice for thread=%s run=%s failed",
            thread_id,
            run_id,
            exc_info=True,
        )
        return ""
    taken = [job for job in jobs if str(job["hook_outbox_id"]) in held]
    if not taken:
        return ""
    notice = "\n\n".join(
        text
        for text in (
            str((job.get("payload") or {}).get("content") or "") for job in taken
        )
        if text
    )
    owed = _hold_map().setdefault(_key(thread_id, run_id), [])
    for job in taken:
        job_id = str(job["hook_outbox_id"])
        if job_id not in owed:
            owed.append(job_id)
    if notice:
        logger.info(
            "[NATIVE_WAKE] Thread %s run %s held %d notice(s)",
            thread_id,
            run_id,
            len(taken),
        )
    return notice


async def accept_native_notice(thread_id: str, run_id: str) -> None:
    """Confirm notices once the turn that carries them is being checkpointed.

    Nothing is confirmed when this task is not holding any. A confirm that
    fails leaves the ids held, so the caller can give the rows back.
    """
    if not thread_id or not run_id:
        return
    owed = _hold_map().get(_key(thread_id, run_id))
    if not owed:
        return
    from src.server.database.runs import outbox as outbox_db

    await outbox_db.confirm_jobs(list(owed), owner=("run", run_id))
    _hold_map().pop(_key(thread_id, run_id), None)


async def release_native_notice(thread_id: str, run_id: str) -> None:
    """Give held notices back. A failed replacement turn must not consume them."""
    if not thread_id or not run_id:
        return
    owed = _hold_map().get(_key(thread_id, run_id))
    if not owed:
        return
    from src.server.database.runs import outbox as outbox_db

    try:
        await outbox_db.release_jobs(list(owed), owner=("run", run_id))
    except Exception:  # noqa: BLE001 - the hold stays for the run's ending
        logger.warning(
            "[NATIVE_WAKE] Releasing a notice for thread=%s run=%s failed",
            thread_id,
            run_id,
            exc_info=True,
        )
        return
    _hold_map().pop(_key(thread_id, run_id), None)


__all__ = [
    "accept_native_notice",
    "release_native_notice",
    "take_native_notice",
]
