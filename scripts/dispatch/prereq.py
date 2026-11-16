#!/usr/bin/env python3
"""Launch checks for fleet review/gate jobs, and a sandbox-safe .done marker.

tuiq was starting reviews and gates while the author commit was still missing
or was older than the prompt that asked for the work. Sandboxed agents (dsh)
also cannot create ``logs/<run>.log.done`` (EPERM), so the dispatcher never
saw them finish. ``bin/`` is edited by other sessions; this module is the
contract those scripts call after review. See docs/dispatch/TOOLING-FIXES.md.
"""

from __future__ import annotations

import argparse
import errno
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

STATUS_READY = "ready"
STATUS_NOT_READY = "not-ready"

REASON_NOT_REVIEW = "not-review-or-gate"
REASON_BAD_JOB = "bad-job-name"
REASON_PROMPT = "prompt-missing"
REASON_COMMIT = "commit-missing"
REASON_NOT_HEAD = "commit-not-in-head"
REASON_NOT_NEWER = "commit-not-newer"
REASON_GIT = "git-failed"
REASON_READY = "author-commit-newer-than-prompt"

# Same shapes revq skips and tui treats as reviews, plus gate jobs.
# ``gate`` / ``gate-*`` only: ``gateway`` is not a gate. ``cur-`` requires the
# dash, matching the pane check ``cur-*``.
_REVIEW_OR_GATE = re.compile(
    r"^(?:"
    r"gate(?:-.*)?|"
    r"rv\d*(?:-.*)?|"
    r"rr\d*(?:-.*)?|"
    r"tr\d*(?:-.*)?|"
    r"dbrev\d*(?:-.*)?|"
    r"review\d*(?:-.*)?|"
    r"cur-.*"
    r")$"
)
_JOB_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")
_AUTHOR_FROM_NAME = re.compile(
    r"^(?:rv\d*|rr\d*|tr\d*|dbrev\d*|review\d*|cur)-(?P<job>[A-Za-z0-9][A-Za-z0-9._-]*)$"
)
_AUTHOR_JOB_LINE = re.compile(
    r"(?m)^Author-Job:[ \t]+([A-Za-z0-9][A-Za-z0-9._-]*)[ \t]*$"
)
_SHA = re.compile(r"^[0-9a-f]{40}$")

# Remote runs already drop markers here (bin/rtui). Local sandboxes that cannot
# write logs use the same dispatcher-owned directory unless DISPATCH_DONE_DIR
# or --fallback points somewhere the sandbox is allowed to create files.
def _worktree_root() -> str:
    """Match ``src.config.models.conductor_worktree_root`` without importing it.

    This file is executed as a script, so ``src`` is not on ``sys.path``.
    """
    for name in ("CONDUCTOR_WORKTREE_ROOT", "CONDUCTOR_WT_ROOT"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return "~/.conductor/worktrees"


def _default_fallback_dir() -> Path:
    return Path(_worktree_root()).expanduser() / "jobs" / ".done"


DEFAULT_FALLBACK_DIR = _default_fallback_dir()
_GIT_TIMEOUT_S = 30
_PROMPT_SCAN_BYTES = 65536


@dataclass(frozen=True, slots=True)
class PrereqResult:
    """``status`` is ``ready`` or ``not-ready``. ``reason`` is a stable token."""

    status: str
    reason: str

    @property
    def ready(self) -> bool:
        return self.status == STATUS_READY


def is_review_or_gate(job_name: str) -> bool:
    """True for review and gate run names tuiq must hold for an author commit."""
    return _REVIEW_OR_GATE.fullmatch(job_name) is not None


def default_fallback_dir() -> Path:
    """Dispatcher-owned marker directory. ``DISPATCH_DONE_DIR`` overrides it."""
    override = os.environ.get("DISPATCH_DONE_DIR", "").strip()
    if override:
        return Path(override)
    return DEFAULT_FALLBACK_DIR


def check_prereq(
    worktree: str | Path,
    expected_commit: str,
    job_name: str,
    *,
    prompts_dir: str | Path | None = None,
) -> PrereqResult:
    """Return not-ready unless this review/gate can see the author commit.

    The commit named by ``expected_commit`` has to resolve in ``worktree``,
    be contained in HEAD, and have a committer time strictly after the author
    prompt's mtime. The author prompt is the baseline, not the review or gate
    prompt: revq writes the review prompt after ``.done``, which is after a
    real commit, so that file's mtime would hold every finished review.
    Dev jobs are ready immediately; holding them is tuiq's other problem.

    Git runs as ``git -C <worktree>`` with repo-locating environment variables
    removed, so a caller ``GIT_DIR`` cannot point the check at another checkout.
    """
    if not is_review_or_gate(job_name):
        return PrereqResult(STATUS_READY, REASON_NOT_REVIEW)
    if _JOB_TOKEN.fullmatch(job_name) is None:
        return PrereqResult(STATUS_NOT_READY, REASON_BAD_JOB)

    root = Path(worktree)
    prompts = (
        Path(prompts_dir)
        if prompts_dir is not None
        else root.resolve().parent / "prompts"
    )
    prompt = _resolve_prompt(prompts, job_name)
    if prompt is None:
        return PrereqResult(STATUS_NOT_READY, REASON_PROMPT)

    sha, failure = _resolve_commit(root, expected_commit)
    if sha is None:
        return PrereqResult(STATUS_NOT_READY, failure or REASON_COMMIT)
    if not _commit_in_head(root, sha):
        return PrereqResult(STATUS_NOT_READY, REASON_NOT_HEAD)
    try:
        prompt_mtime = prompt.stat().st_mtime
    except OSError:
        return PrereqResult(STATUS_NOT_READY, REASON_PROMPT)
    committed_at = _committer_time(root, sha)
    if committed_at is None:
        return PrereqResult(STATUS_NOT_READY, REASON_GIT)
    if committed_at <= prompt_mtime:
        return PrereqResult(STATUS_NOT_READY, REASON_NOT_NEWER)
    return PrereqResult(STATUS_READY, REASON_READY)


def write_done_marker(
    marker: str | Path, *, fallback_dir: str | Path | None = None
) -> Path:
    """Create ``marker``. On sandbox EPERM/EACCES, create it under ``fallback_dir``.

    The primary path is the ``touch <log>.done`` tui asks the agent to run.
    A seatbelt or sandbox that denies the worktree logs directory raises EPERM
    (sometimes EACCES). Other errors propagate: a missing logs directory is a
    dispatcher bug, not a signal to hide in the fallback. The fallback file
    records the primary path so a sweep outside the sandbox can adopt it.
    """
    target = Path(marker)
    try:
        _touch_regular(target)
    except OSError as exc:
        if not _sandbox_denied(exc):
            raise
        denied = exc
    else:
        return target

    destination = _fallback_destination(
        Path(fallback_dir) if fallback_dir is not None else default_fallback_dir(),
        target.name,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    _touch_regular(destination)
    _write_fallback_note(destination, target, denied.errno or errno.EPERM)
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="prereq")
    sub = parser.add_subparsers(dest="cmd", required=True)

    check = sub.add_parser("check", help="exit 0 when a review/gate may launch")
    check.add_argument("--worktree", required=True, type=Path)
    check.add_argument("--commit", required=True)
    check.add_argument("--job", required=True)
    check.add_argument("--prompts", type=Path)

    mark = sub.add_parser("mark-done", help="write a .done marker, with EPERM fallback")
    mark.add_argument("--marker", required=True, type=Path)
    mark.add_argument("--fallback", type=Path)

    args = parser.parse_args(argv)
    if args.cmd == "check":
        result = check_prereq(
            args.worktree,
            args.commit,
            args.job,
            prompts_dir=args.prompts,
        )
        print(result.status)
        print(result.reason)
        return 0 if result.ready else 3

    try:
        written = write_done_marker(args.marker, fallback_dir=args.fallback)
    except (OSError, ValueError) as exc:
        print(f"mark-done: {exc}", file=sys.stderr)
        return 1
    print(written)
    return 0


def _resolve_prompt(prompts: Path, job_name: str) -> Path | None:
    """Author prompt named by the review, or by an ``Author-Job`` line.

    A gate name does not strip to the author run (``gate-stack-r4-r6`` is not
    ``stack-r4-sync-r6``). The gate prompt's ``Author-Job`` line is that link.
    A header that names a missing file fails closed rather than falling back
    to the gate prompt, which is often written after the commit.

    A review name that does strip (``rv-foo``, ``rr9-foo``) also fails closed
    when that author file is missing. Falling through to the review file
    compares against a prompt revq writes after ``.done``. The missing
    baseline then looks like ``commit-not-newer`` forever, or like a false
    ready when the review file is older than the commit. An unreadable
    prompts directory is the same ``prompt-missing`` result, not an exception.
    """
    own = _prompt_path(prompts, job_name)
    if own is not None and _is_prompt_file(own):
        named = _author_job_header(own)
        if named is not None:
            target = _prompt_path(prompts, named)
            if target is not None and _is_prompt_file(target):
                return target
            return None
    author = _author_from_job_name(job_name)
    if author is not None:
        author_prompt = _prompt_path(prompts, author)
        if author_prompt is not None and _is_prompt_file(author_prompt):
            return author_prompt
        return None
    if own is not None and _is_prompt_file(own):
        return own
    return None


def _is_prompt_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _author_from_job_name(job_name: str) -> str | None:
    match = _AUTHOR_FROM_NAME.fullmatch(job_name)
    if match is None:
        return None
    return match.group("job")


def _author_job_header(path: Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(_PROMPT_SCAN_BYTES)
    except OSError:
        return None
    match = _AUTHOR_JOB_LINE.search(text)
    if match is None:
        return None
    return match.group(1)


def _prompt_path(prompts: Path, job_name: str) -> Path | None:
    if _JOB_TOKEN.fullmatch(job_name) is None:
        return None
    try:
        root = prompts.resolve()
        path = (root / f"{job_name}.txt").resolve()
    except OSError:
        return None
    if path.parent != root:
        return None
    return path


def _resolve_commit(worktree: Path, expected: str) -> tuple[str | None, str | None]:
    """Return ``(sha, None)`` or ``(None, reason)``. One git call either way."""
    token = expected.strip()
    if not token or "\n" in token or token.startswith("-"):
        return None, REASON_COMMIT
    proc = _git(
        worktree, "rev-parse", "--verify", "--end-of-options", f"{token}^{{commit}}"
    )
    if proc is None:
        return None, REASON_GIT
    if proc.returncode != 0:
        return None, REASON_COMMIT
    sha = proc.stdout.strip()
    if _SHA.fullmatch(sha) is None:
        return None, REASON_GIT
    return sha, None


def _commit_in_head(worktree: Path, sha: str) -> bool:
    proc = _git(worktree, "merge-base", "--is-ancestor", sha, "HEAD")
    return proc is not None and proc.returncode == 0


def _committer_time(worktree: Path, sha: str) -> int | None:
    proc = _git(worktree, "log", "-1", "--format=%ct", sha)
    if proc is None or proc.returncode != 0:
        return None
    raw = proc.stdout.strip()
    if not raw.isdigit():
        return None
    return int(raw)


# Select a repository, an object store, or a ref namespace. `git -C` does not
# override GIT_DIR, so a fleet shell that exported these would make the check
# read the caller's checkout. GIT_CONFIG* is the same class (injected config).
# Identity and pager variables stay; they do not choose a repository.
_REPO_LOCATING_ENV = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_INDEX_FILE",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_GRAFT_FILE",
        "GIT_REPLACE_REF_BASE",
        "GIT_IMPLICIT_WORK_TREE",
    }
)


def _git_env() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in _REPO_LOCATING_ENV and not key.startswith("GIT_CONFIG")
    }
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def _git(worktree: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    # `-C` is the only repo location. The sanitized env drops GIT_DIR and the
    # other variables that would override it.
    try:
        return subprocess.run(
            ["git", "-C", os.fspath(worktree), *args],
            check=False,
            capture_output=True,
            text=True,
            env=_git_env(),
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _sandbox_denied(exc: OSError) -> bool:
    return exc.errno in (errno.EPERM, errno.EACCES)


def _touch_regular(path: Path) -> None:
    # A symlink here would write outside the marker path. Sandboxes already
    # fail the real logs file with EPERM; refusing a link takes the same branch
    # for the primary path and aborts if the fallback itself is a link.
    if path.is_symlink():
        raise OSError(errno.EPERM, "refusing to follow symlink", os.fspath(path))
    fd = os.open(os.fspath(path), os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        os.utime(fd, None)
    finally:
        os.close(fd)


def _fallback_destination(fallback_dir: Path, name: str) -> Path:
    if name in {"", ".", ".."} or "/" in name or "\\" in name or "\x00" in name:
        raise ValueError(f"unsafe marker name: {name!r}")
    # Resolve the directory only. Resolving the marker would follow a symlink
    # planted at that name and hide it from the EPERM refusal below.
    root = fallback_dir.resolve()
    destination = root / name
    if destination.parent != root:
        raise ValueError(f"unsafe marker name: {name!r}")
    return destination


def _write_fallback_note(path: Path, primary: Path, err: int) -> None:
    code = errno.errorcode.get(err, str(err))
    note = f"primary={primary}\nerrno={code}\n"
    fd = os.open(os.fspath(path), os.O_WRONLY | os.O_TRUNC)
    try:
        os.write(fd, note.encode("utf-8"))
    finally:
        os.close(fd)


if __name__ == "__main__":
    raise SystemExit(main())
