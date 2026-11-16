"""Glue-only SuperAgent slot over the production P turn loop.

Doubles stay in the contract package. The turn order lives in
``src.server.services.superagent_runtime`` — this package only adapts them.
"""

from prototypes.superagent.production.adapter import make_adapter

__all__ = ["make_adapter"]
