"""Reading and updating a thread's native-runtime binding."""

from __future__ import annotations

from typing import Any, Mapping

from src.server.services.native_runtime.contracts import (
    NATIVE_RUNTIME_META_KEY,
    NativeRuntimeError,
    NativeRuntimeSpec,
)
from src.utils.provider_roles import provider_role


def read_spec(thread: Mapping[str, Any] | None) -> NativeRuntimeSpec | None:
    """The binding a thread row carries, or None for the PTC runtime."""
    if not thread:
        return None
    return NativeRuntimeSpec.from_thread_metadata(thread.get("metadata"))


def metadata_for(provider: str, *, cwd: str | None = None) -> dict[str, Any]:
    """The initial ``metadata`` fragment for a newly created native thread."""
    provider = (provider or "").strip()
    if not provider:
        raise NativeRuntimeError("native provider is required")
    if provider_role(provider) != "native":
        raise NativeRuntimeError(
            f"provider {provider!r} is {provider_role(provider)}-only and cannot bind a native thread"
        )
    return NativeRuntimeSpec(provider=provider, cwd=cwd).to_metadata()


def with_session_metadata(
    metadata: Mapping[str, Any] | None, spec: NativeRuntimeSpec
) -> dict[str, Any]:
    """Return the whole metadata object with the runtime's latest session id.

    Only the native-runtime key is replaced; origin and other annotations are
    preserved.
    """
    merged = dict(metadata or {})
    merged.update(spec.to_metadata())
    return merged


def assert_ptc_thread(thread: Mapping[str, Any] | None) -> None:
    """Refuse a native thread on a PTC-only path with a typed error."""
    spec = read_spec(thread)
    if spec is not None:
        raise NativeRuntimeError(
            f"thread is bound to native runtime {spec.provider!r}; "
            "it cannot run through the PTC workflow"
        )


def has_native_binding(metadata: Mapping[str, Any] | None) -> bool:
    return bool((metadata or {}).get(NATIVE_RUNTIME_META_KEY))
