"""The local daemon enforces a per-Ticket operation capability."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from scripts import conductor_local_daemon as daemon
from src.conductor_cli.daemon_capability import GrantError, parse_capability
from src.conductor_cli.daemon_policy import PolicyStore
from tests.unit.conftest import completion_report_json


@pytest.fixture(autouse=True)
def _enforce_grant_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests pin N44's enforce behaviour; the modes have their own tests (N58)."""
    monkeypatch.setenv("CONDUCTOR_DAEMON_GRANT_VERIFIER_MODE", "enforce")


@pytest.fixture(autouse=True)
def local_isolation_policy(tmp_path, monkeypatch):
    """Merged daemon: every run that reaches the CLI is workspace-isolated."""
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

DAEMON_ID = "11111111-1111-4111-8111-111111111111"
OTHER_DAEMON = "22222222-2222-4222-8222-222222222222"
LEAK = "MARKER-capability-leak"


class TrustingGrantVerifier:
    """Test double. It reads the grant body and does not authenticate it."""

    def verify(self, grant):
        return parse_capability(grant)


class RejectingGrantVerifier:
    def verify(self, grant):
        raise GrantError("signature missing")


def _policy_file(tmp_path: Path) -> Path:
    path = tmp_path / "daemon_policy.yaml"
    path.write_text(POLICY, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _store(tmp_path: Path) -> tuple[PolicyStore, str]:
    store = PolicyStore(_policy_file(tmp_path))
    return store, store.load().version


def _grant(version: str, **overrides) -> dict:
    body = {
        "ticket_id": "t-cap",
        "daemon_id": DAEMON_ID,
        "policy_version": version,
        "expires_at": "2099-01-01T00:00:00Z",
        "operations": ["repo.read"],
    }
    body.update(overrides)
    return body


def _ticket(version: str, **overrides) -> dict:
    ticket = {
        "ticket_id": "t-cap",
        "lease_id": "11111111-1111-4111-8111-111111111111",
        "lease_duration_seconds": 60,
        "workspace_id": "test-workspace",
        "worker_provider": "codex",
        "speculative": False,
        "title": "Check the tree",
        "scope": "run git status",
        "policy_version": version,
        "capability": _grant(version),
    }
    ticket.update(overrides)
    return ticket


def _api(ticket: dict) -> Mock:
    api = Mock()
    api.claim.return_value = {
        "ticket": ticket,
        "prompt": "Check the tree\n\nScope:\nrun git status",
    }
    return api


async def _run(
    api: Mock,
    store: PolicyStore,
    *,
    text: str = completion_report_json(summary="git status is clean"),
    verifier=TrustingGrantVerifier(),
    daemon_id: str = DAEMON_ID,
):
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=text)))
    with patch(
        "src.llms.native_cli.registry.get_adapter", return_value=adapter
    ) as get_adapter:
        assert (
            await daemon.run_one(
                api,
                "codex",
                "/tmp",
                10,
                policy_store=store,
                daemon_id=daemon_id,
                grant_verifier=verifier,
            )
            is True
        )
    return get_adapter


@pytest.mark.asyncio
async def test_an_operation_inside_the_capability_runs_and_is_reported(tmp_path):
    store, version = _store(tmp_path)
    api = _api(_ticket(version))

    get_adapter = await _run(
        api, store, text=completion_report_json(summary="git status is clean")
    )

    get_adapter.assert_called_once()
    report = api.report.call_args.args[-1]
    assert report["state"] == "ready"
    assert report["summary"] == "git status is clean"


@pytest.mark.asyncio
async def test_an_operation_outside_the_capability_never_reaches_the_cli(tmp_path):
    store, version = _store(tmp_path)
    api = _api(_ticket(version, scope="run pytest"))

    get_adapter = await _run(api, store)

    get_adapter.assert_not_called()
    summary = api.report.call_args.args[-1]["summary"]
    assert api.report.call_args.args[-1]["state"] == "failed"
    assert "test.run" in summary
    assert "outside the operation capability" in summary


@pytest.mark.asyncio
async def test_an_expired_capability_is_refused_before_the_cli(tmp_path):
    store, version = _store(tmp_path)
    api = _api(
        _ticket(version, capability=_grant(version, expires_at="2000-01-01T00:00:00Z"))
    )

    get_adapter = await _run(api, store)

    get_adapter.assert_not_called()
    assert "expired" in api.report.call_args.args[-1]["summary"]


@pytest.mark.asyncio
async def test_a_capability_for_a_different_ticket_is_refused(tmp_path):
    store, version = _store(tmp_path)
    api = _api(_ticket(version, capability=_grant(version, ticket_id="t-other")))

    get_adapter = await _run(api, store)

    get_adapter.assert_not_called()
    assert "different ticket" in api.report.call_args.args[-1]["summary"]


@pytest.mark.asyncio
async def test_a_capability_for_a_different_daemon_is_refused(tmp_path):
    store, version = _store(tmp_path)
    api = _api(_ticket(version, capability=_grant(version, daemon_id=OTHER_DAEMON)))

    get_adapter = await _run(api, store)

    get_adapter.assert_not_called()
    assert "different daemon" in api.report.call_args.args[-1]["summary"]


@pytest.mark.asyncio
async def test_a_policy_version_mismatch_is_refused(tmp_path):
    store, version = _store(tmp_path)
    api = _api(
        _ticket(
            version,
            capability=_grant(version, policy_version="0123456789abcdef"),
        )
    )

    get_adapter = await _run(api, store)

    get_adapter.assert_not_called()
    summary = api.report.call_args.args[-1]["summary"]
    assert "0123456789abcdef" in summary
    assert version in summary


@pytest.mark.asyncio
async def test_a_result_outside_the_capability_is_not_reported(tmp_path):
    store, version = _store(tmp_path)
    api = _api(_ticket(version))

    get_adapter = await _run(
        api,
        store,
        text=f"I ran pytest\n{LEAK}",
    )

    get_adapter.assert_called_once()
    report = api.report.call_args.args[-1]
    assert report["state"] == "failed"
    assert "test.run" in report["summary"]
    assert "outside the operation capability" in report["summary"]
    assert LEAK not in report["summary"]
    assert LEAK not in report["question"]
    assert "I ran pytest" not in report["summary"]


@pytest.mark.asyncio
async def test_a_cli_failure_outside_the_capability_is_not_reported(tmp_path):
    store, version = _store(tmp_path)
    api = _api(_ticket(version))
    adapter = SimpleNamespace(
        run=AsyncMock(side_effect=RuntimeError(f"I ran pytest\n{LEAK}"))
    )

    with patch(
        "src.llms.native_cli.registry.get_adapter", return_value=adapter
    ) as get_adapter:
        assert (
            await daemon.run_one(
                api,
                "codex",
                "/tmp",
                10,
                policy_store=store,
                daemon_id=DAEMON_ID,
                grant_verifier=TrustingGrantVerifier(),
            )
            is True
        )

    get_adapter.assert_called_once()
    report = api.report.call_args.args[-1]
    assert report["state"] == "failed"
    # The merged workspace-isolation wrapper masks CLI failure text before it
    # reaches the report (only the exception type survives), so the capability
    # result rule has nothing carrying an operation to forward: the failure is
    # reported without the CLI text at all.
    assert "RuntimeError" in report["summary"]
    assert "test.run" not in report["summary"]
    assert LEAK not in report["summary"]


@pytest.mark.asyncio
async def test_a_missing_capability_is_refused(tmp_path):
    store, version = _store(tmp_path)
    ticket = _ticket(version)
    ticket.pop("capability")
    api = _api(ticket)

    get_adapter = await _run(api, store)

    get_adapter.assert_not_called()
    assert "no operation capability" in api.report.call_args.args[-1]["summary"]


@pytest.mark.asyncio
async def test_a_capability_the_device_does_not_allow_is_refused(tmp_path):
    store, version = _store(tmp_path)
    api = _api(
        _ticket(
            version,
            capability=_grant(version, operations=["repo.read", "repo.push"]),
        )
    )

    get_adapter = await _run(api, store)

    get_adapter.assert_not_called()
    summary = api.report.call_args.args[-1]["summary"]
    assert "repo.push" in summary
    assert "does not allow" in summary


@pytest.mark.asyncio
async def test_a_rejected_grant_is_refused_without_its_body(tmp_path):
    store, version = _store(tmp_path)
    api = _api(_ticket(version))

    get_adapter = await _run(api, store, verifier=RejectingGrantVerifier())

    get_adapter.assert_not_called()
    summary = api.report.call_args.args[-1]["summary"]
    assert "rejected" in summary
    assert "signature missing" in summary
    assert "repo.read" not in summary


@pytest.mark.asyncio
async def test_no_verifier_fails_closed(tmp_path):
    store, version = _store(tmp_path)
    api = _api(_ticket(version))

    get_adapter = await _run(api, store, verifier=None)

    get_adapter.assert_not_called()
    assert "no grant verifier" in api.report.call_args.args[-1]["summary"]


@pytest.mark.asyncio
async def test_a_policy_without_a_daemon_id_stops_before_claiming(tmp_path, capsys):
    path = _policy_file(tmp_path)
    args = argparse.Namespace(
        provider="codex",
        policy=str(path),
        require_policy=False,
        max_concurrency=1,
        cwd=str(tmp_path),
        daemon_id="",
    )

    assert await daemon.main_async(args) == 2

    assert "no --daemon-id" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_the_executed_prompt_must_fit_the_capability(tmp_path):
    """The CLI runs the cloud prompt, so it cannot exceed the capability."""
    store, version = _store(tmp_path)
    ticket = _ticket(version, scope="run git status")
    api = _api(ticket)
    api.claim.return_value["prompt"] = "run pytest"
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=completion_report_json())))

    with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter) as get_adapter:
        assert (
            await daemon.run_one(
                api,
                "codex",
                "/tmp",
                10,
                policy_store=store,
                daemon_id=DAEMON_ID,
                grant_verifier=TrustingGrantVerifier(),
            )
            is True
        )

    get_adapter.assert_not_called()
    report = api.report.call_args.args[-1]
    assert report["state"] == "failed"
    assert "capability" in report["summary"].lower()


@pytest.mark.asyncio
async def test_normalized_commands_fit_the_capability_too(tmp_path):
    """`git "status"` is repo.read even though the raw regex misses it."""
    store, version = _store(tmp_path)
    ticket = _ticket(
        version,
        scope="pytest",
        capability=_grant(version, operations=["test.run"]),
    )
    api = _api(ticket)
    api.claim.return_value["prompt"] = 'pytest\ngit "status"'
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=completion_report_json())))

    with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter) as get_adapter:
        assert (
            await daemon.run_one(
                api,
                "codex",
                "/tmp",
                10,
                policy_store=store,
                daemon_id=DAEMON_ID,
                grant_verifier=TrustingGrantVerifier(),
            )
            is True
        )

    get_adapter.assert_not_called()
    assert "capability" in api.report.call_args.args[-1]["summary"].lower()


@pytest.mark.asyncio
async def test_a_normalized_result_outside_the_capability_is_not_reported(tmp_path):
    """A result line that hides an operation inside quotes must not leak."""
    store, version = _store(tmp_path)
    ticket = _ticket(version, scope="run pytest")
    ticket["capability"] = _grant(version, operations=["test.run"])
    api = _api(ticket)
    api.claim.return_value["prompt"] = "run pytest"
    leak = "MARKER-capability-leak"
    adapter = SimpleNamespace(
        run=AsyncMock(
            return_value=SimpleNamespace(text='git "status"\n' + leak)
        )
    )

    with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter):
        assert (
            await daemon.run_one(
                api,
                "codex",
                "/tmp",
                10,
                policy_store=store,
                daemon_id=DAEMON_ID,
                grant_verifier=TrustingGrantVerifier(),
            )
            is True
        )

    report = api.report.call_args.args[-1]
    assert report["state"] == "failed"
    assert leak not in report["summary"]
    assert "repo.read" in report["summary"]


@pytest.mark.asyncio
async def test_list_marked_normalized_commands_fit_the_capability_too(tmp_path):
    """A marked/normalized command still contributes its operation."""
    store, version = _store(tmp_path)
    ticket = _ticket(
        version,
        scope="pytest",
        capability=_grant(version, operations=["test.run"]),
    )
    api = _api(ticket)
    api.claim.return_value["prompt"] = 'pytest\n1) git "status"'
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=completion_report_json())))

    with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter) as get_adapter:
        assert (
            await daemon.run_one(
                api,
                "codex",
                "/tmp",
                10,
                policy_store=store,
                daemon_id=DAEMON_ID,
                grant_verifier=TrustingGrantVerifier(),
            )
            is True
        )

    get_adapter.assert_not_called()
    assert "capability" in api.report.call_args.args[-1]["summary"].lower()


@pytest.mark.asyncio
async def test_a_list_marked_normalized_result_is_not_reported(tmp_path):
    store, version = _store(tmp_path)
    ticket = _ticket(version, scope="run pytest")
    ticket["capability"] = _grant(version, operations=["test.run"])
    api = _api(ticket)
    api.claim.return_value["prompt"] = "run pytest"
    leak = "MARKER-list-marker-leak"
    adapter = SimpleNamespace(
        run=AsyncMock(return_value=SimpleNamespace(text='1) git "status"\n' + leak))
    )

    with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter):
        assert (
            await daemon.run_one(
                api,
                "codex",
                "/tmp",
                10,
                policy_store=store,
                daemon_id=DAEMON_ID,
                grant_verifier=TrustingGrantVerifier(),
            )
            is True
        )

    report = api.report.call_args.args[-1]
    assert report["state"] == "failed"
    assert leak not in report["summary"]
    assert "repo.read" in report["summary"]


@pytest.mark.asyncio
async def test_unresolved_shell_result_text_is_not_reported(tmp_path):
    """Unresolved shell spelling means an incomplete operation set: refuse it."""
    store, version = _store(tmp_path)
    ticket = _ticket(version, scope="run pytest")
    ticket["capability"] = _grant(version, operations=["test.run"])
    api = _api(ticket)
    api.claim.return_value["prompt"] = "run pytest"
    leak = "MARKER-unresolved-result-leak"
    adapter = SimpleNamespace(
        run=AsyncMock(
            return_value=SimpleNamespace(text='git "status">victim\n' + leak)
        )
    )

    with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter):
        assert (
            await daemon.run_one(
                api,
                "codex",
                "/tmp",
                10,
                policy_store=store,
                daemon_id=DAEMON_ID,
                grant_verifier=TrustingGrantVerifier(),
            )
            is True
        )

    report = api.report.call_args.args[-1]
    assert report["state"] == "failed"
    assert leak not in report["summary"]
    assert "status" not in report["summary"]
    assert "cannot classify" in report["summary"]
    assert leak not in str(report.get("question") or "")


@pytest.mark.asyncio
async def test_an_unclassified_command_result_is_not_reported(tmp_path):
    """A result command the policy names no operation for is unclassifiable."""
    store, version = _store(tmp_path)
    ticket = _ticket(version, scope="run pytest")
    ticket["capability"] = _grant(version, operations=["test.run"])
    api = _api(ticket)
    api.claim.return_value["prompt"] = "run pytest"
    leak = "MARKER-unclassified-result-leak"
    adapter = SimpleNamespace(
        run=AsyncMock(return_value=SimpleNamespace(text="rm -rf ./victim\n" + leak))
    )

    with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter):
        assert (
            await daemon.run_one(
                api,
                "codex",
                "/tmp",
                10,
                policy_store=store,
                daemon_id=DAEMON_ID,
                grant_verifier=TrustingGrantVerifier(),
            )
            is True
        )

    report = api.report.call_args.args[-1]
    assert report["state"] == "failed"
    assert leak not in report["summary"]
    assert "cannot classify" in report["summary"]


@pytest.mark.asyncio
async def test_a_chained_unclassified_command_result_is_not_reported(tmp_path):
    store, version = _store(tmp_path)
    ticket = _ticket(version, scope="run pytest")
    ticket["capability"] = _grant(version, operations=["test.run", "repo.read"])
    api = _api(ticket)
    api.claim.return_value["prompt"] = "run pytest"
    leak = "MARKER-chained-result-leak"
    adapter = SimpleNamespace(
        run=AsyncMock(
            return_value=SimpleNamespace(
                text="git status && rm -rf ./victim\n" + leak
            )
        )
    )

    with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter):
        assert (
            await daemon.run_one(
                api,
                "codex",
                "/tmp",
                10,
                policy_store=store,
                daemon_id=DAEMON_ID,
                grant_verifier=TrustingGrantVerifier(),
            )
            is True
        )

    report = api.report.call_args.args[-1]
    assert report["state"] == "failed"
    assert leak not in report["summary"]


class _SwitchingPolicyStore:
    """Returns A, A, then B: models a policy replaced between checks."""

    def __init__(self, policies):
        from pathlib import Path as _Path

        self.path = _Path("/nonexistent/conductor/daemon_policy.yaml")
        self.resolved_path = self.path
        self.policies = policies
        self.calls = 0

    def current(self):
        policy = self.policies[min(self.calls, len(self.policies) - 1)]
        self.calls += 1
        return policy


@pytest.mark.asyncio
async def test_a_policy_replaced_between_checks_cannot_pair_versions(tmp_path):
    """One immutable snapshot decides admission, capability and read-only mode."""
    import yaml

    from src.conductor_cli.daemon_policy import parse_policy

    base = parse_policy(yaml.safe_load(POLICY))
    stricter = parse_policy(
        yaml.safe_load(POLICY.replace(r"'\bpytest\b'", r"'\bpytest\b|\bnose\b'"))
    )
    assert base.version != stricter.version
    ticket = _ticket(
        base.version,
        scope="run git status",
        capability=_grant(stricter.version, operations=["repo.read"]),
    )
    api = _api(ticket)
    api.claim.return_value["prompt"] = "run git status"
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=completion_report_json())))
    store = _SwitchingPolicyStore([base, base, stricter])

    with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter) as get_adapter:
        assert (
            await daemon.run_one(
                api,
                "codex",
                "/tmp",
                10,
                policy_store=store,
                daemon_id=DAEMON_ID,
                grant_verifier=TrustingGrantVerifier(),
            )
            is True
        )

    get_adapter.assert_not_called()
    report = api.report.call_args.args[-1]
    assert report["state"] == "failed"
    assert "capability" in report["summary"].lower()


@pytest.mark.asyncio
async def test_a_normalized_command_in_the_parsed_question_is_not_reported(tmp_path):
    """The decoded report question is checked as well as the raw JSON text."""
    store, version = _store(tmp_path)
    ticket = _ticket(version, scope="run pytest")
    ticket["capability"] = _grant(version, operations=["test.run"])
    api = _api(ticket)
    api.claim.return_value["prompt"] = "run pytest"
    leak = "MARKER-question-leak"
    adapter = SimpleNamespace(
        run=AsyncMock(
            return_value=SimpleNamespace(
                text=completion_report_json(
                    "blocked", leak, next_step='git "status">victim'
                )
            )
        )
    )

    with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter):
        assert (
            await daemon.run_one(
                api,
                "codex",
                "/tmp",
                10,
                policy_store=store,
                daemon_id=DAEMON_ID,
                grant_verifier=TrustingGrantVerifier(),
            )
            is True
        )

    report = api.report.call_args.args[-1]
    assert report["state"] == "failed"
    assert leak not in report["summary"]
    assert leak not in str(report.get("question") or "")
    assert "status" not in report["summary"]


@pytest.mark.asyncio
async def test_a_normalized_command_in_any_decoded_report_field_is_not_reported(tmp_path):
    """A decoded field the body carries (verification) is checked too."""
    store, version = _store(tmp_path)
    ticket = _ticket(version, scope="run pytest")
    ticket["capability"] = _grant(version, operations=["test.run"])
    api = _api(ticket)
    api.claim.return_value["prompt"] = "run pytest"
    leak = "MARKER-question-leak"
    adapter = SimpleNamespace(
        run=AsyncMock(
            return_value=SimpleNamespace(
                text=json.dumps(
                    {
                        "status": "done",
                        "summary": leak,
                        "artifacts": {"commits": [], "files": [], "pr": None},
                        "verification": [
                            {"ran": 'git "status">victim', "result": "passed"}
                        ],
                        "next": "",
                        "handbacks": [],
                    }
                )
            )
        )
    )

    with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter):
        assert (
            await daemon.run_one(
                api,
                "codex",
                "/tmp",
                10,
                policy_store=store,
                daemon_id=DAEMON_ID,
                grant_verifier=TrustingGrantVerifier(),
            )
            is True
        )

    report = api.report.call_args.args[-1]
    assert report["state"] == "failed"
    assert leak not in report["summary"]
    assert leak not in str(report.get("question") or "")
    assert "status" not in report["summary"]


@pytest.mark.asyncio
async def test_a_malformed_verified_capability_fails_with_a_report(tmp_path):
    """A verifier that returns a malformed capability still reports cleanly."""
    from src.conductor_cli.daemon_capability import OperationCapability

    store, version = _store(tmp_path)

    class MalformedVerifier:
        def verify(self, grant):
            return OperationCapability(
                ticket_id="t-cap",
                daemon_id=DAEMON_ID,
                policy_version=version,
                expires_at="not-a-timestamp",  # type: ignore[arg-type]
                operations=frozenset({"repo.read"}),
            )

    ticket = _ticket(version, scope="run git status")
    api = _api(ticket)
    api.claim.return_value["prompt"] = "run git status"
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=completion_report_json())))

    with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter) as get_adapter:
        assert (
            await daemon.run_one(
                api,
                "codex",
                "/tmp",
                10,
                policy_store=store,
                daemon_id=DAEMON_ID,
                grant_verifier=MalformedVerifier(),
            )
            is True
        )

    get_adapter.assert_not_called()
    report = api.report.call_args.args[-1]
    assert report["state"] == "failed"
    assert "could not be authorized" in report["summary"]
