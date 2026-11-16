"""`cond doctor` tells the operator when CodexBar changed under the parsing rules."""

from __future__ import annotations

import json
from types import SimpleNamespace

from src.conductor_cli.codexbar_snapshot import VALIDATED_CODEXBAR_VERSION
from src.conductor_cli.doctor import _codexbar_rules_check


def _settings(tmp_path, payload):
    usage = tmp_path / "usage"
    usage.mkdir(parents=True, exist_ok=True)
    if payload is not None:
        (usage / "codexbar-snapshot.json").write_text(json.dumps(payload), encoding="utf-8")
    return SimpleNamespace(conductor_home=tmp_path)


def test_matching_version_without_drift_is_ok(tmp_path):
    check = _codexbar_rules_check(
        _settings(tmp_path, {"codexbar_version": VALIDATED_CODEXBAR_VERSION, "providers": []})
    )
    assert check["ok"] is True


def test_a_new_codexbar_release_is_reported(tmp_path):
    check = _codexbar_rules_check(_settings(tmp_path, {"codexbar_version": "0.99.0", "providers": []}))
    assert check["ok"] is False
    assert "0.99.0" in check["detail"] and VALIDATED_CODEXBAR_VERSION in check["detail"]


def test_unknown_output_keys_are_reported_by_provider(tmp_path):
    payload = {
        "codexbar_version": VALIDATED_CODEXBAR_VERSION,
        "providers": [
            {"provider": "codex", "schema_drift": ["usage.newField"]},
            {"provider": "cursor"},
        ],
    }
    check = _codexbar_rules_check(_settings(tmp_path, payload))
    assert check["ok"] is False
    assert check["drift"] == {"codex": ["usage.newField"]}


def test_a_snapshot_from_the_old_writer_is_reported(tmp_path):
    check = _codexbar_rules_check(_settings(tmp_path, {"providers": []}))
    assert check["ok"] is False and "version unknown" in check["detail"]


def test_a_missing_snapshot_is_reported(tmp_path):
    check = _codexbar_rules_check(_settings(tmp_path, None))
    assert check["ok"] is False and check["detail"] == "snapshot missing"
