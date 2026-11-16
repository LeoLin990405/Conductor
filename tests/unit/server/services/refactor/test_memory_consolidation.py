from __future__ import annotations

import hashlib
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.server.contracts.memory_policy_metadata import normalize_memory_policy_metadata
from src.config.features import FeatureGate
from src.server.services.refactor import memory_consolidation as consolidation
from src.server.services.refactor.memory_projection import content_hash
from src.server.utils.pg_sanitize import refactor_owner_uuid


class FakeStore:
    def __init__(self, values):
        self.values = values
        self.reads = []
        self.writes = []

    async def aget(self, namespace, key):
        self.reads.append((tuple(namespace), key))
        value = self.values.get((tuple(namespace), key))
        return SimpleNamespace(value={"content": value}) if value is not None else None

    async def aput(self, *args, **kwargs):
        self.writes.append((args, kwargs))
        raise AssertionError("consolidation must never write the Store")


@pytest.fixture(autouse=True)
def automatic_memory_on(monkeypatch):
    monkeypatch.setattr(
        consolidation, "automatic_memory_enabled", AsyncMock(return_value=True)
    )


def _frontmatter(body: str, *, sensitivity="private", condition="when travelling") -> str:
    return (
        "---\n"
        f"sensitivity: {sensitivity}\n"
        f"applies_when: {condition}\n"
        "evidence_quote_ref:\n"
        "  message_id: msg-7\n"
        "  start: 2\n"
        "  end: 8\n"
        "  unit: unicode_codepoint\n"
        "---\n"
        f"{body}"
    )


def _group(contents, *, kind="duplicate", policies=None):
    keys = [f"detail-{index}.md" for index in range(len(contents))]
    source_ids = [f"00000000-0000-0000-0000-{index + 1:012d}" for index in range(len(contents))]
    metadata = policies or [
        normalize_memory_policy_metadata(
            {
                "sensitivity": "private",
                "applies_when": "when travelling",
                "evidence_quote_ref": {
                    "message_id": "msg-7",
                    "start": 2,
                    "end": 8,
                    "unit": "unicode_codepoint",
                },
            }
        )
        for _ in contents
    ]
    hashes = [content_hash(content) for content in contents]
    return {
        "user_id": refactor_owner_uuid("owner-label"),
        "namespace": ["owner-label", "memory"],
        "group_kind": kind,
        "group_ref": "group-identity",
        "source_ids": source_ids,
        "source_keys": keys,
        "source_hashes": hashes,
        "source_signature": ",".join(
            f"{source_id}:{digest}"
            for source_id, digest in zip(source_ids, hashes, strict=True)
        ),
        "source_metadata": metadata,
    }, keys


@pytest.mark.parametrize(
    "body",
    (
        "  indented first line\ntrailing spaces  ",
        "\n\n  indented after blank lines  \n\n",
        "\r\n  CRLF body with trailing spaces  \r\n",
    ),
)
def test_frontmatter_split_preserves_exact_body_bytes(body):
    content = "---\r\nsensitivity: private\r\napplies_when: travelling\r\n---\r\n" + body

    parsed = consolidation._body_and_policy(content)

    assert parsed is not None
    assert parsed[0] == body


@pytest.mark.asyncio
async def test_exact_duplicate_proposal_preserves_body_and_policy_and_only_reads():
    body = "## Preference\n\n  Keep this spacing.\n"
    content = _frontmatter(body)
    group, keys = _group([content, content])
    store = FakeStore(
        {
            (("owner-label", "memory"), key): content
            for key in keys
        }
    )

    rows = await consolidation._proposals_for_group(store=store, group=group)

    assert len(rows) == 1
    proposal = rows[0]
    assert proposal["text"] == body
    assert proposal["source_entry_ids"] == group["source_ids"]
    assert proposal["metadata"]["applies_when"] == "when travelling"
    assert proposal["metadata"]["evidence_quote_ref"]["unit"] == "unicode_codepoint"
    assert proposal["origin"] == "assistant_inference"
    assert proposal["evidence_refs"] == []
    assert proposal["producer_metadata"]["kind"] == "duplicate"
    assert proposal["producer_metadata"]["source_body_hashes"] == [
        hashlib.sha256(body.encode("utf-8")).hexdigest()
    ] * 2
    assert len(store.reads) == 2
    assert store.writes == []


@pytest.mark.asyncio
async def test_conflict_returns_only_observed_bodies_and_audits_choices():
    first = _frontmatter("A\n")
    second = _frontmatter("B\n")
    group, keys = _group([first, second], kind="conflict")
    store = FakeStore(
        {
            (("owner-label", "memory"), keys[0]): first,
            (("owner-label", "memory"), keys[1]): second,
        }
    )

    rows = await consolidation._proposals_for_group(store=store, group=group)

    assert [row["text"] for row in rows] == ["A\n", "B\n"]
    assert all(row["source_entry_ids"] == group["source_ids"] for row in rows)
    assert [row["producer_metadata"]["choice_index"] for row in rows] == [0, 1]
    assert all(row["producer_metadata"]["choice_count"] == 2 for row in rows)
    assert rows[0]["text_hash"] != rows[1]["text_hash"]
    assert store.writes == []


@pytest.mark.asyncio
async def test_tool_constraint_entries_are_never_consolidation_candidates():
    content = _frontmatter("Do not call the external transfer tool.\n")
    group, keys = _group([content, content])
    group["source_metadata"] = [
        {"kind": "constraint", "denied_tools": ["external_transfer"], "sensitivity": "private"},
        {"kind": "constraint", "denied_tools": ["external_transfer"], "sensitivity": "private"},
    ]
    store = FakeStore(
        {(('owner-label', 'memory'), key): content for key in keys}
    )

    assert await consolidation._proposals_for_group(store=store, group=group) == []


@pytest.mark.asyncio
async def test_run_before_utc_cutoff_does_not_enumerate_or_claim(monkeypatch):
    import src.config.features as features

    monkeypatch.setattr(consolidation, "refactor_flag_enabled", lambda key: True)
    monkeypatch.setattr(
        features,
        "system_flag",
        lambda key: SimpleNamespace(enabled=True, gate=FeatureGate.OPT_IN),
    )
    monkeypatch.setattr(consolidation.db_consolidation, "due_run_date", lambda: _async_result(None))

    async def should_not_list(**kwargs):
        raise AssertionError("before-cutoff run must not enumerate owners")

    monkeypatch.setattr(consolidation.db_consolidation, "list_opted_in_users", should_not_list)

    stats = await consolidation.run_due_memory_consolidations(store=FakeStore({}))

    assert stats == {"owners_claimed": 0, "groups_checked": 0, "proposals_created": 0}


@pytest.mark.asyncio
async def test_memory_off_does_not_claim_consolidation_runs(monkeypatch):
    monkeypatch.setattr(
        consolidation, "automatic_memory_enabled", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        consolidation.db_consolidation,
        "due_run_date",
        lambda: _async_result(date(2026, 11, 3)),
    )
    monkeypatch.setattr(
        consolidation.db_consolidation,
        "list_opted_in_users",
        lambda limit: _async_result(["owner-1"]),
    )
    claim = AsyncMock()
    monkeypatch.setattr(consolidation.db_consolidation, "claim_daily_run", claim)

    stats = await consolidation.run_due_memory_consolidations(store=FakeStore({}))

    assert stats["owners_claimed"] == 0
    claim.assert_not_awaited()


@pytest.mark.asyncio
async def test_memory_off_releases_claim_without_reading_candidates(monkeypatch):
    monkeypatch.setattr(
        consolidation, "automatic_memory_enabled", AsyncMock(return_value=False)
    )
    fail_run = AsyncMock()
    list_candidates = AsyncMock(side_effect=AssertionError("must not read candidates"))
    monkeypatch.setattr(consolidation.db_consolidation, "fail_run", fail_run)
    monkeypatch.setattr(
        consolidation.db_consolidation,
        "list_candidate_groups",
        list_candidates,
    )

    await consolidation._run_owner(
        store=FakeStore({}),
        run={
            "run_id": "run-1",
            "user_id": "owner-1",
            "raw_user_id": "owner-1",
            "attempts": 2,
        },
        stats={"groups_checked": 0, "proposals_created": 0},
        deadline=consolidation.asyncio.get_running_loop().time() + 10,
    )

    fail_run.assert_awaited_once_with(
        run_id="run-1",
        user_id="owner-1",
        attempt=2,
        error_code="memory_disabled",
    )
    list_candidates.assert_not_awaited()


@pytest.mark.asyncio
async def test_hash_drift_missing_store_row_secret_and_policy_mismatch_fail_closed():
    first = _frontmatter("Same\n")
    changed = _frontmatter("Changed\n")
    group, keys = _group([first, first])
    drifted = FakeStore(
        {
            (("owner-label", "memory"), keys[0]): first,
            ( ("owner-label", "memory"), keys[1]): changed,
        }
    )
    assert await consolidation._proposals_for_group(store=drifted, group=group) == []

    secret_policies = [
        normalize_memory_policy_metadata({"sensitivity": "secret"}),
        normalize_memory_policy_metadata({"sensitivity": "secret"}),
    ]
    secret_group, secret_keys = _group(
        [first, first], policies=secret_policies
    )
    secret_store = FakeStore(
        {
            ( ("owner-label", "memory"), secret_keys[0]): first,
            ( ("owner-label", "memory"), secret_keys[1]): first,
        }
    )
    assert await consolidation._proposals_for_group(store=secret_store, group=secret_group) == []

    mismatch = [group["source_metadata"][0], {"sensitivity": "secret"}]
    mismatch_group, mismatch_keys = _group([first, first], policies=mismatch)
    mismatch_store = FakeStore(
        {
            (("owner-label", "memory"), mismatch_keys[0]): first,
            (("owner-label", "memory"), mismatch_keys[1]): first,
        }
    )
    assert await consolidation._proposals_for_group(store=mismatch_store, group=mismatch_group) == []


@pytest.mark.asyncio
async def test_run_is_idempotent_per_day_and_owner_failures_are_isolated(monkeypatch):
    import src.config.features as features
    monkeypatch.setattr(consolidation, "refactor_flag_enabled", lambda key: True)
    monkeypatch.setattr(
        features,
        "system_flag",
        lambda key: SimpleNamespace(enabled=True, gate=FeatureGate.OPT_IN),
    )
    monkeypatch.setattr(
        consolidation.db_consolidation,
        "list_opted_in_users",
        lambda limit: _async_result(["owner-1", "owner-2"]),
    )
    monkeypatch.setattr(
        consolidation.db_consolidation,
        "due_run_date",
        lambda: _async_result(date(2026, 11, 3)),
    )
    claimed = []

    async def claim(*, user_id, run_date):
        assert isinstance(run_date, date)
        claimed.append(user_id)
        return {"run_id": user_id, "user_id": user_id, "cursor_key": "", "groups_processed": 0}

    monkeypatch.setattr(consolidation.db_consolidation, "claim_daily_run", claim)
    monkeypatch.setattr(
        consolidation.db_consolidation,
        "is_user_opted_in",
        lambda **kwargs: _async_result(True),
    )
    failed = []

    async def fail_run(**kwargs):
        failed.append(kwargs["user_id"])

    monkeypatch.setattr(consolidation.db_consolidation, "fail_run", fail_run)
    seen = []

    async def run_owner(*, store, run, stats, deadline):
        seen.append(run["user_id"])
        if run["user_id"] == "owner-1":
            raise RuntimeError("synthetic")

    monkeypatch.setattr(consolidation, "_run_owner", run_owner)

    stats = await consolidation.run_due_memory_consolidations(store=FakeStore({}))

    assert claimed == ["owner-1", "owner-2"]
    assert seen == ["owner-1", "owner-2"]
    assert failed == ["owner-1"]
    assert stats["owners_claimed"] == 2


@pytest.mark.asyncio
async def test_scheduler_scan_reaches_users_after_completed_owners(monkeypatch):
    import src.config.features as features
    users = [f"owner-{index}" for index in range(1, 62)]
    monkeypatch.setattr(consolidation, "refactor_flag_enabled", lambda key: True)
    monkeypatch.setattr(
        features,
        "system_flag",
        lambda key: SimpleNamespace(enabled=True, gate=FeatureGate.OPT_IN),
    )
    monkeypatch.setattr(
        consolidation.db_consolidation,
        "list_opted_in_users",
        lambda limit: _async_result(users),
    )
    monkeypatch.setattr(
        consolidation.db_consolidation,
        "due_run_date",
        lambda: _async_result(date(2026, 11, 3)),
    )
    monkeypatch.setattr(
        consolidation.db_consolidation,
        "is_user_opted_in",
        lambda **kwargs: _async_result(True),
    )
    attempts = []

    async def claim(*, user_id, run_date):
        attempts.append(user_id)
        if user_id != "owner-61":
            return None  # completed/backoff rows are harmless in a stale page
        return {
            "run_id": user_id,
            "user_id": user_id,
            "cursor_key": "",
            "groups_processed": 0,
        }

    async def run_owner(*, store, run, stats):
        return None

    monkeypatch.setattr(consolidation.db_consolidation, "claim_daily_run", claim)
    monkeypatch.setattr(consolidation, "_run_owner", run_owner)
    stats = await consolidation.run_due_memory_consolidations(store=FakeStore({}))

    assert attempts[-1] == "owner-61"
    assert len(attempts) == 61
    assert stats["owners_claimed"] == 1


async def _async_result(value):
    return value
