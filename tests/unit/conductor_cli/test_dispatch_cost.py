"""N30: ``conductor dispatch cost <id>`` and ``conductor dispatch cost [--limit N]``."""

from __future__ import annotations

import argparse
import json

import pytest

from src.conductor_cli import dispatch
from src.conductor_cli.client import APIError, CLIError, ServerError

REPORT = {
    "dispatch_id": "3f2a9c1d",
    "state": "COMPLETE",
    "basis": "ledger",
    "counts": {"lines": 2, "merged": 1, "blocked": 1, "failed": 0, "open": 0},
    "lines": [
        {
            "line_id": "line-0",
            "outcome": "merged",
            "author": "kimi",
            "attempts": 5,
            "fix_rounds": 1,
            "wall_secs": 1082,
            "tokens_cli": None,
            "tokens_est": 41000,
            "waste_class": "rate_limited",
            "waste": [{"model": "glm", "class": "rate_limited", "attempts": 3, "wall_secs": 130}],
        },
        {
            "line_id": "line-1",
            "outcome": "blocked",
            "author": "deepseek",
            "attempts": 1,
            "fix_rounds": 0,
            "wall_secs": 0,
            "tokens_cli": None,
            "tokens_est": 0,
            "waste_class": "auth_failed",
            "waste": [{"model": "deepseek", "class": "auth_failed", "attempts": 1, "wall_secs": 0}],
        },
    ],
    "attempts": {"line-0": [], "line-1": []},
    "by_model": [
        {
            "model": "deepseek",
            "attempts": 1,
            "lines": 1,
            "merged": 0,
            "spent_wall_secs": 0,
            "spent_tokens_cli": None,
            "spent_tokens_est": 0,
            "cost_per_merged_line_wall_secs": None,
            "merge_yield": 0.0,
            "waste_share": 1.0,
        },
        {
            "model": "kimi",
            "attempts": 2,
            "lines": 1,
            "merged": 1,
            "spent_wall_secs": 952,
            "spent_tokens_cli": None,
            "spent_tokens_est": 41000,
            "cost_per_merged_line_wall_secs": 952.0,
            "merge_yield": 1.0,
            "waste_share": 0.0,
        },
    ],
    "waste_by_class": [
        {
            "class": "auth_failed",
            "attempts": 1,
            "wall_secs": 0,
            "tokens_cli": None,
            "tokens_est": 0,
            "by_model": {"deepseek": 1},
            "infrastructure": True,
        },
        {
            "class": "rate_limited",
            "attempts": 3,
            "wall_secs": 130,
            "tokens_cli": None,
            "tokens_est": 0,
            "by_model": {"glm": 3},
            "infrastructure": False,
        },
    ],
    "fix_rounds": {"total": 1, "per_merged_line": 1.0},
    "quota_note": "approximate, account-wide (CodexBar)",
}

AGGREGATE = {
    "limit": 5,
    "dispatches": 2,
    "basis": {"ledger": 1, "legacy": 1},
    "by_model": REPORT["by_model"],
    "waste_by_class": REPORT["waste_by_class"],
    "fix_rounds": {"total": 1, "per_merged_line": 1.0},
    "quota_note": "approximate, account-wide (CodexBar)",
}


class FakeClient:
    def __init__(self, payload=None, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls: list[tuple[str, str, object]] = []

    def get(self, path: str, *, params=None):
        self.calls.append(("GET", path, params))
        if self.error is not None:
            raise self.error
        return self.payload

    def post(self, path: str, json=None):  # pragma: no cover - cost never writes
        raise AssertionError("cost must not POST")


def _run(client: FakeClient, *words: str, limit: int | None = None, as_json: bool = False):
    return dispatch._dispatch(
        argparse.Namespace(args=list(words), limit=limit, json=as_json), client
    )


def test_cost_one_dispatch_reads_its_route_and_renders() -> None:
    client = FakeClient(REPORT)
    result = _run(client, "cost", "3f2a9c1d")
    assert client.calls == [("GET", "/api/v1/dispatch/3f2a9c1d/cost", None)]
    assert result["rendered"] == [
        "dispatch 3f2a9c1d  state COMPLETE  basis ledger  lines 2  merged 1  passed 0  blocked 1  failed 0",
        "line    outcome  author    attempts  fix  wall    tok(cli)  tok(est)  waste",
        "line-0  merged   kimi      5         1    18m02s  -         41k       glm rate_limited x3 (2m10s)",
        "line-1  blocked  deepseek  1         0    0s      -         0         deepseek auth_failed (0s)",
        "by model  attempts  spent(wall)  merged  cost/merged  yield  waste%",
        "deepseek  1         0s           0       n/a          0.00   100%",
        "kimi      2         15m52s       1       15m52s       1.00   0%",
        "waste by class       attempts  wall   models",
        "auth_failed (infra)  1         0s     deepseek",
        "rate_limited         3         2m10s  glm",
        "quota: approximate, account-wide (CodexBar)",
    ]


def test_cost_limit_reads_the_aggregate_and_renders_two_tables() -> None:
    client = FakeClient(AGGREGATE)
    result = _run(client, "cost", limit=5)
    assert client.calls == [("GET", "/api/v1/dispatch/cost", {"limit": 5})]
    assert result["rendered"] == [
        "cost over 2 dispatches  limit 5  ledger 1  legacy 1",
        "by model  attempts  spent(wall)  merged  cost/merged  yield  waste%",
        "deepseek  1         0s           0       n/a          0.00   100%",
        "kimi      2         15m52s       1       15m52s       1.00   0%",
        "waste by class       attempts  wall   models",
        "auth_failed (infra)  1         0s     deepseek",
        "rate_limited         3         2m10s  glm",
        "quota: approximate, account-wide (CodexBar)",
    ]


def test_cost_default_limit_is_50() -> None:
    client = FakeClient(AGGREGATE)
    _run(client, "cost")
    assert client.calls == [("GET", "/api/v1/dispatch/cost", {"limit": 50})]


def test_cost_json_is_the_server_object() -> None:
    client = FakeClient(REPORT)
    result = _run(client, "cost", "3f2a9c1d", as_json=True)
    assert json.loads(json.dumps(result)) == REPORT
    assert "rendered" not in result


@pytest.mark.parametrize("status", [404, 405, 501])
def test_server_without_the_endpoint_is_not_supported(status: int) -> None:
    error = (
        APIError("Not Found", status) if status < 500 else ServerError("Not Implemented", status)
    )
    with pytest.raises(CLIError, match="cost report not supported by this server"):
        _run(FakeClient(error=error), "cost", limit=5)


def test_unknown_id_says_not_found_rather_than_unsupported() -> None:
    error = APIError("Dispatch not found", 404)
    with pytest.raises(CLIError, match="dispatch d9 not found"):
        _run(FakeClient(error=error), "cost", "d9")


def test_cost_never_falls_back_to_a_fake_record(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(CLIError):
        _run(FakeClient(error=APIError("Not Found", 404)), "cost", "d1")
    assert not any(tmp_path.rglob("*"))


@pytest.mark.parametrize("limit", [0, 501])
def test_limit_out_of_range_is_a_usage_error(limit: int) -> None:
    client = FakeClient(AGGREGATE)
    with pytest.raises(dispatch.DispatchUsageError):
        _run(client, "cost", limit=limit)
    assert client.calls == []


def test_limit_only_goes_with_cost() -> None:
    with pytest.raises(dispatch.DispatchUsageError, match="--limit goes with cost"):
        _run(FakeClient({}), "status", "d1", limit=5)


def test_id_and_limit_together_are_refused() -> None:
    with pytest.raises(dispatch.DispatchUsageError):
        _run(FakeClient(REPORT), "cost", "d1", limit=5)


def test_parser_accepts_limit() -> None:
    from src.conductor_cli import cli

    args = cli.build_parser().parse_args(["dispatch", "cost", "--limit", "7"])
    assert args.limit == 7
    assert args.args == ["cost"]
