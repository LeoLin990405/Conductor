"""CodexBar snapshot export tests use synthetic CLI output only."""

import json
import io
import os
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.conductor_cli import codexbar_snapshot as snapshot


NOW = "2026-11-02T12:00:00Z"
SENTINEL = "private.person@example.test account_private_123456 token-secret"


@pytest.fixture(autouse=True)
def never_run_codexbar_version(monkeypatch):
    # collect_snapshot stamps the CodexBar version with one extra CLI call; the
    # fake runners below model only `usage` calls.
    monkeypatch.setattr(snapshot, "_codexbar_version", lambda codexbar, timeout: None)


@pytest.fixture(autouse=True)
def never_read_real_deepseek_env(monkeypatch, tmp_path):
    monkeypatch.setattr(snapshot, "DEFAULT_DEEPSEEK_ENV", tmp_path / "absent-env")


def _result(provider, *, usage=None, error=None, returncode=0, stdout=None, stderr=""):
    if stdout is None:
        item = {"provider": provider}
        if usage is not None:
            item["usage"] = usage
        if error is not None:
            item["error"] = error
        stdout = json.dumps([item])
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_collect_runs_allowlist_sequentially_and_projects_only_safe_fields(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        provider = command[3]
        return _result(
            provider,
            usage={
                "updatedAt": "2026-11-02T04:00:00-08:00",
                "primary": {"usedPercent": 35, "resetsAt": "2026-11-02T14:00:00+02:00", "label": SENTINEL},
                "details": [{"rows": [{"value": SENTINEL}]}],
                "loginMethod": SENTINEL,
            },
        )

    monkeypatch.setattr(snapshot.subprocess, "run", fake_run)
    payload = snapshot.collect_snapshot(codexbar="/safe/fake-cli", timeout=23)

    assert [call[0][3] for call in calls] == [p for p in snapshot.PROVIDERS if p != "deepseek"]
    assert all(call[0] == ["/safe/fake-cli", "usage", "--provider", call[0][3], "--format", "json", "--no-color"] for call in calls)
    assert all(call[1] == {"capture_output": True, "text": True, "timeout": 23, "check": False} for call in calls)
    assert payload["providers"][0] == {
        "provider": "codex",
        "kind": "quota",
        "status": "ok",
        "updated_at": NOW,
        "windows": [{"used_percent": 35, "role": "primary", "resets_at_utc": "2026-11-02T12:00:00Z"}],
        # The key name of the unknown window field is reported, never its value.
        "schema_drift": ["window.label"],
    }
    assert SENTINEL not in json.dumps(payload)
    assert set(payload) == {"updatedAt", "codexbar_version", "validated_codexbar_version", "providers"}


@pytest.mark.parametrize(
    "stdout,returncode",
    [
        (json.dumps([{"provider": "codex", "usage": {"updatedAt": NOW, "primary": {"usedPercent": 10}}, "error": {"message": SENTINEL}}]), 0),
        ("not json " + SENTINEL, 0),
        ("[]", 1),
        (json.dumps({"provider": "codex", "usage": {"updatedAt": "old", "primary": {"usedPercent": 1}}}), 0),
        (json.dumps([{ "provider": "codex", "usage": {"updatedAt": NOW, "primary": {"usedPercent": 101}}}]), 0),
        (json.dumps([{ "provider": "codex", "usage": {"updatedAt": NOW, "primary": {"usedPercent": 10, "resetsAt": "bad"}}}]), 0),
        (json.dumps([{ "provider": "codex", "usage": {"updatedAt": NOW, "primary": {"usedPercent": 10}}, "identity": SENTINEL}, {"provider": "codex", "usage": {"updatedAt": NOW, "primary": {"usedPercent": 20}}}]), 0),
    ],
    ids=["error-with-usage", "invalid-json", "nonzero", "stale-timestamp-shape", "out-of-range", "invalid-reset", "multiple-accounts"],
)
def test_bad_or_ambiguous_result_is_unknown_without_source_text(monkeypatch, stdout, returncode):
    monkeypatch.setattr(snapshot.subprocess, "run", lambda *_a, **_k: _result("codex", stdout=stdout, returncode=returncode, stderr=SENTINEL))

    payload = snapshot.collect_snapshot()

    row = payload["providers"][0]
    assert row["status"] == "unknown"
    assert row["windows"] == []
    assert SENTINEL not in json.dumps(payload)


def test_timeouts_and_os_errors_become_unknown(monkeypatch):
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("codexbar", 40, output=SENTINEL, stderr=SENTINEL)

    monkeypatch.setattr(snapshot.subprocess, "run", timeout)
    assert all(row["status"] == "unknown" for row in snapshot.collect_snapshot()["providers"])

    monkeypatch.setattr(snapshot.subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(FileNotFoundError(SENTINEL)))
    assert SENTINEL not in json.dumps(snapshot.collect_snapshot())

    monkeypatch.setattr(snapshot.subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(UnicodeDecodeError("utf-8", b"x", 0, 1, SENTINEL)))
    assert all(row["status"] == "unknown" for row in snapshot.collect_snapshot()["providers"])


def test_one_provider_error_does_not_stop_remaining_provider_calls(monkeypatch):
    failed = {"kimi", "minimax", "stepfun", "mimo", "zai"}
    calls = []

    def fake_run(command, **_kwargs):
        provider = command[3]
        calls.append(provider)
        if provider in failed:
            return _result(provider, error={"message": SENTINEL})
        return _result(provider, usage={"updatedAt": NOW, "primary": {"usedPercent": 30}})

    monkeypatch.setattr(snapshot.subprocess, "run", fake_run)

    payload = snapshot.collect_snapshot()
    rows = {row["provider"]: row for row in payload["providers"]}

    assert calls == [p for p in snapshot.PROVIDERS if p != "deepseek"]
    assert all(rows[name]["status"] == "unknown" for name in failed)
    assert all(rows[name]["status"] == "ok" for name in {"codex", "claude", "grok", "doubao"})
    assert rows["deepseek"]["status"] == "unknown"
    assert SENTINEL not in json.dumps(payload)


def test_defaults_use_expected_snapshot_path_and_timeout(monkeypatch):
    calls = []
    monkeypatch.setattr(
        snapshot.subprocess,
        "run",
        lambda command, **kwargs: (calls.append((command, kwargs)) or _result(
            command[3], usage={"updatedAt": NOW, "primary": {"usedPercent": 30}}
        )),
    )

    snapshot.collect_snapshot()

    assert snapshot.DEFAULT_OUTPUT == Path.home() / ".conductor" / "usage" / "codexbar-snapshot.json"
    assert len(calls) == len(snapshot.PROVIDERS) - 1
    assert all(call[1]["timeout"] == 40.0 for call in calls)


@pytest.mark.parametrize("number", ["NaN", "Infinity", "1" + "0" * 1000])
def test_non_finite_and_unbounded_percentages_are_unknown(monkeypatch, number):
    stdout = (
        '[{"provider":"codex","usage":{"updatedAt":"'
        + NOW
        + '","primary":{"usedPercent":'
        + number
        + "}}}]"
    )
    monkeypatch.setattr(snapshot.subprocess, "run", lambda *_a, **_k: _result("codex", stdout=stdout))

    assert snapshot.collect_snapshot()["providers"][0]["status"] == "unknown"


def test_object_and_wrapped_object_forms_are_accepted():
    direct = {"provider": "codex", "usage": {"updatedAt": NOW, "secondary": {"usedPercent": 100}}}
    wrapped = {"providers": [{"provider": "claude", "usage": {"updatedAt": NOW, "primary": {"usedPercent": 0}}}]}

    assert snapshot._provider_row(direct, "codex")["windows"] == [{"used_percent": 100, "role": "secondary"}]
    assert snapshot._provider_row(wrapped, "claude")["status"] == "ok"


def test_deepseek_codexbar_percentage_cannot_create_balance_or_quota_state():
    row = snapshot._provider_row({
        "provider": "deepseek",
        "usage": {"updatedAt": NOW, "primary": {"usedPercent": 0}},
    }, "deepseek")
    assert row == {"provider": "deepseek", "kind": "balance", "status": "unknown"}


@pytest.mark.parametrize(
    "usage",
    [
        {"updatedAt": NOW, "primary": {"usedPercent": 20}, "secondary": {"usedPercent": 101}},
        {
            "updatedAt": NOW,
            "primary": {"usedPercent": 20},
            "extraRateWindows": [{"window": {"usedPercent": -1}}],
        },
        {
            "updatedAt": NOW,
            "primary": {"usedPercent": 20},
            "secondary": {"usedPercent": 20},
            "tertiary": {"usedPercent": 20},
            "extraRateWindows": [
                {"window": {"usedPercent": 20}} for _ in range(38)
            ],
        },
        {"updatedAt": NOW, "details": [{"rows": [{"label": "Balance", "value": "$10"}]}]},
    ],
    ids=["invalid-secondary-companion", "invalid-extra-companion", "too-many-total-windows", "balance-details-only"],
)
def test_malformed_or_non_quota_companions_cannot_create_headroom(usage):
    row = snapshot._provider_row({"provider": "codex", "usage": usage}, "codex")

    assert row["status"] == "unknown"
    assert row["windows"] == []


def test_exhausted_secondary_window_remains_exhausted_after_reader(tmp_path):
    from src.server.services.native_runtime.usage_headroom import read_usage_headroom

    target = tmp_path / "snapshot.json"
    snapshot.write_snapshot(
        {
            "providers": [
                {
                    "provider": "codex",
                    "status": "ok",
                    "updated_at": NOW,
                    "windows": [{"used_percent": 0}, {"used_percent": 100}],
                }
            ]
        },
        target,
    )

    assert read_usage_headroom(
        str(target), now=datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    ) == {"codex": "exhausted"}


def test_underflowing_utc_offset_timestamp_is_unknown():
    result = snapshot._provider_row(
        {
            "provider": "codex",
            "usage": {
                "updatedAt": "0001-01-01T00:00:00+01:00",
                "primary": {"usedPercent": 20},
            },
        },
        "codex",
    )

    assert result["status"] == "unknown"


def test_write_snapshot_is_private_atomic_and_replaces_existing_file(tmp_path):
    target = tmp_path / "usage" / "snapshot.json"
    target.parent.mkdir()
    target.write_text('{"old":true}\n', encoding="utf-8")

    snapshot.write_snapshot({"providers": []}, target)

    assert json.loads(target.read_text(encoding="utf-8")) == {"providers": []}
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert list(target.parent.iterdir()) == [target]


def test_replace_failure_keeps_previous_file_and_cleans_temp(monkeypatch, tmp_path):
    target = tmp_path / "snapshot.json"
    target.write_text("old", encoding="utf-8")

    def fail_replace(*_args):
        raise OSError("private error detail")

    monkeypatch.setattr(snapshot.os, "replace", fail_replace)
    with pytest.raises(OSError):
        snapshot.write_snapshot({"providers": []}, target)

    assert target.read_text(encoding="utf-8") == "old"
    assert list(tmp_path.iterdir()) == [target]


def test_serialization_failure_preserves_private_file_and_cleans_temp(tmp_path):
    target = tmp_path / "snapshot.json"
    target.write_text("old", encoding="utf-8")
    target.chmod(0o600)

    with pytest.raises(TypeError):
        snapshot.write_snapshot({"unsafe": object()}, target)

    assert target.read_text(encoding="utf-8") == "old"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [target]


def test_symlink_destination_is_replaced_without_changing_link_target(tmp_path):
    target = tmp_path / "snapshot.json"
    original = tmp_path / "original.json"
    original.write_text("original target", encoding="utf-8")
    target.symlink_to(original)

    snapshot.write_snapshot({"providers": []}, target)

    assert not target.is_symlink()
    assert json.loads(target.read_text(encoding="utf-8")) == {"providers": []}
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert original.read_text(encoding="utf-8") == "original target"


def test_stale_cli_observation_stays_stale_when_reader_checks_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot.subprocess, "run", lambda *_a, **_k: _result(
        "codex", usage={"updatedAt": "2026-11-02T10:00:00Z", "primary": {"usedPercent": 5}}
    ))
    target = tmp_path / "snapshot.json"
    snapshot.refresh_snapshot(target)

    from src.server.services.native_runtime.usage_headroom import read_usage_headroom

    assert read_usage_headroom(
        str(target), now=datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc), max_age_seconds=900
    ) == {"deepseek-dsh": "unknown"}


def test_snapshot_reader_and_router_preserve_headroom_order(tmp_path, monkeypatch):
    from src.server.services.native_runtime.router import RuntimeRequest, route_runtime
    from src.server.services.native_runtime.usage_headroom import read_usage_headroom

    def fake_run(command, **kwargs):
        provider = command[3]
        used = {"codex": 100}.get(provider)
        usage = {"updatedAt": datetime.now(timezone.utc).isoformat()}
        if used is not None:
            usage["primary"] = {"usedPercent": used}
        return _result(provider, usage=usage)

    monkeypatch.setattr(snapshot.subprocess, "run", fake_run)
    monkeypatch.setattr(snapshot, "_read_deepseek_balance", lambda *_a, **_k: {
        "provider": "deepseek", "kind": "balance", "status": "ok",
        "balance": {"amount": 100, "currency": "CNY"},
    })
    target = tmp_path / "snapshot.json"
    snapshot.refresh_snapshot(target)
    headroom = read_usage_headroom(
        str(target), now=datetime.now(timezone.utc)
    )

    @dataclass
    class Status:
        provider: str
        capabilities: dict
        installed: bool = True
        live_verified: bool = True

    decision = route_runtime(
        [Status("codex", {"streaming": True}), Status("deepseek-dsh", {"streaming": True})],
        RuntimeRequest(task="refactor code"),
        headroom=headroom,
    )
    assert headroom == {"codex": "exhausted", "deepseek-dsh": "available"}
    assert decision.runtime == "deepseek-dsh"


def test_executable_smoke_from_other_cwd_uses_only_fake_codexbar(tmp_path):
    from src.server.services.native_runtime.router import RuntimeRequest, route_runtime
    from src.server.services.native_runtime.usage_headroom import read_usage_headroom

    fake_cli = tmp_path / "fake-codexbar"
    fake_cli.write_text(
        "#!" + sys.executable + "\n"
        "import json, sys\n"
        "from datetime import datetime, timezone\n"
        "provider = sys.argv[3]\n"
        "used = {'codex': 100}.get(provider)\n"
        "usage = {'updatedAt': datetime.now(timezone.utc).isoformat()}\n"
        "if used is not None: usage['primary'] = {'usedPercent': used}\n"
        "print(json.dumps([{'provider': provider, 'usage': usage}]))\n",
        encoding="utf-8",
    )
    fake_cli.chmod(0o700)
    script = Path(__file__).resolve().parents[3] / "scripts" / "codexbar_snapshot.py"
    output = tmp_path / "snapshot.json"
    completed = subprocess.run(
        [sys.executable, str(script), "--output", str(output), "--codexbar", str(fake_cli),
         "--deepseek-env-file", str(tmp_path / "absent-env")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        env={"PATH": "/usr/bin:/bin", "PYTHON_DOTENV_DISABLED": "1"},
        check=False,
    )

    assert completed.returncode == 0
    assert output.is_file()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert "fake-codexbar" not in completed.stdout + completed.stderr
    headroom = read_usage_headroom(
        str(output), now=datetime.now(timezone.utc)
    )

    @dataclass
    class Status:
        provider: str
        capabilities: dict
        installed: bool = True
        live_verified: bool = True

    decision = route_runtime(
        [Status("codex", {"streaming": True}), Status("deepseek-dsh", {"streaming": True})],
        RuntimeRequest(task="refactor code"),
        headroom=headroom,
    )
    assert decision.runtime == "deepseek-dsh"


def test_deepseek_balance_uses_only_total_balance_and_never_leaks_credentials(tmp_path, capsys):
    env_file = tmp_path / "deepseek.env"
    env_file.write_text("export DEEPSEEK_API_KEY='fake-secret-value'\n", encoding="utf-8")

    class Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    class FakeOpener:
        request = None

        def open(self, request, timeout):
            self.request = request
            assert timeout == 10
            return Response(json.dumps({
                "is_available": True,
                "balance_infos": [
                    {"currency": "USD", "total_balance": "99", "granted_balance": 9999},
                    {"currency": "CNY", "total_balance": "40.5", "topped_up_balance": 8888},
                ],
            }).encode())

    opener = FakeOpener()
    row = snapshot._read_deepseek_balance(env_file, timeout=10, opener=opener)
    assert row == {
        "provider": "deepseek", "kind": "balance", "status": "ok",
        "balance": {"amount": 40.5, "currency": "CNY"},
    }
    assert opener.request.full_url == "https://api.deepseek.com/user/balance"
    assert opener.request.get_header("Authorization") == "Bearer fake-secret-value"
    assert "fake-secret-value" not in capsys.readouterr().out


class _FakeHTTPResponse(io.BytesIO):
    def __init__(self, body, status=200):
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class _FakeOpener:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if self.error:
            raise self.error
        return self.response


def _fake_balance_env(path):
    path.write_text("export DEEPSEEK_API_KEY=\"fake-key-value\" # local test\n", encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "response,error",
    [
        (_FakeHTTPResponse(b'{"is_available":true,"balance_infos":[]}', status=503), None),
        (None, urllib.error.URLError("private transport details")),
        (None, TimeoutError("private timeout details")),
        (_FakeHTTPResponse(b"not json"), None),
        (_FakeHTTPResponse(b" " * (snapshot._MAX_HTTP_BYTES + 1)), None),
        (_FakeHTTPResponse(b'{"balance_infos":[]}'), None),
        (_FakeHTTPResponse(b'{"is_available":"true","balance_infos":[]}'), None),
        (_FakeHTTPResponse(b'{"is_available":true,"balance_infos":[{"currency":"CNY","total_balance":true}]}'), None),
        (_FakeHTTPResponse(b'{"is_available":true,"balance_infos":[{"currency":"CNY","total_balance":"NaN"}]}'), None),
        (_FakeHTTPResponse(b'{"is_available":true,"balance_infos":[{"currency":"CNY","total_balance":"1e999"}]}'), None),
        (_FakeHTTPResponse(b'{"is_available":true,"balance_infos":[{"currency":"CNY","total_balance":' + b"9" * 1000 + b'}]}'), None),
        (_FakeHTTPResponse(b'{"is_available":true,"balance_infos":[{"currency":"CNY"}]}'), None),
        (_FakeHTTPResponse(b'{"is_available":true,"balance_infos":[{"currency":"CNY","total_balance":1},{"currency":"CNY","total_balance":2},{"currency":"USD","total_balance":90}]}'), None),
    ],
    ids=["http-status", "url-error", "timeout", "invalid-json", "oversized", "missing-availability", "wrong-availability", "bool-amount", "nan-amount", "infinite-amount", "extreme-integer", "missing-total", "conflicting-cny"],
)
def test_deepseek_fetch_failures_and_ambiguous_shapes_stay_unknown(tmp_path, response, error):
    env_file = _fake_balance_env(tmp_path / "deepseek.env")
    opener = _FakeOpener(response=response, error=error)
    row = snapshot._read_deepseek_balance(env_file, opener=opener)
    assert row == {"provider": "deepseek", "kind": "balance", "status": "unknown"}
    assert len(opener.calls) == 1
    request, _timeout = opener.calls[0]
    assert request.full_url == snapshot._DEEPSEEK_URL


@pytest.mark.parametrize("content", ["", "DEEPSEEK_API_KEY=\n", "DEEPSEEK_API_KEY='unterminated\n", "OTHER_KEY=private\n"])
def test_missing_or_malformed_env_never_calls_opener(tmp_path, content):
    env_file = tmp_path / "deepseek.env"
    env_file.write_text(content, encoding="utf-8")
    opener = _FakeOpener(response=_FakeHTTPResponse(b"{}"))
    assert snapshot._read_deepseek_balance(env_file, opener=opener)["status"] == "unknown"
    assert opener.calls == []


def test_fifo_env_path_is_rejected_without_blocking_or_network(tmp_path):
    env_fifo = tmp_path / "deepseek.env.fifo"
    os.mkfifo(env_fifo)
    opener = _FakeOpener(response=_FakeHTTPResponse(b"{}"))
    assert snapshot._env_key(env_fifo) is None
    assert snapshot._read_deepseek_balance(env_fifo, opener=opener)["status"] == "unknown"
    assert opener.calls == []


def test_nonpositive_balance_when_api_says_unavailable_is_still_a_valid_observation(tmp_path):
    env_file = _fake_balance_env(tmp_path / "deepseek.env")
    opener = _FakeOpener(response=_FakeHTTPResponse(
        b'{"is_available":false,"balance_infos":[{"currency":"CNY","total_balance":"0"}]}'
    ))
    row = snapshot._read_deepseek_balance(env_file, opener=opener)
    assert row["status"] == "ok"
    assert row["balance"] == {"amount": 0.0, "currency": "CNY"}


def test_unavailable_positive_balance_is_unknown(tmp_path):
    env_file = _fake_balance_env(tmp_path / "deepseek.env")
    opener = _FakeOpener(response=_FakeHTTPResponse(
        b'{"is_available":false,"balance_infos":[{"currency":"CNY","total_balance":"20"}]}'
    ))
    assert snapshot._read_deepseek_balance(env_file, opener=opener) == {
        "provider": "deepseek", "kind": "balance", "status": "unknown"
    }


def test_zero_balance_is_exhausted_in_reader(tmp_path):
    from src.server.services.native_runtime.usage_headroom import read_usage_headroom

    target = tmp_path / "snapshot.json"
    snapshot.write_snapshot({"providers": [{
        "provider": "deepseek", "kind": "balance", "status": "ok",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "balance": {"amount": 0, "currency": "CNY"},
    }]}, target)
    assert read_usage_headroom(str(target), now=datetime.now(timezone.utc)) == {
        "deepseek-dsh": "exhausted"
    }


def test_cli_prints_redacted_summary_and_persists_private_numeric_balance(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(snapshot.subprocess, "run", lambda command, **_kwargs: _result(
        command[3], usage={"updatedAt": NOW, "primary": {"usedPercent": 1}}
    ))
    monkeypatch.setattr(snapshot, "_read_deepseek_balance", lambda *_args, **_kwargs: {
        "provider": "deepseek", "kind": "balance", "status": "ok",
        "balance": {"amount": 12.34, "currency": "CNY"},
    })
    target = tmp_path / "snapshot.json"

    assert snapshot.main(["--output", str(target), "--deepseek-env-file", str(tmp_path / "unused")]) == 0
    printed = capsys.readouterr().out
    saved = json.loads(target.read_text(encoding="utf-8"))
    deepseek = next(row for row in saved["providers"] if row["provider"] == "deepseek")
    assert "12.34" not in printed and "fake-key-value" not in printed
    assert deepseek["balance"] == {"amount": 12.34, "currency": "CNY"}
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.parametrize("amount,expected", [(49, "low"), (50, "available")])
def test_fresh_balance_reader_applies_threshold_transition(tmp_path, monkeypatch, amount, expected):
    from src.server.services.native_runtime.usage_headroom import read_usage_headroom

    monkeypatch.setattr(snapshot.subprocess, "run", lambda command, **_kwargs: _result(
        command[3], error={"message": "not used"}
    ))
    monkeypatch.setattr(snapshot, "_read_deepseek_balance", lambda *_args, **_kwargs: {
        "provider": "deepseek", "kind": "balance", "status": "ok",
        "balance": {"amount": amount, "currency": "CNY"},
    })
    target = tmp_path / "snapshot.json"
    snapshot.refresh_snapshot(target)
    states = read_usage_headroom(str(target), now=datetime.now(timezone.utc))
    assert states["deepseek-dsh"] == expected


def test_redirect_handler_refuses_redirects_without_network():
    handler = snapshot._NoRedirect()
    assert handler.redirect_request(
        urllib.request.Request(snapshot._DEEPSEEK_URL), None, 302, "Found", {},
        "https://attacker.example/collect",
    ) is None


def _stamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def test_collect_queries_all_twelve_enabled_providers(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command[3])
        return _result(command[3], usage={"updatedAt": NOW, "primary": {"usedPercent": 10}})

    monkeypatch.setattr(snapshot.subprocess, "run", fake_run)
    payload = snapshot.collect_snapshot(codexbar="/safe/fake-cli")

    assert snapshot.PROVIDERS == (
        "codex", "claude", "cursor", "zai", "minimax", "manus",
        "kimi", "mimo", "doubao", "deepseek", "stepfun", "grok",
    )
    assert calls == [p for p in snapshot.PROVIDERS if p != "deepseek"]
    rows = {row["provider"]: row for row in payload["providers"]}
    assert rows["cursor"]["kind"] == "quota"
    assert rows["manus"]["kind"] == "credits"
    assert all(row["status"] == "ok" for row in rows.values() if row["provider"] != "deepseek")


def test_cursor_usage_warning_flags_only_active_windows_at_or_above_80():
    now = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
    future = _stamp(now + timedelta(hours=1))
    past = _stamp(now - timedelta(seconds=1))

    assert snapshot._usage_warning("cursor", [{"used_percent": 80}], now) == "approaching_limit"
    assert snapshot._usage_warning("cursor", [{"used_percent": 79.9}], now) is None
    assert snapshot._usage_warning("cursor", [{"used_percent": 95, "resets_at_utc": future}], now) == "approaching_limit"
    assert snapshot._usage_warning("cursor", [{"used_percent": 95, "resets_at_utc": past}], now) is None
    assert snapshot._usage_warning("cursor", [{"used_percent": "95"}], now) is None
    assert snapshot._usage_warning("codex", [{"used_percent": 100}], now) is None
    assert snapshot._usage_warning("manus", [{"used_percent": 100}], now) is None


def test_collect_stamps_cursor_warning_only_on_ok_rows(monkeypatch):
    def fake_run(command, **kwargs):
        provider = command[3]
        if provider == "cursor":
            return _result(provider, usage={"updatedAt": NOW, "primary": {"usedPercent": 82}})
        if provider == "manus":
            return _result(provider, error={"message": SENTINEL})
        return _result(provider, usage={"updatedAt": NOW, "primary": {"usedPercent": 10}})

    monkeypatch.setattr(snapshot.subprocess, "run", fake_run)
    rows = {row["provider"]: row for row in snapshot.collect_snapshot()["providers"]}

    assert rows["cursor"]["usage_warning"] == "approaching_limit"
    assert "usage_warning" not in rows["codex"]
    assert "usage_warning" not in rows["manus"]


def test_manus_credits_row_parses_windows_and_ignores_identity_text():
    payload = [{
        "provider": "manus",
        "usage": {
            "updatedAt": NOW,
            "primary": {"usedPercent": 30, "resetDescription": "Total 1,000 credits"},
            "secondary": {"usedPercent": 12, "resetsAt": "2026-11-05T00:00:00Z"},
            "identity": {"loginMethod": SENTINEL},
        },
    }]

    row = snapshot._credits_row(payload, "manus")

    assert row == {
        "provider": "manus",
        "kind": "credits",
        "status": "ok",
        "updated_at": NOW,
        "windows": [
            {"used_percent": 30, "role": "primary"},
            {"used_percent": 12, "role": "secondary", "resets_at_utc": "2026-11-05T00:00:00Z"},
        ],
    }
    assert SENTINEL not in json.dumps(row)


def test_manus_credits_allowance_from_structured_fields():
    via_quota = [{
        "provider": "manus",
        "usage": {
            "updatedAt": NOW,
            "primary": {"usedPercent": 5, "quotaLimit": 1000, "quotaUnit": "credits"},
        },
    }]
    assert snapshot._credits_row(via_quota, "manus")["credits"] == {"total": 1000}

    via_direct = [{
        "provider": "manus",
        "usage": {"updatedAt": NOW, "primary": {"usedPercent": 5}, "credits": 250.0},
    }]
    assert snapshot._credits_row(via_direct, "manus")["credits"] == {"total": 250.0}

    via_both = [{
        "provider": "manus",
        "usage": {
            "updatedAt": NOW,
            "primary": {"usedPercent": 5, "quotaLimit": 100, "quotaUnit": "credits"},
            "credits": 250.0,
        },
    }]
    assert snapshot._credits_row(via_both, "manus")["credits"] == {"total": 250.0}


def test_manus_non_credit_quota_units_do_not_create_a_credits_block():
    payload = [{
        "provider": "manus",
        "usage": {
            "updatedAt": NOW,
            "primary": {"usedPercent": 5, "quotaLimit": 1000, "quotaUnit": "requests"},
        },
    }]

    row = snapshot._credits_row(payload, "manus")

    assert row["status"] == "ok"
    assert "credits" not in row


@pytest.mark.parametrize(
    "usage",
    [
        {"updatedAt": NOW, "primary": {"usedPercent": 5}, "credits": "lots"},
        {"updatedAt": NOW, "primary": {"usedPercent": 5}, "credits": -1},
        {"updatedAt": NOW, "primary": {"usedPercent": 5}, "credits": 10**13},
        {"updatedAt": NOW, "primary": {"usedPercent": 5}, "credits": float("nan")},
        {"updatedAt": NOW, "primary": {"usedPercent": 5, "quotaLimit": "1000", "quotaUnit": "credits"}},
        {"updatedAt": NOW, "primary": {"usedPercent": 5, "quotaLimit": 1000}},
        {"updatedAt": NOW, "primary": {"usedPercent": 5, "quotaUnit": "credits"}},
        {"updatedAt": NOW, "primary": {"usedPercent": 5, "quotaLimit": -5, "quotaUnit": "credits"}},
        {"updatedAt": NOW, "primary": {"usedPercent": 5, "quotaLimit": 10**13, "quotaUnit": "credits"}},
    ],
    ids=[
        "string-credits", "negative-credits", "oversized-credits", "nan-credits",
        "string-quota-limit", "missing-quota-unit", "missing-quota-limit",
        "negative-quota-limit", "oversized-quota-limit",
    ],
)
def test_manus_malformed_credits_fields_stay_unknown(usage):
    row = snapshot._credits_row([{"provider": "manus", "usage": usage}], "manus")

    assert row == {"provider": "manus", "kind": "credits", "status": "unknown", "windows": []}


def test_credits_provider_cli_failure_row_carries_credits_kind(monkeypatch):
    monkeypatch.setattr(snapshot.subprocess, "run", lambda *_a, **_k: _result("manus", returncode=1))

    assert snapshot._read_credits_provider("/safe/fake-cli", "manus", 10) == {
        "provider": "manus", "kind": "credits", "status": "unknown", "windows": [],
    }


def test_cursor_row_is_quota_kind_without_credits_handling():
    row = snapshot._provider_row(
        {"provider": "cursor", "usage": {"updatedAt": NOW, "primary": {"usedPercent": 20, "credits": 5}}},
        "cursor",
    )

    assert row == {
        "provider": "cursor",
        "kind": "quota",
        "status": "ok",
        "updated_at": NOW,
        "windows": [{"used_percent": 20, "role": "primary"}],
        "schema_drift": ["window.credits"],
    }
