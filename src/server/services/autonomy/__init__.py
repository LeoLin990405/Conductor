"""Event-driven Super-agent orchestration.

The package is deliberately independent of the manual Projects scheduler:
Tickets live in Postgres, external effects are leased from a durable queue, and
one advisory-locked pass advances work no matter which backend worker serves it.
"""
