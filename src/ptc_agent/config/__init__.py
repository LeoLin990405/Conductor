"""R9b shim: re-export for src/ptc_agent-internal importers; removed in L21/L25/L28.

The configuration package lives in ``src/config/runtime`` (R9b L08). Each
submodule here (``ptc_agent.config.core`` and the rest) is an alias of the real
module, so both names share one module object and its state. ``src`` is only
importable with the repo root on ``sys.path``; outside it (a CLI or TUI started
from another directory) the same package resolves as top-level ``config``.
"""

try:
    from src.config.runtime import *  # noqa: F403
    from src.config.runtime import __all__  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover - exercised from a non-root cwd
    if exc.name != "src":
        raise
    from config.runtime import *  # noqa: F403
    from config.runtime import __all__  # noqa: F401
