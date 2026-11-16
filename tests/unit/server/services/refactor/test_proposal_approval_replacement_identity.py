"""Replacement approval binds lineage to the active projected revision.

The approval reader deliberately finds active rows even when retrieval validity
has expired or starts in the future. A superseded row for the same key must not
replace that source identity, including while retrying an already-written
revision after the Store and projection are temporarily out of sync.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
import yaml

from src.server.contracts.memory_validity import is_current
from src.server.services.refactor import proposal_approval as approval

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _offline_projection_snapshot_db(monkeypatch):
    from src.server.database.refactor import memory as db_memory

    monkeypatch.setattr(
        db_memory, "get_projection_rows", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        db_memory, "restore_projection_rows", AsyncMock(return_value=None)
    )
    from src.server.database.refactor import memory_tool_policy as db_policy

    monkeypatch.setattr(
        db_policy, "get_denials_snapshot", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        db_policy, "restore_denials_snapshot", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        "src.server.database.refactor.memory_tool_policy.has_active_denials",
        AsyncMock(return_value=False),
    )

USER_ID = "11111111-1111-1111-1111-111111111111"
ACTIVE_ENTRY_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SUPERSEDED_ENTRY_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
OLD_KEY = "preference.md"

PROPOSAL = {
    "proposal_id": "replacement-1",
    "user_id": USER_ID,
    "proposal_type": "user",
    "proposal_key": "preference-update",
    "text": "Use English by default.",
    "change_kind": "correction",
    "replaces_entry_key": OLD_KEY,
    "reason": "The prior preference was incorrect.",
}


class _Item:
    def __init__(self, value):
        self.value = value


class FakeStore:
    def __init__(self, entries):
        self.entries = dict(entries)
        self.aput_calls = []

    async def aget(self, namespace, key):
        value = self.entries.get((tuple(namespace), key))
        return _Item(value) if value is not None else None

    async def aput(self, namespace, key, value):
        self.aput_calls.append((tuple(namespace), key, value))
        self.entries[(tuple(namespace), key)] = value

    async def adelete(self, namespace, key):
        self.entries.pop((tuple(namespace), key), None)


def _row(entry_id, *, status, metadata):
    return {
        "entry_id": entry_id,
        "user_id": USER_ID,
        "tier": "user",
        "namespace": (USER_ID, "memory"),
        "entry_key": OLD_KEY,
        "content_hash": f"sha256:{entry_id[0] * 64}",
        "status": status,
        "metadata": metadata,
    }


def _patch_fakes(monkeypatch, *, active_row):
    from src.server.database.refactor import memory as db_memory

    active_read = AsyncMock(return_value=active_row)
    superseded_read = AsyncMock(
        return_value=_row(
            SUPERSEDED_ENTRY_ID,
            status="superseded",
            metadata={"lifecycle": "superseded"},
        )
    )
    monkeypatch.setattr(db_memory, "get_active_entry_for_approval", active_read)
    monkeypatch.setattr(db_memory, "get_entry", superseded_read)

    async def fake_aget(store, namespace, key):
        return await store.aget(namespace, key)

    async def fake_aput(store, namespace, key, value):
        await store.aput(namespace, key, value)

    monkeypatch.setattr("src.server.app._store_helpers.aget", fake_aget)
    monkeypatch.setattr("src.server.app._store_helpers.aput", fake_aput)
    projection = AsyncMock(return_value={"row": {}})
    monkeypatch.setattr(
        "src.server.services.refactor.memory_dual_write.project_memory_write",
        projection,
    )
    policy_sync = AsyncMock()
    monkeypatch.setattr(
        "src.server.services.refactor.memory_tool_policy_approval.sync_approved_memory_policy",
        policy_sync,
    )
    return active_read, superseded_read, projection, policy_sync


def _source_store(*, old_content, new_content=None, new_key=None):
    entries = {
        ((USER_ID, "memory"), OLD_KEY): {"content": old_content},
    }
    if new_content is None:
        index = f"- [Prior preference]({OLD_KEY}) — Prior preference\n"
    else:
        index = f"- [Use English by default.]({new_key}) — Use English by default.\n"
        entries[((USER_ID, "memory"), new_key)] = {"content": new_content}
    entries[((USER_ID, "memory"), "memory.md")] = {"content": index}
    return FakeStore(entries)


@pytest.mark.parametrize(
    "validity_metadata",
    [
        {"valid_until": "2000-01-01T00:00:00+00:00"},
        {"valid_from": "2999-01-01T00:00:00+00:00"},
    ],
    ids=["expired-active-row", "future-active-row"],
)
async def test_replacement_prefers_expired_or_future_active_identity(
    monkeypatch, validity_metadata
):
    assert not is_current(validity_metadata, now=datetime(2026, 11, 3, tzinfo=timezone.utc))
    active_row = _row(ACTIVE_ENTRY_ID, status="active", metadata=validity_metadata)
    active_read, superseded_read, _, policy_sync = _patch_fakes(
        monkeypatch, active_row=active_row
    )
    old_content = (
        "---\ndescription: Prior preference\n"
        + yaml.safe_dump(validity_metadata, sort_keys=False)
        + "---\nPrior preference.\n"
    )
    store = _source_store(old_content=old_content)

    result = await approval.approve_proposal(
        proposal=PROPOSAL,
        store=store,
        owner_id=USER_ID,
        approval_source="manual",
    )

    active_read.assert_awaited_once_with(
        user_id=USER_ID, tier="user", entry_key=OLD_KEY
    )
    superseded_read.assert_not_awaited()
    detail = store.entries[((USER_ID, "memory"), result["detail_key"])]
    frontmatter = yaml.safe_load(detail["content"].split("---\n", 2)[1])
    assert frontmatter["source_entry_id"] == ACTIVE_ENTRY_ID
    policy_sync.assert_awaited_once()
    assert policy_sync.await_args.kwargs["replaced_entry_ids"] == (ACTIVE_ENTRY_ID,)


async def test_retry_collision_keeps_active_status_identity_when_store_lifecycle_is_retired(
    monkeypatch,
):
    # A failed projection can leave an active-status row whose metadata already
    # says superseded. The retry must preserve that row's ID instead of falling
    # back to another superseded row for the same entry key.
    active_row = _row(
        ACTIVE_ENTRY_ID,
        status="active",
        metadata={"lifecycle": "wrong"},
    )
    active_read, superseded_read, _, policy_sync = _patch_fakes(
        monkeypatch, active_row=active_row
    )
    proposal = {**PROPOSAL, "text": "Use English by default."}
    detail_key = approval._detail_key(proposal)
    existing_new_content = approval._detail_content(
        proposal,
        now="2026-11-02T00:00:00+00:00",
        source_entry_id=ACTIVE_ENTRY_ID,
        lifecycle="active",
    )["content"]
    old_content = "---\ndescription: Prior preference\nlifecycle: wrong\n---\nPrior preference.\n"
    store = _source_store(
        old_content=old_content,
        new_content=existing_new_content,
        new_key=detail_key,
    )

    result = await approval.approve_proposal(
        proposal=proposal,
        store=store,
        owner_id=USER_ID,
        approval_source="manual",
    )

    assert result["detail_key"] == detail_key
    active_read.assert_awaited_once_with(
        user_id=USER_ID, tier="user", entry_key=OLD_KEY
    )
    superseded_read.assert_not_awaited()
    detail = store.entries[((USER_ID, "memory"), detail_key)]
    frontmatter = yaml.safe_load(detail["content"].split("---\n", 2)[1])
    assert frontmatter["source_entry_id"] == ACTIVE_ENTRY_ID
    policy_sync.assert_awaited_once()
    assert policy_sync.await_args.kwargs["replaced_entry_ids"] == (ACTIVE_ENTRY_ID,)
