"""An unsigned or badly-signed lease never reaches the native CLI."""

from __future__ import annotations

import argparse
import os
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from scripts import conductor_local_daemon as daemon
from src.conductor_cli.daemon_grant import (
    OriginCheck,
    SigningKey,
    TrustedKeys,
    sign_grant,
)
from src.conductor_cli.daemon_policy import parse_policy
from src.server.services.autonomy.daemon_grants import report_divergence
from tests.unit.conftest import completion_report_json

_POLICY_SEED = parse_policy(
    {
        "format": 1,
        "read_only_operations": ["repo.read"],
        "operations": [
            {"operation": "repo.read", "patterns": [r"\bgit\s+status\b"]},
        ],
        "providers": {"codex": {"allow": ["repo.read"]}},
    }
)

TICKET_ID = "66666666-6666-4666-8666-666666666666"
LEASE_ID = "77777777-7777-4777-8777-777777777777"
DAEMON_ID = "55555555-5555-4555-8555-555555555555"


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


def _ticket(**overrides) -> dict:
    ticket = {
        "ticket_id": TICKET_ID,
        "lease_id": LEASE_ID,
        "lease_duration_seconds": 60,
        "worker_provider": "codex",
        "speculative": False,
        "policy_version": "abc123",
        "title": "Check the tree",
        "scope": "run git status",
        "workspace_id": "test-workspace",
    }
    ticket.update(overrides)
    return ticket


def _grant(key: SigningKey, ticket: dict, **overrides) -> dict:
    now = int(time.time())
    fields = {
        "key": key,
        "lease_id": ticket["lease_id"],
        "ticket_id": ticket["ticket_id"],
        "daemon_id": DAEMON_ID,
        "policy_version": ticket["policy_version"],
        "expires_at": now + 60,
        "issued_at": now,
    }
    fields.update(overrides)
    return sign_grant(**fields)


def _origin(tmp_path: Path, *keys: SigningKey) -> OriginCheck:
    from src.conductor_cli.daemon_grant import NonceStore

    return OriginCheck(TrustedKeys.trust(*keys), NonceStore(tmp_path / "nonces.json"))


def _api(ticket: dict, grant: dict | None) -> Mock:
    api = Mock()
    payload = {
        "ticket": ticket,
        "prompt": "Check the tree\n\nScope:\nrun git status",
    }
    if grant is not None:
        payload["grant"] = grant
    api.claim.return_value = payload
    api.lease_status.return_value = {"revoked": False, "revoked_at": None}
    return api


async def _run(api: Mock, origin: OriginCheck | None):
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=completion_report_json())))
    with patch(
        "src.llms.native_cli.registry.get_adapter", return_value=adapter
    ) as get_adapter:
        result = await daemon.run_one(
            api, "codex", "/tmp", 10, origin=origin
        )
    return result, get_adapter, adapter


def _signed_binding(grant: dict) -> dict:
    return {
        "daemon_id": grant["daemon_id"],
        "policy_version": grant["policy_version"],
        "grant": grant,
        "revoked_at": None,
    }


def _report_lands(api: Mock, grant: dict) -> dict:
    report = api.report.call_args.args[-1]
    assert (
        report_divergence(
            _signed_binding(grant),
            daemon_id=str(report.get("daemon_id") or ""),
            policy_version=str(report.get("policy_version") or ""),
            state=report["state"],
        )
        == ""
    )
    return report


class _ReplacedPolicy:
    """The file matches the lease until the run is underway, then it moves."""

    def __init__(self) -> None:
        self.reads = 0
        # Outside every workspace and the isolation root, like a real policy.
        self.path = Path("/nonexistent/conductor/daemon_policy.yaml")

    def current(self):
        self.reads += 1
        version = "abc123" if self.reads < 3 else "replaced"
        return replace(_POLICY_SEED, version=version)


def _refused(get_adapter, api: Mock) -> str:
    get_adapter.assert_not_called()
    report = api.report.call_args.args[-1]
    assert report["state"] == "failed"
    return report["summary"]


@pytest.mark.asyncio
async def test_a_valid_grant_runs_the_cli(tmp_path):
    key = SigningKey.generate("server")
    ticket = _ticket()
    api = _api(ticket, _grant(key, ticket))

    result, get_adapter, adapter = await _run(api, _origin(tmp_path, key))

    assert result is True
    get_adapter.assert_called_once()
    adapter.run.assert_awaited()
    api.lease_status.assert_called_once_with(LEASE_ID)
    report = api.report.call_args.args[-1]
    assert report["daemon_id"] == DAEMON_ID
    assert report["policy_version"] == "abc123"


@pytest.mark.asyncio
async def test_an_expired_grant_never_reaches_the_cli(tmp_path):
    key = SigningKey.generate("server")
    ticket = _ticket()
    now = int(time.time())
    api = _api(
        ticket,
        _grant(key, ticket, expires_at=now - 1, issued_at=now - 30),
    )

    _result, get_adapter, _adapter = await _run(api, _origin(tmp_path, key))

    assert "expired" in _refused(get_adapter, api)


@pytest.mark.asyncio
async def test_a_grant_signed_by_the_wrong_key_never_reaches_the_cli(tmp_path):
    trusted = SigningKey.generate("server")
    other = SigningKey.generate("other")
    ticket = _ticket()
    api = _api(ticket, _grant(other, ticket))

    _result, get_adapter, _adapter = await _run(api, _origin(tmp_path, trusted))

    assert "trusted server key" in _refused(get_adapter, api)


@pytest.mark.asyncio
async def test_a_grant_signed_by_the_rotated_key_runs_the_cli(tmp_path):
    old = SigningKey.generate("2026-old")
    new = SigningKey.generate("2026-new")
    ticket = _ticket()
    api = _api(ticket, _grant(new, ticket))

    result, get_adapter, adapter = await _run(api, _origin(tmp_path, old, new))

    assert result is True
    get_adapter.assert_called_once()
    adapter.run.assert_awaited()


@pytest.mark.asyncio
async def test_a_replayed_grant_never_reaches_the_cli(tmp_path):
    key = SigningKey.generate("server")
    ticket = _ticket()
    origin = _origin(tmp_path, key)
    api = _api(ticket, _grant(key, ticket))
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=completion_report_json())))

    with patch(
        "src.llms.native_cli.registry.get_adapter", return_value=adapter
    ) as get_adapter:
        assert await daemon.run_one(api, "codex", "/tmp", 10, origin=origin) is True
        assert await daemon.run_one(api, "codex", "/tmp", 10, origin=origin) is True

    assert get_adapter.call_count == 1
    assert "already used" in api.report.call_args.args[-1]["summary"]


@pytest.mark.asyncio
async def test_a_revoked_grant_never_reaches_the_cli(tmp_path):
    key = SigningKey.generate("server")
    ticket = _ticket()
    api = _api(ticket, _grant(key, ticket))
    api.lease_status.return_value = {
        "revoked": True,
        "revoked_at": "2026-11-03T00:00:00+00:00",
    }

    _result, get_adapter, _adapter = await _run(api, _origin(tmp_path, key))

    assert "revoked" in _refused(get_adapter, api)


@pytest.mark.asyncio
async def test_an_unsigned_grant_never_reaches_the_cli(tmp_path):
    key = SigningKey.generate("server")
    api = _api(_ticket(), None)

    _result, get_adapter, _adapter = await _run(api, _origin(tmp_path, key))

    assert "no signed grant" in _refused(get_adapter, api)


@pytest.mark.asyncio
async def test_an_unreachable_revocation_poll_fails_closed(tmp_path):
    key = SigningKey.generate("server")
    ticket = _ticket()
    api = _api(ticket, _grant(key, ticket))
    api.lease_status.side_effect = daemon.ApiError("down", status=503, retryable=True)

    _result, get_adapter, _adapter = await _run(api, _origin(tmp_path, key))

    assert "could not be confirmed" in _refused(get_adapter, api)
    assert api.lease_status.call_count == 1


@pytest.mark.asyncio
async def test_a_daemon_without_trusted_keys_still_runs_an_unsigned_ticket():
    api = _api(_ticket(), None)
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=completion_report_json())))

    with patch(
        "src.llms.native_cli.registry.get_adapter", return_value=adapter
    ) as get_adapter:
        assert await daemon.run_one(api, "codex", "/tmp", 10) is True

    get_adapter.assert_called_once()
    api.lease_status.assert_not_called()


@pytest.mark.asyncio
async def test_a_daemon_without_keys_reports_a_grant_carrying_lease():
    key = SigningKey.generate("server")
    ticket = _ticket(daemon_id=DAEMON_ID)
    grant = _grant(key, ticket)
    api = _api(ticket, grant)
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=completion_report_json())))

    with patch(
        "src.llms.native_cli.registry.get_adapter", return_value=adapter
    ) as get_adapter:
        assert await daemon.run_one(api, "codex", "/tmp", 10) is True

    get_adapter.assert_called_once()
    adapter.run.assert_awaited()
    api.lease_status.assert_not_called()
    report = _report_lands(api, grant)
    assert report["state"] == "ready"
    assert report["daemon_id"] == DAEMON_ID
    assert report["policy_version"] == "abc123"


@pytest.mark.asyncio
async def test_a_refused_grant_reports_without_the_signed_identity(tmp_path):
    key = SigningKey.generate("server")
    ticket = _ticket(daemon_id=DAEMON_ID)
    now = int(time.time())
    grant = _grant(key, ticket, expires_at=now - 1, issued_at=now - 30)
    api = _api(ticket, grant)

    _result, get_adapter, _adapter = await _run(api, _origin(tmp_path, key))

    assert "expired" in _refused(get_adapter, api)
    report = _report_lands(api, grant)
    assert report.get("daemon_id", "") == ""
    assert report.get("policy_version", "") == ""


@pytest.mark.asyncio
async def test_an_early_cancel_reports_without_the_signed_identity(tmp_path):
    key = SigningKey.generate("server")
    ticket = _ticket(daemon_id=DAEMON_ID)
    grant = _grant(key, ticket)
    api = _api(ticket, grant)
    manager = daemon.LeaseManager(api)

    await daemon.run_claimed(
        api,
        "codex",
        "/tmp",
        10,
        {"ticket": ticket, "grant": grant, "prompt": "Check the tree"},
        manager,
        cancel_before_start=True,
        origin=_origin(tmp_path, key),
    )

    report = _report_lands(api, grant)
    assert report["state"] == "failed"
    assert "cancelled" in report["summary"]
    assert report.get("daemon_id", "") == ""
    assert report.get("policy_version", "") == ""
    api.lease_status.assert_not_called()


@pytest.mark.asyncio
async def test_a_policy_replaced_during_the_run_still_reports_the_grant(tmp_path):
    key = SigningKey.generate("server")
    ticket = _ticket(daemon_id=DAEMON_ID)
    grant = _grant(key, ticket)
    api = _api(ticket, grant)
    store = _ReplacedPolicy()
    adapter = SimpleNamespace(run=AsyncMock(side_effect=RuntimeError("cli down")))

    with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter):
        assert (
            await daemon.run_one(
                api,
                "codex",
                "/tmp",
                10,
                origin=_origin(tmp_path, key),
                policy_store=store,
            )
            is True
        )

    report = _report_lands(api, grant)
    assert report["state"] == "failed"
    assert report["daemon_id"] == DAEMON_ID
    assert report["policy_version"] == "abc123"
    assert store.reads == 2


class TestOriginResolution:
    def test_a_trusted_key_file_is_loaded(self, tmp_path):
        key = SigningKey.generate("server")
        path = tmp_path / "keys.json"
        path.write_text(
            '{"format": 1, "keys": [{"kid": "server", "public_key": "%s"}]}'
            % key.public_b64(),
            encoding="utf-8",
        )

        origin, error = daemon._open_origin(
            argparse.Namespace(
                trusted_keys=str(path), require_origin=False, nonce_file=str(tmp_path / "n.json")
            )
        )

        assert error == ""
        assert origin is not None
        assert origin.keys.public_key("server") == key.public_bytes()

    def test_a_missing_key_file_stops_the_daemon_when_origin_is_required(self, tmp_path):
        origin, error = daemon._open_origin(
            argparse.Namespace(
                trusted_keys=str(tmp_path / "absent.json"),
                require_origin=True,
                nonce_file="",
            )
        )

        assert origin is None
        assert "No trusted server keys" in error

    def test_no_key_file_leaves_origin_unchecked(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "src.conductor_cli.daemon_grant.DEFAULT_TRUSTED_KEYS",
            tmp_path / "absent.json",
        )

        origin, error = daemon._open_origin(
            argparse.Namespace(trusted_keys="", require_origin=False, nonce_file="")
        )

        assert origin is None
        assert error == ""


@pytest.mark.asyncio
async def test_origin_files_inside_a_worker_writable_mount_are_refused(tmp_path):
    """Writable trusted keys or a reset replay record would defeat origin checks."""
    from src.conductor_cli.daemon_grant import NonceStore

    key = SigningKey.generate("server")
    ticket = _ticket(daemon_id=DAEMON_ID)
    grant = _grant(key, ticket)
    workspace = tmp_path / "workspace"
    origin = OriginCheck(
        TrustedKeys(keys={key.kid: key.public_bytes()}, path=workspace / "keys.json"),
        NonceStore(workspace / "nonces.json"),
    )
    api = _api(ticket, grant)
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=completion_report_json())))

    with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter) as get_adapter:
        assert (
            await daemon.run_one(api, "codex", "/tmp", 10, origin=origin) is True
        )

    get_adapter.assert_not_called()
    report = api.report.call_args.args[-1]
    assert report["state"] == "failed"
    assert "worker-writable" in report["summary"]


@pytest.mark.asyncio
async def test_a_signed_grant_for_another_daemon_is_refused_locally(tmp_path):
    key = SigningKey.generate("server")
    ticket = _ticket(daemon_id=DAEMON_ID)
    grant = _grant(key, ticket, daemon_id="99999999-9999-4999-8999-999999999999")
    api = _api(ticket, grant)
    adapter = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text=completion_report_json())))

    with patch("src.llms.native_cli.registry.get_adapter", return_value=adapter) as get_adapter:
        assert (
            await daemon.run_one(
                api,
                "codex",
                "/tmp",
                10,
                origin=_origin(tmp_path, key),
                expected_daemon_id=DAEMON_ID,
            )
            is True
        )

    get_adapter.assert_not_called()
    assert "does not match" in api.report.call_args.args[-1]["summary"]


@pytest.mark.asyncio
async def test_a_revocation_status_naming_another_daemon_is_refused():
    api = Mock()
    api.lease_status.return_value = {
        "revoked": False,
        "revoked_at": None,
        "daemon_id": "99999999-9999-4999-8999-999999999999",
    }

    reason = await daemon._revocation_rejection(
        api, LEASE_ID, expected_daemon_id=DAEMON_ID
    )

    assert "different daemon" in reason


@pytest.mark.asyncio
async def test_a_grant_that_expires_during_the_revocation_poll_is_refused(
    tmp_path, monkeypatch
):
    key = SigningKey.generate("server")
    ticket = _ticket(daemon_id=DAEMON_ID)
    grant = _grant(key, ticket, expires_at=int(time.time()) + 5)
    origin = _origin(tmp_path, key)
    api = Mock()
    api.lease_status.return_value = {"revoked": False, "revoked_at": None}
    clock = {"now": int(time.time())}

    async def slow_poll(*_args, **_kwargs):
        clock["now"] += 100  # the poll took longer than the grant's life
        return ""

    monkeypatch.setattr(daemon, "_revocation_rejection", slow_poll)
    monkeypatch.setattr(daemon, "time", SimpleNamespace(time=lambda: clock["now"]))

    reason, _verified = await daemon._origin_rejection(
        origin, api, ticket, grant, expected_daemon_id=DAEMON_ID
    )

    assert "expired" in reason
    assert origin.nonces.seen(str(grant["nonce"]), now=clock["now"]) is False


def test_the_replay_location_authorization_uses_the_pinned_path(tmp_path):
    """Location auth must check the pinned ledger, not a re-resolved alias."""
    from src.conductor_cli.daemon_grant import NonceStore

    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = tmp_path / "state"
    alias.symlink_to(workspace, target_is_directory=True)
    nonces = NonceStore(alias / "nonces.json")  # pins inside the workspace
    alias.unlink()
    alias.symlink_to(outside, target_is_directory=True)  # repointed after pinning
    origin = OriginCheck(
        TrustedKeys(keys={"k": b"x" * 32}, path=outside / "keys.json"), nonces
    )

    reason = daemon._origin_location_rejection(origin, str(workspace), "")

    assert "worker-writable" in reason


def test_the_trusted_key_location_authorization_uses_the_pinned_path(tmp_path):
    """Location auth must check the pinned key file, not a re-resolved alias."""
    import json
    import os

    key = SigningKey.generate("server")
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    key_file = workspace / "keys.json"
    key_file.write_text(
        json.dumps(
            {
                "format": 1,
                "keys": [{"kid": key.kid, "public_key": key.public_b64()}],
            }
        ),
        encoding="utf-8",
    )
    os.chmod(key_file, 0o600)
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = tmp_path / "state"
    alias.symlink_to(workspace, target_is_directory=True)
    trust = TrustedKeys.load(alias / "keys.json")  # pins inside the workspace
    alias.unlink()
    alias.symlink_to(outside, target_is_directory=True)  # repointed after load
    from src.conductor_cli.daemon_grant import NonceStore

    origin = OriginCheck(trust, NonceStore(outside / "nonces.json"))

    reason = daemon._origin_location_rejection(origin, str(workspace), "")

    assert "worker-writable" in reason


def test_a_trusted_key_alias_changed_during_loading_cannot_move_the_authorization(
    tmp_path, monkeypatch
):
    """Keys are read and authorized from the location pinned before opening."""
    import json
    import os

    from src.conductor_cli import daemon_grant as grant_module

    key = SigningKey.generate("server")
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    key_file = workspace / "keys.json"
    key_file.write_text(
        json.dumps(
            {
                "format": 1,
                "keys": [{"kid": key.kid, "public_key": key.public_b64()}],
            }
        ),
        encoding="utf-8",
    )
    os.chmod(key_file, 0o600)
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = tmp_path / "state"
    alias.symlink_to(workspace, target_is_directory=True)
    real_read = grant_module.read_owned_text

    def racing_read(target, label, **kwargs):
        text = real_read(target, label, **kwargs)
        # The alias is re-pointed after the bytes were read but before the old
        # implementation resolved the authorization path.
        alias.unlink()
        alias.symlink_to(outside, target_is_directory=True)
        return text

    monkeypatch.setattr(grant_module, "read_owned_text", racing_read)
    trust = TrustedKeys.load(alias / "keys.json")

    assert trust.resolved_path is not None
    assert str(trust.resolved_path).startswith(str(workspace.resolve()))
    from src.conductor_cli.daemon_grant import NonceStore

    origin = OriginCheck(trust, NonceStore(outside / "nonces.json"))
    reason = daemon._origin_location_rejection(origin, str(workspace), "")
    assert "worker-writable" in reason


def test_replacing_the_pinned_key_file_cannot_move_the_authorization(tmp_path):
    """Authorization keeps the pinned location even after the file is swapped."""
    import json
    import os

    key = SigningKey.generate("server")
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    key_file = workspace / "keys.json"
    key_file.write_text(
        json.dumps(
            {
                "format": 1,
                "keys": [{"kid": key.kid, "public_key": key.public_b64()}],
            }
        ),
        encoding="utf-8",
    )
    os.chmod(key_file, 0o600)
    outside = tmp_path / "outside"
    outside.mkdir()
    trust = TrustedKeys.load(key_file)  # pins the workspace location
    key_file.unlink()
    key_file.symlink_to(outside / "keys.json")
    from src.conductor_cli.daemon_grant import NonceStore

    origin = OriginCheck(trust, NonceStore(outside / "nonces.json"))
    reason = daemon._origin_location_rejection(origin, str(workspace), "")

    assert "worker-writable" in reason
