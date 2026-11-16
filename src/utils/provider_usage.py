"""Provider usage helpers (R9b L03).

Alias of the canonical module ``src/conductor_cli/provider_usage.py``: it lives in the
package the ``conductor`` wheel ships, because ``conductor_cli`` needs it there
(the wheel has no ``src``, ``utils`` or ``config`` package). Both names are
one module object, so private names, module state and patch targets are shared.
"""

import sys

try:
    from src.conductor_cli import provider_usage as _module
except ModuleNotFoundError as exc:  # outside the repo root, or the installed wheel
    if exc.name != "src":
        raise
    from conductor_cli import provider_usage as _module

sys.modules[__name__] = _module
