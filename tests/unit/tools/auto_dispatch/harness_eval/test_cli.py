"""The script plans by default and imports the live reviewer lazily."""

from __future__ import annotations

import importlib.util

import pytest

from harness_eval_fakes import REPO_ROOT, build_world


def _main():
    path = REPO_ROOT / "scripts" / "harness_eval.py"
    spec = importlib.util.spec_from_file_location("harness_eval_cli", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


def test_help_documents_the_manual_export(capsys) -> None:
    main = _main()
    with pytest.raises(SystemExit) as caught:
        main(["--help"])
    assert caught.value.code == 0
    text = capsys.readouterr().out
    assert "conductor dispatch status <id> --json" in text
    assert "GET /api/v1/dispatch/{id}/lines" in text


def test_plan_and_run_without_live_write_a_plan(tmp_path, capsys) -> None:
    world = build_world(tmp_path)
    main = _main()
    assert main(["plan", str(world.experiment)]) == 0
    assert "planned_runs: 2" in capsys.readouterr().out
    assert (world.output / "plan.json").is_file()
    assert not (world.output / "repos").exists()

    assert main(["run", str(world.experiment)]) == 0
    assert "planned_runs: 2" in capsys.readouterr().out


def test_live_without_an_exact_confirm_exits_2(tmp_path) -> None:
    world = build_world(tmp_path)
    main = _main()
    assert main(["run", str(world.experiment), "--live"]) == 2
    assert main(["run", str(world.experiment), "--live", "--confirm-runs", "99"]) == 2
    assert not (world.output / "repos").exists()
    assert not (world.output / "worktrees").exists()


def test_report_without_a_plan_exits_2(tmp_path) -> None:
    assert _main()(["report", str(tmp_path / "missing")]) == 2


def test_run_passes_an_explicit_serial_lock_dir(tmp_path, monkeypatch, capsys) -> None:
    path = REPO_ROOT / "scripts" / "harness_eval.py"
    spec = importlib.util.spec_from_file_location("harness_eval_cli_lock", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    seen: dict = {}

    async def fake_execute(experiment, **kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(module, "execute", fake_execute)
    lock = tmp_path / "backend-locks"
    assert module.main(["run", str(tmp_path / "e.json"), "--serial-lock-dir", str(lock)]) == 0
    assert seen["serial_lock_dir"] == lock
    seen.clear()
    assert module.main(["run", str(tmp_path / "e.json")]) == 0
    assert seen["serial_lock_dir"] is None
    with pytest.raises(SystemExit):
        module.main(["run", "--help"])
    assert "backend's launcher environment" in " ".join(capsys.readouterr().out.split())
