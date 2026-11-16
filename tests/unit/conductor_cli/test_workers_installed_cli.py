"""``conductor workers`` as an installed program sees it.

The wheel that ships ``conductor`` contains ``conductor_cli`` and not
``src.llms``, so a command that imports the native-CLI layer dies with
``ModuleNotFoundError`` -- or keeps running with no data at all -- as soon
as it is run from anywhere but a checkout.

These tests run the **installed** program: the ``conductor`` script of the
running virtualenv, from a working directory that holds no ``src`` package,
with no ``PYTHONPATH`` and a ``HOME`` of its own. That is what a user's
machine looks like.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

#: The env var the compat configuration points at, and the secret in it.
ENV_NAME = "W9A_TEST_API_KEY"
SENTINEL = "sk-" "w7a-test-only-sentinel"

#: The name of the compat worker the configuration holds.
COMPAT_WORKER = "qwen-compat"


def _conductor() -> Path:
    """The ``conductor`` script of the virtualenv the tests run in."""
    executable = Path(sys.executable).with_name("conductor")
    assert executable.exists(), f"no conductor script next to {sys.executable}"
    return executable


def _compat_config(home: Path) -> Path:
    """A compat worker configuration that names a secret."""
    path = home / ".config" / "conductor" / "compat-workers.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "workers": [
                    {
                        "name": COMPAT_WORKER,
                        "protocol": "openai-completions",
                        "base_url": "https://example.invalid/v1",
                        "model": "w7a-fake-model",
                        "api_key_env": ENV_NAME,
                        "context_window_tokens": 131072,
                        "review_capable": False,
                    }
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _run(home: Path, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the installed ``conductor`` the way a user's shell runs it."""
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", ""),
        ENV_NAME: SENTINEL,
    }
    # No PYTHONPATH: a checkout must not be able to lend the command the
    # modules the wheel does not ship.
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [str(_conductor()), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd),
        timeout=60,
        check=False,
    )


def test_the_installed_command_runs_outside_a_checkout(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _compat_config(home)

    completed = _run(home, tmp_path, "workers", "--json")

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert isinstance(payload, dict)
    assert isinstance(payload.get("workers"), list)


def test_the_installed_command_never_prints_the_compat_secret(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _compat_config(home)

    completed = _run(home, tmp_path, "workers", "--json")

    assert completed.returncode == 0, completed.stderr
    assert SENTINEL not in completed.stdout
    assert SENTINEL not in completed.stderr


def test_the_installed_command_runs_without_the_json_flag(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _compat_config(home)

    completed = _run(home, tmp_path, "workers")

    assert completed.returncode == 0, completed.stderr
    assert SENTINEL not in completed.stdout
    assert SENTINEL not in completed.stderr
    assert completed.stdout.strip()


def test_compat_workers_are_not_part_of_the_session_statistics(tmp_path: Path) -> None:
    """A compat worker is not a session, so it is not a row of this listing.

    ``conductor workers`` reports the health of **sessions**: how many a
    worker has, how much of its context window the current one has used,
    when it failed, how often it rotated and whether it is out of quota.
    A compat worker is a configured adapter -- it has a name, a protocol
    and a model, and no session until a turn is run through it -- and its
    configuration is read by ``src.config.compat``, which the wheel that
    ships this command does not ship either. So the listing stays a listing
    of sessions, and a compat worker does not appear in it.

    This is one half of a choice the PRD for this round left open: the
    other half would be a row of its own for every compat worker. The
    reason for this half is written down in ``docs/WORKER-HEALTH.md``.
    """
    home = tmp_path / "home"
    home.mkdir()
    _compat_config(home)

    completed = _run(home, tmp_path, "workers", "--json")

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    providers = [row.get("provider") for row in payload["workers"]]
    assert COMPAT_WORKER not in providers
    assert all(row.get("kind") != "compat" for row in payload["workers"])


def _state_home(home: Path, state: bytes, legacy: dict[str, str]) -> Path:
    """A home directory with a state file and a legacy map in it."""
    native = home / ".cache" / "conductor" / "native-cli"
    native.mkdir(parents=True, exist_ok=True)
    (native / "worker-sessions.json").write_bytes(state)
    (native / "sessions.json").write_text(json.dumps(legacy), encoding="utf-8")
    return home


def test_an_unreadable_state_file_is_unknown_and_not_zero(tmp_path: Path) -> None:
    """A state file nobody could read must not become a healthy worker.

    The command runs for real here -- the installed program, from a
    directory that holds no ``src`` package -- with a state file that is
    broken. The session of the legacy map is still listed, and everything
    about it is **unknown**: a zero would say the worker never used its
    context and never failed, about a file nobody read.
    """
    home = tmp_path / "home"
    home.mkdir()
    _state_home(home, b"{not json", {"thread:t1:kimi": "s-1"})

    completed = _run(home, tmp_path, "workers", "--json")

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    rows = {row["provider"]: row for row in payload["workers"]}
    assert "kimi" in rows
    row = rows["kimi"]
    assert row["sessions"] == 1
    assert row["context_used_percent"] is None
    assert row["last_failure_class"] is None
    assert row["rotations"] is None


def test_an_oversized_state_file_is_unknown_and_not_zero(tmp_path: Path) -> None:
    """A state file over the size cap is not read, and not invented."""
    home = tmp_path / "home"
    home.mkdir()
    payload = {
        "lines": {
            "thread:t1:kimi": {
                "provider": "kimi",
                "session_id": "s-1",
                "input_tokens": 131072,
                "rotations": 2,
                "last_failure_class": "rate_limited",
            }
        },
        "workers": {"kimi": {"success_seq": 5}},
    }
    blob = json.dumps(payload).encode("utf-8")
    padded = blob[:-1] + b" " * (1_349_181 - len(blob)) + b"}"
    _state_home(home, padded, {"thread:t1:kimi": "s-1"})

    completed = _run(home, tmp_path, "workers", "--json")

    assert completed.returncode == 0, completed.stderr
    rows = {row["provider"]: row for row in json.loads(completed.stdout)["workers"]}
    row = rows["kimi"]
    assert row["sessions"] == 1
    assert row["context_used_percent"] is None
    assert row["last_failure_class"] is None
    assert row["rotations"] is None


def test_the_table_shows_a_dash_for_a_count_nobody_knows(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _state_home(home, b"{not json", {"thread:t1:kimi": "s-1"})

    completed = _run(home, tmp_path, "workers")

    assert completed.returncode == 0, completed.stderr
    line = next(
        line for line in completed.stdout.splitlines() if line.startswith("kimi")
    )
    cells = line.split()
    # provider sessions context failure rotations ...
    assert cells[2] == "unknown"
    assert cells[4] == "-"
