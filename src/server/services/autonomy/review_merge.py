"""Server-side review queue and merge execution for host-local line worktrees.

The review queue is the operator-facing record: the server refuses a line
reviewing itself and pins the reviewed commit when the review is queued, so a
verdict survives a restart and cannot be stamped onto a commit created after
the review started. It is a workflow record, not the merge gate: the gate is
a Codex review this module runs itself, which cannot be forged by a caller and
has no recorded-verdict-vs-landing race.

Merge is the irreversible half and is only reachable from a local caller: the
transport peer must be loopback, the request must arrive under a configured
local host name, forwarding headers are refused, and the ``super_agent``
feature must be enabled. A merge then:

1. resolves both worktrees under the configured root (a request names a line,
   never a path; symlinked worktrees are refused);
2. rehearses the merge in a throwaway git worktree, runs the tests against that
   merged tree, and requires that tree to be unchanged after the tests/Codex;
3. runs a mandatory Codex review of the rehearsed merge and requires
   ``VERDICT: MERGE`` from Codex's final answer (missing/failed/blocked is a
   structured error);
4. writes a durable ``merge_started`` audit row;
5. lands the target branch on the exact rehearsed merge commit with a
   compare-and-swap ``git update-ref``, so the landed commit is byte-identical
   to the tested and reviewed one and a concurrent branch advance is refused
   rather than overwritten. The target worktree is left for the operator to
   sync (the response says ``worktree_sync_required``).

Every refusal, failure and result is written to the durable audit table. The
module never runs a shell: ``git``, the test command and Codex are argv lists;
process output is bounded and credential-scrubbed before it is returned, and
raw output is never stored in the audit table.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from src.config.settings import get_review_merge_config
from src.server.database.autonomy import reviews as review_db
from src.server.utils.error_sanitization import sanitize_error_text

logger = logging.getLogger(__name__)

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

# A local caller may sit behind a same-host front end the operator explicitly
# grants; the transport peer and the absence of forwarding headers are still
# required, so this cannot open a remote path.
_FORWARDED_HEADER_NAMES = frozenset(
    {
        "forwarded",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
        "x-real-ip",
        "cf-connecting-ip",
        "true-client-ip",
        "x-client-ip",
    }
)
_GIT_TIMEOUT_SECONDS = 60.0
_MAX_LIST_LIMIT = 200
_MAX_CONFLICTS = 20
_SAFE_PROMPT_LEN = 200
_CODEX_FINAL_VERDICT_RE = re.compile(
    r"^[ \t]*VERDICT[ \t]*:[ \t]*(MERGE|BLOCK)[ \t]*$", re.IGNORECASE
)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
_SECRET_ENV_NAME_RE = re.compile(
    r"(?i)(token|secret|password|passwd|api[_-]?key|private[_-]?key|credential)"
)
_KNOWN_TOKEN_SHAPE_RE = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"glpat-[A-Za-z0-9_-]{16,})\b"
)


def _redact_known_secrets(text: str) -> str:
    """Remove inherited environment secrets and well-known token shapes.

    A failing test command inherits the server environment, so a test that
    echoes a credential (``PGPASSWORD``, ``GITHUB_TOKEN``, ...) must not have
    that value survive into the returned diagnostic tail. Environment values
    and token-shaped matches are found in the *original* text and redacted as
    one union of spans: doing one class first can change the other just enough
    that a fragment survives (a short password can split a token; a token-shaped
    password can lose its prefix before the exact-value match).
    """
    from src.server.utils.error_sanitization import (
        _BEARER_TOKEN_RE,
        _GOOGLE_KEY_RE,
        _KEY_PARAM_RE,
        _SK_TOKEN_RE,
        _URL_KEY_QUERY_RE,
        _URL_USERINFO_RE,
    )

    value = text or ""
    spans: list[tuple[int, int]] = []
    patterns = (
        _URL_USERINFO_RE,
        _BEARER_TOKEN_RE,
        _KEY_PARAM_RE,
        _URL_KEY_QUERY_RE,
        _SK_TOKEN_RE,
        _GOOGLE_KEY_RE,
        _KNOWN_TOKEN_SHAPE_RE,
    )
    for pattern in patterns:
        for match in pattern.finditer(value):
            spans.append((match.start(), match.end()))
    for name, secret in os.environ.items():
        if not secret or not _SECRET_ENV_NAME_RE.search(name):
            continue
        start = value.find(secret)
        while start != -1:
            spans.append((start, start + len(secret)))
            start = value.find(secret, start + 1)
    if not spans:
        return value
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    out: list[str] = []
    cursor = 0
    for start, end in merged:
        out.append(value[cursor:start])
        out.append("[REDACTED]")
        cursor = end
    out.append(value[cursor:])
    return "".join(out)

_CODEX_RETRY_REMEDIATION = (
    "Fix the Codex invocation (review_merge.codex_command / "
    "CONDUCTOR_CODEX_COMMAND) and retry; the target branch was not touched."
)


class ReviewMergeError(Exception):
    """A structured refusal or failure the API returns to the CLI.

    ``kind`` decides the audit action: a precondition the caller can fix is a
    refusal, a crash or a failed rehearsal/merge is a failure.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        remediation: str = "",
        status_code: int = 409,
        detail: dict[str, Any] | None = None,
        kind: Literal["refused", "failed"] = "refused",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.remediation = remediation
        self.status_code = status_code
        self.detail = detail or {}
        self.kind = kind

    def as_detail(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "remediation": self.remediation,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class MergePreconditions:
    """A checked merge request, pinned to the SHAs the rehearsal will use."""

    root: Path
    line: str
    line_path: Path
    line_branch: str
    line_head: str
    into: str
    target_path: Path
    target_head: str
    target_branch: str


@dataclass(frozen=True)
class MergeRehearsal:
    """The exact commit the tests and Codex approved, plus its inputs.

    ``merge_commit`` is the rehearsal worktree's HEAD: a real merge commit in
    the target repository's object database (linked worktrees share it). Landing
    moves the target branch onto this exact object, so the landed commit is
    byte-identical to the reviewed one.
    """

    line_head: str
    target_head: str
    merge_commit: str
    codex_verdict: str


# ---------------------------------------------------------------------------
# Root and line resolution (the only place a request becomes a filesystem path)
# ---------------------------------------------------------------------------


def configured_root() -> Path:
    return Path(get_review_merge_config().worktree_root).expanduser().resolve()


def resolve_request_root(claimed_root: str | None) -> Path:
    """Resolve the client's root against the server's configured root.

    A client never chooses where the server reads or writes: the CLI sends its
    own root only so a mismatch is reported here instead of surfacing later as
    a confusing "unknown line".
    """
    root = configured_root()
    claimed = (claimed_root or "").strip()
    if not claimed:
        return root
    # The claimed root is the caller's text, and resolving it follows symlinks
    # on this host: what it resolved to, or what the OS said about it, is not
    # repeated, so the answer cannot be used to read the filesystem. The
    # server's own root is, because that is what the client has to match.
    # ``~name`` of an unknown user is a RuntimeError and a NUL a ValueError.
    try:
        candidate = Path(claimed).expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ReviewMergeError(
            "worktree_root_invalid",
            "The requested worktree root cannot be resolved.",
            remediation="Pass a local absolute path, or omit root to use the server's configured root.",
            status_code=400,
        ) from exc
    if candidate != root:
        raise ReviewMergeError(
            "worktree_root_mismatch",
            f"The Conductor server resolves lines under {root}, not the root the request named.",
            remediation=(
                "Start the backend with CONDUCTOR_WT_ROOT pointing at the same "
                "root, or set review_merge.worktree_root in config.yaml, then retry."
            ),
            status_code=409,
            detail={"server_root": str(root)},
        )
    return root


def _valid_line_name(name: str) -> bool:
    """The CLI's line grammar: one directory name, never a path.

    Names may contain spaces or dots (a worktree directory can); what is
    rejected here is anything that could address a path rather than a direct
    child, which the ``path.parent == root`` check below then re-verifies.
    """
    return (
        bool(name)
        and len(name) <= 200
        and name not in {".", ".."}
        and "/" not in name
        and "\\" not in name
        and "\0" not in name
        and not Path(name).is_absolute()
    )


def resolve_line(root: Path, name: str, *, field: str = "line") -> Path:
    """Resolve a line name to a git worktree directly under ``root``.

    Rejects separators, absolute paths and symlinks that escape the root: the
    merge path runs tests and ``git merge`` at the resolved directory.

    ``field`` is the request field the name came from (``line`` or
    ``reviewer``), which a refusal says in place of the name: until it has
    resolved, the name is the caller's text and is not read back.
    """
    if not isinstance(name, str) or not _valid_line_name(name):
        raise ReviewMergeError(
            "line_not_found",
            f"Unknown {field}.",
            remediation="Pass the worktree directory name directly under the configured worktree root.",
            status_code=404,
        )
    try:
        path = (root / name).resolve()
    except OSError as exc:
        raise ReviewMergeError(
            "line_not_found",
            f"The {field} cannot be resolved under the worktree root.",
            status_code=404,
        ) from exc
    try:
        inside_root = path.parent == root
    except OSError:
        inside_root = False
    if not inside_root or not path.is_dir() or not (path / ".git").exists():
        raise ReviewMergeError(
            "line_not_found",
            f"Unknown {field}: no git worktree of that name directly under {root}.",
            remediation="Create the line worktree under the configured root, or set CONDUCTOR_WT_ROOT on both the CLI and the server.",
            status_code=404,
        )
    return path


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(path), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except OSError as exc:
        raise ReviewMergeError(
            "git_unavailable",
            "Git is not available on the Conductor host.",
            remediation="Install git and make sure the server can run it, then retry.",
            status_code=500,
            kind="failed",
            detail={"reason": str(exc)[:200]},
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ReviewMergeError(
            "git_timeout",
            f"Git did not finish within {_GIT_TIMEOUT_SECONDS:.0f}s on {path.name}.",
            remediation="Check for a hung git process or index lock, then retry.",
            status_code=500,
            kind="failed",
        ) from exc


def _git_text(path: Path, *args: str) -> str:
    result = _git(path, *args)
    if result.returncode:
        output = _tail((result.stdout or "") + (result.stderr or ""))
        raise ReviewMergeError(
            "git_failed",
            f"git {' '.join(args)} failed in {path}.",
            remediation="Inspect the worktree with git status, fix the repository state, then retry.",
            status_code=409,
            detail={"output": output},
            kind="failed",
        )
    return result.stdout.strip()


def _head(path: Path) -> str:
    return _git_text(path, "rev-parse", "HEAD")


def _branch(path: Path) -> str:
    """Current branch, or ``""`` for a detached HEAD."""
    result = _git(path, "branch", "--show-current")
    if result.returncode:
        raise ReviewMergeError(
            "git_failed",
            f"Could not read the branch of {path.name}.",
            remediation="Inspect the worktree with git status, then retry.",
            status_code=409,
            detail={"output": _tail((result.stdout or "") + (result.stderr or ""))},
            kind="failed",
        )
    return result.stdout.strip()


def _is_dirty(path: Path) -> bool:
    result = _git(path, "status", "--porcelain")
    if result.returncode:
        raise ReviewMergeError(
            "git_failed",
            f"Could not read the status of {path.name}.",
            remediation="Inspect the worktree with git status, then retry.",
            status_code=409,
            detail={"output": _tail((result.stdout or "") + (result.stderr or ""))},
            kind="failed",
        )
    return bool(result.stdout.strip())


def _has_hidden_index_flags(path: Path) -> bool:
    """True when a tracked path is assume-unchanged or skip-worktree.

    ``git status --porcelain`` deliberately hides both flags; a test command
    could therefore modify such a file while the landing fence still sees the
    original commit. Refuse the rehearsal instead of trusting a hidden index.
    """
    result = _git(path, "ls-files", "-v")
    if result.returncode:
        raise ReviewMergeError(
            "git_failed",
            f"Could not read the index state of {path.name}.",
            remediation="Inspect the index with git ls-files -v, then retry.",
            status_code=409,
            detail={"output": _tail((result.stdout or "") + (result.stderr or ""))},
            kind="failed",
        )
    for line in result.stdout.splitlines():
        if line and (line[0].islower() or line[0] == "S"):
            return True
    return False


def _valid_branch_ref(name: str) -> bool:
    if not name or name.startswith("-") or len(name) > 200:
        return False
    result = _git(Path.cwd(), "check-ref-format", "--branch", name)
    return result.returncode == 0


def resolve_target_worktree(root: Path, into: str) -> Path:
    """The one worktree under ``root`` currently checked out on ``into``.

    A symlinked worktree that resolves outside the root is refused: the merge
    runs ``git merge`` at the resolved directory, and only the configured root
    is inside the server's path boundary.
    """
    if not _valid_branch_ref(into):
        raise ReviewMergeError(
            "invalid_target_branch",
            f"{into!r} is not a valid git branch name.",
            remediation="Pass the branch to merge into, for example --into integrate/r2.",
            status_code=400,
        )
    try:
        children = sorted(
            (
                child
                for child in root.iterdir()
                if child.is_dir() and (child / ".git").exists()
            ),
            key=lambda child: child.name,
        )
    except OSError as exc:
        raise ReviewMergeError(
            "root_unreadable",
            f"The worktree root {root} cannot be read.",
            remediation="Check the server's CONDUCTOR_WT_ROOT and its filesystem permissions.",
            status_code=500,
            kind="failed",
            detail={"reason": str(exc)[:200]},
        ) from exc
    matches: list[Path] = []
    for child in children:
        if _branch(child) != into:
            continue
        resolved = child.resolve()
        if resolved.parent != root:
            raise ReviewMergeError(
                "target_worktree_outside_root",
                f"The worktree for {into!r} resolves outside the configured worktree root.",
                remediation=(
                    "Use a real worktree directly under the root; a symlinked "
                    "worktree is refused because the merge would run outside the root."
                ),
                status_code=409,
            )
        matches.append(resolved)
    if not matches:
        raise ReviewMergeError(
            "target_worktree_missing",
            f"No worktree under {root} is checked out on {into!r}.",
            remediation=(
                f"Check out {into!r} in a worktree under {root} "
                "(for example: git worktree add <path> " + into + ") and retry."
            ),
            status_code=409,
        )
    if len(matches) > 1:
        names = ", ".join(match.name for match in matches)
        raise ReviewMergeError(
            "target_worktree_ambiguous",
            f"Several worktrees under {root} are checked out on {into!r}: {names}.",
            remediation="Leave exactly one worktree on the target branch, then retry.",
            status_code=409,
        )
    return matches[0]


def same_worktree(left: Path, right: Path) -> bool:
    """True when two line paths are the same directory on disk.

    Case-insensitive filesystems (APFS default) make ``LineA`` and ``linea``
    the same folder while ``resolve()`` strings still differ, so the
    self-review check must compare file identity, not path text.
    """
    try:
        return left.samefile(right)
    except OSError:
        return left.resolve() == right.resolve()


# ---------------------------------------------------------------------------
# Local-only merge privilege
# ---------------------------------------------------------------------------


def local_merge_hosts() -> frozenset[str]:
    return frozenset(
        str(host).strip().lower().rstrip(".")
        for host in get_review_merge_config().local_hosts
        if str(host).strip()
    )


def is_local_merge_request(
    *, peer: str | None, hostname: str | None, headers: Any
) -> bool:
    """Whether a merge request is a local, unproxied one.

    Three independent conditions, all required: the TCP peer is loopback, no
    forwarding header is present (it would mean a proxy is in between), and
    the Host name is one the operator listed as local (``local_hosts``; that
    list is the explicit local permission for a same-host front end). The Host
    check also blocks a same-host proxy that presents a public name.
    """
    if not peer or peer not in LOOPBACK_HOSTS:
        return False
    for name in _FORWARDED_HEADER_NAMES:
        if name in headers:
            return False
    host = (hostname or "").strip().lower().rstrip(".")
    return bool(host) and host in local_merge_hosts()


# ---------------------------------------------------------------------------
# Reviews
# ---------------------------------------------------------------------------


def review_view(row: dict[str, Any]) -> dict[str, Any]:
    """The wire shape: ``id`` keeps the envelope the pre-server CLI printed."""
    return {
        "id": row["review_id"],
        "line": row["line"],
        "reviewer": row["reviewer"],
        "provider": row["reviewer_provider"],
        "state": row["state"],
        "verdict": row["verdict"],
        "head_sha": row["head_sha"],
        "note": row["note"],
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
    }


def _clamp_limit(limit: int) -> int:
    return max(1, min(int(limit), _MAX_LIST_LIMIT))


def _plan_review(
    *, line: str, reviewer: str, provider: str, claimed_root: str | None
) -> dict[str, Any]:
    """Synchronous path/permission inspection; runs off the event loop.

    ``git`` subprocesses here can take up to the git timeout, so the route's
    async layer hands the whole inspection to a worker thread and only the DB
    write stays on the loop.
    """
    root = resolve_request_root(claimed_root)
    line_path = resolve_line(root, line)
    reviewer_path = resolve_line(root, reviewer, field="reviewer")
    if line == reviewer or same_worktree(line_path, reviewer_path):
        raise ReviewMergeError(
            "self_review_refused",
            f"Line {line!r} cannot review itself.",
            remediation="Name a different reviewer line; a line may never review its own work.",
            status_code=409,
        )
    normalized = (provider or "codex").strip().lower()
    if not re.fullmatch(r"[a-z0-9_.-]{1,64}", normalized):
        raise ReviewMergeError(
            "invalid_provider",
            "reviewer provider must be a short lowercase identifier.",
            remediation="Use --provider codex (the cross-review queue is the Codex queue).",
            status_code=400,
        )
    try:
        head_sha = _head(line_path)
    except ReviewMergeError as exc:
        raise ReviewMergeError(
            "review_line_unreadable",
            f"Cannot read line {line!r} to pin the review to a commit.",
            remediation="Check the line worktree with git status, then retry.",
            status_code=409,
            detail={"cause": exc.code},
        ) from exc
    return {
        "line": line,
        "reviewer": reviewer,
        "head_sha": head_sha,
        "reviewer_provider": normalized,
    }


async def queue_review(
    *,
    user_id: str,
    line: str,
    reviewer: str,
    provider: str,
    claimed_root: str | None,
) -> dict[str, Any]:
    planned = await asyncio.to_thread(
        _plan_review,
        line=line,
        reviewer=reviewer,
        provider=provider,
        claimed_root=claimed_root,
    )
    row = await review_db.create_review(user_id=user_id, **planned)
    return review_view(row)


async def list_reviews(
    user_id: str, *, line: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    rows = await review_db.list_reviews(user_id, line=line, limit=_clamp_limit(limit))
    return [review_view(row) for row in rows]


async def get_review(user_id: str, review_id: str) -> dict[str, Any]:
    row = await review_db.get_review(user_id, review_id)
    if row is None:
        raise ReviewMergeError(
            "review_not_found",
            "Review not found.",
            remediation="List the queue with: conductor review status",
            status_code=404,
        )
    return review_view(row)


def _assert_review_current(
    *, review_id: str, line: str, pinned: str, claimed_root: str | None
) -> None:
    """Verify the queued review still covers the line head (sync, thread-safe)."""
    root = resolve_request_root(claimed_root)
    line_path = resolve_line(root, line)
    try:
        current_head = _head(line_path)
    except ReviewMergeError as exc:
        raise ReviewMergeError(
            "review_line_unreadable",
            f"Cannot read the reviewed line {line!r} to verify its commit.",
            remediation="Restore the line worktree, queue a fresh review, then record its verdict.",
            status_code=409,
            detail={"cause": exc.code},
        ) from exc
    if not pinned or pinned != current_head:
        raise ReviewMergeError(
            "review_stale",
            (
                f"Review {review_id} was queued for {pinned[:12] or 'no commit'}, "
                f"but the line head is now {current_head[:12]}."
            ),
            remediation=(
                "The line moved after the review was queued. Queue a fresh review "
                "for the current head with: conductor review run <line> --by <reviewer>"
            ),
            status_code=409,
            detail={"reviewed_sha": pinned, "head_sha": current_head},
        )


async def complete_review(
    *,
    user_id: str,
    review_id: str,
    verdict: str,
    note: str,
    claimed_root: str | None,
) -> dict[str, Any]:
    if verdict not in {"merge", "block"}:
        raise ReviewMergeError(
            "invalid_verdict",
            "verdict must be 'merge' or 'block'.",
            remediation="Pass --verdict MERGE or --verdict BLOCK.",
            status_code=400,
        )
    row = await review_db.get_review(user_id, review_id)
    if row is None:
        raise ReviewMergeError(
            "review_not_found",
            "Review not found.",
            remediation="List the queue with: conductor review status",
            status_code=404,
        )
    if row["state"] != "queued":
        raise ReviewMergeError(
            "review_not_open",
            f"Review {review_id} is already {row['state']}.",
            remediation="Queue a new review for this line instead of reusing a finished verdict.",
            status_code=409,
        )
    await asyncio.to_thread(
        _assert_review_current,
        review_id=review_id,
        line=row["line"],
        pinned=str(row.get("head_sha") or ""),
        claimed_root=claimed_root,
    )
    updated = await review_db.complete_review(
        user_id, review_id, verdict=verdict, note=note
    )
    if updated is None:
        raise ReviewMergeError(
            "review_not_open",
            f"Review {review_id} was completed by another request.",
            remediation="Queue a new review for this line instead of reusing a finished verdict.",
            status_code=409,
        )
    return review_view(updated)


async def list_audit(
    user_id: str,
    *,
    line: str | None = None,
    action: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    if action is not None and action not in review_db.AUDIT_ACTIONS:
        raise ReviewMergeError(
            "invalid_audit_action",
            "Unknown audit action.",
            remediation=(
                "Filter by review_requested, review_completed, merge_refused, "
                "merge_started, merge_merged or merge_failed."
            ),
            status_code=400,
        )
    return await review_db.list_audit(
        user_id, line=line, action=action, limit=_clamp_limit(limit)
    )


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


def _short(sha: str) -> str:
    return sha[:12]


def _tail(text: str, limit: int | None = None) -> str:
    """Credential-scrubbed tail of untrusted process text.

    ``output_tail_chars == 0`` means no truncation (the operator's explicit
    choice); otherwise the tail is the last 30 non-empty lines, cut to the
    configured character budget. Scrubbing runs while the text is whole and
    again after the cut, so a credential split at the boundary is redacted
    first and cannot leak as a fragment.
    """
    config_limit = get_review_merge_config().output_tail_chars
    bound = config_limit if limit is None else min(limit, config_limit)
    # Redact inherited env values before control-character sanitization: a
    # URL-shaped password can otherwise be partly rewritten and no longer
    # match the original secret value.
    scrubbed = _redact_known_secrets(text or "")
    scrubbed = sanitize_error_text(scrubbed)
    if not bound:
        return _redact_known_secrets(sanitize_error_text(scrubbed.strip()))
    tail = "\n".join([line for line in scrubbed.strip().splitlines() if line][-30:])
    tail = _redact_known_secrets(sanitize_error_text(tail))
    if len(tail) > bound:
        tail = tail[-bound:]
    return _redact_known_secrets(sanitize_error_text(tail))


def _prompt_safe(value: str) -> str:
    """One printable line, bounded, for text interpolated into a prompt."""
    return _CONTROL_CHAR_RE.sub("", str(value))[:_SAFE_PROMPT_LEN].strip()


def _prepare_merge(
    *,
    line: str,
    into: str,
    claimed_root: str | None,
) -> MergePreconditions:
    """Resolve both sides and refuse any unsafe starting state."""
    root = resolve_request_root(claimed_root)
    line_path = resolve_line(root, line)
    line_branch = _branch(line_path)
    if not line_branch:
        raise ReviewMergeError(
            "line_detached",
            f"Line {line!r} is on a detached HEAD; nothing would name the merged branch.",
            remediation="Check out a branch in the line worktree, commit the work, then retry.",
            status_code=409,
        )
    if line_branch == into:
        raise ReviewMergeError(
            "same_branch",
            f"Line {line!r} is already on {into!r}.",
            remediation="Name the line branch to merge into the target branch.",
            status_code=409,
        )
    target_path = resolve_target_worktree(root, into)
    if same_worktree(target_path, line_path):
        raise ReviewMergeError(
            "same_worktree",
            f"Line {line!r} is the worktree checked out on {into!r}.",
            remediation="Pick a line worktree that is not the target branch's worktree.",
            status_code=409,
        )
    if _is_dirty(target_path):
        raise ReviewMergeError(
            "target_dirty",
            f"Target worktree {target_path.name} has uncommitted changes.",
            remediation="Commit or stash them in the target worktree, then retry.",
            status_code=409,
        )
    if _is_dirty(line_path):
        raise ReviewMergeError(
            "line_dirty",
            f"Line worktree {line} has uncommitted changes; the branch tip is what would merge.",
            remediation="Commit or stash them in the line worktree, then retry.",
            status_code=409,
        )
    target_head = _head(target_path)
    target_branch = _branch(target_path)
    if target_branch != into:
        # A checkout can switch the resolved worktree between finding it and
        # reading its branch; never record a branch other than the requested
        # target, or the lock-time branch check would accept the wrong one.
        raise ReviewMergeError(
            "target_worktree_moved",
            (
                f"The worktree for {into!r} switched to {target_branch!r} while it "
                "was being resolved."
            ),
            remediation="Retry once the target worktree is stably checked out on the target branch.",
            status_code=409,
        )
    return MergePreconditions(
        root=root,
        line=line,
        line_path=line_path,
        line_branch=line_branch,
        line_head=_head(line_path),
        into=into,
        target_path=target_path,
        target_head=target_head,
        target_branch=target_branch,
    )


def _conflicted_paths(path: Path) -> list[str]:
    result = _git(path, "diff", "--name-only", "--diff-filter=U")
    if result.returncode:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()][
        :_MAX_CONFLICTS
    ]


def _remove_temp_worktree(target_path: Path, tmpdir: Path) -> None:
    # ``-f -f`` also removes a rehearsal merge that stopped mid-conflict; the
    # rmtree is the fallback for a worktree that was never registered.
    subprocess.run(
        [
            "git",
            "-C",
            str(target_path),
            "worktree",
            "remove",
            "--force",
            "--force",
            str(tmpdir),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    shutil.rmtree(tmpdir, ignore_errors=True)


# Installed as sitecustomize.py ahead of the test interpreter's own startup:
# it strips the *line* checkout's paths (including editable-install paths added
# by .pth files) from sys.path and sys.meta_path, so a package that the merged
# tree deleted cannot silently resolve from the unmerged checkout.
_IMPORT_GUARD = """\
import os
import sys

_prefixes = [
    os.path.abspath(value)
    for value in (
        os.environ.get("CONDUCTOR_REHEARSAL_LEAK_PREFIX", "").strip(),
        os.environ.get("CONDUCTOR_REHEARSAL_SERVER_PREFIX", "").strip(),
    )
    if value
]


def _leaked(entry):
    resolved = os.path.abspath(entry or os.getcwd())
    for root in _prefixes:
        if resolved == root or resolved.startswith(root + os.sep):
            relative = os.path.relpath(resolved, root)
            # Keep virtualenvs: their site-packages hold the test runner and
            # dependencies. Only checkout source paths leak, including the
            # server's editable install when the line has no .venv.
            if not any(
                part == ".venv" or part.startswith(".venv-")
                for part in relative.split(os.sep)
            ):
                return True
    return False


sys.path[:] = [entry for entry in sys.path if not _leaked(entry)]
sys.meta_path[:] = [
    finder
    for finder in sys.meta_path
    if "__editable__" not in getattr(finder, "__module__", "")
    and "__editable__" not in type(finder).__module__
]
"""


def _write_import_guard() -> Path:
    directory = Path(tempfile.mkdtemp(prefix="conductor-import-guard-"))
    (directory / "sitecustomize.py").write_text(_IMPORT_GUARD, encoding="utf-8")
    return directory


def _run_process(
    argv: list[str], *, cwd: Path, env: dict[str, str], timeout: float
) -> tuple[int, str, str, bool]:
    """Run one argv without a shell, killing the whole process group on timeout."""
    try:
        process = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError:
        raise
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return process.returncode, stdout or "", stderr or "", False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        return -1, "", "", True


def _run_merge(
    argv: list[str], *, cwd: Path, line_path: Path | None = None
) -> tuple[bool, str, bool]:
    """Run the test command in the rehearsal worktree, without a shell."""
    env = os.environ.copy()
    env["CONDUCTOR_WT_ROOT"] = str(configured_root())
    guard: Path | None = None
    rehearsal_paths: list[str] = []
    if line_path is not None:
        guard = _write_import_guard()
        env["CONDUCTOR_REHEARSAL_LEAK_PREFIX"] = str(line_path)
        env["CONDUCTOR_REHEARSAL_SERVER_PREFIX"] = str(
            Path(__file__).resolve().parents[4]
        )
        rehearsal_paths.append(str(guard))
    # The line's virtualenv may carry an editable install of this project that
    # points at the *line* checkout. Put the rehearsal tree (and its src/) ahead
    # of that on sys.path so tests import the merged sources they are judging,
    # not the line's working copy; the import guard strips the line checkout
    # entirely so a deleted package cannot fall back to it.
    rehearsal_paths.extend([str(cwd), str(Path(cwd) / "src")])
    ptc_cli = Path(cwd) / "libs" / "ptc-cli"
    if ptc_cli.is_dir():
        rehearsal_paths.append(str(ptc_cli))
    existing_pythonpath = env.get("PYTHONPATH", "")
    if existing_pythonpath:
        rehearsal_paths.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(rehearsal_paths)
    config = get_review_merge_config()
    try:
        returncode, stdout, stderr, timed_out = _run_process(
            argv, cwd=cwd, env=env, timeout=config.test_timeout_seconds
        )
    except OSError as exc:
        raise ReviewMergeError(
            "test_command_failed",
            "The test command could not be started in the rehearsal worktree.",
            remediation="Check CONDUCTOR_MERGE_TEST_CMD / --test-cmd, then retry.",
            status_code=409,
            kind="failed",
            detail={"reason": _tail(str(exc))},
        ) from exc
    finally:
        if guard is not None:
            shutil.rmtree(guard, ignore_errors=True)
    if timed_out:
        return False, "", True
    return returncode == 0, stdout + stderr, False


def _test_command(line_path: Path, configured: str | None) -> list[str]:
    config = get_review_merge_config()
    text = (configured or "").strip() or config.test_command.strip()
    if text:
        try:
            argv = shlex.split(text)
        except ValueError as exc:
            raise ReviewMergeError(
                "test_command_invalid",
                "The test command is not a valid argv string.",
                remediation="Quote paths and pass a single command, for example: --test-cmd '.venv/bin/python -m pytest -q'",
                status_code=400,
                detail={"reason": sanitize_error_text(str(exc))[:200]},
            ) from exc
        if not argv:
            raise ReviewMergeError(
                "test_command_invalid",
                "The test command is empty.",
                remediation="Omit --test-cmd to use the default, or pass a real command.",
                status_code=400,
            )
        return argv
    # The line's interpreter sees the merged files because the tests run with
    # the rehearsal worktree as cwd; if it has no venv the server's does.
    python = line_path / ".venv" / "bin" / "python"
    interpreter = str(python) if python.is_file() else sys.executable
    return [interpreter, "-m", "pytest", "-q"]


def _resolve_test_argv(
    argv: list[str], *, line_path: Path, worktree: Path
) -> list[str]:
    """Keep a relative interpreter path working from the rehearsal worktree.

    Before the merge moved server-side, ``--test-cmd '.venv/bin/python -m
    pytest -q'`` named a path inside the line worktree. The rehearsal worktree
    never contains ``.venv`` (gitignored, not copied), so a relative first
    argument that exists only in the line worktree is anchored there; one that
    exists in the merged tree (a repo script) stays relative to the rehearsal.
    """
    if not argv:
        return argv
    first = argv[0]
    if Path(first).is_absolute() or ("/" not in first and "\\" not in first):
        return argv
    if (worktree / first).exists():
        return argv
    candidate = line_path / first
    if candidate.is_file():
        return [str(candidate), *argv[1:]]
    return argv


# ---------------------------------------------------------------------------
# Merge-time Codex review
# ---------------------------------------------------------------------------


def _codex_argv() -> list[str]:
    config = get_review_merge_config()
    try:
        argv = shlex.split(config.codex_command)
    except ValueError as exc:
        raise ReviewMergeError(
            "codex_command_invalid",
            "The configured Codex command is not a valid argv string.",
            remediation="Fix review_merge.codex_command / CONDUCTOR_CODEX_COMMAND, then retry.",
            status_code=500,
            kind="failed",
        ) from exc
    if not argv:
        raise ReviewMergeError(
            "codex_unavailable",
            "The merge-time Codex review is not configured; a merge requires it.",
            remediation="Install the Codex CLI or set review_merge.codex_command, then retry.",
            status_code=500,
            kind="failed",
        )
    return argv


def _codex_prompt(*, line: str, into: str, target_head: str) -> str:
    return (
        f"MANDATORY PRE-MERGE REVIEW for line {_prompt_safe(line)} merging into "
        f"{_prompt_safe(into)}.\n\n"
        "The current worktree is the exact merge result that will land: a merge "
        f"of the line onto target commit {target_head}. Review the incoming change "
        f"with `git diff {target_head} HEAD`, inspect the changed files and their "
        "tests, and do not modify anything.\n\n"
        "Report concrete findings with file paths (or state that there are none). "
        "End your final message with exactly one line, either:\n"
        "VERDICT: MERGE\n"
        "or\n"
        "VERDICT: BLOCK"
    )


def _parse_codex_verdict(final_message: str) -> str | None:
    """The verdict on the **last non-empty line** of Codex's final message.

    Only the dedicated final-answer output is inspected: stdout/stderr are
    diagnostics and may quote the prompt, earlier drafts or incidental text, so
    a stray ``VERDICT: MERGE`` there must never gate a merge. The very last
    line has to be the verdict, which is exactly what the prompt asks for.
    """
    lines = [line for line in (final_message or "").splitlines() if line.strip()]
    if not lines:
        return None
    match = _CODEX_FINAL_VERDICT_RE.fullmatch(lines[-1].strip())
    if match is None:
        return None
    return match.group(1).lower()


def _read_final_message(path: Path, limit: int = 65536) -> str:
    """Read the tail of Codex's final-answer file.

    Only the end matters (the verdict is the final line) and the file is
    untrusted-sized, so read the last ``limit`` bytes instead of truncating from
    the start: a BLOCK at the end must never be cut off in favour of an earlier
    draft verdict in the retained prefix.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            if size > limit:
                stream.seek(size - limit)
                data = stream.read()
                # The retained tail starts mid-line; that partial line is not
                # a complete answer line and must not be parsed as one.
                newline = data.find(b"\n")
                data = data[newline + 1 :] if newline != -1 else b""
            else:
                data = stream.read()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def _run_codex_merge_review(
    *,
    worktree: Path,
    line: str,
    into: str,
    target_head: str,
) -> str:
    """Run the mandatory Codex review of the rehearsed merge.

    Returns ``"merge"``; every failure (missing binary, timeout, non-zero exit,
    missing verdict, BLOCK) raises a structured error and leaves the target
    branch untouched. The final-answer file is always removed. Tests replace
    this function to stay off the network.
    """
    config = get_review_merge_config()
    base_argv = _codex_argv()
    descriptor, final_path_text = tempfile.mkstemp(
        prefix="conductor-codex-final-", suffix=".txt"
    )
    os.close(descriptor)
    final_path = Path(final_path_text)
    argv = [
        *base_argv,
        "exec",
        "--sandbox",
        "read-only",
        "--color",
        "never",
        "-C",
        str(worktree),
        "--output-last-message",
        str(final_path),
        _codex_prompt(line=line, into=into, target_head=target_head),
    ]
    try:
        try:
            returncode, stdout, stderr, timed_out = _run_process(
                argv,
                cwd=worktree,
                env=os.environ.copy(),
                timeout=config.codex_timeout_seconds,
            )
        except OSError as exc:
            raise ReviewMergeError(
                "codex_unavailable",
                "The Codex review command could not be started; the target branch was not touched.",
                remediation=(
                    "Install the Codex CLI (or point review_merge.codex_command at it), "
                    "check its login with `codex login`, then retry."
                ),
                status_code=409,
                kind="failed",
                detail={"reason": sanitize_error_text(str(exc))[:300]},
            ) from exc
        if timed_out:
            raise ReviewMergeError(
                "codex_timeout",
                (
                    "The Codex pre-merge review did not finish within "
                    f"{config.codex_timeout_seconds:.0f}s; the target branch was not touched."
                ),
                remediation=_CODEX_RETRY_REMEDIATION,
                status_code=409,
                kind="failed",
                detail={"timeout_seconds": config.codex_timeout_seconds},
            )
        final_message = _read_final_message(final_path)
        if returncode != 0:
            raise ReviewMergeError(
                "codex_failed",
                (
                    "The Codex pre-merge review exited with status "
                    f"{returncode}; the target branch was not touched."
                ),
                remediation=_CODEX_RETRY_REMEDIATION,
                status_code=409,
                kind="failed",
                detail={"exit_code": returncode},
            )
        verdict = _parse_codex_verdict(final_message)
        if verdict is None:
            raise ReviewMergeError(
                "codex_verdict_missing",
                (
                    "The Codex pre-merge review did not end its final message with a "
                    "verdict; the target branch was not touched."
                ),
                remediation=(
                    "Check that Codex can write its final message "
                    "(--output-last-message) and follows the prompt's VERDICT line, "
                    "then retry."
                ),
                status_code=409,
                kind="failed",
                detail={"review_output_omitted": True},
            )
        if verdict == "block":
            raise ReviewMergeError(
                "codex_blocked",
                "The Codex pre-merge review returned BLOCK; the target branch was not touched.",
                remediation=(
                    "Address the review findings on the line branch, commit, re-run "
                    "the Codex review, then retry."
                ),
                status_code=409,
                kind="refused",
                detail={"verdict": "block", "review_output_omitted": True},
            )
        return verdict
    finally:
        final_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Rehearsal and landing
# ---------------------------------------------------------------------------


def _verify_unchanged(state: MergePreconditions) -> None:
    """Refuse when either side moved since the rehearsal pinned its SHAs."""
    if _is_dirty(state.line_path) or _branch(state.line_path) != state.line_branch:
        raise ReviewMergeError(
            "line_changed",
            f"Line {state.line!r} changed while the merge was being prepared.",
            remediation="Commit or discard the change, re-run the review and merge.",
            status_code=409,
            kind="failed",
        )
    current_line = _head(state.line_path)
    if current_line != state.line_head:
        raise ReviewMergeError(
            "line_advanced",
            f"Line {state.line!r} advanced while the merge was being prepared.",
            remediation="Re-run the Codex review and the merge for the new head.",
            status_code=409,
            kind="failed",
            detail={"tested_sha": state.line_head, "head_sha": current_line},
        )
    if _is_dirty(state.target_path) or _branch(state.target_path) != state.target_branch:
        raise ReviewMergeError(
            "target_changed",
            f"Target {state.into!r} changed while the merge was being prepared.",
            remediation="Commit or discard the target worktree change, then retry.",
            status_code=409,
            kind="failed",
        )
    if _head(state.target_path) != state.target_head:
        raise ReviewMergeError(
            "target_advanced",
            f"Target {state.into!r} advanced while the merge was being prepared.",
            remediation="Re-merge the line onto the new target head, then retry.",
            status_code=409,
            kind="failed",
            detail={"tested_target_sha": state.target_head},
        )


def _rehearse(state: MergePreconditions, test_cmd: str | None) -> MergeRehearsal:
    """Test the merge and Codex-review it in a throwaway worktree.

    Runs in a worker thread: the tests and the Codex review can each take as
    long as their configured timeouts. The target branch is never touched here.
    """
    config = get_review_merge_config()
    raw_argv = _test_command(state.line_path, test_cmd)
    merge_message = f"Merge line {state.line} into {state.into}"
    tmpdir = Path(tempfile.mkdtemp(prefix="conductor-merge-"))
    worktree_added = False
    codex_verdict = ""
    try:
        add = _git(
            state.target_path,
            "worktree",
            "add",
            "--detach",
            "--force",
            str(tmpdir),
            state.target_head,
        )
        if add.returncode:
            raise ReviewMergeError(
                "test_worktree_failed",
                "Could not create the temporary rehearsal worktree.",
                remediation="Check disk space and that the target repository is writable, then retry.",
                status_code=409,
                kind="failed",
                detail={"output": _tail((add.stdout or "") + (add.stderr or ""))},
            )
        worktree_added = True
        rehearsal = _git(
            tmpdir, "merge", "--no-ff", "-m", merge_message, "--", state.line_head
        )
        if rehearsal.returncode:
            conflicts = _conflicted_paths(tmpdir)
            if conflicts:
                raise ReviewMergeError(
                    "merge_conflict",
                    f"Merging {state.line!r} into {state.into!r} conflicts; the target branch was not touched.",
                    remediation=(
                        "Resolve the conflicts on the line branch against the target, commit, "
                        "retry; the merge-time Codex review runs again automatically."
                    ),
                    status_code=409,
                    kind="failed",
                    detail={"conflicts": conflicts, "target_branch_untouched": True},
                )
            raise ReviewMergeError(
                "merge_failed",
                f"The rehearsal merge of {state.line!r} into {state.into!r} failed; the target branch was not touched.",
                remediation=(
                    "Check that both worktrees share one git repository and that the line "
                    "branch is mergeable, then retry."
                ),
                status_code=409,
                kind="failed",
                detail={"output": _tail((rehearsal.stdout or "") + (rehearsal.stderr or ""))},
            )
        rehearsal_head = _head(tmpdir)
        argv = _resolve_test_argv(raw_argv, line_path=state.line_path, worktree=tmpdir)
        green, output, timed_out = _run_merge(
            argv, cwd=tmpdir, line_path=state.line_path
        )
        if timed_out:
            raise ReviewMergeError(
                "tests_timeout",
                f"The test run did not finish within {config.test_timeout_seconds:.0f}s; the target branch was not touched.",
                remediation="Speed up the focused test command via --test-cmd, or raise review_merge.test_timeout_seconds.",
                status_code=409,
                kind="failed",
                detail={"timeout_seconds": config.test_timeout_seconds},
            )
        if not green:
            raise ReviewMergeError(
                "tests_failed",
                "The merge rehearsal is red; the target branch was not touched.",
                remediation=(
                    "Fix the failing tests on the line branch, commit, then retry; the "
                    "merge-time Codex review runs again automatically."
                ),
                status_code=409,
                kind="failed",
                detail={"output_tail": _tail(output)},
            )
        codex_verdict = _run_codex_merge_review(
            worktree=tmpdir,
            line=state.line,
            into=state.into,
            target_head=state.target_head,
        )
        # The commit that lands must be the tree the tests and Codex saw.
        # A test (or hook) that edits tracked files or commits in the rehearsal
        # would otherwise let untested contents through the landing fence.
        if (
            _is_dirty(tmpdir)
            or _has_hidden_index_flags(tmpdir)
            or _head(tmpdir) != rehearsal_head
        ):
            raise ReviewMergeError(
                "rehearsal_modified",
                (
                    "The test run or Codex changed the rehearsal worktree, so the "
                    "tested contents would differ from the merge result; the target "
                    "branch was not touched."
                ),
                remediation=(
                    "Make the test command read-only with respect to the checkout "
                    "(no commits, no edits to tracked files), then retry."
                ),
                status_code=409,
                kind="failed",
                detail={"conflicts": [], "target_branch_untouched": True},
            )
    finally:
        try:
            _remove_temp_worktree(state.target_path, tmpdir)
        except Exception:  # cleanup must never mask the structured result
            logger.exception(
                "review/merge: could not remove rehearsal worktree %s (added=%s)",
                tmpdir,
                worktree_added,
            )
    _verify_unchanged(state)
    return MergeRehearsal(
        line_head=state.line_head,
        target_head=state.target_head,
        merge_commit=rehearsal_head,
        codex_verdict=codex_verdict,
    )


def _land(state: MergePreconditions, rehearsal: MergeRehearsal) -> dict[str, Any]:
    """Land the rehearsed merge commit on the named target branch.

    Landing is one ref-only compare-and-set:

    ``git update-ref refs/heads/<into> <rehearsed merge commit> <tested target head>``

    It either moves the named branch onto the exact commit the tests and Codex
    reviewed, or changes nothing (``409 target_advanced``). The server does not
    touch the target worktree or index: a checked-out worktree may hold
    uncommitted edits or change concurrently, and rewriting it server-side is
    the filesystem/race surface this contract deliberately drops. On success
    the response says ``worktree_sync_required: true`` so the caller knows to
    sync its checkout (for example ``git -C <target> reset --hard``).

    The CAS names the ref and its expected old value; it does not depend on
    what the worktree currently has checked out and needs no worktree locks.
    """
    if rehearsal.merge_commit == state.target_head:
        raise ReviewMergeError(
            "already_merged",
            f"Line {state.line!r} is already merged into {state.into!r}; nothing to land.",
            remediation=(
                "Commit new work on the line branch, re-run the Codex review, then retry."
            ),
            status_code=409,
            detail={"target_head": state.target_head},
        )
    cas = _git(
        state.target_path,
        "update-ref",
        "--no-deref",
        f"refs/heads/{state.into}",
        rehearsal.merge_commit,
        state.target_head,
    )
    if cas.returncode:
        raise ReviewMergeError(
            "target_advanced",
            (
                "The target branch moved while the merge was landing; the reviewed "
                "merge was not applied."
            ),
            remediation=(
                "Re-run the merge so the line is re-rehearsed against the new target "
                "head and Codex reviews it again."
            ),
            status_code=409,
            kind="failed",
            detail={
                "expected_target": state.target_head,
                "output": _tail((cas.stdout or "") + (cas.stderr or "")),
            },
        )
    return {
        "merged": True,
        "line": state.line,
        "branch": state.line_branch,
        "into": state.into,
        "commit": rehearsal.merge_commit,
        "tested_sha": state.line_head,
        "tests": "green",
        "pushed": False,
        "tested_in_temporary_worktree": True,
        "codex_verdict": "merge",
        "landed_ref": f"refs/heads/{state.into}",
        "worktree_sync_required": True,
    }


# ---------------------------------------------------------------------------
# Merge orchestration + audit
# ---------------------------------------------------------------------------


def _audit_error_detail(exc: ReviewMergeError) -> dict[str, Any]:
    """Audit-safe error payload: no raw process output ever reaches the table."""
    payload = exc.as_detail()
    inner = dict(payload.get("detail") or {})
    for key in (
        "output",
        "output_tail",
        "final_message_tail",
        "merge_output",
        "abort_output",
    ):
        inner.pop(key, None)
    payload["detail"] = inner
    return payload


async def _audit_merge(
    *,
    user_id: str,
    action: str,
    line: str,
    into: str,
    outcome: str,
    detail: dict[str, Any],
) -> None:
    """Best-effort audit write for refusals/failures.

    ``merge_started`` is the durable record that gates the landing; it is
    written with fail-closed semantics separately. These rows describe an
    attempt that did not land (or the landing result), so a database failure is
    logged rather than masking the original structured error.
    """
    try:
        await review_db.record_audit(
            user_id=user_id,
            action=action,
            line=line,
            target_branch=into,
            outcome=outcome,
            detail=detail,
        )
    except Exception:  # noqa: BLE001 - documented above
        logger.exception(
            "review/merge: audit write failed for %s on line %s", action, line
        )


async def audit_local_denial(user_id: str, *, line: str, into: str) -> None:
    await _audit_merge(
        user_id=user_id,
        action="merge_refused",
        line=line,
        into=into,
        outcome="refused",
        detail={
            "code": "local_only",
            "message": "Merge is only available to a local caller on the Conductor host.",
            "remediation": "Run the merge from the machine that runs the Conductor backend.",
        },
    )


async def merge(
    *,
    user_id: str,
    line: str,
    into: str,
    test_cmd: str | None,
    claimed_root: str | None,
) -> dict[str, Any]:
    """Rehearse, run the mandatory Codex review, then land the merge.

    The queue review is the operator's durable record; the merge gate is the
    Codex review this route executes itself against the exact rehearsal
    worktree. Trusting only a review the server ran removes both a forged
    verdict path and the race between a verdict being recorded and the land.
    """
    try:
        state = await asyncio.to_thread(
            _prepare_merge, line=line, into=into, claimed_root=claimed_root
        )
        rehearsal = await asyncio.to_thread(_rehearse, state, test_cmd)
        try:
            # Durable intent record BEFORE git moves: if the process dies after
            # this point, the audit says a merge started for these SHAs.
            await review_db.record_audit(
                user_id=user_id,
                action="merge_started",
                line=line,
                target_branch=into,
                outcome="ok",
                detail={
                    "tested_sha": rehearsal.line_head,
                    "tested_target_sha": rehearsal.target_head,
                    "merge_commit": rehearsal.merge_commit,
                    "codex_verdict": rehearsal.codex_verdict,
                },
            )
        except Exception as exc:  # noqa: BLE001 - fail closed before landing
            logger.exception("review/merge: merge_started audit write failed")
            raise ReviewMergeError(
                "audit_unavailable",
                (
                    "The merge was not attempted because its durable audit record "
                    "could not be written."
                ),
                remediation=(
                    "Check the database connection, then retry. The target branch "
                    "was not touched."
                ),
                status_code=503,
                kind="failed",
            ) from exc
        result = await asyncio.to_thread(_land, state, rehearsal)
    except ReviewMergeError as exc:
        await _audit_merge(
            user_id=user_id,
            action="merge_refused" if exc.kind == "refused" else "merge_failed",
            line=line,
            into=into,
            outcome=exc.kind,
            detail=_audit_error_detail(exc),
        )
        raise
    except Exception as exc:  # noqa: BLE001 - recorded, then surfaced as a structured error
        logger.exception("review/merge: unexpected merge failure on line %s", line)
        error = ReviewMergeError(
            "merge_internal_error",
            "The merge failed unexpectedly; the target branch was not intentionally changed.",
            remediation="Inspect the Conductor backend log, check the target worktree, then retry.",
            status_code=500,
            kind="failed",
            detail={"reason": sanitize_error_text(str(exc))[:500]},
        )
        await _audit_merge(
            user_id=user_id,
            action="merge_failed",
            line=line,
            into=into,
            outcome="failed",
            detail=_audit_error_detail(error),
        )
        raise error from exc
    await _audit_merge(
        user_id=user_id,
        action="merge_merged",
        line=line,
        into=into,
        outcome="ok",
        detail={
            "tested_sha": result["tested_sha"],
            "commit": result["commit"],
            "codex_verdict": result["codex_verdict"],
            "tested_in_temporary_worktree": True,
            "landed_ref": result["landed_ref"],
            "worktree_sync_required": True,
        },
    )
    return result
