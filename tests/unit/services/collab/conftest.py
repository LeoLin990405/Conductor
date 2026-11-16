"""Shared fixtures for the collab service tests (N80 L2).

* HOME is a temporary directory and the real HOME is asserted untouched
  (the collab tripwire from ``tests/unit/_isolated_home.py``);
* every persistence call goes to an in-memory :class:`FakeStore`;
* the worktree, Git, vault and redaction seams are replaced, so no git
  process, database, vault, keychain or socket is used;
* ``CONDUCTOR_COLLAB_MODE`` defaults to ``full`` here (tests that need
  another stage set it), and each test gets a fresh rate limiter.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.collab.ratelimit import RateLimiter
from src.server.services.collab import leases as lease_svc
from src.server.services.collab import report as report_svc
from src.server.services.collab import tools
from tests.unit._isolated_home import assert_untouched, isolate
from tests.unit.services.collab.collab_fakes import FakeStore, install

LOOPBACK = "127.0.0.1"
BASE_SHA = "b" * 40


@pytest.fixture(autouse=True)
def isolated_collab_service_home(tmp_path, monkeypatch):
    home, original, before = isolate(tmp_path, monkeypatch)
    yield home
    assert_untouched(original, before)


@pytest.fixture
def store(monkeypatch) -> FakeStore:
    tools.load_handlers()
    fake = install(monkeypatch, FakeStore())
    monkeypatch.setenv("CONDUCTOR_COLLAB_MODE", "full")
    monkeypatch.delenv("INTERNAL_SERVICE_TOKEN", raising=False)

    async def worktree(ticket: dict[str, Any]) -> dict[str, Any]:
        return {
            "path": f"/tmp/collab-wt/{ticket['ticket_id']}",
            "branch": f"conductor/ticket/{ticket['ticket_id']}",
            "base_sha": BASE_SHA,
            "push_guard": True,
        }

    monkeypatch.setattr(lease_svc, "prepare_claim_worktree", worktree)

    async def facts(lease: dict[str, Any]) -> report_svc.GitFacts:
        return fake.git_facts

    fake.git_facts = report_svc.GitFacts(
        commit_delivery="delivered",
        dirty_summary=None,
        changed_paths=("docs/protocol.md",),
        branch=None,
        has_upstream=False,
    )
    monkeypatch.setattr(report_svc, "collect_git_facts", facts)
    return fake


@pytest.fixture
def limiter() -> RateLimiter:
    return RateLimiter()


@pytest.fixture
def call(store, limiter):
    """``await call(tool, token, **arguments)`` -> ``(status, envelope)``."""

    async def run(
        tool: str,
        token: str | None = None,
        *,
        headers: dict[str, str] | None = None,
        client_host: str = LOOPBACK,
        **arguments: Any,
    ):
        sent = dict(headers or {})
        if token is not None:
            sent["authorization"] = f"Bearer {token}"
        return await tools.invoke(
            tool,
            arguments=arguments,
            headers=sent,
            client_host=client_host,
            limiter=limiter,
        )

    return run
