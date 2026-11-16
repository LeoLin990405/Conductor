"""R9b shim: re-export for src/ptc_agent-internal importers; removed in L21/L25/L28.

The module lives at src/utils/paths.py. Importing this shim runs
``ptc_agent/core/__init__.py``, which already needs ``src`` on the path, so the
``src.`` prefix is safe here. Code that runs from any cwd (the TUI) imports
``utils.paths`` instead.
"""

from src.utils.paths import *  # noqa: F403
