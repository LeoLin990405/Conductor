"""JSON-safe PostgreSQL row projections for durable deliberation aggregates."""

import json
from datetime import datetime, timezone

from src.server.database.autonomy.deliberations import (
    _ROUND_FIELDS,
    _columns,
    _deliberation,
    _dissent,
    _round,
)


def test_dissent_projection_serializes_postgres_timestamp_for_run_payload() -> None:
    created_at = datetime(2026, 11, 2, 12, 34, 56, tzinfo=timezone.utc)
    row = _dissent(
        {
            "dissent_id": "dissent-id",
            "deliberation_id": "deliberation-id",
            "participant_id": "participant-id",
            "round_id": "round-id",
            "agent_key": "grok",
            "reason": "Keep the risk explicit.",
            "created_at": created_at,
        }
    )

    encoded = json.dumps(row)
    assert row["created_at"] == "2026-11-02T12:34:56+00:00"
    assert "Keep the risk explicit." in encoded


def test_round_projection_keeps_provider_group_snapshot_json_safe() -> None:
    created_at = datetime(2026, 11, 2, 12, 34, 56, tzinfo=timezone.utc)
    row = _round(
        {
            "round_id": "round-id",
            "deliberation_id": "deliberation-id",
            "round_index": 1,
            "kind": "propose",
            "provider_groups": [
                {"group_key": "fast", "agent_keys": ["alpha", "beta"]}
            ],
            "status": "closed",
            "created_at": created_at,
            "closed_at": created_at,
        }
    )

    assert row["provider_groups"] == [
        {"group_key": "fast", "agent_keys": ["alpha", "beta"]}
    ]
    assert row["created_at"] == "2026-11-02T12:34:56+00:00"
    assert json.loads(json.dumps(row))["provider_groups"] == row["provider_groups"]


def test_continuation_parent_projection_and_group_json_binding() -> None:
    parent_id = "22222222-2222-4222-8222-222222222222"
    deliberation = _deliberation(
        {
            "deliberation_id": "11111111-1111-4111-8111-111111111111",
            "ticket_id": "ticket-id",
            "parent_deliberation_id": parent_id,
            "consensus_plan": None,
            "created_at": None,
            "updated_at": None,
        }
    )
    columns, params = _columns(
        {"round_id": "round-id", "provider_groups": [{"group_key": "all"}]},
        _ROUND_FIELDS,
    )

    assert deliberation["parent_deliberation_id"] == parent_id
    assert columns == ["round_id", "provider_groups"]
    assert params[1].obj == [{"group_key": "all"}]
