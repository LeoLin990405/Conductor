"""``task_report``: the N1 completion contract, reconciled with system facts.

The ``report`` object is validated by ``completion_report_from_text`` exactly
as a Worker's is (sanitized, never repaired; a bad one is ``invalid_schema``).
Then the server, not the app, checks the lease's worktree (PRD 5.3, 5.4):

* commit delivery since the lease's base and uncommitted changes, through the
  hardened read-only Git forms of ``git_delivery`` -> ``git_not_delivered``
  via ``reconcile_completion``; new commits that change no path are not a
  delivery;
* every changed path (rename detection off, so a rename counts both its
  source and its destination, and a deletion counts) is inside the Ticket's
  ``write_scope`` (the gate's
  ``scope_violations`` matcher; an empty scope allows no change) and HEAD is
  still the lease branch -> ``out_of_scope``;
* an upstream on the lease branch -> ``push_suspected``.

A refuting fact vetoes the claim to ``blocked``; the system check always wins
over the app's word. The reconciled state goes through ``record_lease_report``
(the only path that moves the Ticket for a report), together with the report
row, the ``reported`` ownership event and the audit row, in one transaction.
``done`` that survives the checks ends the lease; anything else keeps it.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from src.collab.types import InvalidSchemaError
from src.server.database.collab import leases as lease_db
from src.server.database.collab import reviews as review_db
from src.server.services.autonomy.completion import (
    ReconciledCompletion,
    completion_question,
    completion_report_from_text,
    completion_summary,
    git_delivered_from_checks,
    reconcile_completion,
)
from src.server.services.collab import tickets as task_svc
from src.server.services.collab.leases import held_lease, lease_refusal
from src.server.services.collab.tools import ToolCall, handler

OUT_OF_SCOPE = "out_of_scope"
PUSH_SUSPECTED = "push_suspected"
GIT_NOT_DELIVERED = "git_not_delivered"
_HEX = frozenset("0123456789abcdef")


@dataclass(frozen=True)
class GitFacts:
    """What the server saw in the lease worktree; ``None`` means unknown."""

    commit_delivery: str = "not_checked"
    dirty_summary: str | None = None
    changed_paths: tuple[str, ...] | None = None
    branch: str | None = None
    has_upstream: bool | None = None


def _is_full_sha(value: str) -> bool:
    return len(value) in (40, 64) and set(value) <= _HEX


def _status_paths(output: str) -> tuple[str, ...]:
    """Paths in ``git status --porcelain=v1 -z`` output, both sides of a rename."""
    records = output.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4:
            continue
        code, path = record[:2], record[3:]
        paths.append(path)
        if "R" in code or "C" in code:
            if index < len(records) and records[index]:
                paths.append(records[index])
            index += 1
    return tuple(dict.fromkeys(paths))


def committed_paths_command(base_sha: str, head: str) -> tuple[str, ...]:
    """The read-only Git form listing every path committed since ``base_sha``."""
    return (
        "diff", "--name-only", "-z", "--no-renames", "--no-ext-diff", "--no-textconv",
        f"{base_sha}...{head}", "--",
    )


def _git_facts_sync(worktree: str, base_sha: str) -> GitFacts:
    from src.server.services.autonomy import git_delivery as gd

    def run(*args: str) -> tuple[int, str]:
        result = gd.run_isolated_git_readonly(worktree, list(args), timeout=10)
        return result.returncode, result.stdout.decode("utf-8", "replace")

    try:
        code, head = run("rev-parse", "HEAD")
        head = head.strip()
        if code != 0 or not _is_full_sha(head) or not _is_full_sha(base_sha):
            return GitFacts(commit_delivery="unknown", dirty_summary="Git workspace status is unknown because it could not be checked safely.")
        code, count = run("rev-list", "--count", f"{base_sha}..{head}")
        delivery = "unknown"
        if code == 0 and count.strip().isdigit():
            delivery = "delivered" if int(count.strip()) > 0 else "missing"
        # ``--no-renames``: with rename (or copy) detection a committed
        # ``git mv outside/x inside/x`` lists only the new path, so the source
        # it removed would never reach the scope check. Without it every path a
        # commit touched is listed, deletions included. ``-z``: paths unquoted.
        code, names = run(*committed_paths_command(base_sha, head))
        changed = tuple(path for path in names.split("\0") if path) if code == 0 else None
        code, status = run(
            "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=all"
        )
        dirty = None
        if code != 0:
            dirty = "Git workspace status is unknown because it could not be checked safely."
        else:
            extra = _status_paths(status)
            if extra:
                dirty = "The git workspace has uncommitted changes."
                changed = tuple(dict.fromkeys((changed or ()) + extra)) if changed is not None else None
        code, branch = run("branch", "--show-current")
        upstream_code, _ = run("rev-parse", "--verify", "@{upstream}")
        return GitFacts(
            commit_delivery=delivery,
            dirty_summary=dirty,
            changed_paths=changed,
            branch=branch.strip() if code == 0 else None,
            has_upstream=upstream_code == 0,
        )
    except (gd.GitDeliveryError, OSError, UnicodeError):
        return GitFacts(
            commit_delivery="unknown",
            dirty_summary="Git workspace status is unknown because it could not be checked safely.",
        )


async def collect_git_facts(lease: Mapping[str, Any]) -> GitFacts:
    """The system facts for the lease's worktree (a seam tests replace)."""
    worktree = str(lease.get("worktree_path") or "")
    base_sha = str(lease.get("worktree_base_sha") or "")
    if not worktree or not base_sha:
        return GitFacts(commit_delivery="not_checked")
    return await asyncio.to_thread(_git_facts_sync, worktree, base_sha)


def scope_violations(paths: Sequence[str], write_scope: Sequence[str]) -> tuple[str, ...]:
    """Changed paths outside ``write_scope``; an empty scope permits nothing."""
    from src.tools.auto_dispatch.gates import path_matches

    patterns = [pattern for pattern in write_scope if pattern]
    return tuple(
        path for path in paths if not any(path_matches(pattern, path) for pattern in patterns)
    )


@dataclass(frozen=True)
class Reconciliation:
    ticket_state: str
    reason: str
    violations: tuple[str, ...] = ()


def reconcile(report: Any, facts: GitFacts, *, write_scope: Sequence[str], lease_branch: str) -> Reconciliation:
    """Facts win: delivery, then write scope and branch, then a suspected push."""
    commit_delivery = facts.commit_delivery
    if commit_delivery == "delivered" and facts.changed_paths == ():
        # New commits that change no path (``git commit --allow-empty``, or a
        # change and its revert) deliver nothing.
        commit_delivery = "missing"
    git_delivered = git_delivered_from_checks(facts.dirty_summary, commit_delivery)
    outcome = reconcile_completion(report, git_delivered=git_delivered, tests_passed=None)
    state = outcome.state
    reason = outcome.disagreement_reason or ""
    violations: tuple[str, ...] = ()
    if facts.changed_paths:
        violations = scope_violations(facts.changed_paths, write_scope)
    off_branch = bool(lease_branch) and facts.branch is not None and facts.branch != lease_branch
    if violations or off_branch:
        return Reconciliation("blocked", OUT_OF_SCOPE, violations)
    if facts.has_upstream:
        return Reconciliation("blocked", PUSH_SUSPECTED)
    if not reason and facts.changed_paths is None:
        # Paths that could not be read cannot be shown to fit the write scope.
        return Reconciliation("blocked", OUT_OF_SCOPE)
    if state not in lease_db.REPORT_TICKET_STATES:
        state = "blocked"
    return Reconciliation(state, reason)


async def _insert_report_row(
    conn: Any,
    *,
    ticket_id: str,
    state: str,
    summary: str,
    question: str | None,
    body: str,
    git_check: str | None,
) -> None:
    """The ``autonomy_ticket_reports`` row; no revision bump (the lease fence).

    ``git_check`` is constrained by migration 146 to ``unsupported_runtime``
    or NULL, so the veto reason lives in the body's ``reconciled`` object.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO autonomy_ticket_reports
                (ticket_id, state, summary, question, body, git_check)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (ticket_id, state, summary[:20000], question, body[: 64 * 1024], git_check),
        )


@handler("task_report")
async def task_report(call: ToolCall) -> dict[str, Any]:
    lease_id = str(call.arguments["lease_id"])
    call.set_target(lease_id)
    # A handoff_pending lease can no longer report (6.1).
    lease = await held_lease(call, lease_id, states=frozenset({"active"}))
    ticket = await task_svc.ticket_for_lease(lease, user_id=call.caller.user_id)
    call.require_task_mode(task_svc.task_facts(ticket))
    raw = call.arguments["report"]
    report = completion_report_from_text(json.dumps(raw, ensure_ascii=False))
    if report is None:
        raise InvalidSchemaError(
            "invalid_schema: the report does not match the completion contract"
        )
    facts = await collect_git_facts(lease)
    outcome = reconcile(
        report,
        facts,
        write_scope=task_svc.write_scope_of(ticket),
        lease_branch=str(lease.get("worktree_branch") or ""),
    )
    vetoed = bool(outcome.reason)
    async with call.transaction() as conn:
        row = await lease_db.record_lease_report(
            str(lease["lease_id"]),
            app_id=call.caller.app_id,
            ticket_status=outcome.ticket_state,
            conn=conn,
        )
        if row is None:
            raise lease_refusal(
                await lease_db.get_lease(str(lease["lease_id"]), conn=conn), call.caller.app_id
            )
        state = str(row.get("ticket_status") or outcome.ticket_state)
        verdict = ReconciledCompletion(
            state, vetoed, outcome.reason or None
        )
        summary = f"[{call.caller.actor}] {completion_summary(report, verdict)}"
        question = completion_question(report, verdict)
        if vetoed and outcome.reason in {OUT_OF_SCOPE, PUSH_SUSPECTED}:
            question = f"System check vetoed the app report: {outcome.reason}."
        body = json.dumps(
            {
                "source": "app",
                "actor": call.caller.actor,
                "report": report.to_payload(),
                "reconciled": {
                    "vetoed": vetoed,
                    "reason": outcome.reason,
                    "out_of_scope_paths": list(outcome.violations[:50]),
                },
            },
            ensure_ascii=False,
        )
        await _insert_report_row(
            conn,
            ticket_id=str(ticket["ticket_id"]),
            state=state,
            summary=summary,
            question=question,
            body=body,
            git_check=None,
        )
        ended = str(row.get("state") or "") == "completed"
        await task_svc.record_event(
            conn,
            ticket=ticket,
            actor=call.caller.actor,
            collab_event="reported",
            from_owner=f"app:{call.caller.app_id}",
            to_owner="" if ended else f"app:{call.caller.app_id}",
            reason=outcome.reason,
            extra={"status": report.status, "ticket_state": state, "lease_id": str(lease["lease_id"])},
        )
        if report.status == "needs_review":
            commit = next(
                (c for c in report.artifacts.commits if 7 <= len(c) <= 40 and set(c) <= _HEX),
                "",
            )
            if commit:
                await review_db.create_review(
                    requester_app_id=call.caller.app_id,
                    author_app_id=call.caller.app_id,
                    target_ref=str(lease["target_ref"]),
                    commit_sha=commit,
                    reviewer="native",
                    lease_id=str(lease["lease_id"]),
                    conn=conn,
                )
        data = {
            "ticket_state": state,
            "reconciled": {"vetoed": vetoed, "reason": outcome.reason},
        }
        return await call.finish(conn, data)


__all__ = [
    "GIT_NOT_DELIVERED",
    "GitFacts",
    "OUT_OF_SCOPE",
    "PUSH_SUSPECTED",
    "Reconciliation",
    "collect_git_facts",
    "reconcile",
    "scope_violations",
    "task_report",
]
