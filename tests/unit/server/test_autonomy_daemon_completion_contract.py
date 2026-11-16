"""Mixed deploy: the JSON completion prompt goes only to daemons that parse it."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from src.server.models.autonomy import DaemonClaimRequest, DaemonReport
from src.server.services.autonomy.completion import COMPLETION_REPORT_PROMPT
from tests.unit.server.test_autonomy_daemon_followup import (  # noqa: F401
    TOKEN_HEADERS,
    _daemon_authed,
    _daemon_ticket,
    client,
)


async def _claim(client, body):  # noqa: F811 - the fixture is passed in
    with (
        _daemon_authed(),
        patch(
            "src.server.services.autonomy.worker.claim_daemon_ticket",
            new=AsyncMock(return_value=_daemon_ticket(status="running", attempts=2)),
        ),
        patch(
            "src.server.app.autonomy.source_db.get_source_any_user",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.server.services.autonomy.shared_context.worker_shared_context_text",
            new=AsyncMock(return_value=""),
        ),
    ):
        resp = await client.post(
            "/api/v1/autonomy/daemon/claim", headers=TOKEN_HEADERS, json=body
        )
    assert resp.status_code == 200
    return resp.json()


@pytest.mark.asyncio
async def test_a_contract_daemon_gets_the_json_prompt_and_the_echo(client):  # noqa: F811
    body = await _claim(client, {"completion_contract": 1})

    assert COMPLETION_REPORT_PROMPT in body["prompt"]
    assert "AUTONOMY_STATE" not in body["prompt"]
    assert body["ticket"]["completion_contract"] == 1
    assert body["ticket"]["attempts"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("request_body", [None, {}, {"policy_version": "v1"}])
async def test_an_older_daemon_keeps_the_marker_prompt(client, request_body):  # noqa: F811
    body = await _claim(client, request_body)

    assert "AUTONOMY_STATE: ready | blocked | failed | dropped" in body["prompt"]
    assert COMPLETION_REPORT_PROMPT not in body["prompt"]
    assert body["ticket"]["completion_contract"] == 0


def test_an_older_server_model_ignores_the_new_claim_field():
    assert DaemonClaimRequest.model_validate({"completion_contract": 1}).completion_contract == 1
    assert DaemonClaimRequest.model_validate({}).completion_contract == 0


@pytest.mark.parametrize(
    "reason", ["completion_report_missing", "completion_no_change"]
)
def test_the_server_accepts_the_new_attention_reasons(reason):
    report = DaemonReport.model_validate(
        {"lease_id": str(uuid4()), "state": "waiting", "attention_reason": reason}
    )
    assert report.attention_reason == reason


def test_follow_up_wording_matches_the_prompt_the_daemon_got():
    from src.server.services.autonomy.worker import append_followups

    inputs = [{"input_id": "i1", "kind": "answer", "text": "Use dev."}]
    current = append_followups("PROMPT", inputs)
    legacy = append_followups("PROMPT", inputs, completion_contract=False)

    assert current.endswith("completion report as instructed above.")
    assert "AUTONOMY_STATE" not in current
    assert legacy.endswith("AUTONOMY_STATE report lines as instructed above.")


@pytest.mark.asyncio
async def test_a_legacy_resume_claim_gets_marker_follow_up_wording(client):  # noqa: F811
    from datetime import datetime, timezone

    inputs = [
        {
            "input_id": "i1",
            "kind": "answer",
            "text": "Use the dev branch",
            "created_at": datetime.now(timezone.utc),
        }
    ]
    with (
        _daemon_authed(),
        patch(
            "src.server.services.autonomy.worker.claim_daemon_ticket",
            new=AsyncMock(
                return_value=_daemon_ticket(status="running", inputs=inputs)
            ),
        ),
        patch(
            "src.server.app.autonomy.source_db.get_source_any_user",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.server.services.autonomy.shared_context.worker_shared_context_text",
            new=AsyncMock(return_value=""),
        ),
    ):
        resp = await client.post(
            "/api/v1/autonomy/daemon/claim", headers=TOKEN_HEADERS, json={}
        )
    assert resp.status_code == 200
    assert resp.json()["prompt"].rstrip().endswith(
        "AUTONOMY_STATE report lines as instructed above."
    )
