"""The eight development Workers are registered adapters, and they are isolated.

``src/llms/native_cli/registry.py`` is the adapter lookup; the daemon's
``supported_providers()`` is what it will launch; and ``process.run_process`` is
the launch boundary every native adapter goes through, so every Worker is
wrapped by the isolation layer. These tests pin those three facts, and pin
that the read-only refusal for the CLIs that have no read-only mode is
unchanged.
"""

from __future__ import annotations

import inspect
import sys

import pytest

from src.llms.native_cli import get_adapter, get_read_only_adapter
from src.llms.native_cli.base import ReadOnlyUnavailable
from src.llms.native_cli.registry import list_adapters

# The eight Workers, by the provider key the native runtime uses.
WORKER_PROVIDERS = [
    ("kimi", "KimiCliAdapter"),
    ("zcode", "ZCodeCliAdapter"),
    ("grok", "GrokCliAdapter"),
    ("cursor", "CursorAgentAdapter"),
    ("stepfun", "StepFunAdapter"),
    ("deepseek-dsh", "DeepSeekDshAdapter"),
    ("minimax", "MiniMaxCodeAdapter"),
    ("doubao", "DoubaoArkCliAdapter"),
]

# These CLIs have no read-only mode an adapter can select, so the read-only
# lane must keep refusing them. The dispatch pool records the same reason.
NO_READ_ONLY_MODE = ["kimi", "deepseek", "glm", "minimax"]


def _daemon_supported_providers() -> set[str]:
    """The providers the local daemon is willing to launch."""
    from scripts.conductor_local_daemon import supported_providers

    return supported_providers()


@pytest.mark.parametrize(("provider", "adapter"), WORKER_PROVIDERS, ids=[w[0] for w in WORKER_PROVIDERS])
def test_a_worker_provider_builds_its_adapter(provider, adapter, tmp_path) -> None:
    binary = tmp_path / "fake"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    instance = get_adapter(provider, binary=str(binary))
    assert instance.__class__.__name__ == adapter


@pytest.mark.parametrize(("provider", "adapter"), WORKER_PROVIDERS, ids=[w[0] for w in WORKER_PROVIDERS])
def test_a_worker_provider_is_offered_by_the_daemon(provider, adapter) -> None:
    assert provider in _daemon_supported_providers()


@pytest.mark.parametrize(("provider", "adapter"), WORKER_PROVIDERS, ids=[w[0] for w in WORKER_PROVIDERS])
def test_a_worker_adapter_starts_through_the_isolation_boundary(provider, adapter) -> None:
    """Every native adapter launches through ``process.run_process``."""
    import src.llms.native_cli.process as process_module
    from src.llms.native_cli import registry as registry_module

    module = sys.modules[registry_module._ADAPTERS[provider].__module__]
    source = inspect.getsource(module)
    assert "run_process" in source or "stream_process" in source, module.__name__
    assert hasattr(process_module, "prepare_process")


def test_the_adapter_list_reports_each_cli_once() -> None:
    providers = [row["provider"] for row in list_adapters()]
    assert len(providers) == len(set(providers))


def test_glm_and_zcode_share_one_adapter_registration() -> None:
    from src.llms.native_cli import registry as registry_module

    assert registry_module._ADAPTERS["glm"] is registry_module._ADAPTERS["zcode"]


def test_zcode_prefers_the_official_binary_when_both_exist(monkeypatch) -> None:
    from src.llms.native_cli import zcode

    found = {"zcode-cli": "/fake/zcode-cli", "zcode": "/fake/zcode"}
    monkeypatch.delenv("NATIVE_CLI_ZCODE_BIN", raising=False)
    monkeypatch.setattr(zcode.shutil, "which", lambda name: found.get(name))

    assert zcode.ZCodeCliAdapter().binary == "/fake/zcode"


def test_zcode_without_an_override_resolves_the_official_binary(monkeypatch) -> None:
    from src.llms.native_cli import zcode

    monkeypatch.delenv("NATIVE_CLI_ZCODE_BIN", raising=False)
    monkeypatch.setattr(
        zcode.shutil, "which", lambda name: "/fake/zcode" if name == "zcode" else None
    )

    assert zcode.ZCodeCliAdapter().binary == "/fake/zcode"


def test_zcode_falls_back_to_the_app_binary(monkeypatch) -> None:
    from src.llms.native_cli import zcode

    monkeypatch.delenv("NATIVE_CLI_ZCODE_BIN", raising=False)
    monkeypatch.setattr(
        zcode.shutil, "which", lambda name: {"zcode": "/fake/zcode"}.get(name)
    )

    assert zcode.ZCodeCliAdapter().binary == "/fake/zcode"


def test_clean_room_is_detected_by_invocation_or_resolved_name(tmp_path) -> None:
    """The two-step rule: invocation name first, then the resolved target."""
    from src.llms.native_cli.zcode import _is_clean_room_cli

    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "cli.js").write_text("// clean-room target\n", encoding="utf-8")
    plain_cli = lib / "zcode-cli"
    plain_cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "zcode-cli").symlink_to(lib / "cli.js")
    (bin_dir / "glm-custom").symlink_to(plain_cli)
    (bin_dir / "zcode").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    assert _is_clean_room_cli(str(bin_dir / "zcode-cli")) is True  # fast path
    assert _is_clean_room_cli(str(bin_dir / "glm-custom")) is True  # resolved name
    assert _is_clean_room_cli(str(bin_dir / "zcode")) is False  # plain wrapper
    assert _is_clean_room_cli(str(lib / "cli.js")) is False  # resolved target itself


@pytest.mark.parametrize("provider", NO_READ_ONLY_MODE)
def test_a_cli_without_a_read_only_mode_is_still_refused(provider) -> None:
    with pytest.raises(ReadOnlyUnavailable):
        get_read_only_adapter(provider)


def test_the_read_only_lane_still_serves_the_clis_that_have_one() -> None:
    for provider in ("codex", "claude-code", "stepfun"):
        assert get_read_only_adapter(provider) is not None
