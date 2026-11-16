"""The frozen branch HEAD a native report's delivery is measured against.

A native Worker reports when it is done, and the delivery check that follows
reads the workspace some time later. The branch can move in between — a
retry's commit, an operator's push, a pull — and then the count of delivered
commits and the summary of the delivered tree describe work this Ticket never
did, and nothing records which HEAD they were read from.

This module resolves that HEAD once, at report time, records it in the Ticket
mandate (a JSONB column, so a freeze needs no migration), and pins the
delivery to it: the commit count and the summary then cover only the commits
reachable from the frozen HEAD. A commit that lands after the freeze is not
this delivery's, and the mandate says which commit the delivery was measured
against.

Three states are kept apart on purpose. A branch that held no commit when the
freeze was taken is an *empty* freeze: the delivery covered nothing, and a
commit that lands later is still not this delivery's. A HEAD that could not
be read at all is *unresolved*: there is no frozen commit to measure against,
so the delivery is unknown and a commit that lands later cannot confirm it.
A workspace that is not a Git repository has no delivery to check at all.

The summary belongs to the freeze. It is captured with it, from the same
reading of the workspace, and recorded beside it: a settlement that reuses
the freeze reuses the summary, so a later commit or edit cannot change what
the delivery says.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from typing import Any, Mapping

from src.server.services.autonomy.git_delivery import (
    GitDeliveryError,
    GitSnapshot,
    WorkspaceHead,
    _UNSAFE_STATUS,
    _is_object_id,
    capture_git_snapshot,
    resolve_workspace_head,
    run_isolated_git_readonly,
    safe_git_commit_delivery,
    safe_git_workspace_summary,
)

logger = logging.getLogger(__name__)

#: The mandate key that holds the frozen branch HEAD. The mandate is a JSONB
#: column, so recording a freeze needs no migration.
DELIVERY_FREEZE_KEY = "git_delivery_freeze"

#: The mandate key that holds the delivery a frozen HEAD produced.
DELIVERY_RESULT_KEY = "git_delivery_result"

#: The version of the frozen-HEAD record.
DELIVERY_FREEZE_VERSION = 1

#: The version of the delivery result a frozen HEAD produced.
DELIVERY_RESULT_VERSION = 2

#: The status a Ticket must hold while its delivery state is written.
DELIVERY_WRITE_STATUS = "running"


class DeliveryStateNotDurable(RuntimeError):
    """The frozen delivery state could not be recorded, so it is not confirmed.

    A Ticket whose delivery cannot be recorded must not settle on it: the
    commit it claims would have no record of which commit it measured.
    """


@dataclass(frozen=True)
class DeliveryFreeze:
    """The branch HEAD a delivery is measured against, and where it was read."""

    #: The commit the branch pointed at when the freeze was taken. None with
    #: ``known`` set means the branch held no commit yet.
    head: str | None = None
    #: The branch that commit was read from, when the workspace was on one.
    branch: str | None = None
    #: The workspace identity the head belongs to, so a record recorded for
    #: another checkout is never read as this one's.
    root_id: str | None = None
    #: False only when HEAD could not be read at all. An unborn branch reads
    #: as known with no head: the branch exists and has no commit yet.
    known: bool = False
    #: The run the freeze belongs to, so a later attempt never reads it.
    thread_id: str = ""
    run_id: str = ""
    attempt: int = 0
    #: The commits reachable from the frozen HEAD that the baseline does not
    #: hold, or None when the range could not be counted.
    commits: int | None = None
    #: The workspace summary captured together with this freeze: the state of
    #: the delivery at the frozen commit. It belongs to the freeze, so a
    #: later settlement reuses it instead of reading the live workspace
    #: again; None when the freeze carries no summary of its own.
    summary: str | None = None

    @property
    def resolved(self) -> bool:
        """Whether HEAD was read: a commit, or a branch that held none."""
        return self.known

    @property
    def empty(self) -> bool:
        """Whether the branch held no commit when the freeze was taken."""
        return self.known and self.head is None

    def to_record(self) -> dict[str, Any]:
        """The persisted form: the frozen HEAD, the run it belongs to and the
        workspace summary that was captured with it."""
        return {
            "version": DELIVERY_FREEZE_VERSION,
            "known": self.known,
            "empty": self.empty,
            "head": self.head if self.known else None,
            "branch": self.branch,
            "root_id": self.root_id,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "attempt": self.attempt,
            "commits": self.commits,
            "summary": self.summary,
        }

    @classmethod
    def from_record(cls, value: Any) -> "DeliveryFreeze | None":
        """The freeze a record holds, or None when it holds no usable one."""
        if not isinstance(value, Mapping):
            return None
        if value.get("version") != DELIVERY_FREEZE_VERSION:
            return None
        head = value.get("head")
        if head is not None and not _is_object_id(head):
            return None
        known = value.get("known") is True
        if known and value.get("empty") is not True and head is None:
            # A known freeze that names no commit is the empty one; a record
            # that claims to know a commit and names none is not readable.
            return None
        branch = value.get("branch")
        root_id = value.get("root_id")
        commits = value.get("commits")
        attempt = value.get("attempt")
        summary = value.get("summary")
        return cls(
            head=head,
            branch=branch if isinstance(branch, str) else None,
            root_id=root_id if isinstance(root_id, str) else None,
            known=known,
            thread_id=str(value.get("thread_id") or ""),
            run_id=str(value.get("run_id") or ""),
            attempt=attempt if isinstance(attempt, int) else 0,
            commits=commits if isinstance(commits, int) else None,
            summary=summary if isinstance(summary, str) else None,
        )


@dataclass(frozen=True)
class FrozenDelivery:
    """What a frozen HEAD says about a Ticket's delivery."""

    #: The delivery verdict: delivered, missing, unknown, unverified or
    #: not_checked (see ``git_delivery.safe_git_commit_delivery``).
    verdict: str = "not_checked"
    #: The workspace summary, taken against the frozen HEAD.
    summary: str | None = None
    #: The commits reachable from the frozen HEAD that the baseline does not
    #: hold, or None when the range could not be counted.
    commits: int | None = None
    #: The freeze the measurement was made against.
    freeze: DeliveryFreeze | None = None


def resolve_delivery_head(cwd: str) -> DeliveryFreeze:
    """The branch HEAD of ``cwd`` right now, read with the hardened runner."""
    head: WorkspaceHead = resolve_workspace_head(cwd)
    return DeliveryFreeze(
        head=head.head,
        branch=head.branch,
        root_id=head.root_id,
        known=head.known,
    )


def capture_delivery_snapshot(cwd: str) -> tuple[DeliveryFreeze, GitSnapshot]:
    """The freeze and the workspace state of ``cwd``, read at one instant.

    The snapshot carries the HEAD the freeze names, so both describe the
    same moment: the commit the delivery is measured against and the
    workspace the summary is taken from are one reading, not two.
    """
    snapshot = capture_git_snapshot(cwd)
    try:
        head: WorkspaceHead = resolve_workspace_head(cwd)
    except (GitDeliveryError, OSError):
        head = WorkspaceHead()
    freeze = DeliveryFreeze(
        head=snapshot.head if snapshot.head_known else None,
        branch=head.branch,
        root_id=snapshot.root_id,
        known=snapshot.head_known,
    )
    return freeze, snapshot


def frozen_state(mandate: Mapping[str, Any] | None) -> DeliveryFreeze | None:
    """The freeze a mandate records, when it records one."""
    if not isinstance(mandate, Mapping):
        return None
    return DeliveryFreeze.from_record(mandate.get(DELIVERY_FREEZE_KEY))


def frozen_head(mandate: Mapping[str, Any] | None) -> str | None:
    """The frozen HEAD a mandate records, when it records a commit."""
    freeze = frozen_state(mandate)
    if freeze is None or not freeze.resolved or freeze.head is None:
        return None
    return freeze.head


def bound_freeze(
    mandate: Mapping[str, Any] | None,
    *,
    thread_id: str,
    run_id: str,
    attempt: int,
) -> DeliveryFreeze | None:
    """The freeze a mandate records for this thread, run and attempt.

    A freeze belongs to the run that took it: a later attempt measures
    against its own HEAD, never against the one an earlier attempt froze.

    A freeze whose HEAD could not be read is returned too: the report's
    delivery stays unknown on every retry, instead of a retry reading the
    live branch and letting a commit made after the report confirm it.
    """
    freeze = frozen_state(mandate)
    if freeze is None:
        return None
    if freeze.thread_id != thread_id or freeze.run_id != run_id:
        return None
    if freeze.attempt != attempt:
        return None
    return freeze


def frozen_commit_count(cwd: str, base_head: str, tip: str) -> int | None:
    """The commits reachable from ``tip`` that ``base_head`` does not hold.

    None when the range cannot be counted: both ends must be commits of this
    workspace, and a range Git cannot resolve is not a count of zero.
    """
    if not _is_object_id(base_head) or not _is_object_id(tip):
        return None
    try:
        result = run_isolated_git_readonly(
            cwd, ["rev-list", "--count", f"{base_head}..{tip}"], timeout=5
        )
    except (GitDeliveryError, OSError):
        return None
    count = result.stdout.decode("ascii", errors="replace").strip()
    if result.returncode != 0 or not count.isdigit():
        return None
    return int(count)


def frozen_delivery(
    cwd: str,
    baseline: GitSnapshot | None,
    freeze: DeliveryFreeze | None,
    *,
    baseline_unavailable: bool = False,
    current: GitSnapshot | None = None,
    summary: str | None = None,
) -> FrozenDelivery:
    """The delivery verdict and summary, limited to the frozen HEAD.

    A freeze that names a commit measures the range up to that commit. A
    freeze that names none (the branch held no commit when it was taken)
    measures a range that ends nowhere: the delivery covered nothing, and a
    commit that lands later is not this delivery's.

    A freeze that could not be read at all measures nothing: the delivery is
    unknown, so a commit that lands later cannot confirm a delivery nobody
    froze. A workspace that is not a Git repository has no delivery to check
    at all.

    ``current`` is the workspace snapshot taken with the freeze, and
    ``summary`` the summary captured with it. A measurement that has neither
    does not read the live workspace: the live workspace is not the one the
    freeze was taken from, so it cannot say what was left uncommitted then.
    """
    if freeze is None or not freeze.resolved:
        if not baseline_unavailable and (baseline is None or baseline.root_id is None):
            # Not a Git workspace: there is no delivery to check.
            return FrozenDelivery(
                verdict="not_checked", summary=None, commits=None, freeze=freeze
            )
        # The HEAD could not be read, so there is no frozen commit to measure
        # against. The delivery is unknown: a commit that lands later must
        # not confirm a delivery nobody froze.
        return FrozenDelivery(
            verdict="unknown",
            summary=_UNSAFE_STATUS,
            commits=None,
            freeze=freeze,
        )
    tip = freeze.head
    if summary is None:
        summary = (
            _UNSAFE_STATUS
            if current is None
            else safe_git_workspace_summary(
                cwd,
                baseline,
                baseline_unavailable=baseline_unavailable,
                tip=tip,
                current=current,
            )
        )
    verdict = safe_git_commit_delivery(
        cwd,
        baseline,
        baseline_unavailable=baseline_unavailable,
        tip=tip,
        frozen=True,
    )
    commits = None
    if tip is not None and baseline is not None and baseline.head:
        commits = frozen_commit_count(cwd, baseline.head, tip)
    return FrozenDelivery(
        verdict=verdict, summary=summary, commits=commits, freeze=freeze
    )


def delivery_state_fields(
    freeze: DeliveryFreeze,
    delivery: FrozenDelivery,
    *,
    thread_id: str,
    run_id: str,
    attempt: int,
    baseline_clean: bool = False,
) -> dict[str, Any]:
    """The mandate fields a frozen delivery is recorded with.

    The freeze and the delivery it produced are written together: a result
    without its freeze says nothing about which commit it measured, and a
    freeze without its result measures nothing.
    """
    return {
        DELIVERY_FREEZE_KEY: replace(
            freeze,
            thread_id=thread_id,
            run_id=run_id,
            attempt=attempt,
            commits=delivery.commits,
            # The summary belongs to the freeze: it was captured with it, so
            # a settlement that reuses this freeze reuses this summary too.
            summary=delivery.summary,
        ).to_record(),
        DELIVERY_RESULT_KEY: {
            "version": DELIVERY_RESULT_VERSION,
            "thread_id": thread_id,
            "run_id": run_id,
            "attempt": attempt,
            "summary": delivery.summary or "",
            "commit_delivery": delivery.verdict,
            # No uncommitted work before the run: an unchanged clean
            # workspace then really holds nothing undelivered.
            "baseline_clean": baseline_clean,
            # The commit the delivery was measured against. A frozen empty
            # branch names no commit and is still a frozen measurement.
            "frozen": freeze.resolved,
            "frozen_head": freeze.head if freeze.resolved else None,
            "commits": delivery.commits,
        },
    }


async def record_delivery_state(
    ticket_id: str,
    fields: Mapping[str, Any],
    *,
    thread_id: str,
    run_id: str = "",
    attempt: int = 0,
    ticket_db: Any = None,
    tries: int = 3,
) -> bool:
    """Write the frozen delivery state into the Ticket mandate.

    The mandate is read again and written with a revision fence, so a
    concurrent write — a native run's own delivery result, a stop — is never
    overwritten by this record, and this record never overwrites it. A lost
    race is retried with the same frozen HEAD, so a retry measures the same
    commit rather than a later one.
    """
    from src.server.database.autonomy import tickets as default_ticket_db

    db = default_ticket_db if ticket_db is None else ticket_db
    for _ in range(max(1, tries)):
        try:
            ticket = await db.get_ticket(ticket_id)
            if ticket is None:
                return False
            mandate = dict(ticket.get("mandate") or {})
            mandate.update(fields)
            kwargs: dict[str, Any] = {
                "expected_status": DELIVERY_WRITE_STATUS,
                "expected_thread_id": thread_id,
                "mandate": mandate,
            }
            if run_id:
                kwargs["expected_run_id"] = run_id
            revision = ticket.get("revision")
            if isinstance(revision, int):
                kwargs["expected_revision"] = revision
            updated = await db.update_ticket(str(ticket["ticket_id"]), **kwargs)
        except Exception:  # noqa: BLE001 - the delivery was already measured
            logger.warning(
                "[autonomy] delivery freeze could not be recorded ticket=%s",
                ticket_id,
                exc_info=True,
            )
            return False
        if updated is not None:
            return True
    logger.warning(
        "[autonomy] delivery freeze kept losing the write race ticket=%s",
        ticket_id,
    )
    return False


async def measure_delivery(
    cwd: str,
    *,
    baseline: GitSnapshot | None = None,
    baseline_unavailable: bool = False,
    freeze: DeliveryFreeze | None = None,
    summary: str | None = None,
) -> FrozenDelivery:
    """Measure the delivery against the frozen HEAD.

    ``freeze`` is a freeze that was already taken when the report arrived:
    the delivery is measured against that commit, never against a later one,
    and the summary is the one captured with it. A freeze without a summary
    of its own measures the commit but says nothing about the workspace: the
    live workspace is not the one the freeze was taken from.

    When there is no freeze, the HEAD and the workspace state are read here,
    together, and frozen for every later retry.
    """
    if freeze is None:
        freeze, current = await asyncio.to_thread(capture_delivery_snapshot, cwd)
        return await asyncio.to_thread(
            frozen_delivery,
            cwd,
            baseline,
            freeze,
            baseline_unavailable=baseline_unavailable,
            current=current,
        )
    if summary is None:
        summary = freeze.summary
    return await asyncio.to_thread(
        frozen_delivery,
        cwd,
        baseline,
        freeze,
        baseline_unavailable=baseline_unavailable,
        summary=summary,
    )
