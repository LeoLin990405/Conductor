"""The per-thread permission mode (D3 unification layer).

Modes are the contract vocabulary (manual/auto/skip), but this layer only
accepts the two that are safe today:

- ``auto`` — the current behaviour: the user's standing approvals (hand-offs,
  workspace creation) are honoured, everything else asks.
- ``manual`` — strict: no standing approval is honoured; every approval card is
  shown.

``skip`` is rejected. Suppressing approval cards needs an approval-capable gate
that does not exist yet; see docs/refactor/0008 D3. The flag keeps the whole
surface default-off, so an unchanged deployment behaves exactly as before.
"""

from __future__ import annotations

from typing import Any

from src.server.contracts.refactor import PERMISSION_MODES, ContractError
from src.server.database.refactor import permissions as db_permissions
from src.server.services.refactor_flags import refactor_flag_enabled

DEFAULT_PERMISSION_MODE = "auto"
SUPPORTED_PERMISSION_MODES: tuple[str, ...] = ("manual", "auto")


def _validate(mode: str) -> str:
    value = str(mode or "").strip()
    if value not in SUPPORTED_PERMISSION_MODES:
        raise ContractError(
            f"unsupported permission mode {value!r}; supported: "
            f"{list(SUPPORTED_PERMISSION_MODES)}"
        )
    return value


async def read_permission_mode(thread_id: str) -> str:
    """The thread's mode, or the default when the flag is off / no row exists."""
    if not refactor_flag_enabled("permissions"):
        return DEFAULT_PERMISSION_MODE
    row = await db_permissions.get_mode(thread_id)
    mode = str((row or {}).get("mode") or DEFAULT_PERMISSION_MODE)
    return mode if mode in PERMISSION_MODES else DEFAULT_PERMISSION_MODE


async def effective_permission_mode(thread_id: str | None) -> str:
    """Turn-path read: never raises, degrades to the default on any failure."""
    if not thread_id or not refactor_flag_enabled("permissions"):
        return DEFAULT_PERMISSION_MODE
    try:
        return await read_permission_mode(thread_id)
    except Exception:
        return DEFAULT_PERMISSION_MODE


async def set_permission_mode(
    *, thread_id: str, mode: str, updated_by: str | None = None
) -> dict[str, Any] | None:
    """Persist a mode; ``None`` when the flag is off (write is a no-op)."""
    if not refactor_flag_enabled("permissions"):
        return None
    return await db_permissions.set_mode(
        thread_id=thread_id, mode=_validate(mode), updated_by=updated_by
    )
