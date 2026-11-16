"""Fake-only safety checks for the memory forget cascade.

These tests keep Store, projection, proposal queue, and federation behind local
fakes. They never need a database, credentials, or provider transport.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ptc_agent.agent.backends import store_label
from src.server.services.refactor import memory_forget as forget

pytestmark = pytest.mark.asyncio

OWNER = "11111111-1111-4111-8111-111111111111"
OTHER_OWNER = "22222222-2222-4222-8222-222222222222"
ENTRY = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CHILD = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
SOURCE_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
SOURCE_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2"
MERGED = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
NS = (OWNER, "memory")
BODY = "Private remembered detail that must not survive."


def _row(entry_id=ENTRY, *, key="fact.md", namespace=NS, source_entry_id=None):
    return {
        "entry_id": entry_id,
        "user_id": OWNER,
        "tier": "user",
        "namespace": list(namespace),
        "entry_key": key,
        "content_hash": "sha256:fixture-fingerprint",
        "source_entry_id": source_entry_id,
    }


class FakeStore:
    def __init__(self, items=None, *, fail_delete_keys=(), events=None):
        self.items = {
            (tuple(namespace), key): SimpleNamespace(value=value)
            for (namespace, key), value in (items or {}).items()
        }
        self.fail_delete_keys = set(fail_delete_keys)
        self.calls = []
        self.events = events if events is not None else []

    async def aget(self, namespace, key, **kwargs):
        self.calls.append(("get", tuple(namespace), key))
        self.events.append("store:get")
        return self.items.get((tuple(namespace), key))

    async def asearch(self, namespace, *, limit=10, offset=0, **kwargs):
        prefix = tuple(namespace)
        items = [
            SimpleNamespace(key=key, namespace=ns, value=item.value)
            for (ns, key), item in self.items.items()
            if ns[:len(prefix)] == prefix
        ]
        return items[offset : offset + limit]

    async def adelete(self, namespace, key, **kwargs):
        self.calls.append(("delete", tuple(namespace), key))
        self.events.append("store:delete")
        if key in self.fail_delete_keys:
            raise RuntimeError("fixture store failure")
        self.items.pop((tuple(namespace), key), None)

    async def aput(self, namespace, key, value, **kwargs):
        self.calls.append(("put", tuple(namespace), key))
        self.events.append("store:put")
        self.items[(tuple(namespace), key)] = SimpleNamespace(value=value)


def _install_db(monkeypatch, rows, *, fail_erase=False, fail_policy_purge=False):
    active_rows = list(rows)
    tombstones = []
    deleted = []
    events = []
    proposal_keys_deleted = []

    async def closure(*, user_id, entry_id):
        events.append("closure")
        # The fake enforces the owner predicate at the boundary, as SQL must.
        if user_id != OWNER or entry_id != ENTRY:
            return []
        if entry_id != ENTRY or user_id != OWNER:
            return []
        return list(active_rows)

    async def record(*, user_id, rows, root_entry_id, reason):
        events.append("tombstones")
        tombstones.extend(
            {**dict(row), "user_id": user_id, "root_entry_id": root_entry_id, "reason": reason}
            for row in rows
        )
        return len(rows)

    async def get_tombstone(*, user_id, entry_id):
        return next(
            (row for row in tombstones if row["user_id"] == user_id and row["entry_id"] == entry_id),
            None,
        )

    async def get_tombstone_group(*, user_id, root_entry_id):
        return [
            row for row in tombstones
            if row["user_id"] == user_id and row["root_entry_id"] == root_entry_id
        ]

    async def match_locators(*, user_id, root_entry_id, candidates):
        expected_group = await get_tombstone_group(
            user_id=user_id, root_entry_id=root_entry_id
        )
        matches = []
        for candidate in candidates:
            for row in expected_group:
                if (
                    tuple(candidate["namespace"]) == tuple(row["namespace"])
                    and candidate["entry_key"] == row["entry_key"]
                ):
                    matches.append({**candidate, "entry_id": row["entry_id"]})
        return matches

    async def erase(*, user_id, rows, proposal_keys=()):
        events.append("projection")
        if fail_erase:
            raise RuntimeError("fixture projection failure")
        deleted.extend((user_id, dict(row)) for row in rows)
        proposal_keys_deleted.extend(proposal_keys)
        active_rows[:] = [row for row in active_rows if row not in rows]
        return {"entries": len(rows), "proposals": len(proposal_keys)}

    async def purge_policy(*, user_id, entry_ids):
        expected = set(entry_ids)
        tombstoned = {
            row["entry_id"]
            for row in tombstones
            if row["user_id"] == user_id
        }
        assert expected <= tombstoned
        events.append("policy_purge:" + ",".join(sorted(expected)))
        if fail_policy_purge:
            raise RuntimeError("fixture policy cleanup failure")
        return len(expected)

    async def get_by_ids(*, user_id, entry_ids):
        events.append("projection_retry")
        wanted = set(entry_ids)
        return [
            row for row in active_rows if str(row.get("entry_id") or "") in wanted
        ]

    monkeypatch.setattr(forget.db_memory, "get_forget_closure", closure)
    monkeypatch.setattr(forget.db_memory, "get_entries_by_ids", get_by_ids)
    @asynccontextmanager
    async def owner_lock(user_id):
        assert user_id in {OWNER, OTHER_OWNER}
        events.append("owner_lock:enter")
        try:
            yield
        finally:
            events.append("owner_lock:exit")

    monkeypatch.setattr(forget.memory_revisions, "owner_memory_lock", owner_lock)
    monkeypatch.setattr(
        forget.db_memory, "get_forget_tombstone", get_tombstone
    )
    monkeypatch.setattr(forget.db_memory, "get_forget_tombstone_group", get_tombstone_group)
    monkeypatch.setattr(forget.db_memory, "match_forget_tombstone_locators", match_locators)
    monkeypatch.setattr(forget.db_memory, "record_forget_tombstones", record)
    monkeypatch.setattr(forget.db_memory, "forget_entry_rows", erase)
    monkeypatch.setattr(
        forget.db_tool_policy, "purge_forgotten_denials", purge_policy
    )
    monkeypatch.setattr(
        forget.db_memory, "record_forget_provider_notices", AsyncMock()
    )
    monkeypatch.setattr(
        forget,
        "_notify_federation",
        AsyncMock(return_value={"memfw": "read_only"}),
    )
    return tombstones, deleted, events, proposal_keys_deleted


async def test_other_owner_identifier_cannot_mutate_any_carrier(monkeypatch):
    tombstones, deleted, _, _ = _install_db(monkeypatch, [_row()])
    store = FakeStore({(NS, "fact.md"): {"content": BODY}})

    result = await forget.forget_memory_entry(
        user_id=OTHER_OWNER,
        entry_id=ENTRY,
        store=store,
        reason=forget.ForgetReason.USER_REQUEST,
    )

    assert result.status == "not_found"
    assert result.carriers["store"] == "not_found"
    assert store.calls == []
    assert tombstones == []
    assert deleted == []


async def test_cycle_closure_erases_only_named_details_and_pointer_lines(monkeypatch):
    rows = [
        _row(source_entry_id=CHILD),
        _row(CHILD, key="derived.md", source_entry_id=ENTRY),
        # The back-edge must not make the service loop or delete unrelated content.
    ]
    tombstones, deleted, events, proposal_keys = _install_db(monkeypatch, rows)
    index = (
        "- [Target](fact.md) — target summary\n"
        "- [Target [review]](fact.md) — bracket title alias\n"
        "- [Target alias](./fact.md) — alias summary\n"
        "- [Legacy target](proposed/fact.md) — legacy summary\n"
        "- [Derived](derived.md) — derived summary\n"
        "- [Legacy derived](proposed/derived.md) — old derived summary\n"
        "- [Keep](keep.md) — unrelated summary\n"
    )
    store = FakeStore(
        {
            (NS, "fact.md"): {"content": BODY},
            (NS, "derived.md"): {"content": "Derived body"},
            (NS, "keep.md"): {"content": "Keep body"},
            (NS, "memory.md"): {"content": index},
        }, events=events
    )

    result = await forget.forget_memory_entry(
        user_id=OWNER,
        entry_id=ENTRY,
        store=store,
        reason=forget.ForgetReason.USER_REQUEST,
    )

    # A read-only federated source still holds its own copy, so global status
    # must remain partial even after all local carriers were erased.
    assert result.status == "partial"
    assert (NS, "fact.md") not in store.items
    assert (NS, "derived.md") not in store.items
    assert store.items[(NS, "keep.md")].value["content"] == "Keep body"
    cleaned_index = store.items[(NS, "memory.md")].value["content"]
    assert "Target" not in cleaned_index
    assert "bracket title alias" not in cleaned_index
    assert "Legacy target" not in cleaned_index
    assert "Derived" not in cleaned_index
    assert "Legacy derived" not in cleaned_index
    assert "keep.md" in cleaned_index
    assert len(tombstones) == 2
    assert len(deleted) == 2
    assert set(proposal_keys) == {"fact", "derived"}
    for tombstone in tombstones:
        assert BODY not in repr(tombstone)
        assert "index_text" not in tombstone
        assert "provenance" not in tombstone
    assert "read_only" in result.carriers.values()
    assert events.index("tombstones") < events.index("store:put")
    assert events.index("owner_lock:enter") < events.index("closure")
    assert events.index("projection") < events.index("owner_lock:exit")
    assert events.index("tombstones") < next(
        index for index, event in enumerate(events) if event.startswith("policy_purge:")
    )
    assert events.index("store:delete") < events.index("projection")
    assert "policy_purge:" + ",".join(sorted([ENTRY, CHILD])) in events


async def test_forget_strips_generated_revision_suffix_for_queue_and_legacy_key(monkeypatch):
    detail_key = "fact--r123e4567-e89b-12d3-a456-426614174000.md"
    tombstones, deleted, _, proposal_keys = _install_db(
        monkeypatch, [_row(key=detail_key)]
    )
    monkeypatch.setattr(
        forget, "_notify_federation", AsyncMock(return_value={"memfw": "deleted"})
    )
    store = FakeStore({
        (NS, detail_key): {"content": BODY},
        (NS, "proposed/fact.md"): {"content": "pending"},
        (NS, "memory.md"): {
            "content": "- [Fact](proposed/fact.md) — fact\n"
        },
    })

    result = await forget.forget_memory_entry(
        user_id=OWNER,
        entry_id=ENTRY,
        store=store,
        reason=forget.ForgetReason.USER_REQUEST,
    )

    assert result.status == "completed"
    assert proposal_keys == ["fact"]
    assert (NS, "proposed/fact.md") not in store.items
    assert tombstones and deleted


async def test_forget_reports_absent_queue_rows_truthfully(monkeypatch):
    _install_db(monkeypatch, [_row()])
    monkeypatch.setattr(
        forget, "_notify_federation", AsyncMock(return_value={"memfw": "deleted"})
    )
    monkeypatch.setattr(
        forget.db_memory,
        "forget_entry_rows",
        AsyncMock(return_value={"entries": 1, "proposals": 0}),
    )
    store = FakeStore({(NS, "fact.md"): {"content": BODY}})

    result = await forget.forget_memory_entry(
        user_id=OWNER,
        entry_id=ENTRY,
        store=store,
        reason=forget.ForgetReason.USER_REQUEST,
    )

    assert result.status == "completed"
    assert result.carriers["proposals"] == "absent"


async def test_forget_keeps_ordinary_double_hyphen_r_key_mapping():
    store = FakeStore({
        (NS, "notes--review.md"): {"content": "approved note"},
        (NS, "proposed/notes.md"): {"content": "unrelated proposal"},
    })
    await forget._erase_store_rows(store, [_row(key="notes--review.md")])
    assert (NS, "notes--review.md") not in store.items
    assert (NS, "proposed/notes.md") in store.items


async def test_forgetting_either_consolidation_parent_erases_merged_revision(monkeypatch):
    rows = [
        _row(SOURCE_A, key="source-a.md"),
        _row(SOURCE_B, key="source-b.md"),
        _row(MERGED, key="merged.md", source_entry_id=SOURCE_A),
    ]
    lineage = {MERGED: {SOURCE_A, SOURCE_B}}
    _install_db(monkeypatch, rows)

    async def closure(*, user_id, entry_id):
        if user_id != OWNER:
            return []
        by_id = {row["entry_id"]: row for row in rows}
        found = {entry_id} if entry_id in by_id else set()
        frontier = list(found)
        while frontier:
            parent = frontier.pop()
            for row in rows:
                child = row["entry_id"]
                follows_single = row.get("source_entry_id") == parent
                follows_lineage = parent in lineage.get(child, set())
                if child not in found and (follows_single or follows_lineage):
                    found.add(child)
                    frontier.append(child)
        return [by_id[item] for item in found]

    monkeypatch.setattr(forget.db_memory, "get_forget_closure", closure)
    for root, removed_key in ((SOURCE_A, "source-a.md"), (SOURCE_B, "source-b.md")):
        store = FakeStore({
            (NS, "source-a.md"): {"content": "source a"},
            (NS, "source-b.md"): {"content": "source b"},
            (NS, "merged.md"): {"content": "merged"},
            (NS, "memory.md"): {
                "content": "- [A](source-a.md) — a\n- [B](source-b.md) — b\n"
                "- [Merged](merged.md) — merged\n"
            },
        })

        await forget.forget_memory_entry(
            user_id=OWNER,
            entry_id=root,
            store=store,
            reason=forget.ForgetReason.USER_REQUEST,
        )

        assert (NS, "merged.md") not in store.items
        assert (NS, removed_key) not in store.items


async def test_store_failure_keeps_projection_hidden_and_reports_partial(monkeypatch):
    tombstones, deleted, _, _ = _install_db(monkeypatch, [_row()])
    store = FakeStore(
        {(NS, "fact.md"): {"content": BODY}}, fail_delete_keys={"fact.md"}
    )

    result = await forget.forget_memory_entry(
        user_id=OWNER,
        entry_id=ENTRY,
        store=store,
        reason=forget.ForgetReason.CORRECTION,
    )

    assert result.status == "partial"
    assert result.carriers["store"] == "failed"
    assert result.carriers["projection"] == "pending"
    assert len(tombstones) == 1
    assert deleted == []
    assert (NS, "fact.md") in store.items


async def test_retry_completes_after_transient_store_failure(monkeypatch):
    tombstones, deleted, _, _ = _install_db(monkeypatch, [_row()])
    store = FakeStore(
        {(NS, "fact.md"): {"content": BODY}}, fail_delete_keys={"fact.md"}
    )
    first = await forget.forget_memory_entry(
        user_id=OWNER, entry_id=ENTRY, store=store,
        reason=forget.ForgetReason.USER_REQUEST,
    )
    store.fail_delete_keys.clear()
    second = await forget.forget_memory_entry(
        user_id=OWNER, entry_id=ENTRY, store=store,
        reason=forget.ForgetReason.USER_REQUEST,
    )

    assert first.status == "partial"
    assert second.status == "partial"  # MemFW remains read-only.
    assert (NS, "fact.md") not in store.items
    assert len(tombstones) >= 1
    assert deleted


async def test_list_index_content_and_tombstone_retry_remove_bracket_title_pointer(monkeypatch):
    tombstones, _, _, _ = _install_db(monkeypatch, [_row()])
    index_lines = [
        "# Memories",
        "- [Fact [review]](fact.md) — private detail",
        "- [Title [nested](safe.md) more](fact.md) — ambiguous pointer",
        "- [Keep](keep.md)",
    ]
    store = FakeStore(
        {
            (NS, "fact.md"): {"content": BODY},
            (NS, "keep.md"): {"content": "unrelated"},
            (NS, "memory.md"): {"content": index_lines},
        },
    )
    first = await forget.forget_memory_entry(
        user_id=OWNER, entry_id=ENTRY, store=store,
        reason=forget.ForgetReason.USER_REQUEST,
    )
    assert first.status == "partial"
    # Reintroduce a stale reference to exercise the retry path's owner scan.
    store.items[(NS, "memory.md")].value["content"] = index_lines
    store.items[(NS, "proposed/fact.md")] = SimpleNamespace(
        value={"content": "stale proposal copy"}
    )
    second = await forget.forget_memory_entry(
        user_id=OWNER, entry_id=ENTRY, store=store,
        reason=forget.ForgetReason.USER_REQUEST,
    )

    assert second.status == "partial"  # The configured provider remains read-only.
    assert (NS, "fact.md") not in store.items
    assert (NS, "proposed/fact.md") not in store.items
    cleaned = store.items[(NS, "memory.md")].value["content"]
    assert isinstance(cleaned, str)
    assert "private detail" not in cleaned
    assert "ambiguous pointer" not in cleaned
    assert "keep.md" in cleaned
    assert tombstones


@pytest.mark.parametrize("scan_mode", ["failed", "truncated"])
async def test_tombstone_retry_scan_failure_stays_partial(monkeypatch, scan_mode):
    _install_db(monkeypatch, [_row()])
    monkeypatch.setattr(
        forget, "_notify_federation", AsyncMock(return_value={"remote": "deleted"})
    )
    stale_lines = ["- [Fact [review]](fact.md) — private detail"]
    store = FakeStore({
        (NS, "fact.md"): {"content": BODY},
        (NS, "memory.md"): {"content": stale_lines},
    })
    first = await forget.forget_memory_entry(
        user_id=OWNER, entry_id=ENTRY, store=store,
        reason=forget.ForgetReason.USER_REQUEST,
    )
    assert first.status == "completed"
    store.items[(NS, "memory.md")].value["content"] = stale_lines
    if scan_mode == "failed":
        store.asearch = AsyncMock(side_effect=RuntimeError("scan unavailable"))
    else:
        store.asearch = AsyncMock(
            return_value=[SimpleNamespace() for _ in range(forget.MAX_FORGET_ROWS + 1)]
        )

    result = await forget.forget_memory_entry(
        user_id=OWNER, entry_id=ENTRY, store=store,
        reason=forget.ForgetReason.USER_REQUEST,
    )

    assert result.status == "partial"
    assert result.carriers["store"] == "failed"
    assert store.items[(NS, "memory.md")].value["content"] == stale_lines


async def test_unsupported_initial_index_shape_cannot_report_completed(monkeypatch):
    _install_db(monkeypatch, [_row()])
    monkeypatch.setattr(
        forget, "_notify_federation", AsyncMock(return_value={"remote": "deleted"})
    )
    store = FakeStore({
        (NS, "fact.md"): {"content": BODY},
        (NS, "memory.md"): {"content": {"unexpected": "object"}},
    })

    result = await forget.forget_memory_entry(
        user_id=OWNER, entry_id=ENTRY, store=store,
        reason=forget.ForgetReason.USER_REQUEST,
    )

    assert result.status == "partial"
    assert result.carriers["store"] == "failed"
    assert (NS, "fact.md") in store.items


async def test_tombstone_retry_chunks_bounded_locator_candidates(monkeypatch):
    tombstones, _, _, _ = _install_db(monkeypatch, [_row()])
    monkeypatch.setattr(
        forget, "_notify_federation", AsyncMock(return_value={"remote": "deleted"})
    )
    store = FakeStore({
        (NS, "fact.md"): {"content": BODY},
        (NS, "memory.md"): {"content": "- [Fact](fact.md)\n"},
    })
    await forget.forget_memory_entry(
        user_id=OWNER, entry_id=ENTRY, store=store,
        reason=forget.ForgetReason.USER_REQUEST,
    )

    pointer_lines = [f"- [Other {i}](other-{i}.md)" for i in range(130)]
    pointer_lines.append("- [Fact](fact.md)")
    store.items[(NS, "memory.md")].value["content"] = pointer_lines
    for index in range(130):
        store.items[(NS, f"other-{index}.md")] = SimpleNamespace(
            value={"content": "unrelated"}
        )
    batch_sizes = []

    async def match_locators(*, user_id, root_entry_id, candidates):
        batch_sizes.append(len(candidates))
        return [
            {**candidate, "entry_id": row["entry_id"]}
            for candidate in candidates
            for row in tombstones
            if tuple(candidate["namespace"]) == tuple(row["namespace"])
            and candidate["entry_key"] == row["entry_key"]
        ]

    monkeypatch.setattr(forget.db_memory, "match_forget_tombstone_locators", match_locators)
    result = await forget.forget_memory_entry(
        user_id=OWNER, entry_id=ENTRY, store=store,
        reason=forget.ForgetReason.USER_REQUEST,
    )

    assert result.carriers["store"] == "deleted"
    assert len(batch_sizes) == 2
    assert max(batch_sizes) <= 256
    assert (NS, "memory.md") in store.items


async def test_retry_scan_uses_escaped_oss_store_label(monkeypatch):
    user_id = "local.user@example.test"
    namespace = (store_label(user_id), "memory")
    store = FakeStore({(namespace, "memory.md"): {"content": ""}})
    searched = []
    original_search = store.asearch

    async def record_search(prefix, *, limit, offset, **kwargs):
        searched.append(tuple(prefix))
        return await original_search(prefix, limit=limit, offset=offset, **kwargs)

    store.asearch = record_search
    monkeypatch.setattr(
        forget.db_memory,
        "match_forget_tombstone_locators",
        AsyncMock(return_value=[]),
    )

    cleaned = await forget._retry_store_cleanup(
        user_id=user_id,
        root_entry_id=ENTRY,
        store=store,
        tombstones=[{"entry_id": ENTRY}],
    )

    assert cleaned is True
    assert searched == [(store_label(user_id),)]


async def test_policy_purge_failure_is_partial_and_tombstone_retry_retries_it(monkeypatch):
    tombstones, _, _, _ = _install_db(
        monkeypatch, [_row()], fail_policy_purge=True
    )
    store = FakeStore({(NS, "fact.md"): {"content": BODY}})

    first = await forget.forget_memory_entry(
        user_id=OWNER,
        entry_id=ENTRY,
        store=store,
        reason=forget.ForgetReason.USER_REQUEST,
    )
    monkeypatch.setattr(
        forget.db_tool_policy,
        "purge_forgotten_denials",
        AsyncMock(return_value=1),
    )
    second = await forget.forget_memory_entry(
        user_id=OWNER,
        entry_id=ENTRY,
        store=store,
        reason=forget.ForgetReason.USER_REQUEST,
    )

    assert first.status == "partial"
    assert first.carriers["tool_policy"] == "failed"
    assert second.carriers["tool_policy"] == "deleted"
    assert len(tombstones) == 1


async def test_retry_replays_provider_notice_for_entire_tombstone_group(monkeypatch):
    rows = [
        _row(),
        _row(CHILD, key="derived.md", source_entry_id=ENTRY),
    ]
    tombstones, deleted, _, _ = _install_db(monkeypatch, rows)
    store = FakeStore({
        (NS, "fact.md"): {"content": BODY},
        (NS, "derived.md"): {"content": "Derived private detail"},
    })
    calls = []

    async def notify(*, user_id, entry_id, content_hash):
        calls.append((entry_id, content_hash))
        # The descendant provider notice fails on the first attempt. The retry
        # starts from a now-empty projection, so only loading the tombstone
        # group can ensure that descendant is retried.
        if entry_id == CHILD and calls.count((entry_id, content_hash)) == 1:
            return {"remote": "unavailable"}
        return {"remote": "deleted"}

    monkeypatch.setattr(forget, "_notify_federation", notify)
    first = await forget.forget_memory_entry(
        user_id=OWNER, entry_id=ENTRY, store=store,
        reason=forget.ForgetReason.USER_REQUEST,
    )
    second = await forget.forget_memory_entry(
        user_id=OWNER, entry_id=ENTRY, store=store,
        reason=forget.ForgetReason.USER_REQUEST,
    )

    assert first.status == "partial"
    assert second.status == "completed"
    assert len(deleted) == 2
    assert [entry_id for entry_id, _ in calls].count(ENTRY) == 2
    assert [entry_id for entry_id, _ in calls].count(CHILD) == 2
    assert len(tombstones) == 2
    assert all(row["root_entry_id"] == ENTRY for row in tombstones)


async def test_projection_failure_after_store_delete_stays_partial(monkeypatch):
    tombstones, deleted, _, _ = _install_db(
        monkeypatch, [_row()], fail_erase=True
    )
    store = FakeStore({(NS, "fact.md"): {"content": BODY}})

    result = await forget.forget_memory_entry(
        user_id=OWNER,
        entry_id=ENTRY,
        store=store,
        reason=forget.ForgetReason.USER_REQUEST,
    )

    assert result.status == "partial"
    assert result.carriers["store"] == "deleted"
    assert result.carriers["projection"] == "failed"
    assert result.carriers["proposals"] == "failed"
    assert (NS, "fact.md") not in store.items
    assert tombstones
    assert deleted == []


async def test_foreign_namespace_in_projection_row_is_never_deleted(monkeypatch):
    foreign_ns = (OTHER_OWNER, "memory")
    tombstones, deleted, _, _ = _install_db(
        monkeypatch, [_row(namespace=foreign_ns)]
    )
    store = FakeStore({(foreign_ns, "fact.md"): {"content": BODY}})

    try:
        result = await forget.forget_memory_entry(
            user_id=OWNER,
            entry_id=ENTRY,
            store=store,
            reason=forget.ForgetReason.USER_REQUEST,
        )
    except ValueError:
        # A corrupted projection row is rejected before any mutation.
        pass
    else:
        assert result.status != "completed"
    assert store.calls == []
    assert (foreign_ns, "fact.md") in store.items
    assert tombstones == []
    assert deleted == []


async def test_federation_deleted_ack_is_preserved_in_notice_status(monkeypatch):
    row = _row()
    monkeypatch.setattr(
        forget,
        "_notify_federation",
        AsyncMock(return_value={"conductor_pg": "deleted", "memfw": "read_only"}),
    )
    notices = AsyncMock()
    monkeypatch.setattr(forget.db_memory, "record_forget_provider_notices", notices)

    result = await forget._notify_rows(OWNER, [row])

    assert result["federation:conductor_pg:" + ENTRY] == "deleted"
    assert result["federation:memfw:" + ENTRY] == "read_only"
    assert notices.await_args.kwargs["notices"] == [
        {"entry_id": ENTRY, "provider": "conductor_pg", "status": "deleted"},
        {"entry_id": ENTRY, "provider": "memfw", "status": "read_only"},
    ]


async def test_retry_after_failed_projection_purges_it_before_completion(monkeypatch):
    """A tombstone-only retry must finish a projection purge it inherited."""
    rows = [_row()]
    tombstones, deleted, events, _keys = _install_db(monkeypatch, rows)
    tombstones.append(
        {**rows[0], "user_id": OWNER, "root_entry_id": ENTRY, "reason": "user_request"}
    )

    async def no_closure(**_kwargs):
        return []

    monotpatch_rows = list(rows)

    async def get_by_ids(*, user_id, entry_ids):
        assert user_id == OWNER
        return monotpatch_rows

    monkeypatch.setattr(forget.db_memory, "get_forget_closure", no_closure)
    monkeypatch.setattr(forget.db_memory, "get_entries_by_ids", get_by_ids)
    monkeypatch.setattr(
        forget, "_notify_federation", AsyncMock(return_value={"memfw": "deleted"})
    )
    store = FakeStore({})  # canonical copies already erased

    result = await forget.forget_memory_entry(
        user_id=OWNER,
        entry_id=ENTRY,
        store=store,
        reason=forget.ForgetReason.USER_REQUEST,
    )

    assert result.carriers["projection"] == "deleted"
    assert result.carriers["proposals"] in ("deleted", "absent")
    assert len(deleted) == 1
    assert result.status == "completed"


async def test_retry_projection_purge_failure_stays_partial(monkeypatch):
    rows = [_row()]
    tombstones, _deleted, _events, _keys = _install_db(monkeypatch, rows)
    tombstones.append(
        {**rows[0], "user_id": OWNER, "root_entry_id": ENTRY, "reason": "user_request"}
    )

    async def no_closure(**_kwargs):
        return []

    async def broken_by_ids(**_kwargs):
        raise RuntimeError("projection backend down")

    monkeypatch.setattr(forget.db_memory, "get_forget_closure", no_closure)
    monkeypatch.setattr(forget.db_memory, "get_entries_by_ids", broken_by_ids)
    monkeypatch.setattr(
        forget, "_notify_federation", AsyncMock(return_value={"memfw": "deleted"})
    )
    store = FakeStore({})

    result = await forget.forget_memory_entry(
        user_id=OWNER,
        entry_id=ENTRY,
        store=store,
        reason=forget.ForgetReason.USER_REQUEST,
    )

    assert result.carriers["projection"] == "failed"
    assert result.status == "partial"
