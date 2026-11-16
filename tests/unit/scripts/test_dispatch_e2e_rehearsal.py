"""The dev-adapter rehearsal script: success, a dirty root, and a failed step."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.dispatch import e2e_rehearsal as rehearsal

_SCRIPT = Path(rehearsal.__file__).resolve()


def test_rehearsal_approves_and_rejects(tmp_path: Path) -> None:
    before = dict(os.environ)
    root = tmp_path / "rehearsal"
    code = rehearsal.main(["--root", str(root), "--json"])
    assert dict(os.environ) == before
    evidence = json.loads((root / "evidence.json").read_text(encoding="utf-8"))
    text = json.dumps(evidence)
    assert "@" not in text
    home = str(Path.home())
    if home:
        assert home not in text
    for path in (root / "home").glob("*.json"):
        token = json.loads(path.read_text(encoding="utf-8")).get("approval_token")
        if isinstance(token, str) and token:
            assert token not in text
    by_name = {step["name"]: step for step in evidence["steps"]}
    assert code == 0
    assert by_name["start"]["invocation"] == "in_process:drive_dev"
    assert by_name["status"]["invocation"] == "cli"
    assert by_name["approve"]["invocation"] == "in_process:decide"
    assert by_name["start-reject"]["invocation"] == "in_process:drive_dev"
    assert by_name["reject"]["invocation"] == "in_process:decide"
    assert by_name["reject"]["assertion"]["ok"] is True
    assert by_name["start"]["command"] == ["drive_dev", by_name["start"]["run_id"]]
    for name in ("start", "start-reject"):
        gates = by_name[name]["gates"]
        assert gates["command"] == ["python", "-c", "import sys; sys.exit(0)"]
        assert gates["g5_exit_code"] == 0
        assert gates["g2"]
        assert all(item["exit_code"] == 0 for item in gates["g2"])
    assert evidence["proof"] == {
        "adapter": "dev",
        "gates": "on",
        "api": "not-contacted",
        "launcher": "recording",
    }
    for name in ("start", "status", "approve", "start-reject", "reject"):
        assert by_name[name]["assertion"]["ok"] is True
        assert by_name[name]["run_id"]
        assert by_name[name]["command"]
        assert isinstance(by_name[name]["duration_ms"], int)


def test_a_non_empty_root_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "busy"
    root.mkdir()
    (root / "keep").write_text("already here", encoding="utf-8")
    assert rehearsal.main(["--root", str(root), "--json"]) == 2
    assert not (root / "evidence.json").exists()


def test_a_failed_step_is_named_and_returns_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected")

    monkeypatch.setattr(rehearsal, "drive_dev", boom)
    root = tmp_path / "fail"
    assert rehearsal.main(["--root", str(root), "--json"]) == 1
    evidence = json.loads((root / "evidence.json").read_text(encoding="utf-8"))
    failed = [step for step in evidence["steps"] if not step["assertion"]["ok"]]
    assert [step["name"] for step in failed] == ["start"]
    assert failed[0]["exit_code"] == 1


def test_a_relative_root_from_another_directory(tmp_path: Path) -> None:
    work = tmp_path / "cwd"
    work.mkdir()
    completed = subprocess.run(
        [sys.executable, str(_SCRIPT), "--root", "relative-confirm", "--json"],
        cwd=work,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    evidence = json.loads((work / "relative-confirm" / "evidence.json").read_text(encoding="utf-8"))
    assert [step["name"] for step in evidence["steps"] if not step["assertion"]["ok"]] == []


def test_fresh_process_stdout_is_json(tmp_path: Path) -> None:
    root = tmp_path / "fresh"
    completed = subprocess.run(
        [sys.executable, str(_SCRIPT), "--root", str(root), "--json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    parsed = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert parsed["steps"][0]["name"] == "start"


def test_prepare_failure_is_a_named_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise rehearsal.StepFailure("git failed")

    monkeypatch.setattr(rehearsal, "seed_repo", boom)
    root = tmp_path / "prep"
    assert rehearsal.main(["--root", str(root), "--json"]) == 1
    evidence = json.loads((root / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["steps"][0]["name"] == "prepare"
    assert evidence["steps"][0]["exit_code"] == 1
    assert evidence["steps"][0]["assertion"]["ok"] is False


def test_scrub_redacts_an_approval_token(tmp_path: Path) -> None:
    root = tmp_path / "tokens"
    home = root / "home"
    home.mkdir(parents=True)
    token = "abc123token"
    (home / "e2e.json").write_text(
        json.dumps({"approval_token": token}),
        encoding="utf-8",
    )
    evidence = {"proof": {}, "steps": [{"assertion": {"detail": f"leak {token}"}}]}
    rehearsal._scrub(evidence, root)
    text = json.dumps(evidence)
    assert token not in text
    assert "[redacted]" in text


def _assert_failed_prepare(parsed: dict[str, object], *, error_type: str) -> None:
    steps = parsed["steps"]
    assert isinstance(steps, list) and steps
    step = steps[0]
    assert isinstance(step, dict)
    assert step["name"] == "prepare"
    assert step["exit_code"] != 0
    assert step["error_type"] == error_type
    assert step["assertion"]["prepare_ok"] is False
    assert step["message"]


def test_missing_git_records_prepare(tmp_path: Path) -> None:
    empty = tmp_path / "empty-path"
    empty.mkdir()
    root = tmp_path / "nogit"
    env = os.environ.copy()
    env["PATH"] = str(empty)
    completed = subprocess.run(
        [sys.executable, str(_SCRIPT), "--root", str(root), "--json"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    parsed = json.loads(completed.stdout)
    assert completed.returncode != 0
    _assert_failed_prepare(parsed, error_type="FileNotFoundError")
    assert (root / "evidence.json").is_file()
    assert str(Path.home()) not in completed.stdout


def test_prepare_oserror_in_a_fresh_process(tmp_path: Path) -> None:
    root = tmp_path / "oserr"
    repo = _SCRIPT.parents[2]
    code = """
import contextlib
import sys
sys.path.insert(0, sys.argv[1])
with contextlib.redirect_stdout(sys.stderr):
    import scripts.dispatch.e2e_rehearsal as rehearsal

def boom(*_args, **_kwargs):
    raise OSError("injected")

rehearsal.seed_repo = boom
raise SystemExit(rehearsal.main(["--root", sys.argv[2], "--json"]))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code, str(repo), str(root)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    parsed = json.loads(completed.stdout)
    assert completed.returncode != 0
    _assert_failed_prepare(parsed, error_type="OSError")
    assert (root / "evidence.json").is_file()


def test_unwritable_root_still_prints_json(tmp_path: Path) -> None:
    root = tmp_path / "locked"
    root.mkdir()
    root.chmod(0o555)
    try:
        completed = subprocess.run(
            [sys.executable, str(_SCRIPT), "--root", str(root), "--json"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
        parsed = json.loads(completed.stdout)
        assert completed.returncode != 0
        _assert_failed_prepare(parsed, error_type="PermissionError")
        assert str(Path.home()) not in completed.stdout
    finally:
        root.chmod(0o755)


def test_systemexit_during_prepare_is_recorded_then_reraised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise SystemExit(3)

    monkeypatch.setattr(rehearsal, "seed_repo", boom)
    root = tmp_path / "exit"
    with pytest.raises(SystemExit) as caught:
        rehearsal.main(["--root", str(root), "--json"])
    assert caught.value.code == 3
    evidence = json.loads((root / "evidence.json").read_text(encoding="utf-8"))
    _assert_failed_prepare(evidence, error_type="SystemExit")
