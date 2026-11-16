"""Best-effort start and stop for workers that may not be installed.

ImportError means the optional dependency is absent: one info line, and
shutdown is not asked to stop a module that never started. Any other failure
is one warning and does not propagate.
"""

from __future__ import annotations

import importlib
import inspect
import logging

logger = logging.getLogger(__name__)


async def _call(owner: object, name: str) -> None:
    result = getattr(owner, name)()
    if inspect.isawaitable(result):
        await result


async def start_optional(module: str, start_name: str, *, label: str) -> object | None:
    """Import ``module``, call ``start_name``, and return the module.

    Returns None when the module cannot be imported or the start call fails.
    """
    try:
        imported = importlib.import_module(module)
    except ImportError:
        # Absent optional dependency. Shutdown is not told to try again.
        logger.info(
            "%s was skipped because the optional dependency is not installed",
            label,
        )
        return None
    except Exception as exc:
        logger.warning("%s did not start: %s", label, type(exc).__name__)
        return None

    try:
        await _call(imported, start_name)
    except Exception as exc:
        logger.warning("%s did not start: %s", label, type(exc).__name__)
        return None
    return imported


async def stop_optional(module: object | None, stop_name: str, *, label: str) -> None:
    """Call ``stop_name`` on a module from :func:`start_optional`.

    A None module is a worker that was not started; that logs nothing.
    """
    if module is None:
        return
    try:
        await _call(module, stop_name)
    except Exception as exc:
        logger.warning("%s shutdown failed: %s", label, type(exc).__name__)
