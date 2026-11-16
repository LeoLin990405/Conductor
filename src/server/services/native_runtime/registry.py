"""Native runtime registry: the honest capability table over CLI adapters.

This layer does not download or authenticate CLIs. It only wraps adapters that
already exist in ``src.llms.native_cli`` in the runtime contract, reports what
the host actually has, and marks which capabilities the adapter can honestly
provide today.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, AsyncIterator, Type

from src.llms.native_cli import NativeCliError, NativeCliUnavailable
from src.llms.native_cli.claude import ClaudeCodeAdapter
from src.llms.native_cli.codex import CodexExecAdapter
from src.llms.native_cli.cursor import CursorAgentAdapter
from src.llms.native_cli.deepseek import DeepSeekDshAdapter
from src.llms.native_cli.doubao import DoubaoArkCliAdapter
from src.llms.native_cli.grok import GrokCliAdapter
from src.llms.native_cli.kimi import KimiCliAdapter
from src.llms.native_cli.mimo import MiMoCodeAdapter
from src.llms.native_cli.opencode import OpenCodeAdapter
from src.llms.native_cli.minimax import MiniMaxCodeAdapter
from src.llms.native_cli.stepfun import StepFunAdapter
from src.llms.native_cli.zcode import ZCodeCliAdapter
from src.llms.native_cli.zhipu import ZhipuCliAdapter

from src.server.services.native_runtime.codex_app_server import CodexAppServerRuntime
from src.server.services.native_runtime.codex_stream import stream_codex_turn
from src.server.services.native_runtime.dsh_stream import stream_dsh_turn
from src.server.services.native_runtime.grok_stream import stream_grok_turn
from src.server.services.native_runtime.stepfun_stream import stream_stepfun_turn
from src.server.services.native_runtime.minimax_stream import stream_minimax_turn
from src.server.services.native_runtime.zcode_stream import stream_zcode_turn
from src.server.services.native_runtime.contracts import (
    NativeCapabilities,
    NativeEvent,
    NativeRuntime,
    NativeRuntimeError,
)

# Adapter capabilities describe the transport, not a live account. A true
# session/cache capability means the adapter preserves and reports it; live
# verification is a separate flag below.
_ADAPTERS: dict[str, tuple[Type[Any], NativeCapabilities, bool]] = {
    "deepseek-dsh": (
        DeepSeekDshAdapter,
        NativeCapabilities(
            streaming=True,
            tool_events=True,
            session_resume=True,
            cache_usage=True,
            workspace_files=True,
            mcp=True,
            memory=True,
            skills=True,
            cancel=True,
        ),
        True,
    ),
    "codex": (
        CodexExecAdapter,
        NativeCapabilities(
            streaming=True,
            tool_events=True,
            session_resume=True,
            cache_usage=True,
            approvals=True,
            workspace_files=True,
            mcp=True,
            memory=True,
            skills=True,
            cancel=True,
        ),
        True,
    ),
    "claude-code": (
        ClaudeCodeAdapter,
        NativeCapabilities(
            session_resume=True,
            cache_usage=True,
            workspace_files=True,
            cancel=True,
        ),
        True,
    ),
    "cursor": (
        CursorAgentAdapter,
        NativeCapabilities(
            session_resume=True,
            workspace_files=True,
            cancel=True,
        ),
        True,
    ),
    # gemini and qwen are PTC-only: their CLI/account policies do not suit a
    # delegated native lane.
    "kimi": (
        KimiCliAdapter,
        NativeCapabilities(session_resume=True, workspace_files=True),
        True,
    ),
    "grok": (
        GrokCliAdapter,
        NativeCapabilities(
            streaming=True,
            tool_events=True,
            session_resume=True,
            cache_usage=True,
            workspace_files=True,
            cancel=True,
        ),
        True,
    ),
    "stepfun": (
        StepFunAdapter,
        NativeCapabilities(
            streaming=True,
            tool_events=True,
            session_resume=True,
            workspace_files=True,
            cancel=True,
        ),
        True,
    ),
    "zcode": (
        ZCodeCliAdapter,
        NativeCapabilities(
            session_resume=True,
            cache_usage=True,
            workspace_files=True,
            streaming=True,
            tool_events=True,
            mcp=True,
            memory=True,
            skills=True,
            cancel=True,
        ),
        True,
    ),
    "zhipu": (
        ZhipuCliAdapter,
        NativeCapabilities(cancel=True),
        True,
    ),
    "minimax": (
        MiniMaxCodeAdapter,
        NativeCapabilities(
            streaming=True,
            tool_events=True,
            session_resume=True,
            workspace_files=True,
            mcp=True,
            memory=True,
            skills=True,
            cancel=True,
        ),
        True,
    ),
    "mimo": (
        MiMoCodeAdapter,
        NativeCapabilities(
            session_resume=True,
            cache_usage=True,
            workspace_files=True,
            mcp=True,
            memory=True,
            skills=True,
            cancel=True,
        ),
        True,
    ),
    "opencode": (
        OpenCodeAdapter,
        NativeCapabilities(
            session_resume=True,
            cache_usage=True,
            workspace_files=True,
            cancel=True,
        ),
        True,
    ),
    "doubao": (
        DoubaoArkCliAdapter,
        NativeCapabilities(cache_usage=True, cancel=True),
        True,
    ),
}

_BINARY_ENV = {
    "deepseek-dsh": "NATIVE_CLI_DSH_BIN",
    "codex": "NATIVE_CLI_CODEX_BIN",
    "claude-code": "NATIVE_CLI_CLAUDE_BIN",
    "cursor": "NATIVE_CLI_CURSOR_BIN",
    "kimi": "NATIVE_CLI_KIMI_BIN",
    "grok": "NATIVE_CLI_GROK_BIN",
    "stepfun": "NATIVE_CLI_STEPFUN_BIN",
    "zcode": "NATIVE_CLI_ZCODE_BIN",
    "zhipu": "NATIVE_CLI_ZHIPU_BIN",
    "minimax": "NATIVE_CLI_MINIMAX_BIN",
    "mimo": "NATIVE_CLI_MIMO_BIN",
    "opencode": "NATIVE_CLI_OPENCODE_BIN",
    "doubao": "NATIVE_CLI_DOUBAO_BIN",
}

# The binary names a provider is looked up under, tried in order. The zcode
# Worker's default is the official ZCode app CLI ("zcode"); the clean-room
# ``zcode-cli`` speaks a different grammar and is selected only through an
# explicit override (NATIVE_CLI_ZCODE_BIN), never by this lookup. The zhipu
# entry keeps the clean-room name first for the lanes that still name it.
_DEFAULT_BINARY: Mapping[str, tuple[str, ...]] = {
    "deepseek-dsh": ("dsh",),
    "codex": ("codex",),
    "claude-code": ("claude",),
    "cursor": ("cursor-agent",),
    "kimi": ("kimi",),
    "grok": ("grok",),
    "stepfun": ("step",),
    "zcode": ("zcode",),
    "zhipu": ("zcode-cli", "zcode"),
    "minimax": ("mcode",),
    "mimo": ("mimo",),
    "opencode": ("opencode",),
    "doubao": ("arkcli",),
}

# The nine development Workers, by the name the dispatch roster uses, mapped
# to the native runtime key that serves them. The mapping is one-way and
# read-only: a Worker name is a dispatch concept, a runtime key is a registry
# concept, and nothing here invents a provider.
#
# GLM is "zcode" rather than "zhipu": both are the same CLI, but the zhipu
# adapter only offers "cancel", while a Worker needs the full capability set.
# The zhipu entry stays in the registry for the lanes that still name it.
DEVELOPMENT_WORKERS: Mapping[str, str] = MappingProxyType(
    {
        "kimi": "kimi",
        "glm": "zcode",
        "grok": "grok",
        "cursor": "cursor",
        "stepfun": "stepfun",
        "deepseek": "deepseek-dsh",
        "minimax": "minimax",
        "doubao": "doubao",
        "opencode": "opencode",
    }
)

# Roles that decide or judge, and never write code. They are not Workers and
# must never be started as one.
RESERVED_NON_WORKERS = frozenset({"codex", "claude", "claude-code"})


def native_runtime_ids() -> frozenset[str]:
    """Every runtime id a thread can be bound to.

    Two ids can serve one CLI (``zcode`` is the Worker with the full capability
    set, ``zhipu`` the cancel-only lane), so a caller that folds aliases onto a
    single identity -- routing, operator policy -- must be able to ask which
    spellings are already a concrete id and keep them apart.
    """
    return frozenset(_ADAPTERS)


def worker_provider_id(name: str) -> str:
    """The native runtime key for a development Worker name.

    Raises ``NativeRuntimeError`` for a name that is not a Worker, so a typo
    or a reserved role is refused at the call site instead of being launched.
    """
    key = (name or "").strip().lower()
    provider = DEVELOPMENT_WORKERS.get(key)
    if provider is None:
        if key in RESERVED_NON_WORKERS:
            raise NativeRuntimeError(
                f"{name!r} is a reserved role, not a development Worker; "
                f"available: {', '.join(sorted(DEVELOPMENT_WORKERS))}"
            )
        raise NativeRuntimeError(
            f"unknown development Worker {name!r}; available: "
            f"{', '.join(sorted(DEVELOPMENT_WORKERS))}"
        )
    return provider


@dataclass(frozen=True)
class NativeRuntimeStatus:
    provider: str
    installed: bool
    binary: str | None
    live_verified: bool
    capabilities: NativeCapabilities
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "installed": self.installed,
            "binary": self.binary,
            "live_verified": self.live_verified,
            "capabilities": self.capabilities.as_dict(),
            "error": self.error,
        }


def provider_ids() -> tuple[str, ...]:
    return tuple(_ADAPTERS)


def capabilities_for(provider: str) -> NativeCapabilities:
    entry = _ADAPTERS.get(provider)
    if entry is None:
        raise NativeRuntimeError(
            f"unknown native provider {provider!r}; available: "
            f"{', '.join(sorted(_ADAPTERS))}"
        )
    return entry[1]


def _discovered_binary(provider: str, explicit: str | None = None) -> str | None:
    env_name = _BINARY_ENV.get(provider)
    configured = explicit or (env_name and os.environ.get(env_name))
    if configured:
        return str(configured)
    for candidate in _DEFAULT_BINARY.get(provider, ()):
        found = shutil.which(candidate)
        if found:
            return found
    return None


_ACP_CAPABILITIES = NativeCapabilities(
    streaming=True,
    tool_events=True,
    session_resume=True,
    approvals=True,
    mcp=True,
    memory=True,
    skills=True,
    workspace_files=True,
    cancel=True,
)


def _acp_enabled(provider: str) -> bool:
    raw = os.environ.get("NATIVE_CLI_ACP", "")
    names = {name.strip() for name in raw.split(",") if name.strip()}
    return provider in names


def _disabled_providers() -> set[str]:
    from src.config.settings import get_native_runtime_routing_config

    aliases = {"glm": "zcode", "deepseek": "deepseek-dsh", "claude": "claude-code"}
    return {
        aliases.get(str(provider).strip().lower(), str(provider).strip().lower())
        for provider in get_native_runtime_routing_config().disabled_providers
        if str(provider).strip()
    }


def disabled_provider_reason(provider: str) -> str | None:
    canonical = {
        "glm": "zcode",
        "deepseek": "deepseek-dsh",
        "claude": "claude-code",
    }.get(provider.strip().lower(), provider.strip().lower())
    if canonical not in _disabled_providers():
        return None
    return "disabled by operator policy"


def _zcode_clean_room(*, adapter_binary: str | None) -> bool:
    from src.llms.native_cli.zcode import _is_clean_room_cli

    return bool(adapter_binary) and _is_clean_room_cli(adapter_binary)


def _without_desktop_capabilities(caps: NativeCapabilities) -> NativeCapabilities:
    """The clean-room zcode-cli has no MCP bridge and no permission broker."""
    return replace(caps, mcp=False, memory=False, skills=False, approvals=False)


def status_for(provider: str) -> NativeRuntimeStatus:
    provider = {"glm": "zcode"}.get(provider, provider)
    cls, caps, live = _ADAPTERS.get(provider, (None, NativeCapabilities(), False))  # type: ignore[arg-type]
    if cls is None:
        raise NativeRuntimeError(
            f"unknown native provider {provider!r}; available: "
            f"{', '.join(sorted(_ADAPTERS))}"
        )
    binary: str | None = None
    error: str | None = None
    try:
        adapter = cls()
        binary = adapter.binary
    except NativeCliUnavailable as exc:
        error = str(exc)
    except Exception as exc:  # a bad explicit path must not hide in "not installed"
        error = f"{type(exc).__name__}: {exc}"
    disabled_providers = _disabled_providers()
    if provider in disabled_providers:
        reason = "disabled by operator policy"
        error = f"{error}; {reason}" if error else reason
        live = False
    if provider != "zhipu" and caps and _acp_enabled(provider):
        caps = _ACP_CAPABILITIES
    if (
        provider == "stepfun"
        and caps
        and os.environ.get("NATIVE_CLI_STEPFUN_APPROVALS", "").strip().lower()
        in {"1", "true", "yes", "on"}
    ):
        caps = NativeCapabilities(
            streaming=True,
            tool_events=True,
            session_resume=True,
            approvals=True,
            workspace_files=True,
            cancel=True,
        )
    if provider == "zcode" and caps and _zcode_clean_room(adapter_binary=binary):
        caps = _without_desktop_capabilities(caps)
    elif (
        provider == "zcode"
        and caps
        and os.environ.get("NATIVE_CLI_ZCODE_APPROVALS", "").strip().lower()
        in {"1", "true", "yes", "on"}
    ):
        caps = NativeCapabilities(
            streaming=True,
            tool_events=True,
            session_resume=True,
            approvals=True,
            cache_usage=True,
            mcp=True,
            memory=True,
            skills=True,
            workspace_files=True,
            cancel=True,
        )
    return NativeRuntimeStatus(
        provider=provider,
        installed=binary is not None,
        binary=binary,
        live_verified=live,
        capabilities=caps,
        error=error,
    )


def list_statuses() -> list[NativeRuntimeStatus]:
    return [status_for(provider) for provider in sorted(_ADAPTERS)]


class AdapterNativeRuntime:
    """One-shot runtime over an existing CLI adapter.

    This is the fallback transport. It preserves native session/cache through
    the adapter, but yields the completed answer as one event because the
    adapter's current contract does not expose a live stream. It never wraps
    the CLI in Conductor's prompt or tool loop.
    """

    provider: str

    def __init__(self, provider: str, *, adapter: Any | None = None, **kwargs: Any) -> None:
        entry = _ADAPTERS.get(provider)
        if entry is None:
            raise NativeRuntimeError(f"unknown native provider {provider!r}")
        self.provider = provider
        self._capabilities = entry[1]
        self._adapter = adapter or entry[0](**kwargs)
        self._turn_task: asyncio.Task[Any] | None = None

    def capabilities(self) -> NativeCapabilities:
        if self.provider == "zcode" and getattr(self._adapter, "clean_room", False):
            return _without_desktop_capabilities(self._capabilities)
        return self._capabilities

    async def stream_turn(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | None = None,
    ) -> AsyncIterator[NativeEvent]:
        try:
            if self.provider in {"codex", "deepseek-dsh", "minimax", "zcode", "grok", "stepfun"} and hasattr(
                self._adapter, "build_invocation"
            ):
                stream = {
                    "codex": stream_codex_turn,
                    "deepseek-dsh": stream_dsh_turn,
                    "minimax": stream_minimax_turn,
                    "zcode": stream_zcode_turn,
                    "grok": stream_grok_turn,
                    "stepfun": stream_stepfun_turn,
                }[self.provider]
                async for event in stream(
                    self._adapter, prompt, session_id=session_id, cwd=cwd
                ):
                    yield event
                return
            self._turn_task = asyncio.current_task()
            result = await self._adapter.run(prompt, session_id=session_id, cwd=cwd)
        except NativeCliError as exc:
            yield NativeEvent(kind="error", text=str(exc))
            return
        yield NativeEvent(
            kind="text_delta",
            text=result.text,
            session_id=result.session_id,
        )
        yield NativeEvent(
            kind="usage",
            session_id=result.session_id,
            data={
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "cache_read_tokens": result.usage.cache_read_tokens,
                "cache_write_tokens": result.usage.cache_write_tokens,
                "total_tokens": result.usage.total_tokens,
            },
        )
        yield NativeEvent(
            kind="turn_completed",
            session_id=result.session_id,
            data={"events": result.events},
        )

    async def cancel(self) -> None:
        # One-shot adapters have no process handle. Cancelling the running
        # task unwinds their ``run_process``, whose BaseException path kills
        # the CLI process group.
        task = self._turn_task
        self._turn_task = None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()


def get_runtime(provider: str, **kwargs: Any) -> NativeRuntime:
    provider = {"glm": "zcode"}.get(provider, provider)
    if provider not in _ADAPTERS:
        raise NativeRuntimeError(
            f"unknown native provider {provider!r}; available: "
            f"{', '.join(sorted(_ADAPTERS))}"
        )
    reason = disabled_provider_reason(provider)
    if reason:
        raise NativeRuntimeError(f"native provider {provider!r} is unavailable: {reason}")
    transport = kwargs.pop("transport", None)
    if transport == "stepfun-rpc":
        from src.server.services.native_runtime.stepfun_rpc import StepFunRpcRuntime

        return StepFunRpcRuntime(**kwargs)
    if transport == "zcode-approval":
        from src.server.services.native_runtime.zcode_approval import ZCodeApprovalRuntime

        return ZCodeApprovalRuntime(**kwargs)
    if transport == "acp":
        from src.server.services.native_runtime.acp_runtime import AcpNativeRuntime

        return AcpNativeRuntime(provider=provider, **kwargs)
    if provider == "codex" and transport == "app-server":
        return CodexAppServerRuntime(**kwargs)
    return AdapterNativeRuntime(provider, **kwargs)
