"""Ticket worktree binding persistence: the fences that keep a checkout honest.

No database: ``_FakeTickets`` keeps Ticket rows as dicts and *interprets* the
SQL the helpers send (WHERE conjuncts, SET assignments, RETURNING lists). A
fence that is missing from the SQL is therefore missing from the evaluation
too, so these tests fail if a helper stops fencing, rather than replaying
canned results. Unknown SQL shapes raise, so the interpreter cannot silently
accept a predicate it does not understand.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from src.server.database.autonomy import daemons, tickets
from src.server.models import autonomy as models

T0 = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
SHA = "a" * 40
OTHER_SHA = "b" * 40
DAEMON = str(uuid4())
OTHER_DAEMON = str(uuid4())
LEASE = str(uuid4())
OTHER_LEASE = str(uuid4())

WORKTREE_COLUMNS = (
    "worktree_path",
    "worktree_generation",
    "worktree_cleanup_pending",
    "worktree_cleanup_attempts",
    "worktree_cleanup_next_retry_at",
    "worktree_branch",
    "worktree_base_sha",
    "worktree_base_branch",
    "worktree_repo",
    "worktree_host",
    "worktree_daemon_id",
    "worktree_lease_id",
    "worktree_stale_cleanups",
)


def _columns(text: str) -> list[str]:
    return [c.strip() for c in text.replace("\n", " ").split(",") if c.strip()]


# ---------------------------------------------------------------- fake database


def _split_top(expr: str, sep: str) -> list[str]:
    """Split on ``sep`` outside parentheses and quotes."""
    parts: list[str] = []
    depth = 0
    quoted = False
    start = 0
    i = 0
    while i < len(expr):
        ch = expr[i]
        if ch == "'":
            quoted = not quoted
        elif not quoted and ch == "(":
            depth += 1
        elif not quoted and ch == ")":
            depth -= 1
        elif not quoted and depth == 0 and expr.startswith(sep, i):
            parts.append(expr[start:i].strip())
            i += len(sep)
            start = i
            continue
        i += 1
    parts.append(expr[start:].strip())
    return parts


def _wrapped(expr: str) -> bool:
    if not (expr.startswith("(") and expr.endswith(")")):
        return False
    depth = 0
    for i, ch in enumerate(expr):
        depth += ch == "("
        depth -= ch == ")"
        if depth == 0 and i < len(expr) - 1:
            return False
    return True


_PARAM = r"%\((\w+)\)s(?:::uuid|::jsonb)?"


def _param(value: Any) -> Any:
    """A bound value as the database would see it (``Json`` wrappers unwrapped)."""
    return getattr(value, "obj", value)


class _FakeTickets:
    """In-memory autonomy_tickets that evaluates the statements it receives."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.now = T0
        self.revoked_leases: set[str] = set()
        self.statements: list[str] = []

    # -- seeding
    def add(self, ticket_id: str = "t-1", **overrides: Any) -> dict[str, Any]:
        row: dict[str, Any] = {c: None for c in _columns(tickets.TICKET_COLUMNS)}
        row.update(
            ticket_id=ticket_id,
            user_id="u1",
            workspace_id="ws-1",
            title="Ticket",
            scope="do the work",
            status="running",
            mandate={},
            worker_provider="claude-code",
            worker_model="",
            worker_daemon_id=None,
            worker_lease_id=None,
            worker_lease_claim_id=None,
            worker_lease_expires_at=None,
            attempts=1,
            summary="",
            error="",
            revision=4,
            switches=[],
            created_at=T0,
            updated_at=T0,
            closed_at=None,
            close_reason="",
            worktree_path="",
            worktree_generation=0,
            worktree_cleanup_pending=False,
            worktree_cleanup_attempts=0,
            worktree_cleanup_next_retry_at=None,
            worktree_branch="",
            worktree_base_sha=SHA,
            worktree_base_branch="origin/main",
            worktree_repo="",
            worktree_host="",
            worktree_daemon_id=None,
            worktree_lease_id=None,
            worktree_stale_cleanups=[],
        )
        row.update(overrides)
        self.rows[ticket_id] = row
        return row

    @asynccontextmanager
    async def connection(self, conn: Any = None):
        yield _FakeConnection(self)

    # -- interpreter
    def run(self, sql: str, params: Any) -> tuple[int, list[dict[str, Any]]]:
        text = " ".join(sql.split()).replace("( ", "(").replace(" )", ")")
        self.statements.append(text)
        assert isinstance(params, dict), "worktree helpers bind named parameters"
        update = re.fullmatch(
            r"UPDATE autonomy_tickets SET (.*?) WHERE (.*?)(?: RETURNING (.*))?", text
        )
        if update:
            sets, where, returning = update.groups()
            hits = [r for r in self.rows.values() if self._expr(r, where, params)]
            for row in hits:
                new = {}
                for assignment in _split_top(sets, ","):
                    col, expr = (s.strip() for s in assignment.split("=", 1))
                    assert col in row, f"unknown column {col}"
                    new[col] = self._value(row, expr, params)
                row.update(new)
            out = [self._project(r, returning) for r in hits] if returning else []
            return len(hits), out
        select = re.fullmatch(
            r"SELECT (.*?) FROM autonomy_tickets WHERE (.*?)( FOR UPDATE)?", text
        )
        if select:
            cols, where, _lock = select.groups()
            hits = [r for r in self.rows.values() if self._expr(r, where, params)]
            return len(hits), [self._project(r, cols) for r in hits]
        raise AssertionError(f"unexpected SQL: {text}")

    def _project(self, row: dict[str, Any], cols: str) -> dict[str, Any]:
        out = {}
        for col in _columns(cols):
            assert col in row, f"RETURNING/SELECT names unknown column {col}"
            out[col] = row[col]
        return out

    def _expr(self, row: dict[str, Any], expr: str, params: dict[str, Any]) -> bool:
        expr = expr.strip()
        ors = _split_top(expr, " OR ")
        if len(ors) > 1:
            return any(self._expr(row, part, params) for part in ors)
        ands = _split_top(expr, " AND ")
        if len(ands) > 1:
            return all(self._expr(row, part, params) for part in ands)
        if _wrapped(expr):
            return self._expr(row, expr[1:-1], params)
        return self._atom(row, expr, params)

    def _atom(self, row: dict[str, Any], expr: str, params: dict[str, Any]) -> bool:
        m = re.fullmatch(
            r"NOT EXISTS \(SELECT 1 FROM autonomy_daemon_claims c WHERE c\.lease_id = "
            + _PARAM
            + r" AND c\.revoked_at IS NOT NULL\)",
            expr,
        )
        if m:
            return str(params[m.group(1)]) not in self.revoked_leases
        m = re.fullmatch(
            r"ticket_id IN \(SELECT ticket_id FROM autonomy_tickets WHERE (.*) "
            r"ORDER BY .*? LIMIT " + _PARAM + r" FOR UPDATE SKIP LOCKED\)",
            expr,
        )
        if m:
            inner, limit_key = m.groups()
            due = [r for r in self.rows.values() if self._expr(r, inner, params)]
            due.sort(
                key=lambda r: (
                    r["worktree_cleanup_next_retry_at"] is not None,
                    r["worktree_cleanup_next_retry_at"] or T0,
                    r["ticket_id"],
                )
            )
            return row["ticket_id"] in {r["ticket_id"] for r in due[: params[limit_key]]}
        if expr == "TRUE":
            return True
        if m := re.fullmatch(r"NOT (\w+)", expr):
            return not row[m.group(1)]
        if re.fullmatch(r"\w+", expr):
            return bool(row[expr])
        if m := re.fullmatch(r"(\w+) IS (NOT )?NULL", expr):
            return (row[m.group(1)] is None) != bool(m.group(2))
        if m := re.fullmatch(r"(\w+) IS DISTINCT FROM (\w+)", expr):
            left, right = row[m.group(1)], row[m.group(2)]
            return (None if left is None else str(left)) != (
                None if right is None else str(right)
            )
        if m := re.fullmatch(r"(\w+) = ANY\(" + _PARAM + r"\)", expr):
            return row[m.group(1)] in params[m.group(2)]
        if m := re.fullmatch(r"(\w+) IN \('', " + _PARAM + r"\)", expr):
            return row[m.group(1)] in ("", params[m.group(2)])
        if m := re.fullmatch(r"(\w+) (=|<>|>=|<=|>|<) (.+)", expr):
            col, op, rhs = m.groups()
            left, right = row[col], self._value(row, rhs, params)
            if left is None or right is None:
                return False  # SQL NULL comparison
            if isinstance(left, str):
                right = str(right)  # uuid columns are stored as text here
            return {
                "=": left == right,
                "<>": left != right,
                ">": left > right,
                "<": left < right,
                ">=": left >= right,
                "<=": left <= right,
            }[op]
        raise AssertionError(f"fake cannot evaluate predicate: {expr}")

    def _value(self, row: dict[str, Any], expr: str, params: dict[str, Any]) -> Any:
        expr = expr.strip()
        if expr == "''":
            return ""
        if m := re.fullmatch(r"'([a-z_]+)'", expr):
            return m.group(1)
        if expr == "NULL":
            return None
        if expr in ("TRUE", "FALSE"):
            return expr == "TRUE"
        if re.fullmatch(r"-?\d+", expr):
            return int(expr)
        if expr in ("NOW()", "clock_timestamp()"):
            return self.now
        if m := re.fullmatch(_PARAM, expr):
            value = _param(params[m.group(1)])
            return str(value) if expr.endswith("::uuid") and value is not None else value
        if m := re.fullmatch(r"(\w+) \|\| " + _PARAM, expr):
            return [*row[m.group(1)], *_param(params[m.group(2)])]
        if re.fullmatch(r"[a-z_]+", expr) and expr in row:
            return row[expr]  # column reference, e.g. worker_daemon_id = worktree_daemon_id
        if m := re.fullmatch(r"(\w+) \+ 1", expr):
            return row[m.group(1)] + 1
        if m := re.fullmatch(
            r"clock_timestamp\(\) \+ make_interval\(secs => " + _PARAM + r"\)", expr
        ):
            return self.now + timedelta(seconds=float(params[m.group(1)]))
        raise AssertionError(f"fake cannot evaluate value: {expr}")


class _FakeCursor:
    def __init__(self, db: _FakeTickets) -> None:
        self.db = db
        self.rowcount = 0
        self._rows: list[dict[str, Any]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql: str, params: Any = None):
        self.rowcount, self._rows = self.db.run(sql, params)

    async def fetchone(self):
        return dict(self._rows[0]) if self._rows else None

    async def fetchall(self):
        return [dict(r) for r in self._rows]


class _FakeConnection:
    def __init__(self, db: _FakeTickets) -> None:
        self.db = db

    @asynccontextmanager
    async def transaction(self):
        yield

    def cursor(self, row_factory: Any = None):
        return _FakeCursor(self.db)


@pytest.fixture
def db(monkeypatch) -> _FakeTickets:
    fake = _FakeTickets()
    monkeypatch.setattr(tickets, "get_db_connection", fake.connection)
    return fake


async def _server_bind(path: str = "/srv/wt/t-1", **kw: Any):
    kw.setdefault("branch", "ticket/t-1")
    kw.setdefault("base_sha", SHA)
    kw.setdefault("repo", "/srv/repo")
    return await tickets.bind_ticket_worktree(
        "t-1", worktree_path=path, host=tickets.WORKTREE_HOST_SERVER, **kw
    )


async def _daemon_bind(
    path: str = "/home/me/wt/t-1",
    *,
    daemon_id: str = DAEMON,
    lease_id: str = LEASE,
    **kw: Any,
):
    kw.setdefault("branch", "ticket/t-1")
    kw.setdefault("base_sha", SHA)
    return await tickets.bind_ticket_worktree(
        "t-1",
        worktree_path=path,
        repo="",
        host=tickets.WORKTREE_HOST_DAEMON,
        daemon_id=daemon_id,
        lease_id=lease_id,
        **kw,
    )


def _daemon_ticket(db: _FakeTickets, **overrides: Any) -> dict[str, Any]:
    return db.add(
        worker_daemon_id=DAEMON,
        worker_lease_id=LEASE,
        worker_lease_expires_at=T0 + timedelta(seconds=60),
        **overrides,
    )


# ---------------------------------------------------------------- server binds


@pytest.mark.asyncio
async def test_server_bind_rejects_stale_revision(db):
    row = db.add(revision=4)

    assert await _server_bind(expected_revision=3) is None
    assert row["worktree_path"] == "" and row["worktree_generation"] == 0
    assert row["revision"] == 4

    bound = await _server_bind(expected_revision=4)
    assert bound["worktree_path"] == "/srv/wt/t-1"
    assert bound["worktree_host"] == "server"
    assert bound["worktree_generation"] == 1
    assert bound["revision"] == 5


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "completed"},
        {"status": "cancelled"},
        {"status": "archived"},
        {"status": "failed"},
        {"status": "running", "closed_at": T0},
    ],
    ids=["completed", "cancelled", "archived", "failed", "closed_at"],
)
@pytest.mark.asyncio
async def test_server_bind_rejects_closed_ticket(db, overrides):
    row = db.add(**overrides)

    assert await _server_bind(expected_revision=4) is None
    assert row["worktree_path"] == ""
    assert row["worktree_generation"] == 0


@pytest.mark.asyncio
async def test_server_bind_rejected_after_local_to_daemon_reassignment(db):
    # The Ticket moved to a daemon: revision is current but the server must
    # never persist its own path onto a daemon-owned Ticket.
    row = db.add(worker_daemon_id=DAEMON, revision=7)

    assert await _server_bind(expected_revision=7) is None
    assert row["worktree_path"] == ""


@pytest.mark.asyncio
async def test_bind_requires_the_matching_base_pin(db):
    row = db.add(worktree_base_sha="")
    assert await _server_bind() is None  # no pin yet
    row["worktree_base_sha"] = OTHER_SHA
    assert await _server_bind() is None  # pin differs
    assert row["worktree_path"] == ""


@pytest.mark.asyncio
async def test_bind_never_replaces_another_live_checkout(db):
    row = db.add()
    await _server_bind("/srv/wt/a")

    assert await _server_bind("/srv/wt/b") is None
    assert row["worktree_path"] == "/srv/wt/a"
    assert row["worktree_generation"] == 1


@pytest.mark.asyncio
async def test_server_bind_with_no_revision_still_fences_open_state(db):
    db.add(status="completed")
    assert await _server_bind() is None


# ---------------------------------------------------------------- daemon binds


@pytest.mark.asyncio
async def test_daemon_bind_with_live_lease_binds_daemon_host(db):
    _daemon_ticket(db)

    bound = await _daemon_bind()

    assert bound["worktree_path"] == "/home/me/wt/t-1"
    assert bound["worktree_host"] == "daemon"
    assert bound["worktree_daemon_id"] == DAEMON
    assert bound["worktree_lease_id"] == LEASE
    assert bound["worktree_repo"] == ""
    assert bound["worktree_generation"] == 1


@pytest.mark.asyncio
async def test_daemon_bind_rejected_for_expired_lease(db):
    row = _daemon_ticket(db)
    db.now = T0 + timedelta(seconds=61)  # lease ran out after the HTTP check

    assert await _daemon_bind() is None
    assert row["worktree_path"] == ""


@pytest.mark.asyncio
async def test_daemon_bind_rejected_for_foreign_lease(db):
    row = _daemon_ticket(db)

    assert await _daemon_bind(lease_id=OTHER_LEASE) is None
    assert await _daemon_bind(daemon_id=OTHER_DAEMON) is None
    assert row["worktree_path"] == ""


@pytest.mark.asyncio
async def test_daemon_bind_rejected_for_revoked_lease(db):
    row = _daemon_ticket(db)
    db.revoked_leases.add(LEASE)

    assert await _daemon_bind() is None
    assert row["worktree_path"] == ""


@pytest.mark.asyncio
async def test_daemon_bind_rejected_after_reassignment_to_another_daemon(db):
    row = _daemon_ticket(db)
    # Reassigned: worker_daemon_id changed and a new lease was granted.
    row.update(worker_daemon_id=OTHER_DAEMON, worker_lease_id=OTHER_LEASE)

    assert await _daemon_bind() is None
    assert row["worktree_path"] == ""


@pytest.mark.asyncio
async def test_daemon_bind_rejected_after_reassignment_back_to_local(db):
    row = _daemon_ticket(db)
    row.update(worker_daemon_id=None, worker_lease_id=None, worker_lease_expires_at=None)

    assert await _daemon_bind() is None
    assert row["worktree_path"] == ""


@pytest.mark.asyncio
async def test_daemon_cannot_take_over_a_server_checkout_with_the_same_path(db):
    row = db.add()
    await _server_bind("/same/path")
    row.update(
        worker_daemon_id=DAEMON,
        worker_lease_id=LEASE,
        worker_lease_expires_at=T0 + timedelta(seconds=60),
    )

    assert await _daemon_bind("/same/path") is None
    assert row["worktree_host"] == "server"


@pytest.mark.asyncio
async def test_server_cannot_take_over_a_daemon_checkout_with_the_same_path(db):
    row = _daemon_ticket(db)
    await _daemon_bind("/same/path")
    # Reassigned back to local before the daemon cleaned up.
    row.update(worker_daemon_id=None, worker_lease_id=None, worker_lease_expires_at=None)

    assert await _server_bind("/same/path") is None
    assert row["worktree_host"] == "daemon"
    assert row["worktree_daemon_id"] == DAEMON


@pytest.mark.asyncio
async def test_daemon_cannot_take_over_another_daemons_checkout(db):
    row = _daemon_ticket(db)
    await _daemon_bind("/same/path")
    row.update(worker_daemon_id=OTHER_DAEMON, worker_lease_id=OTHER_LEASE)

    assert await _daemon_bind("/same/path", daemon_id=OTHER_DAEMON, lease_id=OTHER_LEASE) is None
    assert row["worktree_daemon_id"] == DAEMON
    assert row["worktree_generation"] == 1


# ---------------------------------------------------------------- replay


@pytest.mark.asyncio
async def test_idempotent_replay_does_not_bump_generation(db):
    row = _daemon_ticket(db)
    first = await _daemon_bind()
    revision = row["revision"]

    replay = await _daemon_bind()

    assert replay["worktree_generation"] == first["worktree_generation"] == 1
    assert row["revision"] == revision
    assert not any(s.startswith("UPDATE") for s in db.statements[2:])


@pytest.mark.asyncio
async def test_server_replay_does_not_bump_generation(db):
    row = db.add()
    await _server_bind(expected_revision=4)

    replay = await _server_bind(expected_revision=5)

    assert replay["worktree_generation"] == 1
    assert row["revision"] == 5


@pytest.mark.asyncio
async def test_rebind_under_a_new_lease_advances_generation(db):
    # A blocked Ticket resumed under a new lease reuses its checkout but is a
    # new attempt: cleanup must now fence on the new generation and lease.
    row = _daemon_ticket(db)
    await _daemon_bind()
    row["worker_lease_id"] = OTHER_LEASE

    bound = await _daemon_bind(lease_id=OTHER_LEASE)

    assert bound["worktree_generation"] == 2
    assert bound["worktree_lease_id"] == OTHER_LEASE


@pytest.mark.asyncio
async def test_replay_of_a_fenced_out_bind_is_still_refused(db):
    row = _daemon_ticket(db)
    await _daemon_bind()
    row["status"] = "completed"

    assert await _daemon_bind() is None


# ---------------------------------------------------------------- base pin


@pytest.mark.asyncio
async def test_pin_first_write_wins_without_revision_bump(db):
    row = db.add(worktree_base_sha="", worktree_base_branch="")

    first = await tickets.pin_ticket_worktree_base("t-1", base_sha=SHA, base_branch="origin/main")
    second = await tickets.pin_ticket_worktree_base(
        "t-1", base_sha=OTHER_SHA, base_branch="origin/dev"
    )

    assert first["worktree_base_sha"] == SHA
    assert second["worktree_base_sha"] == SHA
    assert second["worktree_base_branch"] == "origin/main"
    assert row["revision"] == 4


@pytest.mark.asyncio
async def test_pin_missing_ticket_returns_none(db):
    assert await tickets.pin_ticket_worktree_base("nope", base_sha=SHA, base_branch="x") is None


@pytest.mark.asyncio
async def test_pin_survives_clear(db):
    row = db.add()
    bound = await _server_bind()

    assert await tickets.clear_ticket_worktree_binding(
        "t-1", worktree_path=bound["worktree_path"], worktree_generation=1
    )
    assert row["worktree_base_sha"] == SHA
    assert row["worktree_generation"] == 1


# ---------------------------------------------------------------- cleanup retry


@pytest.mark.asyncio
async def test_failed_cleanup_is_durable_and_retried_until_cleared(db):
    row = db.add()
    bound = await _server_bind(expected_revision=4)
    path, gen = bound["worktree_path"], bound["worktree_generation"]

    # Removal failed: pending keeps the binding, never rewrites it.
    assert await tickets.record_worktree_cleanup_pending(
        "t-1", worktree_path=path, worktree_generation=gen, delay_seconds=0
    )
    assert row["worktree_cleanup_pending"] is True
    assert row["worktree_cleanup_attempts"] == 1
    assert (row["worktree_path"], row["worktree_generation"]) == (path, gen)
    assert row["worktree_branch"] == "ticket/t-1"
    # No other checkout may bind while the removal is still owed.
    assert await _server_bind("/srv/wt/other") is None
    assert row["worktree_generation"] == gen
    assert row["worktree_cleanup_pending"] is True

    claimed = await tickets.claim_worktree_cleanup_pending(host="server", lease_seconds=60)
    assert [(c["ticket_id"], c["worktree_path"], c["worktree_generation"]) for c in claimed] == [
        ("t-1", path, gen)
    ]
    # Leased: a concurrent sweep does not get the same row.
    assert await tickets.claim_worktree_cleanup_pending(host="server") == []

    # The retry failed too.
    assert await tickets.reschedule_worktree_cleanup_pending(
        "t-1", worktree_path=path, worktree_generation=gen, delay_seconds=30
    )
    assert row["worktree_cleanup_attempts"] == 2
    db.now = T0 + timedelta(seconds=20)
    assert await tickets.claim_worktree_cleanup_pending(host="server") == []
    db.now = T0 + timedelta(seconds=31)
    again = await tickets.claim_worktree_cleanup_pending(host="server")
    assert [(c["worktree_path"], c["worktree_generation"]) for c in again] == [(path, gen)]

    # This retry removed it.
    assert await tickets.clear_ticket_worktree_binding(
        "t-1", worktree_path=path, worktree_generation=gen
    )
    assert row["worktree_path"] == "" and row["worktree_host"] == ""
    assert row["worktree_cleanup_pending"] is False
    assert row["worktree_cleanup_attempts"] == 0
    assert row["worktree_cleanup_next_retry_at"] is None
    assert row["worktree_generation"] == gen
    db.now = T0 + timedelta(hours=1)
    assert await tickets.claim_worktree_cleanup_pending(host="server") == []


@pytest.mark.asyncio
async def test_claim_only_returns_rows_of_the_requested_host(db):
    db.add("t-srv", worktree_path="/srv/wt/x", worktree_host="server", worktree_generation=1,
           worktree_cleanup_pending=True)
    db.add("t-dmn", worktree_path="/home/wt/x", worktree_host="daemon", worktree_generation=1,
           worktree_cleanup_pending=True)

    claimed = await tickets.claim_worktree_cleanup_pending(host="server")

    assert [c["ticket_id"] for c in claimed] == ["t-srv"]


@pytest.mark.asyncio
async def test_reschedule_only_touches_pending_rows(db):
    row = db.add()
    await _server_bind()

    assert not await tickets.reschedule_worktree_cleanup_pending(
        "t-1", worktree_path="/srv/wt/t-1", worktree_generation=1, delay_seconds=5
    )
    assert row["worktree_cleanup_attempts"] == 0


@pytest.mark.asyncio
async def test_stale_attempt_leaves_a_newer_binding_untouched(db):
    row = db.add()
    await _server_bind()
    assert await tickets.clear_ticket_worktree_binding(
        "t-1", worktree_path="/srv/wt/t-1", worktree_generation=1
    )
    await _server_bind()  # same path, new attempt
    assert row["worktree_generation"] == 2
    before = dict(row)

    assert not await tickets.record_worktree_cleanup_pending(
        "t-1", worktree_path="/srv/wt/t-1", worktree_generation=1
    )
    assert not await tickets.clear_ticket_worktree_binding(
        "t-1", worktree_path="/srv/wt/t-1", worktree_generation=1
    )
    assert not await tickets.reschedule_worktree_cleanup_pending(
        "t-1", worktree_path="/srv/wt/t-1", worktree_generation=1, delay_seconds=1
    )
    assert row == before


@pytest.mark.asyncio
async def test_lease_fenced_cleanup_ignores_another_lease(db):
    row = _daemon_ticket(db)
    await _daemon_bind()
    before = dict(row)

    assert not await tickets.clear_ticket_worktree_binding(
        "t-1", worktree_path="/home/me/wt/t-1", worktree_generation=1, lease_id=OTHER_LEASE
    )
    assert not await tickets.record_worktree_cleanup_pending(
        "t-1", worktree_path="/home/me/wt/t-1", worktree_generation=1, lease_id=OTHER_LEASE
    )
    assert row == before
    assert await tickets.clear_ticket_worktree_binding(
        "t-1", worktree_path="/home/me/wt/t-1", worktree_generation=1, lease_id=LEASE
    )
    assert row["worktree_lease_id"] is None and row["worktree_daemon_id"] is None


@pytest.mark.asyncio
async def test_cleanup_helpers_without_a_path_match_nothing(db):
    row = db.add()
    await _server_bind()
    before = dict(row)

    assert not await tickets.clear_ticket_worktree_binding(
        "t-1", worktree_path="", worktree_generation=1
    )
    assert not await tickets.record_worktree_cleanup_pending(
        "t-1", worktree_path="", worktree_generation=1
    )
    assert not await tickets.reschedule_worktree_cleanup_pending(
        "t-1", worktree_path="", worktree_generation=1, delay_seconds=1
    )
    assert row == before


# ---------------------------------------------------------------- update_ticket


@pytest.mark.parametrize("field", WORKTREE_COLUMNS)
@pytest.mark.asyncio
async def test_update_ticket_refuses_worktree_fields(monkeypatch, field):
    @asynccontextmanager
    async def _no_db(conn=None):
        raise AssertionError("update_ticket must refuse before touching the database")
        yield

    monkeypatch.setattr(tickets, "get_db_connection", _no_db)

    with pytest.raises(ValueError, match="Ticket worktree helpers"):
        await tickets.update_ticket("t-1", summary="x", **{field: "/tmp/x"})


# ---------------------------------------------------------------- columns / claims


@pytest.mark.parametrize(
    "columns", [tickets.TICKET_COLUMNS, daemons._TICKET_COLUMNS], ids=["tickets", "daemons"]
)
def test_ticket_columns_carry_every_binding_column(columns):
    assert set(WORKTREE_COLUMNS) <= set(_columns(columns))


def test_claim_result_keeps_lease_fields_and_worktree_binding():
    expires = T0 + timedelta(seconds=90)
    row = {
        "ticket_id": "t-1",
        "worktree_path": "/home/me/wt/t-1",
        "worktree_generation": 3,
        "worktree_daemon_id": uuid4(),
        "worktree_lease_id": uuid4(),
    }

    out = daemons._claim_result(
        row, lease_id=LEASE, lease_expires_at=expires, lease_duration_seconds=90
    )

    assert out["lease_id"] == LEASE
    assert out["lease_expires_at"] == expires
    assert out["lease_duration_seconds"] == 90
    assert out["worktree_path"] == "/home/me/wt/t-1"
    assert out["worktree_generation"] == 3
    assert out["worktree_daemon_id"] == str(row["worktree_daemon_id"])
    assert out["worktree_lease_id"] == str(row["worktree_lease_id"])


@pytest.mark.asyncio
async def test_active_claim_returns_lease_fields_and_worktree_path(monkeypatch):
    expires = T0 + timedelta(seconds=40)
    stored = {
        "ticket_id": "t-1",
        "worker_lease_id": LEASE,
        "worker_lease_expires_at": expires,
        "remaining": 40,
        "policy_version": "p1",
        "input_snapshot": [],
        "worktree_path": "/home/me/wt/t-1",
        "worktree_generation": 2,
        "worktree_daemon_id": UUID(DAEMON),
        "worktree_lease_id": UUID(LEASE),
    }

    class _Cur:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, sql, params=None):
            pass

        async def fetchone(self):
            return dict(stored)

    class _Conn:
        def cursor(self, row_factory=None):
            return _Cur()

    @asynccontextmanager
    async def _conn(conn=None):
        yield _Conn()

    monkeypatch.setattr(daemons, "get_db_connection", _conn)

    out = await daemons.active_claim(DAEMON, LEASE)

    assert {k: out[k] for k in ("lease_id", "lease_expires_at", "lease_duration_seconds")} == {
        "lease_id": LEASE,
        "lease_expires_at": expires,
        "lease_duration_seconds": 40,
    }
    assert out["worktree_path"] == "/home/me/wt/t-1"
    assert out["worktree_generation"] == 2
    # JSON-safe binding for the claim response.
    assert (out["worktree_daemon_id"], out["worktree_lease_id"]) == (DAEMON, LEASE)


def test_ticket_view_exposes_the_binding():
    row = {c: None for c in _columns(tickets.TICKET_COLUMNS)}
    row.update(
        ticket_id="t-1",
        user_id="u1",
        workspace_id="ws",
        title="t",
        scope="s",
        status="blocked",
        mandate={},
        origin_thread_key="k",
        attempts=1,
        summary="",
        error="",
        created_at=T0,
        updated_at=T0,
        close_reason="",
        switches=[],
        worker_provider="",
        worker_model="",
        worktree_path="/home/me/wt/t-1",
        worktree_generation=2,
        worktree_branch="ticket/t-1",
        worktree_base_sha=SHA,
        worktree_host="daemon",
        worktree_cleanup_pending=True,
    )

    view = models.TicketView(**row)

    assert (
        view.worktree_path,
        view.worktree_generation,
        view.worktree_branch,
        view.worktree_base_sha,
        view.worktree_host,
        view.worktree_cleanup_pending,
    ) == ("/home/me/wt/t-1", 2, "ticket/t-1", SHA, "daemon", True)


def test_daemon_worktree_wire_models():
    bind = models.DaemonWorktreeBind(
        lease_id=LEASE,
        worktree_path="/home/me/wt/t-1",
        branch="conductor/ticket/t-1",
        base_sha=SHA,
    )
    assert bind.branch == "conductor/ticket/t-1"

    cleanup = models.DaemonWorktreeCleanup(
        worktree_path="/home/me/wt/t-1", worktree_generation=1, outcome="failed"
    )
    assert cleanup.lease_id is None and cleanup.detail == ""
    with pytest.raises(ValueError):
        models.DaemonWorktreeCleanup(
            worktree_path="/x", worktree_generation=0, outcome="removed"
        )
    with pytest.raises(ValueError):
        models.DaemonWorktreeCleanup(worktree_path="/x", worktree_generation=1, outcome="maybe")


# ---------------------------------------------------------------- lock


class _LockSession:
    def __init__(self, grants: bool) -> None:
        self.grants = grants
        self.calls: list[tuple[str, tuple]] = []
        self.closed = False

    def cursor(self):
        session = self

        class _Cur:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def execute(self, sql, params=()):
                session.calls.append((sql, tuple(params)))

            async def fetchone(self):
                return (session.grants,)

        return _Cur()

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_ticket_worktree_lock_holds_a_session_advisory_lock(monkeypatch):
    session = _LockSession(grants=True)

    async def _open():
        return session

    monkeypatch.setattr(tickets, "open_session_connection", _open)

    async with tickets.ticket_worktree_lock("t-1"):
        assert "pg_try_advisory_lock" in session.calls[0][0]
        assert session.calls[0][1][0] == "t-1"
        assert not any("unlock" in sql for sql, _ in session.calls)

    assert "pg_advisory_unlock" in session.calls[-1][0]
    assert session.calls[-1][1] == session.calls[0][1]
    assert session.closed


@pytest.mark.asyncio
async def test_ticket_worktree_lock_times_out(monkeypatch):
    session = _LockSession(grants=False)

    async def _open():
        return session

    monkeypatch.setattr(tickets, "open_session_connection", _open)

    with pytest.raises(TimeoutError):
        async with tickets.ticket_worktree_lock("t-1", timeout_seconds=0.1):
            pytest.fail("lock must not be entered")
    assert session.closed


# ---------------------------------------------------------------- pending takeover


def _pending(row: dict[str, Any], attempts: int = 3) -> None:
    row.update(
        worktree_cleanup_pending=True,
        worktree_cleanup_attempts=attempts,
        worktree_cleanup_next_retry_at=T0 + timedelta(seconds=30),
    )


@pytest.mark.asyncio
async def test_server_same_path_bind_takes_over_a_pending_checkout(db):
    # Stopped, removal failed (run still winding down), then resumed before
    # the retry: the reopened Ticket takes its checkout back instead of
    # wedging behind its own cleanup.
    row = db.add()
    await _server_bind()
    _pending(row)
    revision = row["revision"]

    bound = await _server_bind()

    assert bound is not None
    assert bound["worktree_path"] == "/srv/wt/t-1"
    assert row["worktree_cleanup_pending"] is False
    assert row["worktree_cleanup_attempts"] == 0
    assert row["worktree_cleanup_next_retry_at"] is None
    # A same-path replay while pending is still a new attempt: a sweep that
    # claimed generation 1 must fence out.
    assert row["worktree_generation"] == 2
    assert row["revision"] == revision + 1
    assert not await tickets.clear_ticket_worktree_binding(
        "t-1", worktree_path="/srv/wt/t-1", worktree_generation=1
    )
    assert not await tickets.record_worktree_cleanup_pending(
        "t-1", worktree_path="/srv/wt/t-1", worktree_generation=1
    )
    assert row["worktree_path"] == "/srv/wt/t-1"
    assert row["worktree_cleanup_pending"] is False
    # A replay of the takeover itself is idempotent again.
    assert (await _server_bind())["worktree_generation"] == 2
    assert await tickets.claim_worktree_cleanup_pending(host="server") == []


@pytest.mark.asyncio
async def test_pending_checkout_still_refuses_a_different_path(db):
    row = db.add()
    await _server_bind()
    _pending(row)
    before = dict(row)

    assert await _server_bind("/srv/wt/other") is None
    assert row == before


@pytest.mark.asyncio
async def test_daemon_same_path_bind_takes_over_its_own_pending_checkout(db):
    row = _daemon_ticket(db)
    await _daemon_bind()
    _pending(row)
    row["worker_lease_id"] = OTHER_LEASE  # resumed under a new lease

    bound = await _daemon_bind(lease_id=OTHER_LEASE)

    assert bound["worktree_lease_id"] == OTHER_LEASE
    assert bound["worktree_generation"] == 2
    assert row["worktree_cleanup_pending"] is False
    assert row["worktree_cleanup_attempts"] == 0


@pytest.mark.asyncio
async def test_pending_takeover_needs_the_same_host_and_daemon(db):
    row = _daemon_ticket(db)
    await _daemon_bind("/same/path")
    _pending(row)
    before = dict(row)

    # Another daemon now holds the Ticket under the same path string.
    row.update(worker_daemon_id=OTHER_DAEMON, worker_lease_id=OTHER_LEASE)
    assert await _daemon_bind("/same/path", daemon_id=OTHER_DAEMON, lease_id=OTHER_LEASE) is None
    # Back to the server under the same path string.
    row.update(worker_daemon_id=None, worker_lease_id=None, worker_lease_expires_at=None)
    assert await _server_bind("/same/path") is None
    row.update(
        worker_daemon_id=before["worker_daemon_id"],
        worker_lease_id=before["worker_lease_id"],
        worker_lease_expires_at=before["worker_lease_expires_at"],
    )
    assert row == before


# ---------------------------------------------------------------- cancel pending


async def _cancel(**kw: Any) -> bool:
    kw.setdefault("worktree_path", "/srv/wt/t-1")
    kw.setdefault("worktree_generation", 1)
    kw.setdefault("host", tickets.WORKTREE_HOST_SERVER)
    return await tickets.cancel_worktree_cleanup_pending("t-1", **kw)


@pytest.mark.asyncio
async def test_cancel_pending_keeps_the_binding(db):
    row = db.add(status="queued")
    await _server_bind(branch="conductor/ticket/t-1")
    _pending(row)
    revision = row["revision"]

    assert await _cancel()

    assert row["worktree_cleanup_pending"] is False
    assert row["worktree_cleanup_attempts"] == 0
    assert row["worktree_cleanup_next_retry_at"] is None
    assert (row["worktree_path"], row["worktree_generation"], row["worktree_host"]) == (
        "/srv/wt/t-1",
        1,
        "server",
    )
    assert row["worktree_branch"] == "conductor/ticket/t-1"
    assert row["revision"] == revision
    assert not await _cancel()  # nothing pending any more


def _server_pending_row(db: _FakeTickets) -> dict[str, Any]:
    row = db.add(
        status="queued",
        worktree_path="/srv/wt/t-1",
        worktree_host="server",
        worktree_generation=1,
    )
    _pending(row)
    return row


def _daemon_pending_row(db: _FakeTickets) -> dict[str, Any]:
    row = db.add(
        status="blocked",
        worker_daemon_id=DAEMON,
        worktree_path="/home/me/wt/t-1",
        worktree_host="daemon",
        worktree_daemon_id=DAEMON,
        worktree_generation=1,
    )
    _pending(row)
    return row


@pytest.mark.parametrize(
    ("row_overrides", "call"),
    [
        ({}, {"worktree_path": "/srv/wt/other"}),
        ({}, {"worktree_generation": 2}),
        ({"worktree_host": "daemon"}, {}),
        ({"status": "completed"}, {}),
        ({"status": "failed"}, {}),
        ({"closed_at": T0}, {}),
        ({"worker_daemon_id": DAEMON}, {}),  # orphaned: moved to a daemon
    ],
    ids=["path", "generation", "host", "completed", "failed", "closed_at", "orphaned"],
)
@pytest.mark.asyncio
async def test_cancel_pending_server_fences(db, row_overrides, call):
    row = _server_pending_row(db)
    row.update(row_overrides)
    before = dict(row)

    assert not await _cancel(**call)
    assert row == before


@pytest.mark.parametrize(
    ("row_overrides", "call"),
    [
        ({}, {"daemon_id": OTHER_DAEMON}),
        # The checkout's daemon no longer holds the Ticket, even when it asks.
        ({"worktree_daemon_id": OTHER_DAEMON}, {"daemon_id": OTHER_DAEMON}),
        ({"worker_daemon_id": OTHER_DAEMON}, {}),  # reassigned to another daemon
        ({"worker_daemon_id": None}, {}),  # reassigned back to the server
        ({"worktree_host": "server"}, {}),
    ],
    ids=["daemon_id", "orphaned_checkout_daemon", "reassigned", "back_to_server", "host"],
)
@pytest.mark.asyncio
async def test_cancel_pending_daemon_fences(db, row_overrides, call):
    row = _daemon_pending_row(db)
    row.update(row_overrides)
    call = dict(call)
    call.setdefault("daemon_id", DAEMON)
    before = dict(row)

    assert not await _cancel(
        worktree_path="/home/me/wt/t-1", host=tickets.WORKTREE_HOST_DAEMON, **call
    )
    assert row == before


@pytest.mark.asyncio
async def test_cancel_pending_daemon_success(db):
    row = _daemon_pending_row(db)

    assert await _cancel(
        worktree_path="/home/me/wt/t-1", host=tickets.WORKTREE_HOST_DAEMON, daemon_id=DAEMON
    )
    assert row["worktree_cleanup_pending"] is False
    assert row["worktree_daemon_id"] == DAEMON


@pytest.mark.asyncio
async def test_cancel_pending_validates_host_arguments(db):
    with pytest.raises(ValueError):
        await _cancel(host="laptop")
    with pytest.raises(ValueError):
        await _cancel(host=tickets.WORKTREE_HOST_DAEMON)
    with pytest.raises(ValueError):
        await _cancel(daemon_id=DAEMON)


# ---------------------------------------------------------------- host fences


@pytest.mark.asyncio
async def test_clear_and_record_fence_on_host_and_daemon(db):
    row = _daemon_ticket(db)
    await _daemon_bind()
    before = dict(row)
    attempt = {"worktree_path": "/home/me/wt/t-1", "worktree_generation": 1}

    for fence in ({"host": "server"}, {"host": "daemon", "daemon_id": OTHER_DAEMON},
                  {"daemon_id": OTHER_DAEMON}):
        assert not await tickets.record_worktree_cleanup_pending("t-1", **attempt, **fence)
        assert not await tickets.clear_ticket_worktree_binding("t-1", **attempt, **fence)
    assert row == before

    assert await tickets.record_worktree_cleanup_pending(
        "t-1", **attempt, host="daemon", daemon_id=DAEMON
    )
    assert row["worktree_cleanup_pending"] is True
    assert await tickets.clear_ticket_worktree_binding(
        "t-1", **attempt, host="daemon", daemon_id=DAEMON
    )
    assert row["worktree_path"] == ""


@pytest.mark.asyncio
async def test_server_host_fence_on_clear_and_record(db):
    row = db.add()
    await _server_bind()
    attempt = {"worktree_path": "/srv/wt/t-1", "worktree_generation": 1}

    assert not await tickets.clear_ticket_worktree_binding("t-1", **attempt, host="daemon")
    assert row["worktree_path"] == "/srv/wt/t-1"
    assert await tickets.clear_ticket_worktree_binding("t-1", **attempt, host="server")
    with pytest.raises(ValueError):
        await tickets.record_worktree_cleanup_pending("t-1", **attempt, host="laptop")


# ---------------------------------------------------------------- attempt cap


@pytest.mark.asyncio
async def test_claim_skips_rows_at_the_attempt_cap(db):
    cap = tickets.MAX_WORKTREE_CLEANUP_ATTEMPTS
    assert cap == 20
    for tid, attempts in (("t-under", cap - 1), ("t-at", cap), ("t-over", cap + 3)):
        db.add(tid, worktree_path=f"/srv/wt/{tid}", worktree_host="server",
               worktree_generation=1, worktree_cleanup_pending=True,
               worktree_cleanup_attempts=attempts)

    claimed = await tickets.claim_worktree_cleanup_pending(host="server")

    assert [c["ticket_id"] for c in claimed] == ["t-under"]
    assert db.rows["t-at"]["worktree_cleanup_pending"] is True  # left visible


# ---------------------------------------------------------------- wire models


def _bind_model(**kw: Any):
    body = {
        "lease_id": LEASE,
        "worktree_path": "/home/me/wt/t-1",
        "branch": "conductor/ticket/t-1",
        "base_sha": SHA,
    }
    body.update(kw)
    return models.DaemonWorktreeBind(**body)


def test_bind_model_drops_base_branch():
    assert "base_branch" not in models.DaemonWorktreeBind.model_fields
    assert not hasattr(_bind_model(base_branch="origin/dev"), "base_branch")


@pytest.mark.parametrize("sha", ["a" * 40, "0123456789abcdef" * 4])
def test_bind_model_accepts_full_object_ids(sha):
    assert _bind_model(base_sha=sha).base_sha == sha


@pytest.mark.parametrize(
    "sha",
    ["a" * 7, "a" * 39, "a" * 41, "a" * 63, "a" * 65, "A" * 40, "g" * 40, " " + "a" * 40],
    ids=["short", "39", "41", "63", "65", "upper", "nonhex", "space"],
)
def test_bind_model_rejects_partial_or_odd_object_ids(sha):
    with pytest.raises(ValueError):
        _bind_model(base_sha=sha)


@pytest.mark.parametrize(
    "branch",
    [
        "conductor/ticket/t-1",
        "conductor/ticket/abc_123.x",
        "conductor/ticket/" + "x" * 183,
    ],
)
def test_bind_model_accepts_ticket_branches(branch):
    assert _bind_model(branch=branch).branch == branch


@pytest.mark.parametrize(
    "branch",
    [
        "ticket/t-1",
        "main",
        "-conductor/ticket/t-1",
        "conductor/ticket/",
        "conductor/ticket/a b",
        "conductor/ticket/a\tb",
        "conductor/ticket/a\x01b",
        "conductor/ticket/a..b",
        "conductor/ticket/a@{1}",
        "conductor/ticket/t-1/",
        "conductor/ticket/t-1.",
        "conductor/ticket/t-1.lock",
        "conductor/ticket/a//b",
        "conductor/ticket/.hidden",
        "conductor/ticket/a~1",
        "conductor/ticket/" + "x" * 184,
    ],
    ids=[
        "no_prefix", "main", "leading_dash", "prefix_only", "space", "tab", "control",
        "dotdot", "reflog", "trailing_slash", "trailing_dot", "lock", "double_slash",
        "dot_component", "tilde", "too_long",
    ],
)
def test_bind_model_rejects_unsafe_branches(branch):
    with pytest.raises(ValueError):
        _bind_model(branch=branch)


@pytest.mark.parametrize(
    "path",
    ["relative/wt", "~/wt", "", "/home/me/../etc", "/home/me/wt\x00x", "/a/" + "x" * 500],
    ids=["relative", "tilde", "empty", "dotdot", "nul", "too_long"],
)
def test_worktree_models_require_an_absolute_path(path):
    with pytest.raises(ValueError):
        _bind_model(worktree_path=path)
    with pytest.raises(ValueError):
        models.DaemonWorktreeCleanup(worktree_path=path, worktree_generation=1, outcome="removed")


def test_cleanup_model_limits():
    ok = models.DaemonWorktreeCleanup(
        worktree_path="/home/me/wt/t-1", worktree_generation=1, outcome="removed",
        detail="x" * 4000,
    )
    assert ok.lease_id is None
    with pytest.raises(ValueError):
        models.DaemonWorktreeCleanup(
            worktree_path="/home/me/wt/t-1", worktree_generation=1, outcome="removed",
            detail="x" * 4001,
        )


# ---------------------------------------------------------------- departed daemon


DAEMON_PATH = "/home/me/wt/t-1"
THIRD_DAEMON = str(uuid4())
THIRD_LEASE = str(uuid4())


def _departed(db: _FakeTickets, *, pending: bool, **overrides: Any) -> dict[str, Any]:
    """DAEMON bound generation 1 under LEASE, then lost the Ticket (handover).

    The handover left the binding (pending for the old daemon or not); the
    Ticket is now the server's (``worker_daemon_id`` NULL) unless overridden.
    """
    fields: dict[str, Any] = dict(
        worktree_path=DAEMON_PATH,
        worktree_generation=1,
        worktree_branch="ticket/t-1",
        worktree_host="daemon",
        worktree_daemon_id=DAEMON,
        worktree_lease_id=LEASE,
        worktree_cleanup_pending=pending,
        worktree_cleanup_attempts=1 if pending else 0,
        worktree_cleanup_next_retry_at=T0 if pending else None,
    )
    fields.update(overrides)
    return db.add(**fields)


def _note(daemon_id: str = DAEMON, path: str = DAEMON_PATH, generation: int = 1,
          lease_id: str | None = LEASE) -> dict[str, Any]:
    return {
        "daemon_id": daemon_id,
        "worktree_path": path,
        "worktree_generation": generation,
        "lease_id": lease_id,
    }


@pytest.mark.parametrize("pending", [True, False], ids=["pending", "not-pending"])
@pytest.mark.asyncio
async def test_server_bind_replaces_a_departed_daemons_binding(db, pending):
    row = _departed(db, pending=pending)

    # Without the observed generation the old fences still refuse it.
    assert await _server_bind() is None
    assert row["worktree_daemon_id"] == DAEMON and row["worktree_stale_cleanups"] == []

    bound = await _server_bind(replace_generation=1)

    assert (bound["worktree_path"], bound["worktree_host"]) == ("/srv/wt/t-1", "server")
    assert bound["worktree_daemon_id"] is None and bound["worktree_lease_id"] is None
    assert bound["worktree_generation"] == 2
    assert bound["worktree_cleanup_pending"] is False
    assert bound["worktree_cleanup_attempts"] == 0
    assert bound["worktree_cleanup_next_retry_at"] is None
    assert bound["worktree_stale_cleanups"] == [_note()]
    assert row["revision"] == 5


@pytest.mark.parametrize("pending", [True, False], ids=["pending", "not-pending"])
@pytest.mark.asyncio
async def test_new_daemon_bind_replaces_a_departed_daemons_binding(db, pending):
    row = _departed(
        db,
        pending=pending,
        worker_daemon_id=OTHER_DAEMON,
        worker_lease_id=OTHER_LEASE,
        worker_lease_expires_at=T0 + timedelta(seconds=60),
    )

    # Same path string, other daemon: still refused without the generation.
    assert await _daemon_bind(daemon_id=OTHER_DAEMON, lease_id=OTHER_LEASE) is None
    assert await _daemon_bind(
        "/home/b/wt/t-1", daemon_id=OTHER_DAEMON, lease_id=OTHER_LEASE
    ) is None
    # A generation the row no longer holds fences out.
    assert await _daemon_bind(
        "/home/b/wt/t-1", daemon_id=OTHER_DAEMON, lease_id=OTHER_LEASE, replace_generation=2
    ) is None
    assert row["worktree_generation"] == 1 and row["worktree_daemon_id"] == DAEMON

    bound = await _daemon_bind(
        "/home/b/wt/t-1", daemon_id=OTHER_DAEMON, lease_id=OTHER_LEASE, replace_generation=1
    )

    assert bound["worktree_path"] == "/home/b/wt/t-1"
    assert (bound["worktree_daemon_id"], bound["worktree_lease_id"]) == (OTHER_DAEMON, OTHER_LEASE)
    assert bound["worktree_generation"] == 2
    assert bound["worktree_cleanup_pending"] is False
    assert bound["worktree_stale_cleanups"] == [_note()]


@pytest.mark.asyncio
async def test_replacement_advances_the_generation_and_fences_a_stale_read(db):
    # The fake advances worktree_generation on every takeover exactly as the
    # SQL says; a second new owner holding the pre-takeover read fences out.
    row = _departed(
        db,
        pending=True,
        worker_daemon_id=OTHER_DAEMON,
        worker_lease_id=OTHER_LEASE,
        worker_lease_expires_at=T0 + timedelta(seconds=60),
    )
    first = await _daemon_bind(
        "/home/b/wt/t-1", daemon_id=OTHER_DAEMON, lease_id=OTHER_LEASE, replace_generation=1
    )
    assert first["worktree_generation"] == 2

    # OTHER_DAEMON loses the Ticket too; THIRD_DAEMON read the row back when it
    # still held DAEMON's generation-1 binding.
    row.update(
        worker_daemon_id=THIRD_DAEMON,
        worker_lease_id=THIRD_LEASE,
        worker_lease_expires_at=T0 + timedelta(seconds=60),
    )
    stale = await _daemon_bind(
        "/home/c/wt/t-1", daemon_id=THIRD_DAEMON, lease_id=THIRD_LEASE, replace_generation=1
    )
    assert stale is None
    assert (row["worktree_path"], row["worktree_generation"]) == ("/home/b/wt/t-1", 2)
    assert row["worktree_stale_cleanups"] == [_note()]

    # The old attempt's cleanup cannot touch the new binding either.
    assert not await tickets.clear_ticket_worktree_binding(
        "t-1", worktree_path=DAEMON_PATH, worktree_generation=1
    )
    assert not await tickets.record_worktree_cleanup_pending(
        "t-1", worktree_path="/home/b/wt/t-1", worktree_generation=1
    )

    fresh = await _daemon_bind(
        "/home/c/wt/t-1", daemon_id=THIRD_DAEMON, lease_id=THIRD_LEASE, replace_generation=2
    )
    assert fresh["worktree_generation"] == 3
    assert fresh["worktree_stale_cleanups"] == [
        _note(),
        _note(OTHER_DAEMON, "/home/b/wt/t-1", 2, OTHER_LEASE),
    ]


@pytest.mark.asyncio
async def test_replace_never_takes_a_binding_whose_daemon_still_holds_the_ticket(db):
    # DAEMON is still the Ticket's worker: its checkout is live, not departed.
    row = _daemon_ticket(
        db,
        worktree_path=DAEMON_PATH,
        worktree_generation=1,
        worktree_branch="ticket/t-1",
        worktree_host="daemon",
        worktree_daemon_id=DAEMON,
        worktree_lease_id=LEASE,
    )

    assert await _daemon_bind("/home/me/wt/other", replace_generation=1) is None
    assert row["worktree_path"] == DAEMON_PATH and row["worktree_stale_cleanups"] == []


@pytest.mark.asyncio
async def test_replace_never_takes_a_server_checkout(db):
    row = db.add(
        worktree_path="/srv/wt/t-1",
        worktree_generation=1,
        worktree_branch="ticket/t-1",
        worktree_host="server",
        worker_daemon_id=OTHER_DAEMON,
        worker_lease_id=OTHER_LEASE,
        worker_lease_expires_at=T0 + timedelta(seconds=60),
    )

    assert await _daemon_bind(
        daemon_id=OTHER_DAEMON, lease_id=OTHER_LEASE, replace_generation=1
    ) is None
    row.update(worker_daemon_id=None, worker_lease_id=None, worker_lease_expires_at=None)
    assert await _server_bind("/srv/wt/other", replace_generation=1) is None
    assert row["worktree_path"] == "/srv/wt/t-1" and row["worktree_generation"] == 1


@pytest.mark.parametrize(
    "overrides, kw",
    [
        ({"status": "completed"}, {}),
        ({"closed_at": T0}, {}),
        ({"worktree_base_sha": OTHER_SHA}, {}),
        ({}, {"expected_revision": 3}),
        ({"worker_daemon_id": OTHER_DAEMON}, {}),
    ],
    ids=["completed", "closed", "base", "revision", "now-on-a-daemon"],
)
@pytest.mark.asyncio
async def test_replace_keeps_every_other_bind_fence(db, overrides, kw):
    row = _departed(db, pending=True, **overrides)

    assert await _server_bind(replace_generation=1, **kw) is None
    assert row["worktree_daemon_id"] == DAEMON and row["worktree_generation"] == 1
    assert row["worktree_stale_cleanups"] == []


@pytest.mark.asyncio
async def test_replace_records_a_null_lease_and_validates_the_generation(db):
    _departed(db, pending=True, worktree_lease_id=None)

    with pytest.raises(ValueError):
        await _server_bind(replace_generation=0)
    bound = await _server_bind(replace_generation=1)

    assert bound["worktree_stale_cleanups"] == [_note(lease_id=None)]


# ---------------------------------------------------------------- stale cleanup notes


async def _resolve(outcome: str, **kw: Any) -> bool:
    kw.setdefault("daemon_id", DAEMON)
    kw.setdefault("worktree_path", DAEMON_PATH)
    kw.setdefault("worktree_generation", 1)
    return await tickets.resolve_stale_worktree_cleanup("t-1", outcome=outcome, **kw)


@pytest.mark.asyncio
async def test_resolve_stale_cleanup_keeps_a_failed_note_and_drops_a_removed_one(db):
    row = _departed(db, pending=True)
    await _server_bind(replace_generation=1)
    binding = {k: row[k] for k in WORKTREE_COLUMNS if k != "worktree_stale_cleanups"}

    assert await _resolve("failed") is True
    assert row["worktree_stale_cleanups"] == [_note()]

    assert await _resolve("removed") is True
    assert row["worktree_stale_cleanups"] == []
    assert await _resolve("removed") is False  # already settled
    assert await _resolve("failed") is False

    # The live (server) binding was never touched.
    assert {k: row[k] for k in binding} == binding


@pytest.mark.parametrize(
    "kw",
    [
        {"daemon_id": OTHER_DAEMON},
        {"worktree_path": "/home/me/wt/other"},
        {"worktree_generation": 2},
        {"worktree_path": ""},
    ],
    ids=["daemon", "path", "generation", "no-path"],
)
@pytest.mark.asyncio
async def test_resolve_stale_cleanup_needs_an_exact_note(db, kw):
    row = _departed(db, pending=True)
    await _server_bind(replace_generation=1)

    assert await _resolve("removed", **kw) is False
    assert row["worktree_stale_cleanups"] == [_note()]


@pytest.mark.asyncio
async def test_resolve_stale_cleanup_drops_only_the_matching_note(db):
    other = _note(OTHER_DAEMON, "/home/b/wt/t-1", 2, OTHER_LEASE)
    row = db.add(worktree_stale_cleanups=[_note(), other])

    assert await _resolve("removed") is True
    assert row["worktree_stale_cleanups"] == [other]


@pytest.mark.asyncio
async def test_resolve_stale_cleanup_validates_arguments(db):
    db.add(worktree_stale_cleanups=[_note()])
    with pytest.raises(ValueError):
        await _resolve("maybe")
    with pytest.raises(ValueError):
        await _resolve("removed", daemon_id="")
    assert await tickets.resolve_stale_worktree_cleanup(
        "missing", daemon_id=DAEMON, worktree_path=DAEMON_PATH,
        worktree_generation=1, outcome="removed",
    ) is False


def test_ticket_rows_carry_stale_cleanups_as_a_list():
    out = tickets.ticket_from_row({"ticket_id": "t-1", "worktree_stale_cleanups": None})
    assert out["worktree_stale_cleanups"] == []


# ---------------------------------------------------------------- base sha length


@pytest.mark.asyncio
async def test_pin_rejects_a_base_sha_longer_than_64(db):
    row = db.add(worktree_base_sha="")

    with pytest.raises(ValueError, match="too long"):
        await tickets.pin_ticket_worktree_base("t-1", base_sha="a" * 65, base_branch="main")
    assert row["worktree_base_sha"] == ""  # never truncated into a different pin

    pinned = await tickets.pin_ticket_worktree_base("t-1", base_sha="c" * 64, base_branch="main")
    assert pinned["worktree_base_sha"] == "c" * 64


@pytest.mark.asyncio
async def test_bind_rejects_a_base_sha_longer_than_64(db):
    db.add(worktree_base_sha="a" * 64)

    with pytest.raises(ValueError, match="too long"):
        await _server_bind(base_sha="a" * 65)
