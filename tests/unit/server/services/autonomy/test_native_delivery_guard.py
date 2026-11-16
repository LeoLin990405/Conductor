"""Native Worker completion must account for its Git workspace state."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.server.database.autonomy.tickets import (
    _REPORT_BODY_LIMIT,
    _report_body,
)
from src.server.services.autonomy import executor
from tests.unit.conftest import completion_report_json
from src.server.services.autonomy.git_delivery import (
    GitDeliveryError,
    WorkspaceDeliveryBusy,
    WorkspaceDeliveryLock,
    capture_git_snapshot,
    git_command_runner,
)


@pytest.fixture(autouse=True)
def _trusted_temp_repo_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CONDUCTOR_WORKER_LOCK_ROOT", str(tmp_path / "locks"))
    def runner(argv, *, cwd, timeout, env):
        return subprocess.run(
            argv,
            cwd=cwd,
            timeout=timeout,
            env=env,
            capture_output=True,
            check=False,
        )

    with git_command_runner(runner):
        yield


class _Cursor:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, *args, **kwargs):
        return None

    async def fetchall(self):
        return [("ticket-1", "user-1", "thread-1")]


class _Connection:
    def cursor(self):
        return _Cursor()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _git_workspace(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.name", "Test Worker")
    _git(path, "config", "user.email", "worker@example.invalid")
    (path / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-q", "-m", "baseline")
    return path


@pytest.mark.asyncio
async def test_native_delivery_wrapper_closes_source_on_cancel_without_finishing():
    from src.server.handlers.chat import native_run

    source_closed = asyncio.Event()
    source_blocked = asyncio.Event()

    async def source():
        try:
            yield "first"
            await source_blocked.wait()
        finally:
            source_closed.set()

    with patch.object(
        native_run, "_finish_native_worker_delivery", new=AsyncMock()
    ) as finish:
        wrapped = native_run._persist_native_delivery_before_run_end(
            source(),
            workspace_id="ws",
            native_cwd="/workspace",
            thread_id="thread",
            run_id="run",
            attempt=1,
            baseline=None,
            baseline_error=False,
        )
        assert await anext(wrapped) == "first"
        await wrapped.aclose()

    assert source_closed.is_set()
    finish.assert_not_awaited()


async def _poll_native_worker(
    *,
    cwd: Path | None,
    assistant: str | None,
    ticket_overrides: dict | None = None,
    metadata_failure: bool = False,
):
    ticket = {
        "ticket_id": "ticket-1",
        "status": "running",
        "worker_provider": "codex",
        "worker_run_id": "run-current",
        "worker_thread_id": "thread-1",
        "attempts": 0,
        **(ticket_overrides or {}),
    }
    dispatcher = AsyncMock()
    dispatcher.thread_run_status = AsyncMock(return_value="completed")
    dispatcher.thread_last_assistant_text = AsyncMock(return_value=assistant)
    metadata = (
        {"native_runtime": {"provider": "codex", "cwd": str(cwd)}}
        if cwd is not None
        else {"native_runtime": {"provider": "codex"}}
    )
    with (
        patch("src.server.database.pool.get_db_connection", return_value=_Connection()),
        patch.object(executor, "ProjectDispatcher", return_value=dispatcher),
        patch.object(executor.ticket_db, "get_ticket", AsyncMock(return_value=ticket)),
        patch.object(executor.ticket_db, "latest_report", AsyncMock(return_value=None)),
        patch(
            "src.server.database.conversation.native_runtime.get_thread_metadata",
            AsyncMock(side_effect=RuntimeError("metadata unavailable"))
            if metadata_failure
            else AsyncMock(return_value=metadata),
        ) as read_metadata,
        patch.object(executor, "apply_worker_report", AsyncMock()) as apply_report,
    ):
        await executor.poll_running_workers()
    apply_report.assert_awaited_once()
    result = dict(apply_report.await_args.kwargs)
    result["_metadata_reader"] = read_metadata
    return result


@pytest.mark.asyncio
async def test_executor_fails_closed_when_v2_result_is_missing(
    tmp_path: Path,
) -> None:
    workspace = _git_workspace(tmp_path / "native-result-handoff")
    baseline = capture_git_snapshot(str(workspace))
    ticket = {
        "ticket_id": "ticket-1",
        "status": "running",
        "worker_provider": "codex",
        "worker_thread_id": "thread-1",
        "worker_run_id": "run-current",
        "attempts": 2,
        "mandate": {
            "git_delivery_baseline": {
                "version": 2,
                "thread_id": "thread-1",
                "run_id": "run-current",
                "attempt": 2,
                "cwd_id": hashlib.sha256(
                    os.fsencode(os.path.realpath(workspace))
                ).hexdigest(),
                "snapshot": baseline.to_record(),
                "pending": False,
            }
        },
    }
    dispatcher = AsyncMock()
    dispatcher.thread_run_status = AsyncMock(return_value="completed")
    dispatcher.thread_last_assistant_text = AsyncMock(
        return_value=completion_report_json(summary="Done.")
    )
    with (
        patch("src.server.database.pool.get_db_connection", return_value=_Connection()),
        patch.object(executor, "ProjectDispatcher", return_value=dispatcher),
        patch.object(executor.ticket_db, "get_ticket", AsyncMock(return_value=ticket)),
        patch.object(executor.ticket_db, "latest_report", AsyncMock(return_value=None)),
        patch.object(executor, "apply_worker_report", AsyncMock()) as apply_report,
    ):
        await executor.poll_running_workers()

    apply_report.assert_awaited_once()
    assert apply_report.await_args.kwargs["state"] == "blocked"
    assert apply_report.await_args.kwargs["attention_reason"] == "git_status_unavailable"


@pytest.mark.asyncio
async def test_executor_uses_already_durable_v2_result(tmp_path: Path) -> None:
    workspace = _git_workspace(tmp_path / "native-result-race")
    baseline = capture_git_snapshot(str(workspace))
    mandate = {
        "git_delivery_baseline": {
            "version": 2,
            "thread_id": "thread-1",
            "run_id": "run-current",
            "attempt": 2,
            "cwd_id": hashlib.sha256(
                os.fsencode(os.path.realpath(workspace))
            ).hexdigest(),
            "snapshot": baseline.to_record(),
            "pending": False,
        }
    }

    mandate["git_delivery_result"] = {
        "version": 2,
        "thread_id": "thread-1",
        "run_id": "run-current",
        "attempt": 2,
        "summary": "Uncommitted git changes detected.",
    }
    report = await _poll_native_worker(
        cwd=workspace,
        assistant=completion_report_json(summary="Done."),
        ticket_overrides={"attempts": 2, "mandate": mandate},
    )

    assert report["state"] == "blocked"
    assert report["attention_reason"] == "uncommitted_changes"


@pytest.mark.asyncio
async def test_pending_native_baseline_binds_unpublished_run_and_rejects_stale_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from src.server.handlers.chat.native_run import (
        _save_native_git_delivery,
        _setup_worker_git_delivery,
    )

    workspace = _git_workspace(tmp_path / "pending-native-baseline")
    ticket = {
        "ticket_id": "ticket-1",
        "status": "running",
        "worker_thread_id": "thread-1",
        "worker_run_id": None,
        "attempts": 2,
        "revision": 7,
        "mandate": {
            "git_delivery_baseline": {
                "version": 2,
                "thread_id": "thread-1",
                "attempt": 2,
                "cwd_id": hashlib.sha256(
                    os.fsencode(os.path.realpath(workspace))
                ).hexdigest(),
                "snapshot": {"version": 1, "error": True},
                "pending": True,
            }
        },
    }
    updates = []

    async def update_ticket(_ticket_id, **kwargs):
        updates.append(kwargs)
        ticket["mandate"] = kwargs["mandate"]
        ticket["revision"] += 1
        return dict(ticket)

    real_capture = capture_git_snapshot
    captured = 0

    def capture(path, **kwargs):
        nonlocal captured
        captured += 1
        return real_capture(path, **kwargs)

    monkeypatch.setattr(
        "src.server.services.autonomy.git_delivery.capture_git_snapshot", capture
    )
    monkeypatch.setattr(
        "src.server.database.autonomy.tickets.get_ticket_by_worker_thread",
        AsyncMock(return_value=ticket),
    )
    monkeypatch.setattr(
        "src.server.database.autonomy.tickets.update_ticket", update_ticket
    )

    baseline, unavailable, attempt = await _setup_worker_git_delivery(
        "thread-1", "run-current", str(workspace), ticket=ticket
    )

    binding = ticket["mandate"]["git_delivery_baseline"]
    assert (
        baseline is not None and not unavailable and attempt == 2
    ), f"{caplog.text}\n{captured=} {updates=} {ticket=}"
    assert captured == 1
    assert binding["run_id"] == "run-current"
    assert binding["pending"] is False
    assert not binding["snapshot"].get("error")
    assert updates[0]["expected_revision"] == 7
    assert "expected_run_id" not in updates[0]

    ticket["worker_run_id"] = "run-newer"
    ticket["revision"] += 1
    with pytest.raises(RuntimeError, match="run changed"):
        await _save_native_git_delivery(
            "thread-1",
            "run-current",
            2,
            {"git_delivery_result": {"run_id": "run-current"}},
            allow_unpublished_run=True,
        )
    assert len(updates) == 1


@pytest.mark.asyncio
async def test_native_non_git_workspace_persists_empty_summary_and_stays_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.server.handlers.chat.native_run import (
        _finish_native_worker_delivery,
        _setup_worker_git_delivery,
    )

    workspace = tmp_path / "native-plain-workspace"
    workspace.mkdir()
    ticket = {
        "ticket_id": "ticket-1",
        "status": "running",
        "worker_thread_id": "thread-1",
        "worker_run_id": "run-current",
        "attempts": 2,
        "revision": 3,
        "mandate": {
            "git_delivery_baseline": {
                "version": 2,
                "thread_id": "thread-1",
                "attempt": 2,
                "cwd_id": hashlib.sha256(
                    os.fsencode(os.path.realpath(workspace))
                ).hexdigest(),
                "snapshot": {"version": 1, "error": True},
                "pending": True,
            }
        },
    }

    async def update_ticket(_ticket_id, **kwargs):
        ticket["mandate"] = kwargs["mandate"]
        ticket["revision"] += 1
        return dict(ticket)

    monkeypatch.setattr(
        "src.server.database.autonomy.tickets.get_ticket_by_worker_thread",
        AsyncMock(return_value=ticket),
    )
    monkeypatch.setattr(
        "src.server.database.autonomy.tickets.update_ticket", update_ticket
    )
    monkeypatch.setattr(
        "src.server.handlers.chat.native_run.sync_workspace_after_turn",
        AsyncMock(return_value=(0, 0)),
    )

    baseline, unavailable, attempt = await _setup_worker_git_delivery(
        "thread-1", "run-current", str(workspace), ticket=ticket
    )
    await _finish_native_worker_delivery(
        workspace_id="workspace-1",
        native_cwd=str(workspace),
        thread_id="thread-1",
        run_id="run-current",
        attempt=attempt,
        baseline=baseline,
        baseline_error=unavailable,
        run_completed=True,
    )

    assert baseline is not None and baseline.root_id is None and not unavailable
    assert ticket["mandate"]["git_delivery_result"]["summary"] == ""
    report = await _poll_native_worker(
        cwd=workspace,
        assistant=completion_report_json(summary="Done."),
        ticket_overrides={
            "worker_thread_id": "thread-1",
            "worker_run_id": "run-current",
            "attempts": 2,
            "mandate": ticket["mandate"],
        },
    )
    assert report["state"] == "ready"


def _baseline_binding(workspace: Path, *, attempt: int = 0) -> dict:
    return {
        "version": 1,
        "thread_id": "thread-1",
        "attempt": attempt,
        "cwd_id": "test-cwd",
        "snapshot": capture_git_snapshot(str(workspace)).to_record(),
    }


@pytest.mark.asyncio
async def test_dirty_native_workspace_blocks_ready_report_and_prepends_status(
    tmp_path: Path,
) -> None:
    workspace = _git_workspace(tmp_path / "dirty")
    baseline = capture_git_snapshot(str(workspace))
    (workspace / "tracked.txt").write_text("unfinished change\n", encoding="utf-8")

    report = await _poll_native_worker(
        cwd=workspace,
        assistant=completion_report_json(summary="Implementation done."),
        ticket_overrides={
            "mandate": {
                "git_delivery_baseline": {
                    "version": 1,
                    "thread_id": "thread-1",
                    "attempt": 0,
                    "cwd_id": "test-cwd",
                    "snapshot": baseline.to_record(),
                }
            }
        },
    )

    assert report["state"] == "blocked"
    assert report["attention_reason"] == "uncommitted_changes"
    assert report["question"] == (
        "Commit or discard the uncommitted workspace changes, then retry."
    )
    assert report["expected_run_id"] == "run-current"
    assert report["body"].startswith(
        "Uncommitted git changes detected.\n"
        "git status --short:\n"
        " M tracked.txt\n"
    )
    assert '"summary": "Implementation done."' in report["body"]


@pytest.mark.asyncio
async def test_dirty_native_workspace_blocks_even_when_final_assistant_is_empty(
    tmp_path: Path,
) -> None:
    workspace = _git_workspace(tmp_path / "empty-report")
    baseline = capture_git_snapshot(str(workspace))
    (workspace / "tracked.txt").write_text("unfinished change\n", encoding="utf-8")

    report = await _poll_native_worker(
        cwd=workspace,
        assistant=None,
        ticket_overrides={
            "mandate": {
                "git_delivery_baseline": {
                    "version": 1,
                    "thread_id": "thread-1",
                    "attempt": 0,
                    "cwd_id": "test-cwd",
                    "snapshot": baseline.to_record(),
                }
            }
        },
    )

    # No completion report is never success; the Git status stays visible.
    assert report["state"] == "waiting"
    assert report["attention_reason"] == "completion_report_missing"
    assert report["body"].startswith("Uncommitted git changes detected.\n")
    assert "tracked.txt" in report["body"]


@pytest.mark.asyncio
async def test_preexisting_native_dirt_does_not_block_and_read_only_run_skips_guard(
    tmp_path: Path,
) -> None:
    workspace = _git_workspace(tmp_path / "readonly-dirty")
    (workspace / "tracked.txt").write_text("operator dirt\n", encoding="utf-8")
    baseline = capture_git_snapshot(str(workspace))
    # The Worker delivers its own commit; the operator's dirt stays.
    (workspace / "worker.txt").write_text("worker change\n", encoding="utf-8")
    _git(workspace, "add", "worker.txt")
    _git(workspace, "commit", "-q", "-m", "worker change")

    report = await _poll_native_worker(
        cwd=workspace,
        assistant=completion_report_json(summary="No new changes."),
        ticket_overrides={
            "mandate": {
                "git_delivery_baseline": {
                    "version": 1,
                    "thread_id": "thread-1",
                    "attempt": 0,
                    "cwd_id": "test-cwd",
                    "snapshot": baseline.to_record(),
                }
            }
        },
    )

    assert report["state"] == "ready"
    assert report["attention_reason"] is None
    assert report["completion"].disagreement is False
    assert '"summary": "No new changes."' in report["body"]

    readonly_report = await _poll_native_worker(
        cwd=workspace,
        assistant=completion_report_json(summary="Read-only check complete."),
        ticket_overrides={"mandate": {"read_only": True}},
    )
    assert readonly_report["state"] == "ready"
    assert readonly_report["attention_reason"] is None


@pytest.mark.asyncio
async def test_native_retry_keeps_original_baseline_for_prior_uncommitted_work(
    tmp_path: Path,
) -> None:
    workspace = _git_workspace(tmp_path / "retry-dirty")
    baseline = capture_git_snapshot(str(workspace))
    (workspace / "unfinished.txt").write_text("first attempt output\n", encoding="utf-8")

    report = await _poll_native_worker(
        cwd=workspace,
        assistant=completion_report_json(summary="No additional edits."),
        ticket_overrides={
            "attempts": 1,
            "mandate": {
                "git_delivery_baseline": {
                    "version": 1,
                    "thread_id": "thread-1",
                    "attempt": 1,
                    "cwd_id": "test-cwd",
                    "snapshot": baseline.to_record(),
                }
            },
        },
    )

    assert report["state"] == "blocked"
    assert report["attention_reason"] == "uncommitted_changes"
    assert "unfinished.txt" in report["body"]


@pytest.mark.asyncio
async def test_committed_native_workspace_completes_as_ready(tmp_path: Path) -> None:
    workspace = _git_workspace(tmp_path / "committed")
    (workspace / "tracked.txt").write_text("committed change\n", encoding="utf-8")
    _git(workspace, "add", "tracked.txt")
    _git(workspace, "commit", "-q", "-m", "worker change")

    report = await _poll_native_worker(cwd=workspace, assistant=completion_report_json(summary="Committed result."))

    assert report["state"] == "ready"
    assert report["attention_reason"] is None
    assert '"summary": "Committed result."' in report["body"]
    assert report["expected_run_id"] == "run-current"


@pytest.mark.asyncio
async def test_non_git_native_workspace_completes_as_ready(tmp_path: Path) -> None:
    workspace = tmp_path / "plain"
    workspace.mkdir()

    report = await _poll_native_worker(cwd=workspace, assistant=completion_report_json(summary="Finished."))

    assert report["state"] == "ready"
    assert report["attention_reason"] is None
    assert '"summary": "Finished."' in report["body"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected_state", "question"),
    [
        ("blocked", "blocked", "Which branch should I use?"),
        ("partial", "waiting", None),
    ],
)
async def test_dirty_workspace_does_not_override_reported_blocked_or_partial_state(
    tmp_path: Path, status: str, expected_state: str, question: str | None
) -> None:
    workspace = _git_workspace(tmp_path / status)
    baseline = capture_git_snapshot(str(workspace))
    (workspace / "tracked.txt").write_text("unfinished change\n", encoding="utf-8")
    assistant = completion_report_json(
        status, "Work stopped.", next_step="Which branch should I use?"
    )

    report = await _poll_native_worker(
        cwd=workspace,
        assistant=assistant,
        ticket_overrides={
            "mandate": {
                "git_delivery_baseline": {
                    **_baseline_binding(workspace),
                    "snapshot": baseline.to_record(),
                }
            }
        },
    )

    assert report["state"] == expected_state
    assert report["question"] == question
    assert report["attention_reason"] is None
    assert report["body"].startswith("Uncommitted git changes detected.\n")


@pytest.mark.asyncio
async def test_long_redacted_report_retains_dirty_notice_after_storage_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _git_workspace(tmp_path / "long-body")
    baseline = capture_git_snapshot(str(workspace))
    (workspace / "tracked.txt").write_text("unfinished change\n", encoding="utf-8")
    sentinel = "native-worker-secret-sentinel-123456789"
    monkeypatch.setenv("TEST_API_KEY", sentinel)
    monkeypatch.setattr(
        "src.llms.native_cli.isolation.current_worker_isolation",
        lambda: SimpleNamespace(env_names=frozenset({"TEST_API_KEY"})),
    )
    assistant = f"{sentinel}\n" + ("result text " * 7000)

    report = await _poll_native_worker(
        cwd=workspace,
        assistant=assistant,
        ticket_overrides={
            "mandate": {
                "git_delivery_baseline": {
                    **_baseline_binding(workspace),
                    "snapshot": baseline.to_record(),
                }
            }
        },
    )
    stored_body = _report_body(report["body"])

    assert stored_body is not None
    assert len(stored_body.encode("utf-8")) <= _REPORT_BODY_LIMIT
    assert stored_body.startswith(
        "Uncommitted git changes detected.\n"
        "git status --short:\n"
        " M tracked.txt\n"
    )
    assert "report truncated" in stored_body
    assert sentinel not in stored_body


@pytest.mark.asyncio
async def test_executor_caps_native_metadata_retries_and_fails_closed(
    tmp_path: Path,
) -> None:
    workspace = _git_workspace(tmp_path / "metadata-retries")
    baseline = capture_git_snapshot(str(workspace))
    report = await _poll_native_worker(
        cwd=workspace,
        assistant=completion_report_json(summary="Implementation done."),
        ticket_overrides={
            "mandate": {
                "git_delivery_baseline": {
                    "version": 1,
                    "thread_id": "thread-1",
                    "attempt": 0,
                    "cwd_id": "test-cwd",
                    "snapshot": baseline.to_record(),
                }
            }
        },
        metadata_failure=True,
    )

    assert report["state"] == "blocked"
    assert report["attention_reason"] == "git_status_unavailable"
    assert report["_metadata_reader"].await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("disconnect", [False, True])
@pytest.mark.parametrize(
    "baseline_failure", ["none", "ticket_metadata", "baseline_write", "lock_busy", "clean"]
)
@pytest.mark.parametrize("delivery_mode", ["writable", "read_only", "speculative"])
async def test_native_worker_entry_persists_result_before_run_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    disconnect: bool,
    baseline_failure: str,
    delivery_mode: str,
) -> None:
    from src.server.handlers.chat import native_run
    from src.server.models.chat import ChatRequest
    from src.server.services.native_runtime.codex_app_server import CodexAppServerRuntime
    from src.server.services.native_runtime.contracts import NativeRuntimeSpec
    from src.server.services.turn_runtime import TurnRoute

    thread_id = "thread-1"
    run_id = "run-current"
    user_id = "user-1"
    cwd = tmp_path / "native-private-cwd"
    cwd.mkdir()
    lock_root = tmp_path / "native-locks"
    release_cli = tmp_path / "release-cli"
    binary = tmp_path / "fake-codex"
    binary.write_text(
        f"#!{os.sys.executable}\n"
        "import json, pathlib, sys, time\n"
        f"release = pathlib.Path({str(release_cli)!r})\n"
        "for line in sys.stdin:\n"
        " message = json.loads(line)\n"
        " method = message.get('method')\n"
        " request_id = message.get('id')\n"
        " def reply(result):\n"
        "  print(json.dumps({'jsonrpc':'2.0','id':request_id,'result':result}), flush=True)\n"
        " def emit(method, params):\n"
        "  print(json.dumps({'jsonrpc':'2.0','method':method,'params':params}), flush=True)\n"
        " if method == 'initialize': reply({})\n"
        " elif method == 'thread/start': reply({'thread': {'id': 'fake-thread'}})\n"
        " elif method == 'turn/start':\n"
        "  reply({'turn': {'id': 'turn-1'}})\n"
        "  while not release.exists(): time.sleep(0.01)\n"
        "  emit('item/agentMessage/delta', {'threadId':'fake-thread','turnId':'turn-1','itemId':'msg-1','delta':'fake cli complete'})\n"
        "  emit('turn/completed', {'threadId':'fake-thread','turn':{'id':'turn-1','status':'completed','items':[]}})\n",
        encoding="utf-8",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    events: list[str] = []
    cli_started = asyncio.Event()
    run_finished = asyncio.Event()
    ticket = {
        "ticket_id": "ticket-native-entry",
        "status": "running",
        "worker_thread_id": thread_id,
        # The launch receipt is persisted after native dispatch starts.
        "worker_run_id": None,
        "attempts": 2,
        "revision": 4,
        "mandate": {
            **({"read_only": True} if delivery_mode == "read_only" else {}),
            **({"speculative": True} if delivery_mode == "speculative" else {}),
            "git_delivery_baseline": {
                "version": 2,
                "thread_id": thread_id,
                "attempt": 2,
                "cwd_id": hashlib.sha256(
                    os.fsencode(os.path.realpath(cwd))
                ).hexdigest(),
                "snapshot": {"version": 1, "error": True},
                "pending": True,
            }
        },
    }
    writes = 0
    ticket_reads = 0
    poll_result = None
    poll_observed_running = False

    async def read_ticket(*_args, **_kwargs):
        nonlocal ticket_reads
        ticket_reads += 1
        events.append("baseline-metadata-read")
        if baseline_failure == "ticket_metadata" and ticket_reads == 1:
            raise RuntimeError("transient ticket metadata failure")
        return ticket

    async def update_ticket(_ticket_id, **kwargs):
        nonlocal writes, poll_result, poll_observed_running
        writes += 1
        if baseline_failure == "baseline_write" and writes == 1:
            return None
        ticket["mandate"] = dict(kwargs["mandate"])
        if "git_delivery_result" in ticket["mandate"]:
            events.append("result-saved")
            poll_observed_running = (
                executor.executions[(thread_id, run_id)].status == "running"
            )
            poll_result = await _poll_native_worker(
                cwd=cwd,
                assistant=completion_report_json(summary="Done."),
                ticket_overrides={
                    "worker_thread_id": thread_id,
                    "worker_run_id": run_id,
                    "attempts": 2,
                    "mandate": ticket["mandate"],
                },
            )
            events.append("executor-poll-after-result")
        return dict(ticket)

    async def materialize(*, workspace_id, cwd, user_id):
        assert workspace_id == "ws-native-entry"
        assert user_id == "user-1"
        events.append("materialize")
        repo = Path(cwd)
        (repo / "tracked.txt").write_text("workspace seed\n", encoding="utf-8")
        _git(repo, "init", "-q")
        _git(repo, "config", "user.name", "Test Worker")
        _git(repo, "config", "user.email", "worker@example.invalid")
        _git(repo, "add", "tracked.txt")
        _git(repo, "commit", "-q", "-m", "materialized workspace")

    actual_capture = capture_git_snapshot

    def capture_after_materialization(path: str, **kwargs):
        if "baseline" not in events:
            assert (Path(path) / "tracked.txt").read_text(encoding="utf-8") == "workspace seed\n"
            events.append("baseline")
        else:
            events.append("completion-probe")
        return actual_capture(path, **kwargs)

    runtime = CodexAppServerRuntime(
        binary=str(binary), cwd=str(cwd), broker_thread_id=thread_id
    )

    if baseline_failure == "lock_busy":
        class BusyDeliveryLock:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                raise WorkspaceDeliveryBusy("workspace handoff is busy")

            async def __aexit__(self, *_args):
                return False

        monkeypatch.setattr(
            "src.server.services.autonomy.git_delivery.AsyncWorkspaceDeliveryLock",
            BusyDeliveryLock,
        )

    class FakeProducer:
        def __init__(self, **_kwargs):
            pass

        def stream_workflow(self, **_kwargs):
            async def workflow():
                cli_started.set()
                async for event in runtime.stream_turn("fake prompt", cwd=str(cwd)):
                    if event.kind == "text_delta":
                        events.append("codex-text")
                events.append("codex-cleaned")
                assert runtime._client is None
                if baseline_failure != "clean":
                    (cwd / "tracked.txt").write_text("worker edit\n", encoding="utf-8")
                elif delivery_mode == "writable":
                    # A clean run delivers its work as a commit.
                    (cwd / "tracked.txt").write_text("worker edit\n", encoding="utf-8")
                    _git(cwd, "commit", "-q", "-am", "worker edit")
                yield "fake-workflow-finished"

            return workflow()

    class FakeExecutor:
        executions = {}

        async def start_run(self, *, thread_id, run_id, workflow_generator, on_registered, **_kwargs):
            execution = SimpleNamespace(status="running", task=None)

            async def consume():
                try:
                    async for _event in workflow_generator:
                        pass
                    execution.status = "completed"
                    events.append("run_finished")
                except BaseException:
                    execution.status = "cancelled"
                    raise
                finally:
                    run_finished.set()

            execution.task = asyncio.create_task(consume())
            self.executions[(thread_id, run_id)] = execution
            on_registered()
            return execution

    executor = FakeExecutor()
    scope = SimpleNamespace(
        transfer_to_executor=lambda: events.append("transferred"),
        release_admission=lambda: events.append("released-admission"),
    )

    async def stream_log(*_args, **_kwargs):
        await cli_started.wait()
        with WorkspaceDeliveryLock(str(cwd), timeout=0.05, lock_root=str(lock_root)):
            events.append("lock-free-during-codex")
        if not disconnect:
            release_cli.touch()
        yield "event: progress\\ndata: {}\\n\\n"
        await run_finished.wait()
        yield "event: complete\\ndata: {}\\n\\n"

    monkeypatch.setenv("CONDUCTOR_WORKER_LOCK_ROOT", str(lock_root))
    monkeypatch.setattr(
        "src.server.services.autonomy.git_delivery.capture_git_snapshot",
        capture_after_materialization,
    )
    with (
        patch.object(native_run, "init_tracking", return_value=(SimpleNamespace(record_native_usage=lambda **_kwargs: None), None)),
        patch.object(native_run, "_native_cwd", return_value=str(cwd)),
        patch.object(native_run, "materialize_workspace", new=materialize),
        patch.object(native_run, "collect_builtin_mcp_servers", new=AsyncMock(return_value=[])),
        patch.object(native_run, "issue_memory_token", return_value="fake-capability"),
        patch.object(native_run, "get_runtime", return_value=runtime) as get_runtime,
        patch.object(
            native_run, "build_native_graph", return_value=object()
        ) as build_graph,
        patch.object(native_run, "build_graph_config", return_value={}),
        patch.object(native_run, "normalize_request_messages", return_value=[{"role": "user", "content": "run"}]),
        patch.object(native_run, "turn_surface", return_value="test"),
        patch.object(native_run, "RunSSEProducer", FakeProducer),
        patch.object(native_run, "LocalRunExecutor", SimpleNamespace(get_instance=lambda: executor)),
        patch.object(native_run, "stream_from_log", new=stream_log),
        patch.object(native_run, "sync_workspace_after_turn", new=AsyncMock(return_value=(0, 0))),
        patch(
            "src.server.database.autonomy.tickets.get_ticket_by_worker_thread",
            new=read_ticket,
        ),
        patch("src.server.database.autonomy.tickets.update_ticket", new=update_ticket),
    ):
        workflow = native_run.astream_native_workflow(
            request=ChatRequest(workspace_id="ws-native-entry", messages=[{"role": "user", "content": "run"}]),
            thread_id=thread_id,
            run_id=run_id,
            user_input="run",
            user_id=user_id,
            route=TurnRoute("ws-native-entry", "worker"),
            spec=NativeRuntimeSpec(provider="codex"),
            scope=scope,
            run_handle=SimpleNamespace(checkpointer=None, turn_index=1),
            turn_context=SimpleNamespace(tool_timezone="UTC"),
            config=SimpleNamespace(llm=SimpleNamespace(name="fake-model")),
            is_byok=False,
        )
        if disconnect:
            assert await anext(workflow) == "event: progress\\ndata: {}\\n\\n"
            await workflow.aclose()
            assert not run_finished.is_set()
            with WorkspaceDeliveryLock(str(cwd), timeout=0.05, lock_root=str(lock_root)):
                pass
        else:
            output = [event async for event in workflow]
            assert output == ["event: progress\\ndata: {}\\n\\n", "event: complete\\ndata: {}\\n\\n"]

        get_runtime.assert_called_once()
        # A Worker turn that writes no text completes empty (a missing
        # completion report) instead of failing the run.
        assert build_graph.call_args.kwargs["allow_empty_answer"] is True
        runtime_kwargs = get_runtime.call_args.kwargs
        assert runtime_kwargs["transport"] == "app-server"
        assert runtime_kwargs["cwd"] == str(cwd)
        assert events.index("materialize") < events.index("baseline-metadata-read")
        if "baseline" in events:
            assert events.index("baseline-metadata-read") < events.index("baseline")
        assert "lock-free-during-codex" in events

        if disconnect:
            assert "codex-cleaned" not in events
            release_cli.touch()
            await executor.executions[(thread_id, run_id)].task
        assert "codex-cleaned" in events
        if delivery_mode == "writable" and baseline_failure != "baseline_write":
            assert events.index("result-saved") < events.index("run_finished")
            assert events.index("result-saved") < events.index("executor-poll-after-result")
            assert poll_observed_running
            expected_attention = {
                "none": "uncommitted_changes",
                "ticket_metadata": "uncommitted_changes",
                "lock_busy": "git_status_unavailable",
                "clean": None,
            }[baseline_failure]
            assert poll_result["attention_reason"] == expected_attention
        setup_event = "baseline" if "baseline" in events else "baseline-metadata-read"
        assert events.index(setup_event) < events.index("codex-cleaned")
        assert run_finished.is_set()
        assert runtime._client is None

        # The executor-owned workflow wrapper completes after the SSE disconnect.
        acquired = False
        for _ in range(30):
            try:
                with WorkspaceDeliveryLock(
                    str(cwd), timeout=0.05, lock_root=str(lock_root)
                ):
                    acquired = True
                break
            except GitDeliveryError:
                await asyncio.sleep(0.02)
        assert acquired
        if delivery_mode == "writable" and baseline_failure != "baseline_write":
            assert "git_delivery_result" in ticket["mandate"]

    if delivery_mode != "writable":
        assert "git_delivery_result" not in ticket["mandate"]
        completion = await _poll_native_worker(
            cwd=cwd,
            assistant=completion_report_json(summary="Done."),
            ticket_overrides={
                "worker_thread_id": thread_id,
                "worker_run_id": run_id,
                "attempts": 2,
                "mandate": ticket["mandate"],
            },
        )
        # A speculative run that finds nothing to do is dropped, not ready.
        assert completion["state"] == (
            "dropped" if delivery_mode == "speculative" else "ready"
        )
    elif baseline_failure in {"baseline_write", "lock_busy"}:
        assert ticket["mandate"]["git_delivery_baseline"]["snapshot"]["error"] is True
        if baseline_failure == "baseline_write":
            assert "git_delivery_result" not in ticket["mandate"]
        else:
            assert ticket["mandate"]["git_delivery_result"]["summary"] == (
                "Git workspace status is unknown because it could not be checked safely."
            )
        completion = await _poll_native_worker(
            cwd=cwd,
            assistant=completion_report_json(summary="Done."),
            ticket_overrides={
                "worker_thread_id": thread_id,
                "worker_run_id": run_id,
                "attempts": 2,
                "mandate": ticket["mandate"],
            },
        )
        assert completion["state"] == "blocked"
        assert completion["attention_reason"] == "git_status_unavailable"
    else:
        if baseline_failure == "ticket_metadata":
            assert ticket_reads >= 2
        baseline_binding = ticket["mandate"]["git_delivery_baseline"]
        result_binding = ticket["mandate"]["git_delivery_result"]
        assert baseline_binding["version"] == 2
        assert result_binding["version"] == 2
        assert result_binding["thread_id"] == thread_id
        assert result_binding["run_id"] == run_id
        assert result_binding["attempt"] == 2
        if baseline_failure == "clean":
            assert result_binding["summary"] == ""
            assert result_binding["commit_delivery"] == "delivered"
        else:
            assert "Uncommitted git changes detected." in result_binding["summary"]
            assert result_binding["commit_delivery"] == "missing"
        completion = await _poll_native_worker(
            cwd=cwd,
            assistant=completion_report_json(summary="Done."),
            ticket_overrides={
                "worker_thread_id": thread_id,
                "worker_run_id": run_id,
                "attempts": 2,
                "mandate": ticket["mandate"],
            },
        )
        if baseline_failure == "clean":
            assert completion["state"] == "ready"
        else:
            assert completion["state"] == "blocked"
            assert completion["attention_reason"] == "uncommitted_changes"


@pytest.mark.asyncio
async def test_a_new_thread_after_a_committing_run_still_counts_that_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: re-baselining on a new thread must not hide run 1's commit."""
    from src.server.handlers.chat.native_run import (
        _finish_native_worker_delivery,
        _setup_worker_git_delivery,
    )

    workspace = _git_workspace(tmp_path / "rebaseline")
    ticket = {
        "ticket_id": "ticket-1",
        "status": "running",
        "worker_thread_id": "thread-1",
        "worker_run_id": "run-1",
        "attempts": 1,
        "revision": 1,
        "mandate": {},
    }

    async def update_ticket(_ticket_id, **kwargs):
        ticket["mandate"] = kwargs["mandate"]
        ticket["revision"] += 1
        return dict(ticket)

    monkeypatch.setattr(
        "src.server.database.autonomy.tickets.get_ticket_by_worker_thread",
        AsyncMock(side_effect=lambda _thread: ticket),
    )
    monkeypatch.setattr(
        "src.server.database.autonomy.tickets.update_ticket", update_ticket
    )
    monkeypatch.setattr(
        "src.server.handlers.chat.native_run.sync_workspace_after_turn",
        AsyncMock(return_value=(0, 0)),
    )

    await _setup_worker_git_delivery("thread-1", "run-1", str(workspace), ticket=ticket)
    origin = ticket["mandate"]["git_delivery_origin"]
    assert origin["known"] is True
    # Run 1 commits, then reports partial; the Ticket moves to a new thread.
    (workspace / "tracked.txt").write_text("run 1\n", encoding="utf-8")
    _git(workspace, "commit", "-q", "-am", "run 1")
    ticket.update(worker_thread_id="thread-2", worker_run_id="run-2", attempts=2)

    baseline, unavailable, attempt = await _setup_worker_git_delivery(
        "thread-2", "run-2", str(workspace), ticket=ticket
    )
    await _finish_native_worker_delivery(
        workspace_id="workspace-1",
        native_cwd=str(workspace),
        thread_id="thread-2",
        run_id="run-2",
        attempt=attempt,
        baseline=baseline,
        baseline_error=unavailable,
        run_completed=True,
    )

    assert ticket["mandate"]["git_delivery_origin"] == origin
    result = ticket["mandate"]["git_delivery_result"]
    assert result["summary"] == ""
    assert result["commit_delivery"] == "delivered"


@pytest.mark.asyncio
async def test_a_first_run_without_a_baseline_never_records_a_later_head_as_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.server.handlers.chat.native_run import _setup_worker_git_delivery
    from src.server.services.autonomy import git_delivery

    workspace = _git_workspace(tmp_path / "late-origin")
    ticket = {
        "ticket_id": "ticket-1",
        "status": "running",
        "worker_thread_id": "thread-1",
        "worker_run_id": "run-1",
        "attempts": 1,
        "revision": 1,
        "mandate": {},
    }

    async def update_ticket(_ticket_id, **kwargs):
        ticket["mandate"] = kwargs["mandate"]
        ticket["revision"] += 1
        return dict(ticket)

    monkeypatch.setattr(
        "src.server.database.autonomy.tickets.get_ticket_by_worker_thread",
        AsyncMock(side_effect=lambda _thread: ticket),
    )
    monkeypatch.setattr(
        "src.server.database.autonomy.tickets.update_ticket", update_ticket
    )
    real_capture = git_delivery.capture_git_snapshot

    def failing_capture(*_args, **_kwargs):
        raise git_delivery.GitDeliveryError("unavailable")

    monkeypatch.setattr(git_delivery, "capture_git_snapshot", failing_capture)
    await _setup_worker_git_delivery("thread-1", "run-1", str(workspace), ticket=ticket)
    assert ticket["mandate"]["git_delivery_origin"] == {"version": 1, "known": False}

    monkeypatch.setattr(git_delivery, "capture_git_snapshot", real_capture)
    ticket.update(worker_run_id="run-2", attempts=2)
    baseline, unavailable, _attempt = await _setup_worker_git_delivery(
        "thread-1", "run-2", str(workspace), ticket=ticket
    )

    assert ticket["mandate"]["git_delivery_origin"] == {"version": 1, "known": False}
    assert baseline is not None and not unavailable
    assert baseline.origin_unknown is True


@pytest.mark.asyncio
async def test_a_placeholder_only_launch_then_retry_still_checks_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: attempt 1 left only its pending placeholder (the launch
    failed before the baseline); the retry must record a known origin, so a
    bare done on a clean tree does not settle ready."""
    from src.server.handlers.chat.native_run import (
        _finish_native_worker_delivery,
        _setup_worker_git_delivery,
    )
    from src.server.services.autonomy.completion import settle_native_completion
    from src.server.services.autonomy.worker import mark_unknown_delivery_origin

    workspace = _git_workspace(tmp_path / "placeholder-retry")
    cwd_id = hashlib.sha256(os.fsencode(os.path.realpath(workspace))).hexdigest()

    def placeholder(attempt: int) -> dict:
        return {
            "version": 2,
            "thread_id": "thread-1",
            "attempt": attempt,
            "cwd_id": cwd_id,
            "snapshot": {"version": 1, "error": True},
            "pending": True,
        }

    ticket = {
        "ticket_id": "ticket-1",
        "status": "running",
        "worker_thread_id": "thread-1",
        "worker_run_id": "run-2",
        "attempts": 2,
        "revision": 1,
        "mandate": {"git_delivery_baseline": placeholder(1)},
    }
    # The retry's launch: worker.py sees only attempt 1's placeholder.
    mandate = ticket["mandate"]
    mark_unknown_delivery_origin(mandate, mandate["git_delivery_baseline"])
    mandate["git_delivery_baseline"] = placeholder(2)

    async def update_ticket(_ticket_id, **kwargs):
        ticket["mandate"] = kwargs["mandate"]
        ticket["revision"] += 1
        return dict(ticket)

    monkeypatch.setattr(
        "src.server.database.autonomy.tickets.get_ticket_by_worker_thread",
        AsyncMock(side_effect=lambda _thread: ticket),
    )
    monkeypatch.setattr(
        "src.server.database.autonomy.tickets.update_ticket", update_ticket
    )
    monkeypatch.setattr(
        "src.server.handlers.chat.native_run.sync_workspace_after_turn",
        AsyncMock(return_value=(0, 0)),
    )

    baseline, unavailable, attempt = await _setup_worker_git_delivery(
        "thread-1", "run-2", str(workspace), ticket=ticket
    )
    assert ticket["mandate"]["git_delivery_origin"]["known"] is True
    await _finish_native_worker_delivery(
        workspace_id="workspace-1",
        native_cwd=str(workspace),
        thread_id="thread-1",
        run_id="run-2",
        attempt=attempt,
        baseline=baseline,
        baseline_error=unavailable,
        run_completed=True,
    )
    result = ticket["mandate"]["git_delivery_result"]
    assert result["commit_delivery"] == "missing"

    outcome = settle_native_completion(
        completion_report_json(),
        git_summary=result["summary"],
        commit_delivery=result["commit_delivery"],
        baseline_clean=result["baseline_clean"],
    )
    assert outcome["state"] != "ready"
