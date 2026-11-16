"""N44: a policy-backed daemon verifies the claim's signed lease grant."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from scripts import conductor_local_daemon as daemon
from src.conductor_cli.daemon_grant import (
    NonceStore,
    OriginCheck,
    SigningKey,
    TrustedKeys,
    sign_grant,
)
from src.conductor_cli.daemon_policy import PolicyStore
from tests.unit.conftest import completion_report_json


@pytest.fixture(autouse=True)
def _enforce_grant_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests pin N44's enforce behaviour; the modes have their own tests (N58)."""
    monkeypatch.setenv("CONDUCTOR_DAEMON_GRANT_VERIFIER_MODE", "enforce")


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
providers:
  codex:
    allow:
      - repo.read
      - test.run
"""

DAEMON_ID = "11111111-1111-4111-8111-111111111111"
TICKET_ID = "22222222-2222-4222-8222-222222222222"
LEASE_ID = "33333333-3333-4333-8333-333333333333"


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


def _write_keys(path: Path, *keys: SigningKey) -> None:
    path.write_text(
        json.dumps(
            {
                "format": 1,
                "keys": [
                    {"kid": key.kid, "public_key": key.public_b64()} for key in keys
                ],
            }
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def _policy_store(tmp_path: Path) -> tuple[PolicyStore, str]:
    path = tmp_path / "daemon_policy.yaml"
    path.write_text(POLICY, encoding="utf-8")
    os.chmod(path, 0o600)
    store = PolicyStore(path)
    return store, store.load().version


def _lease_grant(key: SigningKey, *, version: str, **overrides) -> dict:
    """The top-level claim grant the server actually emits."""
    now = int(time.time())
    fields = {
        "key": key,
        "lease_id": LEASE_ID,
        "ticket_id": TICKET_ID,
        "daemon_id": DAEMON_ID,
        "policy_version": version,
        "expires_at": now + 600,
        "issued_at": now,
    }
    fields.update(overrides)
    return sign_grant(**fields)


def _ticket(version: str, **overrides) -> dict:
    # No ticket.capability: matches src/server/app/autonomy.py claim shape.
    ticket = {
        "ticket_id": TICKET_ID,
        "lease_id": LEASE_ID,
        "lease_duration_seconds": 60,
        "workspace_id": "test-workspace",
        "worker_provider": "codex",
        "speculative": False,
        "title": "Check the tree",
        "scope": "run git status",
        "policy_version": version,
        "daemon_id": DAEMON_ID,
    }
    ticket.update(overrides)
    return ticket


def _api(ticket: dict, grant: dict | None) -> Mock:
    api = Mock()
    payload = {
        "ticket": ticket,
        "prompt": "Check the tree\n\nScope:\nrun git status",
    }
    if grant is not None:
        payload["grant"] = grant
    api.claim.return_value = payload
    api.lease_status.return_value = {
        "revoked": False,
        "revoked_at": None,
        "daemon_id": DAEMON_ID,
    }
    return api


def test_grant_verifier_builds_from_trusted_keys(tmp_path):
    key = SigningKey.generate("server")
    keys_path = tmp_path / "keys.json"
    _write_keys(keys_path, key)

    verifier = daemon._grant_verifier(
        argparse.Namespace(trusted_keys=str(keys_path), daemon_id=DAEMON_ID)
    )

    assert isinstance(verifier, daemon._SignedLeaseCapabilityVerifier)
    assert verifier.daemon_id == DAEMON_ID
    assert verifier.keys.public_key("server") == key.public_bytes()


def test_grant_verifier_is_absent_without_trusted_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.conductor_cli.daemon_grant.DEFAULT_TRUSTED_KEYS",
        tmp_path / "absent.json",
    )

    verifier = daemon._grant_verifier(
        argparse.Namespace(trusted_keys="", daemon_id=DAEMON_ID)
    )

    assert verifier is None


def test_grant_verifier_is_absent_when_trusted_keys_are_unusable(tmp_path):
    path = tmp_path / "keys.json"
    path.write_text("{not-json", encoding="utf-8")
    os.chmod(path, 0o600)

    verifier = daemon._grant_verifier(
        argparse.Namespace(trusted_keys=str(path), daemon_id=DAEMON_ID)
    )

    assert verifier is None


def test_grant_verifier_is_absent_when_trusted_keys_are_not_utf8(tmp_path):
    path = tmp_path / "keys.json"
    path.write_bytes(b"\xff\xfe not utf-8")
    os.chmod(path, 0o600)

    verifier = daemon._grant_verifier(
        argparse.Namespace(trusted_keys=str(path), daemon_id=DAEMON_ID)
    )

    assert verifier is None


def test_verifier_accepts_the_server_lease_grant_format():
    """The review repro: a lease grant (no operations) must verify."""
    key = SigningKey.generate("server")
    grant = sign_grant(
        key=key,
        lease_id="l",
        ticket_id="t",
        daemon_id="d",
        policy_version="a" * 64,
        expires_at=int(time.time()) + 600,
    )
    verifier = daemon._SignedLeaseCapabilityVerifier(
        TrustedKeys.trust(key), daemon_id="d"
    )

    capability = verifier.verify(grant)

    assert capability.ticket_id == "t"
    assert capability.daemon_id == "d"
    assert capability.policy_version == "a" * 64
    assert capability.operations == frozenset()


def test_unsigned_operations_on_a_lease_grant_are_refused():
    """operations is outside the signed payload — a relay must not widen it."""
    from src.conductor_cli.daemon_capability import GrantError, authorize

    key = SigningKey.generate("k")
    grant = sign_grant(
        key=key,
        lease_id="l",
        ticket_id="t",
        daemon_id="d",
        policy_version="v",
        expires_at=int(time.time()) + 600,
    )
    verifier = daemon._SignedLeaseCapabilityVerifier(
        TrustedKeys.trust(key), daemon_id="d"
    )
    allowed = frozenset({"repo.read", "test.run"})

    # Baseline: a clean lease grant authorizes under the local allowlist fill.
    clean = verifier.verify(grant)
    assert clean.operations == frozenset()
    filled = type(clean)(
        ticket_id=clean.ticket_id,
        daemon_id=clean.daemon_id,
        policy_version=clean.policy_version,
        expires_at=clean.expires_at,
        operations=allowed,
    )
    assert authorize(
        filled,
        ticket_id="t",
        daemon_id="d",
        policy_version="v",
        allowed_operations=allowed,
        requested={"repo.read"},
    ).allowed

    # Tamper: attach unsigned operations (signature still verifies for lease fields).
    widened = dict(grant)
    widened["operations"] = ["repo.read", "test.run", "repo.push"]
    with pytest.raises(GrantError, match="unsigned operation"):
        verifier.verify(widened)

    # Tamper: empty operations list is still an unsigned field.
    stripped = dict(grant)
    stripped["operations"] = []
    with pytest.raises(GrantError, match="unsigned operation"):
        verifier.verify(stripped)


@pytest.mark.asyncio
async def test_policy_keys_and_signed_lease_let_a_ticket_run(tmp_path):
    """End-to-end on the actual claim format: policy + keys + signed lease."""
    key = SigningKey.generate("server")
    store, version = _policy_store(tmp_path)
    lease_grant = _lease_grant(key, version=version)
    # No ticket.capability — only the top-level grant the server returns.
    api = _api(_ticket(version), grant=lease_grant)
    origin = OriginCheck(TrustedKeys.trust(key), NonceStore(tmp_path / "nonces.json"))
    verifier = daemon._SignedLeaseCapabilityVerifier(
        TrustedKeys.trust(key), daemon_id=DAEMON_ID
    )
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=completion_report_json(summary="clean"))))

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
                expected_daemon_id=DAEMON_ID,
                origin=origin,
                grant_verifier=verifier,
            )
            is True
        )

    get_adapter.assert_called_once()
    report = api.report.call_args.args[-1]
    assert report["state"] == "ready"
    assert report["summary"] == "clean"


@pytest.mark.asyncio
async def test_a_lease_signed_by_an_untrusted_key_never_reaches_the_cli(tmp_path):
    trusted = SigningKey.generate("server")
    other = SigningKey.generate("other")
    store, version = _policy_store(tmp_path)
    api = _api(_ticket(version), grant=_lease_grant(other, version=version))
    verifier = daemon._SignedLeaseCapabilityVerifier(
        TrustedKeys.trust(trusted), daemon_id=DAEMON_ID
    )
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=completion_report_json(summary="clean"))))

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
                grant_verifier=verifier,
            )
            is True
        )

    get_adapter.assert_not_called()
    summary = api.report.call_args.args[-1]["summary"]
    assert "rejected" in summary or "trusted" in summary or "signed grant" in summary


@pytest.mark.asyncio
async def test_a_lease_with_tampered_unsigned_operations_never_reaches_the_cli(
    tmp_path,
):
    key = SigningKey.generate("server")
    store, version = _policy_store(tmp_path)
    grant = _lease_grant(key, version=version)
    grant["operations"] = ["repo.read", "test.run", "repo.push"]
    api = _api(_ticket(version), grant=grant)
    verifier = daemon._SignedLeaseCapabilityVerifier(
        TrustedKeys.trust(key), daemon_id=DAEMON_ID
    )

    with patch(
        "src.llms.native_cli.registry.get_adapter",
        return_value=SimpleNamespace(run=AsyncMock()),
    ) as get_adapter:
        assert (
            await daemon.run_one(
                api,
                "codex",
                "/tmp",
                10,
                policy_store=store,
                daemon_id=DAEMON_ID,
                grant_verifier=verifier,
            )
            is True
        )

    get_adapter.assert_not_called()
    summary = api.report.call_args.args[-1]["summary"]
    assert "rejected" in summary
    assert "unsigned" in summary


@pytest.mark.asyncio
async def test_a_lease_bound_to_another_lease_id_is_refused(tmp_path):
    key = SigningKey.generate("server")
    store, version = _policy_store(tmp_path)
    other_lease = "44444444-4444-4444-8444-444444444444"
    grant = _lease_grant(key, version=version, lease_id=other_lease)
    api = _api(_ticket(version), grant=grant)
    verifier = daemon._SignedLeaseCapabilityVerifier(
        TrustedKeys.trust(key), daemon_id=DAEMON_ID
    )

    with patch(
        "src.llms.native_cli.registry.get_adapter",
        return_value=SimpleNamespace(run=AsyncMock()),
    ) as get_adapter:
        assert (
            await daemon.run_one(
                api,
                "codex",
                "/tmp",
                10,
                policy_store=store,
                daemon_id=DAEMON_ID,
                grant_verifier=verifier,
            )
            is True
        )

    get_adapter.assert_not_called()
    assert "different lease" in api.report.call_args.args[-1]["summary"]


@pytest.mark.asyncio
async def test_a_lease_bound_to_another_daemon_id_is_refused(tmp_path):
    key = SigningKey.generate("server")
    store, version = _policy_store(tmp_path)
    other_daemon = "55555555-5555-4555-8555-555555555555"
    grant = _lease_grant(key, version=version, daemon_id=other_daemon)
    api = _api(_ticket(version), grant=grant)
    verifier = daemon._SignedLeaseCapabilityVerifier(
        TrustedKeys.trust(key), daemon_id=DAEMON_ID
    )

    with patch(
        "src.llms.native_cli.registry.get_adapter",
        return_value=SimpleNamespace(run=AsyncMock()),
    ) as get_adapter:
        assert (
            await daemon.run_one(
                api,
                "codex",
                "/tmp",
                10,
                policy_store=store,
                daemon_id=DAEMON_ID,
                grant_verifier=verifier,
            )
            is True
        )

    get_adapter.assert_not_called()
    summary = api.report.call_args.args[-1]["summary"]
    assert "rejected" in summary
    assert "different daemon" in summary


@pytest.mark.asyncio
async def test_a_lease_bound_to_another_policy_version_is_refused(tmp_path):
    key = SigningKey.generate("server")
    store, version = _policy_store(tmp_path)
    grant = _lease_grant(key, version="b" * 64)
    api = _api(_ticket(version), grant=grant)
    verifier = daemon._SignedLeaseCapabilityVerifier(
        TrustedKeys.trust(key), daemon_id=DAEMON_ID
    )

    with patch(
        "src.llms.native_cli.registry.get_adapter",
        return_value=SimpleNamespace(run=AsyncMock()),
    ) as get_adapter:
        assert (
            await daemon.run_one(
                api,
                "codex",
                "/tmp",
                10,
                policy_store=store,
                daemon_id=DAEMON_ID,
                grant_verifier=verifier,
            )
            is True
        )

    get_adapter.assert_not_called()
    summary = api.report.call_args.args[-1]["summary"]
    assert "policy" in summary


def test_grant_verifier_is_absent_without_a_daemon_id(tmp_path):
    """Fail closed: an unbound verifier would accept any daemon's grant."""
    key = SigningKey.generate("server")
    keys_path = tmp_path / "keys.json"
    _write_keys(keys_path, key)

    verifier = daemon._grant_verifier(
        argparse.Namespace(trusted_keys=str(keys_path))
    )

    assert verifier is None


def test_signed_lease_verifier_requires_a_daemon_id():
    key = SigningKey.generate("server")
    with pytest.raises(ValueError, match="daemon id"):
        daemon._SignedLeaseCapabilityVerifier(TrustedKeys.trust(key), daemon_id="")


@pytest.mark.asyncio
async def test_built_verifier_runs_a_ticket_with_only_the_claim_grant(tmp_path):
    key = SigningKey.generate("server")
    keys_path = tmp_path / "keys.json"
    _write_keys(keys_path, key)
    store, version = _policy_store(tmp_path)
    verifier = daemon._grant_verifier(
        argparse.Namespace(trusted_keys=str(keys_path), daemon_id=DAEMON_ID)
    )
    assert verifier is not None
    api = _api(_ticket(version), grant=_lease_grant(key, version=version))
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=completion_report_json())))

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
                grant_verifier=verifier,
            )
            is True
        )

    get_adapter.assert_called_once()
    assert api.report.call_args.args[-1]["state"] == "ready"
