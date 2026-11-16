"""A private writable scratch dir for one read-only cross-review run.

A reviewer runs as one read-only native-CLI turn, so its CLI sandbox denies every
write. Tests still need somewhere to put temp dirs and tool caches. A scratch dir
lives outside the repository (inside the run's isolation dir), and while it is
current every native CLI child gets TMPDIR, UV_CACHE_DIR and the test caches pointed
into it. The repository itself stays read-only; Codex is given the scratch as its
only writable root.
"""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

_SUBDIRS = ("tmp", "uv-cache", "pytest-cache", "xdg-cache", "npm-cache", "pycache")


def _real(path: Path | str) -> Path:
    return Path(os.path.realpath(os.fspath(path)))


def paths_overlap(a: Path | str, b: Path | str) -> bool:
    """True when either path is the other or contains it, after resolving aliases."""
    ra, rb = _real(a), _real(b)
    return ra == rb or ra.is_relative_to(rb) or rb.is_relative_to(ra)


@dataclass(frozen=True)
class ReviewScratch:
    root: Path
    repo: Path

    @property
    def tmp(self) -> Path:
        return self.root / "tmp"

    @property
    def uv_cache(self) -> Path:
        return self.root / "uv-cache"

    @property
    def pytest_cache(self) -> Path:
        return self.root / "pytest-cache"

    @property
    def xdg_cache(self) -> Path:
        return self.root / "xdg-cache"

    @property
    def npm_cache(self) -> Path:
        return self.root / "npm-cache"

    @property
    def pycache(self) -> Path:
        return self.root / "pycache"

    def env(self) -> dict[str, str]:
        tmp = str(self.tmp)
        return {
            "TMPDIR": tmp,
            "TMP": tmp,
            "TEMP": tmp,
            "UV_CACHE_DIR": str(self.uv_cache),
            # ``uv run`` must not try to sync the read-only repo .venv.
            "UV_NO_SYNC": "1",
            "PYTHONPYCACHEPREFIX": str(self.pycache),
            "PYTHONDONTWRITEBYTECODE": "1",
            # The cache plugin writes .pytest_cache into the repo; turn it off.
            "PYTEST_ADDOPTS": "-p no:cacheprovider",
            "XDG_CACHE_HOME": str(self.xdg_cache),
            "npm_config_cache": str(self.npm_cache),
        }


_CURRENT: ContextVar[ReviewScratch | None] = ContextVar(
    "native_cli_review_scratch", default=None
)


def current_review_scratch() -> ReviewScratch | None:
    return _CURRENT.get()


@contextmanager
def review_scratch(base_dir: Path | str, repo: Path | str) -> Iterator[ReviewScratch]:
    """Create a fresh 0700 scratch under ``base_dir`` and make it current.

    Refuses (ValueError) when the scratch base and the repository overlap in either
    direction: a scratch inside the repo would make the repo writable, and a repo
    inside the scratch would be deleted with it.
    """
    repo_real = _real(repo)
    base_real = _real(base_dir)
    if paths_overlap(base_real, repo_real):
        raise ValueError(
            f"review scratch base {base_real} overlaps the repository {repo_real}"
        )
    base_real.mkdir(parents=True, exist_ok=True)
    root = base_real / uuid.uuid4().hex
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    scratch = ReviewScratch(root=root, repo=repo_real)
    token = None
    try:
        for name in _SUBDIRS:
            (root / name).mkdir(mode=0o700)
        token = _CURRENT.set(scratch)
        yield scratch
    finally:
        if token is not None:
            _CURRENT.reset(token)
        shutil.rmtree(root, ignore_errors=True)


def scratch_prompt_note(scratch: ReviewScratch) -> str:
    return (
        f"Sandbox: the repository at {scratch.repo} is read-only. Your writable "
        f"scratch is {scratch.root}; TMPDIR, UV_CACHE_DIR and test caches already "
        "point there. Run tests from the repo with `-p no:cacheprovider`. If a "
        "command still cannot run, rely on the host-run G2 results in the evidence "
        'rather than writing "unverified".'
    )
