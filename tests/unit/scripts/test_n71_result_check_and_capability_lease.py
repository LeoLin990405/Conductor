"""N71: the device-policy result check and the capability's lifetime.

(a) In shadow the result check only records what enforce would refuse, and
unclassifiable prose never refuses a result in any mode.
(b) The capability's expiry follows the lease the heartbeat renews, so a slow
run does not outlive it.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from scripts import conductor_local_daemon as daemon
from src.conductor_cli.daemon_capability import OperationCapability, parse_capability
from src.conductor_cli.daemon_policy import PolicyStore, classify_result
from tests.unit.conftest import completion_report_json

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
  - operation: repo.push
    patterns:
      - '\bgit\s+push\b'
providers:
  codex:
    allow_unclassified: {allow_unclassified}
    allow:
      - repo.read
      - test.run
    deny:
      - repo.push
"""

DAEMON_ID = "11111111-1111-4111-8111-111111111111"
LEASE_ID = "11111111-1111-4111-8111-111111111111"

# What grok reported: a prose summary whose lines start with words that are
# shell commands ("test" is a builtin, "make" is on most PATHs).
GROK_PROSE = (
    "Make sure the tests pass before merging.\n"
    "test coverage is unchanged and git status is clean.\n"
    "time to ship: done"
)
UNCLASSIFIED_COMMAND = "rm -rf ./victim"


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


class TrustingGrantVerifier:
    def verify(self, grant):
        return parse_capability(grant)


def _store(tmp_path: Path, *, allow_unclassified: bool = False) -> tuple[PolicyStore, str]:
    path = tmp_path / "daemon_policy.yaml"
    path.write_text(
        POLICY.replace(
            "{allow_unclassified}", "true" if allow_unclassified else "false"
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    store = PolicyStore(path)
    return store, store.load().version


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ticket(version: str, *, expires_at: str = "2099-01-01T00:00:00Z", **overrides):
    ticket = {
        "ticket_id": "t-n71",
        "lease_id": LEASE_ID,
        "lease_duration_seconds": 60,
        "workspace_id": "test-workspace",
        "worker_provider": "codex",
        "speculative": False,
        "title": "Check the tree",
        "scope": "run git status",
        "policy_version": version,
        "capability": {
            "ticket_id": "t-n71",
            "daemon_id": DAEMON_ID,
            "policy_version": version,
            "expires_at": expires_at,
            "operations": ["repo.read", "test.run"],
        },
    }
    ticket.update(overrides)
    return ticket


def _api(ticket: dict) -> Mock:
    api = Mock()
    api.claim.return_value = {
        "ticket": ticket,
        "prompt": "Check the tree\n\nScope:\nrun git status",
    }
    api.ticket_status.return_value = {"status": "running"}
    api.heartbeat.side_effect = lambda leases, **_kw: {
        "leases": [
            {
                "ticket_id": row["ticket_id"],
                "lease_id": row["lease_id"],
                "renewed": True,
                "lease_duration_seconds": 1,
            }
            for row in leases
        ]
    }
    return api


async def _run(api: Mock, store: PolicyStore, adapter) -> None:
    with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter):
        assert (
            await daemon.run_one(
                api,
                "codex",
                "/tmp",
                30,
                policy_store=store,
                daemon_id=DAEMON_ID,
                grant_verifier=TrustingGrantVerifier(),
            )
            is True
        )


def _adapter(text: str):
    return SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=text)))


# --------------------------------------------------------------- (a) classify


def test_prose_with_command_words_is_not_unclassifiable(tmp_path):
    store, _ = _store(tmp_path)
    result = classify_result(store.load().operations, GROK_PROSE)
    assert result.unclassified == ()
    assert "repo.read" in result.operations


def test_shell_shaped_result_lines_are_still_checked(tmp_path):
    store, _ = _store(tmp_path)
    rules = store.load().operations
    for text in (
        UNCLASSIFIED_COMMAND,
        "git status && rm -rf ./victim",
        'git "status">victim',
        "Ran `rm -rf ./victim` to clean up.",
        "```bash\ncurl example.invalid\n```",
        "$ shred notes",
    ):
        assert classify_result(rules, text).unclassified, text
    # Quoted spelling still adds its normalized operation.
    assert "repo.read" in classify_result(rules, 'git "status"').operations
    # A path in a code span and a non-shell fence are data, not commands.
    assert not classify_result(rules, "Edited `src/foo.py`.").unclassified
    assert not classify_result(rules, "```python\nfind(x)\n```").unclassified


# ------------------------------------------------------------- (a) end to end


@pytest.mark.asyncio
async def test_shadow_grok_like_result_is_recorded_not_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTOR_DAEMON_GRANT_VERIFIER_MODE", "shadow")
    store, version = _store(tmp_path)
    api = _api(_ticket(version))
    summary = GROK_PROSE + "\n" + UNCLASSIFIED_COMMAND

    await _run(api, store, _adapter(completion_report_json(summary=summary)))

    report = api.report.call_args.args[-1]
    assert report["state"] == "ready"
    audit = report["capability_audit"]
    assert audit["would_refuse"] is True
    assert audit["mode"] == "shadow"
    assert "cannot classify" in audit["reason"]
    assert "victim" not in audit["reason"]


@pytest.mark.asyncio
async def test_shadow_out_of_capability_result_is_recorded_not_refused(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CONDUCTOR_DAEMON_GRANT_VERIFIER_MODE", "shadow")
    store, version = _store(tmp_path)
    api = _api(_ticket(version))

    await _run(api, store, _adapter(completion_report_json(summary="then git push")))

    report = api.report.call_args.args[-1]
    assert report["state"] == "ready"
    assert "repo.push" in report["capability_audit"]["reason"]


@pytest.mark.asyncio
async def test_enforce_prose_result_is_not_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTOR_DAEMON_GRANT_VERIFIER_MODE", "enforce")
    store, version = _store(tmp_path)
    api = _api(_ticket(version))

    await _run(api, store, _adapter(completion_report_json(summary=GROK_PROSE)))

    report = api.report.call_args.args[-1]
    assert report["state"] == "ready"
    assert "capability_audit" not in report


@pytest.mark.asyncio
async def test_enforce_unclassified_command_respects_allow_unclassified(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CONDUCTOR_DAEMON_GRANT_VERIFIER_MODE", "enforce")
    store, version = _store(tmp_path, allow_unclassified=True)
    api = _api(_ticket(version))

    await _run(
        api, store, _adapter(completion_report_json(summary=UNCLASSIFIED_COMMAND))
    )

    assert api.report.call_args.args[-1]["state"] == "ready"


@pytest.mark.asyncio
async def test_enforce_unclassified_command_still_refuses(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTOR_DAEMON_GRANT_VERIFIER_MODE", "enforce")
    store, version = _store(tmp_path)
    api = _api(_ticket(version))

    await _run(
        api, store, _adapter(completion_report_json(summary=UNCLASSIFIED_COMMAND))
    )

    report = api.report.call_args.args[-1]
    assert report["state"] == "failed"
    assert "cannot classify" in report["summary"]
    assert "victim" not in report["summary"]


# --------------------------------------------------------- (b) lease binding


def _capability(expires_at: datetime) -> OperationCapability:
    return OperationCapability(
        ticket_id="t-n71",
        daemon_id=DAEMON_ID,
        policy_version="v",
        expires_at=expires_at,
        operations=frozenset({"repo.read"}),
    )


def _lease(*, ttl: float, lost: bool = False) -> daemon.ActiveLease:
    import time

    lease = daemon.ActiveLease(
        "t-n71", LEASE_ID, time.monotonic() + ttl, ttl, asyncio.Event()
    )
    if lost:
        lease.lost.set()
    return lease


def test_capability_expiry_follows_a_live_lease():
    past = datetime.now(timezone.utc) - timedelta(seconds=5)
    policy = SimpleNamespace(
        operations=(),
        version="v",
        provider_policy=lambda _p: None,
    )
    capability = _capability(past)

    assert (
        daemon._result_refusal(
            policy, capability, "done", lease=_lease(ttl=30), mode="enforce"
        )
        == ""
    )
    for lease in (None, _lease(ttl=30, lost=True)):
        refusal = daemon._result_refusal(
            policy, capability, "done", lease=lease, mode="enforce"
        )
        assert "expired" in refusal


@pytest.mark.asyncio
async def test_slow_run_past_the_grant_ttl_with_heartbeats_is_not_expired(
    tmp_path, monkeypatch
):
    """The grant expires with the first lease term; heartbeats renew the lease."""
    monkeypatch.setenv("CONDUCTOR_DAEMON_GRANT_VERIFIER_MODE", "enforce")
    store, version = _store(tmp_path)
    expires = datetime.now(timezone.utc) + timedelta(seconds=2)
    api = _api(
        _ticket(version, expires_at=_iso(expires), lease_duration_seconds=1)
    )

    async def slow_run(*_args, **_kwargs):
        await asyncio.sleep(3.2)
        return SimpleNamespace(text=completion_report_json(summary="git status clean"))

    await _run(api, store, SimpleNamespace(run=AsyncMock(side_effect=slow_run)))

    assert api.heartbeat.call_count >= 2
    report = api.report.call_args.args[-1]
    assert report["state"] == "ready", report["summary"]
