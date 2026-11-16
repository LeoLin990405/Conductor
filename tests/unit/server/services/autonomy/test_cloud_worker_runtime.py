"""The cloud Worker OS-isolation switch fails closed in every state.

The shipped Docker runtime registers a verifier and a launcher; these tests
pin the default-off refusal, malformed/unknown/unavailable runtime refusals,
the verified-but-unlaunchable refusal, the registration extension point, and
the launch effect's isolated-launch path (the runtime itself is faked here; its
command construction is covered in test_docker_worker_runtime.py).
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.server.services.autonomy import cloud_runtime, worker


def _set_switch(monkeypatch, value: str | None) -> None:
    monkeypatch.setattr(
        cloud_runtime, "get_autonomy_cloud_worker_runtime", lambda: value
    )


@pytest.fixture(autouse=True)
def _empty_runtime_registry(monkeypatch):
    # Every test starts from an empty registry; resolve() re-installs only the
    # shipped Docker runtime, so tests that want another runtime patch these.
    monkeypatch.setattr(cloud_runtime, "ISOLATED_RUNTIME_VERIFIERS", {})
    monkeypatch.setattr(cloud_runtime, "ISOLATED_RUNTIME_LAUNCHERS", {})


@pytest.fixture(autouse=True)
def _no_ambient_switch(monkeypatch):
    monkeypatch.delenv("AUTONOMY_CLOUD_WORKER_RUNTIME", raising=False)


def _cloud_ticket(**overrides):
    ticket = {
        "ticket_id": "ticket-1",
        "user_id": "user-1",
        "workspace_id": "ws-1",
        "status": "queued",
        "revision": 7,
        "worker_daemon_id": None,
        "worker_thread_id": None,
        "worker_run_id": None,
        "worker_provider": "codex",
        "worker_model": "gpt-x",
        "mandate": {},
    }
    ticket.update(overrides)
    return ticket


# ---------------------------------------------------------------------------
# Default off
# ---------------------------------------------------------------------------


def test_switch_is_off_by_default_and_refuses_with_the_legacy_error(monkeypatch):
    _set_switch(monkeypatch, None)

    isolation = cloud_runtime.resolve_cloud_worker_isolation()

    assert isolation.requested is None
    assert isolation.enabled is False
    assert isolation.verified is False
    assert isolation.launch_allowed is False
    assert cloud_runtime.cloud_worker_launch_allowed() is False
    assert (
        cloud_runtime.cloud_worker_refusal_error()
        == worker._CLOUD_WORKER_REJECTION_ERROR
    )
    assert worker._CLOUD_WORKER_REJECTION_ERROR.startswith(
        "Cloud Worker execution is disabled"
    )


@pytest.mark.parametrize("value", ["", "  ", "none", "None", "off", "disabled"])
def test_off_values_keep_the_default_refusal(monkeypatch, value):
    _set_switch(monkeypatch, value)

    isolation = cloud_runtime.resolve_cloud_worker_isolation()

    assert isolation.enabled is False
    assert isolation.requested is None
    assert cloud_runtime.cloud_worker_launch_allowed() is False
    assert (
        cloud_runtime.cloud_worker_refusal_error()
        == worker._CLOUD_WORKER_REJECTION_ERROR
    )


# ---------------------------------------------------------------------------
# Enabled but not verified
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["podman", "daytona", "e2b", "podman-alt", " gVisor "])
def test_unknown_runtime_is_refused_and_named(monkeypatch, value):
    _set_switch(monkeypatch, value)

    isolation = cloud_runtime.resolve_cloud_worker_isolation()
    error = cloud_runtime.cloud_worker_refusal_error()

    assert isolation.enabled is True
    assert isolation.verified is False
    assert isolation.launch_allowed is False
    assert isolation.requested == value.strip().lower()
    assert "not implemented by this build" in isolation.reason
    assert cloud_runtime.cloud_worker_launch_allowed() is False
    assert error != worker._CLOUD_WORKER_REJECTION_ERROR
    assert error.startswith("Cloud Worker execution is disabled: ")
    assert isolation.requested in error
    assert "Assign the Ticket to a configured local daemon." in error


@pytest.mark.parametrize(
    "value",
    [
        "../etc/passwd",
        "docker; rm -rf /",
        "$(reboot)",
        "docker && curl evil",
        "doc ker",
        "a" * 65,
        "docker/runtime",
    ],
)
def test_malformed_runtime_value_cannot_become_a_runtime(monkeypatch, value):
    _set_switch(monkeypatch, value)

    isolation = cloud_runtime.resolve_cloud_worker_isolation()
    error = cloud_runtime.cloud_worker_refusal_error()

    assert isolation.enabled is True
    assert isolation.verified is False
    assert isolation.launch_allowed is False
    assert "not a valid runtime identifier" in isolation.reason
    assert "not a valid runtime identifier" in error
    assert error.startswith("Cloud Worker execution is disabled: ")


def test_registered_runtime_reports_its_unavailable_reason(monkeypatch):
    _set_switch(monkeypatch, "docker")
    monkeypatch.setattr(
        cloud_runtime,
        "ISOLATED_RUNTIME_VERIFIERS",
        {"docker": lambda: (False, "sandbox socket missing")},
    )

    isolation = cloud_runtime.resolve_cloud_worker_isolation()
    error = cloud_runtime.cloud_worker_refusal_error()

    assert isolation.enabled is True
    assert isolation.verified is False
    assert cloud_runtime.cloud_worker_launch_allowed() is False
    assert "sandbox socket missing" in isolation.reason
    assert "sandbox socket missing" in error


def test_raising_verifier_fails_closed(monkeypatch):
    def _boom():
        raise RuntimeError("probe crashed")

    _set_switch(monkeypatch, "docker")
    monkeypatch.setattr(
        cloud_runtime, "ISOLATED_RUNTIME_VERIFIERS", {"docker": _boom}
    )

    isolation = cloud_runtime.resolve_cloud_worker_isolation()

    assert isolation.enabled is True
    assert isolation.verified is False
    assert isolation.launch_allowed is False
    assert "RuntimeError" in isolation.reason
    assert cloud_runtime.cloud_worker_launch_allowed() is False


@pytest.mark.parametrize(
    "result",
    [
        "not a pair",
        "ok",  # a 2-char string unpacks like a pair; must not count
        ("false", "x"),  # a truthy string is not a boolean
        (1, "x"),
        (True, None),
        [True, "x"],
        True,
    ],
)
def test_verifier_result_off_contract_fails_closed(monkeypatch, result):
    _set_switch(monkeypatch, "docker")
    monkeypatch.setattr(
        cloud_runtime,
        "ISOLATED_RUNTIME_VERIFIERS",
        {"docker": lambda: result},
    )

    isolation = cloud_runtime.resolve_cloud_worker_isolation()

    assert isolation.enabled is True
    assert isolation.verified is False
    assert isolation.launch_allowed is False
    assert "invalid result" in isolation.reason


def test_verifier_contract_requires_a_real_boolean(monkeypatch):
    """A false bool is a valid, negative verification, not an invalid result."""
    _set_switch(monkeypatch, "docker")
    monkeypatch.setattr(
        cloud_runtime,
        "ISOLATED_RUNTIME_VERIFIERS",
        {"docker": lambda: (False, "boundary missing")},
    )

    isolation = cloud_runtime.resolve_cloud_worker_isolation()

    assert isolation.verified is False
    assert "boundary missing" in isolation.reason
    assert "invalid result" not in isolation.reason


def test_a_verifier_without_a_launcher_cannot_launch(monkeypatch):
    _set_switch(monkeypatch, "custom")
    monkeypatch.setattr(
        cloud_runtime,
        "ISOLATED_RUNTIME_VERIFIERS",
        {"custom": lambda: (True, "boundary ready")},
    )

    isolation = cloud_runtime.resolve_cloud_worker_isolation()
    error = cloud_runtime.cloud_worker_refusal_error()

    assert isolation.enabled is True
    assert isolation.verified is True
    assert isolation.launch_wired is False
    assert isolation.launch_allowed is False
    assert cloud_runtime.cloud_worker_launch_allowed() is False
    assert "verified available" in error
    assert "no isolated cloud Worker launcher is registered" in error
    assert error.startswith("Cloud Worker execution is disabled: ")


# ---------------------------------------------------------------------------
# Extension point registration
# ---------------------------------------------------------------------------


def test_registered_runtime_is_resolved_without_a_duplicate(monkeypatch):
    _set_switch(monkeypatch, "docker")
    verifier = lambda: (False, "not ready")  # noqa: E731 - tiny local double
    cloud_runtime.register_cloud_worker_runtime("Docker", verifier)

    assert cloud_runtime.ISOLATED_RUNTIME_VERIFIERS["docker"] is verifier
    assert cloud_runtime.resolve_cloud_worker_isolation().enabled is True


@pytest.mark.parametrize(
    "runtime_id", ["", "  ", "none", "off", "disabled", "../x", "with space"]
)
def test_register_rejects_off_and_malformed_ids(runtime_id):
    with pytest.raises(ValueError):
        cloud_runtime.register_cloud_worker_runtime(runtime_id, lambda: (True, ""))


def test_register_rejects_duplicates_and_non_callables():
    cloud_runtime.register_cloud_worker_runtime("docker", lambda: (False, ""))
    with pytest.raises(ValueError):
        cloud_runtime.register_cloud_worker_runtime("docker", lambda: (True, ""))
    with pytest.raises(TypeError):
        cloud_runtime.register_cloud_worker_runtime("gvisor", "not a callable")


# ---------------------------------------------------------------------------
# The refusal paths keep failing closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_launch_effect_refuses_with_the_switch_diagnostic(monkeypatch):
    _set_switch(monkeypatch, "podman")
    ticket = _cloud_ticket()
    reject = AsyncMock(return_value={"ticket_id": "ticket-1"})
    monkeypatch.setattr(
        worker.ticket_db, "get_ticket", AsyncMock(return_value=ticket)
    )
    monkeypatch.setattr(worker.ticket_db, "fail_unisolated_cloud_worker", reject)
    dispatcher = AsyncMock()
    monkeypatch.setattr(worker, "ProjectDispatcher", dispatcher)

    await worker.launch_worker_effect(
        SimpleNamespace(payload={"ticket_id": "ticket-1"})
    )

    reject.assert_awaited_once()
    assert "not implemented by this build" in reject.await_args.kwargs["error"]
    dispatcher.assert_not_called()


@pytest.mark.asyncio
async def test_launch_effect_hands_a_verified_launch_to_the_isolated_path(
    monkeypatch,
):
    _set_switch(monkeypatch, "custom")
    monkeypatch.setattr(
        cloud_runtime,
        "ISOLATED_RUNTIME_VERIFIERS",
        {"custom": lambda: (True, "boundary ready")},
    )

    async def _launcher(ticket, prompt):
        return {"state": "ready", "summary": "done", "question": ""}

    monkeypatch.setattr(
        cloud_runtime, "ISOLATED_RUNTIME_LAUNCHERS", {"custom": _launcher}
    )
    ticket = _cloud_ticket()
    monkeypatch.setattr(
        worker.ticket_db, "get_ticket", AsyncMock(return_value=ticket)
    )
    monkeypatch.setattr(worker, "_worker_slot_available", AsyncMock(return_value=True))
    reject = AsyncMock()
    monkeypatch.setattr(worker.ticket_db, "fail_unisolated_cloud_worker", reject)
    isolated = AsyncMock()
    monkeypatch.setattr(worker, "_launch_isolated_cloud_worker", isolated)
    dispatcher = AsyncMock()
    monkeypatch.setattr(worker, "ProjectDispatcher", dispatcher)

    await worker.launch_worker_effect(
        SimpleNamespace(payload={"ticket_id": "ticket-1"}, effect_id="e1", attempts=0)
    )

    isolated.assert_awaited_once()
    assert isolated.await_args.args[0] is ticket
    reject.assert_not_awaited()
    dispatcher.assert_not_called()


@pytest.mark.asyncio
async def test_launch_effect_settles_an_orphaned_isolated_run_closed(monkeypatch):
    effect = SimpleNamespace(payload={"ticket_id": "ticket-1"}, effect_id="e1", attempts=0)
    _set_switch(monkeypatch, "podman")  # recovery must not need the runtime
    ticket = _cloud_ticket(
        status="running",
        worker_thread_id="thread-1",
        worker_run_id=worker._isolated_run_id("ticket-1", effect),
    )
    monkeypatch.setattr(
        worker.ticket_db, "get_ticket", AsyncMock(return_value=ticket)
    )
    monkeypatch.setattr(worker, "_claim_effect_lease", AsyncMock(return_value=True))
    failed = AsyncMock()
    monkeypatch.setattr(worker, "_fail_isolated_launch", failed)
    isolated = AsyncMock()
    monkeypatch.setattr(worker, "_launch_isolated_cloud_worker", isolated)

    await worker.launch_worker_effect(effect)

    isolated.assert_not_awaited()
    failed.assert_awaited_once()
    assert failed.await_args.kwargs["thread_id"] == "thread-1"


@pytest.mark.asyncio
async def test_stale_effect_does_not_settle_an_orphan_it_no_longer_owns(monkeypatch):
    effect = SimpleNamespace(payload={"ticket_id": "ticket-1"}, effect_id="e1", attempts=0)
    _set_switch(monkeypatch, "podman")
    ticket = _cloud_ticket(
        status="running",
        worker_thread_id="thread-1",
        worker_run_id=worker._isolated_run_id("ticket-1", effect),
    )
    monkeypatch.setattr(
        worker.ticket_db, "get_ticket", AsyncMock(return_value=ticket)
    )
    monkeypatch.setattr(worker, "_claim_effect_lease", AsyncMock(return_value=False))
    failed = AsyncMock()
    monkeypatch.setattr(worker, "_fail_isolated_launch", failed)

    await worker.launch_worker_effect(effect)

    failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_launch_effect_leaves_another_effects_bound_run_alone(monkeypatch):
    effect = SimpleNamespace(payload={"ticket_id": "ticket-1"}, effect_id="e1", attempts=0)
    _set_switch(monkeypatch, "podman")
    ticket = _cloud_ticket(
        status="running", worker_thread_id="thread-1", worker_run_id="run-of-another-effect"
    )
    monkeypatch.setattr(
        worker.ticket_db, "get_ticket", AsyncMock(return_value=ticket)
    )
    failed = AsyncMock()
    monkeypatch.setattr(worker, "_fail_isolated_launch", failed)

    await worker.launch_worker_effect(effect)

    failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_reply_refusal_returns_before_retrying_when_the_runtime_is_ready(
    monkeypatch,
):
    _set_switch(monkeypatch, "custom")
    monkeypatch.setattr(
        cloud_runtime,
        "ISOLATED_RUNTIME_VERIFIERS",
        {"custom": lambda: (True, "boundary ready")},
    )

    async def _launcher(ticket, prompt):
        return {"state": "ready", "summary": "", "question": ""}

    monkeypatch.setattr(
        cloud_runtime, "ISOLATED_RUNTIME_LAUNCHERS", {"custom": _launcher}
    )
    ticket = _cloud_ticket(
        status="running", worker_thread_id="thread-1", worker_run_id=None
    )
    get_ticket = AsyncMock(return_value=ticket)
    monkeypatch.setattr(worker.ticket_db, "get_ticket", get_ticket)

    outcome = await worker.refuse_unisolated_cloud_dispatch_retrying(ticket)

    assert outcome == (ticket, False)
    get_ticket.assert_not_awaited()


@pytest.mark.asyncio
async def test_launch_effect_leaves_an_orphan_alone_when_the_runtime_is_off(
    monkeypatch,
):
    _set_switch(monkeypatch, "podman")
    ticket = _cloud_ticket(
        status="running", worker_thread_id="thread-1", worker_run_id="run-1"
    )
    monkeypatch.setattr(
        worker.ticket_db, "get_ticket", AsyncMock(return_value=ticket)
    )
    failed = AsyncMock()
    monkeypatch.setattr(worker, "_fail_isolated_launch", failed)
    isolated = AsyncMock()
    monkeypatch.setattr(worker, "_launch_isolated_cloud_worker", isolated)

    await worker.launch_worker_effect(
        SimpleNamespace(payload={"ticket_id": "ticket-1"}, effect_id="e1", attempts=0)
    )

    failed.assert_not_awaited()
    isolated.assert_not_awaited()


@pytest.mark.asyncio
async def test_refusal_fence_uses_the_switch_diagnostic(monkeypatch):
    _set_switch(monkeypatch, "podman")
    reject = AsyncMock(return_value={"ticket_id": "ticket-1", "status": "failed"})
    monkeypatch.setattr(worker.ticket_db, "fail_unisolated_cloud_worker", reject)

    failed = await worker.refuse_unisolated_cloud_dispatch(_cloud_ticket())

    assert failed is not None
    error = reject.await_args.kwargs["error"]
    assert "not implemented by this build" in error
    assert error.startswith("Cloud Worker execution is disabled: ")


@pytest.mark.asyncio
async def test_refusal_fence_steps_aside_for_a_wired_runtime(monkeypatch):
    """A registered launcher (verify + launch pair) is the allowed case."""
    _set_switch(monkeypatch, "custom")
    monkeypatch.setattr(
        cloud_runtime,
        "ISOLATED_RUNTIME_VERIFIERS",
        {"custom": lambda: (True, "ready")},
    )

    async def _launcher(ticket, prompt):
        return {"state": "ready", "summary": "", "question": ""}

    monkeypatch.setattr(
        cloud_runtime, "ISOLATED_RUNTIME_LAUNCHERS", {"custom": _launcher}
    )
    reject = AsyncMock()
    monkeypatch.setattr(worker.ticket_db, "fail_unisolated_cloud_worker", reject)

    failed = await worker.refuse_unisolated_cloud_dispatch(_cloud_ticket())

    assert failed is None
    reject.assert_not_awaited()


# ---------------------------------------------------------------------------
# The isolated launch helper itself
# ---------------------------------------------------------------------------


def _prepared_ticket(**overrides):
    ticket = _cloud_ticket(**overrides)
    return ticket


def _patch_launch_plumbing(monkeypatch):
    monkeypatch.setattr(worker, "_claim_effect_lease", AsyncMock(return_value=True))
    monkeypatch.setattr(
        worker,
        "_mark_isolated_container_started",
        AsyncMock(return_value={"ticket_id": "ticket-1"}),
    )


async def _fake_guarded(coro, **_kwargs):
    coro.close()
    return {"state": "ready", "summary": "ok", "question": ""}


@pytest.mark.asyncio
async def test_isolated_launch_binds_a_run_id_and_settles_the_report(monkeypatch):
    ticket = _prepared_ticket()
    bound = _prepared_ticket(worker_thread_id="thread-1", worker_run_id="run-1")
    monkeypatch.setattr(
        worker, "_prepare_isolated_launch", AsyncMock(return_value=("prompt", "", [], "thread-1", bound))
    )
    _patch_launch_plumbing(monkeypatch)
    monkeypatch.setattr(worker, "_run_isolated_launch_guarded", _fake_guarded)
    applied = AsyncMock()
    monkeypatch.setattr(worker, "_apply_isolated_worker_report", applied)
    failed = AsyncMock()
    monkeypatch.setattr(worker, "_fail_isolated_launch", failed)

    await worker._launch_isolated_cloud_worker(
        ticket, "ticket-1", SimpleNamespace(effect_id="e1", attempts=0)
    )

    applied.assert_awaited_once()
    assert applied.await_args.kwargs["state"] == "ready"
    failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_isolated_launch_settles_closed_when_the_runtime_refuses(monkeypatch):
    ticket = _prepared_ticket()
    bound = _prepared_ticket(worker_thread_id="thread-1", worker_run_id="run-1")
    monkeypatch.setattr(
        worker, "_prepare_isolated_launch", AsyncMock(return_value=("prompt", "", [], "thread-1", bound))
    )

    async def _refuse(coro, **_kwargs):
        coro.close()
        raise cloud_runtime.CloudWorkerLaunchError("runtime exploded")

    _patch_launch_plumbing(monkeypatch)
    monkeypatch.setattr(worker, "_run_isolated_launch_guarded", _refuse)
    failed = AsyncMock()
    monkeypatch.setattr(worker, "_fail_isolated_launch", failed)
    applied = AsyncMock()
    monkeypatch.setattr(worker, "_apply_isolated_worker_report", applied)

    await worker._launch_isolated_cloud_worker(
        ticket, "ticket-1", SimpleNamespace(effect_id="e1", attempts=0)
    )

    failed.assert_awaited_once()
    assert "runtime exploded" in failed.await_args.kwargs["error"]
    applied.assert_not_awaited()


@pytest.mark.asyncio
async def test_isolated_launch_report_persistence_failure_is_retried(monkeypatch):
    """A DB failure must not be acknowledged: the effect retry settles it."""
    ticket = _prepared_ticket()
    bound = _prepared_ticket(worker_thread_id="thread-1", worker_run_id="run-1")
    monkeypatch.setattr(
        worker, "_prepare_isolated_launch", AsyncMock(return_value=("prompt", "", [], "thread-1", bound))
    )
    _patch_launch_plumbing(monkeypatch)
    monkeypatch.setattr(worker, "_run_isolated_launch_guarded", _fake_guarded)

    async def _db_down(**_kwargs):
        raise RuntimeError("report database down")

    monkeypatch.setattr(worker, "_apply_isolated_worker_report", _db_down)
    failed = AsyncMock()
    monkeypatch.setattr(worker, "_fail_isolated_launch", failed)

    with pytest.raises(RuntimeError, match="database down"):
        await worker._launch_isolated_cloud_worker(
            ticket, "ticket-1", SimpleNamespace(effect_id="e1", attempts=0)
        )

    failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_isolated_launch_guard_cancels_the_run_when_the_lease_is_lost(
    monkeypatch,
):
    async def _never_finishes(_ticket, _prompt):
        await asyncio.sleep(3600)

    lease_lost = asyncio.Event()
    lease_lost.set()

    with pytest.raises(worker._EffectLeaseLost, match="lease was lost"):
        await worker._run_isolated_launch_guarded(
            _never_finishes(_cloud_ticket(), "prompt"),
            lease_lost=lease_lost,
            ownership_uncertain=asyncio.Event(),
            ticket_stopped=asyncio.Event(),
        )


@pytest.mark.asyncio
async def test_isolated_worker_report_ignores_stale_and_ended_lines(monkeypatch):
    from src.server.services.autonomy import host

    async def _stale(**_kwargs):
        raise host.StaleWorkerReport("ticket-1")

    monkeypatch.setattr(host, "apply_worker_report", _stale)
    await worker._apply_isolated_worker_report(
        ticket_id="ticket-1", thread_id="thread-1", state="ready", summary="ok"
    )

    async def _ended(**_kwargs):
        raise ValueError("ticket already ended")

    monkeypatch.setattr(host, "apply_worker_report", _ended)
    await worker._apply_isolated_worker_report(
        ticket_id="ticket-1", thread_id="thread-1", state="ready", summary="ok"
    )

    async def _db_down(**_kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(host, "apply_worker_report", _db_down)
    with pytest.raises(RuntimeError, match="db down"):
        await worker._apply_isolated_worker_report(
            ticket_id="ticket-1", thread_id="thread-1", state="ready", summary="ok"
        )

# ---------------------------------------------------------------------------
# Publication, cancellation and liveness plumbing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thread_publication_binds_the_run_id_in_the_same_write(monkeypatch):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lock(_ticket_id):
        yield

    update = AsyncMock(return_value=_cloud_ticket(status="running"))
    monkeypatch.setattr(worker, "_instruction_handoff_lock", _lock)
    monkeypatch.setattr(worker.ticket_db, "update_ticket", update)
    monkeypatch.setattr(worker, "_pending_instruction_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr(worker, "_enqueue_instruction_deliveries", AsyncMock())
    mandate = {"cloud_worker": {"isolated": True, "run_id": "run-1"}}

    await worker._publish_thread_and_queue_late_instructions(
        _cloud_ticket(), "ticket-1", "thread-1", set(),
        run_id="run-1", mandate=mandate,
    )

    kwargs = update.await_args.kwargs
    assert kwargs["worker_thread_id"] == "thread-1"
    assert kwargs["worker_run_id"] == "run-1"
    assert kwargs["mandate"] == mandate
    assert kwargs["status"] == "running"
    # A stale preparation snapshot (e.g. a queued switch that staged a new
    # handoff) must lose this CAS instead of overwriting newer fields.
    assert kwargs["expected_revision"] == 7


@pytest.mark.asyncio
async def test_stale_publication_snapshot_loses_the_revision_fence(monkeypatch):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lock(_ticket_id):
        yield

    update = AsyncMock(return_value=None)
    monkeypatch.setattr(worker, "_instruction_handoff_lock", _lock)
    monkeypatch.setattr(worker.ticket_db, "update_ticket", update)

    published = await worker._publish_thread_and_queue_late_instructions(
        _cloud_ticket(revision=3), "ticket-1", "thread-1", set()
    )

    assert published is None
    assert update.await_args.kwargs["expected_revision"] == 3


@pytest.mark.asyncio
async def test_guard_cancels_the_container_launch_on_parent_cancellation(monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _keep_lease(_effect, _lease_lost, _uncertain):
        await asyncio.sleep(3600)

    async def _slow_launch(_ticket, _prompt):
        started.set()
        try:
            await asyncio.sleep(3600)
        finally:
            cancelled.set()

    monkeypatch.setattr(worker, "_extend_effect_lease", _keep_lease)
    task = asyncio.create_task(
        worker._run_isolated_launch_guarded(
            _slow_launch({}, "prompt"),
            lease_lost=asyncio.Event(),
            ownership_uncertain=asyncio.Event(),
            ticket_stopped=asyncio.Event(),
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_isolated_run_heartbeat_updates_the_mandate(monkeypatch):
    ticket = _cloud_ticket(
        status="running", worker_thread_id="thread-1", worker_run_id="run-1",
        mandate={"cloud_worker": {"isolated": True, "heartbeat_at": "old"}},
    )
    monkeypatch.setattr(worker.ticket_db, "get_ticket", AsyncMock(return_value=ticket))
    touch = AsyncMock(return_value=True)
    update = AsyncMock(return_value=ticket)
    monkeypatch.setattr(worker.ticket_db, "touch_isolated_worker_heartbeat", touch)
    monkeypatch.setattr(worker.ticket_db, "update_ticket", update)
    stop = asyncio.Event()
    ticket_stopped = asyncio.Event()

    task = asyncio.create_task(
        worker._isolated_run_heartbeat(
            ticket_id="ticket-1",
            thread_id="thread-1",
            run_id="run-1",
            stop=stop,
            ticket_stopped=ticket_stopped,
            ownership_uncertain=asyncio.Event(),
            poll_seconds=0.01,
            heartbeat_seconds=0.01,
        )
    )
    await asyncio.sleep(0.05)
    stop.set()
    await task
    assert not ticket_stopped.is_set()

    touch.assert_awaited()
    kwargs = touch.await_args.kwargs
    assert kwargs["thread_id"] == "thread-1"
    assert kwargs["run_id"] == "run-1"
    assert kwargs["heartbeat_at"] != "old"
    # A stale heartbeat must not replace the full mandate: doing so could
    # write container_started back to False.
    update.assert_not_awaited()

@pytest.mark.asyncio
async def test_isolated_launch_does_not_start_without_a_claimable_lease(monkeypatch):
    ticket = _prepared_ticket()
    monkeypatch.setattr(worker, "_claim_effect_lease", AsyncMock(return_value=False))
    prepare = AsyncMock()
    monkeypatch.setattr(worker, "_prepare_isolated_launch", prepare)
    guarded = AsyncMock()
    monkeypatch.setattr(worker, "_run_isolated_launch_guarded", guarded)

    await worker._launch_isolated_cloud_worker(
        ticket, "ticket-1", SimpleNamespace(effect_id="e1", attempts=0)
    )

    prepare.assert_not_awaited()
    guarded.assert_not_awaited()


@pytest.mark.asyncio
async def test_isolated_launch_does_not_start_after_losing_the_lease_in_preparation(
    monkeypatch,
):
    ticket = _prepared_ticket()
    bound = _prepared_ticket(worker_thread_id="thread-1", worker_run_id="run-1")
    async def _claim(_effect):
        await asyncio.sleep(0)
        return True

    monkeypatch.setattr(worker, "_claim_effect_lease", _claim)

    async def _lose(_effect, lease_lost, _uncertain):
        lease_lost.set()

    monkeypatch.setattr(worker, "_extend_effect_lease", _lose)
    monkeypatch.setattr(
        worker,
        "_prepare_isolated_launch",
        AsyncMock(return_value=("prompt", "", [], "thread-1", bound)),
    )
    guarded = AsyncMock()
    monkeypatch.setattr(worker, "_run_isolated_launch_guarded", guarded)
    failed = AsyncMock()
    monkeypatch.setattr(worker, "_fail_isolated_launch", failed)
    applied = AsyncMock()
    monkeypatch.setattr(worker, "_apply_isolated_worker_report", applied)

    await worker._launch_isolated_cloud_worker(
        ticket, "ticket-1", SimpleNamespace(effect_id="e1", attempts=0)
    )

    guarded.assert_not_awaited()
    failed.assert_not_awaited()
    applied.assert_not_awaited()


@pytest.mark.asyncio
async def test_guard_returns_stopped_and_cancels_the_container(monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()
    ticket_stopped = asyncio.Event()

    async def _slow_launch(_ticket, _prompt):
        started.set()
        try:
            await asyncio.sleep(3600)
        finally:
            cancelled.set()

    task = asyncio.create_task(
        worker._run_isolated_launch_guarded(
            _slow_launch({}, "prompt"),
            lease_lost=asyncio.Event(),
            ownership_uncertain=asyncio.Event(),
            ticket_stopped=ticket_stopped,
        )
    )
    await started.wait()
    ticket_stopped.set()

    assert await task is None
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_stopped_run_is_not_settled_by_the_launcher(monkeypatch):
    ticket = _prepared_ticket()
    bound = _prepared_ticket(worker_thread_id="thread-1", worker_run_id="run-1")
    monkeypatch.setattr(worker, "_claim_effect_lease", AsyncMock(return_value=True))
    monkeypatch.setattr(
        worker,
        "_mark_isolated_container_started",
        AsyncMock(return_value={"ticket_id": "ticket-1"}),
    )
    monkeypatch.setattr(
        worker,
        "_prepare_isolated_launch",
        AsyncMock(return_value=("prompt", "", [], "thread-1", bound)),
    )
    async def _stopped(coro, **_kwargs):
        coro.close()
        return None

    monkeypatch.setattr(worker, "_run_isolated_launch_guarded", _stopped)
    applied = AsyncMock()
    monkeypatch.setattr(worker, "_apply_isolated_worker_report", applied)
    failed = AsyncMock()
    monkeypatch.setattr(worker, "_fail_isolated_launch", failed)

    await worker._launch_isolated_cloud_worker(
        ticket, "ticket-1", SimpleNamespace(effect_id="e1", attempts=0)
    )

    applied.assert_not_awaited()
    failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_heartbeat_notices_a_stopped_ticket(monkeypatch):
    stopped_ticket = _cloud_ticket(
        status="failed", worker_thread_id="thread-1", worker_run_id="run-1"
    )
    monkeypatch.setattr(
        worker.ticket_db, "get_ticket", AsyncMock(return_value=stopped_ticket)
    )
    ticket_stopped = asyncio.Event()

    await worker._isolated_run_heartbeat(
        ticket_id="ticket-1",
        thread_id="thread-1",
        run_id="run-1",
        stop=asyncio.Event(),
        ticket_stopped=ticket_stopped,
        ownership_uncertain=asyncio.Event(),
        poll_seconds=0.01,
        heartbeat_seconds=0.01,
    )

    assert ticket_stopped.is_set()

@pytest.mark.asyncio
async def test_heartbeat_read_failures_cancel_the_run_after_the_bound(monkeypatch):
    monkeypatch.setattr(
        worker.ticket_db,
        "get_ticket",
        AsyncMock(side_effect=RuntimeError("db down")),
    )
    ticket_stopped = asyncio.Event()
    ownership_uncertain = asyncio.Event()

    await worker._isolated_run_heartbeat(
        ticket_id="ticket-1",
        thread_id="thread-1",
        run_id="run-1",
        stop=asyncio.Event(),
        ticket_stopped=ticket_stopped,
        ownership_uncertain=ownership_uncertain,
        poll_seconds=0.001,
        heartbeat_seconds=0.001,
        max_read_failures=2,
    )

    assert ownership_uncertain.is_set()
    assert not ticket_stopped.is_set()


@pytest.mark.asyncio
async def test_guard_raises_uncertain_when_ownership_is_unknown(monkeypatch):
    async def _never_finishes(_ticket, _prompt):
        await asyncio.sleep(3600)

    ownership_uncertain = asyncio.Event()
    ownership_uncertain.set()

    with pytest.raises(worker._EffectLeaseUncertain):
        await worker._run_isolated_launch_guarded(
            _never_finishes({}, "prompt"),
            lease_lost=asyncio.Event(),
            ownership_uncertain=ownership_uncertain,
            ticket_stopped=asyncio.Event(),
        )


@pytest.mark.asyncio
async def test_launch_rethrows_uncertain_ownership_so_the_effect_retries(
    monkeypatch,
):
    ticket = _prepared_ticket()
    monkeypatch.setattr(worker, "_claim_effect_lease", AsyncMock(return_value=True))
    monkeypatch.setattr(
        worker,
        "_mark_isolated_container_started",
        AsyncMock(return_value={"ticket_id": "ticket-1"}),
    )
    monkeypatch.setattr(
        worker,
        "_prepare_isolated_launch",
        AsyncMock(return_value=("prompt", "", [], "thread-1", _prepared_ticket())),
    )

    async def _uncertain(coro, **_kwargs):
        coro.close()
        raise worker._EffectLeaseUncertain("ownership unknown")

    monkeypatch.setattr(worker, "_run_isolated_launch_guarded", _uncertain)
    applied = AsyncMock()
    monkeypatch.setattr(worker, "_apply_isolated_worker_report", applied)

    with pytest.raises(worker._EffectLeaseUncertain):
        await worker._launch_isolated_cloud_worker(
            ticket, "ticket-1", SimpleNamespace(effect_id="e1", attempts=0)
        )

    applied.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_queued_line_binds_the_old_thread(monkeypatch):
    ticket = _prepared_ticket(worker_thread_id="thread-1", worker_run_id="old-run")
    queued = _cloud_ticket(
        status="queued", worker_thread_id="thread-1", worker_run_id="old-run",
        revision=9,
    )
    monkeypatch.setattr(
        worker, "_cloud_worker_prompt", AsyncMock(return_value=("prompt", ""))
    )
    monkeypatch.setattr(
        worker,
        "_prompt_with_pending_instructions",
        AsyncMock(return_value=("prompt", [])),
    )
    monkeypatch.setattr(worker, "_task_stopped", lambda _ticket: False)
    monkeypatch.setattr(
        worker.ticket_db, "get_ticket", AsyncMock(return_value=queued)
    )
    update = AsyncMock(return_value={**queued, "status": "running"})
    monkeypatch.setattr(worker.ticket_db, "update_ticket", update)

    prepared = await worker._prepare_isolated_launch(
        ticket, "ticket-1", "run-1"
    )

    assert prepared is not None
    kwargs = update.await_args.kwargs
    assert kwargs["expected_status"] == "queued"
    assert kwargs["status"] == "running"
    assert kwargs["worker_thread_id"] == "thread-1"
    assert kwargs["worker_run_id"] == "run-1"

@pytest.mark.asyncio
async def test_preparation_lease_uncertainty_stays_retryable(monkeypatch):
    ticket = _prepared_ticket()
    bound = _prepared_ticket(worker_thread_id="thread-1", worker_run_id="run-1")

    async def _claim(_effect):
        await asyncio.sleep(0)
        return True

    monkeypatch.setattr(worker, "_claim_effect_lease", _claim)

    async def _uncertain(_effect, lease_lost, ownership_uncertain):
        ownership_uncertain.set()
        lease_lost.set()

    monkeypatch.setattr(worker, "_extend_effect_lease", _uncertain)
    monkeypatch.setattr(
        worker,
        "_prepare_isolated_launch",
        AsyncMock(return_value=("prompt", "", [], "thread-1", bound)),
    )
    guarded = AsyncMock()
    monkeypatch.setattr(worker, "_run_isolated_launch_guarded", guarded)

    with pytest.raises(worker._EffectLeaseUncertain):
        await worker._launch_isolated_cloud_worker(
            ticket, "ticket-1", SimpleNamespace(effect_id="e1", attempts=0)
        )

    guarded.assert_not_awaited()


def _running_binding_ticket(**overrides):
    return _cloud_ticket(
        status="running",
        worker_thread_id="thread-1",
        worker_run_id=overrides.pop("worker_run_id", None),
        revision=overrides.pop("revision", 9),
        **overrides,
    )


@pytest.mark.asyncio
async def test_binding_a_running_line_owned_by_another_run_is_a_noop(monkeypatch):
    ticket = _prepared_ticket(worker_thread_id="thread-1", worker_run_id="other-run")
    monkeypatch.setattr(
        worker, "_cloud_worker_prompt", AsyncMock(return_value=("prompt", ""))
    )
    monkeypatch.setattr(
        worker, "_prompt_with_pending_instructions", AsyncMock(return_value=("prompt", []))
    )
    monkeypatch.setattr(worker, "_task_stopped", lambda _ticket: False)
    monkeypatch.setattr(
        worker.ticket_db,
        "get_ticket",
        AsyncMock(return_value=_running_binding_ticket(worker_run_id="other-run")),
    )
    update = AsyncMock()
    monkeypatch.setattr(worker.ticket_db, "update_ticket", update)

    prepared = await worker._prepare_isolated_launch(ticket, "ticket-1", "run-1")

    assert prepared is None
    update.assert_not_awaited()


@pytest.mark.asyncio
async def test_binding_the_same_run_id_resumes_the_attempt(monkeypatch):
    ticket = _prepared_ticket(worker_thread_id="thread-1")
    current = _running_binding_ticket(worker_run_id="run-1", revision=10)
    monkeypatch.setattr(
        worker, "_cloud_worker_prompt", AsyncMock(return_value=("prompt", ""))
    )
    monkeypatch.setattr(
        worker, "_prompt_with_pending_instructions", AsyncMock(return_value=("prompt", []))
    )
    monkeypatch.setattr(worker, "_task_stopped", lambda _ticket: False)
    monkeypatch.setattr(worker.ticket_db, "get_ticket", AsyncMock(return_value=current))
    update = AsyncMock()
    monkeypatch.setattr(worker.ticket_db, "update_ticket", update)

    prepared = await worker._prepare_isolated_launch(ticket, "ticket-1", "run-1")

    assert prepared is not None
    assert prepared[4] is current
    update.assert_not_awaited()


@pytest.mark.asyncio
async def test_still_queued_cas_miss_stays_retryable(monkeypatch):
    """A reply can bump the revision during thread creation; retry, not ACK."""
    ticket = _prepared_ticket(worker_thread_id="thread-1")
    queued = _cloud_ticket(status="queued", worker_thread_id="thread-1", revision=9)
    monkeypatch.setattr(
        worker, "_cloud_worker_prompt", AsyncMock(return_value=("prompt", ""))
    )
    monkeypatch.setattr(
        worker, "_prompt_with_pending_instructions", AsyncMock(return_value=("prompt", []))
    )
    monkeypatch.setattr(worker, "_task_stopped", lambda _ticket: False)
    monkeypatch.setattr(worker.ticket_db, "get_ticket", AsyncMock(return_value=queued))
    monkeypatch.setattr(
        worker.ticket_db, "update_ticket", AsyncMock(return_value=None)
    )

    with pytest.raises(worker._RetryableLaunch):
        await worker._prepare_isolated_launch(ticket, "ticket-1", "run-1")


@pytest.mark.asyncio
async def test_queued_cas_miss_after_another_attempt_bound_is_a_noop(monkeypatch):
    ticket = _prepared_ticket(worker_thread_id="thread-1")
    queued = _cloud_ticket(status="queued", worker_thread_id="thread-1", revision=9)
    running = _cloud_ticket(
        status="running", worker_thread_id="thread-1", revision=10,
        worker_run_id="another-run",
    )
    monkeypatch.setattr(
        worker, "_cloud_worker_prompt", AsyncMock(return_value=("prompt", ""))
    )
    monkeypatch.setattr(
        worker, "_prompt_with_pending_instructions", AsyncMock(return_value=("prompt", []))
    )
    monkeypatch.setattr(worker, "_task_stopped", lambda _ticket: False)
    monkeypatch.setattr(
        worker.ticket_db, "get_ticket", AsyncMock(side_effect=[queued, running])
    )
    monkeypatch.setattr(
        worker.ticket_db, "update_ticket", AsyncMock(return_value=None)
    )

    assert await worker._prepare_isolated_launch(ticket, "ticket-1", "run-1") is None

@pytest.mark.asyncio
async def test_revision_zero_is_preserved_in_the_binding_fence(monkeypatch):
    ticket = _prepared_ticket(worker_thread_id="thread-1", revision=0)
    queued = _cloud_ticket(
        status="queued", worker_thread_id="thread-1", revision=0
    )
    monkeypatch.setattr(
        worker, "_cloud_worker_prompt", AsyncMock(return_value=("prompt", ""))
    )
    monkeypatch.setattr(
        worker, "_prompt_with_pending_instructions", AsyncMock(return_value=("prompt", []))
    )
    monkeypatch.setattr(worker, "_task_stopped", lambda _ticket: False)
    monkeypatch.setattr(worker.ticket_db, "get_ticket", AsyncMock(return_value=queued))
    update = AsyncMock(return_value={**queued, "status": "running"})
    monkeypatch.setattr(worker.ticket_db, "update_ticket", update)

    prepared = await worker._prepare_isolated_launch(ticket, "ticket-1", "run-1")

    assert prepared is not None
    assert update.await_args.kwargs["expected_revision"] == 0


@pytest.mark.asyncio
async def test_preparation_exception_with_uncertain_ownership_stays_retryable(
    monkeypatch,
):
    ticket = _prepared_ticket()

    async def _claim(_effect):
        await asyncio.sleep(0)
        return True

    monkeypatch.setattr(worker, "_claim_effect_lease", _claim)

    async def _uncertain(_effect, lease_lost, ownership_uncertain):
        ownership_uncertain.set()
        lease_lost.set()

    monkeypatch.setattr(worker, "_extend_effect_lease", _uncertain)

    async def _fail_prepare(**_kwargs):
        raise RuntimeError("prepare failed")

    monkeypatch.setattr(worker, "_prepare_isolated_launch", _fail_prepare)
    failed = AsyncMock()
    monkeypatch.setattr(worker, "_fail_isolated_launch", failed)

    with pytest.raises(worker._EffectLeaseUncertain):
        await worker._launch_isolated_cloud_worker(
            ticket, "ticket-1", SimpleNamespace(effect_id="e1", attempts=0)
        )

    failed.assert_not_awaited()

@pytest.mark.asyncio
async def test_guard_ownership_signal_outranks_a_completed_launch(monkeypatch):
    async def _finish(_ticket, _prompt):
        return {"state": "ready", "summary": "", "question": ""}

    uncertain = asyncio.Event()
    uncertain.set()

    with pytest.raises(worker._EffectLeaseUncertain):
        await worker._run_isolated_launch_guarded(
            _finish({}, "prompt"),
            lease_lost=asyncio.Event(),
            ownership_uncertain=uncertain,
            ticket_stopped=asyncio.Event(),
        )


@pytest.mark.asyncio
async def test_threadless_failure_uses_one_atomic_transition_and_report(monkeypatch):
    atomic = AsyncMock(return_value=None)
    update = AsyncMock()
    add_report = AsyncMock()
    monkeypatch.setattr(worker.ticket_db, "fail_ticket_with_report", atomic)
    monkeypatch.setattr(worker.ticket_db, "update_ticket", update)
    monkeypatch.setattr(worker.ticket_db, "add_report", add_report)
    monkeypatch.setattr(
        worker.ticket_db,
        "get_ticket",
        AsyncMock(return_value=_cloud_ticket(status="queued", revision=7)),
    )

    with pytest.raises(worker._RetryableLaunch):
        await worker._fail_isolated_launch(
            ticket_id="ticket-1",
            thread_id="",
            error="failed closed",
            expected_revision=7,
        )

    atomic.assert_awaited_once()
    assert atomic.await_args.kwargs["expected_revision"] == 7
    # The transition and its report are one helper transaction, never two
    # separate writes that a retry could not repair.
    update.assert_not_awaited()
    add_report.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "current",
    [
        None,
        _cloud_ticket(status="failed", revision=8),
        _cloud_ticket(status="cancelled", revision=8),
        _cloud_ticket(status="running", revision=8, worker_thread_id="thread-2"),
        _cloud_ticket(status="queued", revision=8, worker_run_id="run-2"),
    ],
)
async def test_threadless_failure_fence_miss_on_settled_or_transferred_is_ack(
    monkeypatch, current
):
    atomic = AsyncMock(return_value=None)
    monkeypatch.setattr(worker.ticket_db, "fail_ticket_with_report", atomic)
    monkeypatch.setattr(
        worker.ticket_db, "get_ticket", AsyncMock(return_value=current)
    )

    # No exception: the line is settled or another attempt owns it.
    await worker._fail_isolated_launch(
        ticket_id="ticket-1",
        thread_id="",
        error="failed closed",
        expected_revision=7,
    )

# ---------------------------------------------------------------------------
# Follow-up: publication failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_late_instruction_failure_after_publication_is_retryable(monkeypatch):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lock(_ticket_id):
        yield

    monkeypatch.setattr(worker, "_instruction_handoff_lock", _lock)
    monkeypatch.setattr(
        worker.ticket_db, "update_ticket", AsyncMock(return_value=_cloud_ticket(status="running"))
    )
    monkeypatch.setattr(
        worker,
        "_pending_instruction_rows",
        AsyncMock(side_effect=RuntimeError("late read failed")),
    )

    with pytest.raises(worker._RetryableLaunch, match="late instruction"):
        await worker._publish_thread_and_queue_late_instructions(
            _cloud_ticket(), "ticket-1", "thread-1", set()
        )


@pytest.mark.asyncio
async def test_retry_resumes_a_published_run_that_never_reached_the_container(
    monkeypatch,
):
    effect = SimpleNamespace(payload={"ticket_id": "ticket-1"}, effect_id="e1", attempts=0)
    _set_switch(monkeypatch, "podman")
    ticket = _cloud_ticket(
        status="running",
        worker_thread_id="thread-1",
        worker_run_id=worker._isolated_run_id("ticket-1", effect),
        mandate={
            "cloud_worker": {
                "isolated": True,
                "run_id": worker._isolated_run_id("ticket-1", effect),
                "container_started": False,
            }
        },
    )
    monkeypatch.setattr(
        worker.ticket_db, "get_ticket", AsyncMock(return_value=ticket)
    )
    resumed = AsyncMock()
    monkeypatch.setattr(worker, "_launch_isolated_cloud_worker", resumed)
    failed = AsyncMock()
    monkeypatch.setattr(worker, "_fail_isolated_launch", failed)

    await worker.launch_worker_effect(effect)

    resumed.assert_awaited_once()
    failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_orphan_after_the_container_started_is_settled_closed(monkeypatch):
    effect = SimpleNamespace(payload={"ticket_id": "ticket-1"}, effect_id="e1", attempts=0)
    _set_switch(monkeypatch, "podman")
    ticket = _cloud_ticket(
        status="running",
        worker_thread_id="thread-1",
        worker_run_id=worker._isolated_run_id("ticket-1", effect),
        mandate={
            "cloud_worker": {
                "isolated": True,
                "run_id": worker._isolated_run_id("ticket-1", effect),
                "container_started": True,
            }
        },
    )
    monkeypatch.setattr(
        worker.ticket_db, "get_ticket", AsyncMock(return_value=ticket)
    )
    monkeypatch.setattr(worker, "_claim_effect_lease", AsyncMock(return_value=True))
    resumed = AsyncMock()
    monkeypatch.setattr(worker, "_launch_isolated_cloud_worker", resumed)
    failed = AsyncMock()
    monkeypatch.setattr(worker, "_fail_isolated_launch", failed)

    await worker.launch_worker_effect(effect)

    failed.assert_awaited_once()
    resumed.assert_not_awaited()
