from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.conductor_cli import usage
from src.conductor_cli import cli
from src.conductor_cli.codexbar_snapshot import PROVIDERS
from src.utils.provider_usage import provider_kind


def _expected_unknown(provider: str) -> dict:
    kind = provider_kind(provider)
    return {
        "provider": provider,
        **({"kind": kind} if kind != "quota" else {}),
        "status": "unknown",
    }


def _worktree_temp_dir() -> Path:
    return Path(
        tempfile.mkdtemp(prefix=".conductor-usage-test-", dir=Path(__file__).parent)
    )


def _stamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def test_headroom_uses_lowest_remaining_active_window_and_ignores_expired() -> None:
    now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    row = {
        "provider": "codex",
        "status": "ok",
        "updated_at": _stamp(now - timedelta(minutes=2)),
        "windows": [
            {"used_percent": 45, "resets_at_utc": _stamp(now + timedelta(hours=1))},
            {"used_percent": 80, "resets_at_utc": _stamp(now + timedelta(days=1))},
            {"used_percent": 99, "resets_at_utc": _stamp(now - timedelta(seconds=1))},
        ],
    }

    result = usage._provider_headroom(row, now)

    assert result == {"provider": "codex", "status": "ok", "remaining_percent": 20}


def test_balance_provider_never_projects_quota_windows_to_percent() -> None:
    now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    row = {
        "provider": "deepseek-dsh",
        "status": "ok",
        "updated_at": _stamp(now),
        "windows": [{"used_percent": 0}],
    }

    assert usage._provider_headroom(row, now) == {
        "provider": "deepseek-dsh",
        "kind": "balance",
        "status": "unknown",
    }


def test_headroom_marks_stale_missing_error_money_and_expired_rows_unknown() -> None:
    now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    good_time = _stamp(now - timedelta(minutes=1))
    rows = [
        {
            "provider": "codex",
            "status": "ok",
            "updated_at": _stamp(now - timedelta(minutes=16)),
            "windows": [{"used_percent": 1}],
        },
        {"provider": "claude", "status": "ok", "windows": [{"used_percent": 1}]},
        {
            "provider": "grok",
            "status": "error",
            "updated_at": good_time,
            "windows": [{"used_percent": 1}],
        },
        {
            "provider": "kimi",
            "status": "ok",
            "updated_at": good_time,
            "windows": [{"balance": 12.5}],
        },
        {
            "provider": "doubao",
            "status": "ok",
            "updated_at": good_time,
            "windows": [
                {
                    "used_percent": 100,
                    "resets_at_utc": _stamp(now - timedelta(seconds=1)),
                }
            ],
        },
        {
            "provider": "minimax",
            "status": "ok",
            "updated_at": good_time,
            "windows": [{"used_percent": True}],
        },
    ]
    assert [usage._provider_headroom(row, now) for row in rows] == [
        {"provider": provider, "status": "unknown"}
        for provider in ("codex", "claude", "grok", "kimi", "doubao", "minimax")
    ]


def test_parser_rejects_extreme_numbers_timestamps_and_oversized_windows() -> None:
    now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    good_time = _stamp(now - timedelta(minutes=1))
    assert usage._number(10**1000) is None
    assert usage._timestamp("0001-01-01T00:00:00+01:00") is None
    row = {
        "provider": "codex",
        "status": "ok",
        "updated_at": good_time,
        "windows": [{"used_percent": 1}] * 41,
    }
    assert usage._provider_headroom(row, now) == {
        "provider": "codex",
        "status": "unknown",
    }


def test_snapshot_reader_rejects_symlink_fifo_and_oversized_file() -> None:
    directory = _worktree_temp_dir()
    try:
        target = directory / "target.json"
        target.write_text(json.dumps({"providers": []}), encoding="utf-8")
        link = directory / "link.json"
        link.symlink_to(target)
        fifo = directory / "fifo.json"
        os.mkfifo(fifo)
        large = directory / "large.json"
        large.write_bytes(b" " * (usage._MAX_SNAPSHOT_BYTES + 1))
        expected = [_expected_unknown(provider) for provider in PROVIDERS]

        assert usage.load_headroom(link)["providers"] == expected
        assert usage.load_headroom(fifo)["providers"] == expected
        assert usage.load_headroom(large)["providers"] == expected
    finally:
        shutil.rmtree(directory)


def test_usage_command_bypasses_api_client_and_emits_only_reduced_fields(
    capsys, monkeypatch
) -> None:
    directory = _worktree_temp_dir()
    snapshot = directory / "snapshot.json"
    now = datetime.now(timezone.utc)
    rows = [
        {
            "provider": "codex",
            "status": "ok",
            "updated_at": _stamp(now),
            "windows": [{"used_percent": 25}],
            "identity": "must not escape",
        },
        {"provider": "claude", "status": "error", "windows": []},
    ]
    snapshot.write_text(
        json.dumps({"providers": rows, "raw": "must not escape"}), encoding="utf-8"
    )
    monkeypatch.setattr(
        cli,
        "APIClient",
        lambda **_: (_ for _ in ()).throw(AssertionError("APIClient created")),
    )
    monkeypatch.setattr(
        "src.conductor_cli.codexbar_snapshot.refresh_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("snapshot refreshed")),
    )
    try:
        exit_code = cli.main(
            ["usage", "--no-refresh", "--snapshot", str(snapshot), "--json"]
        )
        stdout = capsys.readouterr().out
        envelope = json.loads(stdout)
        output = envelope["data"]
        assert exit_code == 0
        assert envelope["api_version"] == "v1"
        assert envelope["command"] == "usage"
        assert envelope["errors"] is None
        assert output["providers"][0] == {
            "provider": "codex",
            "status": "ok",
            "remaining_percent": 75,
        }
        assert output["providers"][1] == {"provider": "claude", "status": "unknown"}
        assert "identity" not in stdout
        assert "raw" not in json.dumps(output)
    finally:
        shutil.rmtree(directory)


def test_usage_refresh_passes_explicit_snapshot_and_codexbar_without_api_client(
    monkeypatch,
) -> None:
    directory = _worktree_temp_dir()
    path = directory / "snapshot.json"
    calls: list[tuple[Path, str, float]] = []

    def fake_refresh(snapshot_path, *, codexbar, timeout):
        calls.append((snapshot_path, codexbar, timeout))
        snapshot_path.write_text(json.dumps({"providers": []}), encoding="utf-8")
        return {"providers": []}

    monkeypatch.setattr(
        "src.conductor_cli.codexbar_snapshot.refresh_snapshot", fake_refresh
    )
    try:
        result = usage.usage_result(path=path, codexbar="/fake/codexbar", timeout=12.0)
        assert calls == [(path, "/fake/codexbar", 12.0)]
        assert result["providers"] == [_expected_unknown(provider) for provider in PROVIDERS]
    finally:
        shutil.rmtree(directory)


def test_no_refresh_renders_balance_status_and_hides_amount_by_default(capsys, monkeypatch):
    directory = _worktree_temp_dir()
    snapshot = directory / "snapshot.json"
    now = datetime.now(timezone.utc)
    snapshot.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "provider": "deepseek-dsh",
                        "status": "ok",
                        "updated_at": _stamp(now),
                        "balance": {"amount": 75, "currency": "CNY"},
                        "identity": "private",
                    },
                    {
                        "provider": "codex",
                        "status": "ok",
                        "updated_at": _stamp(now),
                        "windows": [{"used_percent": 20}],
                    },
                    {
                        "provider": "manus",
                        "kind": "balance",
                        "status": "ok",
                        "updated_at": _stamp(now),
                        "balance": {"amount": 140, "currency": "CREDITS"},
                    },
                    {
                        "provider": "private\nidentity",
                        "kind": "balance",
                        "status": "ok",
                        "updated_at": _stamp(now),
                        "balance": {"amount": 999, "currency": "CNY"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli,
        "APIClient",
        lambda **_: (_ for _ in ()).throw(AssertionError("APIClient created")),
    )
    monkeypatch.setattr(
        "src.conductor_cli.config_files.find_config_file", lambda _: None
    )
    try:
        code = cli.main(["usage", "--no-refresh", "--snapshot", str(snapshot), "--json"])
        rendered = capsys.readouterr().out
        result = json.loads(rendered)["data"]
        deepseek = next(row for row in result["providers"] if row["provider"] == "deepseek")
        manus = next(row for row in result["providers"] if row["provider"] == "manus")
        assert code == 0
        assert deepseek == {"provider": "deepseek", "kind": "balance", "status": "available"}
        assert manus == {"provider": "manus", "kind": "balance", "status": "available"}
        assert "75" not in rendered
        assert "private" not in rendered
        assert "identity" not in rendered
        assert "999" not in rendered
        assert next(row for row in result["providers"] if row["provider"] == "codex")["remaining_percent"] == 80

        code = cli.main([
            "usage", "--no-refresh", "--snapshot", str(snapshot), "--show-balance", "--json"
        ])
        result = json.loads(capsys.readouterr().out)["data"]
        deepseek = next(row for row in result["providers"] if row["provider"] == "deepseek")
        assert code == 0
        assert deepseek["balance"] == {"amount": 75, "currency": "CNY"}

        cli.main(["usage", "--no-refresh", "--snapshot", str(snapshot), "--show-balance"])
        text = capsys.readouterr().out
        assert "deepseek: balance ok (75 CNY)" in text
        assert "codex: 80% remaining" in text
    finally:
        shutil.rmtree(directory)


def test_balance_unknown_for_stale_malformed_and_unconfigured_currency(tmp_path, monkeypatch, capsys):
    now = datetime.now(timezone.utc)
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "providers": [
                    {"provider": "deepseek-dsh", "status": "ok", "updated_at": _stamp(now), "balance": {"amount": 10, "currency": "CNY"}},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("src.conductor_cli.config_files.find_config_file", lambda _: None)
    result = usage.usage_result(path=snapshot, no_refresh=True)
    assert next(row for row in result["providers"] if row["provider"] == "deepseek")["status"] == "low"

    for balance in (
        {"amount": True, "currency": "CNY"},
        {"amount": 10, "currency": "USD"},
    ):
        snapshot.write_text(
            json.dumps({"providers": [{"provider": "deepseek", "status": "ok", "updated_at": _stamp(now), "balance": balance}]}),
            encoding="utf-8",
        )
        result = usage.usage_result(path=snapshot, no_refresh=True)
        assert next(row for row in result["providers"] if row["provider"] == "deepseek")["status"] == "unknown"

    snapshot.write_text(
        json.dumps({"providers": [{"provider": "deepseek", "status": "ok", "updated_at": _stamp(now - timedelta(hours=1)), "balance": {"amount": 10, "currency": "CNY"}}]}),
        encoding="utf-8",
    )
    result = usage.usage_result(path=snapshot, no_refresh=True)
    deepseek = next(row for row in result["providers"] if row["provider"] == "deepseek")
    assert deepseek["status"] == "unknown"
    usage.render_usage({"providers": [deepseek]}, json_mode=False)
    assert capsys.readouterr().out == "deepseek: balance unknown\n"


def test_balance_thresholds_use_operator_config(monkeypatch, tmp_path):
    now = datetime.now(timezone.utc)
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "provider": "deepseek-dsh",
                        "status": "ok",
                        "updated_at": _stamp(now),
                        "balance": {"amount": 60, "currency": "CNY"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        usage,
        "_configured_balance_thresholds",
        lambda: {"deepseek": {"CNY": 75}},
    )
    result = usage.usage_result(path=snapshot, no_refresh=True)
    deepseek = next(row for row in result["providers"] if row["provider"] == "deepseek")
    assert deepseek == {"provider": "deepseek", "kind": "balance", "status": "low"}


def test_threshold_config_canonicalizes_deepseek_alias_and_currency(monkeypatch, tmp_path):
    from src.utils.provider_usage import normalize_low_balance_thresholds

    config_path = tmp_path / "config.yaml"
    raw_config = {
        "native_runtime": {
            "usage_headroom": {"low_balance_thresholds": {"deepseek-dsh": {"cny": 75}}}
        }
    }
    monkeypatch.setattr(
        "src.conductor_cli.config_files.find_config_file", lambda name: config_path
    )
    monkeypatch.setattr(
        "src.conductor_cli.config_files.load_yaml_config", lambda _path: raw_config
    )

    configured = usage._configured_balance_thresholds()
    backend_normalized = normalize_low_balance_thresholds(
        raw_config["native_runtime"]["usage_headroom"]["low_balance_thresholds"]
    )
    assert configured == backend_normalized == {"deepseek": {"CNY": 75}}


def test_malformed_threshold_config_fails_closed_without_echoing_data(monkeypatch, tmp_path):
    from src.utils.provider_usage import balance_state

    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(
        "src.conductor_cli.config_files.find_config_file", lambda _name: config_path
    )
    monkeypatch.setattr(
        "src.conductor_cli.config_files.load_yaml_config",
        lambda _path: {
            "native_runtime": {
                "usage_headroom": {"low_balance_thresholds": {"deepseek": {"CNY": "secret-ish"}}}
            }
        },
    )
    thresholds = usage._configured_balance_thresholds()
    assert thresholds == {}
    assert balance_state("deepseek", {"amount": 60, "currency": "CNY"}, thresholds) == "unknown"


def test_cursor_warning_flag_survives_headroom_and_renders_warn_line(tmp_path, monkeypatch, capsys):
    now = datetime.now(timezone.utc)
    snapshot_file = tmp_path / "snapshot.json"
    snapshot_file.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "provider": "cursor",
                        "kind": "quota",
                        "status": "ok",
                        "updated_at": _stamp(now),
                        "windows": [{"used_percent": 85}],
                        "usage_warning": "approaching_limit",
                    },
                    {
                        "provider": "codex",
                        "kind": "quota",
                        "status": "ok",
                        "updated_at": _stamp(now),
                        "windows": [{"used_percent": 85}],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("src.conductor_cli.config_files.find_config_file", lambda _: None)

    result = usage.load_headroom(snapshot_file, now=now)
    cursor = next(row for row in result["providers"] if row["provider"] == "cursor")
    codex = next(row for row in result["providers"] if row["provider"] == "codex")
    assert cursor == {
        "provider": "cursor",
        "status": "ok",
        "remaining_percent": 15,
        "usage_warning": "approaching_limit",
    }
    assert codex == {"provider": "codex", "status": "ok", "remaining_percent": 15}

    usage.render_usage({"providers": [cursor]}, json_mode=False)
    text = capsys.readouterr().out
    assert text == (
        "cursor: 15% remaining\n"
        "cursor: warning: usage at or above 80% of a billing window\n"
    )


def test_cursor_warning_drops_when_triggering_window_expires(capsys) -> None:
    """A stale snapshot flag must not warn once its window has reset."""
    now = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
    expired = {
        "provider": "cursor",
        "status": "ok",
        "updated_at": _stamp(now - timedelta(minutes=1)),
        "windows": [
            {"used_percent": 95, "resets_at_utc": _stamp(now - timedelta(seconds=1))},
            {"used_percent": 10, "resets_at_utc": _stamp(now + timedelta(hours=5))},
        ],
        "usage_warning": "approaching_limit",
    }
    still_open = {
        "provider": "cursor",
        "status": "ok",
        "updated_at": _stamp(now - timedelta(minutes=1)),
        "windows": [
            {"used_percent": 95, "resets_at_utc": _stamp(now - timedelta(seconds=1))},
            {"used_percent": 80, "resets_at_utc": _stamp(now + timedelta(hours=5))},
        ],
        "usage_warning": "approaching_limit",
    }

    expired_result = usage._provider_headroom(expired, now)
    open_result = usage._provider_headroom(still_open, now)

    assert expired_result == {
        "provider": "cursor",
        "status": "ok",
        "remaining_percent": 90,
    }
    assert open_result == {
        "provider": "cursor",
        "status": "ok",
        "remaining_percent": 20,
        "usage_warning": "approaching_limit",
    }
    usage.render_usage({"providers": [expired_result]}, json_mode=False)
    assert capsys.readouterr().out == "cursor: 90% remaining\n"


def test_headroom_drops_unrecognized_warning_flag(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    snapshot_file = tmp_path / "snapshot.json"
    snapshot_file.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "provider": "cursor",
                        "status": "ok",
                        "updated_at": _stamp(now),
                        "windows": [{"used_percent": 10}],
                        "usage_warning": "bogus<script>",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("src.conductor_cli.config_files.find_config_file", lambda _: None)

    result = usage.load_headroom(snapshot_file, now=now)
    assert next(row for row in result["providers"] if row["provider"] == "cursor") == {
        "provider": "cursor", "status": "ok", "remaining_percent": 90,
    }


def test_stale_cursor_warning_row_is_unknown_without_flag(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    snapshot_file = tmp_path / "snapshot.json"
    snapshot_file.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "provider": "cursor",
                        "status": "ok",
                        "updated_at": _stamp(now - timedelta(minutes=20)),
                        "windows": [{"used_percent": 95}],
                        "usage_warning": "approaching_limit",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("src.conductor_cli.config_files.find_config_file", lambda _: None)

    result = usage.load_headroom(snapshot_file, now=now)
    assert next(row for row in result["providers"] if row["provider"] == "cursor") == {
        "provider": "cursor", "status": "unknown",
    }


def test_credits_kind_row_displays_remaining_with_credits_suffix(tmp_path, monkeypatch, capsys):
    now = datetime.now(timezone.utc)
    snapshot_file = tmp_path / "snapshot.json"
    snapshot_file.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "provider": "manus",
                        "kind": "credits",
                        "status": "ok",
                        "updated_at": _stamp(now),
                        "windows": [{"used_percent": 28}],
                        "credits": {"total": 1000},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("src.conductor_cli.config_files.find_config_file", lambda _: None)

    result = usage.load_headroom(snapshot_file, now=now)
    manus = next(row for row in result["providers"] if row["provider"] == "manus")
    assert manus == {
        "provider": "manus",
        "kind": "credits",
        "status": "ok",
        "remaining_percent": 72,
    }

    usage.render_usage({"providers": [manus]}, json_mode=False)
    assert capsys.readouterr().out == "manus: 72% remaining (credits)\n"


def test_credits_provider_without_windows_is_unknown(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    snapshot_file = tmp_path / "snapshot.json"
    snapshot_file.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "provider": "manus",
                        "kind": "credits",
                        "status": "ok",
                        "updated_at": _stamp(now),
                        "credits": {"total": 1000},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("src.conductor_cli.config_files.find_config_file", lambda _: None)

    result = usage.load_headroom(snapshot_file, now=now)
    assert result["providers"] == [_expected_unknown(provider) for provider in PROVIDERS]
    manus = next(row for row in result["providers"] if row["provider"] == "manus")
    assert manus == {"provider": "manus", "kind": "credits", "status": "unknown"}
