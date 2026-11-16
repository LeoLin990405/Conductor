"""RepoBar and GitHub events. Recording is the default; fix-CI is opt-in per repo.

The wake keeps a small projection (repository, kind, conclusion, url, name).
The raw delivery is not stored. Identity comes from the configured source, not
from the delivery. A CI failure becomes a Ticket only when that repository
opted in and the workspace binding allows it.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Set
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from src.server.services.autonomy.security import redact_external_text
from src.server.services.autonomy.wakers.base import (
    WakeEvent,
    redact_wake_payload,
    wake_idempotency_key,
)

logger = logging.getLogger(__name__)

_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_FAILURES = frozenset({"failure", "failed", "timed_out", "startup_failure", "error"})
_CI_NAMES = frozenset(
    {
        "ci",
        "ci_failure",
        "ci_failed",
        "ci_success",
        "check",
        "checks",
        "check_suite",
        "check_run",
        "workflow",
        "workflow_run",
        "status",
    }
)
_CI_FAILURE_LABELS = frozenset({"ci_failure", "ci_failed", "workflow_failed", "check_failure"})
_CHANGES = frozenset({"changes_requested", "pr_changes_requested"})
_LIST_READS = ("actions/runs", "issues", "pulls", "releases")
_GH_FIELD = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
_GH_VALUE = re.compile(r"^[A-Za-z0-9_.:T+-]{1,80}$")
_MAX_SOURCES = 3
_PAGE = 20
# Lists read newest-first with a page continuation on the source row.
_SCANNED_LISTS = ("actions/runs", "issues", "releases")
# Per poll: pages per scanned list and per PR's reviews. A wall-clock budget
# below also stops paging early; the source timeout would otherwise discard
# the whole poll and repeat it forever on a busy repository.
_MAX_LIST_PAGES = 3
_REVIEW_PAGE = 100
_MAX_REVIEW_PAGES = 3
_MAX_PAGE_NUMBER = 1000
# Soft: no further list page starts after this. Hard: every read is cut off
# here, below ``_SOURCE_TIMEOUT_SECONDS``, leaving time to commit progress.
_LIST_BUDGET_SECONDS = 1.5
_POLL_BUDGET_SECONDS = 3.0
# Runs and releases are also swept end to end, a few pages per poll, for
# rows that changed behind idle ones (a re-run, a late-published draft).
_SWEPT_LISTS = ("actions/runs", "releases")
_MAX_SWEEP_PAGES = 2
_RUN_RERUN_HORIZON = timedelta(days=30)
# The stamp each list is ordered by on GitHub.
_ORDER_KEYS = {
    "issues": "updated_at",
    "pulls": "updated_at",
    "releases": "created_at",
    "actions/runs": "created_at",
}
_MAX_PULL_DETAILS = 3
_SOURCE_TIMEOUT_SECONDS = 4.0
_GH_TIMEOUT_SECONDS = 2.5
_DEFAULT_POLL_SECONDS = 60
_MIN_POLL_SECONDS = 15
_LOOKBACK = timedelta(hours=1)
_OVERLAP = timedelta(seconds=10)
_REPO_KEYS = ("repository", "github_repository")
_REPO_LIST_KEYS = ("repositories", "github_repositories")

FetchRead = Callable[[Mapping[str, Any], str, Mapping[str, str]], Awaitable[object]]
GhRun = Callable[[list[str]], Awaitable[tuple[int, bytes, bytes]]]
WorkspaceOwner = Callable[[str, str, str], Awaitable[bool]]
# Persist the poll's wakes. Called before the source cursor moves.
EnqueueEvents = Callable[[list[WakeEvent]], Awaitable[object]]


class RepoBarPollError(RuntimeError):
    """A repository poll failed. The message is safe to log."""


@dataclass(frozen=True, slots=True)
class RepoBinding:
    """Owner and workspace taken from source configuration, not from a delivery."""

    user_id: str
    workspace_id: str
    repository: str
    fix_ci: bool = False
    source_id: str = ""


class RepoBarWaker:
    """Switchable repository source. Disabled collects nothing."""

    name = "repobar"

    def __init__(self, *, enabled: bool) -> None:
        self._enabled = bool(enabled)

    def enabled(self) -> bool:
        return self._enabled

    def collect(self, raw: object) -> list[WakeEvent]:
        if not self._enabled or raw is None:
            return []
        if isinstance(raw, list):
            events: list[WakeEvent] = []
            for item in raw[:50]:
                events.extend(self.collect(item))
            return events
        if not isinstance(raw, Mapping):
            return []
        event = repository_event(raw)
        return [event] if event is not None else []


def repository_event(
    raw: Mapping[str, Any], binding: RepoBinding | None = None
) -> WakeEvent | None:
    """Project one RepoBar or GitHub delivery into a redacted wake.

    Without a binding the wake carries no owner. A binding that names a
    different repository drops the delivery instead of trusting the payload.
    """
    repository = _repository_name(raw)
    if not _REPOSITORY.fullmatch(repository):
        return None
    if binding is None:
        user_id = ""
        workspace_id = ""
    else:
        if repository.lower() != binding.repository.strip().lower():
            return None
        user_id = binding.user_id.strip()
        workspace_id = binding.workspace_id.strip()
        repository = binding.repository.strip()
    kind, external_id, fields = _classify(raw, repository)
    if not kind or not external_id:
        return None
    payload = redact_wake_payload(fields)
    subject = f"{user_id}:{workspace_id}:{repository.lower()}"
    return WakeEvent(
        source="repobar",
        kind=kind,
        idempotency_key=wake_idempotency_key("repobar", kind, subject, external_id),
        payload=dict(payload) if isinstance(payload, dict) else {},
        repository=repository,
        user_id=user_id,
        workspace_id=workspace_id,
        observed_at=str(raw.get("observed_at") or fields.get("updated_at") or ""),
    )


def repo_action(kind: str, *, fix_ci: bool) -> str:
    """``create_ticket`` only for a CI failure on a repo that opted in."""
    if kind == "ci_failure" and fix_ci:
        return "create_ticket"
    return "record"


def fix_ci_enabled(repository: str, enabled_repos: Set[str] | None) -> bool:
    """True only when this repository is named. An empty set records and stops."""
    if not enabled_repos:
        return False
    name = repository.strip().lower()
    return name in {
        str(item).strip().lower() for item in enabled_repos if str(item).strip()
    }


def fix_ci_draft(event: WakeEvent) -> dict[str, Any]:
    """Ticket fields for one CI failure. The wake key is the origin, so a retry finds it."""
    name = str(event.payload.get("name") or "CI failed")
    url = str(event.payload.get("url") or "")
    scope = name if not url else f"{name}\n{url}"
    return {
        "user_id": event.user_id,
        "workspace_id": event.workspace_id,
        "title": f"Fix CI failure on {event.repository}"[:200],
        "scope": scope[:2000],
        "mandate": {
            "kind": "fix_ci",
            "repository": event.repository,
            "wake_key": event.idempotency_key,
            "url": url[:500],
        },
        "origin_trigger_id": event.idempotency_key,
        "origin_thread_key": f"github:{event.repository}"[:200],
        "policy_version": "",
    }


# In-process holders per origin key. ``origin_trigger_id`` has no unique
# index, so find-then-create is only safe while one caller holds the key.
_FIX_CI_LOCKS: dict[str, tuple[asyncio.Lock, list[int]]] = {}
_FIX_CI_LOCK_SALT = 751


async def ensure_fix_ci_ticket(
    draft: dict[str, Any], *, find, create, lock=None
) -> str:
    """Create the fix-CI Ticket once. ``find`` sees the row a lost ack already wrote.

    Two wakes for the same failure (a redelivery, two workers claiming the
    effect, a webhook racing the poll) would both miss on ``find`` and both
    insert. The check and the insert run under a per-origin lock: an
    in-process lock always, plus ``lock`` (the cross-process advisory lock
    on the default DB path). ``find`` is repeated after acquiring, so the
    loser of the race returns the winner's Ticket.
    """
    origin = str(draft.get("origin_trigger_id") or "")
    local, users = _FIX_CI_LOCKS.setdefault(origin, (asyncio.Lock(), [0]))
    # Counted, not ``locked()``: a released lock reads unlocked before its
    # next waiter runs, and dropping it then would let a third caller in.
    users[0] += 1
    try:
        async with local:
            if lock is None:
                return await _find_or_create(origin, draft, find=find, create=create)
            async with lock(origin):
                return await _find_or_create(origin, draft, find=find, create=create)
    finally:
        users[0] -= 1
        if users[0] == 0 and _FIX_CI_LOCKS.get(origin, (None,))[0] is local:
            _FIX_CI_LOCKS.pop(origin, None)


async def _find_or_create(origin: str, draft: dict[str, Any], *, find, create) -> str:
    existing = await find(origin)
    if existing:
        return str(existing)
    return str(await create(draft))


@asynccontextmanager
async def fix_ci_origin_lock(origin: str, *, timeout_seconds: float = 10.0):
    """Session advisory lock for one fix-CI origin, across processes.

    Session-level rather than transaction-level because ``find`` and
    ``create`` each use their own pooled connection. A busy lock raises so
    the wake effect is retried instead of creating a second Ticket.
    """
    from src.server.database.pool import open_session_connection

    db = await open_session_connection()
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    key = f"fix-ci:{origin}"
    try:
        while True:
            async with db.cursor() as cur:
                await cur.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s, %s))",
                    (key, _FIX_CI_LOCK_SALT),
                )
                acquired = bool((await cur.fetchone() or (False,))[0])
            if acquired:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("fix-CI ticket lock is busy; retry the wake")
            await asyncio.sleep(0.05)
        try:
            yield
        finally:
            async with db.cursor() as cur:
                await cur.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, %s))",
                    (key, _FIX_CI_LOCK_SALT),
                )
    finally:
        await db.close()


def matching_binding(
    event: WakeEvent, bindings: list[RepoBinding] | None
) -> RepoBinding | None:
    """The one configured binding for this wake's owner, workspace, and repo.

    An empty identity matches nothing, so an unbound delivery cannot attach
    itself to a repository someone else configured.
    """
    if not bindings or not event.user_id or not event.workspace_id:
        return None
    repo = event.repository.strip().lower()
    if not repo:
        return None
    matches = [
        item
        for item in bindings
        if item.user_id == event.user_id
        and item.workspace_id == event.workspace_id
        and item.repository.strip().lower() == repo
    ]
    if len(matches) != 1:
        return None
    return matches[0]


def declared_repositories(config: object) -> set[str] | None:
    """Repos a workspace config names, or None when it names none.

    An empty declaration is a set, so a workspace that listed repositories
    and then cleared them does not fall open.
    """
    if not isinstance(config, Mapping):
        return None
    found = False
    names: set[str] = set()
    for key in _REPO_KEYS:
        if key not in config:
            continue
        found = True
        text = str(config.get(key) or "").strip().lower()
        if text:
            names.add(text)
    for key in _REPO_LIST_KEYS:
        if key not in config:
            continue
        found = True
        raw = config.get(key)
        if isinstance(raw, str) and raw.strip():
            names.add(raw.strip().lower())
        elif isinstance(raw, list):
            for item in raw:
                text = str(item or "").strip().lower()
                if text:
                    names.add(text)
    if not found:
        return None
    return names


def workspace_allows(
    user_id: str,
    workspace_id: str,
    repository: str,
    workspace: Mapping[str, Any] | None,
) -> bool:
    """Owner match, plus the workspace repo set when that config declares one."""
    if not isinstance(workspace, Mapping):
        return False
    if str(workspace.get("user_id") or "") != user_id:
        return False
    row_id = str(workspace.get("workspace_id") or "")
    if row_id and row_id != workspace_id:
        return False
    declared = declared_repositories(workspace.get("config"))
    if declared is None:
        return True
    return repository.strip().lower() in declared


async def workspace_owner(user_id: str, workspace_id: str, repository: str) -> bool:
    """Read the workspace row. A lookup error propagates so the effect retries."""
    from src.server.database.workspace import get_workspace

    workspace = await get_workspace(workspace_id)
    return workspace_allows(user_id, workspace_id, repository, workspace)


def binding_from_source(row: Mapping[str, Any]) -> RepoBinding | None:
    """Binding from a source row. Payload fields are not consulted.

    Only ``github`` is a production source kind (``SourceKind``); RepoBar is
    the waker reading it, not a kind of its own.
    """
    if str(row.get("kind") or "") != "github":
        return None
    config = row.get("config")
    if not isinstance(config, Mapping):
        return None
    repository = str(config.get("repository") or "").strip()
    user_id = str(row.get("user_id") or "").strip()
    workspace_id = str(config.get("workspace_id") or "").strip()
    source_id = str(row.get("source_id") or "").strip()
    if not _REPOSITORY.fullmatch(repository) or not user_id or not workspace_id:
        return None
    if not source_id:
        return None
    return RepoBinding(
        user_id=user_id,
        workspace_id=workspace_id,
        repository=repository,
        fix_ci=_flag(config.get("fix_ci")),
        source_id=source_id,
    )


async def load_repo_bindings(user_id: str = "") -> list[RepoBinding]:
    """Enabled GitHub source bindings. An empty user id lists every owner."""
    from src.server.database.autonomy.sources import list_enabled_sources

    owner = str(user_id or "")
    rows = await list_enabled_sources(limit=100)
    bindings: list[RepoBinding] = []
    for row in rows:
        if owner and str(row.get("user_id") or "") != owner:
            continue
        binding = binding_from_source(row)
        if binding is not None:
            bindings.append(binding)
    return bindings


async def poll_repobar_source(
    row: Mapping[str, Any],
    *,
    save_state: Callable[..., Awaitable[None]] | None = None,
    fetch: FetchRead | None = None,
    gh_run: GhRun | None = None,
    workspace_owner_check: WorkspaceOwner | None = None,
    feature_enabled: Callable[[str], Awaitable[bool]] | None = None,
    now: Callable[[], datetime] | None = None,
    enqueue_events: EnqueueEvents | None = None,
) -> list[WakeEvent]:
    """Poll one repository source. Used by ``poll_source_effect``.

    Wake effects commit before (or with) the cursor. ``issue_opened`` comes
    from this list-poll path when an issue was created inside the window.
    """
    binding = binding_from_source(row)
    if binding is None:
        raise RepoBarPollError("source is not a repository binding")
    moment = _aware(now() if now is not None else datetime.now(timezone.utc))
    needs_http = False
    async with _maybe_session(needs_http) as session:
        return await asyncio.wait_for(
            _poll_source(
                row,
                binding,
                moment=moment,
                fetch=fetch,
                gh_run=gh_run,
                session=session,
                save_state=save_state,
                enqueue_events=enqueue_events,
                workspace_owner_check=workspace_owner_check or workspace_owner,
                feature_enabled=feature_enabled or _default_feature,
            ),
            timeout=_SOURCE_TIMEOUT_SECONDS,
        )


async def poll_configured_repositories(
    *,
    list_sources: Callable[..., Awaitable[list[dict[str, Any]]]] | None = None,
    save_state: Callable[..., Awaitable[None]] | None = None,
    fetch: FetchRead | None = None,
    gh_run: GhRun | None = None,
    workspace_owner_check: WorkspaceOwner | None = None,
    feature_enabled: Callable[[str], Awaitable[bool]] | None = None,
    now: Callable[[], datetime] | None = None,
    enqueue_events: EnqueueEvents | None = None,
) -> list[WakeEvent]:
    """Poll a few due repository sources. One source failing does not stop the rest.

    Cursors live on the source row. A failed fetch does not move the cursor.
    Wake effects are stored before the cursor moves. With no injected writer
    the effect rows and the cursor commit in one transaction, so a crash
    cannot drop events that the next poll will never see again. The pass is
    capped so it can run while the scheduler holds its lock.
    """
    moment = _aware(now() if now is not None else datetime.now(timezone.utc))
    rows = await (list_sources or _default_sources)(limit=100)
    chosen: list[tuple[Mapping[str, Any], RepoBinding]] = []
    for row in rows:
        if len(chosen) >= _MAX_SOURCES:
            break
        if not isinstance(row, Mapping) or not _source_due(row, moment):
            continue
        binding = binding_from_source(row)
        if binding is None:
            continue
        chosen.append((row, binding))
    needs_http = False
    events: list[WakeEvent] = []
    async with _maybe_session(needs_http) as session:
        for row, binding in chosen:
            try:
                found = await asyncio.wait_for(
                    _poll_source(
                        row,
                        binding,
                        moment=moment,
                        fetch=fetch,
                        gh_run=gh_run,
                        session=session,
                        save_state=save_state,
                        enqueue_events=enqueue_events,
                        workspace_owner_check=workspace_owner_check or workspace_owner,
                        feature_enabled=feature_enabled or _default_feature,
                    ),
                    timeout=_SOURCE_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                await _defer_failed(
                    save_state, binding, row, moment, "repository poll timed out"
                )
                continue
            except Exception as exc:  # noqa: BLE001 - one source stays isolated
                await _defer_failed(
                    save_state,
                    binding,
                    row,
                    moment,
                    redact_external_text(str(exc))[:500],
                )
                continue
            events.extend(found)
    return events


async def _defer_failed(
    save_state: Callable[..., Awaitable[None]] | None,
    binding: RepoBinding,
    row: Mapping[str, Any],
    moment: datetime,
    reason: str,
) -> None:
    """Log a failed source and push only its next-poll time back.

    A source that fails before it saves state stays due and, with the list
    ordered by ``updated_at``, keeps the same slot at the front: three broken
    repositories would starve every healthy one behind them. The cursor is
    not touched, so the next attempt re-reads the same window. The deferral
    itself is best effort; its failure must not stop the pass either.
    """
    logger.error(
        "[autonomy] repobar source %s failed: %s", binding.source_id, reason
    )
    config = row.get("config") if isinstance(row.get("config"), Mapping) else {}
    try:
        await _defer(
            save_state or _default_save, binding, moment, _poll_seconds(config)
        )
    except Exception as exc:  # noqa: BLE001 - the remaining sources still run
        logger.error(
            "[autonomy] repobar source %s deferral failed: %s",
            binding.source_id,
            redact_external_text(str(exc))[:500],
        )


def _repository_name(raw: Mapping[str, Any]) -> str:
    repo = raw.get("repository", raw.get("repo"))
    if isinstance(repo, str):
        return repo.strip()
    if isinstance(repo, Mapping):
        return str(repo.get("full_name") or "").strip()
    return ""


def _classify(
    raw: Mapping[str, Any], repository: str
) -> tuple[str, str, dict[str, str]]:
    workflow = raw.get("workflow_run")
    if isinstance(workflow, Mapping):
        return _ci_from_conclusion(
            workflow.get("conclusion"),
            external_id=_run_external_id(workflow),
            name=str(workflow.get("name") or workflow.get("display_title") or ""),
            url=str(workflow.get("html_url") or workflow.get("url") or ""),
            updated_at=str(workflow.get("updated_at") or ""),
        )
    check_run = raw.get("check_run")
    if isinstance(check_run, Mapping):
        return _ci_from_conclusion(
            check_run.get("conclusion"),
            external_id=f"check_run:{check_run.get('id')}",
            name=str(check_run.get("name") or ""),
            url=str(check_run.get("html_url") or check_run.get("url") or ""),
            updated_at=str(check_run.get("completed_at") or ""),
        )
    check_suite = raw.get("check_suite")
    if isinstance(check_suite, Mapping):
        return _ci_from_conclusion(
            check_suite.get("conclusion"),
            external_id=f"check_suite:{check_suite.get('id')}",
            name="check suite",
            url=str(check_suite.get("url") or ""),
            updated_at=str(check_suite.get("updated_at") or ""),
        )
    review = raw.get("review")
    if not isinstance(review, Mapping):
        review = raw.get("pull_request_review")
    if isinstance(review, Mapping):
        return _review_event(raw, review)
    pull = raw.get("pull_request")
    if isinstance(pull, Mapping):
        return _pull_event(raw, pull)
    issue = raw.get("issue")
    if isinstance(issue, Mapping) and "pull_request" not in issue:
        return _issue_event(raw, issue)
    release = raw.get("release")
    if isinstance(release, Mapping):
        return _release_event(raw, release)
    return _flat_event(raw, repository)


def _run_external_id(workflow: Mapping[str, Any]) -> str:
    """``workflow_run:<id>``, plus the attempt for a re-run.

    A re-run keeps the run id. Without the attempt its failure would share
    the first attempt's wake key and be dropped as a duplicate.
    """
    base = f"workflow_run:{workflow.get('id')}"
    if _ident(workflow.get("id")) == "":
        return base
    attempt = _int_or(workflow.get("run_attempt"), 1)
    if attempt > 1:
        return f"{base}:attempt:{attempt}"
    return base


def _review_event(
    raw: Mapping[str, Any], review: Mapping[str, Any]
) -> tuple[str, str, dict[str, str]]:
    pull = raw.get("pull_request") if isinstance(raw.get("pull_request"), Mapping) else {}
    number = _ident(pull.get("number") or pull.get("id"))
    review_id = _ident(review.get("id"))
    state = str(review.get("state") or "").strip().lower()
    action = str(raw.get("action") or "").strip().lower()
    name = str(pull.get("title") or "")
    url = str(review.get("html_url") or pull.get("html_url") or pull.get("url") or "")
    updated = str(review.get("submitted_at") or pull.get("updated_at") or "")
    if state in _CHANGES or action in _CHANGES:
        external = f"pr_changes_requested:{number}:{review_id}"
        return _record_kind(
            "pr_changes_requested",
            external_id=external,
            name=name,
            url=url,
            updated_at=updated,
        )
    external = f"pull_request_review:{number}:{review_id}:{state or action}"
    return _record_kind(
        "pull_request_review",
        external_id=external,
        name=name,
        url=url,
        updated_at=updated,
    )


def _pull_event(
    raw: Mapping[str, Any], pull: Mapping[str, Any]
) -> tuple[str, str, dict[str, str]]:
    action = str(raw.get("action") or "").strip().lower()
    number = _ident(pull.get("number") or pull.get("id"))
    name = str(pull.get("title") or "")
    url = str(pull.get("html_url") or pull.get("url") or "")
    updated = str(pull.get("updated_at") or "")
    if action in {"review_requested", "pr_review_requested"}:
        reviewer = _reviewer_id(raw.get("requested_reviewer") or raw.get("reviewer"))
        return _record_kind(
            "pr_review_requested",
            external_id=f"pr_review_requested:{number}:{reviewer}",
            name=name,
            url=url,
            updated_at=updated,
        )
    if action in _CHANGES:
        return _record_kind(
            "pr_changes_requested",
            external_id=f"pr_changes_requested:{number}:{action}",
            name=name,
            url=url,
            updated_at=updated,
        )
    if not action:
        return _record_kind(
            "pull_request",
            external_id=f"pull_request:{number}",
            name=name,
            url=url,
            updated_at=updated,
        )
    stamp = _pull_stamp(pull)
    external = f"pull_request:{number}:{action}"
    if stamp:
        external = f"{external}:{stamp}"
    return _record_kind(
        "pull_request",
        external_id=external,
        name=name,
        url=url,
        updated_at=updated,
    )


def _issue_event(
    raw: Mapping[str, Any], issue: Mapping[str, Any]
) -> tuple[str, str, dict[str, str]]:
    action = str(raw.get("action") or "").strip().lower()
    number = _ident(issue.get("number") or issue.get("id"))
    name = str(issue.get("title") or "")
    url = str(issue.get("html_url") or issue.get("url") or "")
    updated = str(issue.get("updated_at") or "")
    if action == "opened":
        return _record_kind(
            "issue_opened",
            external_id=f"issue_opened:{number}",
            name=name,
            url=url,
            updated_at=updated,
        )
    external = f"issue:{number}" if not action else f"issue:{number}:{action}"
    return _record_kind(
        "issue", external_id=external, name=name, url=url, updated_at=updated
    )


def _release_event(
    raw: Mapping[str, Any], release: Mapping[str, Any]
) -> tuple[str, str, dict[str, str]]:
    action = str(raw.get("action") or "").strip().lower()
    release_id = _ident(release.get("id"))
    name = str(release.get("tag_name") or release.get("name") or "")
    url = str(release.get("html_url") or release.get("url") or "")
    updated = str(release.get("published_at") or release.get("created_at") or "")
    if action == "published":
        return _record_kind(
            "release_published",
            external_id=f"release_published:{release_id}",
            name=name,
            url=url,
            updated_at=updated,
        )
    external = f"release:{release_id}" if not action else f"release:{release_id}:{action}"
    return _record_kind(
        "release", external_id=external, name=name, url=url, updated_at=updated
    )


def _flat_event(
    raw: Mapping[str, Any], repository: str
) -> tuple[str, str, dict[str, str]]:
    label = (
        str(raw.get("event") or raw.get("kind") or raw.get("type") or "")
        .strip()
        .lower()
    )
    conclusion = (
        str(raw.get("conclusion") or raw.get("status") or raw.get("state") or "")
        .strip()
        .lower()
    )
    external = str(raw.get("event_id") or raw.get("delivery") or raw.get("id") or "")
    name = str(raw.get("name") or raw.get("title") or "")
    url = str(raw.get("url") or raw.get("html_url") or "")
    updated_at = str(raw.get("updated_at") or raw.get("observed_at") or "")
    reviewer = _reviewer_id(raw.get("requested_reviewer") or raw.get("reviewer"))
    if label in {"pull_request", "pr"}:
        kind = "pull_request"
    elif label in {"issue", "issues"}:
        kind = "issue"
    elif label == "release":
        kind = "release"
    elif label == "pr_review_requested":
        kind = "pr_review_requested"
        if reviewer:
            external = f"{external}:{reviewer}" if external else reviewer
    elif label == "pr_changes_requested":
        kind = "pr_changes_requested"
    elif label == "issue_opened":
        kind = "issue_opened"
    elif label == "release_published":
        kind = "release_published"
    elif label in _CI_NAMES or label in _CI_FAILURE_LABELS:
        external_id = external or _fallback_id(repository, label, url, name)
        implied = "failure" if "fail" in label else ""
        return _ci_from_conclusion(
            conclusion or implied,
            external_id=external_id,
            name=name,
            url=url,
            updated_at=updated_at,
        )
    else:
        return "", "", {}
    if not external:
        external = _fallback_id(repository, kind, url, name)
    return _record_kind(
        kind, external_id=external, name=name, url=url, updated_at=updated_at
    )


def _ci_from_conclusion(
    conclusion: object, *, external_id: str, name: str, url: str, updated_at: str
) -> tuple[str, str, dict[str, str]]:
    text = str(conclusion or "").strip().lower()
    if text in _FAILURES:
        kind = "ci_failure"
    elif text == "success":
        kind = "ci_success"
    else:
        kind = "ci"
    if not external_id or external_id.endswith(":None") or external_id.endswith(":"):
        external_id = _fallback_id(kind, text, url, name)
    return (
        kind,
        external_id,
        {
            "name": name,
            "url": url,
            "conclusion": text,
            "updated_at": updated_at,
        },
    )


def _record_kind(
    kind: str, *, external_id: str, name: str, url: str, updated_at: str
) -> tuple[str, str, dict[str, str]]:
    if not external_id or external_id.endswith(":None") or external_id.endswith(":"):
        external_id = _fallback_id(kind, name, url, updated_at)
    return kind, external_id, {"name": name, "url": url, "updated_at": updated_at}


def _fallback_id(*parts: str) -> str:
    raw = "|".join(part for part in parts if part)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _ident(value: object) -> str:
    text = str(value or "").strip()
    if not text or text.lower() == "none":
        return ""
    return text


def _reviewer_id(reviewer: object) -> str:
    if isinstance(reviewer, Mapping):
        return _ident(reviewer.get("id") or reviewer.get("login"))
    return _ident(reviewer)


def _pull_stamp(pull: Mapping[str, Any]) -> str:
    head = pull.get("head")
    if isinstance(head, Mapping) and head.get("sha"):
        return str(head.get("sha"))
    return str(pull.get("updated_at") or "")


def _flag(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "on"}
    if isinstance(value, int):
        return value == 1
    return False


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _aware(parsed)


def _stamp(moment: datetime) -> str:
    return _aware(moment).strftime("%Y-%m-%dT%H:%M:%SZ")


def _poll_seconds(config: Mapping[str, Any]) -> int:
    try:
        value = int(config.get("poll_seconds") or _DEFAULT_POLL_SECONDS)
    except (TypeError, ValueError):
        value = _DEFAULT_POLL_SECONDS
    return max(_MIN_POLL_SECONDS, min(3600, value))


def _source_due(row: Mapping[str, Any], moment: datetime) -> bool:
    state = row.get("state")
    if not isinstance(state, Mapping):
        return True
    nxt = _parse_time(state.get("waker_next_poll_at"))
    return nxt is None or nxt <= moment



def _waker_read_path(repository: str, path: str) -> bool:
    """True for the read-only RepoBar poll paths of one configured repository.

    Lives here so github.py credential-broker policy stays outside this line.
    """
    import re

    repo_path = f"/repos/{repository}"
    return (
        re.fullmatch(
            re.escape(repo_path)
            + r"/(?:actions/runs|issues|pulls|pulls/[0-9]+|"
            r"pulls/[0-9]+/reviews|pulls/[0-9]+/requested_reviewers|releases)",
            path,
        )
        is not None
    )



def _since(row: Mapping[str, Any], moment: datetime) -> datetime:
    state = row.get("state") if isinstance(row.get("state"), Mapping) else {}
    cursor = _parse_time(state.get("waker_cursor"))
    if cursor is None:
        return moment - _LOOKBACK
    return cursor


async def _default_sources(*, limit: int = 100) -> list[dict[str, Any]]:
    from src.server.database.autonomy.sources import list_enabled_sources

    return await list_enabled_sources(limit=limit)


async def _default_save(user_id: str, source_id: str, state: dict[str, Any]) -> None:
    from src.server.database.autonomy.sources import update_source

    await update_source(user_id, source_id, state=state)


async def _default_feature(user_id: str) -> bool:
    from src.server.services.features import user_feature_enabled

    return bool(await user_feature_enabled(user_id, "super_agent"))


@asynccontextmanager
async def _maybe_session(needed: bool):
    if not needed:
        yield None
        return
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=3, connect=2)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        yield session


async def _poll_source(
    row: Mapping[str, Any],
    binding: RepoBinding,
    *,
    moment: datetime,
    fetch: FetchRead | None,
    gh_run: GhRun | None,
    session: Any,
    save_state: Callable[..., Awaitable[None]] | None,
    enqueue_events: EnqueueEvents | None,
    workspace_owner_check: WorkspaceOwner,
    feature_enabled: Callable[[str], Awaitable[bool]],
) -> list[WakeEvent]:
    """One bounded poll. Whatever was read before the budget ran out is saved.

    Every GitHub read is cut off at ``_POLL_BUDGET_SECONDS``, below the
    source timeout, so a slow ``gh`` costs the unread pages, not the poll:
    the pages already read are enqueued and their continuation is saved.
    """
    config = row.get("config") if isinstance(row.get("config"), Mapping) else {}
    interval = _poll_seconds(config)
    writer = save_state or _default_save
    if not await feature_enabled(binding.user_id):
        await _defer(writer, binding, moment, interval)
        return []
    if not await workspace_owner_check(
        binding.user_id, binding.workspace_id, binding.repository
    ):
        await _defer(writer, binding, moment, interval)
        return []
    since = _since(row, moment)
    source_state = row.get("state") if isinstance(row.get("state"), Mapping) else {}
    pulls_page = _pulls_page(source_state)
    clock = time.monotonic()
    soft = clock + _LIST_BUDGET_SECONDS
    hard = clock + _POLL_BUDGET_SECONDS
    saved_scans = _list_scans(source_state)
    saved_reviews = _review_pages(source_state)
    # PRs whose detail reads were cut off last time are known before the
    # list comes back. Reading them alongside the lists keeps a slow GitHub
    # from always spending the budget on the lists and never reaching them.
    early = [
        number
        for number in _pull_ids(source_state.get("waker_pull_pending"))
        if number in saved_reviews
    ][:_MAX_PULL_DETAILS]
    reads = await asyncio.gather(
        _list_reads(
            row,
            binding.repository,
            since,
            moment=moment,
            pulls_page=pulls_page,
            saved_scans=saved_scans,
            saved_sweeps=_sweeps(source_state),
            soft=soft,
            hard=hard,
            fetch=fetch,
            gh_run=gh_run,
            session=session,
        ),
        _pull_details(
            row,
            binding,
            since,
            numbers=early,
            review_pages=saved_reviews,
            deadline=hard,
            fetch=fetch,
            gh_run=gh_run,
            session=session,
        ),
        return_exceptions=True,
    )
    for result in reads:
        if isinstance(result, BaseException):
            raise result
    (bodies, scans, sweeps, pulls_ok), (detail_events, unread_reviews) = reads
    list_scans, lists_started, floors = _list_scan_progress(
        saved_scans, scans, since, moment
    )
    events = _events_from_lists(binding, bodies, since)
    pulls_repeat = False
    pulls_sig = ""
    stored_sig = str(source_state.get("waker_pulls_sig") or "")
    if pulls_ok:
        pulls_items = _as_list(bodies.get("pulls"))
        pulls_sig = _page_signature(pulls_items) if len(pulls_items) >= _PAGE else ""
        pulls_repeat = pulls_page > 1 and bool(stored_sig) and pulls_sig == stored_sig
    if not pulls_ok or pulls_repeat:
        # Nothing new to visit: either the list was not read, or the server
        # handed back the page it gave last time (it ignores ``page``).
        visit, rest, done = [], [], []
        if pulls_repeat:
            floor = _page_floor(_as_list(bodies.get("pulls")), "pulls")
            if floor is not None:
                floors.append(floor)
    else:
        visit, rest, done = _pull_visit_plan(bodies.get("pulls"), since, source_state)
    if early:
        # This poll's detail reads went to the waiting PRs.
        finished = [n for n in early if n not in unread_reviews]
        queue = [n for n in [*visit, *rest] if n not in early]
        rest = [n for n in early if n in unread_reviews] + queue
        visit = finished
    elif visit:
        more, unread_reviews = await _pull_details(
            row,
            binding,
            since,
            numbers=visit,
            review_pages=saved_reviews,
            deadline=hard,
            fetch=fetch,
            gh_run=gh_run,
            session=session,
        )
        detail_events.extend(more)
        # A PR whose reads were cut off by the budget is not done: it goes
        # back to the front of the pending queue, which also holds the cursor.
        rest = [n for n in visit if n in unread_reviews] + [
            n for n in rest if n not in unread_reviews
        ]
        visit = [n for n in visit if n not in unread_reviews]
    events.extend(detail_events)
    kept = _kept(events, since)
    # Sweep rows are due against their own floor, which can be older than
    # ``since``: that is the point of the sweep.
    seen_keys = {event.idempotency_key for event in kept}
    for suffix, (items, _entry, floor) in sweeps.items():
        for event in _events_from_lists(binding, _list_body(suffix, items), floor):
            if event.idempotency_key not in seen_keys:
                seen_keys.add(event.idempotency_key)
                kept.append(event)
    state = _poll_state(
        bodies,
        since,
        moment,
        interval,
        visit=visit,
        rest=rest,
        done=done,
        pulls_page=pulls_page,
        scan_started=_scan_started(source_state),
        list_scans=list_scans,
        lists_started=lists_started,
        review_pages=unread_reviews,
        pulls_ok=pulls_ok,
        pulls_repeat=pulls_repeat,
        pulls_sig=pulls_sig,
        previous_sig=stored_sig,
        floors=floors,
    )
    state["waker_sweep"] = {suffix: entry for suffix, (_i, entry, _f) in sweeps.items()}
    if not pulls_ok:
        # The pulls continuation was not advanced; keep the stored one. Only
        # review pages read for the waiting PRs move.
        merged = {str(k): v for k, v in saved_reviews.items() if k not in early}
        merged.update({str(k): v for k, v in unread_reviews.items()})
        state["waker_review_pages"] = merged
    # Effects first. A cursor that commits on its own drops this page when
    # the enqueue fails or the process dies before it runs.
    await _persist_poll(
        binding,
        kept,
        state,
        enqueue_events=enqueue_events,
        save_state=save_state,
    )
    return kept


async def _persist_poll(
    binding: RepoBinding,
    events: list[WakeEvent],
    state: dict[str, Any],
    *,
    enqueue_events: EnqueueEvents | None,
    save_state: Callable[..., Awaitable[None]] | None,
) -> None:
    """Store wakes before the cursor. The default path commits both together."""
    if enqueue_events is None and save_state is None:
        await _commit_poll(binding, events, state)
        return
    if enqueue_events is not None:
        await enqueue_events(events)
    writer = save_state or _default_save
    await writer(binding.user_id, binding.source_id, state)


async def _commit_poll(
    binding: RepoBinding, events: list[WakeEvent], state: dict[str, Any]
) -> None:
    from src.server.services.autonomy.wakers.source_store import commit_source_wakes

    wakes: list[tuple[str, str, dict[str, Any]]] = []
    for event in events:
        key = event.idempotency_key.strip()
        if not key:
            continue
        wakes.append((event.user_id or binding.user_id, key, event.as_payload()))
    await commit_source_wakes(
        user_id=binding.user_id,
        source_id=binding.source_id,
        wakes=wakes,
        state=state,
    )


async def _defer(save_state, binding: RepoBinding, moment: datetime, interval: int) -> None:
    await save_state(
        binding.user_id,
        binding.source_id,
        {"waker_next_poll_at": _stamp(moment + timedelta(seconds=interval))},
    )


def _list_params(
    suffix: str, since: datetime, *, page: int = 1
) -> dict[str, str]:
    params = {"per_page": str(_PAGE)}
    if suffix in _SCANNED_LISTS:
        params["page"] = str(max(1, min(int(page), _MAX_PAGE_NUMBER)))
    if suffix == "issues":
        params.update(
            {
                "state": "all",
                "sort": "updated",
                "direction": "desc",
                "since": _stamp(since),
            }
        )
    elif suffix == "pulls":
        # Durable page continuation lives on the source row. A single
        # ``per_page`` window would leave PR 21 unread forever.
        params.update(
            {
                "state": "all",
                "sort": "updated",
                "direction": "desc",
                "page": str(max(1, int(page))),
            }
        )
    return params


def _pulls_page(state: Mapping[str, Any]) -> int:
    raw = state.get("waker_pulls_page")
    if isinstance(raw, bool) or raw is None:
        return 1
    try:
        page = int(raw)
    except (TypeError, ValueError):
        return 1
    return max(1, min(page, 1000))


class _OutOfTime(Exception):
    """A read did not finish before the poll budget ran out."""


async def _timed(awaitable: Awaitable[object], deadline: float | None) -> object:
    if deadline is None:
        return await awaitable
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        if asyncio.iscoroutine(awaitable):
            awaitable.close()
        raise _OutOfTime
    try:
        return await asyncio.wait_for(awaitable, timeout=remaining)
    except TimeoutError:
        raise _OutOfTime from None


@dataclass(slots=True)
class _Scan:
    """What one list scan read on this poll."""

    items: list[Mapping[str, Any]]
    # Next unread page, or 0 once the window is covered.
    next_page: int
    # Set when the scan ended on a repeated page: the oldest stamp on it.
    # The cursor may not pass it, since rows below were never read.
    floor: datetime | None = None
    signature: str = ""
    last_page: int = 0
    exhausted: bool = False


def _list_body(suffix: str, items: list[Mapping[str, Any]]) -> dict[str, object]:
    if suffix == "actions/runs":
        return {suffix: {"workflow_runs": items}}
    return {suffix: items}


async def _list_reads(
    row: Mapping[str, Any],
    repository: str,
    since: datetime,
    *,
    moment: datetime,
    pulls_page: int = 1,
    saved_scans: Mapping[str, tuple[int, datetime | None, str]] | None = None,
    saved_sweeps: Mapping[str, Mapping[str, Any]] | None = None,
    soft: float | None = None,
    hard: float | None = None,
    fetch: FetchRead | None,
    gh_run: GhRun | None,
    session: Any,
) -> tuple[
    dict[str, object],
    dict[str, _Scan],
    dict[str, tuple[list[Mapping[str, Any]], dict[str, Any], datetime]],
    bool,
]:
    """List bodies, each list's scan result, sweep results, and pulls-read.

    Pulls read one page per poll (``_poll_state`` owns that scan). Runs,
    issues and releases page until the window is covered, starting where
    the previous poll stopped. Runs and releases are also swept (below).
    """
    scans_in = dict(saved_scans or {})
    sweeps_in = dict(saved_sweeps or {})

    async def pulls() -> object:
        try:
            return await _timed(
                _read(
                    row,
                    repository,
                    "pulls",
                    _list_params("pulls", since, page=pulls_page),
                    fetch=fetch,
                    gh_run=gh_run,
                    session=session,
                ),
                hard,
            )
        except _OutOfTime:
            return _UNREAD

    async def scan(suffix: str):
        page, _started, sig = scans_in.get(suffix, (1, None, ""))
        result = await _scan_list(
            row,
            repository,
            suffix,
            since,
            start_page=page,
            previous_sig=sig,
            soft=soft,
            hard=hard,
            fetch=fetch,
            gh_run=gh_run,
            session=session,
        )
        sweep = None
        if suffix in _SWEPT_LISTS:
            sweep = await _sweep_list(
                row,
                repository,
                suffix,
                entry=sweeps_in.get(suffix) or {},
                since=since,
                moment=moment,
                main=result if page <= 1 else None,
                soft=soft,
                hard=hard,
                fetch=fetch,
                gh_run=gh_run,
                session=session,
            )
        return result, sweep

    results = await asyncio.gather(
        pulls(), *[scan(suffix) for suffix in _SCANNED_LISTS], return_exceptions=True
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result
    pulls_body = results[0]
    pulls_ok = pulls_body is not _UNREAD
    bodies: dict[str, object] = {"pulls": pulls_body if pulls_ok else []}
    scans: dict[str, _Scan] = {}
    sweeps: dict[str, tuple[list[Mapping[str, Any]], dict[str, Any], datetime]] = {}
    for suffix, (result, sweep) in zip(_SCANNED_LISTS, results[1:], strict=True):
        bodies.update(_list_body(suffix, result.items))
        scans[suffix] = result
        if sweep is not None:
            sweeps[suffix] = sweep
    return bodies, scans, sweeps, pulls_ok


_UNREAD = object()


async def _scan_list(
    row: Mapping[str, Any],
    repository: str,
    suffix: str,
    since: datetime,
    *,
    start_page: int,
    previous_sig: str = "",
    soft: float | None = None,
    hard: float | None = None,
    fetch: FetchRead | None,
    gh_run: GhRun | None,
    session: Any,
) -> _Scan:
    """Read newest-first pages until one is short or reaches ``since``.

    One page of 20 is not the window: 25 issues opened in a minute left five
    on page 2 while the cursor moved past them. New activity only pushes
    rows down the list, so resuming at a later page on the next poll
    re-reads some rows (idempotent) but never skips one. A read cut off by
    the budget leaves the continuation at that page.
    """
    items: list[Mapping[str, Any]] = []
    page = max(1, min(int(start_page), _MAX_PAGE_NUMBER))
    previous = previous_sig if page > 1 else ""
    last = previous
    for count in range(_MAX_LIST_PAGES):
        if count and soft is not None and time.monotonic() >= soft:
            return _Scan(items, page, signature=last)
        try:
            body = await _timed(
                _read(
                    row,
                    repository,
                    suffix,
                    _list_params(suffix, since, page=page),
                    fetch=fetch,
                    gh_run=gh_run,
                    session=session,
                ),
                hard,
            )
        except _OutOfTime:
            return _Scan(items, page, signature=last)
        batch = _as_list(body)
        signature = _page_signature(batch)
        if previous and signature == previous:
            # A server that ignores ``page`` repeats itself. End the scan, but
            # hold the cursor at the oldest row it showed: anything below it
            # was never read.
            return _Scan(
                items,
                0,
                floor=_page_floor(batch, suffix),
                signature=signature,
                last_page=page,
            )
        items.extend(batch)
        if len(batch) < _PAGE or page >= _MAX_PAGE_NUMBER:
            return _Scan(items, 0, signature=signature, last_page=page, exhausted=True)
        if _page_reaches_since(suffix, batch, since):
            return _Scan(items, 0, signature=signature, last_page=page)
        previous = signature
        last = signature
        page += 1
    return _Scan(items, page, signature=last, last_page=page - 1)


async def _sweep_list(
    row: Mapping[str, Any],
    repository: str,
    suffix: str,
    *,
    entry: Mapping[str, Any],
    since: datetime,
    moment: datetime,
    main: _Scan | None,
    soft: float | None,
    hard: float | None,
    fetch: FetchRead | None,
    gh_run: GhRun | None,
    session: Any,
) -> tuple[list[Mapping[str, Any]], dict[str, Any], datetime]:
    """Walk the whole list a few pages per poll for rows that changed late.

    Runs are ordered by creation and releases by ``created_at``, but a re-run
    of an old workflow changes ``updated_at`` and a draft published today
    keeps its old ``created_at``. Such a row sits behind idle rows, where the
    windowed scan stops. The sweep walks down with its own continuation and
    reports rows whose sweep stamp is at or after the floor: the moment the
    previous sweep cycle started. Rows only move down, so a row changed at
    time ``u`` is visited by the first cycle that starts after ``u``, and
    that cycle's floor is at or before ``u``. It never holds the cursor.
    Returns the candidate rows, the entry to save, and the floor.
    """
    page = _int_or(entry.get("page"), 0)
    candidates: list[Mapping[str, Any]] = []
    if page <= 0:
        floor = _parse_time(entry.get("floor")) or since
        started = moment
        page = 1
        previous = ""
        if main is not None and main.last_page >= 1:
            # The windowed scan read pages 1..k from the top just now.
            candidates.extend(main.items)
            if main.exhausted:
                return candidates, _sweep_done(started), floor
            page = main.last_page + 1
            previous = main.signature
    else:
        floor = _parse_time(entry.get("floor")) or since
        started = _parse_time(entry.get("started_at")) or moment
        previous = str(entry.get("sig") or "")
    page = min(page, _MAX_PAGE_NUMBER)
    for count in range(_MAX_SWEEP_PAGES):
        # The first page is read even past the soft budget (N69): the
        # windowed scan runs first, and on a slow GitHub it alone can use the
        # budget up, so the sweep would never move. The hard deadline still
        # bounds that page.
        if count and soft is not None and time.monotonic() >= soft:
            break
        try:
            body = await _timed(
                _read(
                    row,
                    repository,
                    suffix,
                    _list_params(suffix, since, page=page),
                    fetch=fetch,
                    gh_run=gh_run,
                    session=session,
                ),
                hard,
            )
        except _OutOfTime:
            break
        batch = _as_list(body)
        signature = _page_signature(batch)
        if previous and signature == previous:
            return candidates, _sweep_done(started), floor
        candidates.extend(batch)
        if len(batch) < _PAGE or page >= _MAX_PAGE_NUMBER:
            return candidates, _sweep_done(started), floor
        if suffix == "actions/runs" and all(
            (_parse_time(item.get("created_at")) or moment) < moment - _RUN_RERUN_HORIZON
            for item in batch
        ):
            # GitHub only re-runs recent workflow runs.
            return candidates, _sweep_done(started), floor
        previous = signature
        page += 1
    return (
        candidates,
        {
            "page": page,
            "started_at": _stamp(started),
            "floor": _stamp(floor),
            "sig": previous,
        },
        floor,
    )


def _sweep_done(started: datetime) -> dict[str, Any]:
    # The next cycle reports rows changed since this one started.
    return {"page": 0, "started_at": "", "floor": _stamp(started - _OVERLAP), "sig": ""}


def _sweeps(state: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw = state.get("waker_sweep")
    if not isinstance(raw, Mapping):
        return {}
    return {
        suffix: raw[suffix]
        for suffix in _SWEPT_LISTS
        if isinstance(raw.get(suffix), Mapping)
    }


def _int_or(value: object, default: int) -> int:
    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _page_reaches_since(
    suffix: str, batch: list[Mapping[str, Any]], since: datetime
) -> bool:
    """Whether later pages can only hold rows older than the window.

    Issues and releases are ordered by the stamp ``_item_is_due`` reads, so
    one old row ends the scan; a full page of old releases is not "more to
    read". Runs are ordered by creation while due-ness is ``updated_at``, so
    a page with something still due keeps going; rows deeper than an idle
    page are the sweep's job.
    """
    due = [_item_is_due(item, since) for item in batch]
    if suffix == "actions/runs":
        return not any(due)
    return not all(due)


def _page_signature(batch: list[Mapping[str, Any]]) -> str:
    rows = [
        [
            str(item.get("id") or item.get("number") or ""),
            str(item.get("updated_at") or item.get("created_at") or ""),
        ]
        for item in batch
    ]
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()[:24]


def _page_floor(batch: list[Mapping[str, Any]], suffix: str) -> datetime | None:
    key = _ORDER_KEYS.get(suffix, "updated_at")
    stamps = [stamp for stamp in (_parse_time(item.get(key)) for item in batch) if stamp]
    return min(stamps) if stamps else None


def _list_scans(
    state: Mapping[str, Any],
) -> dict[str, tuple[int, datetime | None, str]]:
    """Saved list continuations: next page, when it left page 1, last page seen."""
    raw = state.get("waker_list_scan")
    if not isinstance(raw, Mapping):
        return {}
    scans: dict[str, tuple[int, datetime | None, str]] = {}
    for suffix in _SCANNED_LISTS:
        entry = raw.get(suffix)
        if not isinstance(entry, Mapping):
            continue
        number = _int_or(entry.get("page"), 0)
        if number <= 1:
            continue
        scans[suffix] = (
            min(number, _MAX_PAGE_NUMBER),
            _parse_time(entry.get("started_at")),
            str(entry.get("sig") or ""),
        )
    return scans


def _list_scan_progress(
    saved: Mapping[str, tuple[int, datetime | None, str]],
    scans: Mapping[str, _Scan],
    since: datetime,
    moment: datetime,
) -> tuple[dict[str, dict[str, Any]], datetime | None, list[datetime]]:
    """Continuations to save, the earliest scan start, and repeat floors.

    A scan that read page 1 on this poll started now. One resumed from an
    earlier poll started when it read page 1 then: a row updated after that
    moment jumped back to page 1 and is only covered by the next window, so
    the cursor may not pass it.
    """
    pending: dict[str, dict[str, Any]] = {}
    starts: list[datetime] = []
    floors: list[datetime] = []
    for suffix in _SCANNED_LISTS:
        page, started, _sig = saved.get(suffix, (1, None, ""))
        if page <= 1:
            started = moment
        elif started is None:
            started = since + _OVERLAP
        scan = scans.get(suffix)
        if scan is None:
            continue
        if scan.next_page:
            pending[suffix] = {
                "page": scan.next_page,
                "started_at": _stamp(started),
                "sig": scan.signature,
            }
            continue
        starts.append(started)
        if scan.floor is not None:
            floors.append(scan.floor)
    if pending:
        return pending, None, floors
    return pending, min(starts) if starts else None, floors


def _review_pages(state: Mapping[str, Any]) -> dict[int, int]:
    raw = state.get("waker_review_pages")
    if not isinstance(raw, Mapping):
        return {}
    pages: dict[int, int] = {}
    for key, value in list(raw.items())[:_PAGE]:
        number = _pull_number({"number": key})
        page = _int_or(value, 0)
        if number is None or page < 1:
            continue
        pages[number] = min(page, _MAX_PAGE_NUMBER)
    return pages


def _events_from_lists(
    binding: RepoBinding, bodies: Mapping[str, object], since: datetime
) -> list[WakeEvent]:
    events: list[WakeEvent] = []
    runs = bodies.get("actions/runs")
    run_items = runs.get("workflow_runs") if isinstance(runs, Mapping) else []
    if isinstance(run_items, list):
        for run in run_items:
            if isinstance(run, Mapping):
                events.extend(
                    _project(
                        binding,
                        {"repository": binding.repository, "workflow_run": run},
                        since,
                    )
                )
    for issue in _as_list(bodies.get("issues")):
        # The issues list has no webhook action. An issue created inside
        # this window is an open; an older issue that was only updated is not.
        body: dict[str, Any] = {"repository": binding.repository, "issue": issue}
        created = _parse_time(issue.get("created_at"))
        if created is not None and created >= since:
            body["action"] = "opened"
        events.extend(_project(binding, body, since))
    for pull in _as_list(bodies.get("pulls"))[:_PAGE]:
        # ``updated`` plus the head sha keeps two pushes from sharing a key.
        events.extend(
            _project(
                binding,
                {
                    "repository": binding.repository,
                    "action": "updated",
                    "pull_request": pull,
                },
                since,
            )
        )
    for release in _as_list(bodies.get("releases")):
        action = "published" if release.get("published_at") else ""
        body: dict[str, Any] = {"repository": binding.repository, "release": release}
        if action:
            body["action"] = action
        events.extend(_project(binding, body, since))
    return events


async def _pull_details(
    row: Mapping[str, Any],
    binding: RepoBinding,
    since: datetime,
    *,
    numbers: list[int],
    review_pages: Mapping[int, int] | None = None,
    deadline: float | None = None,
    fetch: FetchRead | None,
    gh_run: GhRun | None,
    session: Any,
) -> tuple[list[WakeEvent], dict[int, int]]:
    """Review and reviewer wakes, plus where to resume each unfinished PR.

    A PR is unfinished when its review pages ran past the page cap or a read
    was cut off by the budget; the value is the review page to start at.
    """
    chosen = [number for number in numbers if isinstance(number, int) and number > 0]
    chosen = chosen[:_MAX_PULL_DETAILS]
    if not chosen:
        return [], {}
    pages = dict(review_pages or {})

    async def reviewers(number: int) -> object:
        try:
            return await _timed(
                _read(
                    row,
                    binding.repository,
                    f"pulls/{number}/requested_reviewers",
                    {"per_page": str(_PAGE)},
                    fetch=fetch,
                    gh_run=gh_run,
                    session=session,
                ),
                deadline,
            )
        except _OutOfTime:
            return _UNREAD

    reads: list[Awaitable[object]] = []
    for number in chosen:
        reads.append(
            _read_reviews(
                row,
                binding.repository,
                number,
                start_page=pages.get(number, 1),
                deadline=deadline,
                fetch=fetch,
                gh_run=gh_run,
                session=session,
            )
        )
        reads.append(reviewers(number))
    results = await asyncio.gather(*reads, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    events: list[WakeEvent] = []
    unread: dict[int, int] = {}
    for index, number in enumerate(chosen):
        reviews, next_page, last_page = results[2 * index]  # type: ignore[misc]
        listed = results[2 * index + 1]
        if next_page:
            unread[number] = next_page
        elif listed is _UNREAD:
            # Reviews done; only the reviewer read is owed. Resume on the
            # last review page so the retry re-reads one short page.
            unread[number] = max(1, last_page)
        for review in reviews:
            events.extend(
                _project(
                    binding,
                    {
                        "repository": binding.repository,
                        "action": "submitted",
                        "review": review,
                        "pull_request": {"number": number},
                    },
                    since,
                )
            )
        users = listed.get("users") if isinstance(listed, Mapping) else []
        if not isinstance(users, list):
            continue
        for user in users[:_PAGE]:
            if not isinstance(user, Mapping):
                continue
            events.extend(
                _project(
                    binding,
                    {
                        "repository": binding.repository,
                        "action": "review_requested",
                        "pull_request": {"number": number},
                        "requested_reviewer": user,
                    },
                    since,
                )
            )
    return events, unread


async def _read_reviews(
    row: Mapping[str, Any],
    repository: str,
    number: int,
    *,
    start_page: int,
    deadline: float | None,
    fetch: FetchRead | None,
    gh_run: GhRun | None,
    session: Any,
) -> tuple[list[Mapping[str, Any]], int, int]:
    """Review pages of one PR: items, next page (0 when done), last page read.

    GitHub lists reviews oldest first, so the newest ``changes_requested``
    sits on the last page; one page of 20 never saw it on a busy PR. Pages
    only grow at the end, so resuming at a saved page skips nothing.
    """
    items: list[Mapping[str, Any]] = []
    page = max(1, min(int(start_page), _MAX_PAGE_NUMBER))
    previous = ""
    last = 0
    for _ in range(_MAX_REVIEW_PAGES):
        try:
            body = await _timed(
                _read(
                    row,
                    repository,
                    f"pulls/{number}/reviews",
                    {"per_page": str(_REVIEW_PAGE), "page": str(page)},
                    fetch=fetch,
                    gh_run=gh_run,
                    session=session,
                ),
                deadline,
            )
        except _OutOfTime:
            return items, page, last
        batch = _as_list(body)
        signature = _page_signature(batch)
        if previous and signature == previous:
            return items, 0, last
        items.extend(batch)
        last = page
        if len(batch) < _REVIEW_PAGE or page >= _MAX_PAGE_NUMBER:
            return items, 0, last
        previous = signature
        page += 1
    return items, page, last


def _project(
    binding: RepoBinding, raw: Mapping[str, Any], since: datetime
) -> list[WakeEvent]:
    event = repository_event(raw, binding=binding)
    if event is None or not _event_is_due(event, since):
        return []
    return [event]


def _kept(events: list[WakeEvent], since: datetime) -> list[WakeEvent]:
    return [event for event in events if _event_is_due(event, since)]


def _event_is_due(event: WakeEvent, since: datetime) -> bool:
    moment = _parse_time(event.observed_at or event.payload.get("updated_at"))
    if moment is None:
        return True
    return moment >= since


def _item_is_due(item: Mapping[str, Any], since: datetime) -> bool:
    for key in ("updated_at", "created_at", "published_at", "submitted_at"):
        moment = _parse_time(item.get(key))
        if moment is not None:
            return moment >= since
    return True


def _as_list(body: object) -> list[Mapping[str, Any]]:
    if isinstance(body, list):
        return [item for item in body if isinstance(item, Mapping)]
    if isinstance(body, Mapping):
        for key in ("items", "workflow_runs"):
            raw = body.get(key)
            if isinstance(raw, list):
                return [item for item in raw if isinstance(item, Mapping)]
    return []


def _pull_number(pull: Mapping[str, Any]) -> int | None:
    raw = pull.get("number")
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        number = int(raw)
    except (TypeError, ValueError):
        return None
    if number <= 0 or number > 1_000_000_000:
        return None
    return number


def _pull_ids(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    numbers: list[int] = []
    for item in value[:_PAGE]:
        if isinstance(item, Mapping):
            number = _pull_number(item)
        else:
            number = _pull_number({"number": item})
        if number is None or number in numbers:
            continue
        numbers.append(number)
    return numbers


def _pull_version(pull: Mapping[str, Any]) -> str:
    for key in ("updated_at", "created_at"):
        stamp = str(pull.get(key) or "").strip()
        if stamp:
            return stamp
    return ""


def _pull_done_versions(value: object) -> dict[int, str]:
    """PR number → last observed ``updated_at``. Bare numbers mean unknown."""
    if not isinstance(value, list):
        return {}
    versions: dict[int, str] = {}
    for item in value[:_PAGE]:
        if isinstance(item, Mapping):
            number = _pull_number(item)
            if number is None:
                continue
            versions[number] = str(item.get("updated_at") or "")
            continue
        number = _pull_number({"number": item})
        if number is not None and number not in versions:
            versions[number] = ""
    return versions


def _pull_done_marks(
    numbers: list[int], listed: Mapping[int, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    marks: list[dict[str, Any]] = []
    seen: set[int] = set()
    for number in numbers:
        if number in seen or number not in listed:
            continue
        seen.add(number)
        marks.append(
            {"number": number, "updated_at": _pull_version(listed[number])}
        )
        if len(marks) >= _PAGE:
            break
    return marks


def _pull_visit_plan(
    pulls_body: object, since: datetime, state: Mapping[str, Any]
) -> tuple[list[int], list[int], list[int]]:
    """Next review reads, the PRs still waiting, and the ones already read.

    Only ``_MAX_PULL_DETAILS`` pulls get a review read on one poll. The rest
    stay pending so the shared cursor cannot step past them. A done PR whose
    list ``updated_at`` moved is queued again so a review that landed while
    the backlog drained is not skipped when the cursor finally advances.
    """
    listed: dict[int, Mapping[str, Any]] = {}
    order: list[int] = []
    for pull in _as_list(pulls_body):
        number = _pull_number(pull)
        if number is None or number in listed:
            continue
        listed[number] = pull
        order.append(number)
    done_versions = {
        number: version
        for number, version in _pull_done_versions(state.get("waker_pull_done")).items()
        if number in listed
    }
    pending = [
        number
        for number in _pull_ids(state.get("waker_pull_pending"))
        if number in listed and number not in done_versions
    ]
    pending_set = set(pending)
    revisit: list[int] = []
    # Newer observations on already-visited PRs are re-read, but never take
    # every detail slot: at least one slot stays for never-visited pending
    # PRs so a chatty trio cannot starve the backlog indefinitely.
    for number in order:
        if number not in done_versions:
            continue
        current = _pull_version(listed[number])
        if current and current <= done_versions[number]:
            continue
        revisit.append(number)
    revisit_cap = (
        max(0, _MAX_PULL_DETAILS - 1) if pending else _MAX_PULL_DETAILS
    )
    queue: list[int] = list(revisit[:revisit_cap])
    deferred_revisits = revisit[revisit_cap:]
    for number in pending:
        if number not in queue:
            queue.append(number)
    for number in order:
        if number in done_versions or number in pending_set or number in queue:
            continue
        if not _item_is_due(listed[number], since):
            continue
        queue.append(number)
    # Deferred revisits stay behind pending so progress is guaranteed, but
    # they remain in the continuation instead of being dropped.
    for number in deferred_revisits:
        if number not in queue:
            queue.append(number)
    visit = queue[:_MAX_PULL_DETAILS]
    rest = queue[_MAX_PULL_DETAILS:]
    queued = set(queue)
    done = [
        number
        for number in done_versions
        if number in listed and number not in queued
    ]
    return visit, rest, done


def _poll_state(
    bodies: Mapping[str, object],
    since: datetime,
    moment: datetime,
    interval: int,
    *,
    visit: list[int],
    rest: list[int],
    done: list[int],
    pulls_page: int = 1,
    scan_started: datetime | None = None,
    list_scans: Mapping[str, Mapping[str, Any]] | None = None,
    lists_started: datetime | None = None,
    review_pages: Mapping[int, int] | None = None,
    pulls_ok: bool = True,
    pulls_repeat: bool = False,
    pulls_sig: str = "",
    previous_sig: str = "",
    floors: list[datetime] | None = None,
) -> dict[str, Any]:
    """Cursor plus unread-PR and list-page continuation.

    Runs, issues and releases carry their own page continuation
    (``list_scans``). The shared cursor moves only on a poll where every
    list, the pulls scan and the pending review reads are all done, and then
    no further than the earliest moment any of those scans read page 1, nor
    past the oldest row of a page a server repeated (``floors``).

    The pulls list is sorted by ``updated_at`` descending with no ``since``
    filter, so it is the whole PR history. A page implies another due page
    only when it is full and every PR on it is still inside the window; a
    page that reaches an older PR ends the scan. Waiting for a short page
    instead walks the whole history, and since page 1 of any repo with 20+
    PRs is always full, a "rescan from page 1" rule loops forever without
    ever moving the cursor. A page identical to the one read before it
    (a server ignoring ``page``) also ends the scan.

    While a scan spans pages the shared cursor stays at ``since``. When it
    ends, the cursor moves to when the scan left page 1, not to this poll's
    time: a PR updated while later pages drained jumped back to page 1 with
    an ``updated_at`` after that moment, so the next scan still sees it as
    due. Rows only shift down during a scan (a re-read), never up past the
    reader. Done marks carry the list ``updated_at`` so a later review on a
    visited PR re-enters the queue before the window moves.

    When the pulls page was not read (budget), the pulls keys are left out
    so the stored continuation stands, and the cursor stays.
    """
    common = {
        "waker_next_poll_at": _stamp(moment + timedelta(seconds=interval)),
        "waker_list_scan": {k: dict(v) for k, v in (list_scans or {}).items()},
    }
    if not pulls_ok:
        return {"waker_cursor": _stamp(since), **common}
    listed: dict[int, Mapping[str, Any]] = {}
    for pull in _as_list(bodies.get("pulls")):
        number = _pull_number(pull)
        if number is not None and number not in listed:
            listed[number] = pull
    more_pages = (
        not pulls_repeat
        and len(listed) >= _PAGE
        and all(_item_is_due(pull, since) for pull in listed.values())
    )
    page = max(1, int(pulls_page))
    started = scan_started
    if page > 1 and started is None:
        # A mid-scan row written before this mark existed: fall back to the
        # window start, which never moves the cursor past an unread update.
        started = since + _OVERLAP
    signature = ""
    if rest:
        cursor = since
        next_page = page
        # Still draining this page: keep the mark of the page before it, or
        # the next re-read of this page would look like a repeat.
        signature = previous_sig if page > 1 else ""
        # Visit first so a re-read PR records its newer updated_at.
        seen: list[int] = []
        for number in [*visit, *done]:
            if number not in seen:
                seen.append(number)
        pending = list(rest)
        visited = _pull_done_marks(seen, listed)
    elif more_pages:
        # More due list pages remain. Do not advance the shared cursor past them.
        cursor = since
        next_page = page + 1
        pending = []
        visited = []
        signature = pulls_sig
        if page == 1:
            # Page 1 is fully read as of this poll; later pages inherit it.
            started = moment
    elif list_scans:
        # Pulls are done but a list still has unread due pages. Keep the
        # window; the done marks stop the next pulls pass re-reading reviews.
        cursor = since
        next_page = 1
        pending = []
        seen = []
        for number in [*visit, *done]:
            if number not in seen:
                seen.append(number)
        visited = _pull_done_marks(seen, listed)
        started = None
    else:
        cursor = moment - _OVERLAP
        for bound in (started, lists_started):
            if bound is not None:
                cursor = min(cursor, bound - _OVERLAP)
        for floor in floors or []:
            cursor = min(cursor, floor)
        cursor = max(since, cursor)
        next_page = 1
        pending = []
        visited = []
        started = None
    pending = pending[:_PAGE]
    reviews = {
        str(number): value
        for number, value in (review_pages or {}).items()
        if number in pending
    }
    return {
        "waker_cursor": _stamp(cursor),
        **common,
        "waker_pull_pending": pending,
        "waker_pull_done": visited,
        "waker_pulls_page": next_page,
        "waker_scan_started_at": _stamp(started) if started is not None else "",
        "waker_pulls_sig": signature,
        "waker_review_pages": reviews,
    }


def _scan_started(state: Mapping[str, Any]) -> datetime | None:
    return _parse_time(state.get("waker_scan_started_at"))


async def _read(
    row: Mapping[str, Any],
    repository: str,
    suffix: str,
    params: Mapping[str, str],
    *,
    fetch: FetchRead | None,
    gh_run: GhRun | None,
    session: Any,
) -> object:
    path = f"/repos/{repository}/{suffix}"
    if not _waker_read_path(repository, path):
        raise RepoBarPollError("GitHub read is not allowed")
    clean = _query(params)
    if fetch is not None:
        return await fetch(row, path, clean)
    # List polls use ``gh api`` (or an injected fetch). Widening the GitHub
    # credential-broker allow set is outside this line's file set.
    return await _gh_get(repository, suffix, clean, gh_run=gh_run)


def _query(params: Mapping[str, str]) -> dict[str, str]:
    clean: dict[str, str] = {}
    for key, value in params.items():
        if not _GH_FIELD.fullmatch(str(key)) or not _GH_VALUE.fullmatch(str(value)):
            raise RepoBarPollError("GitHub read is not allowed")
        clean[str(key)] = str(value)
    return clean



async def _gh_get(
    repository: str,
    suffix: str,
    params: Mapping[str, str],
    *,
    gh_run: GhRun | None,
) -> object:
    argv = ["gh", "api", "--method", "GET", f"repos/{repository}/{suffix}"]
    for key, value in params.items():
        argv.extend(["--raw-field", f"{key}={value}"])
    if "--paginate" in argv:
        raise RepoBarPollError("GitHub read is not allowed")
    runner = gh_run or _default_gh_run
    try:
        code, stdout, stderr = await runner(argv)
    except RepoBarPollError:
        raise
    except Exception:  # noqa: BLE001 - the argv and stderr stay off this message
        raise RepoBarPollError("gh api failed") from None
    if code != 0:
        logger.error(
            "[autonomy] gh api failed: %s",
            redact_external_text(stderr.decode("utf-8", errors="replace"))[:500],
        )
        raise RepoBarPollError("gh api failed")
    try:
        return json.loads(stdout.decode("utf-8") or "null")
    except (UnicodeError, json.JSONDecodeError):
        raise RepoBarPollError("gh api failed") from None


async def _default_gh_run(argv: list[str]) -> tuple[int, bytes, bytes]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise RepoBarPollError("gh api failed") from None
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_GH_TIMEOUT_SECONDS
        )
    except BaseException as exc:
        # Its own timeout, or a cancel from the poll or source deadline
        # above it (N69): either way the gh process must not outlive the
        # read. The wait is shielded so a second cancel cannot skip it.
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.shield(proc.wait())
        if isinstance(exc, TimeoutError):
            raise RepoBarPollError("gh api failed") from None
        raise
    return proc.returncode or 0, stdout or b"", stderr or b""
