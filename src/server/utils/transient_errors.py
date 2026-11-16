"""Transient-error types shared by the server and the sandbox runtime.

``SandboxTransientError`` lives here (R9b L11) so server modules such as
``src.server.database.workspace`` can raise and catch it without importing the
sandbox runtime. ``ptc_agent.core.sandbox.runtime`` re-exports this same class,
so ``isinstance`` checks and the app's exception handler see one object.
"""

from __future__ import annotations

__all__ = ["SandboxTransientError"]


class SandboxTransientError(RuntimeError):
    """Transient sandbox transport error.

    Raised when an operation fails due to transient transport issues and cannot be
    safely retried automatically.
    """
