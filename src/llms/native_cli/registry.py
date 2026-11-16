"""Adapter lookup and provider aliases for native CLI-backed turns."""

from __future__ import annotations

from collections.abc import Mapping
from functools import partial
from typing import Any

from src.config.compat import load_compat_workers
from src.llms.native_cli.base import NativeCliError, ReadOnlyUnavailable
from src.llms.native_cli.claude import ClaudeCodeAdapter
from src.llms.native_cli.codex import CodexExecAdapter
from src.llms.native_cli.compat import CompatDshAdapter
from src.llms.native_cli.cursor import CursorAgentAdapter
from src.llms.native_cli.deepseek import DeepSeekDshAdapter
from src.llms.native_cli.doubao import DoubaoArkCliAdapter
from src.llms.native_cli.grok import GrokCliAdapter
from src.llms.native_cli.kimi import KimiCliAdapter
from src.llms.native_cli.mimo import MiMoCodeAdapter
from src.llms.native_cli.opencode import OpenCodeAdapter
from src.llms.native_cli.minimax import MiniMaxCodeAdapter
from src.llms.native_cli.minimax_mmx import MiniMaxMmxAdapter
from src.llms.native_cli.stepfun import StepFunAdapter
from src.llms.native_cli.zcode import ZCodeCliAdapter
from src.llms.native_cli.zhipu import ZhipuCliAdapter

# Gemini and Qwen remain PTC-only: their CLI/account policies do not suit a
# delegated native lane. Text-only transports stay available to the adapter
# tool but are not registered as native runtimes.
_ADAPTERS = {
    "deepseek-dsh": DeepSeekDshAdapter,
    "doubao": DoubaoArkCliAdapter,
    "arkcli": DoubaoArkCliAdapter,
    "mmx": MiniMaxMmxAdapter,
    "minimax-mmx": MiniMaxMmxAdapter,
    "codex": CodexExecAdapter,
    "claude-code": ClaudeCodeAdapter,
    "cursor": CursorAgentAdapter,
    "kimi": KimiCliAdapter,
    "grok": GrokCliAdapter,
    "stepfun": StepFunAdapter,
    "step": StepFunAdapter,
    "minimax": MiniMaxCodeAdapter,
    "mimo": MiMoCodeAdapter,
    "opencode": OpenCodeAdapter,
    "zcode": ZCodeCliAdapter,
    "glm": ZCodeCliAdapter,
    "zhipu": ZhipuCliAdapter,
}


# The dispatch pool says "claude" and "deepseek" where this registry says "claude-code" and
# "deepseek-dsh". Only a read-only turn takes the pool's spelling: ``get_adapter`` keeps
# refusing it, because NativeAgent keys its MCP bridge, credentials and streaming on the
# registered name, and an adapter built under another spelling would run with all three
# silently off. Nor are they in ``_ADAPTERS``, so ``list_adapters`` reports each CLI once.
_POOL_NAMES = {"claude": "claude-code", "deepseek": "deepseek-dsh"}

# The constructor arguments that put an adapter in a mode the CLI itself enforces, keyed by
# class so every spelling of a provider (step/stepfun, arkcli/doubao) shares one entry.
# What enforces each: Codex's is an OS sandbox; a fresh StepFun turn starts the CLI with no
# tools, extensions or project files; Claude Code's is no tools, no user or project
# settings (so no hooks) and no MCP; Doubao's reply is text because the adapter passes
# ``arkcli +chat`` no ``--tools``.
#
# Claude's ``--permission-mode plan`` is not what makes it read-only: plan mode is advice
# the model follows, and a hook in the project's own ``.claude/settings.json`` runs under it,
# as does a plan file the CLI writes outside the working directory. The adapter's isolated
# mode is what takes those away.
#
# An adapter missing here is refused: a CLI nobody checked is not read-only. Why GLM, Kimi,
# DSH and MiniMax Code are refused is recorded beside the dispatch pool
# (``REVIEW_INELIGIBLE``), which a test keeps equal to what this table refuses.
_READ_ONLY_MODES: dict[type, Mapping[str, Any]] = {
    # A reviewer also runs without the operator's personal memory, global
    # AGENTS.md and config MCP servers (src/llms/native_cli/personal_memory.py).
    CodexExecAdapter: {"read_only": True, "isolate_personal_memory": True},
    ClaudeCodeAdapter: {"isolated": True},
    StepFunAdapter: {"read_only": True},
    DoubaoArkCliAdapter: {},
}


def _compat_adapter_factories() -> dict[str, Any]:
    workers = load_compat_workers(reserved_names=tuple(_ADAPTERS) + tuple(_POOL_NAMES))
    return {worker.name: partial(CompatDshAdapter, worker) for worker in workers}


def _merged_adapters() -> dict[str, Any]:
    return {**_ADAPTERS, **_compat_adapter_factories()}


# The context window each provider's CLI will hit, in tokens. A worker session
# grows with every turn, and a session that reaches the provider's ceiling stops
# being useful: the CLI answers with a context error or with an empty
# conversation. The worker-health session manager reads this table to rotate a
# session before that happens (80% of the window).
#
# ``None`` (a provider that is simply absent here) means "not configured", and a
# provider without a window only rotates passively, when a failure is classified
# as ``context_exhausted``.
#
# Each number is the *smallest* window the provider may enforce, not its largest:
# rotating a little early costs one extra session, rotating too late costs the
# whole conversation. The sources, one per line:
#
# * kimi       262144  — the Kimi Code CLI's own catalogue (``kimi provider list
#                        --json`` reports ``maxContextSize`` 1,048,576 for
#                        k3/k3.1-pro/kimi-for-coding and 262,144 for k3-256k and
#                        kimi-for-coding-highspeed), and the Plus membership tier
#                        caps k3.1-pro/k3 at 262,144 too (docs/PLANS-RESEARCH.md).
#                        The repo records the same 262,144 for the coding lane
#                        (tests/unit/llms/test_kimi_native_parity.py).
# * deepseek   1000000 — the DSH harness' own constant is 1,048,576 and Conductor's
#                        manifest records 1,000,000 (docs/DEEPSEEK-ADAPTATION.md);
#                        the smaller of the two keeps the 80% mark below the
#                        harness' ceiling.
# * doubao     128000  — the ARK Plan route's recorded value, which the repo
#                        keeps until official evidence says otherwise
#                        (docs/DOUBAO-ADAPTATION.md).
# * minimax    1000000 — MiniMax Code drives MiniMax-M3, documented at 1M context
#                        (docs/MINIMAX-ADAPTATION.md).
# * glm        1000000 — the GLM and ZCode adapters both default to GLM-5.3,
#                        documented at 1M context (docs/GLM-ADAPTATION.md).
# * mimo       1048576 — MiMo v2.6 flash/pro (docs/MIMO-ADAPTATION.md).
# * stepfun    262144  — the Step Plan catalogue spans 262,144 (step-router-v1,
#                        step-3.x) to 1,024,000 (step-5-preview); the floor is used
#                        (docs/STEPFUN-ADAPTATION.md).
# * grok       500000  — the OAuth lane's grok-4.6/4.7 rows
#                        (src/llms/manifest/models.json).
# * codex      272000  — the Codex CLI's own model cache reports 272,000 for most
#                        of its models and 500,000 for gpt-6.1-sol; the floor is used.
# * claude     1000000 — the OAuth lane's claude rows
#                        (src/llms/manifest/models.json).
#
# Not configured on purpose: ``cursor`` (neither the repo nor the CLI states a
# window) and ``mmx``/``minimax-mmx`` (the mmx CLI names no model of its own).
#
# Keyed by class, like ``_READ_ONLY_MODES``: every spelling of a provider
# (step/stepfun, arkcli/doubao, glm/zhipu/zcode) shares one entry.
_CONTEXT_WINDOWS: dict[type, int] = {
    KimiCliAdapter: 262_144,
    DeepSeekDshAdapter: 1_000_000,
    DoubaoArkCliAdapter: 128_000,
    MiniMaxCodeAdapter: 1_000_000,
    ZhipuCliAdapter: 1_000_000,
    ZCodeCliAdapter: 1_000_000,
    MiMoCodeAdapter: 1_048_576,
    StepFunAdapter: 262_144,
    GrokCliAdapter: 500_000,
    CodexExecAdapter: 272_000,
    ClaudeCodeAdapter: 1_000_000,
}


def _adapter_class(provider: str) -> Any:
    key = (provider or "").strip().lower()
    adapters = _merged_adapters()
    adapter = adapters.get(key)
    if adapter is None:
        available = ", ".join(sorted(adapters))
        raise NativeCliError(
            f"unknown native CLI provider {provider!r}; available: {available}"
        )
    return adapter


def get_adapter(provider: str, **kwargs):
    return _adapter_class(provider)(**kwargs)


def context_window_tokens(provider: str) -> int | None:
    """The provider's context window in tokens, or None when it is not configured.

    An unknown provider is not an error here: the caller decides what an
    unconfigured window means (the worker-health session manager treats it as
    "rotate only when a failure says the context is exhausted"). The pool's
    spellings resolve the same way ``get_read_only_adapter`` resolves them, so
    a caller holding ``claude`` or ``deepseek`` gets the same answer as one
    holding ``claude-code`` or ``deepseek-dsh``.
    """
    key = (provider or "").strip().lower()
    adapter = _ADAPTERS.get(_POOL_NAMES.get(key, key))
    if adapter is None:
        return None
    return _CONTEXT_WINDOWS.get(adapter)


def get_read_only_adapter(
    provider: str, *, cwd: str | None = None, timeout_seconds: float | None = None
):
    """Build ``provider``'s adapter in its read-only mode, or refuse with ReadOnlyUnavailable.

    There is no ``**kwargs`` on purpose: a caller that could pass ``read_only=False`` or
    ``approve_for_me=True`` could undo the mode this function exists to guarantee.
    """
    key = (provider or "").strip().lower()
    adapter = _adapter_class(_POOL_NAMES.get(key, key))
    mode = _READ_ONLY_MODES.get(adapter)
    if mode is None:
        raise ReadOnlyUnavailable(f"{provider!r} has no read-only mode")
    kwargs: dict[str, Any] = dict(mode)
    if cwd is not None:
        kwargs["cwd"] = cwd
    if timeout_seconds is not None:
        kwargs["timeout_seconds"] = timeout_seconds
    return adapter(**kwargs)


def list_adapters() -> list[dict[str, str]]:
    rows = [
        {"provider": name, "class": cls.__name__} for name, cls in _ADAPTERS.items()
    ]
    rows.extend(
        {"provider": name, "class": CompatDshAdapter.__name__, "kind": "compat"}
        for name in _compat_adapter_factories()
    )
    return sorted(rows, key=lambda row: row["provider"])
