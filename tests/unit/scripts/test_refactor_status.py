"""Refactor status tool: flag reporting, schema state and projection counts.

This is canary/rollback evidence, so the failure that matters is a status that
lies: a flag read from another source, a missing table crashing the whole read,
or a schema comparison that calls a behind database migrated.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import refactor_status

pytestmark = pytest.mark.asyncio

REPO = Path(__file__).resolve().parents[3]


class TestFlags:
    def test_lists_every_flag_with_its_env(self, monkeypatch):
        monkeypatch.setenv("REFACTOR_GOALS_ENABLED", "1")
        monkeypatch.setenv("REFACTOR_MEMORY_INDEX_ENABLED", "true")
        monkeypatch.delenv("REFACTOR_EVENTS_ENABLED", raising=False)
        flags = refactor_status.get_refactor_flags()
        envs = refactor_status.get_refactor_flag_envs()
        assert set(flags) == {
            "goals",
            "events",
            "memory_index",
            "task_tree",
            "subagent_contract",
            "permissions",
            "approval_chain",
            "compaction_events",
        }
        assert flags["goals"] is True
        assert flags["memory_index"] is True
        assert flags["events"] is False
        assert flags["approval_chain"] is False
        assert flags["compaction_events"] is False
        assert all(
            name.startswith("REFACTOR_") and name.endswith("_ENABLED")
            for name in envs.values()
        )

    async def test_render_shows_state_and_env(self):
        text = refactor_status.render_status(
            flags={"goals": True, "events": False},
            envs={
                "goals": "REFACTOR_GOALS_ENABLED",
                "events": "REFACTOR_EVENTS_ENABLED",
            },
            head="092",
        )
        assert "`REFACTOR_GOALS_ENABLED` | ON" in text
        assert "`REFACTOR_EVENTS_ENABLED` | off" in text
        assert "database not read" in text


class TestSchema:
    async def test_state_comparison(self):
        assert refactor_status.schema_state(current="092", head="092") == "migrated"
        assert refactor_status.schema_state(current="085", head="092") == "behind"
        assert refactor_status.schema_state(current="093", head="092") == "ahead"
        assert refactor_status.schema_state(current=None, head="092") == "unknown"
        assert refactor_status.schema_state(current="abc", head="092") == "unknown"

    async def test_repo_head_is_single_146(self):
        assert refactor_status.repo_schema_head() == "151"

    async def test_repo_head_is_single_legacy_127(self):
        # integrate/r2 pinned the head at 127 (subscription lifecycle); the
        # later ticket, memory, schema-cleanup, and report-body migrations
        # continue that line to the current single head.
        assert refactor_status.repo_schema_head() == "151"

    async def test_repo_head_is_single(self):
        assert refactor_status.repo_schema_head() == "151"


class TestDatabase:
    async def test_reads_revision_and_counts_with_absent_tables(self, monkeypatch):
        class Cursor:
            def __init__(self):
                self.calls = []

            async def execute(self, sql, params=None):
                self.calls.append(sql)
                if "alembic_version" in sql:
                    return self
                if "agent_tasks" in sql:
                    raise RuntimeError("relation does not exist")
                return self

            async def fetchone(self):
                if "alembic_version" in self.calls[-1]:
                    return {"version_num": "092"}
                return {"count": 3}

        class Conn:
            def cursor(self, row_factory=None):
                cur = Cursor()

                class CM:
                    async def __aenter__(self):
                        return cur

                    async def __aexit__(self, *args):
                        return False

                return CM()

        class Pool:
            def __init__(self):
                self.conn = Conn()
                self.closed = False

            async def open(self):
                return None

            async def close(self):
                self.closed = True

            def connection(self):
                class CM:
                    def __init__(self, conn):
                        self.conn = conn

                    async def __aenter__(self):
                        return self.conn

                    async def __aexit__(self, *args):
                        return False

                return CM(self.conn)

        pool = Pool()
        monkeypatch.setattr("src.server.database.pool.get_or_create_pool", lambda: pool)
        status = await refactor_status.database_status()
        assert status["schema_revision"] == "092"
        assert status["row_counts"]["agent_goals"] == 3
        assert status["row_counts"]["agent_tasks"] is None
        assert pool.closed is True

    async def test_render_with_database_reports_behind(self):
        text = refactor_status.render_status(
            flags={"goals": False},
            envs={"goals": "REFACTOR_GOALS_ENABLED"},
            head="092",
            database={"schema_revision": "085", "row_counts": {"agent_goals": None}},
        )
        assert "behind" in text
        assert "| agent_goals | absent |" in text


class TestCli:
    async def test_flags_only_json(self, tmp_path, capsys):
        out = tmp_path / "status.json"
        assert await refactor_status.main(["--json", str(out)]) == 0
        data = json.loads(out.read_text())
        assert data["schema_head"] == "151"
        assert set(data["flags"]) == {
            "goals",
            "events",
            "memory_index",
            "task_tree",
            "subagent_contract",
            "permissions",
            "approval_chain",
            "compaction_events",
        }
        assert "database" in data and data["database"] is None
        assert "schema head" in capsys.readouterr().out

    async def test_database_read_failure_exits_2(self, monkeypatch, capsys):
        async def boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(refactor_status, "database_status", boom)
        assert await refactor_status.main(["--database"]) == 2
        assert "db down" in capsys.readouterr().err
