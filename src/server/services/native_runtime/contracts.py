"""The contract between an Conductor thread and a vendor-native agent runtime.

A native runtime is not a tool call: it owns one turn's prompt, tool loop,
session history and prompt cache. Conductor supplies the thread transport,
workspace binding, persistence projection and UI events; it must not rebuild
the CLI's conversation or silently inject Conductor prompts into every turn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal, Mapping, Protocol, runtime_checkable

#: Thread metadata key. The value is a ``native_runtime`` object; existing
#: threads without it stay on the PTC runtime.
NATIVE_RUNTIME_META_KEY = "native_runtime"

#: Native vendor streams expose completed items, not token deltas. Conductor still
#: delivers a completed item progressively instead of one large frame; the UI
#: can render it as it arrives. Keep the chunk small enough to look live, large
#: enough that a long answer does not create hundreds of SSE rows.
_NATIVE_TEXT_CHUNK = 32

NativeEventKind = Literal[
    "session_started",
    "text_delta",
    "thinking_delta",
    "tool_started",
    "tool_result",
    "approval_requested",
    "usage",
    "turn_completed",
    "error",
]


class NativeRuntimeError(RuntimeError):
    """A native runtime could not start or failed one turn."""


class NativeRuntimeUnavailable(NativeRuntimeError):
    """The provider CLI is not installed or is not configured on this host."""


@dataclass(frozen=True)
class NativeRuntimeSpec:
    """The immutable runtime binding stored on a thread.

    ``session_id`` is the CLI's native session, not an Conductor thread. It is
    updated after each turn by the runtime that produced it. ``cwd`` is the
    workspace directory the CLI owns; it must stay stable across turns to keep
    the provider prompt cache warm.
    """

    provider: str
    session_id: str | None = None
    cwd: str | None = None
    model: str | None = None
    version: int = 1

    def to_metadata(self) -> dict[str, Any]:
        return {
            NATIVE_RUNTIME_META_KEY: {
                "provider": self.provider,
                "session_id": self.session_id,
                "cwd": self.cwd,
                "model": self.model,
                "version": self.version,
            }
        }

    @classmethod
    def from_thread_metadata(cls, metadata: Mapping[str, Any] | None) -> "NativeRuntimeSpec | None":
        raw = (metadata or {}).get(NATIVE_RUNTIME_META_KEY)
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise NativeRuntimeError("thread native_runtime metadata is not an object")
        provider = str(raw.get("provider") or "").strip()
        if not provider:
            raise NativeRuntimeError("thread native_runtime metadata has no provider")
        return cls(
            provider=provider,
            session_id=str(raw["session_id"]) if raw.get("session_id") else None,
            cwd=str(raw["cwd"]) if raw.get("cwd") else None,
            model=str(raw["model"]) if raw.get("model") else None,
            version=int(raw.get("version") or 1),
        )

    def with_session(self, session_id: str | None) -> "NativeRuntimeSpec":
        return NativeRuntimeSpec(
            provider=self.provider,
            session_id=session_id,
            cwd=self.cwd,
            model=self.model,
            version=self.version,
        )


@dataclass(frozen=True)
class NativeCapabilities:
    """What one backend can actually do.

    The product may not claim a capability the CLI does not expose. A capability
    that is false must be rejected in the UI/API, not silently emulated.
    """

    streaming: bool = False
    tool_events: bool = False
    session_resume: bool = False
    cache_usage: bool = False
    approvals: bool = False
    mcp: bool = False
    memory: bool = False
    skills: bool = False
    workspace_files: bool = False
    cancel: bool = False

    def as_dict(self) -> dict[str, bool]:
        return {
            "streaming": self.streaming,
            "tool_events": self.tool_events,
            "session_resume": self.session_resume,
            "cache_usage": self.cache_usage,
            "approvals": self.approvals,
            "mcp": self.mcp,
            "memory": self.memory,
            "skills": self.skills,
            "workspace_files": self.workspace_files,
            "cancel": self.cancel,
        }


@dataclass(frozen=True)
class NativeEvent:
    """One normalized event from a native turn.

    Adapters translate provider-specific JSON/NDJSON into these; the chat
    transport translates these into the existing SSE contract. Raw provider
    payloads stay in ``raw`` for audit/debugging but are never required by the
    normalizer.
    """

    kind: NativeEventKind
    text: str = ""
    session_id: str | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)
    raw: Mapping[str, Any] | None = None


def chunk_text(text: str, size: int = _NATIVE_TEXT_CHUNK) -> list[str]:
    """Split a completed vendor message for progressive delivery.

    This is deliberately labelled *chunked*, not token-level: the vendor
    supplied the whole message in one event. It still gives the UI useful
    progressive chunks without inventing token boundaries the provider never
    exposed.
    """
    if not text:
        return []
    step = max(1, int(size))
    return [text[index : index + step] for index in range(0, len(text), step)]


@runtime_checkable
class NativeRuntime(Protocol):
    """One provider's turn executor.

    ``stream_turn`` must yield the final answer before ``turn_completed``. The
    CLI, not Conductor, owns intermediate tool calls and session history.
    """

    provider: str

    def capabilities(self) -> NativeCapabilities: ...

    async def stream_turn(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | None = None,
    ) -> AsyncIterator[NativeEvent]: ...

    async def cancel(self) -> None: ...
