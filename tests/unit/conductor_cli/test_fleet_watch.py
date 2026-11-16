"""``agents fleet --watch --json``: one CLI envelope per refreshed frame."""

from __future__ import annotations

import io
import json

import pytest

from src.conductor_cli import cli, fleet


class FakeClient:
    def __init__(self, payload=None) -> None:
        self.payload = payload or {
            "agents": [
                {
                    "id": "alpha",
                    "role": "reviewer",
                    "status": "running",
                    "commits_ahead": 1,
                    "provider_balance": "unknown",
                },
                {
                    "id": "beta",
                    "role": "implementer",
                    "status": "running",
                    "commits_ahead": 9,
                    "provider_balance": "ok",
                },
            ]
        }
        self.calls: list[str] = []

    def get(self, path: str, *, params=None):
        self.calls.append(path)
        return self.payload

    def close(self) -> None:
        return None


def _args(**overrides):
    args = type(
        "Args",
        (),
        {"json": True, "watch": True, "filter": None, "sort": "id"},
    )()
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def test_watch_json_frames_are_envelopes_with_filtered_normalized_rows() -> None:
    stream = io.StringIO()
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise KeyboardInterrupt

    code = fleet.watch_fleet_envelopes(
        _args(filter=["role=reviewer"], sort="commits"),
        FakeClient(),
        sleep=sleep,
        out=stream,
    )

    assert code == 0
    assert sleeps == [fleet.WATCH_INTERVAL_SECONDS]
    envelope = json.loads(stream.getvalue())
    assert envelope["api_version"] == "v1"
    assert envelope["command"] == "agents fleet"
    assert envelope["exit_code"] == 0
    assert [agent["id"] for agent in envelope["data"]["agents"]] == ["alpha"]
    assert envelope["data"]["agents"][0]["provider_balance"] == "low"


def test_watch_json_failed_scan_sets_a_nonzero_envelope_exit_code() -> None:
    stream = io.StringIO()

    def sleep(seconds: float) -> None:
        raise KeyboardInterrupt

    payload = {"agents": [], "error": "The fleet scan failed."}
    code = fleet.watch_fleet_envelopes(
        _args(), FakeClient(payload), sleep=sleep, out=stream
    )

    assert code == 0
    envelope = json.loads(stream.getvalue())
    assert envelope["exit_code"] == 1
    assert envelope["data"]["error"] == "The fleet scan failed."
    assert envelope["errors"]
    assert envelope["errors"][0]["code"] == "nonzero_exit"


def test_cli_routes_watch_json_through_the_envelope_helper(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    called: dict = {}

    def helper(args, client):
        called["args"] = args
        return 0

    monkeypatch.setattr(fleet, "watch_fleet_envelopes", helper)
    monkeypatch.setattr(cli, "APIClient", lambda **kwargs: FakeClient())

    assert cli.main(["agents", "fleet", "--watch", "--json"]) == 0
    assert called["args"].watch is True
    assert called["args"].json is True
    assert capsys.readouterr().out == ""
