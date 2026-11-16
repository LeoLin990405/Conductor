"""Configuration and registry contracts for DSH-compatible Workers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from src.config.compat import (
    CompatConfigError,
    load_compat_workers,
    validate_compat_base_url,
)
from src.llms.native_cli.base import ReadOnlyUnavailable
from src.llms.native_cli.compat import CompatDshAdapter
from src.llms.native_cli.registry import (
    get_adapter,
    get_read_only_adapter,
    list_adapters,
)

ENV_NAME = "W9A_TEST_API_KEY"
SENTINEL = "sk-" "w9a-test-only-sentinel"


def _worker(**overrides: object) -> dict[str, object]:
    worker: dict[str, object] = {
        "name": "qwen-compat",
        "protocol": "openai-completions",
        "base_url": "https://example.invalid/v1",
        "model": "w9a-fake-model",
        "api_key_env": ENV_NAME,
        "context_window_tokens": 131072,
        "review_capable": False,
    }
    worker.update(overrides)
    return worker


def _write_config(path: Path, *workers: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"workers": list(workers)}, sort_keys=False),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _load(path: Path, **kwargs: object):
    kwargs.setdefault("environ", {ENV_NAME: SENTINEL})
    kwargs.setdefault("reserved_names", {"kimi", "deepseek-dsh"})
    return load_compat_workers(path, **kwargs)


def _config_error(path: Path, **kwargs: object) -> CompatConfigError:
    with pytest.raises(CompatConfigError) as caught:
        _load(path, **kwargs)
    error = caught.value
    assert isinstance(error.reason, str) and error.reason
    assert SENTINEL not in str(error)
    return error


def test_loads_a_secret_free_worker_config(tmp_path: Path) -> None:
    path = _write_config(tmp_path / "compat-workers.yaml", _worker())

    (worker,) = _load(path)

    assert worker.name == "qwen-compat"
    assert worker.protocol == "openai-completions"
    assert worker.base_url == "https://example.invalid/v1"
    assert worker.model == "w9a-fake-model"
    assert worker.api_key_env == ENV_NAME
    assert worker.context_window_tokens == 131072
    assert worker.review_capable is False
    assert SENTINEL not in repr(worker)


@pytest.mark.parametrize(
    "name",
    ["", "a", "Qwen-compat", "1qwen", "-qwen", "qwen_compat", "qwen.compat", "a" * 33],
)
def test_rejects_invalid_worker_names_with_stable_reasons(
    tmp_path: Path, name: str
) -> None:
    path = _write_config(tmp_path / "compat-workers.yaml", _worker(name=name))

    first = _config_error(path)
    second = _config_error(path)

    assert first.reason == second.reason


def test_rejects_a_name_reserved_by_an_existing_adapter(tmp_path: Path) -> None:
    path = _write_config(tmp_path / "compat-workers.yaml", _worker(name="kimi"))

    error = _config_error(path)

    assert "reserved" in error.reason or "name" in error.reason


def test_http_is_rejected_by_default_and_only_loopback_can_opt_in() -> None:
    with pytest.raises(CompatConfigError) as caught:
        validate_compat_base_url("http://127.0.0.1:8080/v1")
    assert caught.value.reason

    assert (
        validate_compat_base_url("http://127.0.0.1:8080/v1", allow_loopback=True)
        == "http://127.0.0.1:8080/v1"
    )
    assert (
        validate_compat_base_url("http://[::1]:8080/v1", allow_loopback=True)
        == "http://[::1]:8080/v1"
    )

    with pytest.raises(CompatConfigError):
        validate_compat_base_url("http://example.invalid/v1", allow_loopback=True)


@pytest.mark.parametrize(
    "url",
    [
        "https://user:sk-" "w9a-test-only-sentinel@example.invalid/v1",
        "https://example.invalid/v1?api_key=sk-" "w9a-test-only-sentinel",
        "https://example.invalid/v1#sk-" "w9a-test-only-sentinel",
    ],
    ids=["userinfo", "query", "fragment"],
)
def test_rejects_url_credentials_query_and_fragment_without_echoing_them(
    url: str,
) -> None:
    with pytest.raises(CompatConfigError) as caught:
        validate_compat_base_url(url)

    assert caught.value.reason
    assert SENTINEL not in str(caught.value)


def test_bracketed_ipv6_is_accepted_and_reconstructed() -> None:
    normalized = validate_compat_base_url("https://[2001:db8::1]:8443/v1")

    assert normalized == "https://[2001:db8::1]:8443/v1"


def test_unbracketed_ipv6_is_rejected() -> None:
    with pytest.raises(CompatConfigError) as caught:
        validate_compat_base_url("https://2001:db8::1:8443/v1")

    assert caught.value.reason


def test_missing_api_key_environment_is_a_stable_error(tmp_path: Path) -> None:
    path = _write_config(tmp_path / "compat-workers.yaml", _worker())

    first = _config_error(path, environ={})
    second = _config_error(path, environ={})

    assert first.reason == second.reason
    assert "env" in first.reason


def test_config_file_must_be_private(tmp_path: Path) -> None:
    path = _write_config(tmp_path / "compat-workers.yaml", _worker())
    path.chmod(0o644)

    error = _config_error(path)

    assert "permission" in error.reason or "mode" in error.reason
    path.chmod(0o600)
    assert _load(path)[0].name == "qwen-compat"


def test_review_capable_true_is_not_accepted_in_v1(tmp_path: Path) -> None:
    path = _write_config(tmp_path / "compat-workers.yaml", _worker(review_capable=True))

    error = _config_error(path)

    assert "review" in error.reason


def test_registry_marks_compat_and_never_exposes_environment_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    config = home / ".config" / "conductor" / "compat-workers.yaml"
    _write_config(config, _worker())
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(ENV_NAME, SENTINEL)

    rows = list_adapters()
    compat = next(row for row in rows if row["provider"] == "qwen-compat")

    assert compat["kind"] == "compat"
    serialized = json.dumps(rows, sort_keys=True)
    assert SENTINEL not in serialized
    assert all(SENTINEL not in str(value) for row in rows for value in row.values())


@pytest.mark.parametrize(
    "env_name",
    [
        "HOME",
        "DSH_HOME",
        "NATIVE_CLI_DSH_HOME",
        "NATIVE_CLI_DSH_BIN",
        "NATIVE_CLI_DSH_PROFILE",
    ],
)
def test_loader_rejects_adapter_controlled_env_names(
    tmp_path: Path, env_name: str
) -> None:
    path = _write_config(
        tmp_path / "compat-workers.yaml", _worker(api_key_env=env_name)
    )

    error = _config_error(path, environ={env_name: SENTINEL})

    assert error.reason == "invalid_api_key_env"


def _configure_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worker: dict[str, object] | None = None,
) -> Path:
    home = tmp_path / "home"
    config = home / ".config" / "conductor" / "compat-workers.yaml"
    _write_config(config, worker or _worker())
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(ENV_NAME, SENTINEL)
    return home


def test_registry_constructs_compat_adapter_through_its_configured_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _configure_registry(tmp_path, monkeypatch)

    adapter = get_adapter(
        "qwen-compat", binary="/opt/dsh", harness_home=str(home / ".dsh")
    )

    assert isinstance(adapter, CompatDshAdapter)
    assert adapter.provider == "qwen-compat"
    assert adapter.review_capable is False


def test_registry_refuses_read_only_compat_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_registry(tmp_path, monkeypatch)

    with pytest.raises(ReadOnlyUnavailable, match="no read-only mode"):
        get_read_only_adapter("qwen-compat")


@pytest.mark.parametrize("name", ["claude", "deepseek"])
def test_registry_reserves_pool_alias_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    _configure_registry(tmp_path, monkeypatch, _worker(name=name))

    with pytest.raises(CompatConfigError) as caught:
        list_adapters()

    assert caught.value.reason == "adapter_name_conflict"


def test_invalid_compat_config_fails_closed_for_builtin_adapter_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_registry(tmp_path, monkeypatch, _worker(name="INVALID"))

    with pytest.raises(CompatConfigError) as caught:
        get_adapter("kimi", binary="/opt/kimi")

    assert caught.value.reason == "invalid_name"


def test_workers_command_exists_and_never_exposes_secrets(
    tmp_path: Path,
) -> None:
    """``conductor workers`` exists, runs, and keeps the compat secret out.

    The baseline of this test was written before the command existed, and
    asserted that it did not. The command exists now, so the test asserts
    what matters about it: it runs, its output is a JSON document, and the
    secret of the compat worker -- whose key is in the environment, not in
    the configuration -- never reaches the terminal.

    The command is run from a directory that holds no ``src`` package and
    without a ``PYTHONPATH``: the wheel that ships ``conductor`` does not
    ship ``src.llms``, and a command that imported it would work in a
    checkout and break in an installation. That is the difference between
    this test passing in a checkout and passing for a user.
    """
    home = tmp_path / "home"
    _write_config(home / ".config" / "conductor" / "compat-workers.yaml", _worker())
    executable = Path(sys.executable).with_name("conductor")
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", ""),
        ENV_NAME: SENTINEL,
    }
    env.pop("PYTHONPATH", None)

    completed = subprocess.run(
        [str(executable), "workers", "--json"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert isinstance(json.loads(completed.stdout), dict)
    assert SENTINEL not in completed.stdout
    assert SENTINEL not in completed.stderr
