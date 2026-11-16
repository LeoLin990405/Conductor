from datetime import datetime, timedelta, timezone

import pytest

from src.server.services.refactor.memory_tool_policy import (
    MemoryToolPolicySnapshot,
    load_memory_tool_policy,
)


@pytest.mark.asyncio
async def test_policy_load_is_owner_scoped_and_independent_of_retrieval_budget():
    calls = []

    async def read_denials(*, user_id):
        calls.append(user_id)
        return [
            {
                "entry_id": "8b5f4822-d24d-48d4-83b5-48bdb23d0f2b",
                "denied_tools": [{"type": "tool", "name": "Bash"}],
                "valid_from": None,
                "valid_until": None,
                "integrity_mismatch": True,
            },
            {
                "entry_id": "a2f1650c-4fe1-473e-9106-e3341e5edc93",
                "denied_tools": [
                    {"type": "mcp", "server": "deploys", "tool": "publish"}
                ],
                "valid_from": None,
                "valid_until": None,
                "integrity_mismatch": False,
            },
            {
                "entry_id": "74b99e34-12d7-4589-ae37-306b65e408c5",
                "denied_tools": [{"type": "tool", "name": "DeleteRecord"}],
                "valid_from": None,
                "valid_until": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
                "integrity_mismatch": False,
            },
        ]

    snapshot = await load_memory_tool_policy(
        user_id="owner-1", read_denials=read_denials
    )

    assert calls == ["owner-1"]
    assert snapshot.blocks_code_execution
    assert not snapshot.native_runtime_allowed
    assert snapshot.integrity_mismatch_entry_ids == (
        "8b5f4822-d24d-48d4-83b5-48bdb23d0f2b",
    )
    assert snapshot.blocks("Bash")
    assert snapshot.blocks("DirectPublish", {"server": "deploys", "tool": "publish"})
    assert not snapshot.blocks("DirectPublish", {"server": "deploys", "tool": "status"})
    assert not snapshot.blocks("DeleteRecord")
    assert len(snapshot.source_entry_ids) == 2
    assert snapshot.digest


@pytest.mark.asyncio
async def test_policy_source_or_shape_failure_yields_deny_all():
    async def broken_source(*, user_id):
        raise RuntimeError("unavailable")

    async def malformed_source(*, user_id):
        return [{"entry_id": "not-an-id", "denied_tools": "do not delete"}]

    for source in (broken_source, malformed_source):
        snapshot = await load_memory_tool_policy(
            user_id="owner-1", read_denials=source
        )
        assert snapshot.deny_all
        assert snapshot.blocks("Read")

    missing_owner = await load_memory_tool_policy(user_id="")
    assert missing_owner.deny_all


def test_empty_policy_allows_execution_surfaces():
    empty = MemoryToolPolicySnapshot()
    assert not empty.blocks_code_execution
    assert empty.native_runtime_allowed
    assert not empty.blocks("ExecuteCode")
