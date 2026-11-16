"""conductor-collab-mcp: the N80 collab protocol for interactive apps.

``client`` (the HTTP contract with ``/api/v1/collab``) and ``heartbeat`` import
nothing from ``src`` so ``conductor_cli`` can share them; ``server`` and
``__main__`` need a Conductor checkout for the protocol schemas. Nothing is
imported here, so ``import collab_mcp.client`` stays light.
"""
