"""One-click local updater for an OSS checkout.

This is deliberately a local-development feature, not a release-channel
updater: it fast-forwards the working tree from ``origin/main``, refreshes
dependencies and rebuilds the web app. It refuses to act on a dirty worktree,
requires a loopback request, and disables itself when the deployment is not an
OSS/local checkout. Packaged desktop builds have no source tree and report
``supported: false``; they should use a signed release feed instead.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
REMOTE = "origin"
BRANCH = "main"
_GIT_TIMEOUT_S = 60
_INSTALL_TIMEOUT_S = 300


class LocalUpdaterError(RuntimeError):
    """A refused or failed update step, safe to show to the local user."""


def _enabled() -> bool:
    flag = (os.getenv("CONDUCTOR_LOCAL_UPDATER") or "").strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        return True
    if flag in {"0", "false", "no", "off"}:
        return False
    # OSS local mode is the only default that gets the button; a platform
    # deployment never runs a git checkout update from a UI request.
    return (os.getenv("HOST_MODE") or "oss").lower() in {"oss", "local"}


def _git(*args: str, timeout: int = _GIT_TIMEOUT_S) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LocalUpdaterError(f"git {args[0]} failed: {exc}") from exc
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip().splitlines()
        raise LocalUpdaterError(message[-1] if message else f"git {args[0]} failed")
    return result.stdout.strip()


def _version() -> str:
    for relative in ("desktop/package.json",):
        try:
            data = json.loads((REPO_ROOT / relative).read_text())
            value = data.get("version")
            if isinstance(value, str) and value:
                return value
        except Exception:
            pass
    try:
        data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
        value = (data.get("project") or {}).get("version")
        if isinstance(value, str) and value:
            return value
    except Exception:
        pass
    return "dev"


def _is_checkout() -> bool:
    return (REPO_ROOT / ".git").exists()


def status() -> dict[str, Any]:
    """What the About/Update card shows without changing anything."""
    if not _is_checkout():
        return {
            "supported": False,
            "enabled": False,
            "version": _version(),
            "reason": "This build has no local git checkout.",
        }
    try:
        branch = _git("rev-parse", "--abbrev-ref", "HEAD")
        commit = _git("rev-parse", "--short", "HEAD")
        dirty = bool(_git("status", "--porcelain"))
        remote_url = _git("remote", "get-url", REMOTE)
    except LocalUpdaterError as exc:
        return {
            "supported": True,
            "enabled": False,
            "version": _version(),
            "reason": str(exc),
        }
    return {
        "supported": True,
        "enabled": _enabled(),
        "version": _version(),
        "commit": commit,
        "branch": branch,
        "dirty": dirty,
        "remote": remote_url,
    }


def check() -> dict[str, Any]:
    if not _is_checkout():
        raise LocalUpdaterError("This build is not a local checkout.")
    if _git("status", "--porcelain"):
        raise LocalUpdaterError("The working tree has local changes; commit or stash them first.")
    try:
        _git("fetch", REMOTE, BRANCH, timeout=90)
    except LocalUpdaterError as exc:
        raise LocalUpdaterError(f"Could not fetch {REMOTE}/{BRANCH}: {exc}") from exc
    current = _git("rev-parse", "--short", "HEAD")
    remote = _git("rev-parse", "--short", f"{REMOTE}/{BRANCH}")
    behind = int(_git("rev-list", "--count", f"HEAD..{REMOTE}/{BRANCH}"))
    ahead = int(_git("rev-list", "--count", f"{REMOTE}/{BRANCH}..HEAD"))
    commits = []
    if behind:
        raw = _git("log", "--oneline", "--max-count=20", f"HEAD..{REMOTE}/{BRANCH}")
        commits = [line for line in raw.splitlines() if line]
    return {
        "update_available": behind > 0,
        "behind": behind,
        "ahead": ahead,
        "current": current,
        "remote": remote,
        "commits": commits,
    }


def _run_step(label: str, command: list[str], timeout: int) -> None:
    try:
        result = subprocess.run(
            command,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LocalUpdaterError(f"{label} failed: {exc}") from exc
    if result.returncode != 0:
        lines = (result.stderr or result.stdout).strip().splitlines()
        raise LocalUpdaterError(f"{label} failed: {lines[-1] if lines else result.returncode}")


def apply() -> dict[str, Any]:
    if not _is_checkout():
        raise LocalUpdaterError("This build is not a local checkout.")
    if not _enabled():
        raise LocalUpdaterError("Local updates are disabled for this deployment.")
    if _git("status", "--porcelain"):
        raise LocalUpdaterError("The working tree has local changes; commit or stash them first.")

    info = check()
    if not info["update_available"]:
        return {"updated": False, "current": info["current"], "message": "Already up to date."}

    old = _git("rev-parse", "--short", "HEAD")
    try:
        _git("merge", "--ff-only", f"{REMOTE}/{BRANCH}")
        _run_step("uv sync", ["uv", "sync", "--frozen"], _INSTALL_TIMEOUT_S)
        _run_step("pnpm install", ["pnpm", "--dir", "web", "install", "--frozen-lockfile"], _INSTALL_TIMEOUT_S)
        _run_step("web build", ["pnpm", "--dir", "web", "build"], _INSTALL_TIMEOUT_S)
    except LocalUpdaterError:
        # Leave the code at the pulled revision; the response names the failed
        # step so the local user can fix dependencies by hand.
        raise
    new = _git("rev-parse", "--short", "HEAD")
    return {
        "updated": True,
        "old_commit": old,
        "new_commit": new,
        "restart_required": True,
        "message": "Code and web build updated. Restart the backend to finish.",
    }


def _restart_process() -> None:
    """Re-exec this server after the HTTP response has a chance to flush."""
    time.sleep(1.2)
    try:
        os.chdir(REPO_ROOT)
        os.execv(sys.executable, [sys.executable, *sys.argv])
    except Exception:  # pragma: no cover - the process is about to disappear
        pass


def restart() -> dict[str, Any]:
    if not _is_checkout():
        raise LocalUpdaterError("This build is not a local checkout.")
    threading.Thread(target=_restart_process, name="local-updater-restart", daemon=True).start()
    return {"restarting": True}
