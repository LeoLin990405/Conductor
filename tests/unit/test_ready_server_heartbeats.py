"""/ready trusts the server heartbeat, not only cond's one-time heartbeat file.

``cond up`` writes ``~/.conductor/run/heartbeats/<id>.json`` once when it
starts a daemon; the daemon then heartbeats to the server, which updates
``autonomy_daemons.last_seen_at``. Before this fix /ready read only the file,
so every cond-managed daemon became "stale" after the TTL (backlog N20).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from src.server.app import ready

FLEET = """
[backend]
port = 54321
[web]
port = 54322
mode = "preview"
[[daemon]]
provider = "codex"
id = "d-codex"
[[daemon]]
provider = "kimi"
id = "d-kimi"
[[daemon]]
provider = "cursor"
id = "d-retired"
retired = true
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path))
    monkeypatch.delenv("CONDUCTOR_DAEMONS_FILE", raising=False)
    monkeypatch.setenv("CONDUCTOR_HEARTBEAT_TTL", "120")
    (tmp_path / "daemons.toml").write_text(FLEET.strip() + "\n", encoding="utf-8")
    beats = tmp_path / "run" / "heartbeats"
    beats.mkdir(parents=True)
    return beats


def _file(beats, ident: str, age_seconds: float) -> None:
    at = (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat()
    (beats / f"{ident}.json").write_text(json.dumps({"id": ident, "at": at}), encoding="utf-8")


def _server(monkeypatch, seen: dict[str, float] | None) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake(ids):
        calls.append(list(ids))
        if seen is None:
            return None
        now = datetime.now(UTC)
        return {ident: now - timedelta(seconds=age) for ident, age in seen.items()}

    monkeypatch.setattr(ready, "server_last_seen", fake)
    return calls


def test_a_fresh_server_heartbeat_wins_over_a_stale_cond_file(home, monkeypatch):
    # The exact cutover picture: cond's files are minutes old, the server saw
    # both daemons a second ago.
    _file(home, "d-codex", 500)
    _file(home, "d-kimi", 500)
    calls = _server(monkeypatch, {"d-codex": 1, "d-kimi": 2})
    result = ready.probe_daemon_heartbeats()
    assert result["ok"] is True, result
    assert calls == [["d-codex", "d-kimi"]]  # retired ids are never asked for


def test_a_daemon_with_no_server_row_and_no_file_is_missing(home, monkeypatch):
    _file(home, "d-codex", 500)
    _server(monkeypatch, {"d-codex": 1})
    result = ready.probe_daemon_heartbeats()
    assert result["ok"] is False
    assert result["missing"] == ["d-kimi"]
    assert result["stale"] == []


def test_stale_on_both_sides_is_stale(home, monkeypatch):
    _file(home, "d-codex", 500)
    _file(home, "d-kimi", 1)
    _server(monkeypatch, {"d-codex": 400})
    result = ready.probe_daemon_heartbeats()
    assert result["ok"] is False
    assert result["stale"] == ["d-codex"]


def test_without_the_server_lookup_the_file_rule_is_unchanged(home, monkeypatch):
    _file(home, "d-codex", 1)
    _file(home, "d-kimi", 500)
    _server(monkeypatch, None)  # no database configured, or the lookup failed
    result = ready.probe_daemon_heartbeats()
    assert result["ok"] is False
    assert result["stale"] == ["d-kimi"]
    _file(home, "d-kimi", 1)
    assert ready.probe_daemon_heartbeats()["ok"] is True


def test_server_last_seen_is_none_without_a_database(monkeypatch):
    for name in ("DB_HOST", "PGHOST", "DB_NAME", "PGDATABASE"):
        monkeypatch.delenv(name, raising=False)
    assert ready.server_last_seen(["d-codex"]) is None


def test_server_last_seen_reports_none_when_the_query_fails(monkeypatch):
    monkeypatch.setenv("DB_HOST", "127.0.0.1")
    monkeypatch.setenv("DB_NAME", "conductor_test")

    class Boom:
        @staticmethod
        def connect(**_kwargs):
            raise OSError("refused")

    monkeypatch.setitem(__import__("sys").modules, "psycopg", Boom)
    assert ready.server_last_seen(["d-codex"]) is None


def test_server_last_seen_parses_rows_and_naive_times(monkeypatch):
    monkeypatch.setenv("DB_HOST", "127.0.0.1")
    monkeypatch.setenv("DB_NAME", "conductor_test")
    aware = datetime(2026, 11, 10, 5, 0, tzinfo=UTC)
    naive = datetime(2026, 11, 10, 5, 1)
    executed: list[tuple] = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params):
            executed.append((sql, params))

        def fetchall(self):
            return [("d-codex", aware), ("d-kimi", naive), ("d-bad", "not-a-time")]

    class Conn:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    class FakePsycopg:
        @staticmethod
        def connect(**kwargs):
            assert kwargs["connect_timeout"] == 2
            return Conn()

    monkeypatch.setitem(__import__("sys").modules, "psycopg", FakePsycopg)
    seen = ready.server_last_seen(["d-codex", "d-kimi", "d-bad"])
    assert seen == {"d-codex": aware, "d-kimi": naive.replace(tzinfo=UTC)}
    assert executed[0][1] == (["d-codex", "d-kimi", "d-bad"],)
    assert "autonomy_daemons" in executed[0][0]
