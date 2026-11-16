"""The MCP process heartbeats held leases only while the app is in use (N80 7).

Every test drives :class:`AutoHeartbeat` with a fake clock: no sleeping, no
network, no keychain. The upstream heartbeat is a recording fake.
"""

from __future__ import annotations

import httpx
import pytest

from src.collab_mcp.client import API_PREFIX, CollabClient
from src.collab_mcp.heartbeat import (
    DEFAULT_HEARTBEAT_SECONDS,
    IDLE_CUTOFF_SECONDS,
    AutoHeartbeat,
)
from src.collab_mcp.server import CollabMCP

MINUTE = 60.0


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Beats:
    def __init__(self, code: str = "ok") -> None:
        self.code = code
        self.sent: list[str] = []

    def __call__(self, lease_id: str) -> dict:
        self.sent.append(lease_id)
        return {"ok": self.code == "ok", "code": self.code, "data": {}}


def _ok(data: dict | None = None) -> dict:
    return {"ok": True, "code": "ok", "data": data or {}, "untrusted_fields": [], "redacted": 0}


def _claimed(heartbeat: AutoHeartbeat, lease_id: str = "lease-1", every: int | None = 60) -> None:
    heartbeat.note_activity()
    heartbeat.observe(
        "task_claim",
        {"ref": "t:x", "revision": 1},
        _ok({"lease_id": lease_id, "heartbeat_every_seconds": every}),
    )


def test_the_rule_constants_match_the_prd() -> None:
    assert IDLE_CUTOFF_SECONDS == 30 * 60
    assert DEFAULT_HEARTBEAT_SECONDS == 60


def test_a_claimed_lease_is_beaten_every_interval_while_active() -> None:
    clock, beats = FakeClock(), Beats()
    heartbeat = AutoHeartbeat(beats, clock=clock)
    _claimed(heartbeat)
    assert heartbeat.leases == ("lease-1",)
    assert heartbeat.tick() == []  # not due yet
    clock.advance(59)
    assert heartbeat.tick() == []
    clock.advance(1)
    assert heartbeat.tick() == ["lease-1"]
    assert heartbeat.tick() == []  # rescheduled
    clock.advance(60)
    assert heartbeat.tick() == ["lease-1"]


def test_auto_heartbeat_stops_after_thirty_idle_minutes() -> None:
    clock, beats = FakeClock(), Beats()
    heartbeat = AutoHeartbeat(beats, clock=clock)
    _claimed(heartbeat)
    # Keep ticking every minute with no tool call from the app.
    for _ in range(29):
        clock.advance(MINUTE)
        assert heartbeat.tick() == ["lease-1"]
    clock.advance(MINUTE - 1)  # 29:59 since the last call: still active
    assert not heartbeat.is_idle()
    clock.advance(1)  # 30:00: idle
    assert heartbeat.is_idle()
    sent_before = len(beats.sent)
    for _ in range(120):
        clock.advance(MINUTE)
        assert heartbeat.tick() == []
    assert len(beats.sent) == sent_before
    # The lease is still known; it is simply left to expire on the server.
    assert heartbeat.leases == ("lease-1",)


def test_the_automatic_beat_never_counts_as_activity() -> None:
    clock, beats = FakeClock(), Beats()
    heartbeat = AutoHeartbeat(beats, clock=clock)
    _claimed(heartbeat)
    for _ in range(40):
        clock.advance(MINUTE)
        heartbeat.tick()
    assert heartbeat.is_idle()
    assert len(beats.sent) == 29


def test_a_new_tool_call_resumes_the_heartbeat() -> None:
    clock, beats = FakeClock(), Beats()
    heartbeat = AutoHeartbeat(beats, clock=clock)
    _claimed(heartbeat)
    clock.advance(31 * MINUTE)
    assert heartbeat.tick() == []
    heartbeat.note_activity()
    assert heartbeat.tick() == ["lease-1"]


def test_nothing_is_sent_before_any_activity() -> None:
    clock, beats = FakeClock(), Beats()
    heartbeat = AutoHeartbeat(beats, clock=clock)
    heartbeat.track("lease-1")
    clock.advance(10 * MINUTE)
    assert heartbeat.tick() == [] and beats.sent == []


@pytest.mark.parametrize("code", ["lease_lost", "lease_expired", "not_found", "unauthorized"])
def test_a_lease_the_server_no_longer_honours_is_dropped(code) -> None:
    clock, beats = FakeClock(), Beats(code)
    heartbeat = AutoHeartbeat(beats, clock=clock)
    _claimed(heartbeat)
    clock.advance(MINUTE)
    assert heartbeat.tick() == ["lease-1"]
    assert heartbeat.leases == ()
    clock.advance(MINUTE)
    assert heartbeat.tick() == [] and beats.sent == ["lease-1"]


def test_an_unreachable_server_is_retried_on_the_next_interval() -> None:
    clock = FakeClock()
    calls: list[str] = []

    def failing(lease_id: str) -> dict:
        calls.append(lease_id)
        raise ConnectionError("down")

    heartbeat = AutoHeartbeat(failing, clock=clock)
    _claimed(heartbeat)
    clock.advance(MINUTE)
    assert heartbeat.tick() == []
    assert heartbeat.leases == ("lease-1",)
    clock.advance(MINUTE)
    heartbeat.tick()
    assert calls == ["lease-1", "lease-1"]


def test_release_and_a_reconciled_done_report_end_the_lease() -> None:
    clock, beats = FakeClock(), Beats()
    heartbeat = AutoHeartbeat(beats, clock=clock)
    _claimed(heartbeat, "lease-1")
    _claimed(heartbeat, "lease-2")
    heartbeat.observe("task_release", {"lease_id": "lease-1", "reason": "other"}, _ok({"lease_id": "lease-1", "state": "released"}))
    assert heartbeat.leases == ("lease-2",)
    heartbeat.observe(
        "task_report",
        {"lease_id": "lease-2", "report": {"status": "done"}},
        _ok({"ticket_state": "ready", "reconciled": {"vetoed": False, "reason": ""}}),
    )
    assert heartbeat.leases == ()


@pytest.mark.parametrize(
    ("status", "vetoed"),
    [("partial", False), ("needs_review", False), ("done", True)],
)
def test_other_reports_and_vetoed_done_keep_the_lease(status, vetoed) -> None:
    clock, beats = FakeClock(), Beats()
    heartbeat = AutoHeartbeat(beats, clock=clock)
    _claimed(heartbeat)
    clock.advance(50)
    heartbeat.observe(
        "task_report",
        {"lease_id": "lease-1", "report": {"status": status}},
        _ok({"ticket_state": "blocked", "reconciled": {"vetoed": vetoed, "reason": "git_not_delivered" if vetoed else ""}}),
    )
    assert heartbeat.leases == ("lease-1",)
    # The report itself counted as a beat: the next one is a full interval on.
    clock.advance(59)
    assert heartbeat.tick() == []
    clock.advance(1)
    assert heartbeat.tick() == ["lease-1"]


def test_a_refused_lease_call_drops_the_lease_and_handoff_accept_adds_one() -> None:
    clock, beats = FakeClock(), Beats()
    heartbeat = AutoHeartbeat(beats, clock=clock)
    _claimed(heartbeat)
    heartbeat.observe(
        "task_report",
        {"lease_id": "lease-1", "report": {"status": "done"}},
        {"ok": False, "code": "lease_lost", "data": {}},
    )
    assert heartbeat.leases == ()
    heartbeat.observe("handoff_accept", {"handoff_id": "h1"}, _ok({"lease_id": "lease-9", "pack": {}}))
    assert heartbeat.leases == ("lease-9",)


def test_an_implausible_interval_falls_back_to_the_default() -> None:
    clock, beats = FakeClock(), Beats()
    heartbeat = AutoHeartbeat(beats, clock=clock)
    _claimed(heartbeat, every=0)
    clock.advance(DEFAULT_HEARTBEAT_SECONDS - 1)
    assert heartbeat.tick() == []
    clock.advance(1)
    assert heartbeat.tick() == ["lease-1"]


def test_the_server_core_wires_tool_calls_into_the_idle_rule() -> None:
    """End to end through CollabMCP with a fake Conductor and a fake clock."""
    token = "tok_" + "ab" * 20
    requests: list[str] = []

    def conductor(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {token}"
        route = request.url.path[len(API_PREFIX):]
        requests.append(route)
        if route.endswith("/claim"):
            data = {
                "lease_id": "lease-1",
                "ref": "t:" + "1" * 8 + "-1111-1111-1111-" + "1" * 12,
                "expires_at": "2026-11-13T10:15:00Z",
                "heartbeat_every_seconds": 60,
                "worktree": {"path": "/w", "branch": "b", "base_sha": "abc1234"},
            }
        elif route.endswith("/heartbeat"):
            data = {"lease_id": "lease-1", "state": "active"}
        else:
            data = {"tasks": []}
        return httpx.Response(200, json=_ok(data))

    clock = FakeClock()
    client = CollabClient(token, transport=httpx.MockTransport(conductor))
    core = CollabMCP(client)
    core.heartbeat = AutoHeartbeat(core._send_heartbeat, clock=clock)

    core.call_sync("task_claim", {"ref": "t:" + "1" * 8 + "-1111-1111-1111-" + "1" * 12, "revision": 2})
    clock.advance(MINUTE)
    assert core.heartbeat.tick() == ["lease-1"]
    assert requests[-1] == "/leases/lease-1/heartbeat"

    # Activity (any tool call) keeps it going past 30 minutes...
    for _ in range(45):
        clock.advance(MINUTE)
        core.call_sync("task_list", {})
        core.heartbeat.tick()
    assert not core.heartbeat.is_idle()

    # ...and 30 quiet minutes stop it.
    beats_before = requests.count("/leases/lease-1/heartbeat")
    for _ in range(31):
        clock.advance(MINUTE)
        core.heartbeat.tick()
    quiet_beats = requests.count("/leases/lease-1/heartbeat") - beats_before
    assert core.heartbeat.is_idle()
    assert quiet_beats == 29
    for _ in range(60):
        clock.advance(MINUTE)
        core.heartbeat.tick()
    assert requests.count("/leases/lease-1/heartbeat") - beats_before == 29


@pytest.mark.asyncio
async def test_the_async_loop_sends_nothing_while_idle() -> None:
    import anyio

    clock, beats = FakeClock(), Beats()
    heartbeat = AutoHeartbeat(beats, clock=clock)
    _claimed(heartbeat)
    clock.advance(IDLE_CUTOFF_SECONDS + MINUTE)
    with anyio.move_on_after(0.2):
        await heartbeat.run(interval=0.01)
    assert beats.sent == []
    heartbeat.note_activity()
    clock.advance(MINUTE)
    with anyio.move_on_after(0.2):
        await heartbeat.run(interval=0.01)
    assert beats.sent == ["lease-1"]
