"""N58 for N44: the grant verifier rolls out as off | shadow | enforce, default shadow.

Only ``enforce`` refuses a policy-backed Ticket on a missing, rejected or
unverifiable grant. ``shadow`` logs the refusal it did not make and runs the
Ticket under the local policy allowlist; ``off`` skips the signature check.
The policy itself (allowlist, deny list, version binding) holds in every mode,
and ``cond doctor`` FAILs on an unbuildable verifier only when enforcing and
never because the policy is absent.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from src.conductor_cli.daemon_capability import (
    GRANT_VERIFIER_MODE_ENV,
    grant_verifier_mode,
)
from src.conductor_cli.doctor import _grant_verifier_check, doctor_report

from .test_conductor_local_daemon_capability import (
    RejectingGrantVerifier,
    _api,
    _run,
    _store,
    _ticket,
    local_isolation_policy,  # noqa: F401 - autouse fixture: runs reach the CLI
)


class _CountingRejecter(RejectingGrantVerifier):
    def __init__(self) -> None:
        self.calls = 0

    def verify(self, grant):
        self.calls += 1
        return super().verify(grant)


@pytest.fixture(autouse=True)
def _clean_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(GRANT_VERIFIER_MODE_ENV, raising=False)


def _set(monkeypatch: pytest.MonkeyPatch, mode: str | None) -> None:
    if mode is not None:
        monkeypatch.setenv(GRANT_VERIFIER_MODE_ENV, mode)


def test_the_mode_defaults_to_shadow() -> None:
    assert grant_verifier_mode({}) == "shadow"
    assert grant_verifier_mode({GRANT_VERIFIER_MODE_ENV: " Enforce "}) == "enforce"
    assert grant_verifier_mode({GRANT_VERIFIER_MODE_ENV: "nope"}) == "shadow"
    assert grant_verifier_mode({GRANT_VERIFIER_MODE_ENV: "off"}) == "off"


@pytest.mark.asyncio
async def test_enforce_refuses_a_rejected_grant(tmp_path, monkeypatch) -> None:
    _set(monkeypatch, "enforce")
    store, version = _store(tmp_path)
    api = _api(_ticket(version))

    get_adapter = await _run(api, store, verifier=RejectingGrantVerifier())

    get_adapter.assert_not_called()
    assert api.report.call_args.args[-1]["state"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, "shadow"])
@pytest.mark.parametrize("verifier", [RejectingGrantVerifier(), None])
async def test_shadow_logs_the_refusal_and_runs(
    tmp_path, monkeypatch, caplog, mode, verifier
) -> None:
    _set(monkeypatch, mode)
    store, version = _store(tmp_path)
    api = _api(_ticket(version))

    with caplog.at_level(logging.WARNING):
        get_adapter = await _run(api, store, verifier=verifier)

    get_adapter.assert_called_once()
    assert api.report.call_args.args[-1]["state"] == "ready"
    assert "would refuse" in caplog.text


@pytest.mark.asyncio
async def test_off_skips_the_verifier(tmp_path, monkeypatch, caplog) -> None:
    _set(monkeypatch, "off")
    store, version = _store(tmp_path)
    api = _api(_ticket(version))
    verifier = _CountingRejecter()

    with caplog.at_level(logging.WARNING):
        get_adapter = await _run(api, store, verifier=verifier)

    get_adapter.assert_called_once()
    assert verifier.calls == 0
    assert "would refuse" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["shadow", "off"])
async def test_the_policy_deny_list_holds_without_the_verifier(
    tmp_path, monkeypatch, mode
) -> None:
    _set(monkeypatch, mode)
    store, version = _store(tmp_path)
    api = _api(_ticket(version, scope="run git push"))

    get_adapter = await _run(api, store, verifier=RejectingGrantVerifier())

    get_adapter.assert_not_called()
    assert api.report.call_args.args[-1]["state"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["shadow", "off"])
async def test_the_policy_version_bind_holds_without_the_verifier(
    tmp_path, monkeypatch, mode
) -> None:
    _set(monkeypatch, mode)
    store, version = _store(tmp_path)
    api = _api(_ticket(version, policy_version="stale"))

    get_adapter = await _run(api, store, verifier=None)

    get_adapter.assert_not_called()


# --------------------------------------------------------------------------
# cond doctor
# --------------------------------------------------------------------------


def _policy_without_keys(tmp_path: Path):
    from types import SimpleNamespace

    policy = tmp_path / "daemon_policy.yaml"
    policy.write_text("format: 1\n", encoding="utf-8")
    os.chmod(policy, 0o600)
    return SimpleNamespace(daemon_policy=policy, trusted_keys=tmp_path / "keys.json")


@pytest.mark.parametrize("mode,ok", [("enforce", False), ("shadow", True), (None, True), ("off", True)])
def test_doctor_fails_an_unbuildable_verifier_only_when_enforcing(
    tmp_path, monkeypatch, mode, ok
) -> None:
    _set(monkeypatch, mode)
    check = _grant_verifier_check(_policy_without_keys(tmp_path))
    assert check["ok"] is ok
    assert "no grant verifier can be built" in str(check["detail"])
    assert check["mode"] == (mode or "shadow")


@pytest.mark.parametrize("mode", ["enforce", "shadow"])
def test_doctor_does_not_fail_on_an_absent_policy(tmp_path, monkeypatch, mode) -> None:
    from tests.unit.test_cond_ops import FakeSystem, _layout

    _set(monkeypatch, mode)
    home = _layout(tmp_path, monkeypatch, web=5170)
    monkeypatch.setenv("CONDUCTOR_WEB_PORT", "5170")
    from src.conductor_cli.ops_support import load_settings

    settings = load_settings()
    if settings.daemon_policy.exists():
        settings.daemon_policy.unlink()
    assert str(settings.daemon_policy).startswith(str(home))
    fake = FakeSystem()
    fake.open_ports.add(5170)

    report = doctor_report(fake, settings)
    by_name = {
        item["name"]: item
        for item in report["checks"]
        if isinstance(item, dict) and "name" in item
    }
    assert by_name["daemon_policy"]["ok"] is True
    assert by_name["daemon_policy"]["detail"] == "absent (optional)"
    assert by_name["grant_verifier"]["ok"] is True
