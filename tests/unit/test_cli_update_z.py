"""`cond up` reports a missing worker CLI as not installed, and step has no personal path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.conductor_cli import ops_cli_update
from src.conductor_cli.ops_cli import main
from src.conductor_cli.ops_cli_update import update_clis
from src.conductor_cli.ops_runtime import prepare
from src.conductor_cli.ops_support import Completed, OpsError
from tests.unit.test_cond_ops import FakeSystem, _layout


class Missing(FakeSystem):
    """These binaries do not exist: starting them raises like RealSystem.run does."""

    def __init__(self, names: set[str]) -> None:
        super().__init__()
        self.names = names

    def run(self, argv, *, cwd=None, env=None, timeout=None):  # noqa: ANN001
        name = Path(argv[0]).name
        if name in self.names:
            self.commands.append(list(argv))
            raise OpsError(f"could not run {argv[0]}: FileNotFoundError")
        return super().run(argv, cwd=cwd, env=env, timeout=timeout)


def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return _layout(tmp_path.resolve(), monkeypatch)


def test_missing_cli_is_not_installed_not_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home(tmp_path, monkeypatch)
    fake = Missing({"grok"})
    system, settings, _fleet = prepare(fake)
    rows = update_clis(system, settings, only="grok", force=True)
    assert [row["result"] for row in rows] == ["not-installed"]
    assert rows[0]["detail"] == "not installed"
    # Nothing was installed on its behalf.
    assert not any(argv[:2] == ["npm", "i"] for argv in fake.commands)
    # Not counted as broken: the provider's daemon is not held down by it.
    assert not ops_cli_update.daemon_is_blocked("grok", rows, settings)
    assert "grok" not in ops_cli_update.persisted_health(settings)


def test_cli_update_exit_code_ignores_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _home(tmp_path, monkeypatch)
    fake = Missing({"grok"})
    assert main(["cli", "update", "--only", "grok", "--force"], system=fake) == 0
    assert "grok: not-installed" in capsys.readouterr().out


def test_other_probe_errors_still_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home(tmp_path, monkeypatch)

    class Denied(FakeSystem):
        def run(self, argv, *, cwd=None, env=None, timeout=None):  # noqa: ANN001
            if Path(argv[0]).name == "grok":
                raise OpsError("could not run grok: PermissionError")
            return super().run(argv, cwd=cwd, env=env, timeout=timeout)

    system, settings, _fleet = prepare(Denied())
    rows = update_clis(system, settings, only="grok", force=True)
    assert rows[0]["result"] == "failed"


def test_installed_cli_still_updates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home(tmp_path, monkeypatch)
    fake = Missing({"dsh"})
    system, settings, _fleet = prepare(fake)
    rows = update_clis(system, settings, only="grok", force=True)
    assert rows[0]["result"] == "updated"
    assert ["npm", "i", "-g", "@xai-official/grok@latest"] in fake.commands


def test_step_default_has_no_personal_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home(tmp_path, monkeypatch)
    system, settings, _fleet = prepare(FakeSystem())
    specs, _interval = ops_cli_update.load_specs(settings)
    step = next(spec for spec in specs if spec.name == "step")
    assert step.binary == "step"
    assert step.update == ("step", "update")
    blob = json.dumps([spec.__dict__ for spec in specs])
    assert "leo-farm" not in blob
    assert "leo-farm" not in Path(ops_cli_update.__file__).read_text(encoding="utf-8")


def test_step_path_override_still_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path, monkeypatch)
    custom = home / "tools" / "step"
    (home / ".conductor" / "clis.toml").write_text(
        f'[step]\npath = "{custom}"\n', encoding="utf-8"
    )

    class Records(FakeSystem):
        def run(self, argv, *, cwd=None, env=None, timeout=None):  # noqa: ANN001
            if argv[0] == str(custom):
                self.commands.append(list(argv))
                return Completed(0, "1.0.0\n", "")
            return super().run(argv, cwd=cwd, env=env, timeout=timeout)

    fake = Records()
    system, settings, _fleet = prepare(fake)
    rows = update_clis(system, settings, only="step", force=True)
    assert rows[0]["result"] == "updated"
    assert [str(custom), "update"] in fake.commands
