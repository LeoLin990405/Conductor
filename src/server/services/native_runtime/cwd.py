"""Safe host-directory binding for a native CLI workspace."""

from __future__ import annotations

import os
from pathlib import Path


def native_cwd_for_thread(thread_id: str, configured_cwd: str | None = None) -> str:
    """Resolve the same stable cwd used by the native chat launch path."""
    if configured_cwd:
        return configured_cwd
    return str(
        Path.home()
        / ".cache"
        / "conductor"
        / "native-cli"
        / "workspaces"
        / thread_id
    )


def _allowed_roots() -> list[Path]:
    roots = [Path.home().resolve(), Path("/tmp").resolve()]
    configured = os.environ.get("NATIVE_CLI_WORKSPACE_ROOT", "").strip()
    if configured:
        roots.append(Path(configured).expanduser().resolve())
    return roots


def resolve_native_cwd(raw: str | None) -> str | None:
    """Resolve an explicit native cwd; None keeps the private cache default.

    This is a security boundary: a request must not point a vendor CLI at
    ``/etc`` or another user's home. The allowed roots are the caller's home,
    the OS temp dir, and the deployment's own native workspace root.
    """
    if raw is None or not str(raw).strip():
        return None
    path = Path(str(raw).strip()).expanduser().resolve()
    if not path.is_dir():
        raise ValueError("native_cwd is not an existing directory")
    allowed = _allowed_roots()
    if not any(path == root or path.is_relative_to(root) for root in allowed):
        raise ValueError(
            "native_cwd must be inside the user's home, /tmp, or "
            "NATIVE_CLI_WORKSPACE_ROOT"
        )
    return str(path)
