"""The host bridge is the privacy boundary between CodexBar and Conductor."""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

from src.conductor_cli import usage

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "codexbar_bridge", ROOT / "scripts" / "codexbar_bridge.py"
)
assert SPEC and SPEC.loader
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


def test_cli_snapshot_drops_identity_and_scrubs_errors():
    payload = [
        {
            "provider": "zai",
            "source": "web",
            "usage": {
                "accountEmail": "person@example.com",
                "identity": {"accountID": "user_01ABCDEFGH"},
                "loginMethod": "max",
                "updatedAt": "2026-10-27T00:00:00Z",
                "primary": {"usedPercent": 7, "resetsAt": "2026-10-27T11:15:04Z"},
            },
        },
        {
            "provider": "claude",
            "error": {"message": "Sign in as person@example.com"},
        },
    ]

    result = bridge.sanitize(payload)

    assert [provider["provider"] for provider in result["providers"]] == ["claude", "zai"]
    assert result["providers"][1]["login_method"] == "max"
    assert result["providers"][1]["windows"][0]["usedPercent"] == 7
    assert result["errors"][0]["message"] == "Sign in as <email>"
    assert "@" not in json.dumps(result)


def test_unreadable_provider_is_reported_unknown_and_keeps_its_reason():
    payload = [
        {
            "provider": "zai",
            "source": "api",
            "usage": {
                "loginMethod": "max",
                "updatedAt": "2026-10-27T00:00:00Z",
                "primary": {"usedPercent": 7},
            },
        },
        {
            "provider": "claude",
            "source": "api",
            "error": {
                "kind": "provider",
                "message": "No available fetch strategy for claude.",
                "code": 1,
            },
        },
    ]

    result = bridge.sanitize(payload)

    rows = {row["provider"]: row for row in result["providers"]}
    assert rows["zai"]["status"] == "ok"
    assert rows["claude"] == {
        "provider": "claude",
        "source": "api",
        "status": "unknown",
        "updated_at": None,
        "windows": [],
        "error": "No available fetch strategy for claude.",
    }
    # The unreadable provider stays listed as not connected for the UI count.
    assert result["errors"] == [
        {"provider": "claude", "message": "No available fetch strategy for claude."}
    ]


def test_bridge_snapshot_keeps_quota_and_reports_unreadable_provider_unknown(tmp_path):
    """A provider CodexBar could not read must not cost the other providers
    their quota and balance state."""
    now = datetime(2026, 11, 3, 8, 0, tzinfo=timezone.utc)
    payload = bridge.sanitize(
        [
            {
                "provider": "zai",
                "source": "api",
                "usage": {
                    "loginMethod": "max",
                    "updatedAt": "2026-11-03T07:59:00Z",
                    "primary": {"usedPercent": 33, "resetsAt": "2026-11-03T12:00:00Z"},
                },
            },
            {
                "provider": "claude",
                "source": "api",
                "error": {"kind": "provider", "message": "No available fetch strategy for claude."},
            },
        ]
    )
    path = tmp_path / "codexbar.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    providers = {
        row["provider"]: row for row in usage.load_headroom(path, now=now)["providers"]
    }

    assert providers["zai"] == {"provider": "zai", "status": "ok", "remaining_percent": 67}
    assert providers["claude"] == {"provider": "claude", "status": "unknown"}
    assert providers["deepseek"] == {
        "provider": "deepseek",
        "kind": "balance",
        "status": "unknown",
    }


def test_widget_snapshot_uses_usage_row_labels(tmp_path):
    path = tmp_path / "widget-snapshot.json"
    path.write_text(
        json.dumps(
            {
                "generatedAt": "2026-10-27T04:00:00Z",
                "entries": [
                    {
                        "provider": "kimi",
                        "updatedAt": "2026-10-27T04:00:00Z",
                        "usageRows": [
                            {"id": "primary", "title": "7-day usage", "percentLeft": 100},
                            {"id": "secondary", "title": "5-hour usage", "percentLeft": 92},
                        ],
                        "primary": {"usedPercent": 0, "windowMinutes": 10080},
                        "secondary": {"usedPercent": 8, "windowMinutes": 300},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = bridge.sanitize_widget_snapshot(json.loads(path.read_text()), path)

    assert result["source"] == "widget-snapshot"
    provider = result["providers"][0]
    assert provider["provider"] == "kimi"
    assert [window["label"] for window in provider["windows"]] == [
        "7-day usage",
        "5-hour usage",
    ]
    assert provider["windows"][1]["usedPercent"] == 8
    assert provider["status"] == "ok"


def test_widget_snapshot_marks_a_provider_without_windows_unknown(tmp_path):
    path = tmp_path / "widget-snapshot.json"
    path.write_text(
        json.dumps(
            {
                "generatedAt": "2026-10-27T04:00:00Z",
                "entries": [
                    {
                        "provider": "claude",
                        "updatedAt": "2026-10-27T04:00:00Z",
                        "usageRows": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = bridge.sanitize_widget_snapshot(json.loads(path.read_text()), path)

    assert result["providers"][0]["status"] == "unknown"
    assert result["providers"][0]["windows"] == []


def test_cli_payload_survives_a_non_zero_exit_code(tmp_path):
    """CodexBar exits 1 as soon as one provider fails, but the payload of the
    providers it could read is still valid."""
    fake = tmp_path / "codexbar"
    fake.write_text(
        "#!/bin/sh\n"
        "echo '[{\"provider\":\"zai\",\"source\":\"api\",\"usage\":{\"loginMethod\":\"max\","
        "\"updatedAt\":\"2026-11-03T07:59:00Z\",\"primary\":{\"usedPercent\":33}}},"
        "{\"provider\":\"claude\",\"error\":{\"message\":\"No available fetch strategy for claude.\"}}]'\n"
        "exit 1\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)

    result = bridge.run_codexbar(str(fake), "all")

    assert "error" not in result
    rows = {row["provider"]: row for row in result["providers"]}
    assert rows["zai"]["status"] == "ok"
    assert rows["claude"]["status"] == "unknown"


def test_unrunnable_cli_reports_an_error_instead_of_raising(tmp_path):
    # A directory cannot be executed, and that holds for a privileged user too.
    not_runnable = tmp_path / "codexbar"
    not_runnable.mkdir()

    result = bridge.run_codexbar(str(not_runnable), "all")

    assert result["providers"] == []
    assert "not runnable" in result["error"]
