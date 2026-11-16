import asyncio
import re
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.server.app import autonomy as api


class FakeDb:
    def __init__(self, row):
        self.row = row
        self.calls = []

    async def get_ticket(self, _id):
        return self.row

    async def record_switch(self, _id, **kw):
        self.calls.append(kw)
        return {**self.row, "worker_provider": kw["record"]["to"]["provider"],
                "worker_model": kw["record"]["to"]["model"], "switches": [kw["record"]]}


def _row(**kw):
    base = {"ticket_id": "t", "user_id": "u", "status": "running", "worker_provider": "codex",
            "worker_model": "gpt-x", "mandate": {"reviewer": {"provider": "kimi", "model": "k2"}}}
    return {**base, **kw}


@pytest.fixture
def setup(monkeypatch):
    async def ok(_u):
        return None

    monkeypatch.setattr(api, "_require_feature", ok)
    monkeypatch.setattr(api, "_ticket_view", lambda r: r)

    def install(row):
        db = FakeDb(row)
        monkeypatch.setattr(api, "tickets_db", db)
        return db

    return install


def _run(body):
    return asyncio.run(api.switch_ticket("00000000-0000-0000-0000-000000000001", body, "u"))


def test_switch_skips_ticket_reviewer(setup):
    db = setup(_row())
    body = api.SwitchRequest(candidates=[{"provider": "kimi", "model": "k2"}, {"provider": "grok", "model": "g4"}],
                             diff="d", findings=["f"])
    out = _run(body)
    assert out["worker_provider"] == "grok"
    assert db.calls[0]["expected_model"] == ("codex", "gpt-x")
    assert "Open findings" in db.calls[0]["handoff_text"]


def test_switch_to_reviewer_refused(setup):
    setup(_row())
    with pytest.raises(HTTPException) as e:
        _run(api.SwitchRequest(to_provider="kimi", to_model="k2"))
    assert e.value.status_code == 409


def test_switch_finished_ticket_refused(setup):
    setup(_row(status="completed"))
    with pytest.raises(HTTPException) as e:
        _run(api.SwitchRequest(to_provider="grok", to_model="g4"))
    assert e.value.status_code == 409


def test_switch_archived_ticket_refused(setup):
    setup(_row(status="archived"))
    with pytest.raises(HTTPException) as e:
        _run(api.SwitchRequest(to_provider="grok", to_model="g4"))
    assert e.value.status_code == 409


def test_switch_other_users_ticket_404(setup):
    setup(_row(user_id="someone"))
    with pytest.raises(HTTPException) as e:
        _run(api.SwitchRequest(to_provider="grok", to_model="g4"))
    assert e.value.status_code == 404


def test_body_reviewer_cannot_override_mandate(setup):
    setup(_row())
    body = api.SwitchRequest.model_validate(
        {"to_provider": "kimi", "to_model": "k2", "reviewer": {"provider": "x", "model": "y"}}
    )
    with pytest.raises(HTTPException) as e:
        _run(body)
    assert e.value.status_code == 409


def test_blank_model_candidate_skipped():
    from src.server.services.autonomy import line_switch as ls

    got = ls.pick_target(
        [ls.ModelRef("kimi", ""), ls.ModelRef("grok", "g4")], ls.ModelRef("codex", "gpt-x")
    )
    assert got.provider == "grok"


def test_switch_running_requeues_atomically_and_cancels_old_run(setup, monkeypatch):
    db = setup(_row(worker_thread_id="th", worker_run_id="r1"))
    cancelled = []

    class FakeDispatcher:
        async def cancel_thread(self, **kw):
            cancelled.append(kw)

    import src.server.services.projects.dispatch as disp

    monkeypatch.setattr(disp, "ProjectDispatcher", FakeDispatcher)
    _run(api.SwitchRequest(to_provider="grok", to_model="g4"))
    # An unisolated cloud switch must not enqueue a launch. The old run is
    # still cancelled so it does not keep executing.
    assert db.calls[0]["launch_effect"] is None
    assert cancelled[0]["run_id"] == "r1"
    assert cancelled[0]["thread_id"] == "th"


def test_switch_survives_cancel_failure(setup, monkeypatch):
    setup(_row(worker_thread_id="th", worker_run_id="r1"))

    class Boom:
        async def cancel_thread(self, **kw):
            raise RuntimeError("down")

    import src.server.services.projects.dispatch as disp

    monkeypatch.setattr(disp, "ProjectDispatcher", Boom)
    out = _run(api.SwitchRequest(to_provider="grok", to_model="g4"))
    assert out["worker_provider"] == "grok"


class _Cur:
    def __init__(self, sink, row):
        self.sink, self.row, self.rowcount = sink, row, 1

    async def execute(self, sql, params=None):
        self.sink.append((sql, params))

    async def fetchone(self):
        return self.row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Tx:
    def __init__(self, sink):
        self.sink = sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        self.sink.append(("COMMIT" if a[0] is None else "ROLLBACK", None))
        return False


class _Conn:
    def __init__(self, sink, row):
        self.sink, self.row = sink, row

    def cursor(self, **kw):
        return _Cur(self.sink, self.row)

    def transaction(self):
        self.sink.append(("BEGIN", None))
        return _Tx(self.sink)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def test_record_switch_requeues_and_fences_lease_in_one_statement(monkeypatch):
    from src.server.database.autonomy import tickets as t

    sink = []
    row = {"ticket_id": "t", "mandate": {}, "switches": [1]}
    monkeypatch.setattr(t, "get_db_connection", lambda: _Conn(sink, row))
    rec = {"to": {"provider": "grok", "model": "g4"}}
    out = asyncio.run(t.record_switch(
        "t", expected_status="running", expected_model=("a", "b"), record=rec,
        handoff_text="h", launch_effect=("u", "launch_worker"),
    ))
    assert out["switches"] == [1]
    assert sink[0][0] == "BEGIN" and sink[-1][0] == "COMMIT"
    sink[:] = [x for x in sink if x[0] not in ("BEGIN", "COMMIT")]
    update_sql = sink[0][0]
    for col in ("worker_thread_id", "worker_run_id", "worker_lease_id", "worker_lease_expires_at"):
        assert f"{col} = CASE WHEN status = 'running' THEN NULL" in update_sql
    assert "status = CASE WHEN status = 'running' THEN 'queued'" in update_sql
    # The launch effect is inserted on the same cursor, i.e. the same transaction.
    assert "autonomy_effects" in sink[1][0]
    assert sink[1][1][3].obj == {"ticket_id": "t"}


def test_record_switch_lost_race_inserts_no_effect(monkeypatch):
    from src.server.database.autonomy import tickets as t

    sink = []
    monkeypatch.setattr(t, "get_db_connection", lambda: _Conn(sink, None))
    out = asyncio.run(t.record_switch(
        "t", expected_status="running", expected_model=("a", "b"),
        record={"to": {"provider": "g", "model": "m"}}, handoff_text="h",
        launch_effect=("u", "launch_worker"),
    ))
    assert out is None and [x[0] for x in sink].count("BEGIN") == 1
    assert not any("autonomy_effects" in (x[0] or "") for x in sink)


def test_clear_handoff_is_fenced_on_delivered_text(monkeypatch):
    from src.server.database.autonomy import tickets as t
    from src.server.services.autonomy import worker as w

    sink = []
    monkeypatch.setattr(t, "get_db_connection", lambda: _Conn(sink, None))
    monkeypatch.setattr(w, "ticket_db", t)
    asyncio.run(w.clear_handoff("t", "handoff A"))
    sql, params = sink[0]
    assert "mandate->>'handoff' = %s" in sql
    assert params == ("t", "handoff A")


def test_resume_request_key_differs_per_switch():
    from src.server.services.autonomy import worker as w

    assert w._resume_request_key("t", {}) == "autonomy:worker:t"
    assert w._resume_request_key("t", {"switches": [1]}) == "autonomy:worker:t:1"


def test_daemon_claim_passes_delivered_handoff(monkeypatch):
    from src.server.services.autonomy import worker as w

    seen = []

    async def claim(*_a, **_k):
        return {"ticket_id": "t", "worker_daemon_id": "d", "mandate": {"handoff": "H"}, "lease_id": "l",
                "lease_expires_at": __import__("datetime").datetime.now(),
                "lease_duration_seconds": 5, "inputs": []}

    async def clear(tid, delivered):
        seen.append((tid, delivered))

    async def daemon(_t):
        return {"daemon_id": "d"}

    async def src(_i):
        return None

    async def shared(_t):
        return ""

    from src.server.database.autonomy import sources
    from src.server.services.autonomy import shared_context

    monkeypatch.setattr(api, "_require_daemon", daemon)
    monkeypatch.setattr(w, "claim_daemon_ticket", claim)
    monkeypatch.setattr(w, "clear_handoff", clear)
    monkeypatch.setattr(sources, "get_source_any_user", src)
    monkeypatch.setattr(shared_context, "worker_shared_context_text", shared)
    out = asyncio.run(api.daemon_claim(None, "tok"))
    assert seen == [("t", "H")]
    assert out["prompt"].startswith("H")


def test_daemon_line_cannot_switch_cross_provider(setup):
    setup(_row(worker_daemon_id="d"))
    with pytest.raises(HTTPException) as e:
        _run(api.SwitchRequest(to_provider="grok", to_model="g4"))
    assert e.value.status_code == 409


def test_daemon_line_candidates_filtered_to_provider(setup):
    db = setup(_row(worker_daemon_id="d"))
    out = _run(api.SwitchRequest(candidates=[
        {"provider": "grok", "model": "g4"}, {"provider": "codex", "model": "gpt-y"}]))
    assert out["worker_provider"] == "codex" and db.calls


def test_stale_native_report_is_rejected(monkeypatch):
    from src.server.services.autonomy import host

    async def get(_id):
        return {"status": "queued", "worker_thread_id": None, "mandate": {}}

    monkeypatch.setattr(host.ticket_db, "get_ticket", get)
    with pytest.raises(host.StaleWorkerReport):
        asyncio.run(host.apply_worker_report(
            ticket_id="t", state="ready", summary="x", expected_thread_id="old"))


def test_ptc_worker_report_stale_line_is_ignored(monkeypatch):
    from src.server.services.autonomy import host

    async def get(_id):
        # A switch moved the Ticket to a replacement thread.
        return {"status": "queued", "worker_thread_id": "new-thread", "mandate": {}}

    add_report = []
    monkeypatch.setattr(host.ticket_db, "get_ticket", get)
    monkeypatch.setattr(
        host.ticket_db, "add_report", lambda **kw: add_report.append(kw)
    )
    out = asyncio.run(
        host.AutonomyWorkerHost(ticket_id="t", thread_id="old-thread")
        .worker_report(state="ready", summary="done")
    )
    assert out == {"ticket_id": "t", "status": "ignored_switched"}
    # The stale report never lands on the Ticket.
    assert add_report == []


def test_ptc_worker_report_current_line_settles(monkeypatch):
    from src.server.services.autonomy import host
    from src.server.database.autonomy import tickets as ticket_db

    async def get(_id):
        return dict(db.ticket)

    db = _StatefulReportDb({
        "ticket_id": "t", "status": "running", "worker_thread_id": "thread-1",
        "mandate": {}, "user_id": "u", "revision": 0,
    })

    async def enqueue(**kw):
        return None

    monkeypatch.setattr(ticket_db, "get_ticket", get)
    monkeypatch.setattr(ticket_db, "get_db_connection", db)
    monkeypatch.setattr(host, "enqueue_effect", AsyncMock(side_effect=enqueue))
    out = asyncio.run(
        host.AutonomyWorkerHost(ticket_id="t", thread_id="thread-1")
        .worker_report(state="ready", summary="done")
    )
    assert out == {"ticket_id": "t", "state": "ready"}
    assert db.reports == [{
        "report_id": "r1", "ticket_id": "t", "state": "ready",
        "summary": "done", "question": None,
        "body": "done", "git_check": None,
    }]
    assert db.ticket["status"] == "completed"
    assert db.ticket["summary"] == "done"
    # Guarded UPDATE bumps revision once; no second standalone bump.
    assert db.ticket["revision"] == 1


class _StatefulReportDb:
    def __init__(self, ticket):
        self.ticket = dict(ticket)
        self.reports = []
        self.fail_insert = False
        self._snapshot = None

    def __call__(self):
        return _StatefulConn(self)


class _StatefulTx:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        self.db._snapshot = (dict(self.db.ticket), list(self.db.reports))
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.db.ticket, self.db.reports = self.db._snapshot
        self.db._snapshot = None
        return False


class _StatefulCur(_Cur):
    def __init__(self, db):
        super().__init__([], None)
        self.db = db
        self.result = None

    async def execute(self, sql, params=None):
        if sql.lstrip().startswith("UPDATE autonomy_tickets SET") and "RETURNING" in sql:
            columns = re.findall(r"([a-z_]+) = %s", sql.split("WHERE", 1)[0])
            ticket_id, expected_thread_id = params[len(columns):]
            if (
                self.db.ticket["ticket_id"] != ticket_id
                or self.db.ticket["status"] != "running"
                or self.db.ticket["worker_thread_id"] != expected_thread_id
            ):
                self.result = None
                return
            self.db.ticket.update(dict(zip(columns, params[:len(columns)])))
            self.db.ticket["revision"] = self.db.ticket.get("revision", 0) + 1
            self.result = dict(self.db.ticket)
            return
        if sql.lstrip().startswith("INSERT INTO autonomy_ticket_reports"):
            if self.db.fail_insert:
                raise RuntimeError("insert failed")
            ticket_id, state, summary, question, body, git_check = params
            report = {
                "report_id": f"r{len(self.db.reports) + 1}",
                "ticket_id": ticket_id,
                "state": state,
                "summary": summary,
                "question": question,
                "body": body,
                "git_check": git_check,
            }
            self.db.reports.append(report)
            self.result = report
            return
        if sql.lstrip().startswith("UPDATE autonomy_tickets SET revision = revision + 1"):
            self.db.ticket["revision"] = self.db.ticket.get("revision", 0) + 1
            self.result = None

    async def fetchone(self):
        return self.result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _StatefulConn:
    def __init__(self, db):
        self.db = db

    def cursor(self, **kw):
        return _StatefulCur(self.db)

    def transaction(self):
        return _StatefulTx(self.db)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def test_ptc_worker_progress_cannot_write_after_switch(monkeypatch):
    """Switching after the host read but before its guarded update leaves no report."""
    from src.server.services.autonomy import host
    from src.server.database.autonomy import tickets as ticket_db

    db = _StatefulReportDb({
        "ticket_id": "t", "status": "running", "worker_thread_id": "old-thread",
        "summary": "prior summary", "revision": 10, "mandate": {}, "user_id": "u",
    })
    switch_after_read = True

    async def get(_id):
        nonlocal switch_after_read
        snapshot = dict(db.ticket)
        if switch_after_read:
            switch_after_read = False
            # This is the switch's committed row state before the report UPDATE.
            db.ticket.update(status="queued", worker_thread_id="new-thread", revision=11)
        return snapshot

    monkeypatch.setattr(ticket_db, "get_ticket", get)
    monkeypatch.setattr(ticket_db, "get_db_connection", db)
    monkeypatch.setattr(host, "enqueue_effect", AsyncMock())

    result = asyncio.run(
        host.AutonomyWorkerHost(ticket_id="t", thread_id="old-thread")
        .worker_progress(summary="stale progress")
    )

    assert result == {"ticket_id": "t", "status": "ignored_switched"}
    assert db.reports == []
    assert db.ticket["status"] == "queued"
    assert db.ticket["worker_thread_id"] == "new-thread"
    assert db.ticket["summary"] == "prior summary"
    assert db.ticket["revision"] == 11


def test_ptc_worker_final_report_cannot_commit_after_switch(monkeypatch):
    from src.server.services.autonomy import host
    from src.server.database.autonomy import tickets as ticket_db

    db = _StatefulReportDb({
        "ticket_id": "t",
        "status": "running",
        "worker_thread_id": "old-thread",
        "summary": "prior summary",
        "revision": 10,
        "mandate": {},
        "user_id": "u",
    })
    switch_after_read = True

    async def get(_id):
        nonlocal switch_after_read
        snapshot = dict(db.ticket)
        if switch_after_read:
            switch_after_read = False
            db.ticket.update(status="queued", worker_thread_id="new-thread", revision=11)
        return snapshot

    monkeypatch.setattr(ticket_db, "get_ticket", get)
    monkeypatch.setattr(ticket_db, "get_db_connection", db)
    monkeypatch.setattr(host, "enqueue_effect", AsyncMock())
    out = asyncio.run(
        host.AutonomyWorkerHost(ticket_id="t", thread_id="old-thread")
        .worker_report(state="ready", summary="stale final")
    )

    assert out == {"ticket_id": "t", "status": "ignored_switched"}
    assert db.reports == []
    assert db.ticket["status"] == "queued"
    assert db.ticket["worker_thread_id"] == "new-thread"
    assert db.ticket["summary"] == "prior summary"
    assert db.ticket["revision"] == 11


def test_ptc_worker_progress_cannot_write_after_line_stops_running(monkeypatch):
    from src.server.services.autonomy import host
    from src.server.database.autonomy import tickets as ticket_db

    db = _StatefulReportDb({
        "ticket_id": "t", "status": "failed", "worker_thread_id": "thread-1",
        "summary": "final failure", "revision": 4, "mandate": {},
    })

    async def get(_id):
        return dict(db.ticket)

    monkeypatch.setattr(ticket_db, "get_ticket", get)
    monkeypatch.setattr(ticket_db, "get_db_connection", db)

    result = asyncio.run(
        host.AutonomyWorkerHost(ticket_id="t", thread_id="thread-1")
        .worker_progress(summary="late progress")
    )

    assert result == {"ticket_id": "t", "status": "ignored_switched"}
    assert db.ticket["summary"] == "final failure"
    assert db.ticket["revision"] == 4
    assert db.reports == []


def test_record_worker_report_fences_and_inserts_in_one_transaction(monkeypatch):
    from src.server.database.autonomy import tickets as t

    sink = []
    row = {
        "ticket_id": "t", "status": "running", "worker_thread_id": "th", "report_id": "r"
    }
    monkeypatch.setattr(t, "get_db_connection", lambda: _Conn(sink, row))

    out = asyncio.run(
        t.record_worker_report(
            ticket_id="t",
            expected_thread_id="th",
            state="ready",
            summary="done",
            body="full result",
            ticket_fields={"status": "completed", "summary": "done"},
        )
    )

    sqls = [sql for sql, _ in sink if sql not in ("BEGIN", "COMMIT", "ROLLBACK")]
    assert out["report_id"] == "r"
    assert sink[0][0] == "BEGIN" and sink[-1][0] == "COMMIT"
    assert "worker_thread_id = %s" in sqls[0]
    assert "status = 'running'" in sqls[0]
    assert "revision = revision + 1" in sqls[0]
    # A terminal settle ends the Ticket's live subscriptions in the same
    # transaction, before the report insert.
    assert "UPDATE autonomy_subscriptions" in sqls[1]
    assert "ended_at IS NULL" in sqls[1]
    assert "INSERT INTO autonomy_ticket_reports" in sqls[2]
    assert "body" in sqls[2]
    assert len(sqls) == 3
    assert not any(
        sql.lstrip().startswith("UPDATE autonomy_tickets SET revision = revision + 1")
        for sql in sqls
    )


def test_record_worker_report_lost_fence_inserts_nothing(monkeypatch):
    from src.server.database.autonomy import tickets as t

    sink = []
    monkeypatch.setattr(t, "get_db_connection", lambda: _Conn(sink, None))

    out = asyncio.run(
        t.record_worker_report(
            ticket_id="t",
            expected_thread_id="old",
            state="progress",
            summary="stale",
            ticket_fields={"summary": "stale"},
        )
    )

    assert out is None
    assert sink[0][0] == "BEGIN" and sink[-1][0] == "COMMIT"
    assert not any("INSERT INTO autonomy_ticket_reports" in sql for sql, _ in sink)


def test_record_worker_report_rolls_back_if_report_insert_fails(monkeypatch):
    from src.server.database.autonomy import tickets as t

    db = _StatefulReportDb({
        "ticket_id": "t", "status": "running", "worker_thread_id": "th",
        "summary": "before", "revision": 3,
    })
    db.fail_insert = True
    monkeypatch.setattr(t, "get_db_connection", db)

    with pytest.raises(RuntimeError, match="insert failed"):
        asyncio.run(
            t.record_worker_report(
                ticket_id="t",
                expected_thread_id="th",
                state="ready",
                summary="done",
                ticket_fields={"status": "completed"},
            )
        )

    assert db.ticket == {
        "ticket_id": "t", "status": "running", "worker_thread_id": "th",
        "summary": "before", "revision": 3,
    }
    assert db.reports == []
