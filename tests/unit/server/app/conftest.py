"""Fixtures for the review/merge API tests: a real git fleet + in-memory queue.

The queue/audit tables are replaced by an in-memory store (the SQL itself is
covered by ``tests/unit/server/database/autonomy/test_reviews_db.py``); the
worktrees are real because the merge gate and the rehearsal are about actual
git state.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.conftest import create_test_app
from tests.unit.server.review_merge_fakes import FakeReviewStore
from tests.unit.server.review_merge_git import add_worktree, git, init_repo

_STORE_FUNCTIONS = (
    "create_review",
    "list_reviews",
    "get_review",
    "complete_review",
    "record_audit",
    "list_audit",
)


@pytest.fixture
def review_store(monkeypatch):
    """Swap the review/audit database functions for one in-memory store."""
    from src.server.database.autonomy import reviews as review_db

    store = FakeReviewStore()
    for name in _STORE_FUNCTIONS:
        monkeypatch.setattr(review_db, name, getattr(store, name))
    return store


@pytest.fixture
def feature_on(monkeypatch):
    """Treat the authenticated caller as a super-agent feature holder."""
    from src.server.services import features

    async def enabled(user_id: str, key: str) -> bool:
        return key == "super_agent"

    monkeypatch.setattr(features, "user_feature_enabled", enabled)


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """A repository plus ``integrate/r2`` and ``feat/line`` worktrees in a root."""
    root = tmp_path / "fleet"
    root.mkdir()
    repo = init_repo(root / "repo", {"base.txt": "base\n"})
    git(repo, "branch", "integrate/r2")
    add_worktree(repo, root / "target", "integrate/r2")
    add_worktree(repo, root / "line", "feat/line", create_branch=True)
    monkeypatch.setenv("CONDUCTOR_WT_ROOT", str(root))
    return SimpleNamespace(
        root=root, repo=repo, target=root / "target", line=root / "line"
    )


LOOPBACK_BASE = "http://127.0.0.1:8000"


@pytest_asyncio.fixture
async def client(review_store, feature_on):
    """Loopback ASGI client: loopback peer, loopback Host, no proxy headers."""
    from src.server.app.review_merge import router

    app = create_test_app(router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url=LOOPBACK_BASE
    ) as http:
        yield http


@pytest_asyncio.fixture
async def remote_client(review_store, feature_on):
    """A non-loopback peer: review is allowed, merge must never be."""
    from src.server.app.review_merge import router

    app = create_test_app(router)
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("203.0.113.7", 5555)),
        base_url=LOOPBACK_BASE,
    ) as http:
        yield http


@pytest_asyncio.fixture
async def proxied_client(review_store, feature_on):
    """A loopback peer behind a same-host front end with a public Host name."""
    from src.server.app.review_merge import router

    app = create_test_app(router)
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 5555)),
        base_url="http://conductor.example.com",
    ) as http:
        yield http
