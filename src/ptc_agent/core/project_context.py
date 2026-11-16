"""R9b shim: re-export for src/ptc_agent-internal importers; removed in L21/L25/L28.

The module lives at src/server/services/project_context.py.
"""

from src.server.services.project_context import *  # noqa: F403
from src.server.services.project_context import __all__  # noqa: F401
from src.server.services.project_context import _current  # noqa: F401
