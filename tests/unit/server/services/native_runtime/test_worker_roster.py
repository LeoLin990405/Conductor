"""The nine development Workers are registered, and the reserved roles are not.

``src/server/services/native_runtime/registry.py`` keeps the honest capability
table over the CLI adapters. These tests pin the Worker roster on top of it:
the nine dispatch Workers resolve to a runtime key that exists and has
capabilities, the reserved roles are refused, and the codex/claude-code entries
are untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.server.services.native_runtime.contracts import NativeRuntimeError
from src.server.services.native_runtime.registry import (
    DEVELOPMENT_WORKERS,
    RESERVED_NON_WORKERS,
    _DEFAULT_BINARY,
    _discovered_binary,
    capabilities_for,
    provider_ids,
    status_for,
    worker_provider_id,
)

# Roster name -> native runtime key -> adapter class name.
EXPECTED = {
    "kimi": ("kimi", "KimiCliAdapter"),
    "glm": ("zcode", "ZCodeCliAdapter"),
    "grok": ("grok", "GrokCliAdapter"),
    "cursor": ("cursor", "CursorAgentAdapter"),
    "stepfun": ("stepfun", "StepFunAdapter"),
    "deepseek": ("deepseek-dsh", "DeepSeekDshAdapter"),
    "minimax": ("minimax", "MiniMaxCodeAdapter"),
    "doubao": ("doubao", "DoubaoArkCliAdapter"),
    "opencode": ("opencode", "OpenCodeAdapter"),
}

ADAPTER_CLASSES = {
    "kimi": "KimiCliAdapter",
    "zcode": "ZCodeCliAdapter",
    "grok": "GrokCliAdapter",
    "cursor": "CursorAgentAdapter",
    "stepfun": "StepFunAdapter",
    "deepseek-dsh": "DeepSeekDshAdapter",
    "minimax": "MiniMaxCodeAdapter",
    "doubao": "DoubaoArkCliAdapter",
    "opencode": "OpenCodeAdapter",
}


def test_the_roster_holds_exactly_the_nine_workers() -> None:
    assert set(DEVELOPMENT_WORKERS) == set(EXPECTED)
    assert len(DEVELOPMENT_WORKERS) == 9


def test_each_worker_resolves_to_its_runtime_key() -> None:
    for roster, (provider, _adapter) in EXPECTED.items():
        assert worker_provider_id(roster) == provider


def test_each_worker_resolves_to_a_registered_provider() -> None:
    known = provider_ids()
    for roster in DEVELOPMENT_WORKERS:
        provider = worker_provider_id(roster)
        assert provider in known, (roster, provider)


def test_each_worker_provider_has_capabilities() -> None:
    for roster in DEVELOPMENT_WORKERS:
        provider = worker_provider_id(roster)
        caps = capabilities_for(provider)
        assert caps is not None


def test_each_worker_provider_uses_the_expected_adapter() -> None:
    from src.server.services.native_runtime import registry as module

    for roster, (provider, adapter) in EXPECTED.items():
        entry = module._ADAPTERS[provider]
        assert entry[0].__name__ == adapter, roster


def test_the_reserved_roles_are_not_workers() -> None:
    assert RESERVED_NON_WORKERS == frozenset({"codex", "claude", "claude-code"})


def test_the_reserved_roles_and_the_workers_do_not_overlap() -> None:
    assert not set(DEVELOPMENT_WORKERS) & set(RESERVED_NON_WORKERS)


@pytest.mark.parametrize("name", ["codex", "claude", "claude-code"])
def test_a_reserved_role_is_refused(name) -> None:
    with pytest.raises(NativeRuntimeError):
        worker_provider_id(name)


@pytest.mark.parametrize("name", ["foo", "mmx", "mimo", ""])
def test_an_unknown_name_is_refused(name) -> None:
    with pytest.raises(NativeRuntimeError):
        worker_provider_id(name)


def test_the_worker_name_is_matched_case_insensitively() -> None:
    assert worker_provider_id("Kimi") == "kimi"
    assert worker_provider_id("  MINIMAX  ") == "minimax"


def test_a_provider_named_by_the_environment_is_found(tmp_path, monkeypatch) -> None:
    binary = tmp_path / "fake-kimi"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("NATIVE_CLI_KIMI_BIN", str(binary))
    assert _discovered_binary("kimi") == str(binary)


def test_the_glm_worker_defaults_to_the_official_zcode_binary(tmp_path, monkeypatch) -> None:
    """The zcode Worker's default is the official app CLI, not clean-room."""
    official = tmp_path / "zcode"
    official.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    official.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    monkeypatch.delenv("NATIVE_CLI_ZCODE_BIN", raising=False)
    monkeypatch.delenv("NATIVE_CLI_ZHIPU_BIN", raising=False)
    assert _DEFAULT_BINARY["zcode"] == ("zcode",)
    found = _discovered_binary("zcode")
    assert found is not None
    assert Path(found).name == "zcode"
    # zhipu's entry is unchanged and still tries the clean-room name first.
    assert _DEFAULT_BINARY["zhipu"] == ("zcode-cli", "zcode")


def test_a_host_with_only_the_clean_room_cli_does_not_satisfy_the_zcode_default(
    tmp_path, monkeypatch
) -> None:
    """Clean-room is explicit-only: it is no longer an implicit GLM fallback."""
    clean_room = tmp_path / "zcode-cli"
    clean_room.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    clean_room.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    monkeypatch.delenv("NATIVE_CLI_ZCODE_BIN", raising=False)
    monkeypatch.delenv("NATIVE_CLI_ZHIPU_BIN", raising=False)
    assert _discovered_binary("zcode") is None
    # zhipu keeps the clean-room candidate, so its own lookup still finds it.
    assert Path(_discovered_binary("zhipu") or "").name == "zcode-cli"


def test_the_dsh_worker_defaults_to_the_official_dsh_binary() -> None:
    """DSH stays the official ``dsh``; there is no clean-room/alternate path."""
    assert _DEFAULT_BINARY["deepseek-dsh"] == ("dsh",)
    assert all(name != "zcode-cli" for name in _DEFAULT_BINARY["deepseek-dsh"])


def test_the_codex_and_claude_entries_are_unchanged() -> None:
    assert _DEFAULT_BINARY["codex"] == ("codex",)
    assert _DEFAULT_BINARY["claude-code"] == ("claude",)


def test_the_status_of_a_worker_provider_does_not_raise(monkeypatch) -> None:
    for roster in DEVELOPMENT_WORKERS:
        provider = worker_provider_id(roster)
        status = status_for(provider)
        assert status.provider == provider


def test_the_role_policy_and_the_worker_roster_agree() -> None:
    roles = pytest.importorskip("src.server.services.autonomy.roles")
    if not hasattr(roles, "runtime_provider_id"):
        pytest.skip("the role policy does not expose runtime_provider_id yet")
    for name, provider in DEVELOPMENT_WORKERS.items():
        assert roles.runtime_provider_id(name) == provider, name


def test_the_worker_roster_names_the_full_capability_glm_provider() -> None:
    """The roster uses "zcode"; "zhipu" only offers "cancel"."""
    assert DEVELOPMENT_WORKERS["glm"] == "zcode"
    assert capabilities_for("zcode") != capabilities_for("zhipu")
