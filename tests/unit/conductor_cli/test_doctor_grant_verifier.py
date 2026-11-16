"""N44: cond doctor FAILs when a daemon policy is present without a verifier."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.conductor_cli.daemon_grant import SigningKey
from src.conductor_cli.doctor import (
    _grant_verifier_check,
    doctor_report,
    format_doctor,
)


def _settings(tmp_path: Path, *, policy: bool, keys: str | None) -> SimpleNamespace:
    policy_path = tmp_path / "daemon_policy.yaml"
    keys_path = tmp_path / "daemon_trusted_keys.json"
    if policy:
        policy_path.write_text("format: 1\n", encoding="utf-8")
        os.chmod(policy_path, 0o600)
    if keys == "valid":
        key = SigningKey.generate("server")
        keys_path.write_text(
            json.dumps(
                {
                    "format": 1,
                    "keys": [{"kid": key.kid, "public_key": key.public_b64()}],
                }
            ),
            encoding="utf-8",
        )
        os.chmod(keys_path, 0o600)
    elif keys == "malformed":
        keys_path.write_text("{not-json", encoding="utf-8")
        os.chmod(keys_path, 0o600)
    # keys is None → no trusted-keys file
    return SimpleNamespace(daemon_policy=policy_path, trusted_keys=keys_path)


@pytest.fixture(autouse=True)
def _enforce_grant_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests pin N44's enforce behaviour; the modes have their own tests (N58)."""
    monkeypatch.setenv("CONDUCTOR_DAEMON_GRANT_VERIFIER_MODE", "enforce")


def test_doctor_is_ok_when_there_is_no_daemon_policy(tmp_path):
    check = _grant_verifier_check(_settings(tmp_path, policy=False, keys=None))

    assert check["ok"] is True
    assert check["name"] == "grant_verifier"
    assert check["detail"] == "no daemon policy"


def test_doctor_fails_when_policy_is_present_without_trusted_keys(tmp_path):
    check = _grant_verifier_check(_settings(tmp_path, policy=True, keys=None))

    assert check["ok"] is False
    assert "no grant verifier can be built" in str(check["detail"])


def test_doctor_fails_when_policy_is_present_and_trusted_keys_are_unusable(tmp_path):
    check = _grant_verifier_check(_settings(tmp_path, policy=True, keys="malformed"))

    assert check["ok"] is False
    assert "no grant verifier can be built" in str(check["detail"])


def test_doctor_fails_when_trusted_keys_are_not_utf8(tmp_path):
    """Non-UTF-8 key bytes must FAIL, not raise through doctor."""
    from unittest.mock import patch

    settings = _settings(tmp_path, policy=True, keys="valid")

    def _bad_bytes(*_args, **_kwargs):
        return bytes([255]).decode("utf-8")

    with patch("src.conductor_cli.daemon_grant.read_owned_text", _bad_bytes):
        check = _grant_verifier_check(settings)

    assert check["ok"] is False
    assert "no grant verifier can be built" in str(check["detail"])


def test_doctor_passes_when_policy_and_trusted_keys_build_a_verifier(tmp_path):
    check = _grant_verifier_check(_settings(tmp_path, policy=True, keys="valid"))

    assert check["ok"] is True
    assert check["detail"] == "ok"


def test_doctor_report_wires_grant_verifier_failure(tmp_path, monkeypatch):
    """doctor_report must include grant_verifier against a real Settings."""
    from tests.unit.test_cond_ops import FakeSystem, _layout

    home = _layout(tmp_path, monkeypatch, web=5170)
    monkeypatch.setenv("CONDUCTOR_WEB_PORT", "5170")
    daemon_policy = home / ".config" / "conductor" / "daemon_policy.yaml"
    daemon_policy.parent.mkdir(parents=True, exist_ok=True)
    daemon_policy.write_text("format: 1\n", encoding="utf-8")
    os.chmod(daemon_policy, 0o600)
    # Trusted keys deliberately absent: policy present → grant_verifier FAIL.
    trusted_keys = home / ".config" / "conductor" / "daemon_trusted_keys.json"
    if trusted_keys.exists():
        trusted_keys.unlink()

    from src.conductor_cli.ops_support import load_settings

    settings = load_settings()
    assert settings.daemon_policy == daemon_policy
    assert settings.trusted_keys == trusted_keys

    fake = FakeSystem()
    fake.open_ports.add(5170)
    report = doctor_report(fake, settings)
    by_name = {
        item["name"]: item
        for item in report["checks"]
        if isinstance(item, dict) and "name" in item
    }

    assert "grant_verifier" in by_name
    assert by_name["grant_verifier"]["ok"] is False
    assert "no grant verifier can be built" in str(by_name["grant_verifier"]["detail"])
    assert report["ok"] is False
    text = format_doctor(report)
    assert "FAIL: grant_verifier" in text
    assert "doctor: failed" in text
