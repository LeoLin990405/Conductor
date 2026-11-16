"""Durable Router baseline snapshots fail closed across corruption and races."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from src.server.services.autonomy import router


def _checksum(value: dict) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _row(snapshot: dict, *, generation: int = 1, invalidated_at=None) -> dict:
    return {
        "snapshot_json": snapshot,
        "created_at": datetime.now(timezone.utc),
        "invalidated_at": invalidated_at,
        "generation": generation,
        "integrity_sha256": _checksum(snapshot),
    }


@pytest.mark.asyncio
async def test_snapshot_miss_saves_versioned_appendix_and_next_read_is_hit():
    expected = router._router_snapshot()
    state = {"row": None}

    async def get_snapshot(_user_id):
        return state["row"]

    async def save_snapshot(_user_id, snapshot_json, *, expected_generation):
        assert expected_generation == 0
        state["row"] = _row(snapshot_json)
        return True

    with (
        patch.object(router.router_db, "get_router_baseline_snapshot", get_snapshot),
        patch.object(router.router_db, "save_router_baseline_snapshot", save_snapshot),
    ):
        first = await router._load_router_baseline_snapshot("user-1")
        second = await router._load_router_baseline_snapshot("user-1")

    assert first == (expected, False)
    assert second == (expected, True)
    assert state["row"]["snapshot_json"]["schema_version"] == 1


@pytest.mark.asyncio
async def test_invalidation_between_read_and_cas_never_uses_stale_snapshot():
    stale_snapshot = {**router._router_snapshot(), "schema_version": 0}
    stale = _row(stale_snapshot, generation=4)
    state = {"row": stale}

    async def get_snapshot(_user_id):
        return state["row"]

    async def invalidate_before_save(_user_id, _snapshot, *, expected_generation):
        assert expected_generation == 4
        state["row"] = {
            **stale,
            "generation": 5,
            "invalidated_at": datetime.now(timezone.utc),
        }
        return False

    with (
        patch.object(router.router_db, "get_router_baseline_snapshot", get_snapshot),
        patch.object(
            router.router_db,
            "save_router_baseline_snapshot",
            invalidate_before_save,
        ),
    ):
        result = await router._load_router_baseline_snapshot("user-1")

    assert result is None


@pytest.mark.asyncio
async def test_corrupt_or_expired_snapshot_is_rebuilt():
    expected = router._router_snapshot()
    state = {
        "row": {
            **_row(expected),
            "integrity_sha256": "corrupt",
        }
    }
    saved = []

    async def get_snapshot(_user_id):
        return state["row"]

    async def save_snapshot(_user_id, snapshot_json, *, expected_generation):
        saved.append((snapshot_json, expected_generation))
        state["row"] = _row(snapshot_json, generation=expected_generation + 1)
        return True

    state["row"]["created_at"] = datetime.now(timezone.utc) - timedelta(hours=2)
    with (
        patch.object(router.router_db, "get_router_baseline_snapshot", get_snapshot),
        patch.object(router.router_db, "save_router_baseline_snapshot", save_snapshot),
    ):
        result = await router._load_router_baseline_snapshot("user-1")

    assert result == (expected, False)
    assert saved == [(expected, 1)]


def test_snapshot_checksum_is_an_integrity_check_not_a_signature():
    snapshot = router._router_snapshot()
    tampered = {**snapshot, "channel_reference": "changed"}
    row = _row(snapshot)
    row["snapshot_json"] = tampered
    assert not router._snapshot_is_current(row, snapshot)
