"""Dispatch rows over a fake psycopg connection: no database, no socket."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from psycopg.types.json import Json

from src.server.database.dispatch import (
    DispatchNotFoundError,
    create_dispatch,
    delete_dispatch,
    get_dispatch,
    list_dispatches,
    pending_approval,
    save_dispatch,
)
from src.tools.auto_dispatch.types import Dispatch, DispatchState, Line, LineStatus, Outcome


class _Cursor:
    def __init__(self, rows=None, *, rowcount=1):
        self.rows = list(rows or [])
        self.rowcount = rowcount
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, sql, params=None):
        self.calls.append((" ".join(str(sql).split()), tuple(params or ())))

    async def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    async def fetchall(self):
        rows, self.rows = self.rows, []
        return rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Connection:
    def __init__(self, cursor):
        self.cur = cursor

    def cursor(self, row_factory=None):
        return self.cur

    @asynccontextmanager
    async def _connect(self):
        yield self


def _connection(cursor):
    conn = _Connection(cursor)

    @asynccontextmanager
    async def get_db_connection():
        yield conn

    return conn, get_db_connection


def _row(**overrides) -> dict:
    row = {
        "dispatch_id": "d1",
        "user_id": "u1",
        "goal": "ship the thing",
        "state": "SUBMITTED",
        "integrate_branch": "",
        "approval_token": None,
        "lines": [],
        "outcomes": {},
        "message": None,
        "created_at": datetime(2026, 11, 3, 9, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 11, 3, 9, 0, tzinfo=UTC),
    }
    row.update(overrides)
    return row


def _dispatch(**overrides) -> Dispatch:
    dispatch = Dispatch(id="d1", goal="ship the thing", user_id="u1", created_at="now")
    dispatch.lines = [Line(id="line-0", name="survey", task="read it")]
    dispatch.outcomes = {"line-0": Outcome("line-0", "codex", 12, 100, True)}
    for key, value in overrides.items():
        setattr(dispatch, key, value)
    return dispatch


@pytest.mark.asyncio
async def test_create_dispatch_inserts_the_dispatch_and_returns_the_row() -> None:
    cursor = _Cursor([_row()])
    _, fake = _connection(cursor)
    with patch("src.server.database.dispatch.get_db_connection", new=fake):
        saved = await create_dispatch(_dispatch())

    sql, params = cursor.calls[0]
    assert "INSERT INTO dispatches" in sql
    assert params[0] == "d1"
    assert params[3] == "SUBMITTED"
    assert isinstance(params[6], Json) and params[6].obj[0]["id"] == "line-0"
    assert isinstance(params[7], Json) and params[7].obj["line-0"]["model"] == "codex"
    assert saved.id == "d1"
    assert saved.goal == "ship the thing"
    assert saved.created_at.startswith("2026-11-03T09:00:00")


@pytest.mark.asyncio
async def test_save_dispatch_updates_every_mutable_column() -> None:
    cursor = _Cursor([_row(state="APPROVAL", approval_token="tok")])
    _, fake = _connection(cursor)
    dispatch = _dispatch(state=DispatchState.APPROVAL, approval_token="tok")
    with patch("src.server.database.dispatch.get_db_connection", new=fake):
        saved = await save_dispatch(dispatch)

    sql, params = cursor.calls[0]
    assert "UPDATE dispatches" in sql
    assert "updated_at = NOW()" in sql
    assert params[0] == "APPROVAL"
    assert params[2] == "tok"
    assert params[-1] == "d1"
    assert saved.state is DispatchState.APPROVAL


@pytest.mark.asyncio
async def test_save_dispatch_of_an_unknown_id_raises() -> None:
    cursor = _Cursor([])
    _, fake = _connection(cursor)
    with patch("src.server.database.dispatch.get_db_connection", new=fake):
        with pytest.raises(DispatchNotFoundError):
            await save_dispatch(_dispatch())


@pytest.mark.asyncio
async def test_get_dispatch_maps_jsonb_lines_and_outcomes() -> None:
    cursor = _Cursor(
        [
            _row(
                state="MONITORING",
                lines=[{"id": "line-0", "name": "survey", "task": "read it"}],
                outcomes={"line-0": Outcome("line-0", "codex", 3, 40, True).to_dict()},
            )
        ]
    )
    _, fake = _connection(cursor)
    with patch("src.server.database.dispatch.get_db_connection", new=fake):
        found = await get_dispatch("d1")

    assert found is not None
    assert found.state is DispatchState.MONITORING
    assert found.line("line-0") is not None
    assert found.line("line-0").status is LineStatus.PENDING
    assert found.outcomes["line-0"].model == "codex"


@pytest.mark.asyncio
async def test_get_dispatch_of_a_missing_row_is_none() -> None:
    cursor = _Cursor([])
    _, fake = _connection(cursor)
    with patch("src.server.database.dispatch.get_db_connection", new=fake):
        assert await get_dispatch("nope") is None


@pytest.mark.asyncio
async def test_list_dispatches_filters_by_user_and_state() -> None:
    cursor = _Cursor([_row(), _row(dispatch_id="d2")])
    _, fake = _connection(cursor)
    with patch("src.server.database.dispatch.get_db_connection", new=fake):
        rows = await list_dispatches(user_id="u1", states=["SUBMITTED", "APPROVAL"], limit=10)

    sql, params = cursor.calls[0]
    assert "user_id = %s" in sql and "state = ANY(%s)" in sql
    assert "ORDER BY created_at DESC LIMIT %s" in sql
    assert params == ("u1", ["SUBMITTED", "APPROVAL"], 10)
    assert [row.id for row in rows] == ["d1", "d2"]


@pytest.mark.asyncio
async def test_list_dispatches_without_filters_selects_everything() -> None:
    cursor = _Cursor([])
    _, fake = _connection(cursor)
    with patch("src.server.database.dispatch.get_db_connection", new=fake):
        assert await list_dispatches() == []

    sql, params = cursor.calls[0]
    assert "WHERE" not in sql
    assert params == (50,)


@pytest.mark.asyncio
async def test_pending_approval_asks_only_for_the_gate_state() -> None:
    cursor = _Cursor([_row(state="APPROVAL")])
    _, fake = _connection(cursor)
    with patch("src.server.database.dispatch.get_db_connection", new=fake):
        rows = await pending_approval("u1")

    _sql, params = cursor.calls[0]
    assert list(params[1]) == ["APPROVAL"]
    assert [row.state for row in rows] == [DispatchState.APPROVAL]


@pytest.mark.asyncio
async def test_delete_dispatch_reports_whether_a_row_went_away() -> None:
    cursor = _Cursor(rowcount=1)
    _, fake = _connection(cursor)
    with patch("src.server.database.dispatch.get_db_connection", new=fake):
        assert await delete_dispatch("d1") is True
    assert cursor.calls[0][0].startswith("DELETE FROM dispatches")

    empty = _Cursor(rowcount=0)
    _, fake_empty = _connection(empty)
    with patch("src.server.database.dispatch.get_db_connection", new=fake_empty):
        assert await delete_dispatch("d1") is False


@pytest.mark.asyncio
async def test_create_and_save_write_gates_and_spec_ref() -> None:
    gates = {"schema_version": "dispatch_gates_v1", "lines": {}}
    for call in (create_dispatch, save_dispatch):
        cursor = _Cursor([_row(gates=gates, spec_ref="spec/abc")])
        _, fake = _connection(cursor)
        with patch("src.server.database.dispatch.get_db_connection", new=fake):
            saved = await call(_dispatch(gates=gates, spec_ref="spec/abc"))
        sql, params = cursor.calls[0]
        assert "gates" in sql and "spec_ref" in sql
        assert any(isinstance(p, Json) and p.obj == gates for p in params)
        assert "spec/abc" in params
        # N45: loading stamps the gates schema revision; nothing else changes.
        assert {k: v for k, v in saved.gates.items() if k != "schema_revision"} == gates
        assert saved.gates.get("schema_revision") in (None, 1)
        assert saved.spec_ref == "spec/abc"


@pytest.mark.asyncio
async def test_get_dispatch_reads_gates_and_spec_ref_and_tolerates_a_pre_141_row() -> None:
    cursor = _Cursor([_row(gates={"lines": {"line-0": {}}}, spec_ref="spec/x"), _row()])
    _, fake = _connection(cursor)
    with patch("src.server.database.dispatch.get_db_connection", new=fake):
        found = await get_dispatch("d1")
        legacy = await get_dispatch("d1")

    assert found is not None and found.gates == {"lines": {"line-0": {}}}
    assert found.spec_ref == "spec/x"
    assert legacy is not None and legacy.gates == {} and legacy.spec_ref == ""
