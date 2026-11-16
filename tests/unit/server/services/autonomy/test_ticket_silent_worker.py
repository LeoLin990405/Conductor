"""A Ticket Worker that produces no output is a dispatch failure of its own class.

Workers that received their prompt and never answered ended in silence: the
Ticket kept waiting. The sweep classifies that run as ``worker_no_output``,
alerts once, and in enforce mode moves the line to another provider through
the existing switch path. No database and no network: the ticket store, the
thread reads and the switch are fakes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.server.services.autonomy import notifications as notes
from src.server.services.autonomy import silent_worker as sw
from src.server.services.autonomy import watchdog, worker_idle

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
STARTED = NOW - timedelta(minutes=40)


def _ticket(**overrides) -> dict:
    ticket = {
        "ticket_id": "ticket-1",
        "user_id": "user-1",
        "workspace_id": "ws-1",
        "title": "Fix the parser",
        "status": "running",
        "revision": 3,
        "worker_provider": "glm",
        "worker_model": "glm-5",
        "worker_thread_id": "thread-1",
        "worker_run_id": "run-1",
        "worker_lease_id": None,
        "worker_daemon_id": None,
        "attempts": 1,
        "switches": [],
        "mandate": {
            "switch_candidates": [
                {"provider": "zcode", "model": "glm-5"},
                {"provider": "kimi", "model": "k2"},
            ]
        },
    }
    ticket.update(overrides)
    return ticket


class FakeStore:
    """The ticket rows, the mandate writes and the switch, in memory."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = {row["ticket_id"]: row for row in rows}
        self.mandate_writes: list[tuple[str, dict]] = []
        self.switches: list[dict] = []
        self.candidate_calls = 0
        self.refuse_mandate = False

    async def list_worker_idle_candidates(self, *, idle_seconds, limit, offset):
        self.candidate_calls += 1
        rows = [dict(row) for row in self.rows.values() if row["status"] == "running"]
        return rows[offset : offset + limit]

    async def update_mandate_record(self, ticket_id, key, *, set_fields=None, **fence):
        if self.refuse_mandate:
            return None
        row = self.rows[ticket_id]
        if fence.get("expected_status") and row["status"] != fence["expected_status"]:
            return None
        if fence.get("expected_revision") is not None and row["revision"] != fence["expected_revision"]:
            return None
        mandate = dict(row.get("mandate") or {})
        record = dict(mandate.get(key) or {})
        for name in fence.get("remove_fields") or ():
            record.pop(name, None)
        record.update(set_fields or {})
        mandate[key] = record
        row["mandate"] = mandate
        if fence.get("touch", True):
            row["revision"] += 1
        self.mandate_writes.append((ticket_id, dict(record)))
        return dict(row)

    async def record_switch(self, ticket_id, *, expected_status, expected_model, record, handoff_text, launch_effect=None):
        row = self.rows[ticket_id]
        if row["status"] != expected_status or (row["worker_provider"], row["worker_model"]) != expected_model:
            return None
        self.switches.append(
            {"record": record, "handoff_text": handoff_text, "launch_effect": launch_effect}
        )
        row.update(
            status="queued",
            worker_provider=record["to"]["provider"],
            worker_model=record["to"]["model"],
            worker_thread_id=None,
            worker_run_id=None,
            switches=[*row.get("switches", []), record],
        )
        row["revision"] += 1
        return dict(row)

    async def latest_report(self, ticket_id):
        return None


@pytest.fixture
def env(monkeypatch):
    """Patch the store and the probes; return a namespace the tests tune."""

    state = SimpleNamespace(
        store=FakeStore([_ticket()]),
        run_status="in_progress",
        assistant_text=None,
        streamed=False,
        committed=False,
        report=None,
        cancelled=[],
        banners=[],
        # The daemon run log: None (no session), or a list of output events.
        daemon_events=None,
        daemon_lease_live=True,
    )

    def install():
        store = state.store
        for name in (
            "list_worker_idle_candidates",
            "update_mandate_record",
            "record_switch",
        ):
            monkeypatch.setattr(sw.ticket_db, name, getattr(store, name))

        async def latest_report(ticket_id):
            return state.report

        monkeypatch.setattr(sw.ticket_db, "latest_report", latest_report)

    install()
    state.install = install
    monkeypatch.setattr(sw, "database_is_open", lambda: True)
    monkeypatch.setattr(
        worker_idle, "_attempt_started_at", AsyncMock(return_value=STARTED)
    )
    monkeypatch.setattr(worker_idle, "_worker_has_commit", lambda t: state.committed)

    async def streamed(ticket):
        return state.streamed

    monkeypatch.setattr(sw, "_has_streamed_output", streamed)

    from src.server.database.autonomy import daemon_output as output_db
    from src.server.database.autonomy import daemons as daemon_db

    async def read_session(ticket_id):
        if state.daemon_events is None:
            return None
        row = state.store.rows.get(ticket_id) or {}
        return {
            "ticket_id": ticket_id,
            "epoch": 1,
            "lease_id": row.get("worker_lease_id"),
            "run_id": None,
            "next_seq": len(state.daemon_events),
            "tainted": False,
        }

    async def read_events(ticket_id, epoch, after_seq, limit):
        return list(enumerate(state.daemon_events or [], start=1))[:limit]

    async def live_lease_ticket(daemon_id, ticket_id, lease_id):
        return state.daemon_lease_live

    monkeypatch.setattr(output_db, "read_session", read_session)
    monkeypatch.setattr(output_db, "read_events", read_events)
    monkeypatch.setattr(daemon_db, "live_lease_ticket", live_lease_ticket)

    class FakeDispatcher:
        async def thread_run_status(self, *, user_id, thread_id):
            return state.run_status

        async def thread_last_assistant_text(self, *, user_id, thread_id):
            return state.assistant_text

        async def cancel_thread(self, *, user_id, thread_id, run_id):
            state.cancelled.append((thread_id, run_id))
            return True

    from src.server.services.projects import dispatch as project_dispatch

    monkeypatch.setattr(project_dispatch, "ProjectDispatcher", FakeDispatcher)

    from src.server.services.autonomy import cloud_runtime, worker

    monkeypatch.setattr(worker, "is_unisolated_cloud_worker", lambda t: False)
    monkeypatch.setattr(
        cloud_runtime,
        "resolve_cloud_worker_isolation_async",
        AsyncMock(return_value=SimpleNamespace(launch_allowed=True)),
    )
    inbox = notes.MemoryNotificationStore()
    state.inbox = inbox

    def channel(title, body):
        state.banners.append((title, body))
        return True

    state.channel = channel
    with notes.override_store(inbox):
        yield state


async def _sweep(state, mode="shadow", **kwargs):
    return await sw.sweep_silent_workers(
        mode=mode, minutes=15, now=NOW, channel=state.channel, **kwargs
    )


# --- switches ----------------------------------------------------------------


def test_mode_switch_spellings(caplog):
    assert sw.silent_worker_mode({}) == "shadow"
    assert sw.silent_worker_mode({sw.MODE_ENV: "off"}) == "off"
    assert sw.silent_worker_mode({sw.MODE_ENV: "enforce"}) == "enforce"
    assert sw.silent_worker_mode({sw.MODE_ENV: "shadow"}) == "shadow"
    with caplog.at_level("WARNING"):
        assert sw.silent_worker_mode({sw.MODE_ENV: "ENFORCE"}) == "shadow"
        assert sw.silent_worker_mode({sw.MODE_ENV: "Off"}) == "shadow"
    assert "using shadow" in caplog.text


def test_minutes_switch_defaults():
    assert sw.silent_worker_minutes({}) == 15
    assert sw.silent_worker_minutes({sw.MINUTES_ENV: "30"}) == 30
    for bad in ("0", "-5", "soon", ""):
        assert sw.silent_worker_minutes({sw.MINUTES_ENV: bad}) == 15


def test_failure_class_is_its_own():
    assert sw.FAILURE_CLASS == "worker_no_output"


# --- classification ------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_output_after_n_minutes_is_classified_and_alerted_once(env):
    result = await _sweep(env)
    assert result.classified == frozenset({"ticket-1"})
    record = env.store.rows["ticket-1"]["mandate"][sw.MANDATE_KEY]
    assert record["version"] == sw.RECORD_VERSION
    assert record["failure_class"] == "worker_no_output"
    assert record["reason"] == "no_output"
    assert record["run_key"] == "run-1"
    assert record["provider"] == "glm"
    assert record["action"] == "recorded"
    assert len(env.banners) == 1
    assert await env.inbox.unread_count("user-1") == 1

    # A second pass over the same silent run writes nothing and alerts nothing.
    writes = len(env.store.mandate_writes)
    again = await _sweep(env)
    assert again.classified == frozenset()
    assert len(env.store.mandate_writes) == writes
    assert len(env.banners) == 1
    assert await env.inbox.unread_count("user-1") == 1


@pytest.mark.asyncio
async def test_alert_is_deduplicated_even_if_the_record_is_lost(env):
    await _sweep(env)
    # Someone cleared the mandate record; the inbox still holds the alert.
    env.store.rows["ticket-1"]["mandate"].pop(sw.MANDATE_KEY)
    await _sweep(env)
    assert len(env.banners) == 1
    assert await env.inbox.unread_count("user-1") == 1


@pytest.mark.asyncio
async def test_empty_final_answer_is_classified(env):
    env.run_status = "completed"
    env.assistant_text = "   "
    result = await _sweep(env)
    assert result.classified == frozenset({"ticket-1"})
    record = env.store.rows["ticket-1"]["mandate"][sw.MANDATE_KEY]
    assert record["reason"] == "empty_final_answer"
    assert "empty final answer" in env.banners[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "signal",
    ["assistant_text", "streamed", "committed", "report"],
)
async def test_output_present_does_nothing(env, signal):
    if signal == "assistant_text":
        env.assistant_text = "Working on the parser."
    elif signal == "streamed":
        env.streamed = True
    elif signal == "committed":
        env.committed = True
    else:
        env.report = {"state": "progress", "created_at": NOW - timedelta(minutes=5)}
    result = await _sweep(env, mode="enforce")
    assert result.classified == frozenset()
    assert env.store.mandate_writes == []
    assert env.store.switches == []
    assert env.banners == []


@pytest.mark.asyncio
async def test_a_report_of_an_earlier_attempt_is_not_output(env):
    env.report = {"state": "progress", "created_at": STARTED - timedelta(minutes=1)}
    result = await _sweep(env)
    assert result.classified == frozenset({"ticket-1"})


@pytest.mark.asyncio
async def test_an_unreadable_run_status_is_not_silence(env):
    env.run_status = None
    result = await _sweep(env)
    assert result.classified == frozenset()
    assert env.store.mandate_writes == []


# --- shadow and enforce -------------------------------------------------------


@pytest.mark.asyncio
async def test_shadow_does_not_switch(env):
    await _sweep(env, mode="shadow")
    assert env.store.switches == []
    assert env.cancelled == []
    assert env.store.rows["ticket-1"]["status"] == "running"
    assert env.store.rows["ticket-1"]["worker_provider"] == "glm"


def _shadow_lines(caplog) -> list[str]:
    return [record.message for record in caplog.records if "shadow_decision" in record.message]


@pytest.mark.asyncio
async def test_shadow_logs_the_switch_enforce_would_have_made(env, caplog):
    with caplog.at_level(logging.WARNING):
        result = await _sweep(env, mode="shadow")
    assert result.classified == frozenset({"ticket-1"})
    [line] = _shadow_lines(caplog)
    assert line.startswith("shadow_decision mode=silent_worker would_change=true")
    assert "subject=ticket-1" in line
    assert "current=record_only enforced=switch" in line
    assert "reason=switch" in line
    assert "signal=no_output" in line
    # The log did not move the line.
    assert env.store.switches == []


@pytest.mark.asyncio
async def test_shadow_logs_that_no_switch_would_happen(env, caplog):
    env.store = FakeStore(
        [_ticket(mandate={"switch_candidates": [{"provider": "zai", "model": "glm-5"}]})]
    )
    env.install()
    with caplog.at_level(logging.WARNING):
        await _sweep(env, mode="shadow")
    [line] = _shadow_lines(caplog)
    assert "would_change=false" in line
    assert "current=record_only enforced=record_only" in line
    assert "reason=no_eligible_target" in line


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mandate",
    [
        # A blank candidate: ``_candidates`` keeps it, ``plan_switch`` skips it.
        {"switch_candidates": [{"provider": "", "model": ""}]},
        # The only candidate is the line's own reviewer: enforce refuses it.
        {
            "switch_candidates": [{"provider": "kimi", "model": "k2"}],
            "reviewer": {"provider": "kimi", "model": "k2"},
        },
    ],
)
async def test_shadow_does_not_over_report_a_switch_enforce_would_refuse(env, caplog, mandate):
    """Regression: shadow asked ``_candidates`` alone and reported a switch.

    Enforce's ``plan_switch`` also filters blank entries, the current model and
    the reviewer, so shadow runs the same plan read-only instead of guessing.
    """
    env.store = FakeStore([_ticket(mandate=mandate)])
    env.install()
    with caplog.at_level(logging.WARNING):
        await _sweep(env, mode="shadow")
    [line] = _shadow_lines(caplog)
    assert "would_change=false" in line
    assert "reason=no_eligible_target" in line


@pytest.mark.asyncio
async def test_a_live_daemon_run_logs_that_enforce_would_not_switch(env, caplog):
    env.store = FakeStore(
        [
            _ticket(
                worker_daemon_id="daemon-1",
                worker_thread_id=None,
                worker_run_id=None,
                worker_lease_id="lease-1",
            )
        ]
    )
    env.install()
    env.daemon_events = [{"kind": "text", "text": "[launcher] started\n"}]
    env.daemon_lease_live = True
    with caplog.at_level(logging.WARNING):
        await _sweep(env, mode="shadow")
    [line] = _shadow_lines(caplog)
    assert "would_change=false" in line
    assert "reason=daemon_run_live" in line


@pytest.mark.asyncio
async def test_enforce_writes_no_shadow_line(env, caplog):
    with caplog.at_level(logging.WARNING):
        await _sweep(env, mode="enforce")
    assert _shadow_lines(caplog) == []


@pytest.mark.asyncio
async def test_enforce_switches_via_the_existing_path_excluding_the_silent_provider(env):
    from src.server.services.autonomy.models import EFFECT_LAUNCH_WORKER

    result = await _sweep(env, mode="enforce")
    assert result.switched == frozenset({"ticket-1"})
    [switch] = env.store.switches
    # zcode is the silent GLM provider under another spelling: skipped.
    assert switch["record"]["to"] == {"provider": "kimi", "model": "k2"}
    assert switch["record"]["from"] == {"provider": "glm", "model": "glm-5"}
    assert switch["record"]["reason"] == "worker_no_output"
    assert switch["launch_effect"] == ("user-1", EFFECT_LAUNCH_WORKER)
    assert "worker_no_output" in switch["handoff_text"]
    assert env.cancelled == [("thread-1", "run-1")]
    row = env.store.rows["ticket-1"]
    assert row["status"] == "queued"
    record = row["mandate"][sw.MANDATE_KEY]
    assert record["action"] == "switched"
    assert record["target"] == {"provider": "kimi", "model": "k2"}
    assert len(env.banners) == 1


@pytest.mark.asyncio
async def test_enforce_after_shadow_switches_the_same_run_once(env):
    await _sweep(env, mode="shadow")
    assert env.store.switches == []
    await _sweep(env, mode="enforce")
    assert len(env.store.switches) == 1
    # The alert was already sent for this run.
    assert len(env.banners) == 1


@pytest.mark.asyncio
async def test_no_eligible_target_is_recorded_and_alerted_without_crash(env):
    env.store = FakeStore(
        [_ticket(mandate={"switch_candidates": [{"provider": "zai", "model": "glm-5"}]})]
    )
    env.install()
    result = await _sweep(env, mode="enforce")
    assert result.classified == frozenset({"ticket-1"})
    assert result.switched == frozenset()
    assert env.store.switches == []
    record = env.store.rows["ticket-1"]["mandate"][sw.MANDATE_KEY]
    assert record["action"] == "no_eligible_target"
    assert len(env.banners) == 1
    assert env.store.rows["ticket-1"]["status"] == "running"


@pytest.mark.asyncio
async def test_a_daemon_line_does_not_switch_across_providers(env):
    env.store = FakeStore(
        [
            _ticket(
                worker_daemon_id="daemon-1",
                worker_thread_id=None,
                worker_run_id=None,
                worker_lease_id="lease-1",
            )
        ]
    )
    env.install()
    env.daemon_events = [{"kind": "text", "text": "[launcher] started\n"}]
    env.daemon_lease_live = False
    result = await _sweep(env, mode="enforce")
    assert result.classified == frozenset({"ticket-1"})
    assert env.store.switches == []
    record = env.store.rows["ticket-1"]["mandate"][sw.MANDATE_KEY]
    assert record["run_key"] == "lease:lease-1"
    assert record["action"] == "no_eligible_target"


@pytest.mark.asyncio
async def test_a_refused_record_write_neither_alerts_nor_switches(env):
    env.store.refuse_mandate = True
    result = await _sweep(env, mode="enforce")
    assert result.classified == frozenset()
    assert env.banners == []
    assert env.store.switches == []


# --- off and bounds ---------------------------------------------------------


@pytest.mark.asyncio
async def test_off_does_nothing(env, monkeypatch):
    monkeypatch.setenv(sw.MODE_ENV, "off")
    result = await sw.sweep_silent_workers(now=NOW, channel=env.channel)
    assert result.mode == "off"
    assert env.store.candidate_calls == 0
    assert env.store.mandate_writes == []
    assert env.banners == []


@pytest.mark.asyncio
async def test_a_closed_pool_reads_nothing(env, monkeypatch):
    monkeypatch.setattr(sw, "database_is_open", lambda: False)
    await _sweep(env, mode="enforce")
    assert env.store.candidate_calls == 0


@pytest.mark.asyncio
async def test_a_pass_is_bounded(env):
    env.store = FakeStore([_ticket(ticket_id=f"t-{i:02d}") for i in range(30)])
    env.install()
    result = await _sweep(env, max_probes=5)
    assert result.probed == 5
    assert len(result.classified) == 5
    # The next pass continues past the rows already judged.
    second = await _sweep(env, max_probes=5)
    assert len(second.classified) == 5
    assert result.classified.isdisjoint(second.classified)


@pytest.mark.asyncio
async def test_one_failing_ticket_does_not_stop_the_pass(env, monkeypatch):
    env.store = FakeStore([_ticket(ticket_id="t-bad"), _ticket(ticket_id="t-ok")])
    env.install()
    real = sw.probe_silence

    async def probe(ticket):
        if ticket["ticket_id"] == "t-bad":
            raise RuntimeError("boom")
        return await real(ticket)

    monkeypatch.setattr(sw, "probe_silence", probe)
    result = await _sweep(env)
    assert result.classified == frozenset({"t-ok"})


# --- watchdog wiring --------------------------------------------------------


def _quiet_watchdog(monkeypatch):
    monkeypatch.setattr(watchdog, "sweep_worker_idle_wakes", AsyncMock())
    monkeypatch.setattr(
        watchdog.ticket_db, "list_watchdog_candidates", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(notes, "check_stuck_tickets", AsyncMock(return_value=0))


@pytest.mark.asyncio
async def test_the_watchdog_runs_the_silent_worker_sweep(monkeypatch):
    _quiet_watchdog(monkeypatch)
    sweep = AsyncMock(return_value=sw.SilentWorkerSweep())
    monkeypatch.setattr(watchdog, "sweep_silent_workers", sweep)
    assert await watchdog.watchdog_sweep() == 0
    sweep.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_a_silent_worker_sweep_exception_is_isolated(monkeypatch):
    _quiet_watchdog(monkeypatch)
    monkeypatch.setattr(
        watchdog, "sweep_silent_workers", AsyncMock(side_effect=RuntimeError("down"))
    )
    rows = [{"ticket_id": "t-q", "status": "queued", "stall_seconds": 2400}]
    monkeypatch.setattr(
        watchdog.ticket_db, "list_watchdog_candidates", AsyncMock(return_value=rows)
    )
    monkeypatch.setattr(
        watchdog.ticket_db, "update_ticket", AsyncMock(return_value={"ticket_id": "t-q"})
    )
    monkeypatch.setattr(watchdog, "enqueue_effect", AsyncMock(return_value=True))
    assert await watchdog.watchdog_sweep() == 1


# --- shadow is neutral ------------------------------------------------------


@pytest.mark.asyncio
async def test_shadow_records_without_moving_the_revision(env):
    result = await _sweep(env, mode="shadow")
    assert result.classified == frozenset({"ticket-1"})
    row = env.store.rows["ticket-1"]
    assert row["mandate"][sw.MANDATE_KEY]["action"] == "recorded"
    assert row["revision"] == 3


@pytest.mark.asyncio
async def test_enforce_record_still_moves_the_revision(env):
    env.store = FakeStore([_ticket(mandate={})])
    env.install()
    await _sweep(env, mode="enforce")
    assert env.store.rows["ticket-1"]["revision"] > 3


# --- daemon lines: the run log, and a confirmed stop before a switch -----------


def _daemon_ticket(**overrides):
    return _ticket(
        worker_daemon_id="daemon-1",
        worker_thread_id=None,
        worker_run_id=None,
        worker_lease_id="lease-1",
        mandate={"switch_candidates": [{"provider": "glm", "model": "glm-5-air"}]},
        **overrides,
    )


@pytest.fixture
def daemon_env(env):
    env.store = FakeStore([_daemon_ticket()])
    env.install()
    return env


@pytest.mark.asyncio
async def test_a_daemon_run_with_no_log_is_not_silence(daemon_env):
    """A CLI that prints only when it ends has streamed nothing: unknown."""
    daemon_env.daemon_events = None
    result = await _sweep(daemon_env, mode="enforce")
    assert result.probed == 1
    assert result.classified == frozenset()
    assert daemon_env.store.mandate_writes == []
    daemon_env.daemon_events = []
    assert (await _sweep(daemon_env, mode="enforce")).classified == frozenset()


@pytest.mark.asyncio
async def test_a_daemon_log_of_another_lease_is_not_read_as_silence(daemon_env, monkeypatch):
    from src.server.database.autonomy import daemon_output as output_db

    daemon_env.daemon_events = [{"kind": "text", "text": "[launcher] started\n"}]
    monkeypatch.setattr(
        output_db,
        "read_session",
        AsyncMock(return_value={"epoch": 1, "lease_id": "lease-0", "next_seq": 3}),
    )
    assert (await _sweep(daemon_env)).classified == frozenset()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "events",
    [
        [{"kind": "text", "text": "Reading the parser.\n"}],
        [{"kind": "tool", "text": ""}],
        [{"kind": "reasoning", "text": "thinking"}],
        [{"kind": "text", "text": '{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}\n'}],
    ],
)
async def test_a_daemon_log_with_model_output_is_not_silent(daemon_env, events):
    daemon_env.daemon_events = events
    result = await _sweep(daemon_env, mode="enforce")
    assert result.classified == frozenset()


@pytest.mark.asyncio
async def test_a_daemon_log_with_only_launcher_lines_is_silent(daemon_env):
    daemon_env.daemon_events = [
        {"kind": "text", "text": "[launcher] started\n"},
        {"kind": "text", "text": "\n"},
    ]
    result = await _sweep(daemon_env)
    assert result.classified == frozenset({"ticket-1"})


@pytest.mark.asyncio
async def test_a_daemon_line_is_not_switched_while_its_run_may_still_write(daemon_env):
    daemon_env.daemon_events = [{"kind": "text", "text": "[launcher] started\n"}]
    daemon_env.daemon_lease_live = True
    result = await _sweep(daemon_env, mode="enforce")
    assert result.classified == frozenset({"ticket-1"})
    assert result.switched == frozenset()
    assert daemon_env.store.switches == []
    row = daemon_env.store.rows["ticket-1"]
    assert row["status"] == "running"
    record = row["mandate"][sw.MANDATE_KEY]
    assert record["action"] == "stop_unconfirmed"
    assert record["detail"] == "daemon_run_live"
    assert daemon_env.cancelled == []


@pytest.mark.asyncio
async def test_an_unreadable_daemon_lease_is_not_a_confirmed_stop(daemon_env, monkeypatch):
    from src.server.database.autonomy import daemons as daemon_db

    daemon_env.daemon_events = [{"kind": "text", "text": "[launcher] started\n"}]
    monkeypatch.setattr(
        daemon_db, "live_lease_ticket", AsyncMock(side_effect=RuntimeError("db"))
    )
    result = await _sweep(daemon_env, mode="enforce")
    assert result.switched == frozenset()
    assert daemon_env.store.switches == []
    record = daemon_env.store.rows["ticket-1"]["mandate"][sw.MANDATE_KEY]
    assert record["action"] == "stop_unconfirmed"


@pytest.mark.asyncio
async def test_a_daemon_line_goes_to_the_switch_plan_once_its_run_is_confirmed_stopped(
    daemon_env, monkeypatch
):
    daemon_env.daemon_events = [{"kind": "text", "text": "[launcher] started\n"}]
    daemon_env.daemon_lease_live = False
    real = sw.ls.plan_switch
    calls = []

    def plan(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(sw.ls, "plan_switch", plan)
    result = await _sweep(daemon_env, mode="enforce")
    assert result.classified == frozenset({"ticket-1"})
    assert len(calls) == 1
    record = daemon_env.store.rows["ticket-1"]["mandate"][sw.MANDATE_KEY]
    # Same-provider rule plus the silent-provider breaker: no target today.
    assert record["action"] == "no_eligible_target"
