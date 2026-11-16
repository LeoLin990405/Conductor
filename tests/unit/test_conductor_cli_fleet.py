"""Tests for the ``conductor agents fleet`` CLI command.

The command is a client of the read-only fleet endpoint, so these tests drive it
with a fake API client and a fake clock: no backend, no worktree, no log file.
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from src.conductor_cli import cli
from src.conductor_cli.fleet import (
    filter_rows,
    format_elapsed,
    parse_filters,
    render_fleet,
    run_agents_fleet,
    sort_rows,
)

AGENTS = [
    {
        "id": "agent-fleet",
        "model": "codex",
        "provider": "codex",
        "role": "dev",
        "status": "running",
        "pid": 101,
        "elapsed_secs": 3723,
        "branch": "feat/agent-fleet",
        "commits_ahead": 3,
        "tests_state": "12 passed",
        "provider_balance": "ok",
        "log_tail": "starting work",
    },
    {
        "id": "memfw-r2",
        "model": "mimo",
        "provider": "mimo",
        "role": "review",
        "status": "stalled",
        "pid": None,
        "elapsed_secs": None,
        "branch": "feat/memfw-r2",
        "commits_ahead": None,
        "tests_state": None,
        "provider_balance": "unknown",
        "log_tail": "",
    },
]

PAYLOAD = {
    "agents": AGENTS,
    "scan_ts": "2026-11-02T12:00:00+00:00",
    "stall_threshold_mins": 5,
    "error": None,
}


class FakeClient:
    def __init__(self, payload: Any = None) -> None:
        self.payload = payload if payload is not None else PAYLOAD
        self.calls: list[str] = []

    def get(self, path: str, *, params: dict | None = None):
        self.calls.append(path)
        return self.payload

    def close(self) -> None:
        return None


class Args:
    json = False
    watch = False
    filter = None
    sort = "id"


class TestFormatElapsed:
    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (0, "00:00"),
            (65, "01:05"),
            (3723, "1:02:03"),
            (90061, "1d01h"),
            (None, "-"),
            (-5, "-"),
            ("3723", "-"),
        ],
    )
    def test_formats(self, seconds, expected):
        assert format_elapsed(seconds) == expected


class TestFilters:
    def test_parses_repeated_filters(self):
        assert parse_filters(["role=dev", "status=running"]) == {
            "role": "dev",
            "status": "running",
        }

    def test_rejects_unknown_keys_and_missing_values(self):
        for value in ("role", "role=", "bogus=dev", "=dev"):
            with pytest.raises(ValueError):
                parse_filters([value])

    def test_filters_by_substring_on_rendered_columns(self):
        rows = filter_rows(AGENTS, {"role": "rev"})
        assert [row["id"] for row in rows] == ["memfw-r2"]

    def test_filters_are_anded(self):
        assert filter_rows(AGENTS, {"role": "dev", "status": "stalled"}) == []

    def test_empty_filter_keeps_everything(self):
        assert len(filter_rows(AGENTS, {})) == 2

    def test_filters_by_provider(self):
        # `provider` is an accepted filter key, so the rendered row has to carry
        # it; without the column the filter raised KeyError instead of matching.
        assert [row["id"] for row in filter_rows(AGENTS, {"provider": "codex"})] == [
            "agent-fleet"
        ]
        assert filter_rows(AGENTS, {"provider": "mimo"}) == [
            row for row in filter_rows(AGENTS, {}) if row["id"] == "memfw-r2"
        ]


class TestSorting:
    def test_by_elapsed_puts_unknown_last(self):
        rows = sort_rows(filter_rows(AGENTS, {}), "elapsed")
        assert [row["id"] for row in rows] == ["agent-fleet", "memfw-r2"]

    def test_by_status_orders_by_severity(self):
        rows = sort_rows(filter_rows(AGENTS, {}), "status")
        assert [row["id"] for row in rows] == ["agent-fleet", "memfw-r2"]

    def test_by_commits_puts_unknown_last(self):
        rows = sort_rows(filter_rows(AGENTS, {}), "commits")
        assert [row["id"] for row in rows] == ["agent-fleet", "memfw-r2"]

    def test_numeric_sorts_are_descending_with_nulls_last(self):
        agents = [
            {"id": "low", "elapsed_secs": 2, "commits_ahead": 1},
            {"id": "high", "elapsed_secs": 10, "commits_ahead": 8},
            {"id": "unknown", "elapsed_secs": None, "commits_ahead": None},
            {"id": "middle", "elapsed_secs": 5, "commits_ahead": 3},
        ]
        rows = filter_rows(agents, {})
        assert [row["id"] for row in sort_rows(rows, "elapsed")] == [
            "high", "middle", "low", "unknown"
        ]
        assert [row["id"] for row in sort_rows(rows, "commits")] == [
            "high", "middle", "low", "unknown"
        ]

    def test_by_commits_puts_the_most_commits_first(self):
        # The dashboard's AHEAD column reads most-commits-first, and the CLI
        # read it the other way round over the same rows. A fixture with one
        # known count and one unknown could not tell the two orders apart, so
        # the disagreement survived every round of tests.
        agents = [
            {**AGENTS[1], "id": "memfw-r2", "commits_ahead": None},
            {**AGENTS[0], "id": "zeta", "commits_ahead": 1},
            {**AGENTS[0], "id": "alpha", "commits_ahead": 5},
        ]
        rows = sort_rows(filter_rows(agents, {}), "commits")
        assert [row["id"] for row in rows] == ["alpha", "zeta", "memfw-r2"]

    def test_by_id(self):
        rows = sort_rows(filter_rows(AGENTS, {}), "id")
        assert [row["id"] for row in rows] == ["agent-fleet", "memfw-r2"]


class TestRendering:
    def test_table_has_the_dashboard_columns(self, capsys):
        render_fleet(PAYLOAD, json_mode=False, filters={}, sort_key="id")
        out = capsys.readouterr().out
        header = out.splitlines()[0]
        for column in ("AGENT", "MODEL", "ROLE", "STATUS", "ELAPSED", "AHEAD", "TESTS", "BAL"):
            assert column in header
        assert "feat/agent-fleet" in out
        assert "1:02:03" in out

    def test_table_reports_an_empty_fleet(self, capsys):
        render_fleet({"agents": []}, json_mode=False, filters={}, sort_key="id")
        assert "No agents found." in capsys.readouterr().out

    def test_table_notes_a_scan_error(self, capsys):
        render_fleet(
            {"agents": [], "error": "Agent fleet discovery is disabled."},
            json_mode=False,
            filters={},
            sort_key="id",
        )
        captured = capsys.readouterr()
        # The scan did not run, so the headline is not "no agents": an empty
        # fleet is a different answer, and a clean one.
        assert "No agents found." not in captured.out
        assert "The fleet scan failed." in captured.out
        assert "disabled" in captured.err

    def test_table_keeps_the_rows_of_a_scan_that_reported_an_error(self, capsys):
        render_fleet(
            {"agents": AGENTS, "error": "The fleet scan failed; the worktrees were not read."},
            json_mode=False,
            filters={},
            sort_key="id",
        )
        captured = capsys.readouterr()
        assert "agent-fleet" in captured.out
        assert "worktrees were not read" in captured.err

    def test_json_mode_prints_the_payload(self, capsys):
        render_fleet(PAYLOAD, json_mode=True, filters={}, sort_key="id")
        assert json.loads(capsys.readouterr().out) == PAYLOAD

    def test_json_filters_and_sorts_raw_agents_and_preserves_envelope(self):
        agents = [
            {"id": "a-low", "model": "Codex-X", "provider": "codex", "role": "dev", "status": "running", "elapsed_secs": 1, "commits_ahead": 2, "private_field": {"kept": True}},
            {"id": "b-high", "model": "Codex-Y", "provider": "codex", "role": "dev", "status": "running", "elapsed_secs": 9, "commits_ahead": 7},
            {"id": "c-other", "model": "Mimo", "provider": "mimo", "role": "review", "status": "done", "elapsed_secs": None, "commits_ahead": None},
        ]
        payload = {"agents": agents, "scan_ts": "scan-time", "error": None, "extra": 42}
        stream = io.StringIO()

        render_fleet(
            payload,
            json_mode=True,
            filters={"provider": "CODEX", "model": "codex"},
            sort_key="commits",
            out=stream,
        )

        result = json.loads(stream.getvalue())
        assert result["agents"] == [agents[1], agents[0]]
        assert result["agents"][1]["private_field"] == {"kept": True}
        assert {key: value for key, value in result.items() if key != "agents"} == {
            "scan_ts": "scan-time",
            "error": None,
            "extra": 42,
        }

    def test_json_mode_keeps_the_filters_and_the_sort(self, capsys):
        # `--filter` and `--sort` describe the view, not the transport: the
        # JSON frame printed the raw scan and dropped both, so a reader that
        # asked for the review agents got the whole fleet in scan order while
        # the help text promised the flags hold.
        render_fleet(PAYLOAD, json_mode=True, filters={"role": "review"}, sort_key="commits")
        frame = json.loads(capsys.readouterr().out)
        assert [agent["id"] for agent in frame["agents"]] == ["memfw-r2"]
        # The frame is still the scan's own: the agents as the endpoint sent
        # them, and the keys that sit around them.
        assert frame["agents"] == [AGENTS[1]]
        assert frame["scan_ts"] == PAYLOAD["scan_ts"]
        assert frame["stall_threshold_mins"] == PAYLOAD["stall_threshold_mins"]

    def test_json_mode_keeps_the_scan_error(self, capsys):
        render_fleet(
            {"agents": AGENTS, "error": "The fleet scan failed."},
            json_mode=True,
            filters={"role": "dev"},
            sort_key="id",
        )
        frame = json.loads(capsys.readouterr().out)
        assert frame["error"] == "The fleet scan failed."
        assert [agent["id"] for agent in frame["agents"]] == ["agent-fleet"]

    def test_json_mode_orders_agents_the_way_the_table_does(self, capsys):
        # The two renderers read one set of rules, so the JSON frame cannot
        # order the fleet differently from the table over it.
        agents = [
            {**AGENTS[1], "id": "memfw-r2", "commits_ahead": None},
            {**AGENTS[0], "id": "zeta", "commits_ahead": 1},
            {**AGENTS[0], "id": "alpha", "commits_ahead": 5},
        ]
        render_fleet(
            {"agents": agents}, json_mode=True, filters={}, sort_key="commits"
        )
        frame = json.loads(capsys.readouterr().out)
        assert [agent["id"] for agent in frame["agents"]] == ["alpha", "zeta", "memfw-r2"]
        table = sort_rows(filter_rows(agents, {}), "commits")
        assert [row["id"] for row in table] == [agent["id"] for agent in frame["agents"]]


class TestRunAgentsFleet:
    def test_single_scan_calls_the_fleet_endpoint(self, capsys):
        client = FakeClient()
        args = Args()
        assert run_agents_fleet(args, client) == 0
        assert client.calls == ["/api/v1/agents/fleet"]
        assert "agent-fleet" in capsys.readouterr().out

    def test_filters_and_sorts_reach_the_renderer(self, capsys):
        args = Args()
        args.filter = ["role=review"]
        args.sort = "status"
        assert run_agents_fleet(args, FakeClient()) == 0
        out = capsys.readouterr().out
        assert "memfw-r2" in out
        assert "agent-fleet" not in out

    def test_json_filters_and_sorts_reach_renderer_and_use_out_stream(self):
        args = Args()
        args.json = True
        args.filter = ["provider=COD", "role=DEV"]
        args.sort = "commits"
        stream = io.StringIO()

        assert run_agents_fleet(args, FakeClient(), out=stream) == 0

        result = json.loads(stream.getvalue())
        assert [agent["id"] for agent in result["agents"]] == ["agent-fleet"]
        assert "AGENT" not in stream.getvalue()

    def test_invalid_filter_is_a_usage_error(self):
        args = Args()
        args.filter = ["nope"]
        with pytest.raises(ValueError):
            run_agents_fleet(args, FakeClient())

    def test_a_json_frame_keeps_the_filters_and_the_sort(self, capsys):
        # The flags reach the renderer in table mode; `--json` used to print
        # the raw scan and drop both of them.
        args = Args()
        args.json = True
        args.filter = ["role=review"]
        args.sort = "commits"
        assert run_agents_fleet(args, FakeClient()) == 0
        frame = json.loads(capsys.readouterr().out)
        assert [agent["id"] for agent in frame["agents"]] == ["memfw-r2"]

    def test_a_failed_scan_exits_nonzero(self, capsys):
        # The endpoint answers 200 for a scan that never ran — a disabled
        # deployment, a scan that raised — as a frame with an `error` and no
        # agents, so the status code alone reported success to automation that
        # had learned nothing about the fleet.
        args = Args()
        client = FakeClient({"agents": [], "error": "The fleet scan failed."})
        assert run_agents_fleet(args, client) == 1
        captured = capsys.readouterr()
        assert "No agents found." not in captured.out
        assert "The fleet scan failed." in captured.out

    def test_a_failed_scan_exits_nonzero_beside_a_filter(self, capsys):
        # The filter cannot have matched anything: the scan returned no rows
        # to filter. Reporting "no agents match" would hide the failure.
        args = Args()
        args.filter = ["role=dev"]
        client = FakeClient({"agents": [], "error": "The fleet scan failed."})
        assert run_agents_fleet(args, client) == 1
        assert "No agents match" not in capsys.readouterr().out

    def test_an_unreadable_response_is_a_failed_scan(self):
        # A 200 whose body is not a scan object is not an empty fleet.
        args = Args()
        client = FakeClient(["not", "a", "scan"])
        assert run_agents_fleet(args, client) == 1

    def test_a_clean_scan_of_an_empty_fleet_still_exits_zero(self, capsys):
        args = Args()
        client = FakeClient({"agents": [], "error": None})
        assert run_agents_fleet(args, client) == 0
        assert "No agents found." in capsys.readouterr().out

    def test_watch_refreshes_until_interrupted(self, capsys):
        args = Args()
        args.watch = True
        client = FakeClient()
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) >= 2:
                raise KeyboardInterrupt

        assert run_agents_fleet(args, client, sleep=sleep) == 0
        assert sleeps == [5.0, 5.0]
        assert client.calls == ["/api/v1/agents/fleet", "/api/v1/agents/fleet"]
        assert capsys.readouterr().out.count("AGENT") == 2

    def test_a_json_watch_frame_carries_no_terminal_escapes(self, capsys):
        # A watch redraws the table in place with a clear-screen escape. A JSON
        # frame is a document for a reader that parses each one, and the escape
        # in front of it is not JSON, so a piped `--watch --json` failed on its
        # first frame.
        args = Args()
        args.watch = True
        args.json = True
        client = FakeClient()
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) >= 2:
                raise KeyboardInterrupt

        stream = io.StringIO()
        assert run_agents_fleet(args, client, sleep=sleep, out=stream) == 0
        # One stream: the frame lands where the clear-screen escape did, and a
        # piped watch reads documents and nothing else.
        assert capsys.readouterr().out == ""
        out = stream.getvalue()
        assert out.count("\033") == 0
        # Every frame is one whole document, so a reader can walk the stream.
        decoder = json.JSONDecoder()
        cursor = out.lstrip()
        frames = []
        while cursor:
            frame, end = decoder.raw_decode(cursor)
            frames.append(frame)
            cursor = cursor[end:].lstrip()
        assert frames == [client.payload, client.payload]

class TestParserWiring:
    def test_fleet_subcommand_is_registered(self):
        args = cli.build_parser().parse_args(
            ["agents", "fleet", "--watch", "--filter", "role=dev", "--sort", "elapsed"]
        )
        assert args.agents_command == "fleet"
        assert args.watch is True
        assert args.filter == ["role=dev"]
        assert args.sort == "elapsed"

    def test_list_subcommand_still_works(self):
        args = cli.build_parser().parse_args(["agents", "list"])
        assert args.agents_command == "list"

    def test_main_runs_the_fleet_command(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "APIClient", lambda **kwargs: FakeClient())
        assert cli.main(["agents", "fleet", "--json"]) == 0
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["command"] == "agents fleet"
        assert envelope["data"] == PAYLOAD

    def test_main_propagates_a_failed_scan(self, monkeypatch, capsys):
        monkeypatch.setattr(
            cli,
            "APIClient",
            lambda **kwargs: FakeClient({"agents": [], "error": "The fleet scan failed."}),
        )
        assert cli.main(["agents", "fleet"]) == 1
        assert "The fleet scan failed." in capsys.readouterr().out
