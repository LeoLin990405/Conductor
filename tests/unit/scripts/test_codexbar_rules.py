"""CodexBar parsing rules against real (sanitized) CodexBar output.

The fixtures under tests/fixtures/codexbar/<version>/ are captured with
scripts/refresh_codexbar_fixtures.py. When CodexBar updates upstream, refresh
them: test_fixtures_only_use_known_keys then names every new output key, and
the rules plus VALIDATED_CODEXBAR_VERSION are updated (docs/CODEXBAR.md).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.conductor_cli import codexbar_snapshot as snap
from src.server.services.native_runtime.usage_headroom import (
    _window_state,
    read_usage_headroom,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "codexbar"
VALIDATED = FIXTURES / snap.VALIDATED_CODEXBAR_VERSION


def _load(provider: str) -> list[dict]:
    return json.loads((VALIDATED / f"{provider}.json").read_text(encoding="utf-8"))


def _fixture_time(rows: list[dict]) -> datetime:
    stamp = rows[0]["usage"]["updatedAt"]
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _state(provider: str, tmp_path: Path) -> str | None:
    rows = _load(provider)
    row = snap._provider_row(rows, provider)
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps({"providers": [row]}), encoding="utf-8")
    now = _fixture_time(rows) + timedelta(minutes=1)
    return read_usage_headroom(str(path), now=now).get(provider)


def test_validated_fixtures_exist():
    for provider in ("codex", "cursor", "stepfun"):
        assert (VALIDATED / f"{provider}.json").is_file(), provider


def test_fixtures_only_use_known_keys():
    # The upstream-sync tripwire: a refreshed fixture with a new key fails here.
    for provider in ("codex", "cursor", "stepfun"):
        for result in _load(provider):
            assert snap._schema_drift(result) == [], provider


def test_codex_full_weekly_window_with_credits_is_low_not_exhausted(tmp_path):
    rows = _load("codex")
    row = snap._provider_row(rows, "codex")
    assert row["credits_remaining"] > 0
    assert {w["role"] for w in row["windows"]} == {"secondary"}
    assert _state("codex", tmp_path) == "low"


def test_cursor_full_third_party_bucket_is_low_not_exhausted(tmp_path):
    row = snap._provider_row(_load("cursor"), "cursor")
    roles = [w["role"] for w in row["windows"]]
    assert roles[:3] == ["primary", "secondary", "tertiary"] and "extra" in roles
    assert _state("cursor", tmp_path) == "low"


def test_stepfun_with_room_is_available(tmp_path):
    assert _state("stepfun", tmp_path) == "available"


NOW = datetime(2026, 11, 10, 7, 0, tzinfo=timezone.utc)
LATER = "2026-11-20T00:00:00Z"


@pytest.mark.parametrize(
    "windows, credits, expected",
    [
        ([{"used_percent": 100, "role": "secondary", "resets_at_utc": LATER}], None, "exhausted"),
        ([{"used_percent": 100, "role": "secondary", "resets_at_utc": LATER}], 0, "exhausted"),
        ([{"used_percent": 100, "role": "secondary", "resets_at_utc": LATER}], 25.5, "low"),
        ([{"used_percent": 100, "role": "primary", "resets_at_utc": LATER}], None, "exhausted"),
        ([{"used_percent": 100, "role": "tertiary", "resets_at_utc": LATER}], None, "low"),
        ([{"used_percent": 100, "role": "extra", "resets_at_utc": LATER}], None, "low"),
        ([{"used_percent": 60, "role": "primary", "resets_at_utc": LATER}], None, "available"),
        # An older snapshot without roles keeps the original rule.
        ([{"used_percent": 100, "resets_at_utc": LATER}], None, "exhausted"),
        # A window that has already reset does not count.
        ([{"used_percent": 100, "role": "primary", "resets_at_utc": "2026-11-01T00:00:00Z"}], None, None),
    ],
)
def test_window_state_rules(windows, credits, expected):
    assert _window_state(windows, now=NOW, credits_remaining=credits) == expected


@pytest.mark.parametrize(
    "block, expected",
    [
        ({"creditsAvailable": True, "balanceReadSucceeded": True, "remaining": 12.5}, 12.5),
        ({"creditsAvailable": False, "remaining": 12.5}, None),
        ({"creditsAvailable": True, "balanceReadSucceeded": False, "remaining": 12.5}, None),
        ({"creditsAvailable": True, "remaining": -1}, None),
        ({"creditsAvailable": True, "remaining": "12"}, None),
        ("not a block", None),
    ],
)
def test_credits_remaining_fails_closed(block, expected):
    assert snap._credits_remaining({"credits": block}) == expected


def test_schema_drift_names_new_keys_only():
    result = {
        "provider": "codex",
        "brandNew": 1,
        "usage": {"updatedAt": "x", "freshField": 2, "primary": {"usedPercent": 1, "newWindowKey": 3}},
        "credits": {"remaining": 1, "surprise": True},
    }
    assert snap._schema_drift(result) == [
        "credits.surprise",
        "result.brandNew",
        "usage.freshField",
        "window.newWindowKey",
    ]


def test_codexbar_version_parsing(monkeypatch):
    class Done:
        def __init__(self, out, code=0):
            self.stdout, self.returncode = out, code

    monkeypatch.setattr(snap.subprocess, "run", lambda *a, **k: Done("CodexBar 0.84.0\n"))
    assert snap._codexbar_version("codexbar", 5) == "0.84.0"
    monkeypatch.setattr(snap.subprocess, "run", lambda *a, **k: Done("something else"))
    assert snap._codexbar_version("codexbar", 5) is None
    monkeypatch.setattr(snap.subprocess, "run", lambda *a, **k: Done("CodexBar 1.0; rm -rf"))
    assert snap._codexbar_version("codexbar", 5) is None


def test_collect_snapshot_records_versions(monkeypatch):
    monkeypatch.setattr(snap, "_codexbar_version", lambda codexbar, timeout: "9.9.9")
    monkeypatch.setattr(snap, "PROVIDERS", ())
    payload = snap.collect_snapshot(codexbar="codexbar", timeout=5)
    assert payload["codexbar_version"] == "9.9.9"
    assert payload["validated_codexbar_version"] == snap.VALIDATED_CODEXBAR_VERSION


def test_tiny_or_unconfirmed_credits_do_not_count():
    assert snap._credits_remaining({"credits": {"creditsAvailable": True, "balanceReadSucceeded": True, "remaining": 0.5}}) is None
    assert snap._credits_remaining({"credits": {"creditsAvailable": True, "remaining": 50}}) is None


def test_prerelease_versions_are_read(monkeypatch):
    class Done:
        stdout, returncode = "CodexBar 0.85.0-beta.1\n", 0

    monkeypatch.setattr(snap.subprocess, "run", lambda *a, **k: Done())
    assert snap._codexbar_version("codexbar", 5) == "0.85.0-beta.1"


def test_credits_quota_window_keys_are_known():
    result = {"provider": "manus", "usage": {"updatedAt": "x", "primary": {"usedPercent": 1, "quotaLimit": 5, "quotaUnit": "credits"}}}
    assert snap._schema_drift(result) == []


def test_fixture_sanitizer_drops_any_identity_key():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "refresh_codexbar_fixtures",
        Path(__file__).resolve().parents[3] / "scripts" / "refresh_codexbar_fixtures.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    raw = {"accountId": "a1", "usage": {"userId": "u", "workspaceName": "w", "providerCost": {"used": 7, "limit": 9}}, "x": [{"loginHint": "z"}]}
    clean = module.sanitize(raw)
    text = json.dumps(clean)
    assert "a1" not in text and '"u"' not in text and '"w"' not in text and '"z"' not in text
    assert clean["usage"]["providerCost"] == {"used": 1000.0, "limit": 1000.0}
