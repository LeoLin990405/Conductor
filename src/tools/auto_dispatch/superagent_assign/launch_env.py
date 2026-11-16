"""The env a Super-agent choice adds to one line's launch (N73 L4).

``scripts/dispatch_launch.sh`` (L2) reads ``CONDUCTOR_LINE_MODEL_ID``,
``CONDUCTOR_LINE_CLI_EFFORT`` and ``CONDUCTOR_LINE_RUNTIME`` and adds the
provider's flags; unset variables give today's argv byte for byte. The
machine registers a line's env only when it *adopts* the Super-agent's choice
(enforce, sampled); shadow and off register nothing, so the launch is the
selector's, unchanged. The env is also persisted on the line snapshot
(``SNAPSHOT_KEY``), so a restarted drive rebuilds it (:func:`restore`), and it
is cleared when the dispatch ends (:func:`clear_dispatch`).
"""

from __future__ import annotations

from typing import Any, Mapping

from .effort import NA, apply_model_id, resolve_effort

MODEL_ID_ENV = "CONDUCTOR_LINE_MODEL_ID"
CLI_EFFORT_ENV = "CONDUCTOR_LINE_CLI_EFFORT"
RUNTIME_ENV = "CONDUCTOR_LINE_RUNTIME"
DEFAULT = "default"
SNAPSHOT_KEY = "assignment_launch_env"

_registry: dict[tuple[str, str], dict[str, str]] = {}


def env_for_choice(choice: Any) -> dict[str, str]:
    """The launch variables for one adopted choice; ``{}`` when all default."""
    model = str(getattr(choice, "model", "") or "").strip().lower()
    raw_effort = getattr(choice, "effort", DEFAULT)
    effort = str(getattr(raw_effort, "value", raw_effort) or DEFAULT)
    model_id = str(getattr(choice, "model_id", "") or "").strip() or DEFAULT
    runtime = str(getattr(choice, "runtime", "") or "").strip() or DEFAULT
    resolved = resolve_effort(model, effort)
    env: dict[str, str] = {}
    launch_id = apply_model_id(model_id, resolved)
    if launch_id:
        env[MODEL_ID_ENV] = launch_id
    token = resolved.launch_token
    if token and token != NA:
        env[CLI_EFFORT_ENV] = token
    if runtime != DEFAULT:
        env[RUNTIME_ENV] = runtime
    return env


def register(dispatch_id: str, line_id: str, env: Mapping[str, str]) -> None:
    key = (str(dispatch_id), str(line_id))
    if env:
        _registry[key] = dict(env)
    else:
        _registry.pop(key, None)


def clear(dispatch_id: str, line_id: str) -> None:
    _registry.pop((str(dispatch_id), str(line_id)), None)


def for_line(dispatch_id: str | None, line_id: str | None) -> dict[str, str]:
    if not dispatch_id or not line_id:
        return {}
    return dict(_registry.get((str(dispatch_id), str(line_id)), {}))


def clear_dispatch(dispatch_id: str) -> None:
    """Forget every line env of a dispatch (it ended)."""
    for key in [key for key in _registry if key[0] == str(dispatch_id)]:
        _registry.pop(key, None)


def restore(dispatch: Any) -> int:
    """Re-register the persisted env of each line; returns how many lines."""
    gates = getattr(dispatch, "gates", None)
    cells = gates.get("lines") if isinstance(gates, Mapping) else None
    count = 0
    if not isinstance(cells, Mapping):
        return 0
    for line_id, snap in cells.items():
        env = snap.get(SNAPSHOT_KEY) if isinstance(snap, Mapping) else None
        if isinstance(env, Mapping) and env:
            allowed = {MODEL_ID_ENV, CLI_EFFORT_ENV, RUNTIME_ENV}
            clean = {str(k): str(v) for k, v in env.items() if k in allowed}
            if clean:
                register(dispatch.id, str(line_id), clean)
                count += 1
    return count
