from __future__ import annotations

import pytest

from src.server.services.autonomy import host


@pytest.mark.asyncio
async def test_route_metric_hook_is_off_without_feature_flag(monkeypatch):
    monkeypatch.setattr(
        "src.config.settings.get_autonomy_router_snapshot_enabled", lambda: False
    )

    async def should_not_call(**_kwargs):
        raise AssertionError("metric DB write must not run while flag is off")

    monkeypatch.setattr(host.router_db, "record_router_route_decision", should_not_call)
    await host._record_router_route_decision(
        user_id="u1", eval_id="e1", trigger_id="t1", decision="accept"
    )


@pytest.mark.asyncio
async def test_route_metric_hook_records_only_after_successful_host_decision(monkeypatch):
    monkeypatch.setattr(
        "src.config.settings.get_autonomy_router_snapshot_enabled", lambda: True
    )
    calls = []

    async def record(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(host.router_db, "record_router_route_decision", record)
    await host._record_router_route_decision(
        user_id="u1", eval_id="e1", trigger_id="t1", decision="attach"
    )
    assert calls == [
        {"user_id": "u1", "eval_id": "e1", "trigger_id": "t1", "decision": "attach"}
    ]


@pytest.mark.asyncio
async def test_route_metric_timeout_does_not_escape_after_route_commit(monkeypatch):
    monkeypatch.setattr(
        "src.config.settings.get_autonomy_router_snapshot_enabled", lambda: True
    )

    async def record(**_kwargs):
        return None

    async def timeout(awaitable, *, timeout):
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(host.router_db, "record_router_route_decision", record)
    monkeypatch.setattr(host.asyncio, "wait_for", timeout)
    await host._record_router_route_decision(
        user_id="u1", eval_id="e1", trigger_id="t1", decision="accept"
    )
