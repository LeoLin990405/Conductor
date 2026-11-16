"""The N80 rollout mode table: which collab verbs the current stage allows.

``CONDUCTOR_COLLAB_MODE`` is one of ``off``, ``shadow``, ``docs`` or ``full``
(N80 section 9). The default is ``off``, and an unset, blank or unknown value
also resolves to ``off``: a typo must close the protocol, never open it.

The table is pure. It reads no database and no clock; the only environment it
reads is the mode variable (and, for the docs stage, the appended high-risk
globs), and only when the caller does not pass a mapping. Every HTTP route,
CLI verb and MCP tool asks :func:`decide` before it reaches a service, and the
lease sweep asks :func:`lease_allowed` after a mode change.

=========  ===========================================  =====================
mode       scopes the stage allows                      task limit
=========  ===========================================  =====================
off        none: every verb is ``mode_denied``          -
shadow     ``read``, ``propose``                        -
docs       adds ``claim``, ``report``, ``ask``,         docs/tests tasks only
           ``review``
full       every scope                                  any ``app_claimable``
                                                        Ticket
=========  ===========================================  =====================

The mode never grants a scope. An app still needs the scope in its own
registration (:mod:`src.collab.scopes`); the mode only takes scopes away.

Docs stage (section 5.2 step 3): a task qualifies only when every
``write_scope`` entry stays inside ``docs/``, ``tests/`` or a Markdown file,
no entry hits ``HIGH_RISK_GLOBS`` (including the patterns
``CONDUCTOR_DISPATCH_HIGH_RISK_GLOBS`` appends), and the task is not
``risk_bearing``. An empty write scope does not qualify: it cannot be shown to
stay inside the docs area, so the check fails closed.

Dispatch lines (``d:`` references) are not claimable in any mode until L4
lands; :data:`LINE_CLAIMS_ENABLED` is the switch L4 flips.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from src.collab.scopes import SCOPES, TOOL_SCOPES
from src.collab.types import ModeDeniedError, NotClaimableError

#: The environment variable that selects the rollout stage.
MODE_ENV = "CONDUCTOR_COLLAB_MODE"

OFF = "off"
SHADOW = "shadow"
DOCS = "docs"
FULL = "full"

#: Rollout order. Rolling back is choosing an earlier entry; no code changes.
MODES: tuple[str, ...] = (OFF, SHADOW, DOCS, FULL)
DEFAULT_MODE = OFF

#: Scopes each stage permits. ``off`` permits nothing, not even ``read``.
MODE_SCOPES: dict[str, frozenset[str]] = {
    OFF: frozenset(),
    SHADOW: frozenset({"read", "propose"}),
    DOCS: frozenset({"read", "propose", "claim", "report", "ask", "review"}),
    FULL: frozenset(SCOPES),
}

#: Scopes whose verbs act on a task and are therefore docs-limited in ``docs``.
TASK_BOUND_SCOPES = frozenset({"claim", "report", "review"})

#: Verbs that only give work back. They stay open whenever the stage allows
#: their scope, so a downgrade to ``docs`` can still release a full-stage
#: lease instead of leaving it to expire.
RELEASE_TOOLS = frozenset({"task_release"})

#: Write-scope roots the docs stage accepts, and the Markdown suffix.
DOCS_ROOTS: tuple[str, ...] = ("docs/", "tests/")
DOCS_SUFFIX = ".md"

#: L4 turns this on once ``Line.app_claimable`` exists in the machine.
LINE_CLAIMS_ENABLED = False

#: Refusal reasons, carried in ``ModeDecision.reason`` and error details.
REASON_OK = "ok"
REASON_MODE_OFF = "mode_off"
REASON_SCOPE_NOT_IN_MODE = "scope_not_in_mode"
REASON_UNKNOWN_TOOL = "unknown_tool"
REASON_TASK_REQUIRED = "task_facts_required"
REASON_NOT_DOCS_TASK = "not_docs_task"
REASON_NOT_APP_CLAIMABLE = "not_app_claimable"
REASON_LINES_DISABLED = "line_claims_not_enabled"


def resolve_mode(
    value: str | None = None, *, environ: Mapping[str, str] | None = None
) -> str:
    """The effective stage: ``value`` if given, else ``CONDUCTOR_COLLAB_MODE``.

    Case and surrounding space are ignored. Anything that is not one of
    :data:`MODES` (unset, blank, a typo) resolves to ``off``.
    """
    if value is None:
        source = os.environ if environ is None else environ
        value = source.get(MODE_ENV)
    text = str(value or "").strip().lower()
    return text if text in MODE_SCOPES else DEFAULT_MODE


def current_mode(*, environ: Mapping[str, str] | None = None) -> str:
    """Shorthand for :func:`resolve_mode` with only the environment."""
    return resolve_mode(None, environ=environ)


def mode_scopes(mode: str) -> frozenset[str]:
    """Scopes ``mode`` allows; an unknown mode allows none (it is ``off``)."""
    return MODE_SCOPES[resolve_mode(mode or OFF)]


def mode_rank(mode: str) -> int:
    """Position in :data:`MODES`, for "is this a downgrade" comparisons."""
    return MODES.index(resolve_mode(mode or OFF))


@dataclass(frozen=True)
class TaskFacts:
    """What the mode table needs to know about the task a verb touches.

    The service layer builds this from the Ticket (``mandate.collab``) or the
    dispatch line; the table never loads it itself.
    """

    kind: str = "ticket"
    app_claimable: bool = False
    write_scope: tuple[str, ...] = ()
    risk_bearing: bool = False

    def __post_init__(self) -> None:
        if self.kind not in {"ticket", "line"}:
            raise ValueError("task kind must be 'ticket' or 'line'")
        scope = self.write_scope
        if isinstance(scope, str):
            raise TypeError("write_scope must be a sequence of paths, not a string")
        object.__setattr__(self, "write_scope", tuple(str(p) for p in scope))
        object.__setattr__(self, "app_claimable", bool(self.app_claimable))
        object.__setattr__(self, "risk_bearing", bool(self.risk_bearing))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TaskFacts":
        """Build from a plain mapping; unknown keys are ignored."""
        scope = value.get("write_scope") or ()
        if isinstance(scope, str):
            scope = (scope,)
        return cls(
            kind=str(value.get("kind") or "ticket"),
            app_claimable=value.get("app_claimable") is True,
            write_scope=tuple(scope),
            risk_bearing=bool(value.get("risk_bearing")),
        )


@dataclass(frozen=True)
class ModeDecision:
    """The answer for one verb: allowed, or the code and reason to refuse."""

    mode: str
    tool: str
    allowed: bool
    code: str = "ok"
    reason: str = REASON_OK

    def raise_for_refusal(self) -> None:
        """Raise the protocol error for a refusal; do nothing when allowed."""
        if self.allowed:
            return
        details = {"mode": self.mode, "tool": self.tool, "reason": self.reason}
        message = f"{self.tool} is refused in collab mode {self.mode}: {self.reason}"
        if self.code == "not_claimable":
            raise NotClaimableError(message, details=details)
        raise ModeDeniedError(message, details=details)


def _normalize_scope_path(path: str) -> str | None:
    """A relative POSIX path, or ``None`` when it could leave the repository."""
    text = str(path or "").strip()
    if not text or "\\" in text or text.startswith("/") or text.startswith("~"):
        return None
    while text.startswith("./"):
        text = text[2:]
    parts = text.split("/")
    if any(part in {"", ".", ".."} for part in parts[:-1]) or parts[-1] in {".", ".."}:
        return None
    return text


def _inside_docs_area(path: str) -> bool:
    normalized = _normalize_scope_path(path)
    if normalized is None:
        return False
    if any(normalized.startswith(root) for root in DOCS_ROOTS):
        return True
    # A Markdown entry may be a glob (``src/**/*.md``) as long as everything it
    # can match ends in ``.md``; a trailing wildcard would not.
    if normalized.endswith(DOCS_SUFFIX):
        stem = normalized[: -len(DOCS_SUFFIX)]
        return bool(stem) and not stem.endswith("/")
    return False


def _high_risk(
    write_scope: Sequence[str],
    *,
    globs: Sequence[str] | None,
    environ: Mapping[str, str] | None,
) -> bool:
    # Imported here: the auto-dispatch package is heavy and only the docs-stage
    # claim path needs its glob matcher. The matcher itself is pure.
    from src.tools.auto_dispatch.auto_merge import high_risk_globs, is_high_risk

    patterns = tuple(globs) if globs is not None else high_risk_globs(environ=environ)
    return is_high_risk([], list(write_scope), False, globs=patterns)


def is_docs_task(
    task: TaskFacts,
    *,
    globs: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """True when the docs stage may hand ``task`` to an app (section 5.2.3).

    ``globs`` overrides the high-risk list; by default it is
    ``HIGH_RISK_GLOBS`` plus whatever ``CONDUCTOR_DISPATCH_HIGH_RISK_GLOBS``
    appends in ``environ`` (the process environment when ``None``).
    """
    if task.risk_bearing or not task.write_scope:
        return False
    if not all(_inside_docs_area(path) for path in task.write_scope):
        return False
    return not _high_risk(task.write_scope, globs=globs, environ=environ)


def _claimable(task: TaskFacts, lines_enabled: bool) -> str:
    if task.kind == "line" and not lines_enabled:
        return REASON_LINES_DISABLED
    if not task.app_claimable:
        return REASON_NOT_APP_CLAIMABLE
    return REASON_OK


def decide(
    tool: str,
    *,
    mode: str | None = None,
    task: TaskFacts | Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    globs: Sequence[str] | None = None,
    lines_enabled: bool | None = None,
) -> ModeDecision:
    """Whether the stage allows ``tool`` (on ``task``, when it names one).

    ``mode`` defaults to ``CONDUCTOR_COLLAB_MODE`` in ``environ``. Checks, in
    order, each refusing with ``mode_denied`` unless noted:

    1. ``off`` refuses everything (``mode_off``);
    2. an unknown tool is refused, never defaulted (``unknown_tool``);
    3. the tool's scope must be in the stage (``scope_not_in_mode``);
    4. ``task_claim`` needs task facts; a dispatch line is ``not_claimable``
       until L4 (``line_claims_not_enabled``) and so is a task without
       ``app_claimable`` (``not_app_claimable``), in every stage;
    5. in ``docs``, a claim/report/review verb needs task facts
       (``task_facts_required``) that pass :func:`is_docs_task`
       (``not_docs_task``). ``task_release`` is exempt so work can always be
       given back; ``agent_ask`` is checked only when it names a task.
    """
    resolved = resolve_mode(mode, environ=environ)
    name = str(tool)
    if resolved == OFF:
        return ModeDecision(resolved, name, False, "mode_denied", REASON_MODE_OFF)
    scope = TOOL_SCOPES.get(name)
    if scope is None:
        return ModeDecision(resolved, name, False, "mode_denied", REASON_UNKNOWN_TOOL)
    if scope not in MODE_SCOPES[resolved]:
        return ModeDecision(resolved, name, False, "mode_denied", REASON_SCOPE_NOT_IN_MODE)

    facts = TaskFacts.from_mapping(task) if isinstance(task, Mapping) else task
    enabled = LINE_CLAIMS_ENABLED if lines_enabled is None else bool(lines_enabled)
    if name == "task_claim":
        if facts is None:
            return ModeDecision(resolved, name, False, "mode_denied", REASON_TASK_REQUIRED)
        reason = _claimable(facts, enabled)
        if reason != REASON_OK:
            return ModeDecision(resolved, name, False, "not_claimable", reason)

    if resolved == DOCS and name not in RELEASE_TOOLS:
        task_bound = scope in TASK_BOUND_SCOPES
        if facts is None and task_bound:
            return ModeDecision(resolved, name, False, "mode_denied", REASON_TASK_REQUIRED)
        if facts is not None and (task_bound or scope == "ask"):
            if not is_docs_task(facts, globs=globs, environ=environ):
                return ModeDecision(resolved, name, False, "mode_denied", REASON_NOT_DOCS_TASK)
    return ModeDecision(resolved, name, True)


def require_mode(
    tool: str,
    *,
    mode: str | None = None,
    task: TaskFacts | Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    globs: Sequence[str] | None = None,
    lines_enabled: bool | None = None,
) -> ModeDecision:
    """:func:`decide`, raising ``ModeDeniedError`` / ``NotClaimableError``."""
    decision = decide(
        tool,
        mode=mode,
        task=task,
        environ=environ,
        globs=globs,
        lines_enabled=lines_enabled,
    )
    decision.raise_for_refusal()
    return decision


def mode_allows(tool: str, *, mode: str | None = None, **kwargs: Any) -> bool:
    """Boolean form of :func:`decide`."""
    return decide(tool, mode=mode, **kwargs).allowed


def lease_allowed(
    task: TaskFacts | Mapping[str, Any],
    *,
    mode: str | None = None,
    environ: Mapping[str, str] | None = None,
    globs: Sequence[str] | None = None,
    lines_enabled: bool | None = None,
) -> bool:
    """Whether an existing lease on ``task`` may stay live in ``mode``.

    The sweep expires a lease this returns False for: every lease in ``off``
    and ``shadow`` (no claim scope), a non-docs lease in ``docs``. Whether the
    task is still ``app_claimable`` is a claim-time question, not asked here.
    """
    return decide(
        "task_heartbeat",
        mode=mode,
        task=task,
        environ=environ,
        globs=globs,
        lines_enabled=lines_enabled,
    ).allowed


def allowed_tools(
    mode: str | None = None, *, environ: Mapping[str, str] | None = None
) -> tuple[str, ...]:
    """Tools whose scope the stage allows, before any task limit."""
    resolved = resolve_mode(mode, environ=environ)
    scopes = MODE_SCOPES[resolved]
    return tuple(sorted(tool for tool, scope in TOOL_SCOPES.items() if scope in scopes))


__all__ = [
    "DEFAULT_MODE",
    "DOCS",
    "DOCS_ROOTS",
    "DOCS_SUFFIX",
    "FULL",
    "LINE_CLAIMS_ENABLED",
    "MODES",
    "MODE_ENV",
    "MODE_SCOPES",
    "OFF",
    "REASON_LINES_DISABLED",
    "REASON_MODE_OFF",
    "REASON_NOT_APP_CLAIMABLE",
    "REASON_NOT_DOCS_TASK",
    "REASON_OK",
    "REASON_SCOPE_NOT_IN_MODE",
    "REASON_TASK_REQUIRED",
    "REASON_UNKNOWN_TOOL",
    "RELEASE_TOOLS",
    "SHADOW",
    "TASK_BOUND_SCOPES",
    "ModeDecision",
    "TaskFacts",
    "allowed_tools",
    "current_mode",
    "decide",
    "is_docs_task",
    "lease_allowed",
    "mode_allows",
    "mode_rank",
    "mode_scopes",
    "require_mode",
    "resolve_mode",
]
