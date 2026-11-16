"""N30: the dispatch cost API, the summary field and the settings that gate it."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.server.services.dispatch import DispatchService, DispatchSettings
from src.tools.auto_dispatch import (
    Dispatch,
    DispatchState,
    FileApprovalGate,
    InMemoryDispatchStore,
    Line,
    LineStatus,
)
from src.tools.auto_dispatch.cost import record_attempt_end, record_attempt_start
from tests.conftest import create_test_app

USER_ID = "test-user-123"
GET_SERVICE = "src.server.app.dispatch._service"
SENTINEL = "SENTINEL-c0st-9a1b-output"
T0 = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)


def _ledgered(dispatch_id: str, *, user_id: str = USER_ID) -> Dispatch:
    """glm 3x rate_limited, then kimi work + fix and merged; one line blocked."""
    dispatch = Dispatch(id=dispatch_id, goal=f"goal {SENTINEL}", user_id=user_id, created_at="now")
    dispatch.state = DispatchState.COMPLETE
    merged = Line(id="line-0", name="n0", task="t0", assigned_model="kimi")
    merged.status = LineStatus.PASSED
    blocked = Line(id="line-1", name="n1", task="t1", assigned_model="deepseek")
    blocked.status = LineStatus.BLOCKED
    dispatch.lines = [merged, blocked]
    gates: dict = {"integration": {"merged_lines": ["line-0"]}}
    for attempt in range(3):
        record_attempt_end(
            gates,
            "line-0",
            run_id=f"glm-{attempt}",
            ended_at=T0 + timedelta(seconds=attempt),
            result="failed",
            failure_class="rate_limited",
            reason_code="worker_rate_limited",
            model="glm",
            attempt=attempt,
            kind="work",
            fix_round=0,
        )
    for index, kind in enumerate(("work", "fix")):
        start = T0 + timedelta(minutes=10 * (index + 1))
        record_attempt_start(
            gates,
            "line-0",
            run_id=f"kimi-{kind}",
            attempt=3,
            model="kimi",
            kind=kind,
            fix_round=index,
            started_at=start,
        )
        record_attempt_end(
            gates,
            "line-0",
            run_id=f"kimi-{kind}",
            ended_at=start + timedelta(seconds=300),
            result="ok",
            tokens_est=9000,
        )
    record_attempt_end(
        gates,
        "line-1",
        run_id="dsh-0",
        ended_at=T0,
        result="failed",
        failure_class="auth_failed",
        reason_code=f"MISSING_CREDENTIAL: no API key {SENTINEL}",
        model="deepseek",
        attempt=0,
        kind="work",
        fix_round=0,
    )
    dispatch.gates = gates
    return dispatch


class Harness:
    def __init__(self, tmp_path) -> None:
        self.store = InMemoryDispatchStore()
        self.service = DispatchService(
            DispatchSettings(proposed_dir=tmp_path / "proposed"),
            store=self.store,
            gate=FileApprovalGate(tmp_path / "proposed"),
        )


@pytest_asyncio.fixture
async def harness(tmp_path) -> Harness:
    return Harness(tmp_path)


@pytest_asyncio.fixture
async def client(harness: Harness):
    from src.server.app.dispatch import router

    app = create_test_app(router)
    service = AsyncMock(wraps=harness.service)
    with patch(GET_SERVICE, return_value=service):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            yield http


@pytest.mark.asyncio
async def test_one_dispatch_cost_report(client, harness: Harness) -> None:
    await harness.store.create(_ledgered("d1"))

    response = await client.get("/api/v1/dispatch/d1/cost")

    assert response.status_code == 200
    body = response.json()
    assert body["dispatch_id"] == "d1"
    assert body["basis"] == "ledger"
    assert len(body["attempts"]["line-0"]) == 5
    waste = {row["class"]: row for row in body["waste_by_class"]}
    assert waste["rate_limited"]["attempts"] == 3
    assert waste["auth_failed"]["infrastructure"] is True
    models = {row["model"]: row for row in body["by_model"]}
    assert models["glm"]["cost_per_merged_line_wall_secs"] is None
    assert models["kimi"]["merged"] == 1
    assert SENTINEL not in response.text


@pytest.mark.asyncio
async def test_unknown_or_foreign_dispatch_is_404(client, harness: Harness) -> None:
    await harness.store.create(_ledgered("d2", user_id="someone-else"))
    assert (await client.get("/api/v1/dispatch/nope/cost")).status_code == 404
    assert (await client.get("/api/v1/dispatch/d2/cost")).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 501, -1])
async def test_aggregate_limit_bounds(client, limit: int) -> None:
    response = await client.get("/api/v1/dispatch/cost", params={"limit": limit})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_aggregate_is_not_swallowed_by_the_id_route(client, harness: Harness) -> None:
    await harness.store.create(_ledgered("d1"))
    await harness.store.create(_ledgered("d3"))
    await harness.store.create(_ledgered("x9", user_id="someone-else"))

    response = await client.get("/api/v1/dispatch/cost", params={"limit": 5})

    assert response.status_code == 200
    body = response.json()
    assert body["limit"] == 5
    # Only the caller's dispatches are summed.
    assert body["dispatches"] == 2
    assert body["basis"] == {"ledger": 2, "legacy": 0}
    assert "lines" not in body and "attempts" not in body
    waste = {row["class"]: row for row in body["waste_by_class"]}
    assert waste["rate_limited"]["attempts"] == 6
    assert SENTINEL not in response.text


@pytest.mark.asyncio
async def test_aggregate_default_limit(client) -> None:
    response = await client.get("/api/v1/dispatch/cost")
    assert response.status_code == 200
    assert response.json()["limit"] == 50


@pytest.mark.asyncio
async def test_summary_cost_is_line_level_only(client, harness: Harness) -> None:
    await harness.store.create(_ledgered("d1"))

    response = await client.get("/api/v1/dispatch/d1")

    assert response.status_code == 200
    body = response.json()
    cost = body["cost"]
    assert set(cost) == {"v", "basis", "counts", "lines"}
    assert cost["counts"]["merged"] == 1
    assert {row["line_id"] for row in cost["lines"]} == {"line-0", "line-1"}
    # The raw ledger is not served on the row view: gates.cost is the summary too.
    assert body["gates"]["cost"] == cost
    assert "attempts" not in cost
    assert "run_id" not in json.dumps(body)


@pytest.mark.asyncio
async def test_summary_cost_for_a_dispatch_without_ledger(client, harness: Harness) -> None:
    dispatch = Dispatch(id="old", goal="g", user_id=USER_ID, created_at="now")
    dispatch.lines = [Line(id="line-0", name="n", task="t")]
    await harness.store.create(dispatch)

    body = (await client.get("/api/v1/dispatch/old")).json()

    assert body["cost"]["basis"] == "legacy"
    assert "cost" not in body["gates"]


def test_settings_defaults_keep_the_selector_off() -> None:
    settings = DispatchSettings.from_env({})
    assert settings.cost_ledger is True
    assert settings.cost_selector is False
    assert settings.cost_weight == 0.15
    assert settings.cost_min_merged == 3


@pytest.mark.parametrize(
    ("raw", "expected"), [("0.3", 0.3), ("9", 0.5), ("-1", 0.0), ("bad", 0.15), ("nan", 0.15)]
)
def test_settings_cost_weight_is_clamped(raw: str, expected: float) -> None:
    assert DispatchSettings.from_env({"CONDUCTOR_DISPATCH_COST_WEIGHT": raw}).cost_weight == expected


def test_settings_flags_parse() -> None:
    settings = DispatchSettings.from_env(
        {
            "CONDUCTOR_DISPATCH_COST_SELECTOR": "on",
            "CONDUCTOR_DISPATCH_COST_LEDGER": "off",
            "CONDUCTOR_DISPATCH_COST_MIN_MERGED": "5",
        }
    )
    assert settings.cost_selector is True
    assert settings.cost_ledger is False
    assert settings.cost_min_merged == 5


def test_selector_kwargs_only_when_the_flag_is_on(tmp_path) -> None:
    store = InMemoryDispatchStore()
    service = DispatchService(DispatchSettings(proposed_dir=tmp_path), store=store)
    assert service._cost_selector_kwargs(DispatchSettings()) == {}
    assert service._cost_selector_kwargs(DispatchSettings(cost_selector=True, cost_weight=0.0)) == {}
    on = service._cost_selector_kwargs(DispatchSettings(cost_selector=True))
    assert on["cost_weight"] == 0.15
    assert hasattr(on["cost_stats"], "cost_per_merged_line")
    # One cache per service.
    again = service._cost_selector_kwargs(DispatchSettings(cost_selector=True))
    assert again["cost_stats"] is on["cost_stats"]


# --- review fixes 3 and 4 ---------------------------------------------------


def _raise_on_ledger(monkeypatch, name: str) -> None:
    """Make one cost builder fail whenever the dispatch carries a ledger."""
    from src.tools.auto_dispatch import cost

    real = getattr(cost, name)

    def builder(dispatch):
        if "cost" in (dispatch.gates or {}):
            raise ValueError("corrupt ledger")
        return real(dispatch)

    monkeypatch.setattr(cost, name, builder)


@pytest.mark.asyncio
async def test_corrupt_ledger_cost_report_falls_back_to_legacy(
    client, harness: Harness, monkeypatch
) -> None:
    await harness.store.create(_ledgered("d1"))
    _raise_on_ledger(monkeypatch, "dispatch_cost_report")

    response = await client.get("/api/v1/dispatch/d1/cost")

    assert response.status_code == 200
    assert response.json()["basis"] == "legacy"
    assert SENTINEL not in response.text


@pytest.mark.asyncio
async def test_corrupt_ledger_summary_falls_back_to_legacy(
    client, harness: Harness, monkeypatch
) -> None:
    await harness.store.create(_ledgered("d1"))
    _raise_on_ledger(monkeypatch, "dispatch_cost_summary")

    body = (await client.get("/api/v1/dispatch/d1")).json()

    assert body["cost"]["basis"] == "legacy"
    assert body["gates"]["cost"] == body["cost"]


def test_dispatch_summary_builds_the_cost_summary_once(monkeypatch) -> None:
    from src.server.services.dispatch import dispatch_summary
    from src.tools.auto_dispatch import cost

    calls: list[str] = []
    real = cost.dispatch_cost_summary

    def counted(dispatch):
        calls.append(dispatch.id)
        return real(dispatch)

    monkeypatch.setattr(cost, "dispatch_cost_summary", counted)

    summary = dispatch_summary(_ledgered("d1"))

    assert calls == ["d1"]
    assert summary["gates"]["cost"] == summary["cost"]
