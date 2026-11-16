"""Native agent runtime contracts and selection."""

from src.server.services.native_runtime.contracts import (
    NATIVE_RUNTIME_META_KEY,
    NativeCapabilities,
    NativeEvent,
    NativeEventKind,
    NativeRuntime,
    NativeRuntimeError,
    NativeRuntimeSpec,
    NativeRuntimeUnavailable,
)
from src.server.services.native_runtime.registry import (
    AdapterNativeRuntime,
    NativeRuntimeStatus,
    capabilities_for,
    get_runtime,
    list_statuses,
    provider_ids,
    status_for,
)
from src.server.services.native_runtime.spec import (
    assert_ptc_thread,
    has_native_binding,
    metadata_for,
    read_spec,
    with_session_metadata,
)

__all__ = [
    "NATIVE_RUNTIME_META_KEY",
    "AdapterNativeRuntime",
    "NativeCapabilities",
    "NativeEvent",
    "NativeEventKind",
    "NativeRuntime",
    "NativeRuntimeError",
    "NativeRuntimeSpec",
    "NativeRuntimeStatus",
    "NativeRuntimeUnavailable",
    "assert_ptc_thread",
    "capabilities_for",
    "get_runtime",
    "has_native_binding",
    "list_statuses",
    "metadata_for",
    "provider_ids",
    "read_spec",
    "status_for",
    "with_session_metadata",
]
