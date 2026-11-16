"""R6 L2: a Ticket's Worker launch stays inside the role registry."""

from __future__ import annotations

import pytest

from src.server.services.autonomy import role_types
from src.server.services.autonomy.role_types import (
    SOURCE_STORE,
    ModelRef,
    RoleConstraints,
    RoleEntry,
    RoleRegistry,
    fallback_registry,
)
from src.server.services.autonomy.worker import _check_registry_worker
from src.server.services.projects.dispatch import DispatchError


@pytest.fixture(autouse=True)
def _reset():
    role_types.set_registry_provider(None)
    yield
    role_types.set_registry_provider(None)


def _store(names):
    base = fallback_registry({}, router_model="r")
    entry = RoleEntry("worker", tuple(ModelRef.parse(n) for n in names), ModelRef.parse(names[0]),
                      RoleConstraints())
    reg = base.with_entry(entry)
    return RoleRegistry(entries=reg.entries, source=SOURCE_STORE, revision=1)


def test_fallback_allows_any_provider() -> None:
    _check_registry_worker({}, "grok")


def test_store_registry_refuses_a_non_candidate() -> None:
    role_types.set_registry_provider(lambda: _store(["stepfun"]))
    _check_registry_worker({}, "stepfun")
    with pytest.raises(DispatchError, match="registry_not_candidate:grok"):
        _check_registry_worker({}, "grok")


def test_router_session_never_reused_by_the_worker() -> None:
    ticket = {"mandate": {"router_session_id": "s1"}, "worker_session_id": "s1"}
    with pytest.raises(DispatchError, match="router_worker_same_session"):
        _check_registry_worker(ticket, "")
    _check_registry_worker({"mandate": {"router_session_id": "s1"}, "worker_session_id": "s2"}, "")
