"""Ticket checkout paths accept the macOS /var alias spelling (P0 sandbox)."""

import hashlib
import sys
from pathlib import Path

import pytest

from src.server.services.autonomy.ticket_worktree import _is_managed_checkout_path

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS system aliases")


def _alias(path: Path) -> Path:
    text = str(path)
    for name in ("var", "tmp"):
        if text.startswith(f"/private/{name}/"):
            return Path(text.replace(f"/private/{name}", f"/{name}", 1))
    pytest.skip("tmp_path is not under a macOS system alias")


def _checkout(root: Path, repo: Path) -> Path:
    repo_key = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:24]
    return root / repo_key / "tickets" / ("a" * 24)


def test_managed_checkout_spelled_through_var_alias_is_recognised(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    checkout = _checkout(tmp_path / "worktrees", repo)
    assert _is_managed_checkout_path(checkout, repo)
    assert _is_managed_checkout_path(_alias(checkout), repo)


def test_managed_checkout_under_user_symlink_is_still_refused(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    checkout = _checkout(tmp_path / "link", repo)
    assert not _is_managed_checkout_path(checkout, repo)
    assert not _is_managed_checkout_path(_alias(checkout), repo)
