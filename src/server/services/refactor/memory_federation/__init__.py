"""Read-only federated retrieval and proposal-provider contracts."""

from .farm import FarmRetrieval
from .fusion import fuse_hits
from .conductor import ConductorPgMemory
from .memfw_reader import MemfwMemory, MemfwReadError, MemfwReader
from .models import MemoryEntry, MemoryHit, MemoryProposal
from .provider import (
    MemoryFederation,
    MemoryProvider,
    NoWritableMemoryProvider,
    ReadOnlyMemoryProviderError,
)

__all__ = [
    "FarmRetrieval",
    "ConductorPgMemory",
    "MemoryEntry",
    "MemoryFederation",
    "MemoryHit",
    "MemoryProposal",
    "MemoryProvider",
    "MemfwMemory",
    "MemfwReadError",
    "MemfwReader",
    "NoWritableMemoryProvider",
    "ReadOnlyMemoryProviderError",
    "fuse_hits",
]
