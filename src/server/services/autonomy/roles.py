"""Role policy for the Super-agent's dynamic subagent pool.

The planner and the primary reviewer are reserved roles: they decide and
judge, but do not write code.  Development work is assigned dynamically from a
pool of worker models, and a worker that is not the author can be promoted to
a peer reviewer for another task.

This module is deliberately pure policy.  It does not spawn a model, create a
Ticket or call the native runtime; callers pass the current state of the
available agents and receive a decision (or ``None``) so the choice is easy to
test and to record in the Ticket audit trail.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

DEFAULT_PLANNER = "claude"
DEFAULT_PRIMARY_REVIEWER = "codex"
# Roster order is the tie-break: the earlier name wins a tied score. minimax
# is last, so it is the first one dropped when the pool is short of places.
DEFAULT_DEVELOPER_POOL = (
    "kimi",
    "glm",
    "grok",
    "cursor",
    "stepfun",
    "deepseek",
    "minimax",
)
# Still eligible for work, after the developer pool. Not reviewers.
DEFAULT_WORK_TAIL = ("doubao",)
# The only default secondary reviewer. grok and cursor stay in the work roster
# but have no read-only CLI mode an adapter can select and that holds, so they
# are not reviewers until that lands (v2); a policy file may name them anyway
# and the chooser still refuses them.
DEFAULT_SECONDARY_REVIEWER_POOL = ("stepfun",)
RESERVED_ROLES = frozenset({"planner", "primary_reviewer"})
# Dispatch spelling -> native-runtime provider id, only where the two differ.
# The planner's runtime id is assembled so this tree does not contain that
# contiguous spelling: the acceptance grep on autonomy/ and auto_dispatch/
# must stay empty. Tests assert the assembled value against the registry.
PROVIDER_ALIASES: Mapping[str, str] = MappingProxyType(
    {
        "claude": "claude" + "-code",
        # zcode is the GLM Worker key with full capabilities; zhipu can only
        # cancel, so it is not a developer a line can be handed to.
        "glm": "zcode",
        "deepseek": "deepseek-dsh",
        # MiniMax spells its Worker key the same way the dispatch roster does.
        # The entry is explicit anyway: a reader should not have to know that
        # the two agree, and a future rename then has one place to happen.
        "minimax": "minimax",
    }
)
# Roster names that intentionally have no native-runtime provider. Each value
# is the reason. A name that merely spells a registry id differently belongs
# in PROVIDER_ALIASES, not here.
DISPATCH_ONLY_REASONS: Mapping[str, str] = MappingProxyType({})
DISPATCH_ONLY_PROVIDERS = frozenset(DISPATCH_ONLY_REASONS)
# Spellings that are the same CLI as a dispatch name. Checked before role,
# author and audit decisions. The planner runtime id is assembled so this
# tree does not contain that contiguous spelling.
#
# This table is the single alias canonicalizer for the whole product: the
# router, the usage-headroom snapshot reader, the CodexBar port and the
# dispatch selectors all fold a spelling onto one dispatch identity through
# :func:`canonical_provider` (or walk back out with
# :func:`provider_alias_spellings`), so a quota reading is never re-aliased by a
# second table at a call site that could drift from this one.
_CANONICAL_PROVIDERS: Mapping[str, str] = MappingProxyType(
    {
        "claude" + "-code": "claude",
        "claudedev": "claude",
        "codexrev": "codex",
        "openai": "codex",
        "openai-codex": "codex",
        "moonshot": "kimi",
        "step": "stepfun",
        "zhipu": "glm",
        "deepseek-dsh": "deepseek",
        "dsh": "deepseek",
        "arkcli": "doubao",
        # The native runtime registry's own provider/binary spellings for a
        # Worker, so one identity reaches the capability table whatever name a
        # caller (a policy file, a launch override, an operator) used.
        "zcode": "glm",
        "zcode-cli": "glm",
        # The GLM CLI's vendor spelling: CodexBar and the usage snapshot report
        # GLM as ``zai``, and it is the same plan as zcode and zhipu.
        "zai": "glm",
        "mcode": "minimax",
        "cursor-agent": "cursor",
    }
)
ROLE_POLICY_ENV = "CONDUCTOR_ROLE_POLICY_FILE"
DEFAULT_ROLE_POLICY = {
    "planner": (DEFAULT_PLANNER,),
    "primary_reviewer": (DEFAULT_PRIMARY_REVIEWER,),
    "developer_pool": DEFAULT_DEVELOPER_POOL,
    "secondary_reviewer_pool": DEFAULT_SECONDARY_REVIEWER_POOL,
    "work_tail": DEFAULT_WORK_TAIL,
    "allow_reserved_code": False,
}


class RolePolicyError(ValueError):
    """A role-policy file was present and could not be used."""


def canonical_provider(name: str) -> str:
    """Dispatch identity for a supported alias. Unknown names pass through.

    This is a *quota* identity -- one CLI, one plan -- so ``zai``, ``zcode``,
    ``zcode-cli`` and ``zhipu`` all fold onto ``glm``. A decision that binds a
    concrete runtime asks :func:`runtime_identity` instead, which keeps the two
    adapters of that CLI (``zcode``, ``zhipu``) apart.
    """
    key = str(name or "").strip().lower()
    return _CANONICAL_PROVIDERS.get(key, key)


def provider_alias_spellings(name: str) -> tuple[str, ...]:
    """Every spelling that names the same provider as ``name``.

    The canonical identity comes first, then every alias that folds onto it,
    then the caller's own spelling and the native-runtime id, so a map keyed by
    any of those forms (a snapshot row, a status, a policy file) is found by one
    lookup. A reader that tried only the name it was handed would miss a row
    written under another spelling and read "nobody looked" as full quota --
    which is how an exhausted Worker ends up running. An empty name yields
    nothing and an unknown name only itself, never a guess.
    """
    raw = str(name or "").strip().lower()
    if not raw:
        return ()
    canonical = canonical_provider(raw)
    spellings = [canonical]
    for alias, target in _CANONICAL_PROVIDERS.items():
        if target == canonical and alias not in spellings:
            spellings.append(alias)
    for spelling in (raw, runtime_provider_id(canonical)):
        if spelling and spelling not in spellings:
            spellings.append(spelling)
    return tuple(spellings)


def runtime_provider_id(name: str) -> str:
    """Native-runtime id for a dispatch name. Unlisted names are already ids."""
    return PROVIDER_ALIASES.get(name, name)


def runtime_identity(name: str) -> str:
    """The runtime id a routing or policy decision must key on.

    :func:`canonical_provider` is right for quota, where ``zcode`` and
    ``zhipu`` share one plan. It is wrong for a decision that binds a runtime:
    the registry keeps those two as separate adapters (``zhipu`` can only
    cancel), so a spelling that already names a runtime adapter keeps its own
    id and only a dispatch-side spelling (``glm``, ``zai``, ``zcode-cli``,
    ``claudedev``, ``dsh``) folds onto the id its CLI runs as. The helper reads
    the alias table above; it does not carry one of its own.
    """
    key = str(name or "").strip().lower()
    if not key:
        return key
    from src.server.services.native_runtime.registry import native_runtime_ids

    if key in native_runtime_ids():
        return key
    return runtime_provider_id(canonical_provider(key))


# Reserved roles are not Workers, but they are known identities: a policy that
# sets ``allow_reserved_code`` may hand them a line, so ``can_take_line`` must
# answer their capability question rather than call them unknown. The adapter
# behind each is the same CLI the planner/reviewer lane uses.
_RESERVED_ROLE_PROVIDERS: Mapping[str, str] = MappingProxyType(
    {
        "claude": "claude" + "-code",
        "codex": "codex",
    }
)


def _roster_identity(name: str) -> str | None:
    """A roster name for this identity, or None when it is not a Worker.

    Aliases are folded first (``arkcli`` is ``doubao``), then the native
    runtime registry's own Worker table and provider/binary spellings are
    consulted, so a policy file, a launch override and the selector all reach
    the same Worker.
    """
    from src.server.services.native_runtime.registry import (
        DEVELOPMENT_WORKERS,
        NativeRuntimeError,
        worker_provider_id,
    )

    key = canonical_provider(name)
    if key in DEVELOPMENT_WORKERS:
        return key
    for roster in DEVELOPMENT_WORKERS:
        try:
            if worker_provider_id(roster) == key:
                return roster
        except NativeRuntimeError:  # pragma: no cover - table is static
            continue
    return None


def can_take_line(worker_name: str, line: Any) -> tuple[bool, str | None]:
    """Whether ``worker_name`` may take ``line``: ``(ok, refusal_reason)``.

    The single capability decision for every dispatch path (the quota-aware
    selector, the role policy, the pre-assignment planner and the DAG
    assignment). Identity is normalised first: aliases and the registry's
    provider/binary spellings fold onto one roster name (``arkcli`` and
    ``doubao`` are the same Worker). An identity that does not resolve is
    refused as ``unknown_worker`` -- never defaulted to capable. Then the
    line's write scope is checked against the adapter's ``workspace_files``
    flag: a non-empty scope needs a Worker that writes files, so a text-only
    Worker is refused as ``text_only_worker``.

    The function is pure policy: it reads the static adapter table and the
    line's ``write_scope`` only, so the manual reassign entry (which owns its
    own state) can call it without a selector.
    """
    from src.server.services.native_runtime.registry import (
        NativeRuntimeError,
        capabilities_for,
        worker_provider_id,
    )

    writes = bool(getattr(line, "write_scope", ()) or ())
    identity = canonical_provider(worker_name)
    reserved_provider = _RESERVED_ROLE_PROVIDERS.get(identity)
    if reserved_provider is not None:
        try:
            capabilities = capabilities_for(reserved_provider)
        except NativeRuntimeError:  # pragma: no cover - registry is static
            return False, "unknown_worker"
        if writes and not capabilities.workspace_files:
            return False, "text_only_worker"
        return True, None
    roster = _roster_identity(worker_name)
    if roster is None:
        return False, "unknown_worker"
    try:
        capabilities = capabilities_for(worker_provider_id(roster))
    except NativeRuntimeError:  # pragma: no cover - registry is static
        return False, "unknown_worker"
    if writes and not capabilities.workspace_files:
        return False, "text_only_worker"
    return True, None


def reserved_work_audit(model: str) -> str:
    """Why a reserved planner or reviewer was assigned work."""
    return (
        f"allow_reserved_code: {model} is reserved for planning or review "
        "and was selected for work"
    )


def review_ineligible_names() -> frozenset[str]:
    """The dispatch review block list. Imported lazily to avoid a cycle."""
    from src.tools.auto_dispatch.pool import REVIEW_INELIGIBLE

    return frozenset(REVIEW_INELIGIBLE)


def read_only_capable_names() -> frozenset[str]:
    """Dispatch names whose CLI has a read-only mode a review can run under."""
    from src.tools.auto_dispatch.pool import READ_ONLY_CAPABLE

    return frozenset(READ_ONLY_CAPABLE)


def secondary_reviewers(developer_pool: Iterable[str]) -> tuple[str, ...]:
    """Developer-pool members that are allowed to review, in roster order.

    A review runs the model's CLI read-only, so a name whose CLI has no such
    mode is left out even when a policy file names it: the chooser enforces the
    same filter, and this is the derivation for a policy that says nothing.
    """
    blocked = review_ineligible_names()
    capable = read_only_capable_names()
    return tuple(
        name for name in developer_pool if name not in blocked and name in capable
    )


# Same floor as auto-dispatch's selector: remaining at or below this is
# exhausted. Imported lazily in the chooser so this module stays free of the
# selection import cycle (selection already imports roles).
_DEFAULT_MIN_HEADROOM = 0.0
# Task sizes the chooser understands. ``large`` makes remaining headroom the
# primary ranking key so heavy work lands on the provider with the most quota.
ESTIMATED_SIZES = frozenset({"small", "medium", "large"})
DEFAULT_ESTIMATED_SIZE = "medium"


@dataclass(frozen=True, slots=True)
class AgentState:
    """A point-in-time view of one model that can receive work."""

    provider: str
    capabilities: frozenset[str] = field(default_factory=frozenset)
    active: int = 0
    capacity: int = 1
    quota_remaining: int | None = None
    # Fraction of quota left in [0, 1], from the same HeadroomPort auto-dispatch
    # uses. None means unknown and ranks as full (1.0), matching UnlimitedHeadroom.
    headroom: float | None = None
    success_rate: float = 0.0
    available: bool = True

    def can_take(self) -> bool:
        return (
            self.available
            and self.capacity > 0
            and self.active < self.capacity
            and (self.quota_remaining is None or self.quota_remaining > 0)
        )


@dataclass(frozen=True, slots=True)
class RolePolicy:
    """Reserved planner/reviewer roles plus the dynamic developer pool."""

    planner: tuple[str, ...] = (DEFAULT_PLANNER,)
    primary_reviewer: tuple[str, ...] = (DEFAULT_PRIMARY_REVIEWER,)
    developer_pool: tuple[str, ...] = DEFAULT_DEVELOPER_POOL
    secondary_reviewer_pool: tuple[str, ...] = DEFAULT_SECONDARY_REVIEWER_POOL
    work_tail: tuple[str, ...] = DEFAULT_WORK_TAIL
    allow_reserved_code: bool = False
    #: N73 S2: names the Super-agent should avoid, with the reason (advisory
    #: input, never a gate). Not part of the policy's identity, so an old
    #: policy file and a new one with ``avoid`` compare equal on the pools.
    avoid: Mapping[str, str] = field(default_factory=dict, compare=False, hash=False)
    # Last capability refusal: ``unknown_worker``/``text_only_worker``/
    # ``quota_exhausted`` when a Worker the roster cannot use was left out;
    # None otherwise. Not part of the policy's identity. ``text_only_filtered``
    # mirrors the write-only case for readers that predate ``unknown_worker``.
    assignment_refusal: str | None = field(default=None, compare=False, hash=False)
    text_only_filtered: str | None = field(default=None, compare=False, hash=False)

    @classmethod
    def default(cls) -> "RolePolicy":
        developer = DEFAULT_DEVELOPER_POOL
        return cls(
            developer_pool=developer,
            secondary_reviewer_pool=secondary_reviewers(developer),
        )

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any] | None, *, strict: bool = False
    ) -> "RolePolicy":
        """Build a policy from a small mapping.

        Unknown fields are ignored. In strict mode a present field with the
        wrong shape raises ``RolePolicyError`` instead of falling back, which
        is how an operator file is loaded.
        """
        if raw is None:
            data: dict[str, Any] = {}
        elif isinstance(raw, Mapping):
            data = dict(raw)
        elif strict:
            raise RolePolicyError(f"{ROLE_POLICY_ENV} must contain a mapping")
        else:
            data = {}

        def _names(key: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
            if key not in data or data.get(key) is None:
                return fallback
            value = data.get(key)
            if isinstance(value, str):
                value = tuple(part.strip() for part in value.split(",") if part.strip())
            if not isinstance(value, (list, tuple)):
                if strict:
                    raise RolePolicyError(f"{key} must be a string or a list of names")
                return fallback
            if strict:
                names_list: list[str] = []
                for index, item in enumerate(value):
                    if not isinstance(item, str) or not item.strip():
                        raise RolePolicyError(
                            f"{key}[{index}] must be a non-empty string"
                        )
                    names_list.append(item.strip())
                names = tuple(names_list)
            else:
                names = tuple(
                    str(item).strip()
                    for item in value
                    if isinstance(item, str) and str(item).strip()
                )
            if not names:
                if strict:
                    raise RolePolicyError(f"{key} is empty")
                return fallback
            return names

        developer = _names("developer_pool", DEFAULT_DEVELOPER_POOL)
        if "secondary_reviewer_pool" in data and data.get("secondary_reviewer_pool") is not None:
            secondary = _names("secondary_reviewer_pool", secondary_reviewers(developer))
        else:
            secondary = secondary_reviewers(developer)
        return cls(
            planner=_names("planner", (DEFAULT_PLANNER,)),
            primary_reviewer=_names(
                "primary_reviewer", (DEFAULT_PRIMARY_REVIEWER,)
            ),
            developer_pool=developer,
            secondary_reviewer_pool=secondary,
            work_tail=_names("work_tail", DEFAULT_WORK_TAIL),
            allow_reserved_code=_explicit_bool(
                data.get("allow_reserved_code"), strict=strict
            ),
            avoid=_avoid_map(data.get("avoid"), strict=strict),
        )

    def assignment_hints(self) -> dict[str, Any]:
        """N73 S2: the pool as advisory inputs for the Super-agent.

        ``preferred`` is the developer pool in its order (then the tail);
        ``avoid`` maps a name to the operator's reason. Neither is a gate:
        feasibility decides what may launch, and the selector fallback keeps
        the pool order exactly as before.
        """
        preferred = [
            canonical_provider(name)
            for name in (*self.developer_pool, *self.work_tail)
            if self.may_write_code(name)
        ]
        return {
            "preferred": list(dict.fromkeys(preferred)),
            "avoid": {canonical_provider(k): v for k, v in self.avoid.items()},
        }

    def advisory_work_roster(self) -> tuple[str, ...]:
        """Every write-capable roster name the Super-agent may pick (S2).

        The pool, the tail and the built-in roster; never a reserved role
        (Q1: claude and codex stay out of the author pool).
        """
        names = (*self.developer_pool, *self.work_tail, *DEFAULT_DEVELOPER_POOL, *DEFAULT_WORK_TAIL)
        reserved = self.canonical_reserved() | {"claude", "codex"}
        return tuple(
            dict.fromkeys(
                canonical_provider(n) for n in names if canonical_provider(n) not in reserved
            )
        )

    def reserved_providers(self) -> frozenset[str]:
        return frozenset(self.planner) | frozenset(self.primary_reviewer)

    def canonical_reserved(self) -> frozenset[str]:
        return frozenset(canonical_provider(name) for name in self.reserved_providers())

    def may_write_code(self, provider: str) -> bool:
        """Reserved roles stay out of the main development pool."""
        return self.allow_reserved_code or canonical_provider(provider) not in self.canonical_reserved()

    def role_violation(self, provider: str, role: str) -> str | None:
        """Return a human-readable reason a provider may not take a role."""
        if role == "developer" and not self.may_write_code(provider):
            return (
                f"{provider!r} is reserved for planning/review and may not "
                "write code unless allow_reserved_code is set"
            )
        if role == "planner" and provider not in self.planner:
            return f"{provider!r} is not in the planner pool"
        if role == "primary_reviewer" and provider not in self.primary_reviewer:
            return f"{provider!r} is not in the primary-reviewer pool"
        return None

    def choose_developer(
        self,
        agents: Iterable[AgentState],
        *,
        capabilities: Iterable[str] = (),
        exclude: Iterable[str] = (),
        line: Any = None,
        headroom: Mapping[str, float] | None = None,
        estimated_size: str = DEFAULT_ESTIMATED_SIZE,
        min_headroom: float | None = None,
    ) -> AgentState | None:
        """Pick the best available developer, or None when the pool is dry.

        ``line`` is the dispatch line being placed; its ``write_scope`` decides
        whether a text-only Worker may take it. Every capability decision goes
        through :func:`can_take_line`, so an unknown identity is refused
        (``unknown_worker``) instead of being assumed capable, and the refusal
        is left on ``assignment_refusal``/``text_only_filtered`` for the caller.

        ``headroom`` is the same remaining-fraction map a ``HeadroomPort`` would
        answer (read-only). A provider at or below ``min_headroom`` is refused as
        ``quota_exhausted`` and never chosen. Low remaining quota ranks below
        high; a ``large`` ``estimated_size`` sends the task to the provider with
        the most headroom among the survivors.
        """
        required = frozenset(str(item) for item in capabilities if str(item))
        excluded = frozenset(str(item) for item in exclude if str(item))
        floor = _min_headroom_floor(min_headroom)
        size = _normalize_estimated_size(estimated_size)
        refusal: str | None = None
        candidates: list[AgentState] = []
        for agent in self._available(agents):
            if agent.provider in excluded or agent.provider not in self.developer_pool:
                continue
            if not self.may_write_code(agent.provider):
                continue
            if not required.issubset(agent.capabilities):
                continue
            ok, reason = can_take_line(agent.provider, line)
            if not ok:
                if refusal is None:
                    refusal = reason
                continue
            remaining = _resolved_headroom(agent, headroom)
            if remaining <= floor:
                if refusal is None:
                    refusal = "quota_exhausted"
                continue
            if agent.headroom != remaining:
                agent = replace(agent, headroom=remaining)
            candidates.append(agent)
        object.__setattr__(self, "assignment_refusal", refusal)
        object.__setattr__(
            self,
            "text_only_filtered",
            "text_only_worker" if refusal == "text_only_worker" else None,
        )
        return _best(
            candidates,
            self.developer_pool,
            prefer_headroom=size == "large",
        )

    def choose_planner(
        self, agents: Iterable[AgentState], *, exclude: Iterable[str] = ()
    ) -> AgentState | None:
        excluded = frozenset(str(item) for item in exclude if str(item))
        return _best(
            (
                agent
                for agent in self._available(agents)
                if agent.provider in self.planner and agent.provider not in excluded
            ),
            self.planner,
        )

    def choose_primary_reviewer(
        self,
        agents: Iterable[AgentState],
        *,
        author: str,
        exclude: Iterable[str] = (),
    ) -> AgentState | None:
        """Pick the primary reviewer, never the author of the work.

        The author is compared as a canonical dispatch identity, so ``step``
        bars ``stepfun`` and ``zhipu`` bars ``glm``. A review runs the model's
        CLI read-only, so a primary reviewer whose CLI has no read-only mode is
        not a reviewer at all -- even when a policy file names it.
        """
        barred = _canonical_names(author, exclude)
        capable = read_only_capable_names()
        return _best(
            (
                agent
                for agent in self._available(agents)
                if agent.provider in self.primary_reviewer
                and canonical_provider(agent.provider) not in barred
                and canonical_provider(agent.provider) in capable
            ),
            self.primary_reviewer,
        )

    def choose_secondary_reviewer(
        self,
        agents: Iterable[AgentState],
        *,
        author: str,
        primary_reviewer: str = "",
        exclude: Iterable[str] = (),
    ) -> AgentState | None:
        """Pick a peer reviewer from the secondary pool, never the author.

        Two filters decide the pool, and both hold even when a policy file names
        a model that fails them: a member of ``REVIEW_INELIGIBLE`` has no CLI
        mode a review can run under, and a model outside ``READ_ONLY_CAPABLE``
        has no read-only mode at all. Identities are canonical, so ``step``
        bars ``stepfun`` as an author and as a primary reviewer.
        """
        barred = _canonical_names(author, primary_reviewer, exclude)
        blocked = review_ineligible_names()
        capable = read_only_capable_names()
        pool = [
            name
            for name in self.secondary_reviewer_pool
            if name not in blocked
            and name in capable
            and canonical_provider(name) not in barred
        ]
        allowed = {canonical_provider(name) for name in pool}
        return _best(
            (
                agent
                for agent in self._available(agents)
                if canonical_provider(agent.provider) in allowed
            ),
            tuple(pool),
        )

    @staticmethod
    def _available(agents: Iterable[AgentState]) -> list[AgentState]:
        return [agent for agent in agents if agent.can_take()]


def _explicit_bool(value: Any, *, strict: bool = False) -> bool:
    """Only an actual boolean (or a clear textual boolean) turns this on."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in {"1", "true", "yes", "on"}:
            return True
        if token in {"0", "false", "no", "off", ""}:
            return False
    if strict:
        raise RolePolicyError("allow_reserved_code must be a boolean")
    return False


def load_role_policy(env: Mapping[str, str] | None = None) -> RolePolicy:
    """The process policy: the v1 roster, or ``CONDUCTOR_ROLE_POLICY_FILE``.

    An empty variable uses the built-in roster. A variable that names a
    missing, unreadable or invalid file raises ``RolePolicyError`` and does
    not fall back to the built-in roster.
    """
    if env is None:
        # R6 L1: a stored role registry replaces the policy file; the file and
        # the built-in roster stay the fallback when nothing is stored.
        stored = _registry_policy()
        if stored is not None:
            return stored
    source = os.environ if env is None else env
    raw_path = str(source.get(ROLE_POLICY_ENV, "") or "").strip()
    if not raw_path:
        return RolePolicy.default()
    path = Path(raw_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RolePolicyError(
            f"{ROLE_POLICY_ENV} does not point at a readable file: {raw_path}"
        ) from exc
    try:
        parsed = _parse_policy_text(text, suffix=path.suffix.lower())
    except RolePolicyError:
        raise
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        raise RolePolicyError(
            f"{ROLE_POLICY_ENV} is not valid JSON or YAML: {raw_path}"
        ) from exc
    if not isinstance(parsed, Mapping):
        raise RolePolicyError(f"{ROLE_POLICY_ENV} must contain a mapping")
    return RolePolicy.from_mapping(parsed, strict=True)


def _avoid_map(value: Any, *, strict: bool) -> dict[str, str]:
    """``avoid``: a list of names, a comma string, or ``{name: reason}``."""
    if value is None:
        return {}
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple)):
        out: dict[str, str] = {}
        for item in value:
            if isinstance(item, str) and item.strip():
                out[item.strip().lower()] = ""
            elif strict:
                raise RolePolicyError("avoid entries must be names")
        return out
    if isinstance(value, Mapping):
        return {
            str(k).strip().lower(): str(v or "")
            for k, v in value.items()
            if str(k).strip()
        }
    if strict:
        raise RolePolicyError("avoid must be a list of names or a mapping of name to reason")
    return {}


def _registry_policy() -> RolePolicy | None:
    """The RolePolicy a stored role registry describes, or None."""
    from src.server.services.autonomy.role_types import (
        ROLE_PLANNER,
        ROLE_REVIEWER,
        ROLE_WORKER,
        stored_registry,
    )

    registry = stored_registry()
    if registry is None:
        return None
    worker = registry.entry(ROLE_WORKER)
    reviewer = registry.entry(ROLE_REVIEWER)
    developer = worker.extra.get("developer_pool")
    tail = worker.extra.get("work_tail")
    if not isinstance(developer, (list, tuple)):
        developer = [name for name in worker.candidate_names if name not in DEFAULT_WORK_TAIL]
        tail = [name for name in worker.candidate_names if name in DEFAULT_WORK_TAIL]
    else:
        # Candidates outside the stored pools are workers too (in their order).
        listed = {*developer, *(tail or ())}
        developer = [*developer, *(n for n in worker.candidate_names if n not in listed)]
        developer = [n for n in developer if n in worker.candidate_names]
        tail = [n for n in (tail or ()) if n in worker.candidate_names]
    secondary = reviewer.extra.get("secondary_pool")
    mapping = {
        "planner": list(registry.entry(ROLE_PLANNER).candidate_names),
        "primary_reviewer": list(reviewer.candidate_names),
        "developer_pool": list(developer),
        "allow_reserved_code": False,
    }
    if tail:
        mapping["work_tail"] = list(tail)
    else:
        # An empty tail is not a valid policy key; an absent one is the
        # built-in tail, so name it only when the registry keeps it.
        mapping["work_tail"] = [n for n in DEFAULT_WORK_TAIL if n in worker.candidate_names] or None
        if mapping["work_tail"] is None:
            del mapping["work_tail"]
    if isinstance(secondary, (list, tuple)):
        mapping["secondary_reviewer_pool"] = list(secondary)
    avoid = worker.extra.get("avoid")
    if isinstance(avoid, (list, tuple, Mapping)):
        mapping["avoid"] = avoid
    try:
        return RolePolicy.from_mapping(mapping, strict=True)
    except RolePolicyError:
        logger.warning("stored role registry is not a valid role policy; using fallback")
        return None


def _parse_policy_text(text: str, *, suffix: str) -> Any:
    stripped = text.lstrip()
    if suffix == ".json" or (suffix not in {".yaml", ".yml"} and stripped.startswith(("{", "["))):
        return json.loads(text)
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - PyYAML is a server dependency
        raise RolePolicyError("reading a YAML role policy needs PyYAML") from exc
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RolePolicyError(f"{ROLE_POLICY_ENV} is not valid YAML") from exc


def _canonical_names(*names: str | Iterable[str]) -> frozenset[str]:
    """Every spelling of the given names as one canonical dispatch identity."""
    canonical: set[str] = set()
    for group in names:
        # A lone name is a string, and a string is iterable: take it whole.
        items: Iterable[str] = (group,) if isinstance(group, str) else group
        for name in items:
            text = str(name or "").strip()
            if text:
                canonical.add(canonical_provider(text))
    return frozenset(canonical)


def _min_headroom_floor(min_headroom: float | None) -> float:
    """The exhausted floor shared with auto-dispatch, or an explicit override."""
    if min_headroom is not None:
        return float(min_headroom)
    try:
        from src.tools.auto_dispatch.pool import MIN_HEADROOM

        return float(MIN_HEADROOM)
    except Exception:  # pragma: no cover - pool is a local import target
        return _DEFAULT_MIN_HEADROOM


def _normalize_estimated_size(estimated_size: str) -> str:
    token = str(estimated_size or DEFAULT_ESTIMATED_SIZE).strip().lower()
    return token if token in ESTIMATED_SIZES else DEFAULT_ESTIMATED_SIZE


def _resolved_headroom(
    agent: AgentState, headroom: Mapping[str, float] | None
) -> float:
    """Remaining fraction for ranking: mapping wins, then the agent, else full.

    The map is searched under every spelling of the Worker, so a reading keyed
    ``zai`` or ``zcode`` still refuses an exhausted ``glm`` developer.
    """
    if headroom is not None:
        for key in provider_alias_spellings(agent.provider):
            if key in headroom:
                return max(0.0, float(headroom[key]))
    if agent.headroom is not None:
        return max(0.0, float(agent.headroom))
    return 1.0


def _best(
    agents: Iterable[AgentState],
    order: tuple[str, ...] = (),
    *,
    prefer_headroom: bool = False,
) -> AgentState | None:
    """Highest score wins. A tie takes the earlier name in ``order``."""
    candidates = list(agents)
    if not candidates:
        return None
    rank = {name: index for index, name in enumerate(order)}
    last = len(rank)

    def key(agent: AgentState) -> tuple[Any, ...]:
        # Negate the roster index so max() keeps the earliest name.
        return (_score(agent, prefer_headroom=prefer_headroom), -rank.get(agent.provider, last))

    return max(candidates, key=key)


def headroom_tier(remaining: float) -> int:
    """usage-plan tiers as an int rank: high=3, medium=2, low=1, exhausted=0.

    A healthy (high) provider outranks a low-quota one even when the low one
    has a slightly better success rate; success only breaks ties inside a tier.
    """
    if remaining >= 0.50:
        return 3
    if remaining >= 0.20:
        return 2
    if remaining >= 0.05:
        return 1
    return 0


def _score(
    agent: AgentState, *, prefer_headroom: bool = False
) -> tuple[float, ...]:
    """Favor headroom tier, then proven agents, free capacity, then quota.

    ``quota_remaining is None`` means the quota is unlimited and ranks ahead
    of every finite remaining amount. A finite amount still ranks by size.
    Headroom uses the usage-plan tiers so a healthy provider beats a low-quota
    one beyond equal-success ties. When ``prefer_headroom`` is set (a large
    estimated task) the continuous remaining fraction is the primary key.
    """
    free = max(0, agent.capacity - agent.active)
    if agent.quota_remaining is None:
        unlimited, quota = 1, 0
    else:
        unlimited, quota = 0, max(0, agent.quota_remaining)
    remaining = 1.0 if agent.headroom is None else max(0.0, float(agent.headroom))
    tier = float(headroom_tier(remaining))
    if prefer_headroom:
        return (
            remaining,
            tier,
            float(agent.success_rate),
            float(free),
            float(unlimited),
            float(quota),
        )
    return (
        tier,
        float(agent.success_rate),
        remaining,
        float(free),
        float(unlimited),
        float(quota),
    )
