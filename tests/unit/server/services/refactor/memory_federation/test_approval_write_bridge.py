"""The approve hook and the MemFW write bridge.

The hook only ever *adds* a MemFW candidate: a staging failure must not roll
back the human-approved Conductor authority, and the candidate carries the
tenant of the proposal or, failing that, the tenant of the user's
memory-federation binding.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.config.models import MemoryFederationUserBinding
from src.server.services.refactor import proposal_approval
from src.server.services.refactor.memory_federation import write_bridge

pytestmark = pytest.mark.asyncio

PROPOSAL = {
    "proposal_id": "proposal-1",
    "user_id": "11111111-1111-1111-1111-111111111111",
    "thread_id": "thread-1",
    "task_run_id": "run-1",
    "proposal_type": "feedback",
    "proposal_key": "feedback-abc123",
    "text": "Always cite sources in research answers.",
    "status": "proposed",
}


class _Item:
    def __init__(self, value):
        self.value = value


class _Store:
    def __init__(self):
        self.entries = {}

    async def aget(self, namespace, key):
        value = self.entries.get((tuple(namespace), key))
        return _Item(value) if value is not None else None

    async def aput(self, namespace, key, value):
        self.entries[(tuple(namespace), key)] = value
        return value

    async def adelete(self, namespace, key):
        self.entries.pop((tuple(namespace), key), None)


def _binding(tenant: str):
    """A memory-federation binding that carries a MemFW tenant."""
    return MemoryFederationUserBinding(
        memfw_url="http://memfw.invalid:18080", memfw_tenant=tenant
    )


def _mock_approval_dependencies(monkeypatch):
    monkeypatch.setattr(
        "src.server.services.refactor.memory_dual_write.project_memory_write",
        AsyncMock(return_value={"row": {}}),
    )
    monkeypatch.setattr(
        "src.server.database.refactor.memory.get_entry",
        AsyncMock(
            return_value={"entry_id": "22222222-2222-2222-2222-222222222222"}
        ),
    )
    monkeypatch.setattr(
        "src.server.database.refactor.memory.get_active_entry_for_approval",
        AsyncMock(
            return_value={
                "entry_id": "22222222-2222-2222-2222-222222222222",
                "content_hash": "sha256:" + "a" * 64,
            }
        ),
    )
    monkeypatch.setattr(
        "src.server.database.refactor.memory.get_projection_rows",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "src.server.database.refactor.memory.restore_projection_rows",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "src.server.database.refactor.memory_tool_policy.has_active_denials",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "src.server.database.refactor.memory_tool_policy.get_denials_snapshot",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "src.server.database.refactor.memory_tool_policy.restore_denials_snapshot",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "src.config.features.system_flag",
        lambda _name: SimpleNamespace(enabled=True),
    )
    monkeypatch.setattr(
        "src.server.services.features.user_feature_enabled",
        AsyncMock(return_value=True),
    )


def _mock_stage(monkeypatch, result=None):
    stage = AsyncMock(
        return_value=result
        if result is not None
        else {"enabled": True, "staged": True, "duplicate": False}
    )
    monkeypatch.setattr(write_bridge, "stage_approved_proposal", stage)
    return stage


async def _approve(monkeypatch, proposal=None):
    return await proposal_approval.approve_proposal(
        proposal=proposal if proposal is not None else PROPOSAL,
        store=_Store(),
        owner_id=PROPOSAL["user_id"],
        approval_source="manual",
    )


async def test_staging_failure_does_not_roll_back_approved_pg_memory(monkeypatch):
    _mock_approval_dependencies(monkeypatch)
    staging_error = {
        "enabled": True,
        "staged": False,
        "error_code": "conflict",
    }
    stage = _mock_stage(monkeypatch, staging_error)
    monkeypatch.setattr(
        "src.config.settings.get_memory_federation_binding",
        lambda user_id: _binding("tenant-a"),
    )
    store = _Store()

    result = await proposal_approval.approve_proposal(
        proposal=PROPOSAL,
        store=store,
        owner_id=PROPOSAL["user_id"],
        approval_source="manual",
    )

    assert result["memfw_staging"] == staging_error
    assert result["memfw_staging"]["error_code"] == "conflict"
    assert result["detail_key"] == "feedback-abc123--rproposal-1.md"
    assert ((PROPOSAL["user_id"], "memory"), result["detail_key"]) in store.entries
    stage.assert_awaited_once()


async def test_tenant_and_endpoint_come_from_the_binding(monkeypatch):
    _mock_approval_dependencies(monkeypatch)
    stage = _mock_stage(monkeypatch)
    monkeypatch.setattr(
        "src.config.settings.get_memory_federation_binding",
        lambda user_id: _binding("tenant-a"),
    )

    await _approve(monkeypatch)

    assert stage.await_args.kwargs["tenant"] == "tenant-a"
    assert stage.await_args.kwargs["endpoint"] == "http://memfw.invalid:18080"
    assert stage.await_args.kwargs["sensitivity"] == "private"


async def test_tenant_comes_from_the_proposal_when_it_has_one(monkeypatch):
    _mock_approval_dependencies(monkeypatch)
    stage = _mock_stage(monkeypatch)
    monkeypatch.setattr(
        "src.config.settings.get_memory_federation_binding",
        lambda user_id: _binding("tenant-a"),
    )

    await _approve(monkeypatch, {**PROPOSAL, "tenant": "proposal-tenant"})

    assert stage.await_args.kwargs["tenant"] == "proposal-tenant"


async def test_a_broken_binding_disables_staging(monkeypatch):
    _mock_approval_dependencies(monkeypatch)
    stage = _mock_stage(monkeypatch)

    def explode(user_id):
        raise RuntimeError("binding unavailable")

    monkeypatch.setattr("src.config.settings.get_memory_federation_binding", explode)

    result = await _approve(monkeypatch)

    assert result["memfw_staging"]["reason"] == "no_user_binding"
    stage.assert_not_awaited()


async def test_a_missing_binding_disables_staging(monkeypatch):
    _mock_approval_dependencies(monkeypatch)
    stage = _mock_stage(monkeypatch)
    monkeypatch.setattr(
        "src.config.settings.get_memory_federation_binding", lambda user_id: None
    )

    result = await _approve(monkeypatch)

    assert result["memfw_staging"]["reason"] == "no_user_binding"
    stage.assert_not_awaited()


async def test_a_local_path_binding_does_not_authorize_remote_staging(monkeypatch):
    _mock_approval_dependencies(monkeypatch)
    stage = _mock_stage(monkeypatch)
    monkeypatch.setattr(
        "src.config.settings.get_memory_federation_binding",
        lambda user_id: MemoryFederationUserBinding(memfw_path="/srv/user-a"),
    )

    result = await _approve(monkeypatch)

    assert result["memfw_staging"]["reason"] == "no_user_binding"
    stage.assert_not_awaited()


async def test_secret_proposal_never_reaches_the_write_bridge(monkeypatch):
    _mock_approval_dependencies(monkeypatch)
    stage = _mock_stage(monkeypatch)
    monkeypatch.setattr(
        "src.config.settings.get_memory_federation_binding",
        lambda user_id: _binding("tenant-a"),
    )

    result = await _approve(
        monkeypatch,
        {**PROPOSAL, "metadata": {"sensitivity": "secret"}},
    )

    assert result["memfw_staging"]["error_code"] == "secret_forbidden"
    stage.assert_not_awaited()


async def test_system_flag_off_disables_staging(monkeypatch):
    _mock_approval_dependencies(monkeypatch)
    stage = _mock_stage(monkeypatch)
    monkeypatch.setattr(
        "src.config.settings.get_memory_federation_binding",
        lambda user_id: _binding("tenant-a"),
    )
    monkeypatch.setattr(
        "src.config.features.system_flag",
        lambda _name: SimpleNamespace(enabled=False),
    )

    result = await _approve(monkeypatch)

    assert result["memfw_staging"]["reason"] == "memory_federation_disabled"
    stage.assert_not_awaited()


async def test_user_opt_in_off_disables_staging(monkeypatch):
    _mock_approval_dependencies(monkeypatch)
    stage = _mock_stage(monkeypatch)
    monkeypatch.setattr(
        "src.config.settings.get_memory_federation_binding",
        lambda user_id: _binding("tenant-a"),
    )
    monkeypatch.setattr(
        "src.server.services.features.user_feature_enabled",
        AsyncMock(return_value=False),
    )

    result = await _approve(monkeypatch)

    assert result["memfw_staging"]["reason"] == "memory_federation_disabled"
    stage.assert_not_awaited()
