"""Filesystem backends behind the agent's read/write/glob/grep tools.

``CompositeFilesystemBackend`` prefix-routes between ``SandboxBackend``
(default, Daytona-backed) and one or more ``StoreBackend`` instances —
the latter wrap a LangGraph ``BaseStore`` to host the cross-workspace
memory and memo tiers under ``.agents/user/`` and ``.agents/workspace/``.
``RequestScopedStoreCache`` is shared across the store-backed routes and
the read-side middleware so per-turn lookups dedupe to one round-trip.
"""

from typing import Union

from .composite import CompositeFilesystemBackend
from src.server.services.store_backends.langgraph_store import (
    InvalidStoreKeyError,
    StoreContentInvalidError,
    StoreContentTooLargeError,
    NamespaceFactory,
    ReadOnlyStoreError,
    StoreBackend,
    lock_for_namespace,
    memo_namespace,
    namespace_write_lock,
    store_label,
    user_memory_namespace,
    validate_store_key,
    session_memory_namespace,
    workspace_memory_namespace,
)
from .sandbox import SandboxBackend
from src.server.services.store_backends.store_cache import RequestScopedStoreCache
from .workflows import (
    PREBUILT_READ_ONLY_ERROR,
    WorkflowsBackend,
    prebuilt_workflow_backend,
    workflow_namespace,
)

# Backward-compat alias
DaytonaBackend = SandboxBackend

FilesystemBackend = Union[SandboxBackend, CompositeFilesystemBackend]

__all__ = [
    "CompositeFilesystemBackend",
    "DaytonaBackend",
    "FilesystemBackend",
    "InvalidStoreKeyError",
    "StoreContentInvalidError",
    "StoreContentTooLargeError",
    "NamespaceFactory",
    "PREBUILT_READ_ONLY_ERROR",
    "ReadOnlyStoreError",
    "RequestScopedStoreCache",
    "SandboxBackend",
    "StoreBackend",
    "WorkflowsBackend",
    "lock_for_namespace",
    "memo_namespace",
    "namespace_write_lock",
    "prebuilt_workflow_backend",
    "store_label",
    "user_memory_namespace",
    "validate_store_key",
    "workflow_namespace",
    "session_memory_namespace",
    "workspace_memory_namespace",
]
