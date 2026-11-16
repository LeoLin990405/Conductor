"""cond doctor FAILs when the dispatch repository has no default Ticket base.

Without origin/main or main there, every daemon Ticket without its own base
stays queued (the server treats it as a server-wide outage), so the operator
needs a visible signal, not only a server log line. Tmp git repos only.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from src.conductor_cli.doctor import _ticket_base_check, format_doctor


def _git(cwd: Path, *args: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)


def _repo(path: Path, branch: str) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", branch)
    _git(path, "config", "user.name", "x")
    _git(path, "config", "user.email", "x@example.invalid")
    (path / "a").write_text("a\n", encoding="utf-8")
    _git(path, "add", "a")
    _git(path, "commit", "-qm", "a")
    return path


def test_missing_default_base_is_a_fail_line(tmp_path: Path) -> None:
    check = _ticket_base_check(_repo(tmp_path / "trunk", "trunk"))
    assert check["ok"] is False
    line = format_doctor({"ok": False, "checks": [check]}).splitlines()[0]
    assert line.startswith("FAIL: ticket_base")
    assert "neither origin/main nor main" in line


def test_main_is_a_default_base(tmp_path: Path) -> None:
    check = _ticket_base_check(_repo(tmp_path / "main", "main"))
    assert check["ok"] is True and "main" in str(check["detail"])


def test_not_a_checkout_fails_and_unconfigured_is_ok(tmp_path: Path) -> None:
    (tmp_path / "plain").mkdir()
    assert _ticket_base_check(tmp_path / "plain")["ok"] is False
    assert _ticket_base_check(None)["ok"] is True
