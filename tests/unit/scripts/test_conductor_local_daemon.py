"""The local daemon parses the native report contract."""

import asyncio
import json
import os
import subprocess
import threading
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from scripts import conductor_local_daemon as daemon
from src.llms.native_cli.process import merged_env
from tests.unit.conftest import assert_daemon_home_isolated, completion_report_json

# Captured before the autouse fixture that reads test claims as current ones.
_REAL_CLAIM_HAS_CONTRACT = daemon._claim_has_completion_contract


@pytest.fixture(autouse=True)
def local_isolation_policy(tmp_path, monkeypatch):
    from src.llms.native_cli import isolation

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = tmp_path / "workspace-policy.json"
    policy.write_text("{}")
    os.chmod(policy, 0o600)
    monkeypatch.setenv("CONDUCTOR_WORKSPACE_POLICY", str(policy))
    monkeypatch.setenv("CONDUCTOR_ISOLATION_ROOT", str(tmp_path / "worker-state"))
    monkeypatch.setattr(
        isolation,
        "read_workspace_policy",
        lambda *_args: {"root": str(workspace), "env": set(), "runtime_read_paths": ()},
    )
    return workspace


def test_daemon_defaults_never_point_at_the_real_home(tmp_path):
    # tests/unit/scripts/conftest.py pins HOME and the module-level defaults.
    assert_daemon_home_isolated(tmp_path.resolve() / "home")


def test_main_async_refuses_startup_without_isolation_root(monkeypatch):
    monkeypatch.delenv("CONDUCTOR_ISOLATION_ROOT", raising=False)
    args = SimpleNamespace(max_concurrency=1, isolation_root="")
    assert asyncio.run(daemon.main_async(args)) == 2


def test_resolve_isolation_root_prefers_flag_and_uses_env_fallback(monkeypatch, tmp_path):
    flag_root = str(tmp_path / "flag-root")
    env_root = str(tmp_path / "env-root")
    monkeypatch.setenv("CONDUCTOR_ISOLATION_ROOT", env_root)
    assert daemon._resolve_isolation_root(SimpleNamespace(isolation_root="")) == env_root
    assert daemon._resolve_isolation_root(SimpleNamespace(isolation_root="  ")) == env_root
    assert (
        daemon._resolve_isolation_root(SimpleNamespace(isolation_root=flag_root))
        == flag_root
    )
    monkeypatch.delenv("CONDUCTOR_ISOLATION_ROOT")
    assert daemon._resolve_isolation_root(SimpleNamespace(isolation_root="")) == ""


def test_api_ticket_status_uses_daemon_status_route():
    api = daemon.Api("https://conductor.example", "daemon-secret")
    with patch.object(api, "_call", return_value={"status": "cancelled"}) as call:
        assert api.ticket_status("ticket-123") == {"status": "cancelled"}
    call.assert_called_once_with(
        "GET", "/api/v1/autonomy/daemon/tickets/ticket-123/status", timeout=None
    )


@pytest.mark.parametrize("explicit", [None, "explicit-token"])
def test_taking_daemon_token_scrubs_only_daemon_credential(monkeypatch, explicit):
    monkeypatch.setenv("CONDUCTOR_DAEMON_TOKEN", "environment-token")
    monkeypatch.setenv("UNRELATED_CHILD_SETTING", "preserved")

    token = daemon._take_daemon_token(explicit)

    assert token == (explicit or "environment-token")
    child_env = merged_env(None)
    assert "CONDUCTOR_DAEMON_TOKEN" not in child_env
    assert os.environ["UNRELATED_CHILD_SETTING"] == "preserved"
    assert child_env["UNRELATED_CHILD_SETTING"] == "preserved"
    api = daemon.Api("http://local", token)
    assert api.token == token


def test_parse_report_reads_a_blocked_completion_report():
    report = daemon.parse_report(
        completion_report_json(
            "blocked",
            "I need a decision before changing the workflow.",
            next_step="Which branch?",
        )
    )
    assert report["state"] == "blocked"
    assert report["question"] == "Which branch?"
    assert "workflow" in report["summary"]
    assert "attention_reason" not in report


@pytest.mark.parametrize(
    "text",
    [
        "The work is done.",
        "AUTONOMY_STATE: ready",
        "AUTONOMY_STATE: blocked\nAUTONOMY_QUESTION: Which branch?",
        "",
    ],
)
def test_parse_report_without_a_valid_report_waits_never_ready(text):
    report = daemon.parse_report(text)
    assert report["state"] == "waiting"
    assert report["attention_reason"] == "completion_report_missing"
    assert report["question"]


def test_parse_report_applies_a_missing_commit_verdict_without_listed_commits():
    """Regression: no-delivery wins even when the Worker lists no commits."""
    report = daemon.parse_report(
        json.dumps(
            {
                **json.loads(completion_report_json()),
                "artifacts": {"commits": [], "files": ["a.py"], "pr": None},
            }
        ),
        git_summary="",
        commit_delivery="missing",
    )
    assert report["state"] == "blocked"
    assert "confirm a delivered commit" in report["question"]
    assert '"disagreement": true' in report["body"]


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_changes", [False, True])
async def test_dirty_git_worker_report_is_needs_action_with_redacted_status(
    tmp_path, local_isolation_policy, commit_changes
):
    workspace = local_isolation_policy
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.name", "test"], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.email", "test@example.invalid"], check=True)
    source = workspace / "tracked.txt"
    source.write_text("before")
    subprocess.run(["git", "-C", str(workspace), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(workspace), "commit", "-qm", "baseline"], check=True)
    api = Mock()
    manager = daemon.LeaseManager(api)
    ticket = {
        "workspace_id": "test-workspace",
        "ticket_id": "t-dirty",
        "lease_id": "11111111-1111-4111-8111-111111111122",
        "lease_duration_seconds": 60,
        "worker_provider": "codex",
    }
    async def worker_run(*_args, **_kwargs):
        source.write_text("after")
        if commit_changes:
            subprocess.run(["git", "-C", str(workspace), "add", "tracked.txt"], check=True)
            subprocess.run(["git", "-C", str(workspace), "commit", "-qm", "completed"], check=True)
        return SimpleNamespace(text=completion_report_json())

    adapter = SimpleNamespace(run=AsyncMock(side_effect=worker_run))
    with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter):
        await daemon.run_claimed(
            api,
            "codex",
            str(workspace),
            30,
            {"ticket": ticket, "prompt": "work"},
            manager,
        )

    report = api.report.call_args.args[-1]
    if commit_changes:
        assert report["state"] == "ready"
        assert "attention_reason" not in report
        body = json.loads(report["body"])
        assert body["status"] == "done"
        assert body["reconciled_state"] == "ready"
        assert body["disagreement"] is False
    else:
        assert report["state"] == "blocked"
        assert report["attention_reason"] == "uncommitted_changes"
        assert report["body"].startswith("Uncommitted git changes detected.")
        assert " M tracked.txt" in report["body"]
        assert "git diff --stat HEAD:" in report["body"]


@pytest.mark.asyncio
async def test_run_claimed_stops_cli_when_ticket_is_cancelled_remotely():
    api = Mock()
    api.ticket_status.return_value = {"status": "cancelled"}
    manager = daemon.LeaseManager(api)
    cancelled = asyncio.Event()

    async def slow_run(prompt, cwd=None):
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    ticket = {
        "workspace_id": "test-workspace",
        "ticket_id": "t1",
        "lease_id": "11111111-1111-4111-8111-111111111111",
        "lease_duration_seconds": 60,
        "worker_provider": "codex",
        "speculative": False,
    }
    with patch(
        "src.llms.native_cli.registry.get_adapter",
        return_value=SimpleNamespace(run=slow_run),
    ):
        result = await daemon.run_claimed(
            api,
            "codex",
            "/tmp",
            30,
            {"ticket": ticket, "prompt": "work"},
            manager,
            poll_interval=0.01,
        )

    assert result is True
    assert cancelled.is_set()
    api.ticket_status.assert_called()
    api.report.assert_not_called()


@pytest.mark.asyncio
async def test_adapter_construction_failure_is_reported():
    api = Mock()
    manager = daemon.LeaseManager(api)
    ticket = {
        "workspace_id": "test-workspace",
        "ticket_id": "t-adapter-failure",
        "lease_id": "11111111-1111-4111-8111-111111111112",
        "lease_duration_seconds": 60,
        "worker_provider": "codex",
        "speculative": False,
    }
    with patch(
        "src.llms.native_cli.registry.get_adapter",
        side_effect=RuntimeError("adapter unavailable"),
    ):
        assert await daemon.run_claimed(
            api, "codex", "/tmp", 30, {"ticket": ticket}, manager
        ) is True
    assert api.report.call_args.args[:2] == (
        "t-adapter-failure",
        ticket["lease_id"],
    )
    assert api.report.call_args.args[2]["state"] == "failed"
    summary = api.report.call_args.args[2]["summary"]
    assert summary.startswith(
        "Local daemon codex failed: RuntimeError: adapter unavailable "
    )
    assert "correlation_id=" in summary


@pytest.mark.asyncio
async def test_missing_workspace_policy_fails_closed_without_adapter(monkeypatch):
    monkeypatch.delenv("CONDUCTOR_WORKSPACE_POLICY", raising=False)
    api = Mock()
    manager = daemon.LeaseManager(api)
    ticket = {
        "ticket_id": "t-no-policy",
        "lease_id": "11111111-1111-4111-8111-111111111113",
        "lease_duration_seconds": 60,
        "worker_provider": "codex",
        "workspace_id": "workspace-no-policy",
    }
    with patch("src.llms.native_cli.registry.get_adapter") as get_adapter:
        assert await daemon.run_claimed(
            api, "codex", "/tmp", 30, {"ticket": ticket, "prompt": "work"}, manager
        ) is True
    get_adapter.assert_not_called()
    report = api.report.call_args.args[2]
    assert report["state"] == "failed"
    assert "isolation policy" in report["summary"]


@pytest.mark.asyncio
async def test_missing_workspace_id_fails_closed_without_adapter():
    api = Mock()
    manager = daemon.LeaseManager(api)
    ticket = {
        "ticket_id": "t-no-workspace-id",
        "lease_id": "11111111-1111-4111-8111-111111111114",
        "lease_duration_seconds": 60,
        "worker_provider": "codex",
    }
    with patch("src.llms.native_cli.registry.get_adapter") as get_adapter:
        assert await daemon.run_claimed(
            api, "codex", "/tmp", 30, {"ticket": ticket, "prompt": "work"}, manager
        ) is True
    get_adapter.assert_not_called()
    assert api.report.call_args.args[2]["state"] == "failed"


def test_parse_report_accepts_a_speculative_plan():
    report = daemon.parse_report(
        completion_report_json("needs_review", "PLAN: inspect only"),
        speculative=True,
    )
    assert report["state"] == "waiting"
    assert report["summary"] == "PLAN: inspect only"


def test_parse_report_drops_a_speculative_run_with_nothing_to_do():
    report = daemon.parse_report(
        completion_report_json("done", "Nothing to fix."), speculative=True
    )
    assert report["state"] == "dropped"


def test_supported_providers_come_from_the_adapter_registry():
    assert "codex" in daemon.supported_providers()
    assert "unknown" not in daemon.supported_providers()


@pytest.mark.asyncio
async def test_provider_mismatch_is_reported_without_running_cli():
    api = Mock()
    api.claim.return_value = {
        "ticket": {
            "workspace_id": "test-workspace",
            "ticket_id": "t1",
            "lease_id": "l1",
            "lease_duration_seconds": 60,
            "worker_provider": "claude-code",
        },
        "prompt": "work",
    }
    with patch("src.llms.native_cli.registry.get_adapter") as get_adapter:
        assert await daemon.run_one(api, "codex", "/tmp", 10) is True
    get_adapter.assert_not_called()
    assert api.report.call_args.args[:2] == ("t1", "l1")
    assert api.report.call_args.args[-1]["state"] == "failed"
    assert "provider mismatch" in api.report.call_args.args[-1]["summary"]


@pytest.mark.asyncio
@pytest.mark.parametrize("speculative,read_only", [(False, False), (True, True)])
async def test_codex_uses_ticket_model_and_observe_read_only_flag(
    speculative, read_only, local_isolation_policy
):
    api = Mock()
    api.claim.return_value = {
        "ticket": {
            "workspace_id": "test-workspace",
            "ticket_id": "t1",
            "lease_id": "l1",
            "lease_duration_seconds": 60,
            "worker_provider": "codex",
            "worker_model": "gpt-test",
            "speculative": speculative,
        },
        "prompt": "work",
    }
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=completion_report_json())))
    with patch(
        "src.llms.native_cli.registry.get_adapter", return_value=adapter
    ) as get_adapter:
        assert await daemon.run_one(api, "codex", "/tmp", 10) is True
    get_adapter.assert_called_once_with(
        "codex",
        cwd=str(local_isolation_policy),
        timeout_seconds=10,
        model="gpt-test",
        isolate_user_instructions=True,
        read_only=read_only,
        outer_sandboxed=True,
    )
    adapter.run.assert_awaited_once_with("work", cwd=str(local_isolation_policy))
    assert api.report.call_args.args[:2] == ("t1", "l1")


@pytest.mark.asyncio
async def test_ticket_worktree_overrides_daemon_cwd_after_validation(
    local_isolation_policy,
):
    root = local_isolation_policy
    selected = root / "repo"
    selected.mkdir(parents=True)
    api = Mock()
    api.claim.return_value = {
        "ticket": {
            "ticket_id": "t-worktree",
            "lease_id": "l-worktree",
            "lease_duration_seconds": 60,
            "worker_provider": "codex",
            "workspace_id": "test-workspace",
            "cwd": str(selected),
        },
        "prompt": "work",
    }
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=completion_report_json())))
    with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter) as get_adapter:
        await daemon.run_one(api, "codex", "/daemon/default", 10, worktree_root=str(root))

    assert get_adapter.call_args.kwargs["cwd"] == str(selected.resolve())
    adapter.run.assert_awaited_once_with("work", cwd=str(selected.resolve()))


@pytest.mark.asyncio
async def test_zcode_http_429_is_reported_as_rate_limited(local_isolation_policy):
    from src.llms.native_cli.base import NativeCliError

    api = Mock()
    api.claim.return_value = {
        "ticket": {
            "workspace_id": "test-workspace",
            "ticket_id": "zcode-429",
            "lease_id": "lease-zcode-429",
            "lease_duration_seconds": 60,
            "worker_provider": "zcode",
            "speculative": False,
        },
        "prompt": "work",
    }
    adapter = SimpleNamespace(
        run=AsyncMock(side_effect=NativeCliError("HTTP 429 Too Many Requests"))
    )

    with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter):
        assert await daemon.run_one(api, "zcode", "/tmp", 10) is True

    report = api.report.call_args.args[-1]
    assert report["state"] == "failed"
    assert "rate_limited/worker_rate_limited" in report["summary"]
    assert "The provider rate limit was reached." in report["summary"]
    assert "HTTP 429" not in report["summary"]


@pytest.mark.asyncio
async def test_dsh_runtime_dependency_failure_uses_inner_cause_without_leaking(
    local_isolation_policy,
):
    from src.llms.native_cli.base import NativeCliError

    api = Mock()
    api.claim.return_value = {
        "ticket": {
            "workspace_id": "test-workspace",
            "ticket_id": "dsh-runtime-dependency",
            "lease_id": "lease-dsh-runtime-dependency",
            "lease_duration_seconds": 60,
            "worker_provider": "deepseek-dsh",
            "speculative": False,
        },
        "prompt": "work",
    }
    inner = NativeCliError(
        "native CLI exited -6: dyld: Library not loaded: "
        "/opt/homebrew/opt/simdjson/lib/libsimdjson.34.dylib; token=diagnostic-secret-sentinel"
    )
    outer = NativeCliError("dsh CLI failed")
    outer.__cause__ = inner
    adapter = SimpleNamespace(run=AsyncMock(side_effect=outer))

    with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter):
        assert await daemon.run_one(api, "deepseek-dsh", "/tmp", 10) is True

    report = api.report.call_args.args[-1]
    assert "crash/deepseek_runtime_dependency_missing" in report["summary"]
    assert "The DSH CLI runtime is missing a required library." in report["summary"]
    assert "diagnostic-secret-sentinel" not in report["summary"]


@pytest.mark.asyncio
async def test_zcode_missing_model_uses_specific_safe_report(local_isolation_policy):
    from src.llms.native_cli.base import NativeCliError

    api = Mock()
    api.claim.return_value = {
        "ticket": {
            "workspace_id": "test-workspace",
            "ticket_id": "zcode-model-selection",
            "lease_id": "lease-zcode-model-selection",
            "lease_duration_seconds": 60,
            "worker_provider": "zcode",
            "speculative": False,
        },
        "prompt": "work",
    }
    adapter = SimpleNamespace(
        run=AsyncMock(side_effect=NativeCliError("zcode has no selected model"))
    )

    with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter):
        assert await daemon.run_one(api, "zcode", "/tmp", 10) is True

    report = api.report.call_args.args[-1]
    assert "unknown/zcode_model_selection_missing" in report["summary"]
    assert "ZCode has no model selected in its local profile." in report["summary"]


@pytest.mark.asyncio
async def test_zcode_plan_quota_uses_safe_quota_report(local_isolation_policy):
    from src.llms.native_cli.base import NativeCliError

    api = Mock()
    api.claim.return_value = {
        "ticket": {
            "workspace_id": "test-workspace",
            "ticket_id": "zcode-plan-quota",
            "lease_id": "lease-zcode-plan-quota",
            "lease_duration_seconds": 60,
            "worker_provider": "zcode",
            "speculative": False,
        },
        "prompt": "work",
    }
    adapter = SimpleNamespace(
        run=AsyncMock(side_effect=NativeCliError("zcode plan quota exhausted"))
    )

    with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter):
        assert await daemon.run_one(api, "zcode", "/tmp", 10) is True

    report = api.report.call_args.args[-1]
    assert "quota_exhausted/zcode_plan_quota_exhausted" in report["summary"]
    assert "ZCode plan usage limit reached." in report["summary"]
    assert "provider-request-secret-sentinel" not in report["summary"]


def test_suppressed_zcode_stack_context_does_not_override_unknown_failure():
    from src.llms.native_cli.base import NativeCliError

    outer = NativeCliError("zcode CLI failed with exit 1")
    outer.__context__ = NativeCliError(
        "!function(){var e='Unauthorized';throw e}() token=context-secret-sentinel"
    )

    summary = daemon._classified_native_cli_failure("zcode", outer)

    assert "unknown/worker_unknown" in summary
    assert "auth_failed" not in summary
    assert "context-secret-sentinel" not in summary


def test_suppressed_implicit_context_does_not_override_unknown_failure():
    from src.llms.native_cli.base import NativeCliError

    try:
        try:
            raise NativeCliError("dyld: library not loaded")
        except NativeCliError:
            raise NativeCliError("adapter failed") from None
    except NativeCliError as outer:
        summary = daemon._classified_native_cli_failure("deepseek-dsh", outer)

    assert "unknown/worker_unknown" in summary
    assert "runtime_dependency_missing" not in summary


def test_explicit_native_cli_cause_remains_classifiable():
    from src.llms.native_cli.base import NativeCliError

    cause = NativeCliError("dyld: library not loaded")
    try:
        raise NativeCliError("adapter failed") from cause
    except NativeCliError as outer:
        summary = daemon._classified_native_cli_failure("deepseek-dsh", outer)

    assert "crash/deepseek_runtime_dependency_missing" in summary
    assert "The DSH CLI runtime is missing a required library." in summary


def test_minimax_runtime_dependency_failure_uses_safe_message():
    from src.llms.native_cli.base import NativeCliError

    summary = daemon._classified_native_cli_failure(
        "minimax", NativeCliError("dyld: library not loaded")
    )

    assert "crash/minimax_runtime_dependency_missing" in summary
    assert "The MiniMax CLI runtime is missing a required library." in summary
    assert "dyld:" not in summary


@pytest.mark.asyncio
@pytest.mark.parametrize("target_kind", ["outside", "missing", "file", "symlink"])
async def test_invalid_ticket_worktree_is_reported_without_starting_cli(tmp_path, target_kind):
    root = tmp_path / "worktrees"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    if target_kind == "outside":
        cwd = outside
    elif target_kind == "missing":
        cwd = root / "missing"
    elif target_kind == "file":
        cwd = root / "file"
        cwd.write_text("x", encoding="utf-8")
    else:
        cwd = root / "escape"
        cwd.symlink_to(outside, target_is_directory=True)
    api = Mock()
    api.claim.return_value = {
        "ticket": {
            "ticket_id": "t-invalid-worktree",
            "lease_id": "l-invalid-worktree",
            "lease_duration_seconds": 60,
            "worker_provider": "codex",
            "cwd": str(cwd),
        },
        "prompt": "work",
    }

    with patch("src.llms.native_cli.registry.get_adapter") as get_adapter:
        await daemon.run_one(api, "codex", "/daemon/default", 10, worktree_root=str(root))

    get_adapter.assert_not_called()
    assert api.report.call_args.args[-1]["state"] == "failed"
    assert "rejected Ticket cwd" in api.report.call_args.args[-1]["summary"]


@pytest.mark.asyncio
async def test_speculative_provider_without_read_only_contract_fails_closed():
    api = Mock()
    api.claim.return_value = {
        "ticket": {
            "workspace_id": "test-workspace",
            "ticket_id": "t1",
            "lease_id": "l1",
            "lease_duration_seconds": 60,
            "worker_provider": "kimi",
            "speculative": True,
        },
        "prompt": "plan",
    }
    with patch("src.llms.native_cli.registry.get_adapter") as get_adapter:
        assert await daemon.run_one(api, "kimi", "/tmp", 10) is True
    get_adapter.assert_not_called()
    assert api.report.call_args.args[:2] == ("t1", "l1")
    assert api.report.call_args.args[-1]["state"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider,speculative,read_only_arg",
    [
        ("codex", True, True),
        ("codex", False, False),
        ("claude-code", True, None),
        ("claude-code", False, None),
        ("cursor", True, True),
        ("cursor", False, False),
        ("grok", True, True),
        ("grok", False, True),
        ("stepfun", True, True),
        ("stepfun", False, False),
        ("zcode", True, True),
        ("zcode", False, True),
    ],
)
async def test_safe_provider_uses_only_its_enforced_permission_mode(
    provider, speculative, read_only_arg
):
    api = Mock()
    api.claim.return_value = {
        "ticket": {
            "workspace_id": "test-workspace",
            "ticket_id": "t1",
            "lease_id": "l1",
            "lease_duration_seconds": 60,
            "worker_provider": provider,
            "speculative": speculative,
        },
        "prompt": "plan",
    }
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=completion_report_json())))
    with patch(
        "src.llms.native_cli.registry.get_adapter", return_value=adapter
    ) as get_adapter:
        assert await daemon.run_one(api, provider, "/tmp", 10) is True
    call_kwargs = get_adapter.call_args.kwargs
    if read_only_arg is None:
        assert "read_only" not in call_kwargs
    else:
        assert call_kwargs["read_only"] is read_only_arg
    if provider == "codex":
        assert call_kwargs["outer_sandboxed"] is True
        assert call_kwargs["isolate_user_instructions"] is True
    elif provider in {"claude", "claude-code"}:
        assert call_kwargs["safe_mode"] is True
    else:
        assert "outer_sandboxed" not in call_kwargs
    assert api.report.call_args.args[:2] == ("t1", "l1")


@pytest.mark.asyncio
async def test_invalid_token_claim_does_not_run_cli():
    api = Mock()
    api.claim.side_effect = daemon.ApiError("POST /claim -> HTTP 401", 401, False)
    with (
        patch("src.llms.native_cli.registry.get_adapter") as get_adapter,
        pytest.raises(RuntimeError, match="401"),
    ):
        await daemon.run_one(api, "codex", "/tmp", 10)
    get_adapter.assert_not_called()


@pytest.mark.asyncio
async def test_claim_retries_same_id_after_lost_response(monkeypatch):
    api = Mock()
    ids = []

    def claim(claim_id, policy_version=""):
        ids.append(claim_id)
        if len(ids) == 1:
            raise daemon.ApiError("transport", retryable=True)
        return {"ticket": None}

    api.claim.side_effect = claim
    monkeypatch.setattr(daemon, "_BACKOFF_BASE", 0)
    monkeypatch.setattr(daemon.asyncio, "sleep", AsyncMock())
    assert await daemon.run_one(api, "codex", "/tmp", 1, claim_id="stable") is False
    assert ids == ["stable", "stable"]
    assert api.claim.call_count == 2


@pytest.mark.asyncio
async def test_cancelled_inflight_run_one_claim_reports_recovered_lease_without_cli(
    monkeypatch,
):
    baseline = asyncio.all_tasks()
    entered = threading.Event()
    release = threading.Event()
    api = Mock()
    api.heartbeat.return_value = {"leases": []}

    def claim(_claim_id, policy_version=""):
        entered.set()
        assert release.wait(timeout=2)
        return {
            "ticket": {
                "workspace_id": "test-workspace",
                "ticket_id": "t-claim-cancel",
                "lease_id": "lease-claim-cancel",
                "lease_duration_seconds": 60,
                "worker_provider": "codex",
            },
            "prompt": "work",
        }

    api.claim.side_effect = claim
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=completion_report_json())))
    with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter):
        task = asyncio.create_task(daemon.run_one(api, "codex", "/tmp", 10))
        await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), timeout=2)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    adapter.run.assert_not_awaited()
    api.report.assert_called_once()
    assert api.report.call_args.args[:2] == ("t-claim-cancel", "lease-claim-cancel")
    assert api.report.call_args.args[2]["state"] == "failed"
    await asyncio.sleep(0)
    assert not {item for item in asyncio.all_tasks() - baseline if not item.done()}


@pytest.mark.asyncio
async def test_cancelled_claim_uses_expiry_fallback_when_response_misses_drain_bound(
    monkeypatch,
):
    baseline = asyncio.all_tasks()
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    monkeypatch.setattr(daemon, "_CANCEL_DRAIN_MAX", 0.01)
    api = Mock()

    def claim(_claim_id, policy_version=""):
        entered.set()
        try:
            assert release.wait(timeout=2)
            return {
                "ticket": {
                    "workspace_id": "test-workspace",
                    "ticket_id": "t-claim-expiry",
                    "lease_id": "lease-claim-expiry",
                    "lease_duration_seconds": 60,
                    "worker_provider": "codex",
                },
                "prompt": "work",
            }
        finally:
            finished.set()

    api.claim.side_effect = claim
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=completion_report_json())))
    try:
        with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter):
            task = asyncio.create_task(daemon.run_one(api, "codex", "/tmp", 10))
            await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=0.5)

        adapter.run.assert_not_awaited()
        api.report.assert_not_called()
    finally:
        release.set()
        assert await asyncio.wait_for(asyncio.to_thread(finished.wait, 1), timeout=2)
    await asyncio.sleep(0)
    assert not {item for item in asyncio.all_tasks() - baseline if not item.done()}


@pytest.mark.asyncio
async def test_cancelled_inflight_main_loop_claim_reports_recovered_lease(
    monkeypatch, tmp_path
):
    baseline = asyncio.all_tasks()
    entered = threading.Event()
    release = threading.Event()
    api = Mock()
    api.heartbeat.return_value = {"leases": []}

    def claim(_claim_id, policy_version=""):
        entered.set()
        assert release.wait(timeout=2)
        return {
            "ticket": {
                "workspace_id": "test-workspace",
                "ticket_id": "t-main-cancel",
                "lease_id": "lease-main-cancel",
                "lease_duration_seconds": 60,
                "worker_provider": "codex",
            },
            "prompt": "work",
        }

    api.claim.side_effect = claim
    monkeypatch.setattr(daemon, "supported_providers", lambda: {"codex"})
    monkeypatch.setattr(daemon, "Api", Mock(return_value=api))
    args = SimpleNamespace(
        token="token",
        provider="codex",
        cwd=str(tmp_path),
        base_url="http://mock",
        once=False,
        max_concurrency=1,
        heartbeat_interval=10,
        timeout=60,
        interval=0.01,
    )
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=completion_report_json())))
    with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter):
        task = asyncio.create_task(daemon.main_async(args))
        await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), timeout=2)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    adapter.run.assert_not_awaited()
    api.report.assert_called_once()
    assert api.report.call_args.args[:2] == ("t-main-cancel", "lease-main-cancel")
    assert api.report.call_args.args[2]["state"] == "failed"
    await asyncio.sleep(0)
    assert not {item for item in asyncio.all_tasks() - baseline if not item.done()}


@pytest.mark.asyncio
async def test_retry_policy_survives_more_than_five_failures(monkeypatch):
    calls = 0

    def call():
        nonlocal calls
        calls += 1
        if calls <= 7:
            raise daemon.ApiError("temporary backend error", 503, True)
        return "ok"

    delays = []

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(daemon.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(daemon.random, "uniform", lambda low, high: high)
    assert await daemon._retry_call(call) == "ok"
    assert calls == 8
    assert len(delays) == 7
    assert all(0 <= delay <= daemon._BACKOFF_MAX for delay in delays)


@pytest.mark.asyncio
async def test_retry_does_not_start_after_deadline():
    call = Mock()
    with pytest.raises(daemon.LeaseLost):
        await daemon._retry_call(call, deadline=daemon.time.monotonic() - 1)
    call.assert_not_called()


@pytest.mark.parametrize(
    "status,retryable",
    [(429, True), (500, True), (503, True), (400, False), (401, False)],
)
def test_http_errors_are_classified_without_exposing_response_body(
    monkeypatch, status, retryable
):
    error = urllib.error.HTTPError(
        "https://secret.invalid/path", status, "failure", {}, None
    )
    error.fp = SimpleNamespace(read=lambda *_: b"secret response data")
    monkeypatch.setattr(daemon.urllib.request, "urlopen", Mock(side_effect=error))
    api = daemon.Api("https://service.invalid", "token", timeout=3)
    with pytest.raises(daemon.ApiError) as captured:
        api._call("POST", "/resource", {"payload": "private"}, timeout=2)
    assert captured.value.retryable is retryable
    assert "secret" not in str(captured.value)
    assert daemon.urllib.request.urlopen.call_args.kwargs["timeout"] == 2


def test_transport_errors_are_retryable(monkeypatch):
    monkeypatch.setattr(
        daemon.urllib.request,
        "urlopen",
        Mock(side_effect=urllib.error.URLError("offline")),
    )
    with pytest.raises(daemon.ApiError) as captured:
        daemon.Api("https://service.invalid", "token")._call("POST", "/resource")
    assert captured.value.retryable is True


@pytest.mark.asyncio
async def test_report_retries_without_rerunning_cli(monkeypatch):
    api = Mock()
    api.claim.return_value = {
        "ticket": {
            "workspace_id": "test-workspace",
            "ticket_id": "t1",
            "lease_id": "l1",
            "lease_duration_seconds": 60,
            "worker_provider": "codex",
        },
        "prompt": "work",
    }
    api.report.side_effect = [daemon.ApiError("lost response", retryable=True), None]
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=completion_report_json())))
    monkeypatch.setattr(daemon, "_BACKOFF_BASE", 0)
    with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter):
        assert await daemon.run_one(api, "codex", "/tmp", 1) is True
    adapter.run.assert_awaited_once()
    assert api.report.call_count == 2
    assert api.report.call_args.args[:2] == ("t1", "l1")


@pytest.mark.asyncio
async def test_cancelled_inflight_report_drains_fenced_request_and_preserves_outcome():
    baseline = asyncio.all_tasks()
    entered = threading.Event()
    release = threading.Event()
    api = Mock()
    api.claim.return_value = {
        "ticket": {
            "workspace_id": "test-workspace",
            "ticket_id": "t-report-cancel",
            "lease_id": "lease-report-cancel",
            "lease_duration_seconds": 60,
            "worker_provider": "codex",
        },
        "prompt": "work",
    }

    def report(*_args, **_kwargs):
        entered.set()
        assert release.wait(timeout=2)
        return {"ticket_id": "t-report-cancel", "state": "ready"}

    api.report.side_effect = report
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=completion_report_json())))
    with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter):
        task = asyncio.create_task(daemon.run_one(api, "codex", "/tmp", 10))
        await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), timeout=2)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    adapter.run.assert_awaited_once()
    api.report.assert_called_once()
    assert api.report.call_args.args[:2] == ("t-report-cancel", "lease-report-cancel")
    assert api.report.call_args.args[2]["state"] == "ready"
    await asyncio.sleep(0)
    assert not {item for item in asyncio.all_tasks() - baseline if not item.done()}


@pytest.mark.asyncio
async def test_heartbeat_wakes_immediately_for_short_replayed_lease():
    api = Mock()
    renewed = asyncio.Event()
    loop = asyncio.get_running_loop()

    def heartbeat(rows, **_kwargs):
        loop.call_soon_threadsafe(renewed.set)
        return {"leases": [{**rows[0], "renewed": True, "lease_duration_seconds": 90}]}

    api.heartbeat.side_effect = heartbeat
    manager = daemon.LeaseManager(api, interval=10)
    heartbeat_task = asyncio.create_task(manager.run())
    await asyncio.sleep(0.01)
    lease = daemon.ActiveLease(
        "t1", "l1", daemon.time.monotonic() + 1, 1, asyncio.Event()
    )
    await manager.add(lease)
    await asyncio.wait_for(renewed.wait(), timeout=0.2)
    await manager.close()
    await asyncio.gather(heartbeat_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_report_retries_after_heartbeat_extends_initial_ttl(monkeypatch):
    api = Mock()
    api.heartbeat.side_effect = lambda rows, **_kwargs: {
        "leases": [{**rows[0], "renewed": True, "lease_duration_seconds": 0.5}]
    }
    failures = 0

    def report(*_args, **_kwargs):
        nonlocal failures
        failures += 1
        if failures <= 3:
            raise daemon.ApiError("temporary", 503, True)

    api.report.side_effect = report
    api.claim.return_value = {
        "ticket": {
            "workspace_id": "test-workspace",
            "ticket_id": "t1",
            "lease_id": "l1",
            "lease_duration_seconds": 0.25,
            "worker_provider": "codex",
        },
        "prompt": "work",
    }

    async def slow_cli(*_args, **_kwargs):
        await asyncio.sleep(0.3)
        return SimpleNamespace(text=completion_report_json())

    adapter = SimpleNamespace(run=AsyncMock(side_effect=slow_cli))
    monkeypatch.setattr(daemon, "_BACKOFF_BASE", 0.01)
    manager = daemon.LeaseManager(api, interval=0.01)
    heartbeat_task = asyncio.create_task(manager.run())
    with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter):
        await daemon.run_claimed(
            api,
            "codex",
            "/tmp",
            1,
            api.claim.return_value,
            manager,
            daemon.time.monotonic(),
        )
    await manager.close()
    await asyncio.gather(heartbeat_task, return_exceptions=True)
    assert api.heartbeat.called
    assert api.report.call_count == 4
    adapter.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_heartbeat_is_not_blocked_by_cli_and_tickets_run_independently():
    api = Mock()
    api.heartbeat.return_value = {
        "leases": [
            {"ticket_id": "t1", "lease_id": "lease-t1", "renewed": True},
            {"ticket_id": "t2", "lease_id": "lease-t2", "renewed": True},
        ]
    }
    manager = daemon.LeaseManager(api, interval=0.01)
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def slow_cli(*_args, **_kwargs):
        entered.set()
        await gate.wait()
        return SimpleNamespace(text=completion_report_json())

    adapter = SimpleNamespace(run=AsyncMock(side_effect=slow_cli))

    def payload(ticket):
        return {
            "ticket": {
                "workspace_id": "test-workspace",
                "ticket_id": ticket,
                "lease_id": f"lease-{ticket}",
                "lease_duration_seconds": 60,
                "worker_provider": "codex",
            },
            "prompt": "work",
        }

    heartbeat = asyncio.create_task(manager.run())
    with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter):
        first = asyncio.create_task(
            daemon.run_claimed(api, "codex", "/tmp", 30, payload("t1"), manager)
        )
        second = asyncio.create_task(
            daemon.run_claimed(api, "codex", "/tmp", 30, payload("t2"), manager)
        )
        await entered.wait()
        for _ in range(20):
            if api.heartbeat.called:
                break
            await asyncio.sleep(0.01)
        assert api.heartbeat.called
        assert len(manager.active) == 2
        gate.set()
        await asyncio.gather(first, second)
    await manager.close()
    heartbeat.cancel()
    await asyncio.gather(heartbeat, return_exceptions=True)


@pytest.mark.asyncio
async def test_lease_loss_cancels_cli_and_prevents_report():
    api = Mock()
    manager = daemon.LeaseManager(api)
    started = asyncio.Event()

    async def cli(*_args, **_kwargs):
        started.set()
        await asyncio.Future()

    adapter = SimpleNamespace(run=AsyncMock(side_effect=cli))
    payload = {
        "ticket": {
            "workspace_id": "test-workspace",
            "ticket_id": "t1",
            "lease_id": "l1",
            "lease_duration_seconds": 60,
            "worker_provider": "codex",
        },
        "prompt": "work",
    }
    with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter):
        task = asyncio.create_task(
            daemon.run_claimed(api, "codex", "/tmp", 30, payload, manager)
        )
        await started.wait()
        manager.active["t1"].lost.set()
        await task
    adapter.run.assert_awaited_once()
    api.report.assert_not_called()


@pytest.mark.asyncio
async def test_expiry_cancels_cli_and_skips_report():
    api = Mock()
    manager = daemon.LeaseManager(api)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def cli(*_args, **_kwargs):
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    adapter = SimpleNamespace(run=AsyncMock(side_effect=cli))
    payload = {
        "ticket": {
            "workspace_id": "test-workspace",
            "ticket_id": "t1",
            "lease_id": "l1",
            "lease_duration_seconds": 0.05,
            "worker_provider": "codex",
        },
        "prompt": "work",
    }
    with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter):
        await daemon.run_claimed(api, "codex", "/tmp", 30, payload, manager)
    assert started.is_set() and cancelled.is_set()
    api.report.assert_not_called()


@pytest.mark.asyncio
async def test_permanent_report_auth_error_fails_once_mode(monkeypatch, tmp_path):
    api = Mock()
    api.claim.return_value = {
        "ticket": {
            "workspace_id": "test-workspace",
            "ticket_id": "t1",
            "lease_id": "l1",
            "lease_duration_seconds": 60,
            "worker_provider": "codex",
        },
        "prompt": "work",
    }
    api.heartbeat.return_value = {
        "leases": [
            {
                "ticket_id": "t1",
                "lease_id": "l1",
                "renewed": True,
                "lease_duration_seconds": 60,
            }
        ]
    }
    api.report.side_effect = daemon.ApiError("unauthorized", 401, False)
    monkeypatch.setattr(daemon, "supported_providers", lambda: {"codex"})
    monkeypatch.setattr(daemon, "Api", Mock(return_value=api))
    args = SimpleNamespace(
        token="token",
        provider="codex",
        cwd=str(tmp_path),
        base_url="http://mock",
        once=True,
        max_concurrency=4,
        heartbeat_interval=10,
        timeout=60,
        interval=0.01,
    )
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=completion_report_json())))
    with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter):
        assert await daemon.main_async(args) == 2


@pytest.mark.asyncio
async def test_main_async_claims_up_to_configured_concurrency(monkeypatch, tmp_path):
    baseline_tasks = asyncio.all_tasks()
    shared_workspace = tmp_path / "shared-workspace"
    shared_workspace.mkdir()
    workspace_roots = {"test-workspace": shared_workspace}
    monkeypatch.setattr(
        "src.llms.native_cli.isolation.read_workspace_policy",
        lambda _policy, workspace_id, _isolation_root: {
            "root": str(workspace_roots[workspace_id]),
            "env": set(),
            "runtime_read_paths": (),
        },
    )
    api = Mock()
    payloads = [
        {
            "ticket": {
                "workspace_id": "test-workspace",
                "ticket_id": f"t{i}",
                "lease_id": f"l{i}",
                "lease_duration_seconds": 60,
                "worker_provider": "codex",
            },
            "prompt": "work",
        }
        for i in range(2)
    ]
    api.claim.side_effect = [*payloads, {"ticket": None}]

    def renew_submitted_leases(leases, **_kwargs):
        return {
            "leases": [
                {
                    **lease,
                    "renewed": True,
                    "lease_duration_seconds": 60,
                }
                for lease in leases or []
            ]
        }

    api.heartbeat.side_effect = renew_submitted_leases
    reports_after_cli_stop = []
    def report_after_stop(*_args, **_kwargs):
        reports_after_cli_stop.append(cancelled.is_set())
        return {}

    api.report.side_effect = report_after_stop
    monkeypatch.setattr(daemon, "supported_providers", lambda: {"codex"})
    monkeypatch.setattr(daemon, "Api", Mock(return_value=api))
    entered = set()
    both_running = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow_cli(_prompt, *, cwd):
        entered.add(asyncio.current_task())
        if len(entered) == 2:
            both_running.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    adapter = SimpleNamespace(run=AsyncMock(side_effect=slow_cli))
    args = SimpleNamespace(
        token="token",
        provider="codex",
        cwd=str(tmp_path),
        base_url="http://mock",
        once=False,
        max_concurrency=2,
        heartbeat_interval=10,
        timeout=60,
        interval=0.01,
    )
    with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter):
        daemon_task = asyncio.create_task(daemon.main_async(args))
        await asyncio.wait_for(both_running.wait(), timeout=2)
        assert len(entered) == 2
        daemon_task.cancel()
        await asyncio.gather(daemon_task, return_exceptions=True)
    assert api.claim.call_count >= 2
    assert cancelled.is_set()
    assert reports_after_cli_stop and all(reports_after_cli_stop)
    cancelled_reports = [
        call for call in api.report.call_args_list if call.args[2]["state"] == "failed"
    ]
    assert {call.args[:2] for call in cancelled_reports} == {("t0", "l0"), ("t1", "l1")}
    await asyncio.sleep(0)
    assert not {
        task for task in asyncio.all_tasks() - baseline_tasks if not task.done()
    }


def test_concurrency_alias_and_bounds(monkeypatch):
    captured = {}

    def capture_run(coro):
        captured["args"] = coro.cr_frame.f_locals["args"]
        coro.close()
        return 0

    monkeypatch.setattr(daemon.asyncio, "run", capture_run)
    assert daemon.main(["--provider", "codex", "--concurrency", "3"]) == 0
    assert captured["args"].max_concurrency == 3
    with pytest.raises(SystemExit):
        daemon.main(["--provider", "codex", "--max-concurrency", "33"])


@pytest.mark.asyncio
async def test_permanent_auth_error_is_not_retried(monkeypatch):
    api = Mock()
    api.claim.side_effect = daemon.ApiError("unauthorized", 401, False)
    monkeypatch.setattr(daemon.asyncio, "sleep", AsyncMock())
    with pytest.raises(daemon.ApiError):
        await daemon.run_one(api, "codex", "/tmp", 1)
    api.claim.assert_called_once()


@pytest.mark.asyncio
async def test_missing_token_exits_before_creating_api_or_running_cli(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("CONDUCTOR_DAEMON_TOKEN", raising=False)
    args = SimpleNamespace(token="", provider="codex", cwd=str(tmp_path), base_url="x")
    with (
        patch.object(daemon, "Api") as api,
        patch("src.llms.native_cli.registry.get_adapter") as get_adapter,
    ):
        assert await daemon.main_async(args) == 2
    api.assert_not_called()
    get_adapter.assert_not_called()


@pytest.mark.asyncio
async def test_unsupported_provider_exits_before_creating_api_or_running_cli(tmp_path):
    args = SimpleNamespace(
        token="token",
        provider="unknown",
        cwd=str(tmp_path),
        base_url="x",
        max_concurrency=1,
    )
    with (
        patch.object(daemon, "supported_providers", return_value={"codex"}),
        patch.object(daemon, "Api") as api,
        patch("src.llms.native_cli.registry.get_adapter") as get_adapter,
    ):
        assert await daemon.main_async(args) == 2
    api.assert_not_called()
    get_adapter.assert_not_called()


@pytest.mark.asyncio
async def test_claim_with_breaker_checks_status_frequently_during_wait(monkeypatch):
    api = Mock()
    breaker = Mock()
    status_checks = []

    def status_fn():
        status_checks.append(time.monotonic())
        return {"retry_in_seconds": 0.05}

    breaker.acquire.return_value = None
    breaker.status.side_effect = status_fn
    api.heartbeat.return_value = None

    monkeypatch.setattr(daemon, "_BACKOFF_BASE", 0)

    acquired = False

    def acquire_after_delay():
        nonlocal acquired
        if len(status_checks) >= 3:
            acquired = True
            return Mock()
        return None

    breaker.acquire.side_effect = acquire_after_delay
    payload = {"ticket": {"ticket_id": "t1"}}
    api.claim.return_value = payload

    result = await daemon._claim_with_breaker(api, breaker, "claim-1")
    assert result[0] == payload
    assert len(status_checks) >= 3


def test_persist_breaker_status_validates_private_path(tmp_path):
    from src.conductor_cli.provider_breaker import ProviderCircuitBreaker

    breaker = ProviderCircuitBreaker()
    provider = "codex"

    status_file = str(tmp_path / "status.json")
    result = daemon._persist_breaker_status(status_file, provider, breaker)
    assert result is True
    assert Path(status_file).exists()


def test_persist_breaker_status_rejects_unsafe_path(tmp_path):
    from src.conductor_cli.provider_breaker import ProviderCircuitBreaker

    breaker = ProviderCircuitBreaker()
    provider = "codex"

    unsafe_path = "/tmp/../../../etc/passwd"
    result = daemon._persist_breaker_status(unsafe_path, provider, breaker)
    assert result is False


def test_persist_breaker_status_returns_false_on_missing_path():
    from src.conductor_cli.provider_breaker import ProviderCircuitBreaker

    breaker = ProviderCircuitBreaker()
    provider = "codex"

    result = daemon._persist_breaker_status("", provider, breaker)
    assert result is False


def test_persist_breaker_status_accepts_relative_path(tmp_path, monkeypatch):
    from src.conductor_cli.provider_breaker import ProviderCircuitBreaker

    # Change to tmp_path so relative paths work
    monkeypatch.chdir(tmp_path)

    breaker = ProviderCircuitBreaker()
    provider = "codex"

    # Use a relative path
    result = daemon._persist_breaker_status("status.json", provider, breaker)
    assert result is True
    assert Path("status.json").exists()


@pytest.mark.asyncio
async def test_main_async_skips_status_writes_on_no_op(monkeypatch, tmp_path):
    api = Mock()
    api.claim.return_value = {"ticket": None}
    api.heartbeat.return_value = {"leases": []}
    status_file = tmp_path / "status.json"

    monkeypatch.setattr(daemon, "supported_providers", lambda: {"codex"})
    monkeypatch.setattr(daemon, "Api", Mock(return_value=api))

    write_count = 0
    original_persist = daemon._persist_breaker_status

    def track_writes(path, provider, breaker):
        nonlocal write_count
        result = original_persist(path, provider, breaker)
        if result:
            write_count += 1
        return result

    monkeypatch.setattr(daemon, "_persist_breaker_status", track_writes)

    args = SimpleNamespace(
        token="token",
        provider="codex",
        cwd=str(tmp_path),
        base_url="http://mock",
        once=False,
        max_concurrency=1,
        heartbeat_interval=10,
        timeout=60,
        interval=0.01,
        status_file=str(status_file),
    )

    task = asyncio.create_task(daemon.main_async(args))
    await asyncio.sleep(0.1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert write_count >= 1


def test_persist_breaker_status_replaces_symlink_without_overwriting_target(tmp_path):
    """Symlink targets should not be overwritten; the symlink itself is replaced."""
    from src.conductor_cli.provider_breaker import ProviderCircuitBreaker

    breaker = ProviderCircuitBreaker()
    provider = "codex"

    # Create a target file with some content
    target_file = tmp_path / "target_data.txt"
    target_file.write_text("original target content")

    # Create a symlink pointing to the target
    status_file = tmp_path / "status.json"
    status_file.symlink_to(target_file)

    # Verify symlink exists and points to target
    assert status_file.is_symlink()
    assert status_file.resolve() == target_file

    # Persist status through the symlink
    result = daemon._persist_breaker_status(str(status_file), provider, breaker)
    assert result is True

    # Verify the symlink was replaced (no longer a symlink)
    assert not status_file.is_symlink()
    assert status_file.is_file()

    # Verify the status file contains JSON data (not the original target content)
    import json
    status_data = json.loads(status_file.read_text())
    assert status_data["provider"] == "codex"
    assert "updated_at" in status_data

    # Verify the original target file is unchanged
    assert target_file.read_text() == "original target content"


def test_api_stop_ack_posts_the_lease_and_non_secret_process_evidence():
    api = daemon.Api("https://conductor.example", "daemon-secret")
    with patch.object(api, "_call", return_value={"stop": {}}) as call:
        api.stop_ack(
            "ticket-123",
            "11111111-1111-4111-8111-111111111111",
            process_gone=True,
            evidence={"signal": "SIGKILL", "pid": 7, "pgid": 7},
        )
    call.assert_called_once_with(
        "POST",
        "/api/v1/autonomy/daemon/tickets/ticket-123/stop/ack",
        {
            "lease_id": "11111111-1111-4111-8111-111111111111",
            "process_gone": True,
            "signal": "SIGKILL",
            "pid": 7,
            "pgid": 7,
        },
        timeout=None,
    )


def _remote_stop_status(verification: str = "pending") -> dict:
    return {
        "status": "cancelled",
        "stop": {
            "verification": verification,
            "deadline_at": (
                datetime.now(timezone.utc) + timedelta(seconds=30)
            ).isoformat(),
        },
    }


@pytest.mark.asyncio
async def test_run_claimed_acknowledges_a_remote_stop_after_the_cli_exits():
    api = Mock()
    api.ticket_status.return_value = _remote_stop_status()
    manager = daemon.LeaseManager(api)
    cancelled = asyncio.Event()

    async def slow_run(prompt, cwd=None):
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    ticket = {
        "workspace_id": "test-workspace",
        "ticket_id": "t-stop",
        "lease_id": "11111111-1111-4111-8111-111111111113",
        "lease_duration_seconds": 60,
        "worker_provider": "codex",
        "speculative": False,
    }
    with patch(
        "src.llms.native_cli.registry.get_adapter",
        return_value=SimpleNamespace(run=slow_run),
    ):
        result = await daemon.run_claimed(
            api,
            "codex",
            "/tmp",
            30,
            {"ticket": ticket, "prompt": "work"},
            manager,
            poll_interval=0.01,
        )

    assert result is True
    assert cancelled.is_set()
    api.report.assert_not_called()
    api.stop_ack.assert_called_once()
    args, kwargs = api.stop_ack.call_args
    assert args == ("t-stop", ticket["lease_id"])
    assert kwargs["process_gone"] is True
    assert kwargs["evidence"]["signal"] == "SIGKILL"
    assert kwargs["evidence"]["processes"] == []


@pytest.mark.asyncio
async def test_stop_ack_request_timeout_fits_the_remaining_budget():
    timeouts: list[float] = []

    def stop_ack(*args, **kwargs):
        timeouts.append(kwargs["timeout"])
        if len(timeouts) == 1:
            raise daemon.ApiError("transport", retryable=True)
        return {"stop": {}}

    api = Mock()
    api.stop_ack.side_effect = stop_ack
    tracker = SimpleNamespace(gone=lambda: True, evidence=lambda: {"processes": []})

    assert await daemon._acknowledge_stop(
        api, "t-stop", "lease-1", tracker, _remote_stop_status()
    ) is True

    assert len(timeouts) == 2
    assert all(value is not None and 0 < value <= daemon._STOP_ACK_MAX for value in timeouts)
    # The retry spends part of the same budget, so its request timeout shrinks.
    assert timeouts[1] < timeouts[0]


@pytest.mark.asyncio
async def test_run_claimed_reports_a_process_that_did_not_exit(monkeypatch):
    class _LiveTracker:
        def gone(self):
            return False

        def evidence(self):
            return {
                "processes": [
                    {"pid": 7, "pgid": 7, "exited": False, "alive": True}
                ]
            }

    monkeypatch.setattr(daemon, "_PROCESS_GONE_GRACE", 0.0)
    api = Mock()
    api.ticket_status.return_value = _remote_stop_status()
    manager = daemon.LeaseManager(api)

    async def slow_run(prompt, cwd=None):
        await asyncio.sleep(60)

    ticket = {
        "workspace_id": "test-workspace",
        "ticket_id": "t-stop-live",
        "lease_id": "11111111-1111-4111-8111-111111111114",
        "lease_duration_seconds": 60,
        "worker_provider": "codex",
        "speculative": False,
    }
    with (
        patch(
            "src.llms.native_cli.registry.get_adapter",
            return_value=SimpleNamespace(run=slow_run),
        ),
        patch("src.llms.native_cli.process.ProcessTracker", _LiveTracker),
    ):
        assert await daemon.run_claimed(
            api,
            "codex",
            "/tmp",
            30,
            {"ticket": ticket, "prompt": "work"},
            manager,
            poll_interval=0.01,
        ) is True

    args, kwargs = api.stop_ack.call_args
    assert kwargs["process_gone"] is False
    assert kwargs["evidence"]["processes"][0]["alive"] is True


@pytest.mark.asyncio
async def test_run_claimed_does_not_retry_a_stale_stop_ack():
    api = Mock()
    api.ticket_status.return_value = _remote_stop_status()
    api.stop_ack.side_effect = daemon.ApiError("stale", status=409)
    manager = daemon.LeaseManager(api)

    async def slow_run(prompt, cwd=None):
        await asyncio.sleep(60)

    ticket = {
        "workspace_id": "test-workspace",
        "ticket_id": "t-stop-stale",
        "lease_id": "11111111-1111-4111-8111-111111111115",
        "lease_duration_seconds": 60,
        "worker_provider": "codex",
        "speculative": False,
    }
    with patch(
        "src.llms.native_cli.registry.get_adapter",
        return_value=SimpleNamespace(run=slow_run),
    ):
        assert await daemon.run_claimed(
            api,
            "codex",
            "/tmp",
            30,
            {"ticket": ticket, "prompt": "work"},
            manager,
            poll_interval=0.01,
        ) is True

    api.stop_ack.assert_called_once()
    api.report.assert_not_called()


@pytest.mark.asyncio
async def test_lease_loss_still_acknowledges_a_pending_stop():
    api = Mock()
    api.ticket_status.return_value = _remote_stop_status()
    manager = daemon.LeaseManager(api)
    cancelled = asyncio.Event()

    async def slow_run(prompt, cwd=None):
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    ticket = {
        "workspace_id": "test-workspace",
        "ticket_id": "t-stop-expiry",
        "lease_id": "11111111-1111-4111-8111-111111111116",
        "lease_duration_seconds": 0.05,
        "worker_provider": "codex",
        "speculative": False,
    }
    with patch(
        "src.llms.native_cli.registry.get_adapter",
        return_value=SimpleNamespace(run=slow_run),
    ):
        assert await daemon.run_claimed(
            api,
            "codex",
            "/tmp",
            30,
            {"ticket": ticket, "prompt": "work"},
            manager,
            poll_interval=60,
        ) is True

    assert cancelled.is_set()
    api.stop_ack.assert_called_once()
    api.report.assert_not_called()


def _git_ticket_workspace(workspace):
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    for key, value in (("user.name", "test"), ("user.email", "test@example.invalid")):
        subprocess.run(["git", "-C", str(workspace), "config", key, value], check=True)
    (workspace / "tracked.txt").write_text("before")
    subprocess.run(["git", "-C", str(workspace), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(workspace), "commit", "-qm", "baseline"], check=True)


async def _run_completion_ticket(workspace, adapter, ticket_id):
    api = Mock()
    manager = daemon.LeaseManager(api)
    ticket = {
        "workspace_id": "test-workspace",
        "ticket_id": ticket_id,
        "lease_id": "11111111-1111-4111-8111-111111111133",
        "lease_duration_seconds": 60,
        "worker_provider": "codex",
    }
    with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter):
        await daemon.run_claimed(
            api,
            "codex",
            str(workspace),
            30,
            {"ticket": ticket, "prompt": "work"},
            manager,
        )
    return api.report.call_args.args[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("commits", [(), ("abc1234",)], ids=["no-commits", "commits"])
async def test_done_report_without_a_new_commit_is_blocked_whatever_it_lists(
    local_isolation_policy, commits
):
    """Regression: a clean workspace with no new commit delivered nothing."""
    workspace = local_isolation_policy
    _git_ticket_workspace(workspace)
    adapter = SimpleNamespace(
        run=AsyncMock(
            return_value=SimpleNamespace(text=completion_report_json(commits=commits))
        )
    )

    report = await _run_completion_ticket(workspace, adapter, f"t-nocommit-{len(commits)}")

    assert report["state"] != "ready"
    if commits:
        assert report["state"] == "blocked"
        assert "confirm a delivered commit" in report["question"]
        body = json.loads(report["body"])
        assert body["disagreement"] is True
        assert body["disagreement_reason"] == "git_not_delivered"
    else:
        # Nothing claimed and nothing changed: a person or the Router decides.
        assert report["state"] == "waiting"
        assert report["attention_reason"] == "completion_no_change"


def _codex_events(*messages):
    events = [{"type": "thread.started", "thread_id": "codex-thread"}]
    events.extend(
        {
            "type": "item.completed",
            "item": {"id": f"item_{index}", "type": "agent_message", "text": text},
        }
        for index, text in enumerate(messages)
    )
    events.append({"type": "turn.completed", "usage": {}})
    return events


@pytest.mark.asyncio
async def test_codex_progress_message_before_the_report_still_settles_ready(
    local_isolation_policy,
):
    """Regression: a progress agent_message must not be joined to the report."""
    from src.llms.native_cli.base import NativeCliResult

    workspace = local_isolation_policy
    _git_ticket_workspace(workspace)
    report_text = completion_report_json(commits=("worker change",))
    progress = "Working on it: reading the tests first."

    async def worker_run(*_args, **_kwargs):
        (workspace / "tracked.txt").write_text("after")
        subprocess.run(["git", "-C", str(workspace), "commit", "-qam", "work"], check=True)
        return NativeCliResult(
            provider="codex",
            text=f"{progress}\n{report_text}",
            raw=tuple(_codex_events(progress, report_text)),
        )

    adapter = SimpleNamespace(run=AsyncMock(side_effect=worker_run))
    report = await _run_completion_ticket(workspace, adapter, "t-progress")

    assert report["state"] == "ready"
    assert "attention_reason" not in report
    assert json.loads(report["body"])["reconciled_state"] == "ready"


def _stream_codex_lines(monkeypatch, events):
    """Script the streamed Codex exec transcript; no CLI process runs."""

    async def fake_stream(_invocation, **_kwargs):
        for event in events:
            yield json.dumps(event)

    monkeypatch.setenv("CONDUCTOR_CODEX_APP_SERVER", "0")
    monkeypatch.setattr("src.llms.native_cli.process.stream_process", fake_stream)


@pytest.mark.asyncio
async def test_successful_codex_turn_without_an_agent_message_waits(
    local_isolation_policy, monkeypatch
):
    """Regression: no final message is a missing report, not a CLI failure."""
    from src.llms.native_cli import codex

    _stream_codex_lines(
        monkeypatch,
        [
            {"type": "thread.started", "thread_id": "codex-thread"},
            {"type": "turn.completed", "usage": {}},
        ],
    )
    adapter = codex.CodexExecAdapter(binary="/fake/codex")

    report = await _run_completion_ticket(local_isolation_policy, adapter, "t-nofinal")

    assert report["state"] == "waiting"
    assert report["attention_reason"] == "completion_report_missing"
    assert report["summary"] == "No valid completion report was received."


@pytest.mark.asyncio
async def test_streamed_codex_progress_then_report_settles_from_the_report(
    local_isolation_policy, monkeypatch
):
    from src.llms.native_cli import codex

    report_text = completion_report_json("needs_review", "Please review the plan.")
    _stream_codex_lines(
        monkeypatch, _codex_events("Working on it: reading the tests.", report_text)
    )
    adapter = codex.CodexExecAdapter(binary="/fake/codex")

    report = await _run_completion_ticket(local_isolation_policy, adapter, "t-stream")

    assert report["state"] == "waiting"
    assert "attention_reason" not in report
    assert report["summary"] == "Please review the plan."


@pytest.mark.asyncio
async def test_a_failed_codex_turn_without_an_agent_message_still_fails(
    local_isolation_policy, monkeypatch
):
    from src.llms.native_cli import codex

    _stream_codex_lines(
        monkeypatch,
        [
            {"type": "thread.started", "thread_id": "codex-thread"},
            {"type": "error", "message": "You've hit your usage limit."},
            {"type": "turn.failed", "error": {"message": "usage limit"}},
        ],
    )
    adapter = codex.CodexExecAdapter(binary="/fake/codex")

    report = await _run_completion_ticket(local_isolation_policy, adapter, "t-turnfail")

    assert report["state"] == "failed"
    assert "attention_reason" not in report


@pytest.mark.asyncio
async def test_a_restarted_daemon_does_not_block_a_done_after_an_earlier_commit(
    local_isolation_policy,
):
    """Regression: run 1 commits, the daemon restarts, run 2 reports done."""
    from src.server.services.autonomy import git_delivery

    workspace = local_isolation_policy
    _git_ticket_workspace(workspace)

    async def run_one(*_args, **_kwargs):
        (workspace / "tracked.txt").write_text("run 1")
        subprocess.run(["git", "-C", str(workspace), "commit", "-qam", "run 1"], check=True)
        return SimpleNamespace(
            text=completion_report_json("partial", "Half done.", commits=("run 1",))
        )

    first = await _run_completion_ticket(
        workspace, SimpleNamespace(run=AsyncMock(side_effect=run_one)), "t-restart"
    )
    assert first["state"] == "waiting"

    git_delivery._TICKET_BASELINES.clear()  # the daemon process restarted
    api = Mock()
    manager = daemon.LeaseManager(api)
    ticket = {
        "workspace_id": "test-workspace",
        "ticket_id": "t-restart",
        "lease_id": "11111111-1111-4111-8111-111111111144",
        "lease_duration_seconds": 60,
        "worker_provider": "codex",
        "attempts": 2,
    }
    adapter = SimpleNamespace(
        run=AsyncMock(
            return_value=SimpleNamespace(
                text=completion_report_json(commits=("run 1",))
            )
        )
    )
    with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter):
        await daemon.run_claimed(
            api, "codex", str(workspace), 30, {"ticket": ticket, "prompt": "w"}, manager
        )
    second = api.report.call_args.args[-1]

    assert second["state"] == "ready"
    assert "Commit the work" not in second["question"]


@pytest.mark.asyncio
async def test_after_a_restart_a_done_without_commits_is_unverified_not_ready(
    local_isolation_policy,
):
    """Regression: the skipped commit check must not ready a bare done."""
    from src.server.services.autonomy import git_delivery

    workspace = local_isolation_policy
    _git_ticket_workspace(workspace)
    git_delivery._TICKET_BASELINES.clear()
    api = Mock()
    manager = daemon.LeaseManager(api)
    ticket = {
        "workspace_id": "test-workspace",
        "ticket_id": "t-restart-bare",
        "lease_id": "11111111-1111-4111-8111-111111111166",
        "lease_duration_seconds": 60,
        "worker_provider": "codex",
        "attempts": 2,
    }
    adapter = SimpleNamespace(
        run=AsyncMock(return_value=SimpleNamespace(text=completion_report_json()))
    )
    with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter):
        await daemon.run_claimed(
            api, "codex", str(workspace), 30, {"ticket": ticket, "prompt": "w"}, manager
        )
    report = api.report.call_args.args[-1]

    assert report["state"] == "waiting"
    assert report["attention_reason"] == "completion_unverified"


def test_a_ticket_checkout_counts_commits_from_its_pinned_base(tmp_path):
    from src.server.services.autonomy.git_delivery import (
        capture_git_snapshot,
        safe_git_commit_delivery,
    )

    workspace = tmp_path / "checkout"
    workspace.mkdir()
    _git_ticket_workspace(workspace)
    base_sha = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    (workspace / "tracked.txt").write_text("run 1")
    subprocess.run(["git", "-C", str(workspace), "commit", "-qam", "run 1"], check=True)
    rebaselined = capture_git_snapshot(str(workspace))

    counted = daemon._delivery_baseline(
        rebaselined, {"base_sha": base_sha}, {"attempts": 3}, baseline_cached=False
    )

    assert safe_git_commit_delivery(str(workspace), counted) == "delivered"
    plain = daemon._delivery_baseline(
        rebaselined, {}, {"attempts": 3}, baseline_cached=False
    )
    assert safe_git_commit_delivery(str(workspace), plain) == "unverified"
    first = daemon._delivery_baseline(
        rebaselined, {}, {"attempts": 1}, baseline_cached=False
    )
    assert safe_git_commit_delivery(str(workspace), first) == "missing"


def test_the_daemon_announces_the_completion_contract_on_claim():
    api = daemon.Api("http://local", "token")
    with patch.object(api, "_call", return_value={}) as call:
        api.claim("claim-1", policy_version="v1")
    body = call.call_args.args[2]
    assert body["completion_contract"] == daemon.COMPLETION_CONTRACT_VERSION


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "state", "question"),
    [
        ("Checked.\nAUTONOMY_STATE: blocked\nAUTONOMY_QUESTION: Which?", "blocked", "Which?"),
        ("All done.", "ready", ""),
    ],
)
async def test_an_older_server_claim_keeps_the_marker_report(
    local_isolation_policy, text, state, question
):
    """Mixed deploy: a claim without the contract echo is parsed as markers."""
    api = Mock()
    manager = daemon.LeaseManager(api)
    ticket = {
        "workspace_id": "test-workspace",
        "ticket_id": f"t-legacy-{state}",
        "lease_id": "11111111-1111-4111-8111-111111111155",
        "lease_duration_seconds": 60,
        "worker_provider": "codex",
        "completion_contract": 0,
    }
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=text)))
    with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter):
        await daemon.run_claimed(
            api, "codex", str(local_isolation_policy), 30,
            {"ticket": ticket, "prompt": "w"}, manager,
        )
    report = api.report.call_args.args[-1]

    assert report["state"] == state
    assert report["question"] == question
    # An older server's DaemonReport would reject the new attention reasons.
    assert "attention_reason" not in report


def test_the_contract_flag_is_read_from_the_claim():
    assert _REAL_CLAIM_HAS_CONTRACT({"completion_contract": 1}) is True
    assert _REAL_CLAIM_HAS_CONTRACT({"completion_contract": 0}) is False
    # An older server sends no field at all: that claim had the marker prompt.
    assert _REAL_CLAIM_HAS_CONTRACT({}) is False
    assert _REAL_CLAIM_HAS_CONTRACT({"completion_contract": "x"}) is False
