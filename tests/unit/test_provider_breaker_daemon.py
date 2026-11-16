"""Daemon admission tests that keep provider health separate from API calls."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from scripts.conductor_local_daemon import (
    ApiError,
    LeaseManager,
    _claim_with_breaker,
    _task_finished_with_breaker,
    main as daemon_main,
    main_async,
    run_claimed,
)
from src.conductor_cli import provider_breaker
from src.conductor_cli.provider_breaker import ProviderCircuitBreaker
from tests.unit.conftest import assert_daemon_home_isolated

# main_async loads this device's policy and trusted keys from HOME defaults.
pytestmark = pytest.mark.usefixtures("isolated_daemon_home")


def test_daemon_defaults_never_point_at_the_real_home(tmp_path):
    assert_daemon_home_isolated(tmp_path.resolve() / "home")


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_cli_accepts_breaker_thresholds_above_concurrency_limit(monkeypatch):
    import scripts.conductor_local_daemon as daemon

    captured = {}

    def capture_run(coroutine):
        captured["args"] = coroutine.cr_frame.f_locals["args"]
        coroutine.close()
        return 0

    monkeypatch.setattr(daemon.asyncio, "run", capture_run)

    assert daemon_main(
        [
            "--provider",
            "codex",
            "--breaker-failure-threshold",
            "100",
            "--breaker-recovery-success-threshold",
            "50",
        ]
    ) == 0
    assert captured["args"].breaker_failure_threshold == 100
    assert captured["args"].breaker_recovery_success_threshold == 50


@pytest.mark.asyncio
async def test_empty_claim_releases_half_open_probe_without_marking_recovery():
    clock = FakeClock()
    breaker = ProviderCircuitBreaker(
        failure_threshold=1, reset_timeout=2, jitter=0, monotonic=clock
    )
    permit = breaker.acquire()
    assert permit is not None
    breaker.record_failure(permit)
    clock.advance(2)

    api = Mock()
    api.claim.return_value = {}
    payload, probe, _ = await _claim_with_breaker(api, breaker, "claim-id")

    assert payload == {}
    assert probe.probe
    assert breaker.status()["state"] == "half_open"
    assert breaker.status()["consecutive_failures"] == 1
    assert breaker.status()["half_open_in_flight"] is False
    assert breaker.acquire() is not None


@pytest.mark.asyncio
async def test_cancelled_claim_releases_half_open_probe():
    clock = FakeClock()
    breaker = ProviderCircuitBreaker(
        failure_threshold=1, reset_timeout=2, jitter=0, monotonic=clock
    )
    permit = breaker.acquire()
    assert permit is not None
    breaker.record_failure(permit)
    clock.advance(2)

    api = Mock()

    async def cancelled_retry(*args, **kwargs):
        raise asyncio.CancelledError

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("scripts.conductor_local_daemon._retry_call", cancelled_retry)
        with pytest.raises(asyncio.CancelledError):
            await _claim_with_breaker(api, breaker, "claim-id")

    assert breaker.status()["state"] == "half_open"
    assert breaker.status()["half_open_in_flight"] is False
    assert breaker.acquire() is not None


@pytest.mark.asyncio
async def test_reported_failed_ticket_state_is_still_provider_success():
    api = Mock()
    api.ticket_status.return_value = {"status": "running"}
    manager = LeaseManager(api)
    breaker = ProviderCircuitBreaker(failure_threshold=1, jitter=0)
    permit = breaker.acquire()
    assert permit is not None
    payload = {
        "ticket": {
            "ticket_id": "ticket-id",
            "lease_id": "lease-id",
            "lease_duration_seconds": 60,
            "worker_provider": "codex",
        },
        "prompt": "work",
    }
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text="AUTONOMY_STATE: failed\n")))

    with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter):
        await run_claimed(
            api,
            "codex",
            "/tmp",
            10,
            payload,
            manager,
            breaker=breaker,
            permit=permit,
            poll_interval=0.01,
        )

    assert breaker.status()["state"] == "closed"
    assert breaker.status()["consecutive_failures"] == 0
    assert api.report.call_args.args[2]["state"] == "failed"


@pytest.mark.asyncio
async def test_native_adapter_exception_opens_breaker_but_reports_ticket_once(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    api = Mock()
    manager = LeaseManager(api)
    breaker = ProviderCircuitBreaker(failure_threshold=1, jitter=0)
    permit = breaker.acquire()
    assert permit is not None
    payload = {
        "ticket": {
            "workspace_id": "test-workspace",
            "ticket_id": "ticket-id",
            "lease_id": "lease-id",
            "lease_duration_seconds": 60,
            "worker_provider": "codex",
        },
        "prompt": "work",
    }

    with (
        patch(
            "src.llms.native_cli.registry.get_adapter",
            side_effect=RuntimeError("adapter unavailable"),
        ) as get_adapter,
        patch(
            "src.llms.native_cli.isolation.read_workspace_policy",
            return_value={
                "root": str(workspace),
                "env": set(),
                "runtime_read_paths": (),
            },
        ),
    ):
        await run_claimed(
            api,
            "codex",
            "/tmp",
            10,
            payload,
            manager,
            breaker=breaker,
            permit=permit,
            workspace_policy=str(tmp_path / "policy.json"),
            isolation_root=str(tmp_path / "worker-state"),
        )

    assert get_adapter.call_count == 1
    assert breaker.status()["state"] == "open"
    assert api.report.call_count == 1
    assert api.report.call_args.args[2]["state"] == "failed"


@pytest.mark.asyncio
async def test_idle_heartbeat_runs_during_breaker_wait_before_next_claim():
    breaker = ProviderCircuitBreaker(
        failure_threshold=1, reset_timeout=0.35, jitter=0
    )
    permit = breaker.acquire()
    assert permit is not None
    breaker.record_failure(permit)
    api = Mock()
    call_order: list[str] = []
    api.heartbeat.side_effect = lambda *args, **kwargs: call_order.append("heartbeat")
    api.claim.side_effect = lambda *args, **kwargs: (call_order.append("claim") or {})

    await _claim_with_breaker(
        api, breaker, "claim-id", idle_heartbeat_interval=0.1
    )

    assert call_order.index("heartbeat") < call_order.index("claim")
    assert call_order.count("heartbeat") >= 1


@pytest.mark.asyncio
async def test_fatal_heartbeat_state_stops_breaker_wait_without_provider_claim():
    breaker = ProviderCircuitBreaker(failure_threshold=1, reset_timeout=30, jitter=0)
    permit = breaker.acquire()
    assert permit is not None
    breaker.record_failure(permit)
    api = Mock()
    manager = Mock(fatal_error=ApiError("unauthorized", 401, False))

    with pytest.raises(ApiError, match="unauthorized"):
        await _claim_with_breaker(api, breaker, "claim-id", manager=manager)

    api.claim.assert_not_called()
    api.heartbeat.assert_not_called()


@pytest.mark.asyncio
async def test_prestart_task_cancellation_releases_half_open_reservation():
    clock = FakeClock()
    breaker = ProviderCircuitBreaker(
        failure_threshold=1, reset_timeout=2, jitter=0, monotonic=clock
    )
    initial = breaker.acquire()
    assert initial is not None
    breaker.record_failure(initial)
    clock.advance(2)
    probe = breaker.acquire()
    assert probe is not None and probe.probe

    async def never_starts():
        raise AssertionError("cancelled before first coroutine step")

    tasks = {asyncio.create_task(never_starts())}
    task = next(iter(tasks))
    task.add_done_callback(
        lambda done: _task_finished_with_breaker(tasks, done, breaker, probe)
    )
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert breaker.status()["state"] == "half_open"
    assert breaker.status()["half_open_in_flight"] is False
    retry_probe = breaker.acquire()
    assert retry_probe is not None and retry_probe.probe


@pytest.mark.asyncio
async def test_status_persistence_oserror_does_not_poison_successful_main_run(
    monkeypatch, tmp_path
):
    import scripts.conductor_local_daemon as daemon

    api = Mock()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    api.claim.return_value = {
        "ticket": {
            "workspace_id": "test-workspace",
            "ticket_id": "ticket-id",
            "lease_id": "lease-id",
            "lease_duration_seconds": 60,
            "worker_provider": "codex",
        },
        "prompt": "work",
    }
    api.ticket_status.return_value = {"status": "running"}
    api.report.return_value = None
    monkeypatch.setattr(daemon, "supported_providers", lambda: {"codex"})
    monkeypatch.setattr(daemon, "Api", Mock(return_value=api))
    monkeypatch.setattr(
        daemon,
        "_persist_breaker_status",
        Mock(side_effect=OSError("status disk unavailable")),
    )
    breakers: list[ProviderCircuitBreaker] = []

    def make_breaker(**kwargs):
        breaker = ProviderCircuitBreaker(**kwargs)
        breakers.append(breaker)
        return breaker

    monkeypatch.setattr(provider_breaker, "ProviderCircuitBreaker", make_breaker)
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text="done")))
    args = SimpleNamespace(
        token="test-token",
        provider="codex",
        cwd=str(tmp_path),
        base_url="http://mock.invalid",
        once=True,
        max_concurrency=1,
        heartbeat_interval=10,
        timeout=60,
        interval=0.01,
        status_file="snapshot.json",
        isolation_root=str(tmp_path / "worker-state"),
        workspace_policy=str(tmp_path / "policy.json"),
        worktree_root=str(tmp_path),
    )

    with (
        patch("src.llms.native_cli.registry.get_adapter", return_value=adapter),
        patch(
            "src.llms.native_cli.isolation.read_workspace_policy",
            return_value={
                "root": str(workspace),
                "env": set(),
                "runtime_read_paths": (),
            },
        ),
    ):
        assert await main_async(args) == 0

    assert api.claim.call_count == 1
    assert adapter.run.await_count == 1
    assert api.report.call_count == 1
    assert breakers[0].status()["state"] == "closed"
    assert breakers[0].status()["consecutive_failures"] == 0


@pytest.fixture(autouse=True)
def _current_server_claims(daemon_claims_have_completion_contract):
    """Claims here stand for a server that sends the completion contract."""
