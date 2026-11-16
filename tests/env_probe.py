"""Probes for host capabilities some tests hard-require, and skip markers.

A sandboxed runner can block ``ps`` exec (EPERM) or run out of pty devices;
tests that exercise process trees or drive a vendor CLI through a pty then
fail for environment reasons, not code reasons. Tests declare the need with
the ``requires_ps`` / ``requires_pty`` marks, and the collection hook here
skips them with a clear reason when this host lacks the capability. Where the
capability is present the marks do nothing.
"""

from __future__ import annotations

import os
import pty
import subprocess
from typing import Any

import pytest


def ps_available() -> bool:
    """True when ``ps`` runs and answers for this process.

    Sandboxes that forbid the exec surface it as ``PermissionError`` from
    ``Popen``; a host without procps raises ``FileNotFoundError``. Both are
    ``OSError`` subclasses.
    """
    try:
        result = subprocess.run(
            ["ps", "-o", "pid=", "-p", str(os.getpid())],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def pty_available() -> bool:
    """True when a pty pair opens."""
    try:
        master_fd, slave_fd = pty.openpty()
    except OSError:
        return False
    os.close(master_fd)
    os.close(slave_fd)
    return True


def pytest_collection_modifyitems(items: list[Any]) -> None:
    """Skip capability-marked tests when this host lacks the capability."""
    probes = (
        ("requires_ps", "ps", ps_available),
        ("requires_pty", "pty", pty_available),
    )
    results: dict[str, bool] = {}
    for item in items:
        for mark_name, what, probe in probes:
            if mark_name not in item.keywords:
                continue
            if mark_name not in results:
                results[mark_name] = probe()
            if not results[mark_name]:
                item.add_marker(
                    pytest.mark.skip(
                        reason=f"{what} is not available in this environment"
                    )
                )
