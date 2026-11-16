"""The role registry service (R6 L1, N2).

One configuration for the four roles. Reads go through a cached snapshot that
is installed process-wide (``role_types.set_registry_provider``), so the
selector, the planner choice and the Router read the same registry without a
database round trip. Writes validate the hard rules, compare-and-swap on the
revision and write an audit row in the same transaction.

With no stored row the snapshot is ``None`` and every consumer uses the legacy
settings (``config.yaml`` ``autonomy_router_model``,
``CONDUCTOR_DISPATCH_PLANNER``, ``CONDUCTOR_ROLE_POLICY_FILE``) exactly as
before. The first write materialises the fallback as the stored document.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from src.server.services.autonomy.role_types import (
    HARD_RULES,
    ROLES,
    SOURCE_STORE,
    ModelRef,
    RoleConstraints,
    RoleEntry,
    RoleRegistry,
    RoleRegistryError,
    current_registry,
    fallback_registry,
    require_valid,
    set_registry_provider,
)

logger = logging.getLogger(__name__)

EDITABLE_FIELDS = frozenset({"candidates", "default", "constraints", "extra"})
MAX_REASON = 2000
MAX_CANDIDATES = 32
MAX_EXTRA_VALUE_BYTES = 2_000
MAX_CHANGE_BYTES = 8_000
#: ``extra`` keys an edit may set, per role. Anything else is refused.
EXTRA_KEYS: Mapping[str, frozenset[str]] = {
    "router": frozenset({"legacy"}),
    "planner": frozenset({"planner_cli", "legacy"}),
    "reviewer": frozenset({"secondary_pool", "legacy"}),
    "worker": frozenset({"developer_pool", "work_tail", "avoid", "legacy"}),
}
#: Keys only the Router handoff writes (``internal=True``); refused from PUT.
INTERNAL_EXTRA_KEYS: Mapping[str, frozenset[str]] = {
    "router": frozenset({"handoff", "handoffs", "handoff_delivered"}),
}
_ROUTER_NAME = re.compile(r"^[a-z0-9][a-z0-9._:/@-]{0,99}$")


def known_roster() -> frozenset[str]:
    """Every roster name a planner, reviewer or worker candidate may be."""
    from src.server.services.autonomy.roles import (
        DEFAULT_DEVELOPER_POOL,
        DEFAULT_PLANNER,
        DEFAULT_PRIMARY_REVIEWER,
        DEFAULT_SECONDARY_REVIEWER_POOL,
        DEFAULT_WORK_TAIL,
        canonical_provider,
    )

    names = {
        *DEFAULT_DEVELOPER_POOL,
        *DEFAULT_WORK_TAIL,
        *DEFAULT_SECONDARY_REVIEWER_POOL,
        DEFAULT_PLANNER,
        DEFAULT_PRIMARY_REVIEWER,
    }
    return frozenset(canonical_provider(name) for name in names)


def _check_change(role: str, changes: Mapping[str, Any], *, internal: bool) -> None:
    """Refuse oversized edits, unknown ``extra`` keys and unknown candidates."""
    from src.server.services.autonomy.roles import canonical_provider

    try:
        size = len(json.dumps(changes, default=str).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise RoleRegistryError("change is not serialisable") from exc
    if size > MAX_CHANGE_BYTES and not internal:
        raise RoleRegistryError("change is too large")
    extra = changes.get("extra")
    if extra is not None:
        if not isinstance(extra, Mapping):
            raise RoleRegistryError("extra must be a mapping")
        allowed = set(EXTRA_KEYS.get(role, ()))
        if internal:
            allowed |= set(INTERNAL_EXTRA_KEYS.get(role, ()))
        for key, value in extra.items():
            if key not in allowed:
                raise RoleRegistryError(f"extra key not allowed for {role}: {str(key)[:40]}")
            if not internal and len(json.dumps(value, default=str).encode("utf-8")) > MAX_EXTRA_VALUE_BYTES:
                raise RoleRegistryError(f"extra.{key} is too large")
    names: list[str] = []
    raw = changes.get("candidates")
    if raw is not None:
        if isinstance(raw, str):
            raw = raw.split(",")
        if not isinstance(raw, (list, tuple)):
            raise RoleRegistryError("candidates must be a list")
        if len(raw) > MAX_CANDIDATES:
            raise RoleRegistryError("too many candidates")
        names.extend(str(item) for item in raw if str(item).strip())
    if changes.get("default"):
        names.append(str(changes["default"]))
    roster = known_roster()
    for raw_name in names:
        name = ModelRef.parse(raw_name).name
        if role == "router":
            if not _ROUTER_NAME.match(name):
                raise RoleRegistryError("router candidate is not a model name")
        elif canonical_provider(name) not in roster:
            raise RoleRegistryError(f"unknown {role} candidate: {name[:40]}")


class RegistryStore(Protocol):
    async def get_registry(self) -> dict[str, Any] | None: ...

    async def save_registry(self, **kwargs: Any) -> int: ...

    async def list_audit(self, *, limit: int = 50, role: str | None = None) -> list[dict]: ...


class RegistryConflictError(RuntimeError):
    """Someone else changed the registry since the caller read it."""


class PostgresRegistryStore:
    """The migration-151 tables."""

    async def get_registry(self) -> dict[str, Any] | None:
        from src.server.database import role_registry as db

        return await db.get_registry()

    async def save_registry(self, **kwargs: Any) -> int:
        from src.server.database import role_registry as db

        try:
            return await db.save_registry(**kwargs)
        except db.RegistryConflict as exc:
            raise RegistryConflictError(str(exc)) from exc

    async def list_audit(self, *, limit: int = 50, role: str | None = None) -> list[dict]:
        from src.server.database import role_registry as db

        return await db.list_audit(limit=limit, role=role)


@dataclass
class InMemoryRegistryStore:
    """Same contract as the tables; used by tests and dev runs."""

    row: dict[str, Any] | None = None
    audit: list[dict[str, Any]] = field(default_factory=list)

    async def get_registry(self) -> dict[str, Any] | None:
        return None if self.row is None else dict(self.row)

    async def save_registry(
        self,
        *,
        document: dict[str, Any],
        expected_revision: int,
        actor: str,
        role: str,
        reason: str,
        before: dict[str, Any] | None,
        after: dict[str, Any],
    ) -> int:
        current = 0 if self.row is None else int(self.row["revision"])
        if current != expected_revision:
            raise RegistryConflictError(str(expected_revision))
        revision = expected_revision + 1
        self.row = {"document": document, "revision": revision, "updated_by": actor}
        self.audit.insert(
            0,
            {
                "audit_id": len(self.audit) + 1,
                "revision": revision,
                "role": role,
                "actor": actor,
                "reason": reason,
                "before": before,
                "after": after,
            },
        )
        return revision

    async def list_audit(self, *, limit: int = 50, role: str | None = None) -> list[dict]:
        rows = [row for row in self.audit if role is None or row["role"] == role]
        return [dict(row) for row in rows[:limit]]


def _entry_with(entry: RoleEntry, changes: Mapping[str, Any]) -> RoleEntry:
    unknown = sorted(set(changes) - EDITABLE_FIELDS)
    if unknown:
        raise RoleRegistryError(f"not editable: {', '.join(unknown)}")
    candidates = entry.candidates
    if "candidates" in changes:
        raw = changes["candidates"]
        if isinstance(raw, str):
            raw = [item for item in raw.split(",")]
        candidates = tuple(ModelRef.parse(item) for item in raw if str(item).strip())
    default = entry.default
    if "default" in changes:
        value = changes["default"]
        default = ModelRef.parse(value) if value else None
    elif default is not None and default.name not in {ref.name for ref in candidates}:
        default = candidates[0] if candidates else None
    if default is not None and default.name not in {ref.name for ref in candidates}:
        # Naming a default that is not yet a candidate adds it first.
        candidates = (default, *candidates)
    constraints = entry.constraints
    if "constraints" in changes:
        merged = {**entry.constraints.to_dict(), **dict(changes["constraints"] or {})}
        constraints = RoleConstraints.from_dict(merged)
    extra = dict(entry.extra)
    if "extra" in changes:
        extra.update(dict(changes["extra"] or {}))
    return RoleEntry(entry.role, candidates, default, constraints, extra)


PLANNER_CLIS = ("claude-code", "llm")


def _check_role_specific(entry: RoleEntry) -> None:
    if entry.role == "planner":
        cli = str(entry.extra.get("planner_cli") or "").strip().lower()
        if cli and cli not in PLANNER_CLIS:
            raise RoleRegistryError(f"planner_cli must be one of {', '.join(PLANNER_CLIS)}")


class RoleRegistryService:
    """Load, cache, validate and audit the role registry."""

    def __init__(self, store: RegistryStore | None = None) -> None:
        self._store: RegistryStore = store or PostgresRegistryStore()
        self._stored: RoleRegistry | None = None
        self._loaded = False
        self._lock = asyncio.Lock()

    # -- reads ---------------------------------------------------------------

    def snapshot(self) -> RoleRegistry | None:
        """The cached stored registry, or ``None`` (legacy settings apply)."""
        return self._stored

    def effective(self) -> RoleRegistry:
        return self._stored if self._stored is not None else fallback_registry()

    async def refresh(self) -> RoleRegistry:
        row = await self._store.get_registry()
        if row is None:
            self._stored = None
        else:
            self._stored = RoleRegistry.from_dict(
                row["document"], source=SOURCE_STORE, revision=int(row["revision"])
            )
        self._loaded = True
        return self.effective()

    async def get(self) -> RoleRegistry:
        if not self._loaded:
            await self.refresh()
        return self.effective()

    async def audit(self, *, limit: int = 50, role: str | None = None) -> list[dict]:
        if role is not None and role not in ROLES:
            raise RoleRegistryError(f"unknown role: {role}")
        rows = await self._store.list_audit(limit=limit, role=role)
        return [_redact_audit_row(row) for row in rows]

    # -- writes --------------------------------------------------------------

    async def update_role(
        self,
        role: str,
        changes: Mapping[str, Any],
        *,
        actor: str,
        reason: str,
        expected_revision: int | None = None,
        internal: bool = False,
    ) -> RoleRegistry:
        """Change one role; validated, compare-and-swap, audited.

        ``internal`` is for the Router handoff only (its ``extra`` keys); an
        API edit is always ``internal=False``.
        """
        if role not in ROLES:
            raise RoleRegistryError("unknown role")
        _check_change(role, changes, internal=internal)
        reason = str(reason or "").strip()
        if not reason:
            raise RoleRegistryError("a change needs a reason")
        if not str(actor or "").strip():
            raise RoleRegistryError("a change needs an actor")
        async with self._lock:
            current = await self.refresh()
            revision = current.revision if current.source == SOURCE_STORE else 0
            if expected_revision is not None and expected_revision != revision:
                raise RegistryConflictError(
                    f"registry is at revision {revision}, not {expected_revision}"
                )
            before = current.entry(role)
            after = _entry_with(before, changes)
            _check_role_specific(after)
            candidate = require_valid(current.with_entry(after))
            document = candidate.to_dict()
            document.pop("source", None)
            document["revision"] = revision + 1
            new_revision = await self._store.save_registry(
                document=document,
                expected_revision=revision,
                actor=str(actor)[:200],
                role=role,
                reason=reason[:MAX_REASON],
                before=before.to_dict() if current.source == SOURCE_STORE else {
                    **before.to_dict(),
                    "source": current.source,
                },
                after=after.to_dict(),
            )
            self._stored = RoleRegistry.from_dict(
                document, source=SOURCE_STORE, revision=new_revision
            )
            logger.info(
                "role registry %s changed by %s (revision %s)", role, actor, new_revision
            )
            return self._stored

    # -- process wiring --------------------------------------------------------

    def install(self) -> None:
        """Make this service's snapshot the process-wide registry.

        Only the serving process is updated. Other processes (another API
        worker, a daemon) keep their snapshot until they start or call
        :meth:`refresh`; the dispatch path reads the registry when a dispatch
        machine is built, so a change applies from the next dispatch on.
        """
        set_registry_provider(self.snapshot)


_service: RoleRegistryService | None = None


def get_service() -> RoleRegistryService:
    global _service
    if _service is None:
        _service = RoleRegistryService()
    return _service


def set_service(service: RoleRegistryService | None) -> None:
    """Tests and the app lifespan swap the process service."""
    global _service
    _service = service


async def install_at_startup() -> None:
    """Load the stored registry and install it; a missing table keeps the fallback."""
    service = get_service()
    try:
        await service.refresh()
    except Exception:
        logger.warning("role registry not loaded; using legacy settings", exc_info=True)
        return
    service.install()


def _redact_entry(entry: Any) -> Any:
    """A role entry with the handoff's per-session records reduced to a count."""
    if not isinstance(entry, dict):
        return entry
    extra = entry.get("extra")
    if not isinstance(extra, dict):
        return entry
    extra = dict(extra)
    handoff = extra.get("handoff")
    if isinstance(handoff, dict) and "sessions" in handoff:
        sessions = handoff.get("sessions")
        extra["handoff"] = {
            **{k: v for k, v in handoff.items() if k != "sessions"},
            "session_count": len(sessions) if isinstance(sessions, dict) else 0,
        }
    extra.pop("handoff_delivered", None)
    return {**entry, "extra": extra}


def _redact_audit_row(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for key in ("before", "after"):
        out[key] = _redact_entry(out.get(key))
    return out


def describe(registry: RoleRegistry | None = None) -> dict[str, Any]:
    """The API / CLI view: the document plus the hard rules.

    The Router handoff's per-session records name other users' Router
    threads and Spill row refs, so the view keeps only their count.
    """
    reg = registry or current_registry()
    view = reg.to_dict()
    view["hard_rules"] = list(HARD_RULES)
    roles = view.get("roles")
    if isinstance(roles, dict) and "router" in roles:
        roles["router"] = _redact_entry(roles["router"])
    return view
