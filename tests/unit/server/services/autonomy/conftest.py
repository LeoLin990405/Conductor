"""Shared fixtures for the autonomy service tests."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.fixture(autouse=True)
def _no_queued_items_after_a_report(monkeypatch):
    """The executor's queue read after a report sees an empty queue.

    ``poll_running_workers`` reads the Ticket's queued instructions after a
    clean ``ready`` report (R5b L2 push-next). These tests have no database
    pool, and a failed read holds the report, so the read is stubbed to the
    empty queue every test before the push existed assumed.
    ``test_report_continuation.py`` patches it per test.
    """
    from src.server.services.autonomy import executor

    monkeypatch.setattr(executor, "queued_next_items", AsyncMock(return_value=[]))
