"""A new dispatch is cut from the up-to-date base, not a stale checkout (N62).

``base_commit`` was ``git_head(repo_root)``: the deployed dispatch-repo
checkout, which only moves on a deploy. F2 was dispatched after F1 merged
(#99) while dispatch-repo still pointed at the commit before it, so its
worker re-created F1's file; N80's PRD (#140) was missing from its lines'
worktrees for the same reason. Before a new dispatch takes its base, this
fetches the configured base ref's remote and compares the checkout with it.

``CONDUCTOR_DISPATCH_FRESH_BASE_MODE`` (off|shadow|enforce, default
``shadow``):

* ``off``: the base is HEAD, nothing is fetched or recorded (the old path);
* ``shadow``: fetch and compare, record ``gates.base_check``, keep HEAD;
* ``enforce``: a checkout behind the ref bases the dispatch on the ref's
  commit instead; a checkout that cannot be checked (fetch failed, ref
  missing) or has diverged from the ref is refused with a reason that says
  what to do. A checkout ahead of the ref keeps HEAD: it already contains
  the ref.

``CONDUCTOR_DISPATCH_BASE_REF`` names the ref (default ``origin/main``); the
part before the first ``/`` is the remote that is fetched. A checkout with no
such remote (a local-only repository) has nothing to be stale against and
keeps HEAD. The fetch reuses the hardening of the workspace pre-start fetch
(#143): no hooks, no fsmonitor daemon, no ``ext::`` transport, no prompt.
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .check_modes import ENFORCE, OFF, check_mode
from .protocols import DispatchError

logger = logging.getLogger(__name__)

FRESH_BASE_MODE_ENV = "CONDUCTOR_DISPATCH_FRESH_BASE_MODE"
BASE_REF_ENV = "CONDUCTOR_DISPATCH_BASE_REF"
FETCH_TIMEOUT_ENV = "CONDUCTOR_DISPATCH_FRESH_BASE_FETCH_TIMEOUT"
DEFAULT_BASE_REF = "origin/main"
DEFAULT_FETCH_TIMEOUT_SECONDS = 30.0

#: Where the check is recorded on the snapshot root.
BASE_CHECK_KEY = "base_check"

# Statuses.
CURRENT = "current"
AHEAD = "ahead"
BEHIND = "behind"
DIVERGED = "diverged"
NO_REMOTE = "no_remote"
FETCH_FAILED = "fetch_failed"
REF_MISSING = "base_ref_missing"
NO_HEAD = "no_head"

#: Statuses enforce refuses: the checkout's freshness is unknown or it diverged.
_REFUSED = frozenset({DIVERGED, FETCH_FAILED, REF_MISSING})

_STDERR_LIMIT = 500


class StaleBaseCheckout(DispatchError):
    """Enforce mode refused to cut a dispatch from this checkout."""

    def __init__(self, record: Mapping[str, Any]) -> None:
        self.record = dict(record)
        super().__init__(refusal_message(record))


def fresh_base_mode(environ: Mapping[str, str] | None = None) -> str:
    return check_mode(FRESH_BASE_MODE_ENV, environ)


def base_ref(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    value = (env.get(BASE_REF_ENV) or "").strip()
    if not value or value.startswith("-"):
        return DEFAULT_BASE_REF
    return value


def _fetch_timeout(environ: Mapping[str, str] | None = None) -> float:
    env = os.environ if environ is None else environ
    try:
        value = float(env.get(FETCH_TIMEOUT_ENV) or DEFAULT_FETCH_TIMEOUT_SECONDS)
    except ValueError:
        return DEFAULT_FETCH_TIMEOUT_SECONDS
    return max(1.0, value)


def _git(repo: Path, *args: str, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    from src.server.services.dispatch_workspaces import _FETCH_HARDENING

    argv = ["git"]
    for item in _FETCH_HARDENING:
        argv.extend(["-c", item])
    argv.extend(["-C", str(repo), *args])
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        check=False,
        stdin=subprocess.DEVNULL,
    )


def _rev(repo: Path, ref: str) -> str | None:
    try:
        done = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    except (OSError, subprocess.SubprocessError):
        return None
    sha = done.stdout.strip()
    return sha if done.returncode == 0 and sha else None


def _redact(text: str) -> str:
    from src.server.services.dispatch_workspaces import redact_git_output

    return redact_git_output(text.strip())[:_STDERR_LIMIT]


def check_base(
    repo_root: Path,
    *,
    mode: str | None = None,
    ref: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """The base commit for a new dispatch from ``repo_root``, and the check's record.

    ``off`` returns HEAD and no record. Otherwise the record says what was
    found (``status``) and which commit the dispatch is based on (``base``).
    Raises :class:`StaleBaseCheckout` only in ``enforce`` mode.
    """
    from .gates import git_head

    mode = mode or fresh_base_mode(environ)
    repo = Path(repo_root)
    head = git_head(repo) or ""
    if mode == OFF:
        return head, None
    ref = ref or base_ref(environ)
    record: dict[str, Any] = {
        "mode": mode,
        "ref": ref,
        "head": head,
        "base": head,
        "fetched": False,
    }
    if not head:
        record["status"] = NO_HEAD
        return head, record
    remote, _sep, branch = ref.partition("/")
    if branch:
        try:
            known = _git(repo, "remote", "get-url", "--", remote)
        except (OSError, subprocess.SubprocessError):
            known = None
        if known is None or known.returncode != 0:
            record["status"] = NO_REMOTE
            return head, record
        try:
            fetched = _git(
                repo,
                "fetch",
                "--no-recurse-submodules",
                "--",
                remote,
                timeout=_fetch_timeout(environ),
            )
            record["fetched"] = fetched.returncode == 0
            if not record["fetched"]:
                record["fetch_error"] = _redact(fetched.stderr)
        except subprocess.TimeoutExpired:
            record["fetch_error"] = "git fetch timed out"
        except (OSError, subprocess.SubprocessError) as exc:
            record["fetch_error"] = f"cannot run git: {type(exc).__name__}"
        if not record["fetched"]:
            record["status"] = FETCH_FAILED
            return _decide(head, record, mode)
    remote_sha = _rev(repo, ref)
    if remote_sha is None:
        record["status"] = REF_MISSING
        return _decide(head, record, mode)
    record["ref_commit"] = remote_sha
    try:
        counted = _git(repo, "rev-list", "--left-right", "--count", f"{head}...{remote_sha}")
        ahead_text, behind_text = counted.stdout.split()
        ahead, behind = int(ahead_text), int(behind_text)
    except (OSError, subprocess.SubprocessError, ValueError):
        record["status"] = REF_MISSING
        return _decide(head, record, mode)
    record.update(ahead=ahead, behind=behind)
    if behind == 0:
        record["status"] = AHEAD if ahead else CURRENT
    elif ahead == 0:
        record["status"] = BEHIND
    else:
        record["status"] = DIVERGED
    return _decide(head, record, mode)


def _decide(head: str, record: dict[str, Any], mode: str) -> tuple[str, dict[str, Any]]:
    status = record.get("status")
    if mode != ENFORCE:
        if status in _REFUSED or status == BEHIND:
            logger.warning(
                "dispatch base check (shadow): checkout %s is %s against %s",
                head[:12],
                status,
                record.get("ref"),
            )
            record["would"] = "refuse" if status in _REFUSED else "base_on_ref"
        return head, record
    if status in _REFUSED:
        raise StaleBaseCheckout(record)
    if status == BEHIND:
        base = str(record["ref_commit"])
        record["base"] = base
        return base, record
    return head, record


def refusal_message(record: Mapping[str, Any]) -> str:
    """Why enforce refused, and what to do, for the dispatch's message."""
    status = record.get("status")
    head = str(record.get("head") or "")[:12]
    ref = record.get("ref")
    if status == DIVERGED:
        detail = (
            f"the dispatch checkout ({head}) has {record.get('ahead')} commit(s) "
            f"not on {ref} and is {record.get('behind')} behind it"
        )
    elif status == FETCH_FAILED:
        detail = f"fetching {ref} failed ({record.get('fetch_error') or 'unknown error'})"
    else:
        detail = f"{ref} does not resolve to a commit in the dispatch checkout"
    return (
        f"base_checkout_{status}: {detail}. Update the dispatch checkout to {ref} "
        f"(or set {BASE_REF_ENV}) and retry, or set {FRESH_BASE_MODE_ENV}=shadow."
    )


__all__ = [
    "BASE_CHECK_KEY",
    "BASE_REF_ENV",
    "DEFAULT_BASE_REF",
    "FETCH_TIMEOUT_ENV",
    "FRESH_BASE_MODE_ENV",
    "StaleBaseCheckout",
    "base_ref",
    "check_base",
    "fresh_base_mode",
    "refusal_message",
]
