"""In-process SuperAgent loop driven through langchain-core message shapes.

No LangGraph graph, no sandbox, and no PTC import. The contract doubles stay
the only model, store, credit gate, and memory.
"""

from prototypes.superagent.p_python.adapter import make_adapter

__all__ = ["make_adapter"]
