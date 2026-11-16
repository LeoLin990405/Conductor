"""The build identity: the version, the tree it was built from, and the dirt.

A version alone does not say which code is running: a checkout, an
installed wheel and a daemon that was started yesterday all report the same
distribution version. These tests pin the one place that answers --
``build_info`` -- and the three places that show it: ``conductor
--version``, ``cond --version`` and the server's ``/health``, with ``cond
doctor`` comparing the running server against the checkout.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError

import pytest

from src.conductor_cli import cli
from src.conductor_cli.build_info import UNKNOWN_SHA, build_info, package_version
from src.conductor_cli.doctor import (
    _build_identity_check,
    doctor_report,
    format_doctor,
)
from src.conductor_cli.output import cli_version
from src.conductor_cli.ops_support import Completed
from src.server.app import utilities
from src.server.app.utilities import health_check

#: The root of the repository: this file is ``<root>/tests/unit/conductor_cli/``.
REPO_ROOT = Path(__file__).resolve().parents[3]
WRAPPER = REPO_ROOT / "scripts" / "cond"


def _http_error(code: int, body: bytes) -> HTTPError:
    """The error urllib raises for a non-2xx response, body and all."""
    return HTTPError(
        "http://127.0.0.1:54399/health", code, "error", {}, io.BytesIO(body)
    )


def _git(cwd: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout.strip()


def _checkout(path: Path) -> tuple[Path, str]:
    """A git checkout with one commit; returns the path and its HEAD."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "x")
    _git(path, "config", "user.email", "x@example.invalid")
    (path / "a").write_text("a\n", encoding="utf-8")
    _git(path, "add", "a")
    _git(path, "commit", "-qm", "a")
    return path, _git(path, "rev-parse", "HEAD")


# ---------------------------------------------------------------------------
# build_info
# ---------------------------------------------------------------------------


def test_build_info_names_the_tree_of_a_checkout(tmp_path: Path) -> None:
    repo, sha = _checkout(tmp_path / "repo")

    info = build_info(repo)

    assert info.sha == sha
    assert info.dirty is False
    assert info.version == package_version()


def test_build_info_calls_a_changed_tree_dirty(tmp_path: Path) -> None:
    repo, sha = _checkout(tmp_path / "repo")

    assert build_info(repo).dirty is False
    (repo / "a").write_text("changed\n", encoding="utf-8")

    info = build_info(repo)
    assert info.sha == sha
    assert info.dirty is True
    assert "dirty" in info.describe()


def test_build_info_outside_a_checkout_is_unknown(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-checkout"
    plain.mkdir()

    info = build_info(plain)

    assert info.sha == UNKNOWN_SHA
    assert info.dirty is False
    assert info.describe() == f"{info.version} (unknown)"


def test_build_info_without_git_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "src.conductor_cli.build_info.shutil.which", lambda *args, **kwargs: None
    )

    assert build_info(tmp_path).sha == UNKNOWN_SHA


def test_the_git_call_is_read_only_and_hardened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading a version must not run a hook, read a config or wait for input."""
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=b"a" * 40 + b"\n", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert build_info(tmp_path).sha == "a" * 40

    argv, kwargs = calls[0]
    assert Path(argv[0]).name == "git"
    # Only ref reads: nothing that writes a ref, an index or an object.
    assert "rev-parse" in argv
    for forbidden in ("commit", "checkout", "add", "write", "update-ref", "fetch", "gc"):
        assert forbidden not in argv
    assert "core.fsmonitor=false" in argv
    assert "core.hooksPath=/dev/null" in argv
    env = kwargs["env"]
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_SYSTEM"] == "/dev/null"
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_OPTIONAL_LOCKS"] == "0"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "HOME" not in env
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["timeout"]


# ---------------------------------------------------------------------------
# conductor --version and conductor version
# ---------------------------------------------------------------------------


def test_cli_version_is_the_package_version() -> None:
    assert cli_version() == package_version()


def test_the_version_command_reports_the_tree(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["version", "--json"]) == 0

    envelope = json.loads(capsys.readouterr().out)
    data = envelope["data"]
    assert data["name"] == "conductor"
    assert data["version"] == cli_version()
    assert data["sha"] == build_info().sha
    assert data["dirty"] == build_info().dirty


def test_the_version_flag_prints_the_identity(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["--version"])

    assert raised.value.code == 0
    printed = capsys.readouterr().out
    assert printed.startswith("conductor ")
    assert cli_version() in printed
    assert build_info().sha in printed


def test_building_the_parser_does_not_read_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every command builds the parser; asking git there would slow all of them."""

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("the parser must not read the build identity")

    monkeypatch.setattr(subprocess, "run", boom)

    assert cli.build_parser().parse_args(["version"]).command == "version"


def test_cond_version_prints_the_checkout_identity(tmp_path: Path) -> None:
    """`cond` launches this checkout, so it names this checkout's tree."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "CONDUCTOR_PYTHON": sys.executable,
        "CONDUCTOR_REPO_ROOT": str(REPO_ROOT),
    }

    result = subprocess.run(
        ["bash", str(WRAPPER), "--version"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    printed = result.stdout.strip()
    assert printed.startswith("conductor ")
    assert _git(REPO_ROOT, "rev-parse", "HEAD") in printed


def test_the_identity_module_needs_no_installed_dependency(tmp_path: Path) -> None:
    """Asking a version must not need the CLI's third-party imports.

    ``-S`` drops site-packages, so anything but the standard library is
    gone. That is the state of a checkout whose dependencies are missing --
    exactly when an operator asks what is running.
    """
    result = subprocess.run(
        [sys.executable, "-S", str(REPO_ROOT / "src" / "conductor_cli" / "build_info.py")],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().startswith("conductor ")


def test_cond_version_answers_on_a_checkout_without_the_identity(
    tmp_path: Path,
) -> None:
    """An older checkout has no identity module; the answer must still come."""
    repo = tmp_path / "old"
    (repo / "src" / "conductor_cli").mkdir(parents=True)
    (repo / "src" / "conductor_cli" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "conductor_cli" / "__main__.py").write_text(
        "print('conductor 0.0.1 (legacy)')\n", encoding="utf-8"
    )
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "CONDUCTOR_PYTHON": sys.executable,
        "CONDUCTOR_REPO_ROOT": str(repo),
    }

    result = subprocess.run(
        ["bash", str(WRAPPER), "--version"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "conductor 0.0.1 (legacy)" in result.stdout


# ---------------------------------------------------------------------------
# The running server
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_reports_the_running_server_sha() -> None:
    result = await health_check()

    assert isinstance(result, dict)
    assert result["sha"] == utilities._SERVER_SHA
    assert result["sha"] == _git(REPO_ROOT, "rev-parse", "HEAD")


@pytest.mark.asyncio
async def test_health_reports_the_tree_the_server_started_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The answer is frozen at start, not read per request.

    A checkout that moved after the server started runs different code than
    the server does; answering with the checkout's current HEAD would report
    a match and hide the mismatch doctor is meant to show.
    """
    monkeypatch.setattr(utilities, "_read_server_sha", lambda root: "c" * 40)

    result = await health_check()

    assert isinstance(result, dict)
    assert result["sha"] == utilities._SERVER_SHA
    assert result["sha"] != "c" * 40


def test_the_server_reads_the_tree_of_its_own_checkout(tmp_path: Path) -> None:
    repo, sha = _checkout(tmp_path / "repo")

    assert utilities._read_server_sha(repo) == sha


def test_the_server_reports_unknown_outside_a_checkout(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-checkout"
    plain.mkdir()

    assert utilities._read_server_sha(plain) == UNKNOWN_SHA


# ---------------------------------------------------------------------------
# cond doctor
# ---------------------------------------------------------------------------


class _HealthSystem:
    """The parts of the system doctor uses, with a fixed ``/health`` answer."""

    def __init__(self, body: str, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.urls: list[str] = []

    def http_get(self, url: str, timeout: float) -> tuple[int, str]:
        self.urls.append(url)
        return self.status, self.body

    def port_open(self, host: str, port: int, timeout: float = 0.3) -> bool:
        return False

    def run(self, argv: list[str], **kwargs: object) -> Completed:
        return Completed(0, "", "")

    def alive(self, pid: int) -> bool:
        return False

    def command(self, pid: int) -> str:
        return ""

    def cwd(self, pid: int) -> str:
        return ""

    def processes(self) -> str:
        """No processes at all: the orphan scan reads this instead of running ``ps``."""
        return ""


def _settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo: Path):
    from src.conductor_cli.ops_support import load_settings

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CONDUCTOR_HOME", str(home / ".conductor"))
    monkeypatch.setenv("CONDUCTOR_REPO_ROOT", str(repo))
    monkeypatch.setenv("CONDUCTOR_BACKEND_PORT", "54399")
    monkeypatch.setenv("CONDUCTOR_WEB_PORT", "54398")
    monkeypatch.delenv("CONDUCTOR_DISPATCH_REPO", raising=False)
    return load_settings()


def test_doctor_shows_the_server_sha_next_to_the_checkout_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, sha = _checkout(tmp_path / "repo")
    settings = _settings(tmp_path, monkeypatch, repo)
    system = _HealthSystem(json.dumps({"status": "healthy", "sha": sha}))

    check = _build_identity_check(system, settings)

    assert check["ok"] is True
    assert check.get("warn") is not True
    assert check["checkout_sha"] == sha
    assert check["server_sha"] == sha
    assert sha in str(check["detail"])
    assert system.urls == [f"{settings.base_url}/health"]


def test_doctor_warns_when_the_server_runs_another_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, sha = _checkout(tmp_path / "repo")
    other = "b" * 40
    settings = _settings(tmp_path, monkeypatch, repo)
    system = _HealthSystem(json.dumps({"status": "healthy", "sha": other}))

    check = _build_identity_check(system, settings)

    # A mismatch is a warning: an operator may deliberately run another tree.
    assert check["ok"] is True
    assert check["warn"] is True
    assert check["checkout_sha"] == sha
    assert check["server_sha"] == other
    line = format_doctor({"ok": True, "checks": [check]}).splitlines()[0]
    assert line.startswith("WARN: build_identity")
    assert sha in line and other in line


def test_doctor_does_not_warn_when_the_server_names_no_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _sha = _checkout(tmp_path / "repo")
    settings = _settings(tmp_path, monkeypatch, repo)
    system = _HealthSystem(json.dumps({"status": "healthy", "sha": UNKNOWN_SHA}))

    check = _build_identity_check(system, settings)

    assert check["ok"] is True
    assert check.get("warn") is not True
    assert check["server_sha"] == UNKNOWN_SHA


def test_doctor_does_not_fail_when_the_server_is_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _sha = _checkout(tmp_path / "repo")
    settings = _settings(tmp_path, monkeypatch, repo)
    system = _HealthSystem("", status=0)

    check = _build_identity_check(system, settings)

    assert check["ok"] is True
    assert check.get("warn") is not True
    assert check["server_sha"] == UNKNOWN_SHA


# ---------------------------------------------------------------------------
# A 503 is a readiness answer, and it still names the tree
# ---------------------------------------------------------------------------


def test_http_get_keeps_the_body_of_an_error_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 503 body is an answer, not a failure.

    urllib raises for a non-2xx response, so without this the body -- and
    the tree in it -- was thrown away with the exception.
    """
    from src.conductor_cli import ops_support

    body = json.dumps({"status": "unhealthy", "sha": "c" * 40}).encode()

    def _raise(url: str, timeout: float | None = None) -> None:
        raise _http_error(503, body)

    monkeypatch.setattr(ops_support, "urlopen", _raise)

    code, text = ops_support.RealSystem().http_get(
        "http://127.0.0.1:54399/health", 5
    )

    assert code == 503
    assert json.loads(text)["sha"] == "c" * 40


def test_doctor_compares_the_tree_of_a_server_that_answers_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/health answers 503 when MCP discovery failed, and still names its tree.

    The status gates readiness, not identity: a mismatch hidden behind a
    503 is a mismatch the operator cannot see.
    """
    repo, sha = _checkout(tmp_path / "repo")
    other = "c" * 40
    settings = _settings(tmp_path, monkeypatch, repo)
    system = _HealthSystem(
        json.dumps({"status": "unhealthy", "sha": other}), status=503
    )

    check = _build_identity_check(system, settings)

    assert check["server_sha"] == other
    assert check["checkout_sha"] == sha
    assert check["warn"] is True
    line = format_doctor({"ok": True, "checks": [check]})
    assert line.startswith("WARN: build_identity")
    assert other in line


def test_doctor_compares_the_tree_through_a_real_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole path: a 503 response keeps its body, and the tree in it."""
    from src.conductor_cli import ops_support

    repo, sha = _checkout(tmp_path / "repo")
    other = "c" * 40
    settings = _settings(tmp_path, monkeypatch, repo)
    body = json.dumps({"status": "unhealthy", "sha": other}).encode()

    def _raise(url: str, timeout: float | None = None) -> None:
        raise _http_error(503, body)

    monkeypatch.setattr(ops_support, "urlopen", _raise)

    check = _build_identity_check(ops_support.RealSystem(), settings)

    assert check["server_sha"] == other
    assert check["checkout_sha"] == sha
    assert check["warn"] is True


def test_doctor_does_not_warn_when_a_503_server_runs_this_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, sha = _checkout(tmp_path / "repo")
    settings = _settings(tmp_path, monkeypatch, repo)
    system = _HealthSystem(
        json.dumps({"status": "unhealthy", "sha": sha}), status=503
    )

    check = _build_identity_check(system, settings)

    assert check["server_sha"] == sha
    assert check.get("warn") is not True


def test_doctor_report_includes_the_build_identity_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, sha = _checkout(tmp_path / "repo")
    settings = _settings(tmp_path, monkeypatch, repo)
    system = _HealthSystem(json.dumps({"status": "healthy", "sha": sha}))

    report = doctor_report(system, settings)

    by_name = {
        item["name"]: item
        for item in report["checks"]
        if isinstance(item, dict) and "name" in item
    }
    assert "build_identity" in by_name
    assert by_name["build_identity"]["checkout_sha"] == sha
    assert by_name["build_identity"]["server_sha"] == sha
    assert "build_identity" in format_doctor(report)


# ---------------------------------------------------------------------------
# The server answer is untrusted text
# ---------------------------------------------------------------------------

#: A tree SHA is hex and nothing else. Anything else is not believed.
_NOT_A_SHA = (
    "\x1b[31m\x1b[2Jred\x1b[0m" + "b" * 40,  # terminal escape sequences
    "b" * 40 + "\x07",  # a bell at the end
    "b" * 40 + "\nok: doctor ok",  # a forged line
    "b" * 7,  # a short id cannot be compared with a full one
    "B" * 40,  # git prints lower case
    "",
)


def test_doctor_does_not_believe_a_control_bearing_server_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server answer is untrusted text: it must not reach the terminal raw.

    The server is another process, and its answer is about to be printed.
    A value that is not a tree SHA is not one, so the server is treated as
    unable to say -- and nothing it sent is rendered.
    """
    repo, _sha = _checkout(tmp_path / "repo")
    settings = _settings(tmp_path, monkeypatch, repo)

    for injected in _NOT_A_SHA:
        system = _HealthSystem(json.dumps({"status": "healthy", "sha": injected}))

        check = _build_identity_check(system, settings)

        # Not a tree SHA: the server cannot name its tree, so no mismatch
        # is claimed and nothing it sent survives.
        assert check["server_sha"] == UNKNOWN_SHA, injected
        assert check.get("warn") is not True, injected
        if injected:
            assert injected not in json.dumps(check), injected
        line = format_doctor({"ok": True, "checks": [check]})
        assert "\x1b" not in line, injected
        assert "\x07" not in line, injected


def test_format_doctor_sanitizes_the_text_it_prints() -> None:
    """Defense in depth: no untrusted text reaches the terminal raw.

    The SHA is validated before it is believed, but a detail can come
    from a process, a file or a log line too -- so every rendered line
    goes through the scrubber the rest of the CLI uses.
    """
    check = {
        "name": "build_identity",
        "ok": True,
        "detail": "\x1b[2Jcleared\x1b[0m\nok: a forged line",
    }

    line = format_doctor({"ok": True, "checks": [check]})

    assert "\x1b" not in line
    # A line break in the text cannot become a line of its own.
    assert len(line.splitlines()) == 2
    assert "a forged line" in line
