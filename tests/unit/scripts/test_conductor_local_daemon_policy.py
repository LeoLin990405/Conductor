"""The local daemon checks a Ticket against this device's own policy."""

from __future__ import annotations

import argparse
import os
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from scripts import conductor_local_daemon as daemon
from src.conductor_cli.daemon_capability import parse_capability
from src.conductor_cli.daemon_policy import PolicyStore
from tests.unit.conftest import completion_report_json


@pytest.fixture(autouse=True)
def _enforce_grant_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests pin N44's enforce behaviour; the modes have their own tests (N58)."""
    monkeypatch.setenv("CONDUCTOR_DAEMON_GRANT_VERIFIER_MODE", "enforce")


@pytest.fixture(autouse=True)
def local_isolation_policy(tmp_path, monkeypatch):
    """The daemon now runs each Ticket inside the merge-time isolation boundary.

    The policy verdict is checked before that boundary; the runs that reach the
    CLI still need a workspace policy and isolation root to get there.
    """
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


_DAEMON_ID = "11111111-1111-4111-8111-111111111111"


class _TrustCapability:
    """Test double. It reads the grant body and does not authenticate it."""

    def verify(self, grant):
        return parse_capability(grant)


def _capability(version: str) -> dict:
    return {
        "ticket_id": "t-policy",
        "daemon_id": _DAEMON_ID,
        "policy_version": version,
        "expires_at": "2099-01-01T00:00:00Z",
        "operations": ["repo.read", "test.run", "file.write"],
    }

POLICY = r"""
format: 1
read_only_operations:
  - repo.read
  - test.run
operations:
  - operation: repo.read
    patterns:
      - '\bgit\s+(status|diff)\b'
  - operation: test.run
    patterns:
      - '\bpytest\b'
  - operation: file.write
    patterns:
      - '\bwrite the file\b'
  - operation: repo.push
    patterns:
      - '\bgit\s+push\b'
providers:
  codex:
    allow:
      - repo.read
      - test.run
      - file.write
    deny:
      - repo.push
"""

TIGHTER_POLICY = POLICY.replace(
    r"'\bpytest\b'", r"'\bpytest\b|\bnose\b'"
)

READ_ONLY_POLICY = POLICY.replace("format: 1\n", "format: 1\nread_only: true\n")

READ_ONLY_PROVIDER_POLICY = POLICY.replace(
    "  codex:\n    allow:", "  codex:\n    read_only: true\n    allow:"
)

UNCLASSIFIED_POLICY = POLICY.replace(
    "  codex:\n    allow:", "  codex:\n    allow_unclassified: true\n    allow:"
)

NARROW_POLICY = POLICY.replace(
    "    allow:\n      - repo.read\n      - test.run\n      - file.write\n",
    "    allow:\n      - repo.read\n",
)

KIMI_READ_ONLY_POLICY = READ_ONLY_POLICY + ("  kimi:\n    allow:\n      - repo.read\n")


def _policy_file(tmp_path: Path, text: str = POLICY) -> Path:
    path = tmp_path / "daemon_policy.yaml"
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _store(tmp_path: Path, text: str = POLICY) -> tuple[PolicyStore, str]:
    store = PolicyStore(_policy_file(tmp_path, text))
    return store, store.load().version


def _ticket(**overrides) -> dict:
    ticket = {
        "ticket_id": "t-policy",
        "workspace_id": "test-workspace",
        "lease_id": "11111111-1111-4111-8111-111111111111",
        "lease_duration_seconds": 60,
        "worker_provider": "codex",
        "speculative": False,
    }
    ticket.update(overrides)
    return ticket


def _api(payload: dict) -> Mock:
    api = Mock()
    api.claim.return_value = payload
    return api


@pytest.mark.asyncio
async def test_an_out_of_scope_operation_never_reaches_the_cli(tmp_path):
    store, version = _store(tmp_path)
    api = _api(
        {
            "ticket": _ticket(
                title="Ship the branch",
                scope="git push origin main",
                policy_version=version,
            ),
            "prompt": "Ship the branch",
        }
    )

    with patch("src.llms.native_cli.registry.get_adapter") as get_adapter:
        assert await daemon.run_one(
            api, "codex", "/tmp", 10, policy_store=store
        ) is True

    get_adapter.assert_not_called()
    report = api.report.call_args.args[-1]
    assert report["state"] == "failed"
    assert "repo.push" in report["summary"]


@pytest.mark.asyncio
async def test_an_allowed_operation_runs_the_cli(tmp_path):
    store, version = _store(tmp_path)
    api = _api(
        {
            "ticket": _ticket(
                title="Check the tree",
                scope="run git status and report",
                policy_version=version,
                capability=_capability(version),
            ),
            "prompt": "Check the tree\n\nrun git status and report",
        }
    )
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=completion_report_json())))

    with patch(
        "src.llms.native_cli.registry.get_adapter", return_value=adapter
    ) as get_adapter:
        assert await daemon.run_one(
            api,
            "codex",
            "/tmp",
            10,
            policy_store=store,
            daemon_id=_DAEMON_ID,
            grant_verifier=_TrustCapability(),
        ) is True

    get_adapter.assert_called_once()
    assert api.report.call_args.args[-1]["state"] == "ready"


@pytest.mark.asyncio
async def test_run_claimed_passes_user_info_and_reports_only_safe_cli_failure(
    tmp_path, monkeypatch, caplog, local_isolation_policy
):
    import logging

    from src.llms.native_cli.base import CliInvocation
    from src.llms.native_cli.isolation import _mac_profile, current_worker_isolation
    from src.llms.native_cli.process import run_process

    workspace = local_isolation_policy
    fake = workspace / "fake-native-cli"
    fake.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' 'No API key found; SECRET_SENTINEL' >&2\n"
        "exit 1\n",
        encoding="utf-8",
    )
    fake.chmod(0o700)
    from src.llms.native_cli import isolation

    monkeypatch.setattr(
        isolation,
        "read_workspace_policy",
        lambda *_args: {
            "root": str(workspace),
            "env": set(),
            "runtime_read_paths": (),
            "user_info": True,
        },
    )

    async def fake_adapter_run(_prompt, *, cwd=None):
        current = current_worker_isolation()
        assert current is not None and current.user_info is True
        profile = _mac_profile(current.workdir, current.home, current.root / "tmp")
        assert 'com.apple.system.opendirectoryd.libinfo' in profile
        return await run_process(CliInvocation(argv=(str(fake),), cwd=cwd))

    adapter = SimpleNamespace(run=fake_adapter_run)
    store, version = _store(tmp_path)
    api = _api(
        {
            "ticket": _ticket(
                title="Check the tree",
                scope="run git status and report",
                policy_version=version,
                capability=_capability(version),
            ),
            "prompt": "run git status and report",
        }
    )

    with caplog.at_level(logging.ERROR, logger=daemon.__name__), patch(
        "src.llms.native_cli.registry.get_adapter", return_value=adapter
    ):
        assert await daemon.run_one(
            api,
            "codex",
            str(workspace),
            10,
            policy_store=store,
            daemon_id=_DAEMON_ID,
            grant_verifier=_TrustCapability(),
        ) is True

    report = api.report.call_args.args[-1]
    assert report["state"] == "failed"
    assert "auth_failed/" in report["summary"]
    assert "No API key found" in report["summary"]
    assert "SECRET_SENTINEL" not in report["summary"]
    assert "SECRET_SENTINEL" not in caplog.text


@pytest.mark.asyncio
async def test_a_forbidden_launch_prompt_never_reaches_the_cli(tmp_path):
    """The cloud sends the prompt next to the Ticket, so it carries its own verdict.

    An allowed scope used to let any prompt through: the CLI ran the prompt,
    not the Ticket's own text.
    """
    store, version = _store(tmp_path)
    api = _api(
        {
            "ticket": _ticket(
                title="Check the tree",
                scope="run git status and report",
                policy_version=version,
                capability=_capability(version),
            ),
            "prompt": "git push origin main",
        }
    )

    with patch("src.llms.native_cli.registry.get_adapter") as get_adapter:
        assert await daemon.run_one(
            api, "codex", "/tmp", 10, policy_store=store
        ) is True

    get_adapter.assert_not_called()
    report = api.report.call_args.args[-1]
    assert report["state"] == "failed"
    assert "repo.push" in report["summary"]


@pytest.mark.asyncio
async def test_a_launch_prompt_outside_the_allowlist_is_refused(tmp_path):
    """The prompt may not ask for more than the provider's allowlist holds."""
    store, version = _store(tmp_path, NARROW_POLICY)
    api = _api(
        {
            "ticket": _ticket(
                title="Check the tree",
                scope="run git status and report",
                policy_version=version,
            ),
            "prompt": "run pytest and report",
        }
    )

    with patch("src.llms.native_cli.registry.get_adapter") as get_adapter:
        assert await daemon.run_one(
            api, "codex", "/tmp", 10, policy_store=store
        ) is True

    get_adapter.assert_not_called()
    report = api.report.call_args.args[-1]
    assert report["state"] == "failed"
    assert "test.run" in report["summary"]


@pytest.mark.asyncio
async def test_the_server_warning_in_the_launch_prompt_does_not_block_the_run(tmp_path):
    """The server's own instruction names no operation the device forbids."""
    store, version = _store(tmp_path)
    api = _api(
        {
            "ticket": _ticket(
                title="Check the tree",
                scope="run git status and report",
                policy_version=version,
                capability=_capability(version),
            ),
            "prompt": (
                "run git status and report\n\n"
                "Do not modify files, run mutating commands, or push."
            ),
        }
    )
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=completion_report_json())))

    with patch(
        "src.llms.native_cli.registry.get_adapter", return_value=adapter
    ) as get_adapter:
        assert await daemon.run_one(
            api,
            "codex",
            "/tmp",
            10,
            policy_store=store,
            daemon_id=_DAEMON_ID,
            grant_verifier=_TrustCapability(),
        ) is True

    get_adapter.assert_called_once()


@pytest.mark.asyncio
async def test_a_read_only_policy_runs_codex_in_read_only_mode(tmp_path):
    """A read-only policy file must not launch a writable sandbox."""
    store, version = _store(tmp_path, READ_ONLY_POLICY)
    api = _api(
        {
            "ticket": _ticket(
                title="Check the tree",
                scope="run git status and report",
                policy_version=version,
                capability={**_capability(version), "operations": ["repo.read"]},
            ),
            "prompt": "run git status and report",
        }
    )
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=completion_report_json())))

    with patch(
        "src.llms.native_cli.registry.get_adapter", return_value=adapter
    ) as get_adapter:
        assert await daemon.run_one(
            api,
            "codex",
            "/tmp",
            10,
            policy_store=store,
            daemon_id=_DAEMON_ID,
            grant_verifier=_TrustCapability(),
        ) is True

    assert get_adapter.call_args.kwargs["read_only"] is True


@pytest.mark.asyncio
async def test_a_read_only_provider_entry_runs_codex_in_read_only_mode(tmp_path):
    """A read-only provider entry must not launch a writable sandbox either."""
    store, version = _store(tmp_path, READ_ONLY_PROVIDER_POLICY)
    api = _api(
        {
            "ticket": _ticket(
                title="Check the tree",
                scope="run git status and report",
                policy_version=version,
                capability={**_capability(version), "operations": ["repo.read"]},
            ),
            "prompt": "run git status and report",
        }
    )
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=completion_report_json())))

    with patch(
        "src.llms.native_cli.registry.get_adapter", return_value=adapter
    ) as get_adapter:
        assert await daemon.run_one(
            api,
            "codex",
            "/tmp",
            10,
            policy_store=store,
            daemon_id=_DAEMON_ID,
            grant_verifier=_TrustCapability(),
        ) is True

    assert get_adapter.call_args.kwargs["read_only"] is True


@pytest.mark.asyncio
async def test_a_speculative_ticket_runs_codex_read_only_under_a_policy(tmp_path):
    store, version = _store(tmp_path)
    api = _api(
        {
            "ticket": _ticket(
                title="Check the tree",
                scope="run git status and report",
                speculative=True,
                policy_version=version,
                capability={**_capability(version), "operations": ["repo.read"]},
            ),
            "prompt": "run git status and report",
        }
    )
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=completion_report_json())))

    with patch(
        "src.llms.native_cli.registry.get_adapter", return_value=adapter
    ) as get_adapter:
        assert await daemon.run_one(
            api,
            "codex",
            "/tmp",
            10,
            policy_store=store,
            daemon_id=_DAEMON_ID,
            grant_verifier=_TrustCapability(),
        ) is True

    assert get_adapter.call_args.kwargs["read_only"] is True


@pytest.mark.asyncio
async def test_an_ordinary_ticket_keeps_its_write_access(tmp_path):
    """A writable policy must not silently turn every run read-only."""
    store, version = _store(tmp_path)
    api = _api(
        {
            "ticket": _ticket(
                title="Ship the branch",
                scope="write the file and report",
                policy_version=version,
                capability=_capability(version),
            ),
            "prompt": "write the file and report",
        }
    )
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=completion_report_json())))

    with patch(
        "src.llms.native_cli.registry.get_adapter", return_value=adapter
    ) as get_adapter:
        assert await daemon.run_one(
            api,
            "codex",
            "/tmp",
            10,
            policy_store=store,
            daemon_id=_DAEMON_ID,
            grant_verifier=_TrustCapability(),
        ) is True

    assert get_adapter.call_args.kwargs["read_only"] is False


@pytest.mark.asyncio
async def test_a_read_only_run_restricts_the_outer_sandbox(tmp_path):
    """The outer isolation must deny workspace writes for a read-only run."""
    store, version = _store(tmp_path, READ_ONLY_POLICY)
    api = _api(
        {
            "ticket": _ticket(
                title="Check the tree",
                scope="run git status and report",
                policy_version=version,
                capability={**_capability(version), "operations": ["repo.read"]},
            ),
            "prompt": "run git status and report",
        }
    )
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=completion_report_json())))
    seen: dict = {}

    def fake_isolation(**kwargs):
        seen.update(kwargs)
        return nullcontext()

    with (
        patch(
            "src.llms.native_cli.isolation.worker_isolation", fake_isolation
        ),
        patch("src.llms.native_cli.registry.get_adapter", return_value=adapter),
    ):
        assert await daemon.run_one(
            api,
            "codex",
            "/tmp",
            10,
            policy_store=store,
            daemon_id=_DAEMON_ID,
            grant_verifier=_TrustCapability(),
        ) is True

    assert seen["read_only"] is True


@pytest.mark.asyncio
async def test_an_ordinary_run_keeps_the_outer_sandbox_writable(tmp_path):
    store, version = _store(tmp_path)
    api = _api(
        {
            "ticket": _ticket(
                title="Ship the branch",
                scope="write the file and report",
                policy_version=version,
                capability=_capability(version),
            ),
            "prompt": "write the file and report",
        }
    )
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=completion_report_json())))
    seen: dict = {}

    def fake_isolation(**kwargs):
        seen.update(kwargs)
        return nullcontext()

    with (
        patch(
            "src.llms.native_cli.isolation.worker_isolation", fake_isolation
        ),
        patch("src.llms.native_cli.registry.get_adapter", return_value=adapter),
    ):
        assert await daemon.run_one(
            api,
            "codex",
            "/tmp",
            10,
            policy_store=store,
            daemon_id=_DAEMON_ID,
            grant_verifier=_TrustCapability(),
        ) is True

    assert seen["read_only"] is False


@pytest.mark.asyncio
async def test_a_read_only_run_checks_grants_against_the_workspace_root(tmp_path):
    """The daemon hands the full policy root, not the narrowed cwd, to isolation."""
    store, version = _store(tmp_path)
    api = _api(
        {
            "ticket": _ticket(
                title="Check the tree",
                scope="run git status and report",
                policy_version=version,
                capability=_capability(version),
            ),
            "prompt": "run git status and report",
        }
    )
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=completion_report_json())))
    seen: dict = {}

    def fake_isolation(**kwargs):
        seen.update(kwargs)
        return nullcontext()

    with (
        patch("src.llms.native_cli.isolation.worker_isolation", fake_isolation),
        patch("src.llms.native_cli.registry.get_adapter", return_value=adapter),
    ):
        assert await daemon.run_one(
            api,
            "codex",
            "/tmp",
            10,
            policy_store=store,
            daemon_id=_DAEMON_ID,
            grant_verifier=_TrustCapability(),
        ) is True

    # The fixture's workspace policy root, not the default run cwd, is the root
    # a read-only write-grant check must cover.
    assert Path(seen["workspace_root"]) == (tmp_path / "workspace").resolve()


@pytest.mark.asyncio
async def test_an_interpreter_argument_does_not_authorise_a_command(tmp_path):
    """A string argument that happens to name an allowed operation is data."""
    store, version = _store(tmp_path)
    api = _api(
        {
            "ticket": _ticket(
                title="Check the tree",
                scope="run git status and report",
                policy_version=version,
            ),
            "prompt": (
                "python3 -c \"open('marker','w').write('git status')\""
            ),
        }
    )

    with patch("src.llms.native_cli.registry.get_adapter") as get_adapter:
        assert await daemon.run_one(
            api, "codex", "/tmp", 10, policy_store=store
        ) is True

    get_adapter.assert_not_called()
    assert "cannot classify" in api.report.call_args.args[-1]["summary"]


@pytest.mark.asyncio
async def test_a_quoted_argument_does_not_authorise_a_command_prompt(tmp_path):
    store, version = _store(tmp_path)
    api = _api(
        {
            "ticket": _ticket(
                title="Check the tree",
                scope="run git status and report",
                policy_version=version,
            ),
            "prompt": "rm -rf ./victim 'git status'",
        }
    )

    with patch("src.llms.native_cli.registry.get_adapter") as get_adapter:
        assert await daemon.run_one(
            api, "codex", "/tmp", 10, policy_store=store
        ) is True

    get_adapter.assert_not_called()
    assert "cannot classify" in api.report.call_args.args[-1]["summary"]


@pytest.mark.asyncio
async def test_an_unclassified_executable_prompt_never_reaches_the_cli(tmp_path):
    """Executable text the policy cannot classify is not a request it allowed."""
    store, version = _store(tmp_path)
    api = _api(
        {
            "ticket": _ticket(
                title="Check the tree",
                scope="run git status and report",
                policy_version=version,
            ),
            "prompt": "rm -rf .",
        }
    )

    with patch("src.llms.native_cli.registry.get_adapter") as get_adapter:
        assert await daemon.run_one(
            api, "codex", "/tmp", 10, policy_store=store
        ) is True

    get_adapter.assert_not_called()
    assert "launch prompt" in api.report.call_args.args[-1]["summary"]


@pytest.mark.asyncio
async def test_a_shell_chained_unclassified_command_never_reaches_the_cli(tmp_path):
    """An allowed command cannot smuggle an unclassified one next to it."""
    store, version = _store(tmp_path)
    api = _api(
        {
            "ticket": _ticket(
                title="Check the tree",
                scope="run git status and report",
                policy_version=version,
            ),
            "prompt": "git status && rm -rf .",
        }
    )

    with patch("src.llms.native_cli.registry.get_adapter") as get_adapter:
        assert await daemon.run_one(
            api, "codex", "/tmp", 10, policy_store=store
        ) is True

    get_adapter.assert_not_called()
    assert "cannot classify" in api.report.call_args.args[-1]["summary"]


@pytest.mark.asyncio
async def test_an_unclassified_command_is_refused_even_when_prose_is_allowed(
    tmp_path,
):
    """allow_unclassified admits prose, not an executable line the policy never named."""
    store, version = _store(tmp_path, UNCLASSIFIED_POLICY)
    api = _api(
        {
            "ticket": _ticket(
                title="Check the tree",
                scope="run git status and report",
                policy_version=version,
            ),
            "prompt": "rm -rf .",
        }
    )

    with patch("src.llms.native_cli.registry.get_adapter") as get_adapter:
        assert await daemon.run_one(
            api, "codex", "/tmp", 10, policy_store=store
        ) is True

    get_adapter.assert_not_called()
    assert "cannot classify" in api.report.call_args.args[-1]["summary"]


@pytest.mark.asyncio
async def test_a_policy_inside_the_workspace_is_refused(tmp_path):
    """A Worker that can write the policy file could widen its own authority."""
    workspace = tmp_path / "workspace"
    policy_path = workspace / "daemon_policy.yaml"
    policy_path.write_text(POLICY, encoding="utf-8")
    os.chmod(policy_path, 0o600)
    store = PolicyStore(policy_path)
    version = store.load().version
    api = _api(
        {
            "ticket": _ticket(
                title="Check the tree",
                scope="run git status and report",
                policy_version=version,
                capability=_capability(version),
            ),
            "prompt": "run git status and report",
        }
    )

    with patch("src.llms.native_cli.registry.get_adapter") as get_adapter:
        assert await daemon.run_one(
            api,
            "codex",
            "/tmp",
            10,
            policy_store=store,
            daemon_id=_DAEMON_ID,
            grant_verifier=_TrustCapability(),
        ) is True

    get_adapter.assert_not_called()
    assert "worker-writable" in api.report.call_args.args[-1]["summary"]


@pytest.mark.asyncio
async def test_a_policy_inside_a_runtime_write_path_is_refused(tmp_path, monkeypatch):
    """Every effective writable mount is checked, not only the workspace root."""
    from src.llms.native_cli import isolation

    workspace = tmp_path / "workspace"
    cli_state = tmp_path / "cli-state"
    cli_state.mkdir()
    policy_path = cli_state / "daemon_policy.yaml"
    policy_path.write_text(POLICY, encoding="utf-8")
    os.chmod(policy_path, 0o600)
    monkeypatch.setattr(
        isolation,
        "read_workspace_policy",
        lambda *_args: {
            "root": str(workspace),
            "env": set(),
            "runtime_read_paths": (),
            "runtime_write_paths": (str(cli_state),),
        },
    )
    store = PolicyStore(policy_path)
    version = store.load().version
    api = _api(
        {
            "ticket": _ticket(
                title="Check the tree",
                scope="run git status and report",
                policy_version=version,
                capability=_capability(version),
            ),
            "prompt": "run git status and report",
        }
    )

    with patch("src.llms.native_cli.registry.get_adapter") as get_adapter:
        assert await daemon.run_one(
            api,
            "codex",
            "/tmp",
            10,
            policy_store=store,
            daemon_id=_DAEMON_ID,
            grant_verifier=_TrustCapability(),
        ) is True

    get_adapter.assert_not_called()
    assert "worker-writable" in api.report.call_args.args[-1]["summary"]


def test_policy_location_check_resolves_an_alias_of_the_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    alias = tmp_path / "alias-workspace"
    if not alias.exists():
        alias.symlink_to(workspace, target_is_directory=True)
    policy_path = workspace / "daemon_policy.yaml"
    policy_path.write_text(POLICY, encoding="utf-8")
    os.chmod(policy_path, 0o600)

    reason = daemon._policy_location_rejection(policy_path, str(alias), "")

    assert "worker-writable" in reason


def test_policy_location_check_rejects_a_case_variant_of_the_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    policy_path = workspace / "daemon_policy.yaml"
    policy_path.write_text(POLICY, encoding="utf-8")
    os.chmod(policy_path, 0o600)
    variant = Path(str(workspace).swapcase())
    if not variant.exists():
        pytest.skip("volume is case-sensitive; case-variant aliases do not exist")

    reason = daemon._policy_location_rejection(policy_path, str(variant), "")

    assert "worker-writable" in reason


@pytest.mark.asyncio
async def test_a_background_separator_does_not_smuggle_a_command(tmp_path):
    store, version = _store(tmp_path)
    api = _api(
        {
            "ticket": _ticket(
                title="Check the tree",
                scope="run git status and report",
                policy_version=version,
            ),
            "prompt": "git status & rm -rf .",
        }
    )

    with patch("src.llms.native_cli.registry.get_adapter") as get_adapter:
        assert await daemon.run_one(
            api, "codex", "/tmp", 10, policy_store=store
        ) is True

    get_adapter.assert_not_called()
    assert "cannot classify" in api.report.call_args.args[-1]["summary"]


@pytest.mark.asyncio
async def test_the_real_server_prompt_reaches_the_cli(tmp_path):
    """The prompt the server actually builds must not fail the executable check."""
    from src.server.services.autonomy.worker import worker_prompt

    store, version = _store(tmp_path)
    ticket = _ticket(
        title="Check the tree",
        scope="run git status and report",
        policy_version=version,
        capability=_capability(version),
    )
    api = _api(
        {
            "ticket": ticket,
            "prompt": worker_prompt(ticket, {}, native=True),
        }
    )
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=completion_report_json())))

    with patch(
        "src.llms.native_cli.registry.get_adapter", return_value=adapter
    ) as get_adapter:
        assert await daemon.run_one(
            api,
            "codex",
            "/tmp",
            10,
            policy_store=store,
            daemon_id=_DAEMON_ID,
            grant_verifier=_TrustCapability(),
        ) is True

    get_adapter.assert_called_once()


@pytest.mark.asyncio
async def test_normalized_command_words_are_judged_by_the_policy(tmp_path):
    """`git p""ush` is the shell's `git push`: deny must see the real command."""
    store, version = _store(tmp_path)
    api = _api(
        {
            "ticket": _ticket(
                title="Check the tree",
                scope="run git status and report",
                policy_version=version,
            ),
            "prompt": 'git status\ngit p""ush origin main',
        }
    )

    with patch("src.llms.native_cli.registry.get_adapter") as get_adapter:
        assert await daemon.run_one(
            api, "codex", "/tmp", 10, policy_store=store
        ) is True

    get_adapter.assert_not_called()
    assert "repo.push" in api.report.call_args.args[-1]["summary"]


@pytest.mark.asyncio
async def test_argument_text_does_not_authorise_a_command(tmp_path):
    store, version = _store(tmp_path)
    api = _api(
        {
            "ticket": _ticket(
                title="Check the tree",
                scope="run git status and report",
                policy_version=version,
            ),
            "prompt": "rm -rf ./victim git status",
        }
    )

    with patch("src.llms.native_cli.registry.get_adapter") as get_adapter:
        assert await daemon.run_one(
            api, "codex", "/tmp", 10, policy_store=store
        ) is True

    get_adapter.assert_not_called()
    assert "cannot classify" in api.report.call_args.args[-1]["summary"]


@pytest.mark.asyncio
async def test_quote_concatenation_does_not_hide_a_command(tmp_path):
    store, version = _store(tmp_path)
    api = _api(
        {
            "ticket": _ticket(
                title="Check the tree",
                scope="run git status and report",
                policy_version=version,
            ),
            "prompt": 'git status\nr"m" -rf ./victim',
        }
    )

    with patch("src.llms.native_cli.registry.get_adapter") as get_adapter:
        assert await daemon.run_one(
            api, "codex", "/tmp", 10, policy_store=store
        ) is True

    get_adapter.assert_not_called()
    assert "cannot classify" in api.report.call_args.args[-1]["summary"]


@pytest.mark.asyncio
async def test_an_escaped_command_word_is_refused(tmp_path):
    store, version = _store(tmp_path)
    api = _api(
        {
            "ticket": _ticket(
                title="Check the tree",
                scope="run git status and report",
                policy_version=version,
            ),
            "prompt": "git status\nr\\m -rf ./victim",
        }
    )

    with patch("src.llms.native_cli.registry.get_adapter") as get_adapter:
        assert await daemon.run_one(
            api, "codex", "/tmp", 10, policy_store=store
        ) is True

    get_adapter.assert_not_called()
    assert "expansion or substitution" in api.report.call_args.args[-1]["summary"]


@pytest.mark.asyncio
async def test_attached_redirection_is_refused(tmp_path):
    store, version = _store(tmp_path)
    api = _api(
        {
            "ticket": _ticket(
                title="Check the tree",
                scope="run git status and report",
                policy_version=version,
            ),
            "prompt": "git status>victim",
        }
    )

    with patch("src.llms.native_cli.registry.get_adapter") as get_adapter:
        assert await daemon.run_one(
            api, "codex", "/tmp", 10, policy_store=store
        ) is True

    get_adapter.assert_not_called()
    assert "redirection" in api.report.call_args.args[-1]["summary"]


@pytest.mark.asyncio
async def test_redirection_attached_to_the_executable_is_refused(tmp_path):
    store, version = _store(tmp_path)
    api = _api(
        {
            "ticket": _ticket(
                title="Check the tree",
                scope="run git status and report",
                policy_version=version,
            ),
            "prompt": "run git status and report\ngit>victim",
        }
    )

    with patch("src.llms.native_cli.registry.get_adapter") as get_adapter:
        assert await daemon.run_one(
            api, "codex", "/tmp", 10, policy_store=store
        ) is True

    get_adapter.assert_not_called()
    assert "redirection" in api.report.call_args.args[-1]["summary"]


@pytest.mark.asyncio
async def test_a_parenthesized_listed_command_is_refused(tmp_path):
    store, version = _store(tmp_path)
    api = _api(
        {
            "ticket": _ticket(
                title="Check the tree",
                scope="run git status and report",
                policy_version=version,
            ),
            "prompt": "run git status and report\n1) rm -rf ./victim",
        }
    )

    with patch("src.llms.native_cli.registry.get_adapter") as get_adapter:
        assert await daemon.run_one(
            api, "codex", "/tmp", 10, policy_store=store
        ) is True

    get_adapter.assert_not_called()
    assert "cannot classify" in api.report.call_args.args[-1]["summary"]


@pytest.mark.asyncio
async def test_a_listed_unclassified_command_is_refused(tmp_path):
    store, version = _store(tmp_path)
    api = _api(
        {
            "ticket": _ticket(
                title="Check the tree",
                scope="run git status and report",
                policy_version=version,
            ),
            "prompt": "run git status and report\n- rm -rf ./victim",
        }
    )

    with patch("src.llms.native_cli.registry.get_adapter") as get_adapter:
        assert await daemon.run_one(
            api, "codex", "/tmp", 10, policy_store=store
        ) is True

    get_adapter.assert_not_called()
    assert "cannot classify" in api.report.call_args.args[-1]["summary"]


@pytest.mark.asyncio
async def test_output_redirection_is_refused(tmp_path):
    store, version = _store(tmp_path)
    api = _api(
        {
            "ticket": _ticket(
                title="Check the tree",
                scope="run git status and report",
                policy_version=version,
            ),
            "prompt": "git status > victim",
        }
    )

    with patch("src.llms.native_cli.registry.get_adapter") as get_adapter:
        assert await daemon.run_one(
            api, "codex", "/tmp", 10, policy_store=store
        ) is True

    get_adapter.assert_not_called()
    assert "redirection" in api.report.call_args.args[-1]["summary"]


@pytest.mark.asyncio
async def test_a_read_only_policy_refuses_a_provider_that_cannot_enforce_it(tmp_path):
    """A run that must not change the device needs an adapter that can hold it."""
    store, version = _store(tmp_path, KIMI_READ_ONLY_POLICY)
    api = _api(
        {
            "ticket": _ticket(
                title="Check the tree",
                scope="run git status and report",
                worker_provider="kimi",
                policy_version=version,
                capability={**_capability(version), "operations": ["repo.read"]},
            ),
            "prompt": "run git status and report",
        }
    )

    with patch("src.llms.native_cli.registry.get_adapter") as get_adapter:
        assert await daemon.run_one(
            api,
            "kimi",
            "/tmp",
            10,
            policy_store=store,
            daemon_id=_DAEMON_ID,
            grant_verifier=_TrustCapability(),
        ) is True

    get_adapter.assert_not_called()
    assert "read-only" in api.report.call_args.args[-1]["summary"]


@pytest.mark.asyncio
async def test_a_lease_without_a_policy_version_is_refused(tmp_path):
    store, _version = _store(tmp_path)
    api = _api(
        {
            "ticket": _ticket(
                title="Check the tree",
                scope="run git status",
                policy_version="",
            ),
            "prompt": "work",
        }
    )

    with patch("src.llms.native_cli.registry.get_adapter") as get_adapter:
        assert await daemon.run_one(
            api, "codex", "/tmp", 10, policy_store=store
        ) is True

    get_adapter.assert_not_called()
    assert "no local policy version" in api.report.call_args.args[-1]["summary"]


@pytest.mark.asyncio
async def test_a_lease_granted_under_an_older_policy_is_refused(tmp_path):
    store, version = _store(tmp_path)
    api = _api(
        {
            "ticket": _ticket(
                title="Check the tree",
                scope="run git status",
                policy_version="0123456789abcdef",
            ),
            "prompt": "work",
        }
    )

    with patch("src.llms.native_cli.registry.get_adapter") as get_adapter:
        assert await daemon.run_one(
            api, "codex", "/tmp", 10, policy_store=store
        ) is True

    get_adapter.assert_not_called()
    summary = api.report.call_args.args[-1]["summary"]
    assert "policy changed" in summary
    assert version in summary


@pytest.mark.asyncio
async def test_a_policy_replaced_after_the_grant_refuses_the_lease(tmp_path):
    store, version = _store(tmp_path)
    api = _api(
        {
            "ticket": _ticket(
                title="Check the tree",
                scope="run git status",
                policy_version=version,
            ),
            "prompt": "work",
        }
    )

    with patch("src.llms.native_cli.registry.get_adapter") as get_adapter:
        # The device tightens its policy while the Ticket was in flight.
        _policy_file(tmp_path, TIGHTER_POLICY)
        assert await daemon.run_one(
            api, "codex", "/tmp", 10, policy_store=store
        ) is True

    get_adapter.assert_not_called()
    assert "policy changed" in api.report.call_args.args[-1]["summary"]


@pytest.mark.asyncio
async def test_an_unreadable_policy_refuses_the_ticket(tmp_path):
    store, version = _store(tmp_path)
    api = _api(
        {
            "ticket": _ticket(
                title="Check the tree",
                scope="run git status",
                policy_version=version,
            ),
            "prompt": "work",
        }
    )

    with patch("src.llms.native_cli.registry.get_adapter") as get_adapter:
        _policy_file(tmp_path, "operations: [broken\n")
        assert await daemon.run_one(
            api, "codex", "/tmp", 10, policy_store=store
        ) is True

    get_adapter.assert_not_called()
    assert "policy is unusable" in api.report.call_args.args[-1]["summary"]


@pytest.mark.asyncio
async def test_a_speculative_ticket_may_not_run_a_mutating_operation(tmp_path):
    store, version = _store(tmp_path)
    api = _api(
        {
            "ticket": _ticket(
                title="Fix the parser",
                scope="write the file and report",
                speculative=True,
                policy_version=version,
            ),
            "prompt": "work",
        }
    )

    with patch("src.llms.native_cli.registry.get_adapter") as get_adapter:
        assert await daemon.run_one(
            api, "codex", "/tmp", 10, policy_store=store
        ) is True

    get_adapter.assert_not_called()
    assert "read-only" in api.report.call_args.args[-1]["summary"]


@pytest.mark.asyncio
async def test_the_claim_carries_the_local_policy_version(tmp_path):
    store, version = _store(tmp_path)
    api = _api({"ticket": None})

    await daemon.run_one(api, "codex", "/tmp", 10, policy_store=store)

    assert api.claim.call_args.kwargs["policy_version"] == version


@pytest.mark.asyncio
async def test_a_daemon_without_a_policy_keeps_running_everything(tmp_path):
    api = _api(
        {
            "ticket": _ticket(
                title="Ship the branch",
                scope="git push origin main",
            ),
            "prompt": "work",
        }
    )
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=completion_report_json())))

    with patch(
        "src.llms.native_cli.registry.get_adapter", return_value=adapter
    ) as get_adapter:
        assert await daemon.run_one(api, "codex", "/tmp", 10) is True

    get_adapter.assert_called_once()


class TestPolicyStoreResolution:
    def test_a_policy_file_is_loaded_and_enforced(self, tmp_path):
        path = _policy_file(tmp_path)

        store, error = daemon._open_policy_store(
            argparse.Namespace(policy=str(path), require_policy=False)
        )

        assert error == ""
        assert store is not None
        assert store.path == path

    def test_a_broken_policy_file_stops_the_daemon(self, tmp_path):
        path = tmp_path / "daemon_policy.yaml"
        path.write_text("operations: [broken\n", encoding="utf-8")
        os.chmod(path, 0o600)

        store, error = daemon._open_policy_store(
            argparse.Namespace(policy=str(path), require_policy=False)
        )

        assert store is None
        assert "unusable" in error

    def test_a_missing_policy_file_stops_the_daemon_when_it_is_required(self, tmp_path):
        store, error = daemon._open_policy_store(
            argparse.Namespace(policy=str(tmp_path / "absent.yaml"), require_policy=True)
        )

        assert store is None
        assert "No local daemon policy" in error

    def test_no_policy_file_leaves_the_daemon_unrestricted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "src.conductor_cli.daemon_policy.DEFAULT_POLICY_PATH",
            tmp_path / "absent.yaml",
        )

        store, error = daemon._open_policy_store(
            argparse.Namespace(policy="", require_policy=False)
        )

        assert store is None
        assert error == ""


@pytest.mark.asyncio
async def test_main_async_stops_when_the_policy_is_unusable(tmp_path, capsys):
    path = tmp_path / "daemon_policy.yaml"
    path.write_text("operations: [broken\n", encoding="utf-8")
    os.chmod(path, 0o600)
    args = argparse.Namespace(
        provider="codex",
        policy=str(path),
        require_policy=False,
        max_concurrency=1,
        cwd=str(tmp_path),
        isolation_root=str(tmp_path / "worker-state"),
    )

    assert await daemon.main_async(args) == 2
    assert "unusable" in capsys.readouterr().err


class TestDaemonStart:
    """The operator points the daemon at its policy file."""

    def _start(self, monkeypatch, tmp_path, policy: str, trusted_keys: str = "") -> list[str]:
        from unittest.mock import Mock

        from src.conductor_cli import daemon as daemon_cli

        monkeypatch.setattr(daemon_cli, "DAEMON_TOKEN_DIR", tmp_path / "credentials")
        monkeypatch.setattr(daemon_cli, "DAEMON_STATE_DIR", tmp_path / "state")
        monkeypatch.setattr(
            daemon_cli,
            "ensure_private_directory",
            lambda path, create=True: path.mkdir(parents=True, exist_ok=True)
            if create
            else None,
        )
        monkeypatch.setattr(daemon_cli, "_check_private_file", lambda path: None)
        monkeypatch.setattr(daemon_cli, "read_token", lambda path: "fake-test-token")
        monkeypatch.setattr(daemon_cli, "_process_fingerprint", lambda pid: "fingerprint")
        monkeypatch.setattr(daemon_cli.time, "sleep", lambda seconds: None)
        popen = Mock(return_value=Mock(pid=43210, poll=Mock(return_value=None)))
        monkeypatch.setattr(daemon_cli.subprocess, "Popen", popen)
        client = Mock(base_url="http://127.0.0.1:8000")
        client.get.return_value = {
            "daemons": [
                {
                    "daemon_id": "11111111-1111-4111-8111-111111111111",
                    "provider": "codex",
                    "max_concurrency": 1,
                }
            ]
        }

        daemon_cli.start_local_daemon(
            client,
            "11111111-1111-4111-8111-111111111111",
            str(tmp_path),
            True,
            policy=policy,
            trusted_keys=trusted_keys,
        )
        return popen.call_args.args[0]

    def test_the_policy_path_reaches_the_daemon(self, monkeypatch, tmp_path):
        command = self._start(monkeypatch, tmp_path, str(tmp_path / "policy.yaml"))

        assert command[command.index("--policy") + 1] == str(tmp_path / "policy.yaml")

    def test_the_daemon_id_reaches_the_process(self, monkeypatch, tmp_path):
        command = self._start(monkeypatch, tmp_path, "")

        assert (
            command[command.index("--daemon-id") + 1]
            == "11111111-1111-4111-8111-111111111111"
        )

    def test_no_policy_path_leaves_the_daemon_on_its_default(self, monkeypatch, tmp_path):
        command = self._start(monkeypatch, tmp_path, "")

        assert "--policy" not in command
        assert "--trusted-keys" not in command

    def test_the_trusted_key_path_reaches_the_daemon(self, monkeypatch, tmp_path):
        command = self._start(
            monkeypatch, tmp_path, "", trusted_keys=str(tmp_path / "keys.json")
        )

        assert command[command.index("--trusted-keys") + 1] == str(
            tmp_path / "keys.json"
        )
