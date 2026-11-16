"""Pick the P runtime lane for a Super-agent dispatch, or none.

The order mirrors the host/tool branch in ``src/server/app/threads/messaging.py``:
a watchdog dispatch first, then the supervisor route, and everything else
(Workers, ordinary user turns) stays on PTC. Kept import-light: the settings
module is read lazily so importing this from the turn path pulls in nothing.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

SuperagentRole = Literal["supervisor", "watchdog"]


@dataclass(frozen=True)
class SuperagentLane:
    """The role, its autonomy tool host and its tools for one P turn."""

    role: SuperagentRole
    host: Any
    tools: tuple[Any, ...]


def superagent_role_for(
    *, platform: str | None, route_role: str | None, is_dispatch: bool
) -> SuperagentRole | None:
    """Which switchable Super-agent role a turn plays; pure, reads no flag.

    ``supervisor`` is keyed on the route role alone, exactly as messaging.py
    does: that role is only ever assigned to an authenticated
    ``superagent_router`` dispatch, so the dispatch check is already behind it.
    """
    if is_dispatch and platform == "superagent_watchdog":
        return "watchdog"
    if route_role == "supervisor":
        return "supervisor"
    return None


def superagent_lane_for(
    *,
    platform: str | None,
    route_role: str | None,
    is_dispatch: bool,
    host: Any,
    tools: Iterable[Any] | None,
) -> SuperagentLane | None:
    """The P lane for this turn when its role's switch is ``p``, else None."""
    role = superagent_role_for(
        platform=platform, route_role=route_role, is_dispatch=is_dispatch
    )
    if role is None:
        return None
    from src.config.settings import get_superagent_runtime

    if get_superagent_runtime(role) != "p":
        return None
    return SuperagentLane(role=role, host=host, tools=tuple(tools or ()))
