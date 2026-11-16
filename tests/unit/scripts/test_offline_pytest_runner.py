import shutil
import subprocess
from pathlib import Path


RUNNER = Path(__file__).resolve().parents[3] / "scripts/tests/run_offline_pytest.sh"


def _copy_runner(repo: Path) -> Path:
    runner = repo / "scripts/tests/run_offline_pytest.sh"
    runner.parent.mkdir(parents=True)
    shutil.copy2(RUNNER, runner)
    return runner


def test_runner_fails_closed_without_repo_interpreter(tmp_path: Path) -> None:
    runner = _copy_runner(tmp_path / "repo")

    result = subprocess.run(
        ["bash", str(runner)],
        env={"PATH": "/usr/bin:/bin", "OFFLINE_TEST_PYTHON": ""},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "Create .venv or set OFFLINE_TEST_PYTHON" in result.stderr
    assert "deepseek-harness" not in result.stderr


def test_runner_uses_explicit_interpreter_with_isolated_offline_environment(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    runner = _copy_runner(repo)
    capture = tmp_path / "capture"
    fake_python = tmp_path / "fake-python"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_python.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\0' \"$@\" > '{capture.with_suffix('.args')}'\n"
        f"env > '{capture.with_suffix('.env')}'\n"
    )
    fake_python.chmod(0o755)
    fake_uv.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\0' \"$@\" > '{capture.with_suffix('.uv-args')}'\n"
        "while [ \"$#\" -gt 0 ] && [ \"$1\" != \"--\" ]; do shift; done\n"
        "[ \"$#\" -gt 0 ] || exit 91\n"
        "shift\n"
        "[ \"$1\" = python ] || exit 92\n"
        "shift\n"
        f"exec '{fake_python}' \"$@\"\n"
    )
    fake_uv.chmod(0o755)

    inherited_env = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "OFFLINE_TEST_PYTHON": str(fake_python),
        "SHOULD_NOT_LEAK": "secret-marker",
    }
    result = subprocess.run(
        ["bash", str(runner), "tests/unit/example.py"],
        env=inherited_env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    uv_args = (
        capture.with_suffix(".uv-args").read_bytes().decode().rstrip("\0").split("\0")
    )
    assert uv_args[:5] == [
        "run",
        "--offline",
        "--no-sync",
        "--python",
        str(fake_python),
    ]
    assert uv_args[5:7] == ["--", "python"]
    args = capture.with_suffix(".args").read_bytes().decode().rstrip("\0").split("\0")
    environment = dict(
        line.split("=", 1) for line in capture.with_suffix(".env").read_text().splitlines()
    )
    assert args[0] == "-c"
    assert "socket.socket.connect = reject_network" in args[1]
    assert "socket.create_connection = reject_network" in args[1]
    assert "dotenv.load_dotenv" in args[1]
    assert args[-1] == "tests/unit/example.py"
    assert environment["PYTHON_DOTENV_DISABLED"] == "1"
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
    assert environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert environment["PYTHONPATH"] == str(repo)
    assert Path(environment["HOME"]).is_relative_to(repo)
    assert Path(environment["TMPDIR"]).is_relative_to(Path(environment["HOME"]))
    assert "SHOULD_NOT_LEAK" not in environment
    assert "OFFLINE_TEST_PYTHON" not in environment
    assert not Path(environment["HOME"]).exists()
