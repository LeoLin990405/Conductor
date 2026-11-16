"""The version and the git identity of the code that is running.

A version alone does not say which code is running: a checkout, an installed
wheel and a daemon that was started yesterday all report the same
distribution version. The git tree SHA is the identity that tells them
apart, so the CLI, ``cond`` and the server's ``/health`` report the same one
and ``cond doctor`` can compare the running server with the checkout.

The git call is read-only and hardened like the one in ``doctor.py``: a
scrubbed environment, no system or global config, no hooks, no fsmonitor, no
prompts and a timeout. Asking a version must never run a hook, write an
index or hang a shell.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

#: What is reported when there is no tree to name.
UNKNOWN_SHA = "unknown"

#: The version reported when no distribution is installed.
UNKNOWN_VERSION = "0.0.0+unknown"

_DISTRIBUTIONS = ("conductor-core", "conductor-cli")

#: A build identity must not keep a shell waiting on a slow filesystem.
_GIT_TIMEOUT = 10.0

#: Only a full object id counts as a SHA; anything else is noise.
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class BuildInfo:
    """What is running: a version, the tree it came from, and whether it moved."""

    version: str
    sha: str
    dirty: bool

    def as_dict(self) -> dict[str, object]:
        """The identity as the ``version`` command reports it."""
        return {"version": self.version, "sha": self.sha, "dirty": self.dirty}

    def describe(self) -> str:
        """One line for a terminal: ``0.1.0 (1a2b3c4d5e6f, dirty)``."""
        state = f"{self.sha}, dirty" if self.dirty else self.sha
        return f"{self.version} ({state})"


def package_version() -> str:
    """The installed distribution version, or a visible fallback."""
    for name in _DISTRIBUTIONS:
        try:
            return metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
    return UNKNOWN_VERSION


def is_sha(value: str) -> bool:
    """True when ``value`` is a full git object id.

    A tree SHA is hex and nothing else, so a value carrying a control
    character, a newline or any other byte is not one. Text that crossed
    a process boundary -- a server answer, a log line -- is believed
    only after this check, because it is about to be printed.
    """
    return bool(SHA_PATTERN.match(value))


def default_checkout() -> Path | None:
    """The checkout this package was loaded from, or None outside one.

    A source checkout keeps the package at ``<root>/src/conductor_cli``; the
    wheel installs it as ``<site-packages>/conductor_cli``, whose parent is
    not ``src``. Only the first shape names a checkout: a venv that happens
    to sit inside an unrelated repository must not lend that repository's
    SHA to this CLI.
    """
    package_dir = Path(__file__).resolve().parent
    parent = package_dir.parent
    if parent.name != "src":
        return None
    root = parent.parent
    return root if (root / ".git").exists() else None


def _git(repo: Path, *args: str) -> str | None:
    """Run a read-only git command in ``repo``; None when git cannot answer.

    The environment is scrubbed and the config is neutralised, so a
    repository cannot make this call run a hook, read a system config or
    wait for a credential prompt.
    """
    git = shutil.which("git", path=os.defpath)
    if git is None:
        return None
    env = {
        "PATH": os.defpath,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    try:
        result = subprocess.run(
            [
                git,
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                *args,
            ],
            cwd=str(repo),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", errors="replace")


def git_sha(repo: Path) -> str:
    """The tree SHA of ``repo``, or ``unknown`` outside a checkout."""
    output = _git(repo, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    if output is None:
        return UNKNOWN_SHA
    sha = output.strip()
    return sha if is_sha(sha) else UNKNOWN_SHA


def git_dirty(repo: Path) -> bool:
    """True when the working tree of ``repo`` differs from its HEAD."""
    output = _git(repo, "status", "--porcelain")
    if output is None:
        return False
    return bool(output.strip())


def build_info(repo: Path | None = None) -> BuildInfo:
    """The identity of ``repo``; the checkout this package came from by default.

    Outside a checkout there is no tree to name, so the SHA is ``unknown``
    and the tree is not dirty -- there is nothing there to be dirty.
    """
    root = repo if repo is not None else default_checkout()
    version = package_version()
    if root is None:
        return BuildInfo(version=version, sha=UNKNOWN_SHA, dirty=False)
    sha = git_sha(root)
    if sha == UNKNOWN_SHA:
        return BuildInfo(version=version, sha=UNKNOWN_SHA, dirty=False)
    return BuildInfo(version=version, sha=sha, dirty=git_dirty(root))


def version_line(name: str = "conductor") -> str:
    """The line ``--version`` prints: ``conductor 0.1.0 (1a2b3c4d, dirty)``.

    It lives next to the identity it formats, and this module imports
    nothing but the standard library, so ``cond --version`` can print the
    same line in a checkout whose dependencies are not installed -- which
    is exactly when an operator asks what is running.
    """
    return f"{name} {build_info().describe()}"


if __name__ == "__main__":  # pragma: no cover - the `cond --version` path
    print(version_line())
