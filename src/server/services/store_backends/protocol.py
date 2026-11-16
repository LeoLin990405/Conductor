"""The file backend a store-backed route borrows its path rules from.

A store route answers the composite's path questions the way the file
backend beside it does, so a path reads the same on every route. It asks
nothing else of that backend. ``SandboxBackend`` satisfies this; so does any
test stub with the same four members.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class FileBackend(Protocol):
    """The members of the file backend that the store routes call."""

    def normalize_path(self, path: str) -> str:
        """A virtual or relative path as the backend's absolute path."""
        ...

    def virtualize_path(self, path: str) -> str:
        """An absolute path as the agent sees it."""
        ...

    def validate_path(self, path: str) -> bool:
        """Whether ``path`` is within the allowed directories."""
        ...

    @property
    def filesystem_config(self) -> Any:
        """The backend's filesystem config (allowed dirs, working dir, validation flag)."""
        ...


__all__ = ["FileBackend"]
