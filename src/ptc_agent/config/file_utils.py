"""R9b shim for src/ptc_agent-internal importers; removed in L21/L25/L28.

Alias of the canonical module ``src/conductor_cli/config_files.py``: it lives in the
package the ``conductor`` wheel ships, because ``conductor_cli`` needs it there
(the wheel has no ``src``, ``utils`` or ``config`` package). Both names are
one module object, so private names, module state and patch targets are shared.
"""

import sys

try:
    from src.conductor_cli import config_files as _module
except ModuleNotFoundError as exc:  # outside the repo root, or the installed wheel
    if exc.name != "src":
        raise
    from conductor_cli import config_files as _module

sys.modules[__name__] = _module
