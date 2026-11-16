"""The live Docker smoke's own exit status and hermeticity.

No Docker is run here: the engine is faked, so these tests pin what the script
decides from what it can and cannot observe. What the smoke observes about a
real engine is the smoke itself (scripts/cloud_worker_smoke.py).
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "cloud_worker_smoke.py"


@pytest.fixture
def smoke(monkeypatch):
    """The script as a module, with its import-time and process-wide effects undone."""
    import structlog

    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")  # restored on teardown
    structlog_config = structlog.get_config()
    spec = importlib.util.spec_from_file_location(
        "cloud_worker_smoke_under_test", SCRIPT
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)

        # Deny by default: a test reaches an engine only through the fake it
        # installs. Without this a regression that lets main() run its
        # scenarios would drive the real Docker from inside pytest.
        def no_real_engine(*_args, **_kwargs):
            raise AssertionError("a smoke unit test reached the real Docker engine")

        monkeypatch.setattr(module.Docker, "run", no_real_engine)
        monkeypatch.setattr(module.Docker, "containers_labelled", no_real_engine)
        yield module
    finally:
        structlog.configure(**structlog_config)


def test_dotenv_stays_disabled_whatever_the_caller_inherited():
    """``setdefault`` kept an inherited ``false``, and importing src.config then read .env."""
    code = (
        "import importlib.util, os, sys\n"
        f"spec = importlib.util.spec_from_file_location('smoke', {str(SCRIPT)!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "print(os.environ['PYTHON_DOTENV_DISABLED'])\n"
    )
    for inherited in ("false", "0", "", "no"):
        env = {**os.environ, "PYTHON_DOTENV_DISABLED": inherited}
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=False,
            env=env,
            cwd=ROOT,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "1", inherited


class _FakeDocker:
    """Stands in for the script's ``Docker`` observer; nothing here runs docker."""

    def __init__(
        self,
        *,
        labelled=None,
        listing_error=None,
        images_rc=0,
        images_out="",
        rm_rc=0,
    ):
        self.labelled = labelled
        self.listing_error = listing_error
        self.images_rc = images_rc
        self.images_out = images_out
        self.rm_rc = rm_rc
        self.calls: list[tuple[str, ...]] = []

    def install(self, monkeypatch, smoke):
        fake = self

        def run(_self, *args, timeout=120.0):
            fake.calls.append(args)
            if args[:1] == ("images",):
                return subprocess.CompletedProcess(
                    args,
                    fake.images_rc,
                    fake.images_out,
                    "boom" if fake.images_rc else "",
                )
            if args[:1] == ("rm",):
                return subprocess.CompletedProcess(args, fake.rm_rc, "", "")
            return subprocess.CompletedProcess(args, 0, "", "")

        def containers_labelled(_self, token, *, label=None, running_only=False):
            if fake.listing_error is not None:
                raise fake.listing_error
            return set(fake.labelled(label) if fake.labelled else ())

        monkeypatch.setattr(smoke.Docker, "run", run)
        monkeypatch.setattr(smoke.Docker, "containers_labelled", containers_labelled)


def _main(smoke, monkeypatch, tmp_path, *extra):
    """``main()`` with the process-wide parts (signals, sockets) and the scenarios stubbed."""

    async def no_scenarios(_self):
        return None

    monkeypatch.setattr(smoke.Smoke, "run", no_scenarios)
    monkeypatch.setattr(smoke, "_forbid_sockets", lambda: None)
    monkeypatch.setattr(smoke.signal, "signal", lambda *_a: None)
    workdir = tmp_path / "work"
    stage = tmp_path / "stage"
    workdir.mkdir()
    stage.mkdir()
    return smoke.main(["--workdir", str(workdir), "--stage-parent", str(stage), *extra])


def test_a_run_whose_cleanup_verifies_exits_zero(smoke, monkeypatch, tmp_path, capsys):
    _FakeDocker().install(monkeypatch, smoke)

    assert _main(smoke, monkeypatch, tmp_path) == 0

    out = capsys.readouterr().out
    assert "0 failed" in out
    assert "FAIL" not in out


def test_a_container_listing_that_errors_is_a_failure_not_an_empty_set(
    smoke, monkeypatch, tmp_path, capsys
):
    """ "docker ps unavailable" used to read as "nothing left behind" and exit 0."""
    _FakeDocker(listing_error=RuntimeError("docker ps unavailable")).install(
        monkeypatch, smoke
    )

    code = _main(smoke, monkeypatch, tmp_path)

    out = capsys.readouterr().out
    assert code != 0
    assert "FAIL  cleanup" in out
    assert "could not" in out and "docker ps unavailable" in out


def test_a_container_that_survives_its_removal_is_a_failure(
    smoke, monkeypatch, tmp_path, capsys
):
    _FakeDocker(
        labelled=lambda label: (
            {"conductor-cloud-worker-aaaa"}
            if label in (None, smoke.RUN_LABEL)
            else set()
        )
    ).install(monkeypatch, smoke)

    code = _main(smoke, monkeypatch, tmp_path)

    assert code != 0
    assert "conductor-cloud-worker-aaaa" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("images_rc", "images_out", "expected"),
    [
        (0, "sha256:abc123\n", "still there"),  # rmi did not remove it
        (1, "", "could not be checked"),  # the engine would not say
    ],
)
def test_a_stub_image_that_is_not_confirmed_gone_is_a_failure(
    smoke, monkeypatch, tmp_path, capsys, images_rc, images_out, expected
):
    _FakeDocker(images_rc=images_rc, images_out=images_out).install(monkeypatch, smoke)

    code = _main(smoke, monkeypatch, tmp_path)

    out = capsys.readouterr().out
    assert code != 0
    assert expected in out
    assert "conductor-cloud-worker-smoke:" in out


def test_a_directory_that_cannot_be_removed_is_a_failure(
    smoke, monkeypatch, tmp_path, capsys
):
    _FakeDocker().install(monkeypatch, smoke)
    monkeypatch.setattr(smoke, "remove_tree", lambda _path: None)

    code = _main(smoke, monkeypatch, tmp_path)

    out = capsys.readouterr().out
    assert code != 0
    assert "is still there" in out


def test_keep_leaves_the_image_and_directories_without_failing(
    smoke, monkeypatch, tmp_path, capsys
):
    fake = _FakeDocker(images_out="sha256:abc123\n")
    fake.install(monkeypatch, smoke)

    code = _main(smoke, monkeypatch, tmp_path, "--keep")

    out = capsys.readouterr().out
    assert code == 0
    assert not any(call[:1] in {("rmi",), ("images",)} for call in fake.calls)
    assert any((tmp_path / "stage").iterdir())  # the run's private root is still there
    # What --keep retains is reported as retained, never as "gone".
    assert "every container, image and directory" not in out
    assert "this run's containers are gone (verified)" in out
    retained = next(line for line in out.splitlines() if "kept by --keep" in line)
    assert retained.startswith("INFO")
    assert "conductor-cloud-worker-smoke:" in retained  # the stub image
    assert str(tmp_path / "stage") in retained  # the staging root's parent, by path
    assert "gone" not in retained


def test_without_keep_everything_is_reported_gone_and_nothing_retained(
    smoke, monkeypatch, tmp_path, capsys
):
    _FakeDocker().install(monkeypatch, smoke)

    assert _main(smoke, monkeypatch, tmp_path) == 0

    out = capsys.readouterr().out
    assert "every container, image and directory of this run is gone (verified)" in out
    assert "kept by --keep" not in out


def test_no_expiry_sweep_reaches_docker_unless_it_is_scoped_to_the_run(
    smoke, monkeypatch, tmp_path
):
    """The backstop under the per-scenario scope: an unscoped sweep is refused unrun."""
    args = smoke.parse_args([])
    args.workdir = tmp_path / "work"
    args.stage_root = tmp_path / "stage"
    run = smoke.Smoke(args)
    rt = run.rt
    commands: list[tuple[str, ...]] = []

    def engine(argv, *, timeout_seconds=30.0):
        commands.append(tuple(argv))
        return rt.DockerCommandResult(argv=tuple(argv), returncode=0, stdout="")

    monkeypatch.setattr(rt, "_reap_expired", rt._reap_expired)  # restored afterwards
    run.guard_sweeps()
    base = rt.DockerWorkerSettings.from_env({})
    unscoped = replace(base, runner=engine)
    scoped = replace(base, runner=engine, sweep_label=f"{smoke.RUN_LABEL}={run.token}")
    monkeypatch.setattr(rt, "_resolve_docker_bin", lambda _settings: "docker")

    with pytest.raises(RuntimeError, match="not scoped"):
        rt.reap_expired_containers(unscoped)
    assert run.refused_sweeps == 1
    assert commands == []  # refused before anything was listed

    assert rt.reap_expired_containers(scoped) == []
    assert run.refused_sweeps == 1
    assert commands and commands[0][1:3] == ("ps", "-a")


def test_directories_are_left_in_place_when_the_containers_are_not_confirmed_gone(
    smoke, monkeypatch, tmp_path, capsys
):
    """No tree is repaired or deleted while a container of the run may be alive."""
    _FakeDocker(
        labelled=lambda label: (
            {"conductor-cloud-worker-aaaa"}
            if label in (None, smoke.RUN_LABEL)
            else set()
        )
    ).install(monkeypatch, smoke)
    touched: list[Path] = []
    monkeypatch.setattr(smoke, "remove_tree", lambda path: touched.append(path))

    code = _main(smoke, monkeypatch, tmp_path)

    out = capsys.readouterr().out
    assert code != 0
    assert touched == []
    assert "left in place" in out
    assert list((tmp_path / "stage").iterdir()), "the run's staging root was deleted"


def test_directories_are_left_in_place_when_the_containers_cannot_be_listed(
    smoke, monkeypatch, tmp_path, capsys
):
    _FakeDocker(listing_error=RuntimeError("docker ps unavailable")).install(
        monkeypatch, smoke
    )
    touched: list[Path] = []
    monkeypatch.setattr(smoke, "remove_tree", lambda path: touched.append(path))

    code = _main(smoke, monkeypatch, tmp_path)

    assert code != 0
    assert touched == []
    assert "left in place" in capsys.readouterr().out
    assert list((tmp_path / "stage").iterdir())


def test_remove_tree_is_the_runtimes_own_walk(smoke, tmp_path):
    """One implementation: a hostile tree goes, and a link swapped in mid-walk is not followed."""
    import os
    import stat

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("host data")
    outside.chmod(0o500)
    tree = tmp_path / "tree"
    (tree / "trap" / "inner").mkdir(parents=True)
    (tree / "trap" / "inner" / "f").write_text("x")
    victim = tree / "victim"
    victim.mkdir()
    (victim / "f").write_text("x")
    for path in (tree / "trap" / "inner", tree / "trap", victim):
        path.chmod(0)
    real_open, swapped = os.open, []

    def opening(path, flags, mode=0o777, *, dir_fd=None):
        try:
            return real_open(path, flags, mode, dir_fd=dir_fd)
        finally:
            # The Worker wins the race right after the walk first meets the directory.
            if path == "victim" and not swapped:
                swapped.append(True)
                real_chmod = os.chmod
                real_chmod(victim, 0o700)
                (victim / "f").unlink()
                victim.rmdir()
                victim.symlink_to(outside)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "open", opening)
        try:
            assert smoke.remove_tree(tree) is True
        finally:
            outside.chmod(0o700)

    assert swapped, "the race was never exercised"
    assert not tree.exists()
    assert (outside / "keep.txt").read_text() == "host data"
    assert (
        stat.S_IMODE(outside.stat().st_mode) == 0o700
    )  # restored by this test, not the walk


def _ps(returncode=0, stdout="", raises=None):
    def run(argv, **_kwargs):
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    return run


_LAUNCH = (
    "docker run --label conductor.cloud-worker-smoke=run1111 "
    "--name conductor-cloud-worker-0123 image codex exec"
)
#: What this run's launch must show: its unique label value and container name.
_WITNESS = ["conductor.cloud-worker-smoke=run1111", "conductor-cloud-worker-0123"]


@pytest.mark.parametrize(
    ("run", "reason"),
    [
        (_ps(1, ""), "failed or listed nothing"),  # ps failed: nothing observed
        (_ps(1, _LAUNCH), "failed or listed nothing"),
        (_ps(0, "  \n"), "failed or listed nothing"),
        (_ps(raises=FileNotFoundError("ps")), "could not be run"),
        (_ps(0, "/bin/zsh\n/usr/bin/python"), "did not show the launch"),
        (_ps(0, _LAUNCH + " SECRET-FORWARDED"), "appears in the process arguments"),
    ],
)
def test_a_ps_that_observed_nothing_does_not_pass_the_absence_check(smoke, run, reason):
    """An empty or failed listing makes any value look absent; it must fail instead."""
    ok, detail = smoke.process_arguments_hide(
        ["SECRET-FORWARDED", "SECRET-LITERAL"],
        expect=_WITNESS,
        run=run,
    )

    assert ok is False
    assert reason in detail
    assert "SECRET" not in detail  # the values are never printed


def test_a_ps_that_shows_the_launch_and_neither_value_passes(smoke):
    ok, detail = smoke.process_arguments_hide(
        ["SECRET-FORWARDED"],
        expect=_WITNESS,
        run=_ps(0, _LAUNCH),
    )

    assert ok is True
    assert "SECRET" not in detail


@pytest.mark.parametrize(
    "listing",
    [
        # another run's launch: same prefix, a different container and label
        "docker run --label conductor.cloud-worker-smoke=other999 "
        "--name conductor-cloud-worker-ffff image codex exec",
        # this run's label but not its container, and the reverse
        "docker run --label conductor.cloud-worker-smoke=run1111 image codex",
        "docker run --name conductor-cloud-worker-0123 image codex",
    ],
)
def test_the_witness_is_this_runs_launch_not_any_workers_launch(smoke, listing):
    """A listing that shows somebody else's Worker observed nothing of ours."""
    ok, detail = smoke.process_arguments_hide(
        ["SECRET-FORWARDED"], expect=_WITNESS, run=_ps(0, listing)
    )

    assert ok is False
    assert "did not show the launch" in detail


def test_a_missing_default_staging_parent_is_refused_not_created(
    smoke, monkeypatch, tmp_path
):
    """The run leaves nothing behind, so it does not create ~/.cache/conductor."""
    home = tmp_path / "home"
    home.mkdir()
    workdir = tmp_path / "work"
    workdir.mkdir()
    # Never the real engine, even if the refusal regresses and the run goes on.
    _FakeDocker().install(monkeypatch, smoke)

    async def no_scenarios(_self):
        return None

    monkeypatch.setattr(smoke.Smoke, "run", no_scenarios)
    monkeypatch.setattr(smoke.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(smoke, "_forbid_sockets", lambda: None)
    monkeypatch.setattr(smoke.signal, "signal", lambda *_a: None)

    with pytest.raises(SystemExit) as refused:
        smoke.main(["--workdir", str(workdir)])

    message = str(refused.value)
    assert str(home / ".cache" / "conductor") in message
    assert "--stage-parent" in message
    assert list(home.iterdir()) == []  # not even ~/.cache
    assert list(workdir.iterdir()) == []  # the scratch directory made first is gone


def test_an_existing_default_staging_parent_is_used_as_it_is(
    smoke, monkeypatch, tmp_path
):
    home = tmp_path / "home"
    parent = home / ".cache" / "conductor"
    parent.mkdir(parents=True)
    (parent / "native-cli").mkdir()
    workdir = tmp_path / "work"
    workdir.mkdir()
    _FakeDocker().install(monkeypatch, smoke)
    monkeypatch.setattr(smoke.Path, "home", classmethod(lambda cls: home))

    async def no_scenarios(_self):
        return None

    monkeypatch.setattr(smoke.Smoke, "run", no_scenarios)
    monkeypatch.setattr(smoke, "_forbid_sockets", lambda: None)
    monkeypatch.setattr(smoke.signal, "signal", lambda *_a: None)

    assert smoke.main(["--workdir", str(workdir)]) == 0

    assert [p.name for p in parent.iterdir()] == [
        "native-cli"
    ]  # untouched, nothing left
