"""The read-only adapter factory the cross-review runs on.

Every model of the dispatch pool is asked for a read-only adapter. Either it gets one
whose real invocation carries its CLI's read-only mode, or it is refused before a
process exists, and the dispatch pool's list of models that may not review is exactly
the set the registry refuses.
"""

from __future__ import annotations

import pytest

from src.llms.native_cli import (
    NativeCliError,
    ReadOnlyUnavailable,
    get_adapter,
    get_read_only_adapter,
    list_adapters,
)
from src.llms.native_cli.registry import _ADAPTERS, _READ_ONLY_MODES
from src.tools.auto_dispatch.pool import MODEL_POOL, REVIEW_INELIGIBLE

# Each marker is an argv run a read-only reviewer must contain, in this order. Claude's is
# not ``--permission-mode plan``: that mode is advice (a project hook, or a plan file
# outside the working directory, still gets through it). What bounds the reviewer is no
# tools at all (``--tools`` with an empty value), no user or project settings, and no MCP.
# StepFun's ``--no-tools`` leaves the project's own extension code, which runs when the
# folder is trusted, so its reviewer also drops extension discovery and project files.
READ_ONLY_ARGV = {
    "codex": [("--sandbox", "read-only")],
    "claude": [("--restricted",), ("--strict-mcp-config",), ("--tools", "")],
    "stepfun": [("-p", "--no-session", "--no-tools"), ("--no-extensions",), ("--no-approve",)],
    "doubao": [("+chat",)],
}
BINARY_ENV = {
    "NATIVE_CLI_CLAUDE_BIN": "/opt/fake/claude",
    "NATIVE_CLI_CODEX_BIN": "/opt/fake/codex",
    "NATIVE_CLI_DOUBAO_BIN": "/opt/fake/arkcli",
    "NATIVE_CLI_STEPFUN_BIN": "/opt/fake/step",
}
WRITE_WIDENING = ("--approve-for-me", "danger-full-access", "workspace-write", "auto", "--approve")


class _Started(Exception):
    """Raised by the fake process boundary once the invocation is captured."""


@pytest.fixture
def started(monkeypatch, tmp_path) -> list:
    """Capture the invocation an adapter would start instead of starting it."""
    invocations: list = []

    async def run_process(invocation, **_kwargs):
        invocations.append(invocation)
        raise _Started

    for module in ("claude", "codex", "doubao", "stepfun"):
        monkeypatch.setattr(f"src.llms.native_cli.{module}.run_process", run_process)
    for name, value in BINARY_ENV.items():
        monkeypatch.setenv(name, value)
    return invocations


@pytest.mark.parametrize("pool_name", ["claude", "deepseek"])
def test_only_the_read_only_factory_knows_the_pool_spellings(pool_name: str) -> None:
    # NativeAgent keys its MCP bridge, credentials and streaming on the registered name, so
    # an adapter it built under the pool's spelling would run with all three silently off.
    with pytest.raises(NativeCliError, match="unknown native CLI provider"):
        get_adapter(pool_name)
    # And an alias is a spelling, not a second adapter in the listing.
    assert pool_name not in [row["provider"] for row in list_adapters()]


@pytest.mark.parametrize("model", MODEL_POOL)
def test_every_pool_model_gets_a_read_only_adapter_or_a_named_refusal(model: str) -> None:
    if model in REVIEW_INELIGIBLE:
        with pytest.raises(ReadOnlyUnavailable, match="no read-only mode"):
            get_read_only_adapter(model, cwd="/tmp", timeout_seconds=5.0)
        return

    adapter = get_read_only_adapter(model, cwd="/tmp", timeout_seconds=5.0)

    assert type(adapter) in _ADAPTERS.values()
    assert type(adapter) in _READ_ONLY_MODES


@pytest.mark.asyncio
@pytest.mark.parametrize("model", MODEL_POOL)
async def test_the_invocation_a_reviewer_would_start_is_read_only(model, started) -> None:
    if model in REVIEW_INELIGIBLE:
        with pytest.raises(ReadOnlyUnavailable):
            get_read_only_adapter(model, cwd="/tmp", timeout_seconds=5.0)
        assert started == [], "a refused model never starts a process"
        return

    adapter = get_read_only_adapter(model, cwd="/tmp", timeout_seconds=5.0)
    with pytest.raises(_Started):
        await adapter.run("review this")

    (invocation,) = started
    argv = invocation.argv
    for marker in READ_ONLY_ARGV[model]:
        assert any(argv[i : i + len(marker)] == marker for i in range(len(argv))), (marker, argv)
    assert not set(WRITE_WIDENING) & set(argv)


@pytest.mark.asyncio
async def test_a_claude_reviewer_is_given_no_tool_to_write_or_run_a_hook_with(started) -> None:
    with pytest.raises(_Started):
        await get_read_only_adapter("claude", cwd="/tmp").run("review this")

    argv = started[0].argv
    # ``--tools ""`` is one empty argument, not a missing list: a bare ``--tools`` would
    # swallow the next flag as its value and leave the default tool set in place.
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--tools") + 2].startswith("--")


@pytest.mark.asyncio
async def test_a_stepfun_reviewer_loads_neither_extensions_nor_project_files(started) -> None:
    with pytest.raises(_Started):
        await get_read_only_adapter("stepfun", cwd="/tmp").run("review this")

    argv = started[0].argv
    assert "--no-extensions" in argv and "--no-approve" in argv
    assert "--approve" not in argv and "-a" not in argv


def test_the_registry_refuses_exactly_the_models_the_pool_says_may_not_review() -> None:
    refused = set()
    for model in MODEL_POOL:
        try:
            get_read_only_adapter(model)
        except ReadOnlyUnavailable:
            refused.add(model)
    assert refused == set(REVIEW_INELIGIBLE)


@pytest.mark.parametrize("spelling", ["step", "arkcli", "claude", "claude-code", "Claude-Code", " CODEX "])
def test_every_spelling_of_a_read_only_provider_shares_its_mode(spelling: str) -> None:
    assert type(get_read_only_adapter(spelling)) in _READ_ONLY_MODES


@pytest.mark.parametrize(
    "provider",
    ["glm", "zhipu", "kimi", "deepseek", "deepseek-dsh", "minimax", "mmx", "grok", "cursor", "mimo", "zcode"],
)
def test_an_adapter_nobody_declared_read_only_is_refused(provider: str) -> None:
    with pytest.raises(ReadOnlyUnavailable):
        get_read_only_adapter(provider)


def test_an_unknown_provider_is_still_an_unknown_provider() -> None:
    with pytest.raises(NativeCliError, match="unknown native CLI provider") as caught:
        get_read_only_adapter("manus")
    assert not isinstance(caught.value, ReadOnlyUnavailable)


@pytest.mark.parametrize(
    "widening",
    [{"read_only": False}, {"approve_for_me": True}, {"outer_sandboxed": True}, {"permission_mode": "yolo"}],
)
def test_a_caller_cannot_widen_what_the_factory_returns(widening) -> None:
    with pytest.raises(TypeError):
        get_read_only_adapter("codex", **widening)
