"""R9b shim: re-export for src/ptc_agent-internal importers; removed in L21/L25/L28.

Aliases ``ptc_agent.core.mcp_schema`` to the real module ``src.server.services.mcp_core.schema``, so both names are one
module object: private names, module state and ``mock.patch`` targets are shared.
"""

import sys

from src.server.services.mcp_core import schema as _module

sys.modules[__name__] = _module
