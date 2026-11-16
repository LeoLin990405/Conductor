"""Reuse of an accepted speculative plan is fenced and does not resume it.

A later signal is stored as passive context only while the locked Ticket is
still an open accepted plan. That row is already delivered, and daemon resume
ignores passive context on that plan, so the observe Worker stays parked for
review.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.server.database.autonomy import daemons, evals, tickets


def _plan(**overrides):
    ticket = {
        "ticket_id": "ticket-1",
        "user_id": "u1",
        "status": "waiting",
        "worker_daemon_id": "d1",
        "worker_provider": "codex",
        "attention": {
            "question": "Speculative plan ready. Review it, then Retry to execute the work."
        },
        "attempts": 1,
        "revision": 3,
        "mandate": {
            "speculative": True,
            "speculative_outcome": {
                "outcome": "accepted",
                "evidence": {"worker_summary": "PLAN: check the save button"},
            },
        },
    }
    ticket.update(overrides)
    return ticket


def _evaluation(**overrides):
    row = {
        "eval_id": "eval-1",
        "thread_key": "thread-1",
        "trigger_ids": ["tr-1"],
        "status": "queued",
        "decision": None,
        "reason": "",
    }
    row.update(overrides)
    return row


class _World:
    def __init__(self, ticket, evaluation):
        self.tickets = {ticket["ticket_id"]: ticket}
        self.evaluation = evaluation
        self.inputs: list[dict] = []
        self.sql: list[str] = []
        self.transactions = 0
        self.daemon = {
            "daemon_id": "d1",
            "user_id": "u1",
            "provider": "codex",
            "max_concurrency": 2,
        }

    def connect(self, *args, **kwargs):
        return _Conn(self)


class _Tx:
    def __init__(self, world: _World):
        self.world = world

    async def __aenter__(self):
        self.world.transactions += 1
        return self

    async def __aexit__(self, *exc):
        return False


class _Conn:
    def __init__(self, world: _World):
        self.world = world

    def cursor(self, row_factory=None):
        return _Cursor(self.world)

    def transaction(self):
        return _Tx(self.world)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _json_text(mandate: dict | None, *keys: str) -> str | None:
    """Postgres `->>` text. A missing key and JSON null are both SQL NULL."""
    current: object = mandate
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    if current is None:
        return None
    if isinstance(current, bool):
        return "true" if current else "false"
    return str(current)


def _sql_eq(left: str | None, right: str) -> bool | None:
    """SQL `=`. Unknown when either side is NULL. Never coerce NULL to false."""
    if left is None:
        return None
    return left == right


def _sql_and(parts: list[bool | None]) -> bool | None:
    if any(part is False for part in parts):
        return False
    if any(part is None for part in parts):
        return None
    return True


def _sql_not(value: bool | None) -> bool | None:
    if value is None:
        return None
    return not value


def _text_equals(sql: str, expr: str, expected: str, actual: str | None) -> bool | None:
    """One JSON comparison, including a NULL-safe form when the SQL uses one."""
    if f"COALESCE({expr}, '') = '{expected}'" in sql:
        return ("" if actual is None else actual) == expected
    if f"{expr} IS NOT DISTINCT FROM '{expected}'" in sql:
        return actual == expected
    return _sql_eq(actual, expected)


def _not_exclusion(
    sql: str,
    *,
    kind: str | None,
    status: str | None,
    mandate: dict | None,
) -> bool | None:
    """Three-valued NOT (passive-context AND).

    WHERE keeps a row only when this is True. NULL rejects it, which is how a
    missing speculative field used to hide a manual Ticket's context update.
    """
    inner = _sql_and(
        [
            _sql_eq(kind, "context"),
            _sql_eq(status, "waiting"),
            _text_equals(
                sql,
                "t.mandate->>'speculative'",
                "true",
                _json_text(mandate, "speculative"),
            ),
            _text_equals(
                sql,
                "t.mandate->'speculative_outcome'->>'outcome'",
                "accepted",
                _json_text(mandate, "speculative_outcome", "outcome"),
            ),
        ]
    )
    return _sql_not(inner)


def _wakes(sql: str, ticket: dict, item: dict) -> bool:
    """Apply the resume predicate the SQL text actually contains."""
    if item.get("ticket_id") != ticket.get("ticket_id"):
        return False
    if "delivered_at IS NULL" in sql and item.get("delivered_at") is not None:
        return False
    if "i.kind = 'context'" in sql and "speculative_outcome" in sql:
        mandate = ticket.get("mandate")
        included = _not_exclusion(
            sql,
            kind=item.get("kind"),
            status=ticket.get("status"),
            mandate=mandate if isinstance(mandate, dict) else None,
        )
        return included is True
    return True


class _Cursor:
    def __init__(self, world: _World):
        self.world = world
        self.row = None
        self.rows: list[dict] = []

    async def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.world.sql.append(normalized)
        self.row = None
        self.rows = []
        self.rowcount = 0
        self._dispatch(normalized, tuple(params or ()))

    async def fetchone(self):
        return self.row

    async def fetchall(self):
        return list(self.rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _dispatch(self, sql: str, params: tuple) -> None:
        if "FROM autonomy_daemons" in sql and "FOR UPDATE" in sql:
            self.row = dict(self.world.daemon) if params[0] == self.world.daemon["daemon_id"] else None
            return
        if "input_snapshot" in sql and "worker_lease_claim_id = %s" in sql:
            self.row = None
            return
        if "COUNT(*) AS running_count" in sql:
            self.row = {"running_count": 0}
            return
        if "FROM autonomy_daemon_claims WHERE claim_id" in sql:
            self.row = None
            return
        if "SELECT ticket_id, user_id, status, mandate" in sql and "FOR UPDATE" in sql:
            ticket = self.world.tickets.get(params[0])
            if ticket is None:
                self.row = None
                return
            self.row = {
                "ticket_id": ticket["ticket_id"],
                "user_id": ticket["user_id"],
                "status": ticket["status"],
                "mandate": dict(ticket["mandate"]),
            }
            return
        if "UPDATE autonomy_evals" in sql and "status = 'decided'" in sql:
            decision, reason, eval_id = params
            evaluation = self.world.evaluation
            if evaluation["eval_id"] == eval_id and evaluation["status"] in {"queued", "delivered"}:
                evaluation["status"] = "decided"
                evaluation["decision"] = decision
                evaluation["reason"] = reason
                self.row = {
                    "eval_id": evaluation["eval_id"],
                    "thread_key": evaluation["thread_key"],
                    "trigger_ids": list(evaluation["trigger_ids"]),
                    "status": "decided",
                    "decision": decision,
                    "reason": reason,
                }
            return
        if "UPDATE autonomy_threads" in sql and "evaluating_eval_id = NULL" in sql:
            return
        if "INSERT INTO autonomy_ticket_inputs" in sql:
            ticket_id, trigger_id, text = params
            delivered = "stamped" if "delivered_at" in sql and "NOW()" in sql else None
            kind = "context" if "'context'" in sql else "unspecified"
            existing = next(
                (
                    item
                    for item in self.world.inputs
                    if item["ticket_id"] == ticket_id and item["trigger_id"] == trigger_id
                ),
                None,
            )
            if existing is not None:
                if "DO UPDATE" in sql and existing.get("delivered_at") is None and delivered:
                    existing["delivered_at"] = delivered
                self.row = dict(existing)
                return
            item = {
                "input_id": f"input-{len(self.world.inputs) + 1}",
                "ticket_id": ticket_id,
                "trigger_id": trigger_id,
                "kind": kind,
                "text": text,
                "delivered_at": delivered,
                "created_at": None,
            }
            self.world.inputs.append(item)
            self.row = dict(item)
            return
        if sql.startswith("UPDATE autonomy_tickets") and "attention = NULL" in sql:
            self._claim(sql, params)
            return
        if (
            sql.startswith("UPDATE autonomy_tickets")
            and "SET revision = revision + 1" in sql
            and "attention" not in sql
        ):
            ticket = self.world.tickets.get(params[0])
            if ticket is not None:
                ticket["revision"] = int(ticket.get("revision") or 0) + 1
            return
        if "FOR UPDATE OF t SKIP LOCKED" in sql:
            self.row = self._candidate(sql, params)
            return
        if sql.startswith("INSERT INTO autonomy_daemon_claims"):
            return
        if "UPDATE autonomy_ticket_inputs" in sql and "delivered_at = NOW()" in sql:
            ticket_id = params[0]
            delivered = []
            for item in self.world.inputs:
                if item["ticket_id"] == ticket_id and item.get("delivered_at") is None:
                    item["delivered_at"] = "stamped"
                    delivered.append(dict(item))
            self.rows = delivered
            return
        if "SET input_snapshot" in sql:
            return
        raise AssertionError(f"unexpected SQL: {sql}")

    def _candidate(self, sql: str, params: tuple):
        daemon_id, user_id, provider = params
        for ticket in self.world.tickets.values():
            if not self._eligible(sql, ticket, daemon_id, user_id, provider):
                continue
            return {"ticket_id": ticket["ticket_id"]}
        return None

    def _eligible(self, sql, ticket, daemon_id, user_id, provider) -> bool:
        if ticket.get("worker_daemon_id") != daemon_id or ticket.get("user_id") != user_id:
            return False
        if ticket.get("worker_provider") not in ("", provider):
            return False
        if ticket.get("status") not in {"blocked", "waiting"}:
            return False
        if "orchestration_only" in sql and (ticket.get("mandate") or {}).get("orchestration_only") is True:
            return False
        return any(_wakes(sql, ticket, item) for item in self.world.inputs)

    def _claim(self, sql: str, params: tuple) -> None:
        _provider, claim_id, _same_claim, _lease_seconds, ticket_id, daemon_id = params
        ticket = self.world.tickets.get(ticket_id)
        if ticket is None or ticket.get("worker_daemon_id") != daemon_id:
            return
        if not self._eligible(sql, ticket, daemon_id, ticket["user_id"], _provider):
            return
        ticket["status"] = "running"
        ticket["attention"] = None
        ticket["attempts"] = int(ticket.get("attempts") or 0) + 1
        ticket["worker_lease_id"] = claim_id
        ticket["worker_lease_expires_at"] = datetime.now(timezone.utc)
        self.row = dict(ticket)


def _bind(monkeypatch, world: _World) -> None:
    monkeypatch.setattr(evals, "get_db_connection", world.connect)
    monkeypatch.setattr(daemons, "get_db_connection", world.connect)


@pytest.mark.asyncio
async def test_reuse_stores_delivered_context_and_does_not_resume(monkeypatch):
    ticket = _plan()
    world = _World(ticket, _evaluation())
    _bind(monkeypatch, world)

    reused = await evals.reuse_open_speculative_plan(
        eval_id="eval-1",
        ticket_id="ticket-1",
        user_id="u1",
        trigger_id="tr-9",
        text="a later signal",
        reason="reused accepted speculative outcome on ticket ticket-1",
    )
    resumed = await daemons.resume_ticket("d1", 4)

    assert reused is not None
    assert reused["ticket_id"] == "ticket-1"
    assert reused["delivered_at"] == "stamped"
    assert reused["mandate"]["speculative_outcome"]["outcome"] == "accepted"
    assert world.evaluation["decision"] == "attach"
    assert world.evaluation["status"] == "decided"
    assert world.inputs == [
        {
            "input_id": "input-1",
            "ticket_id": "ticket-1",
            "trigger_id": "tr-9",
            "kind": "context",
            "text": "a later signal",
            "delivered_at": "stamped",
            "created_at": None,
        }
    ]
    assert world.transactions == 2
    assert resumed is None
    assert ticket["status"] == "waiting"
    assert ticket["attention"]["question"].startswith("Speculative plan ready")
    revision = next(sql for sql in world.sql if "SET revision = revision + 1" in sql)
    assert "attention" not in revision
    assert "status" not in revision
    insert = next(sql for sql in world.sql if "INSERT INTO autonomy_ticket_inputs" in sql)
    assert "delivered_at" in insert and "NOW()" in insert and "'context'" in insert
    assert any("FOR UPDATE" in sql and "SKIP LOCKED" not in sql for sql in world.sql)


@pytest.mark.asyncio
async def test_discarded_plan_does_not_receive_the_signal_or_settle_the_eval(monkeypatch):
    ticket = _plan(
        status="completed",
        attention=None,
        mandate={
            "speculative": True,
            "speculative_outcome": {"outcome": "discarded", "evidence": {"actor": "user"}},
        },
    )
    world = _World(ticket, _evaluation())
    _bind(monkeypatch, world)

    reused = await evals.reuse_open_speculative_plan(
        eval_id="eval-1",
        ticket_id="ticket-1",
        user_id="u1",
        trigger_id="tr-9",
        text="a later signal",
        reason="reused accepted speculative outcome on ticket ticket-1",
    )

    assert reused is None
    assert world.inputs == []
    assert world.evaluation["status"] == "queued"
    assert world.evaluation["decision"] is None
    assert not any("INSERT INTO autonomy_ticket_inputs" in sql for sql in world.sql)
    assert not any("UPDATE autonomy_evals" in sql for sql in world.sql)
    assert ticket["status"] == "completed"


@pytest.mark.asyncio
async def test_undelivered_passive_context_does_not_resume_or_clear_attention(monkeypatch):
    ticket = _plan()
    world = _World(ticket, _evaluation())
    world.inputs.append(
        {
            "input_id": "input-1",
            "ticket_id": "ticket-1",
            "trigger_id": "tr-9",
            "kind": "context",
            "text": "a later signal",
            "delivered_at": None,
            "created_at": None,
        }
    )
    _bind(monkeypatch, world)

    assert await daemons.resume_ticket("d1", 4) is None
    assert ticket["status"] == "waiting"
    assert ticket["attention"] is not None
    candidate = next(sql for sql in world.sql if "FOR UPDATE OF t SKIP LOCKED" in sql)
    assert "delivered_at IS NULL" in candidate
    assert "i.kind = 'context'" in candidate
    assert "speculative_outcome" in candidate
    assert not any("attention = NULL" in sql for sql in world.sql)


@pytest.mark.asyncio
async def test_an_undelivered_answer_still_resumes_and_clears_attention(monkeypatch):
    ticket = _plan()
    world = _World(ticket, _evaluation())
    world.inputs.append(
        {
            "input_id": "input-1",
            "ticket_id": "ticket-1",
            "trigger_id": None,
            "kind": "answer",
            "text": "ship it",
            "delivered_at": None,
            "created_at": None,
        }
    )
    _bind(monkeypatch, world)

    resumed = await daemons.resume_ticket("d1", 4)

    assert resumed is not None
    assert resumed["ticket_id"] == "ticket-1"
    assert ticket["status"] == "running"
    assert ticket["attention"] is None
    assert resumed["inputs"][0]["kind"] == "answer"


@pytest.mark.asyncio
async def test_waiting_discarded_outcome_does_not_consume_the_signal(monkeypatch):
    ticket = _plan(
        mandate={
            "speculative": True,
            "speculative_outcome": {"outcome": "discarded"},
        }
    )
    world = _World(ticket, _evaluation())
    _bind(monkeypatch, world)

    reused = await evals.reuse_open_speculative_plan(
        eval_id="eval-1",
        ticket_id="ticket-1",
        user_id="u1",
        trigger_id="tr-9",
        text="a later signal",
        reason="reused accepted speculative outcome on ticket ticket-1",
    )

    assert reused is None
    assert world.inputs == []
    assert world.evaluation["status"] == "queued"
    assert ticket["status"] == "waiting"
    assert ticket["attention"] is not None


@pytest.mark.asyncio
async def test_waiting_non_speculative_context_still_resumes(monkeypatch):
    ticket = _plan(mandate={"speculative": False}, attention=None)
    world = _World(ticket, _evaluation())
    world.inputs.append(
        {
            "input_id": "input-1",
            "ticket_id": "ticket-1",
            "trigger_id": "tr-9",
            "kind": "context",
            "text": "follow-up",
            "delivered_at": None,
            "created_at": None,
        }
    )
    _bind(monkeypatch, world)

    resumed = await daemons.resume_ticket("d1", 4)

    assert resumed is not None
    assert ticket["status"] == "running"


@pytest.mark.asyncio
async def test_blocked_context_still_resumes(monkeypatch):
    ticket = _plan(status="blocked", attention={"question": "need a choice"})
    world = _World(ticket, _evaluation())
    world.inputs.append(
        {
            "input_id": "input-1",
            "ticket_id": "ticket-1",
            "trigger_id": "tr-9",
            "kind": "context",
            "text": "follow-up",
            "delivered_at": None,
            "created_at": None,
        }
    )
    _bind(monkeypatch, world)

    resumed = await daemons.resume_ticket("d1", 4)

    assert resumed is not None
    assert ticket["status"] == "running"
    assert ticket["attention"] is None


def test_missing_speculative_fields_are_null_until_the_comparison_is_coerced():
    """A raw `=` on an absent mandate field is SQL NULL, and WHERE drops it."""
    raw = " ".join(
        """
        NOT (
            i.kind = 'context'
            AND t.status = 'waiting'
            AND t.mandate->>'speculative' = 'true'
            AND t.mandate->'speculative_outcome'->>'outcome' = 'accepted'
        )
        """.split()
    )
    assert _not_exclusion(raw, kind="context", status="waiting", mandate={}) is None
    assert _not_exclusion(raw, kind="context", status="waiting", mandate=None) is None
    production = " ".join(daemons._PASSIVE_CONTEXT_EXCLUSION.split())
    assert _not_exclusion(production, kind="context", status="waiting", mandate={}) is True
    assert _not_exclusion(production, kind="context", status="waiting", mandate=None) is True
    accepted = {
        "speculative": True,
        "speculative_outcome": {"outcome": "accepted"},
    }
    assert (
        _not_exclusion(production, kind="context", status="waiting", mandate=accepted)
        is False
    )
    assert (
        _not_exclusion(production, kind="answer", status="waiting", mandate=accepted)
        is True
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mandate", [{}, None, {"source_id": "manual"}])
async def test_waiting_ticket_without_speculative_fields_resumes_on_context(
    monkeypatch, mandate
):
    """Manual Tickets omit speculative fields. Their context updates still resume."""
    ticket = _plan(mandate=mandate, attention=None)
    world = _World(ticket, _evaluation())
    world.inputs.append(
        {
            "input_id": "input-1",
            "ticket_id": "ticket-1",
            "trigger_id": "tr-9",
            "kind": "context",
            "text": "follow-up",
            "delivered_at": None,
            "created_at": None,
        }
    )
    _bind(monkeypatch, world)

    resumed = await daemons.resume_ticket("d1", 4)

    assert resumed is not None
    assert ticket["status"] == "running"
    candidate = next(sql for sql in world.sql if "FOR UPDATE OF t SKIP LOCKED" in sql)
    assert (
        _not_exclusion(
            candidate,
            kind="context",
            status="waiting",
            mandate=mandate if isinstance(mandate, dict) else None,
        )
        is True
    )


def _sql_admits_followup(ticket: dict) -> bool:
    """The conditional update delivery uses, applied to a fake row.

    Terminal Tickets are never revived: a completed, cancelled or archived
    Ticket matches nothing, so a follow-up stays pending for a new Eval.
    """
    return ticket.get("status") not in ("completed", "cancelled", "archived")


class _DiscardWorld:
    def __init__(
        self,
        ticket: dict,
        subscriptions: list[dict],
        inputs: list[dict] | None = None,
        triggers: list[dict] | None = None,
        threads: list[dict] | None = None,
        reports: list[dict] | None = None,
    ):
        self.ticket = dict(ticket)
        self.subscriptions = list(subscriptions)
        self.inputs = [dict(item) for item in (inputs or [])]
        self.triggers = {
            str(item["trigger_id"]): dict(item) for item in (triggers or [])
        }
        self.threads = {
            str(item["thread_key"]): dict(item) for item in (threads or [])
        }
        self.reports = [dict(item) for item in (reports or [])]
        self.input_row: dict | None = None
        self.discard_after_read = False
        self.discard_after_claim = False
        self.requeue_failures = 0
        self.stamp_failures = 0
        self.transactions = 0
        self.sql: list[str] = []

    def connect(self, *args, **kwargs):
        return _DiscardConn(self)


class _DiscardTx:
    def __init__(self, world: _DiscardWorld):
        self.world = world

    async def __aenter__(self):
        self.world.transactions += 1
        return self

    async def __aexit__(self, *exc):
        return False


class _DiscardConn:
    def __init__(self, world: _DiscardWorld):
        self.world = world

    def cursor(self, row_factory=None):
        return _DiscardCursor(self.world)

    def transaction(self):
        return _DiscardTx(self.world)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _DiscardCursor:
    def __init__(self, world: _DiscardWorld):
        self.world = world
        self.row = None
        self.rows: list[dict] = []

    async def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.world.sql.append(normalized)
        self.row = None
        self.rows = []
        self.rowcount = 0
        self._dispatch(normalized, tuple(params or ()))

    async def fetchone(self):
        return self.row

    async def fetchall(self):
        return list(self.rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _dispatch(self, sql: str, params: tuple) -> None:
        if "UPDATE autonomy_tickets" in sql and "SET status = 'completed'" in sql:
            summary, mandate, ended_at, ticket_id, expected = params
            ticket = self.world.ticket
            if ticket.get("ticket_id") != ticket_id or ticket.get("status") != expected:
                return
            stored = mandate.obj if hasattr(mandate, "obj") else mandate
            ticket.update(
                status="completed",
                summary=summary,
                attention=None,
                mandate=dict(stored),
                ended_at=ended_at,
            )
            self.row = dict(ticket)
            return
        if "SET status = 'running'" in sql and "NOT IN ('completed', 'cancelled', 'archived')" in sql:
            ticket_id = params[0]
            ticket = self.world.ticket
            if ticket.get("ticket_id") != ticket_id or not _sql_admits_followup(ticket):
                return
            ticket["status"] = "running"
            ticket["attention"] = None
            ticket["revision"] = int(ticket.get("revision") or 0) + 1
            self.row = dict(ticket)
            return
        if (
            sql.startswith("SELECT")
            and "FROM autonomy_tickets WHERE ticket_id" in sql
            and "FOR UPDATE" in sql
            and "worker_daemon_id" not in sql
        ):
            self.row = {"status": self.world.ticket.get("status")}
            return
        if (
            sql.startswith("SELECT")
            and "FROM autonomy_tickets WHERE ticket_id" in sql
            and "worker_daemon_id" in sql
        ):
            self.row = dict(self.world.ticket)
            if self.world.discard_after_read:
                self.world.ticket["status"] = "completed"
                mandate = dict(self.world.ticket.get("mandate") or {})
                mandate["speculative"] = True
                mandate["speculative_outcome"] = {"outcome": "discarded"}
                self.world.ticket["mandate"] = mandate
            return
        if (
            "FROM autonomy_ticket_inputs" in sql
            and "WHERE input_id = %s" in sql
            and not sql.startswith("UPDATE")
        ):
            self.row = dict(self.world.input_row) if self.world.input_row else None
            return
        if "DELETE FROM autonomy_subscriptions" in sql:
            raise AssertionError("discard must soft-end subscriptions, not delete")
        if "UPDATE autonomy_subscriptions" in sql and "ended_at" in sql:
            reason, ticket_id = params
            for row in self.world.subscriptions:
                if row.get("ticket_id") == ticket_id and row.get("ended_at") is None:
                    row["ended_at"] = "now"
                    row["end_reason"] = reason
            return
        if (
            sql.startswith("SELECT")
            and "JOIN autonomy_triggers tr" in sql
            and "WHERE i.input_id = %s" in sql
        ):
            input_id = params[0]
            candidates = list(self.world.inputs)
            if self.world.input_row is not None:
                candidates.append(self.world.input_row)
            for item in candidates:
                if item.get("input_id") != input_id:
                    continue
                trigger = self.world.triggers.get(str(item.get("trigger_id") or ""))
                if trigger is not None:
                    self.row = {
                        "input_id": input_id,
                        "ticket_id": item.get("ticket_id"),
                        "trigger_id": item["trigger_id"],
                        "kind": item.get("kind"),
                        "delivered_at": item.get("delivered_at"),
                        "thread_key": trigger["thread_key"],
                    }
                return
            return
        if "FROM autonomy_ticket_reports r" in sql:
            ticket_ids = {
                row["ticket_id"]
                for row in self.world.subscriptions
                if row.get("thread_key") == params[0]
            }
            self.rows = [
                dict(report)
                for report in self.world.reports
                if report.get("ticket_id") in ticket_ids
            ]
            return
        if "JOIN autonomy_subscriptions s" in sql and "s.ended_at IS NULL" in sql:
            active = [
                row
                for row in self.world.subscriptions
                if row.get("thread_key") == params[0] and row.get("ended_at") is None
            ]
            ticket_id = self.world.ticket.get("ticket_id")
            kind = next(
                (row.get("kind") for row in active if row.get("ticket_id") == ticket_id),
                None,
            )
            if kind is not None:
                row = dict(self.world.ticket)
                row["subscription_kind"] = kind
                self.rows = [row]
            return
        if "FROM autonomy_ticket_inputs i" in sql and "kind = 'trigger'" in sql:
            ticket_id = params[0]
            released = []
            for item in self.world.inputs:
                if item.get("ticket_id") != ticket_id or item.get("delivered_at") is not None:
                    continue
                if item.get("kind") != "trigger":
                    continue
                trigger = self.world.triggers.get(str(item.get("trigger_id")))
                if trigger is None:
                    continue
                released.append(
                    {
                        "input_id": item["input_id"],
                        "trigger_id": item["trigger_id"],
                        "thread_key": trigger["thread_key"],
                    }
                )
            self.rows = released
            return
        if "UPDATE autonomy_threads" in sql and "pending_trigger_ids" in sql:
            if self.world.requeue_failures > 0:
                self.world.requeue_failures -= 1
                raise RuntimeError("requeue unavailable")
            trigger_id, thread_key = params
            thread = self.world.threads.setdefault(
                thread_key,
                {
                    "thread_key": thread_key,
                    "pending_trigger_ids": [],
                    "eval_expiries": 0,
                },
            )
            pending = list(thread.get("pending_trigger_ids") or [])
            if trigger_id not in pending:
                pending.append(trigger_id)
            thread["pending_trigger_ids"] = pending
            thread["eval_expiries"] = 0
            return
        if (
            "UPDATE autonomy_ticket_inputs" in sql
            and "delivered_at = NULL" in sql
            and "WHERE input_id = %s" in sql
        ):
            input_id, expected_ticket_id, claimed_at = params
            candidates = [*self.world.inputs]
            if self.world.input_row is not None:
                candidates = candidates + [self.world.input_row]
            for item in candidates:
                if (
                    item.get("input_id") == input_id
                    and item.get("ticket_id") == expected_ticket_id
                    and item.get("delivered_at") == claimed_at
                ):
                    item["delivered_at"] = None
                    self.rowcount = 1
            return
        if (
            "UPDATE autonomy_ticket_inputs" in sql
            and "delivered_at" in sql
            and "WHERE input_id = %s" in sql
        ):
            if self.world.stamp_failures > 0:
                self.world.stamp_failures -= 1
                raise RuntimeError("stamp unavailable")
            input_id = params[0]
            candidates = self.world.inputs
            if self.world.input_row is not None:
                candidates = candidates + [self.world.input_row]
            for item in candidates:
                if item.get("input_id") == input_id and item.get("delivered_at") is None:
                    item["delivered_at"] = "now"
                    self.row = dict(item)
                    self.rowcount = 1
            if (
                self.world.discard_after_claim
                and "delivered_at = NOW()" in sql
                and "delivered_at IS NULL" in sql
            ):
                # The claim committed; the discard commits before admission
                # takes the ticket row lock.
                ticket = self.world.ticket
                ticket["status"] = "completed"
                mandate = dict(ticket.get("mandate") or {})
                mandate["speculative"] = True
                mandate["speculative_outcome"] = {"outcome": "discarded"}
                ticket["mandate"] = mandate


@pytest.mark.asyncio
async def test_discard_ends_subscriptions_in_the_same_transaction(monkeypatch):
    world = _DiscardWorld(
        {
            "ticket_id": "t1",
            "user_id": "u1",
            "status": "waiting",
            "mandate": {"speculative": True, "orchestration_only": True},
            "attention": {"question": "review"},
            "summary": "PLAN",
        },
        [{"ticket_id": "t1", "thread_key": "thread-1", "kind": "own"}],
    )
    monkeypatch.setattr(tickets, "get_db_connection", world.connect)

    result = await tickets.complete_discarded_plan(
        "t1",
        expected_status="waiting",
        mandate={
            "speculative": True,
            "speculative_outcome": {"outcome": "discarded"},
        },
        ended_at=datetime.now(timezone.utc),
        summary="",
    )

    assert result is not None
    assert result["status"] == "completed"
    assert result["mandate"]["speculative_outcome"]["outcome"] == "discarded"
    # Soft-end, not delete: the rows stay for thread-history reports and the
    # subscription audit listing (same rule as update_ticket / daemon settle).
    assert world.subscriptions == [
        {
            "ticket_id": "t1",
            "thread_key": "thread-1",
            "kind": "own",
            "ended_at": "now",
            "end_reason": tickets.END_TICKET_ENDED,
        }
    ]
    assert world.transactions == 1
    assert not any("DELETE FROM autonomy_subscriptions" in sql for sql in world.sql)
    update_at = next(
        index for index, sql in enumerate(world.sql) if "UPDATE autonomy_tickets" in sql
    )
    end_at = next(
        index
        for index, sql in enumerate(world.sql)
        if "UPDATE autonomy_subscriptions" in sql
    )
    assert update_at < end_at


@pytest.mark.asyncio
async def test_failed_requeue_after_terminal_settlement_heals_on_the_retry(
    monkeypatch,
):
    """Discard wins after the claim; the requeue fails; the retried effect hits
    a terminal ticket and must re-queue the trigger instead of dropping it."""
    input_row = {
        "input_id": "i-1",
        "ticket_id": "t-1",
        "trigger_id": "trg-1",
        "kind": "trigger",
        "text": "again",
        "delivered_at": None,
    }
    world = _DiscardWorld(
        {
            "ticket_id": "t-1",
            "user_id": "u-1",
            "workspace_id": "w-1",
            "status": "waiting",
            "mandate": {"speculative": True, "orchestration_only": True},
            "worker_thread_id": "th-1",
            "attention": {"question": "review"},
            "revision": 2,
        },
        [],
        inputs=[input_row],
        triggers=[{"trigger_id": "trg-1", "thread_key": "thread-1"}],
        threads=[{"thread_key": "thread-1", "pending_trigger_ids": [], "eval_expiries": 2}],
    )
    world.input_row = input_row
    world.discard_after_claim = True
    world.requeue_failures = 1
    monkeypatch.setattr(tickets, "get_db_connection", world.connect)
    dispatch = AsyncMock()
    monkeypatch.setattr(
        "src.server.services.autonomy.inputs.ProjectDispatcher",
        lambda: SimpleNamespace(dispatch_turn=dispatch),
    )
    from src.server.services.autonomy.inputs import deliver_input_effect

    with pytest.raises(RuntimeError, match="requeue unavailable"):
        await deliver_input_effect(
            SimpleNamespace(payload={"input_id": "i-1", "ticket_id": "t-1"})
        )

    # The settlement committed; the trigger is not queued and the claim was released.
    assert world.ticket["status"] == "completed"
    assert world.threads["thread-1"]["pending_trigger_ids"] == []
    assert input_row["delivered_at"] is None

    await deliver_input_effect(
        SimpleNamespace(payload={"input_id": "i-1", "ticket_id": "t-1"})
    )

    dispatch.assert_not_awaited()
    assert world.threads["thread-1"]["pending_trigger_ids"] == ["trg-1"]
    assert input_row["delivered_at"] == "now"


def _terminal_heal_world(ticket_status="completed"):
    input_row = {
        "input_id": "i-1",
        "ticket_id": "t-1",
        "trigger_id": "trg-1",
        "kind": "trigger",
        "text": "again",
        "delivered_at": None,
    }
    world = _DiscardWorld(
        {
            "ticket_id": "t-1",
            "user_id": "u-1",
            "workspace_id": "w-1",
            "status": ticket_status,
            "mandate": {"speculative": True, "orchestration_only": True},
            "worker_thread_id": "th-1",
            "attention": {"question": "review"},
            "revision": 2,
        },
        [],
        inputs=[input_row],
        triggers=[{"trigger_id": "trg-1", "thread_key": "thread-1"}],
        threads=[{"thread_key": "thread-1", "pending_trigger_ids": [], "eval_expiries": 2}],
    )
    world.input_row = input_row
    return world, input_row


@pytest.mark.asyncio
async def test_terminal_trigger_requeue_is_atomic_and_idempotent(monkeypatch):
    """The ownership check, queue append and delivered stamp commit together:
    a duplicate effect call must not queue the Trigger again."""
    world, input_row = _terminal_heal_world()
    monkeypatch.setattr(tickets, "get_db_connection", world.connect)
    dispatch = AsyncMock()
    monkeypatch.setattr(
        "src.server.services.autonomy.inputs.ProjectDispatcher",
        lambda: SimpleNamespace(dispatch_turn=dispatch),
    )
    from src.server.services.autonomy.inputs import deliver_input_effect

    await deliver_input_effect(
        SimpleNamespace(payload={"input_id": "i-1", "ticket_id": "t-1"})
    )

    dispatch.assert_not_awaited()
    assert world.transactions == 1
    assert world.threads["thread-1"]["pending_trigger_ids"] == ["trg-1"]
    assert input_row["delivered_at"] == "now"
    assert any("FOR UPDATE OF i" in sql for sql in world.sql)

    await deliver_input_effect(
        SimpleNamespace(payload={"input_id": "i-1", "ticket_id": "t-1"})
    )
    assert world.threads["thread-1"]["pending_trigger_ids"] == ["trg-1"]


@pytest.mark.asyncio
async def test_terminal_requeue_failure_after_queue_append_retries_once(monkeypatch):
    """A stamp failure after the queue append rolls back (production); the fake
    keeps the append, and the retry must still leave exactly one queued Trigger
    and eventually stamp the input delivered."""
    world, input_row = _terminal_heal_world()
    world.stamp_failures = 1
    monkeypatch.setattr(tickets, "get_db_connection", world.connect)
    monkeypatch.setattr(
        "src.server.services.autonomy.inputs.ProjectDispatcher",
        lambda: SimpleNamespace(dispatch_turn=AsyncMock()),
    )
    from src.server.services.autonomy.inputs import deliver_input_effect

    with pytest.raises(RuntimeError, match="stamp unavailable"):
        await deliver_input_effect(
            SimpleNamespace(payload={"input_id": "i-1", "ticket_id": "t-1"})
        )
    assert input_row["delivered_at"] is None

    await deliver_input_effect(
        SimpleNamespace(payload={"input_id": "i-1", "ticket_id": "t-1"})
    )
    assert world.threads["thread-1"]["pending_trigger_ids"] == ["trg-1"]
    assert input_row["delivered_at"] == "now"


@pytest.mark.asyncio
async def test_discard_race_keeps_the_subscriptions(monkeypatch):
    world = _DiscardWorld(
        {
            "ticket_id": "t1",
            "user_id": "u1",
            "status": "running",
            "mandate": {"speculative": True, "orchestration_only": True},
            "summary": "PLAN",
        },
        [{"ticket_id": "t1", "thread_key": "thread-1", "kind": "own"}],
    )
    monkeypatch.setattr(tickets, "get_db_connection", world.connect)

    result = await tickets.complete_discarded_plan(
        "t1",
        expected_status="waiting",
        mandate={"speculative": True},
        ended_at=datetime.now(timezone.utc),
    )

    assert result is None
    assert world.ticket["status"] == "running"
    assert world.subscriptions == [
        {"ticket_id": "t1", "thread_key": "thread-1", "kind": "own"}
    ]
    assert not any("UPDATE autonomy_subscriptions" in sql for sql in world.sql)
    assert not any("kind = 'trigger'" in sql for sql in world.sql)


@pytest.mark.asyncio
async def test_discard_requeues_an_undelivered_trigger_before_commit(monkeypatch):
    """An insert that won the lock, then lost to discard, must still be evaluated."""
    world = _DiscardWorld(
        {
            "ticket_id": "t1",
            "user_id": "u1",
            "status": "waiting",
            "mandate": {"speculative": True, "orchestration_only": True},
            "summary": "PLAN",
        },
        [{"ticket_id": "t1", "thread_key": "thread-1", "kind": "own"}],
        inputs=[
            {
                "input_id": "in-1",
                "ticket_id": "t1",
                "trigger_id": "trg-1",
                "kind": "trigger",
                "text": "new signal",
                "delivered_at": None,
            },
            {
                "input_id": "in-old",
                "ticket_id": "t1",
                "trigger_id": "trg-old",
                "kind": "trigger",
                "text": "already sent",
                "delivered_at": "earlier",
            },
        ],
        triggers=[{"trigger_id": "trg-1", "thread_key": "thread-1"}],
        threads=[
            {
                "thread_key": "thread-1",
                "pending_trigger_ids": [],
                "eval_expiries": 2,
            }
        ],
    )
    monkeypatch.setattr(tickets, "get_db_connection", world.connect)

    result = await tickets.complete_discarded_plan(
        "t1",
        expected_status="waiting",
        mandate={"speculative": True, "speculative_outcome": {"outcome": "discarded"}},
        ended_at=datetime.now(timezone.utc),
    )

    assert result is not None
    assert world.transactions == 1
    assert world.threads["thread-1"]["pending_trigger_ids"] == ["trg-1"]
    assert world.threads["thread-1"]["eval_expiries"] == 0
    delivered = {item["input_id"]: item["delivered_at"] for item in world.inputs}
    assert delivered["in-1"] == "now"
    assert delivered["in-old"] == "earlier"
    release_at = next(
        index for index, sql in enumerate(world.sql) if "kind = 'trigger'" in sql
    )
    end_at = next(
        index
        for index, sql in enumerate(world.sql)
        if "UPDATE autonomy_subscriptions" in sql
    )
    assert end_at < release_at


@pytest.mark.asyncio
async def test_discarded_plan_reports_stay_in_thread_history_and_routing_ignores_it(
    monkeypatch,
):
    """Soft-ended subscriptions keep the plan's reports joinable for thread
    history while every live-subscriber query keeps ignoring the ended row."""
    world = _DiscardWorld(
        {
            "ticket_id": "t1",
            "user_id": "u1",
            "status": "waiting",
            "mandate": {"speculative": True, "orchestration_only": True},
            "summary": "PLAN",
        },
        [{"ticket_id": "t1", "thread_key": "thread-1", "kind": "own"}],
        reports=[
            {
                "report_id": "r1",
                "ticket_id": "t1",
                "state": "ready",
                "summary": "PLAN",
                "question": None,
                "created_at": "ts",
            }
        ],
    )
    monkeypatch.setattr(tickets, "get_db_connection", world.connect)

    result = await tickets.complete_discarded_plan(
        "t1",
        expected_status="waiting",
        mandate={"speculative": True, "speculative_outcome": {"outcome": "discarded"}},
        ended_at=datetime.now(timezone.utc),
    )

    assert result is not None
    assert world.subscriptions[0]["ended_at"] == "now"
    assert world.subscriptions[0]["end_reason"] == tickets.END_TICKET_ENDED
    assert not any("DELETE FROM autonomy_subscriptions" in sql for sql in world.sql)
    history = await tickets.list_reports_for_thread("thread-1")
    assert [row["report_id"] for row in history] == ["r1"]
    assert await tickets.list_tickets_for_thread("thread-1") == []


@pytest.mark.asyncio
async def test_admission_refuses_a_discarded_plan(monkeypatch):
    world = _DiscardWorld(
        {
            "ticket_id": "t1",
            "user_id": "u1",
            "status": "completed",
            "mandate": {"speculative": True, "orchestration_only": True},
            "attention": {"question": "review"},
            "revision": 4,
        },
        [{"ticket_id": "t1", "thread_key": "thread-1", "kind": "own"}],
    )
    monkeypatch.setattr(tickets, "get_db_connection", world.connect)

    assert await tickets.admit_followup_delivery("t1") is None
    assert world.ticket["status"] == "completed"
    assert world.ticket["attention"] == {"question": "review"}
    assert world.ticket["revision"] == 4


@pytest.mark.asyncio
async def test_admission_refuses_an_ordinary_completed_ticket(monkeypatch):
    """The lifecycle rule makes every terminal Ticket settled for good:
    admission no longer revives an ordinary completed Ticket either, so the
    signal stays pending for a new Eval."""
    world = _DiscardWorld(
        {
            "ticket_id": "t1",
            "user_id": "u1",
            "status": "completed",
            "mandate": {"source_id": "s1"},
            "revision": 1,
        },
        [],
    )
    monkeypatch.setattr(tickets, "get_db_connection", world.connect)

    assert await tickets.admit_followup_delivery("t1") is None
    assert world.ticket["status"] == "completed"
    assert world.ticket["revision"] == 1


@pytest.mark.asyncio
async def test_admission_captures_prior_status_under_the_row_lock(monkeypatch):
    """A statement-snapshot CTE could report a status another admission already
    replaced. The row lock must be taken before prior_status is read."""
    world = _DiscardWorld(
        {
            "ticket_id": "t-1",
            "user_id": "u-1",
            "workspace_id": "w-1",
            "status": "waiting",
            "mandate": {"speculative": True},
            "worker_thread_id": "th-1",
            "attention": {"question": "review"},
            "revision": 2,
        },
        [],
    )
    monkeypatch.setattr(tickets, "get_db_connection", world.connect)

    admitted = await tickets.admit_followup_delivery("t-1")

    assert admitted is not None
    assert admitted["prior_status"] == "waiting"
    select_at = next(
        index
        for index, sql in enumerate(world.sql)
        if "SELECT status" in sql and "FOR UPDATE" in sql
    )
    update_at = next(
        index for index, sql in enumerate(world.sql) if "SET status = 'running'" in sql
    )
    assert select_at < update_at
    assert not any("WITH prior AS" in sql for sql in world.sql)


@pytest.mark.asyncio
async def test_admission_and_discard_cannot_both_win(monkeypatch):
    world = _DiscardWorld(
        {
            "ticket_id": "t1",
            "user_id": "u1",
            "status": "waiting",
            "mandate": {"speculative": True, "orchestration_only": True},
            "revision": 1,
        },
        [{"ticket_id": "t1", "thread_key": "thread-1", "kind": "own"}],
    )
    monkeypatch.setattr(tickets, "get_db_connection", world.connect)

    admitted = await tickets.admit_followup_delivery("t1")
    lost = await tickets.complete_discarded_plan(
        "t1",
        expected_status="waiting",
        mandate={"speculative": True},
        ended_at=datetime.now(timezone.utc),
    )

    assert admitted is not None and admitted["status"] == "running"
    assert lost is None
    assert world.ticket["status"] == "running"
    assert world.subscriptions == [
        {"ticket_id": "t1", "thread_key": "thread-1", "kind": "own"}
    ]


@pytest.mark.asyncio
async def test_stale_delivery_read_does_not_resume_a_discarded_plan(monkeypatch):
    """Discard commits after delivery's read and before its dispatch."""
    world = _DiscardWorld(
        {
            "ticket_id": "t-1",
            "user_id": "u-1",
            "workspace_id": "w-1",
            "status": "waiting",
            "mandate": {"speculative": True, "orchestration_only": True},
            "worker_thread_id": "th-1",
            "attention": {"question": "review"},
            "revision": 2,
        },
        [],
    )
    world.discard_after_read = True
    world.input_row = {
        "input_id": "i-1",
        "ticket_id": "t-1",
        "kind": "trigger",
        "text": "again",
        "delivered_at": None,
    }
    monkeypatch.setattr(tickets, "get_db_connection", world.connect)
    dispatch = AsyncMock()
    monkeypatch.setattr(
        "src.server.services.autonomy.inputs.ProjectDispatcher",
        lambda: SimpleNamespace(dispatch_turn=dispatch),
    )
    from src.server.services.autonomy.inputs import deliver_input_effect

    await deliver_input_effect(
        SimpleNamespace(payload={"input_id": "i-1", "ticket_id": "t-1"})
    )

    dispatch.assert_not_awaited()
    assert any("SET status = 'running'" in sql for sql in world.sql)
    assert world.ticket["status"] == "completed"
    assert world.ticket["attention"] == {"question": "review"}
    assert world.ticket["revision"] == 2


@pytest.mark.asyncio
async def test_claim_then_discard_requeues_the_trigger_and_keeps_the_delivery(
    monkeypatch,
):
    """The claim commits, then the discard wins the ticket row lock before
    admission. The discard's re-queue scan only sees undelivered inputs, so
    releasing the claim would strand it undelivered on the completed plan
    with its trigger already off the pending queue. The input keeps its
    delivery mark and the trigger goes back on the eval queue for a new Eval.
    """
    input_row = {
        "input_id": "i-1",
        "ticket_id": "t-1",
        "trigger_id": "trg-1",
        "kind": "trigger",
        "text": "again",
        "delivered_at": None,
    }
    world = _DiscardWorld(
        {
            "ticket_id": "t-1",
            "user_id": "u-1",
            "workspace_id": "w-1",
            "status": "waiting",
            "mandate": {"speculative": True, "orchestration_only": True},
            "worker_thread_id": "th-1",
            "attention": {"question": "review"},
            "revision": 2,
        },
        [{"ticket_id": "t-1", "thread_key": "thread-1", "kind": "own"}],
        inputs=[input_row],
        triggers=[{"trigger_id": "trg-1", "thread_key": "thread-1"}],
        threads=[{"thread_key": "thread-1", "pending_trigger_ids": [], "eval_expiries": 2}],
    )
    world.input_row = input_row
    world.discard_after_claim = True
    monkeypatch.setattr(tickets, "get_db_connection", world.connect)
    dispatch = AsyncMock()
    monkeypatch.setattr(
        "src.server.services.autonomy.inputs.ProjectDispatcher",
        lambda: SimpleNamespace(dispatch_turn=dispatch),
    )
    from src.server.services.autonomy.inputs import deliver_input_effect

    await deliver_input_effect(
        SimpleNamespace(payload={"input_id": "i-1", "ticket_id": "t-1"})
    )

    dispatch.assert_not_awaited()
    assert world.ticket["status"] == "completed"
    # Kept delivered, not released back to NULL.
    assert input_row["delivered_at"] == "now"
    assert not any("SET delivered_at = NULL" in sql for sql in world.sql)
    assert world.threads["thread-1"]["pending_trigger_ids"] == ["trg-1"]
    assert world.threads["thread-1"]["eval_expiries"] == 0
