"""Shared contract for the unified role registry (R6 / N2).

One configuration holds the four roles -- the Super-agent (Router), the
planner, the reviewer and the worker. Each role has candidates, a default and
constraints. This module is the pure, dependency-free contract the rest of R6
builds on:

* the value types (``ModelRef``, ``RoleConstraints``, ``RoleEntry``,
  ``RoleRegistry``);
* :func:`fallback_registry`, which reads the three legacy places the roles
  lived before the registry (``config.yaml`` ``autonomy_router_model``,
  ``CONDUCTOR_DISPATCH_PLANNER``, and ``RolePolicy`` /
  ``CONDUCTOR_ROLE_POLICY_FILE``) so an install with no stored registry
  behaves exactly as before;
* :func:`validate_registry`, which refuses any edit that would break a hard
  rule (the Router routes and never executes, review is read-only, a
  reviewer that cannot run read-only is never a reviewer);
* a process-wide provider hook (:func:`current_registry`,
  :func:`set_registry_provider`) so consumers read one snapshot without
  knowing whether it came from the store or the fallback.

The hard rules in ``HARD_RULES`` are enforced by the runtime code, not by
this registry: the Router's PTC session has no execution tools, reviewers run
read-only adapters, Stage-Gate bars an author from its own review, and only
a human approval opens the gate. The registry's constraint flags describe a
role and are validated so a document cannot contradict those rules, but no
flag turns a rule on or off at run time.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Mapping

ROLE_ROUTER = "router"
ROLE_PLANNER = "planner"
ROLE_REVIEWER = "reviewer"
ROLE_WORKER = "worker"
ROLES: tuple[str, ...] = (ROLE_ROUTER, ROLE_PLANNER, ROLE_REVIEWER, ROLE_WORKER)

LATEST = "latest"
REGISTRY_VERSION = 1

HARD_RULES: tuple[str, ...] = (
    "router_routes_never_executes",
    "author_never_reviews_own_work",
    "review_is_read_only",
    "merge_needs_human",
)

SOURCE_FALLBACK = "fallback"
SOURCE_STORE = "store"


class RoleRegistryError(ValueError):
    """A registry document is malformed or breaks a hard rule."""


@dataclass(frozen=True)
class ModelRef:
    """A roster name pinned to a version (``kimi@2.5``) or following latest."""

    name: str
    version: str = LATEST

    @classmethod
    def parse(cls, raw: str | "ModelRef") -> "ModelRef":
        if isinstance(raw, ModelRef):
            return raw
        text = str(raw or "").strip()
        if not text:
            raise RoleRegistryError("empty model reference")
        name, sep, version = text.partition("@")
        name = name.strip().lower()
        version = version.strip() if sep else LATEST
        if not name or not version:
            raise RoleRegistryError(f"invalid model reference: {text!r}")
        return cls(name=name, version=version)

    @property
    def pinned(self) -> bool:
        return self.version != LATEST

    @property
    def key(self) -> str:
        """``name`` when following latest, ``name@version`` when pinned."""
        return self.name if not self.pinned else f"{self.name}@{self.version}"

    def __str__(self) -> str:
        return self.key


@dataclass(frozen=True)
class RoleConstraints:
    """What a role's occupant may do. Hard rules are re-checked by validation."""

    read_only: bool = False
    may_write: bool = True
    may_execute: bool = True
    requires_session_resume: bool = False
    excluded: tuple[str, ...] = ()
    max_parallel: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "read_only": self.read_only,
            "may_write": self.may_write,
            "may_execute": self.may_execute,
            "requires_session_resume": self.requires_session_resume,
            "excluded": list(self.excluded),
            "max_parallel": self.max_parallel,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "RoleConstraints":
        raw = raw or {}
        max_parallel = raw.get("max_parallel")
        if max_parallel not in (None, ""):
            try:
                max_parallel = int(max_parallel)
            except (TypeError, ValueError) as exc:
                raise RoleRegistryError("constraints.max_parallel must be an integer") from exc
            if max_parallel < 1:
                raise RoleRegistryError("constraints.max_parallel must be at least 1")
        return cls(
            read_only=bool(raw.get("read_only", False)),
            may_write=bool(raw.get("may_write", True)),
            may_execute=bool(raw.get("may_execute", True)),
            requires_session_resume=bool(raw.get("requires_session_resume", False)),
            excluded=tuple(_names(raw.get("excluded") or ())),
            max_parallel=None if max_parallel in (None, "") else max_parallel,
        )


@dataclass(frozen=True)
class RoleEntry:
    """One role: ordered candidates (owner preference order), default, constraints.

    ``extra`` carries role-specific settings that are not candidates, e.g. the
    reviewer's ``secondary_pool`` or the planner's ``planner_cli``.
    """

    role: str
    candidates: tuple[ModelRef, ...]
    default: ModelRef | None
    constraints: RoleConstraints = field(default_factory=RoleConstraints)
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def candidate_names(self) -> tuple[str, ...]:
        return tuple(ref.name for ref in self.candidates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "candidates": [ref.key for ref in self.candidates],
            "default": self.default.key if self.default else None,
            "constraints": self.constraints.to_dict(),
            "extra": _plain(self.extra),
        }

    @classmethod
    def from_dict(cls, role: str, raw: Mapping[str, Any]) -> "RoleEntry":
        candidates = tuple(ModelRef.parse(item) for item in raw.get("candidates") or ())
        default_raw = raw.get("default")
        default = ModelRef.parse(default_raw) if default_raw else None
        return cls(
            role=role,
            candidates=candidates,
            default=default,
            constraints=RoleConstraints.from_dict(raw.get("constraints")),
            extra=dict(raw.get("extra") or {}),
        )


@dataclass(frozen=True)
class RoleRegistry:
    """The four roles. ``revision`` increments on every audited store write."""

    entries: Mapping[str, RoleEntry]
    source: str = SOURCE_FALLBACK
    revision: int = 0
    version: int = REGISTRY_VERSION

    def entry(self, role: str) -> RoleEntry:
        try:
            return self.entries[role]
        except KeyError as exc:
            raise RoleRegistryError(f"unknown role: {role}") from exc

    def default_name(self, role: str) -> str | None:
        ref = self.entry(role).default
        return ref.name if ref else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source": self.source,
            "revision": self.revision,
            "hard_rules": list(HARD_RULES),
            "roles": {role: self.entries[role].to_dict() for role in ROLES if role in self.entries},
        }

    @classmethod
    def from_dict(
        cls, raw: Mapping[str, Any], *, source: str = SOURCE_STORE, revision: int | None = None
    ) -> "RoleRegistry":
        roles = raw.get("roles")
        if not isinstance(roles, Mapping):
            raise RoleRegistryError("registry document needs a 'roles' mapping")
        unknown = sorted(set(roles) - set(ROLES))
        if unknown:
            raise RoleRegistryError(f"unknown roles: {', '.join(unknown)}")
        entries = {role: RoleEntry.from_dict(role, roles[role]) for role in ROLES if role in roles}
        missing = [role for role in ROLES if role not in entries]
        if missing:
            raise RoleRegistryError(f"missing roles: {', '.join(missing)}")
        return cls(
            entries=entries,
            source=source,
            revision=int(raw.get("revision", 0) if revision is None else revision),
            version=int(raw.get("version", REGISTRY_VERSION)),
        )

    def with_entry(self, entry: RoleEntry) -> "RoleRegistry":
        entries = dict(self.entries)
        entries[entry.role] = entry
        return replace(self, entries=entries)


def _names(values: Iterable[Any]) -> list[str]:
    if isinstance(values, str):
        values = values.split(",")
    return [str(v).strip().lower() for v in values if str(v).strip()]


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


# --- hard-rule validation ------------------------------------------------------


def validate_registry(registry: RoleRegistry) -> list[str]:
    """Return hard-rule violations (empty when the registry is acceptable)."""
    from src.server.services.autonomy.roles import review_ineligible_names

    errors: list[str] = []
    for role in ROLES:
        if role not in registry.entries:
            errors.append(f"missing_role:{role}")
    if errors:
        return errors
    for role, entry in registry.entries.items():
        names = entry.candidate_names
        if len(set(names)) != len(names):
            errors.append(f"duplicate_candidate:{role}")
        if entry.default is not None and entry.default.name not in names:
            errors.append(f"default_not_candidate:{role}")
        if not names:
            errors.append(f"no_candidates:{role}")
    router = registry.entry(ROLE_ROUTER).constraints
    if router.may_execute or router.may_write:
        errors.append("router_routes_never_executes")
    reviewer = registry.entry(ROLE_REVIEWER)
    if reviewer.constraints.may_write or not reviewer.constraints.read_only:
        errors.append("review_is_read_only")
    from src.server.services.autonomy.roles import canonical_provider, read_only_capable_names

    ineligible = review_ineligible_names()
    read_only = read_only_capable_names()
    secondary = _names(reviewer.extra.get("secondary_pool") or ())
    for name in (*reviewer.candidate_names, *secondary):
        if name in ineligible:
            errors.append(f"review_ineligible:{name}")
        elif canonical_provider(name) not in read_only:
            errors.append(f"review_not_read_only_capable:{name}")
    planner = registry.entry(ROLE_PLANNER).constraints
    if planner.may_write or not planner.read_only:
        errors.append("planner_is_read_only")
    worker = registry.entry(ROLE_WORKER).constraints
    if worker.read_only or not worker.may_write:
        errors.append("worker_must_write")
    return errors


def require_valid(registry: RoleRegistry) -> RoleRegistry:
    errors = validate_registry(registry)
    if errors:
        raise RoleRegistryError("; ".join(errors))
    return registry


# --- legacy fallback -------------------------------------------------------------

ROUTER_CONSTRAINTS = RoleConstraints(read_only=True, may_write=False, may_execute=False)
PLANNER_CONSTRAINTS = RoleConstraints(read_only=True, may_write=False, may_execute=True)
REVIEWER_CONSTRAINTS = RoleConstraints(read_only=True, may_write=False, may_execute=True)
WORKER_CONSTRAINTS = RoleConstraints(read_only=False, may_write=True, may_execute=True)


def fallback_registry(
    env: Mapping[str, str] | None = None,
    *,
    router_model: str | None = None,
    role_policy: Any | None = None,
) -> RoleRegistry:
    """The registry the legacy settings describe; used when nothing is stored.

    ``router_model`` defaults to ``get_autonomy_router_model()`` (config.yaml),
    the planner CLI to ``CONDUCTOR_DISPATCH_PLANNER`` and the reviewer and
    worker pools to ``load_role_policy(env)``. Tests pass all three.
    """
    from src.server.services.autonomy.roles import load_role_policy

    source = os.environ if env is None else env
    if router_model is None:
        try:
            from src.config.settings import get_autonomy_router_model

            router_model = get_autonomy_router_model()
        except Exception:  # config unavailable in a bare test process
            router_model = None
    policy = role_policy if role_policy is not None else load_role_policy(source)
    planner_cli = str(source.get("CONDUCTOR_DISPATCH_PLANNER", "") or "").strip().lower()

    router_refs = (ModelRef.parse(router_model),) if router_model else ()
    planner_refs = tuple(ModelRef.parse(n) for n in policy.planner)
    reviewer_refs = tuple(ModelRef.parse(n) for n in policy.primary_reviewer)
    worker_names = list(dict.fromkeys([*policy.developer_pool, *policy.work_tail]))
    worker_refs = tuple(ModelRef.parse(n) for n in worker_names)
    entries = {
        ROLE_ROUTER: RoleEntry(
            ROLE_ROUTER,
            router_refs,
            router_refs[0] if router_refs else None,
            ROUTER_CONSTRAINTS,
            {"legacy": "config.yaml:autonomy_router_model"},
        ),
        ROLE_PLANNER: RoleEntry(
            ROLE_PLANNER,
            planner_refs,
            planner_refs[0] if planner_refs else None,
            PLANNER_CONSTRAINTS,
            {"planner_cli": planner_cli or "claude-code", "legacy": "CONDUCTOR_DISPATCH_PLANNER"},
        ),
        ROLE_REVIEWER: RoleEntry(
            ROLE_REVIEWER,
            reviewer_refs,
            reviewer_refs[0] if reviewer_refs else None,
            REVIEWER_CONSTRAINTS,
            {"secondary_pool": list(policy.secondary_reviewer_pool), "legacy": "RolePolicy"},
        ),
        ROLE_WORKER: RoleEntry(
            ROLE_WORKER,
            worker_refs,
            worker_refs[0] if worker_refs else None,
            WORKER_CONSTRAINTS,
            {
                "developer_pool": list(policy.developer_pool),
                "work_tail": list(policy.work_tail),
                "legacy": "RolePolicy",
            },
        ),
    }
    return RoleRegistry(entries=entries, source=SOURCE_FALLBACK, revision=0)


# --- process-wide provider -------------------------------------------------------

#: Returns the stored registry snapshot, or ``None`` when nothing is stored
#: (then the legacy settings apply). It must never call ``fallback_registry``.
RegistryProvider = Callable[[], "RoleRegistry | None"]
_provider: RegistryProvider | None = None


def set_registry_provider(provider: RegistryProvider | None) -> None:
    """Install the store-backed snapshot reader (``None`` restores the fallback)."""
    global _provider
    _provider = provider


def stored_registry() -> RoleRegistry | None:
    """The stored snapshot, or ``None`` (no provider, nothing stored, or an error)."""
    if _provider is None:
        return None
    try:
        return _provider()
    except Exception:
        return None


def current_registry() -> RoleRegistry:
    """The live registry snapshot; the legacy fallback when nothing is stored."""
    stored = stored_registry()
    return stored if stored is not None else fallback_registry()


def pinned_version(role: str, name: str, registry: RoleRegistry | None = None) -> str:
    """The pinned version of ``name`` in ``role``'s candidates, or ``""`` (latest)."""
    try:
        entry = (registry or current_registry()).entry(role)
    except Exception:
        return ""
    wanted = str(name or "").strip().lower()
    for ref in entry.candidates:
        if ref.name == wanted and ref.pinned:
            return ref.version
    return ""


def registry_installed() -> bool:
    """True when a store-backed provider is installed (else the legacy fallback)."""
    return _provider is not None
