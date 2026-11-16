"""R9b shim: re-export for src/ptc_agent-internal importers; removed in L21/L25/L28.

Aliases ``ptc_agent.config.core`` to the real module in ``src/config/runtime``,
so both names are one module object: private names, module state and
``mock.patch`` targets are shared.
"""

import sys

try:
    from src.config.runtime import core as _module
except ModuleNotFoundError as exc:  # pragma: no cover - exercised from a non-root cwd
    if exc.name != "src":
        raise
    from config.runtime import core as _module

sys.modules[__name__] = _module
