import pytest
from unittest.mock import AsyncMock

from src.server.contracts.memory_tool_policy import snapshot_from_authority_rows
from src.server.database.refactor import memory_tool_policy as db_policy
from src.server.services.refactor.memory_tool_policy_approval import (
    sync_approved_memory_policy,
    sync_reverted_memory_policy,
)


@pytest.mark.asyncio
async def test_manual_constraint_approval_binds_projected_hash_and_typed_denials():
    writes = []

    async def read_entry(**kwargs):
        assert kwargs == {
            "user_id": "owner-1",
            "tier": "user",
            "entry_key": "approved.md",
        }
        return {
            "entry_id": "8b5f4822-d24d-48d4-83b5-48bdb23d0f2b",
            "content_hash": "sha256:" + "a" * 64,
        }

    async def persist(**kwargs):
        writes.append(kwargs)

    await sync_approved_memory_policy(
        user_id="owner-1",
        proposal={
            "metadata": {
                "kind": "constraint",
                "denied_tools": [{"type": "tool", "name": "Bash"}],
                "valid_from": "2026-11-01T00:00:00Z",
            }
        },
        approved_entry_key="approved.md",
        replaced_entry_ids=("a2f1650c-4fe1-473e-9106-e3341e5edc93",),
        read_entry=read_entry,
        persist=persist,
    )

    assert writes == [
        {
            "user_id": "owner-1",
            "entry_id": "8b5f4822-d24d-48d4-83b5-48bdb23d0f2b",
            "denied_tools": [{"type": "tool", "name": "Bash"}],
            "approval_source": "manual",
            "approved_content_hash": "sha256:" + "a" * 64,
            "valid_from": "2026-11-01T00:00:00+00:00",
            "valid_until": None,
            "replaced_entry_ids": ("a2f1650c-4fe1-473e-9106-e3341e5edc93",),
        }
    ]


@pytest.mark.asyncio
async def test_nonconstraint_replacement_retires_old_denials_without_adding_rules():
    writes = []

    async def read_entry(**_kwargs):
        return {
            "entry_id": "8b5f4822-d24d-48d4-83b5-48bdb23d0f2b",
            "content_hash": "sha256:" + "a" * 64,
        }

    async def persist(**kwargs):
        writes.append(kwargs)

    await sync_approved_memory_policy(
        user_id="owner-1",
        proposal={"metadata": {"kind": "preference"}},
        approved_entry_key="approved.md",
        replaced_entry_ids=("a2f1650c-4fe1-473e-9106-e3341e5edc93",),
        read_entry=read_entry,
        persist=persist,
    )

    assert writes[0]["denied_tools"] == []
    assert writes[0]["replaced_entry_ids"] == (
        "a2f1650c-4fe1-473e-9106-e3341e5edc93",
    )


@pytest.mark.asyncio
async def test_missing_kind_cannot_silently_retire_policy_authority():
    from unittest.mock import patch

    async def persist(**_kwargs):
        raise AssertionError("missing kind must not change policy rows")

    with patch.object(db_policy, "has_active_denials", AsyncMock(return_value=True)):
        with pytest.raises(ValueError, match="explicit memory kind"):
            await sync_approved_memory_policy(
                user_id="owner-1",
                proposal={"metadata": {"sensitivity": "private"}},
                approved_entry_key="merged.md",
                replaced_entry_ids=("a2f1650c-4fe1-473e-9106-e3341e5edc93",),
                persist=persist,
            )


@pytest.mark.asyncio
async def test_kindless_legacy_replacement_without_authority_remains_compatible():
    from unittest.mock import patch

    with patch.object(db_policy, "has_active_denials", AsyncMock(return_value=False)) as check:
        await sync_approved_memory_policy(
            user_id="owner-1",
            proposal={"metadata": {"sensitivity": "private"}},
            approved_entry_key="merged.md",
            replaced_entry_ids=("a2f1650c-4fe1-473e-9106-e3341e5edc93",),
        )
    check.assert_awaited_once_with(
        user_id="owner-1",
        entry_ids=("a2f1650c-4fe1-473e-9106-e3341e5edc93",),
    )


@pytest.mark.asyncio
async def test_future_constraint_is_approved_but_snapshot_waits_for_window():
    writes = []

    async def read_entry(**_kwargs):
        return {
            "entry_id": "8b5f4822-d24d-48d4-83b5-48bdb23d0f2b",
            "content_hash": "sha256:" + "a" * 64,
        }

    async def persist(**kwargs):
        writes.append(kwargs)

    await sync_approved_memory_policy(
        user_id="owner-1",
        proposal={
            "metadata": {
                "kind": "constraint",
                "denied_tools": [{"type": "tool", "name": "Bash"}],
                "valid_from": "2099-01-01T00:00:00Z",
            }
        },
        approved_entry_key="future-policy.md",
        read_entry=read_entry,
        persist=persist,
    )

    snapshot = snapshot_from_authority_rows(
        [{
            "entry_id": writes[0]["entry_id"],
            "denied_tools": writes[0]["denied_tools"],
            "valid_from": writes[0]["valid_from"],
            "valid_until": writes[0]["valid_until"],
            "integrity_mismatch": False,
        }]
    )
    assert writes[0]["valid_from"] == "2099-01-01T00:00:00+00:00"
    assert snapshot.denied_tools == ()
    assert not snapshot.blocks("Bash")


@pytest.mark.asyncio
async def test_default_reader_binds_future_policy_through_database_connection(monkeypatch):
    from src.server.database.refactor import memory as db_memory

    approved_row = {
        "entry_id": "8b5f4822-d24d-48d4-83b5-48bdb23d0f2b",
        "content_hash": "sha256:" + "a" * 64,
        "metadata": {"valid_from": "2099-01-01T00:00:00+00:00"},
    }
    query_log = []

    class Cursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, query, params):
            query_log.append((query, params))

        async def fetchone(self):
            return approved_row

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def cursor(self, **_kwargs):
            return Cursor()

    monkeypatch.setattr(db_memory.pool, "get_db_connection", Connection)
    writes = []

    async def persist(**kwargs):
        writes.append(kwargs)

    await sync_approved_memory_policy(
        user_id="8b5f4822-d24d-48d4-83b5-48bdb23d0f2b",
        proposal={
            "metadata": {
                "kind": "constraint",
                "denied_tools": [{"type": "tool", "name": "Bash"}],
                "valid_from": "2099-01-01T00:00:00Z",
            }
        },
        approved_entry_key="future-policy.md",
        persist=persist,
    )

    assert query_log
    query, params = query_log[0]
    assert "status = 'active'" in query
    assert "NOT EXISTS (SELECT 1 FROM memory_forget_tombstones" in query
    assert "valid_from" not in query
    assert params[1:3] == ("user", "future-policy.md")
    assert writes[0]["approved_content_hash"] == approved_row["content_hash"]
    assert writes[0]["valid_from"] == "2099-01-01T00:00:00+00:00"


@pytest.mark.asyncio
async def test_constraint_prose_cannot_be_interpreted_as_a_denial():
    async def read_entry(**_kwargs):
        return {
            "entry_id": "8b5f4822-d24d-48d4-83b5-48bdb23d0f2b",
            "content_hash": "sha256:" + "a" * 64,
        }

    with pytest.raises(ValueError, match="denied_tools"):
        await sync_approved_memory_policy(
            user_id="owner-1",
            proposal={
                "metadata": {
                    "kind": "constraint",
                    "denied_tools": "never run Bash",
                }
            },
            approved_entry_key="approved.md",
            read_entry=read_entry,
            persist=lambda **_kwargs: None,
        )


@pytest.mark.asyncio
async def test_revert_copies_only_frozen_source_policy_to_new_hash_bound_entry():
    calls = []

    async def read_entry(**_kwargs):
        return {
            "entry_id": "8b5f4822-d24d-48d4-83b5-48bdb23d0f2b",
            "content_hash": "sha256:" + "b" * 64,
        }

    async def copy_policy(**kwargs):
        calls.append(kwargs)
        return True

    copied = await sync_reverted_memory_policy(
        user_id="owner-1",
        restored_entry_key="restored.md",
        restored_from_entry_id="a2f1650c-4fe1-473e-9106-e3341e5edc93",
        replaced_entry_id="74b99e34-12d7-4589-ae37-306b65e408c5",
        read_entry=read_entry,
        copy_policy=copy_policy,
    )

    assert copied is True
    assert calls == [
        {
            "user_id": "owner-1",
            "source_entry_id": "a2f1650c-4fe1-473e-9106-e3341e5edc93",
            "restored_entry_id": "8b5f4822-d24d-48d4-83b5-48bdb23d0f2b",
            "restored_content_hash": "sha256:" + "b" * 64,
            "replaced_entry_ids": ("74b99e34-12d7-4589-ae37-306b65e408c5",),
        }
    ]


@pytest.mark.asyncio
async def test_revert_to_nonconstraint_still_retires_current_constraint():
    calls = []

    async def read_entry(**_kwargs):
        return {
            "entry_id": "8b5f4822-d24d-48d4-83b5-48bdb23d0f2b",
            "content_hash": "sha256:" + "c" * 64,
            "metadata": {"kind": "preference"},
        }

    async def copy_policy(**kwargs):
        calls.append(kwargs)
        return False

    restored = await sync_reverted_memory_policy(
        user_id="owner-1",
        restored_entry_key="restored-preference.md",
        restored_from_entry_id="a2f1650c-4fe1-473e-9106-e3341e5edc93",
        replaced_entry_id="74b99e34-12d7-4589-ae37-306b65e408c5",
        read_entry=read_entry,
        copy_policy=copy_policy,
    )

    assert restored is False
    assert calls[0]["replaced_entry_ids"] == (
        "74b99e34-12d7-4589-ae37-306b65e408c5",
    )
