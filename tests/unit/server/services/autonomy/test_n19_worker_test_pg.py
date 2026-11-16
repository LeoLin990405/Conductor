"""N19: optional throwaway PG for Workers, driven by a fake runner (no real PG)."""

from pathlib import Path

import pytest

from src.server.services.autonomy import worker_test_pg as wpg


class FakeRunner:
    def __init__(self, fail: str | None = None):
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.fail = fail
        self.pwfile_text: str | None = None

    def __call__(self, argv, *, env, timeout):
        argv = list(argv)
        self.calls.append((argv, dict(env)))
        tool = Path(argv[0]).name
        step = argv[-1] if tool == "pg_ctl" else tool
        if tool == "initdb":
            pw = next(a for a in argv if a.startswith("--pwfile="))[len("--pwfile="):]
            self.pwfile_text = Path(pw).read_text()
            Path(argv[argv.index("-D") + 1]).mkdir(parents=True)
        if step == self.fail:
            return wpg.RunResult(1, "", f"{step} exploded\n")
        return wpg.RunResult(0)

    def steps(self):
        return [
            Path(argv[0]).name + (":" + argv[-1] if Path(argv[0]).name == "pg_ctl" else "")
            for argv, _ in self.calls
        ]


@pytest.fixture
def bins(tmp_path):
    bin_dir = tmp_path / "pgbin"
    bin_dir.mkdir()
    for name in ("initdb", "pg_ctl", "createdb"):
        (bin_dir / name).write_text("#!/bin/sh\nexit 99\n")
        (bin_dir / name).chmod(0o755)
    return bin_dir


def test_opt_in_defaults_off_and_treats_garbage_as_off(caplog):
    assert wpg.worker_test_pg_enabled({}) is False
    assert wpg.worker_test_pg_enabled({wpg.TEST_PG_ENV: "off"}) is False
    assert wpg.worker_test_pg_enabled({wpg.TEST_PG_ENV: "ON"}) is True
    with caplog.at_level("WARNING"):
        assert wpg.worker_test_pg_enabled({wpg.TEST_PG_ENV: "maybe"}) is False
    assert wpg.TEST_PG_ENV in caplog.text


def test_disabled_context_starts_nothing(tmp_path, bins, monkeypatch):
    monkeypatch.delenv(wpg.TEST_PG_ENV, raising=False)
    runner = FakeRunner()
    with wpg.throwaway_pg(tmp_path / "pg", ticket_id="abc", runner=runner, bin_dir=bins) as pg:
        assert pg is None
    assert runner.calls == []
    assert not (tmp_path / "pg").exists()


@pytest.mark.parametrize("ticket", ["postgres", "POSTGRES"])
def test_never_a_database_named_postgres(ticket):
    assert wpg.database_name_for(ticket) != "postgres"
    for name in ("postgres", " Postgres ", "template1", "a-b", "x;drop"):
        with pytest.raises(wpg.ThrowawayPgError):
            wpg.require_test_database_name(name)


def test_refuses_the_postgres_role(tmp_path, bins):
    with pytest.raises(wpg.ThrowawayPgError, match="role"):
        wpg.start_throwaway_pg(
            tmp_path / "pg", ticket_id="t", runner=FakeRunner(), bin_dir=bins, user="postgres"
        )


def test_lifecycle_injects_test_db_env_and_cleans_up(tmp_path, bins, monkeypatch):
    monkeypatch.setenv("PGHOST", "prod-db.example")
    monkeypatch.setenv("PGDATABASE", "postgres")
    runner = FakeRunner()
    base = tmp_path / "pg"
    with wpg.throwaway_pg(
        base,
        ticket_id="5f2c9a1e-0000-4000-8000-000000000000",
        runner=runner,
        port_picker=lambda: 54999,
        bin_dir=bins,
        enabled=True,
    ) as pg:
        env = pg.env()
        assert set(env) == set(wpg.TEST_DB_ENV_NAMES)
        assert env["TEST_DB_HOST"] == "127.0.0.1"
        assert env["TEST_DB_PORT"] == "54999"
        assert env["TEST_DB_NAME"] == "conductor_worker_5f2c9a1e"
        assert env["TEST_DB_USER"] == wpg.DEFAULT_USER
        assert len(env["TEST_DB_PASSWORD"]) >= 24
        assert env["TEST_DB_PASSWORD"] not in repr(pg)
        assert runner.pwfile_text == env["TEST_DB_PASSWORD"] + "\n"
        assert not (base / "pwfile").exists()
        assert base.stat().st_mode & 0o077 == 0
    assert runner.steps() == ["initdb", "pg_ctl:start", "createdb", "pg_ctl:stop"]
    assert not base.exists()
    initdb, _ = runner.calls[0]
    assert "--auth=scram-sha-256" in initdb and "--no-instructions" in initdb
    start, _ = runner.calls[1]
    options = start[start.index("-o") + 1]
    assert "listen_addresses=127.0.0.1" in options and "-p 54999" in options
    assert "unix_socket_directories=''" in options
    createdb, createdb_env = runner.calls[2]
    assert createdb[-1] == "conductor_worker_5f2c9a1e"
    assert createdb_env["PGPASSWORD"] == env["TEST_DB_PASSWORD"]
    for _, call_env in runner.calls:
        assert "PGHOST" not in call_env and "PGDATABASE" not in call_env
    stop, _ = runner.calls[3]
    assert stop[stop.index("-m") + 1] == "immediate"


def test_cluster_is_stopped_when_the_worker_run_raises(tmp_path, bins):
    runner = FakeRunner()
    with pytest.raises(RuntimeError, match="worker failed"):
        with wpg.throwaway_pg(
            tmp_path / "pg", ticket_id="t1", runner=runner, port_picker=lambda: 1,
            bin_dir=bins, enabled=True,
        ):
            raise RuntimeError("worker failed")
    assert runner.steps()[-1] == "pg_ctl:stop"
    assert not (tmp_path / "pg").exists()


def test_createdb_failure_stops_the_cluster_and_removes_it(tmp_path, bins):
    runner = FakeRunner(fail="createdb")
    with pytest.raises(wpg.ThrowawayPgError, match="createdb failed .*exploded"):
        with wpg.throwaway_pg(
            tmp_path / "pg", ticket_id="t1", runner=runner, port_picker=lambda: 1,
            bin_dir=bins, enabled=True,
        ):
            pytest.fail("must not yield")
    assert runner.steps() == ["initdb", "pg_ctl:start", "createdb", "pg_ctl:stop"]
    assert not (tmp_path / "pg").exists()


def test_initdb_failure_removes_the_password_file_and_base(tmp_path, bins):
    runner = FakeRunner(fail="initdb")
    with pytest.raises(wpg.ThrowawayPgError, match="initdb"):
        with wpg.throwaway_pg(
            tmp_path / "pg", ticket_id="t1", runner=runner, bin_dir=bins, enabled=True
        ):
            pass
    assert runner.steps() == ["initdb"]
    assert not (tmp_path / "pg").exists()


def test_refuses_a_non_empty_base_dir(tmp_path, bins):
    base = tmp_path / "pg"
    base.mkdir()
    (base / "keep.txt").write_text("x")
    with pytest.raises(wpg.ThrowawayPgError, match="empty"):
        wpg.start_throwaway_pg(base, ticket_id="t", runner=FakeRunner(), bin_dir=bins)
    assert (base / "keep.txt").exists()


def test_missing_binaries_fail_before_running_anything(tmp_path):
    runner = FakeRunner()
    with pytest.raises(wpg.ThrowawayPgError, match="initdb"):
        wpg.start_throwaway_pg(
            tmp_path / "pg", ticket_id="t", runner=runner, bin_dir=tmp_path / "nope"
        )
    assert runner.calls == []


def test_context_never_removes_a_non_empty_base_dir(tmp_path, bins):
    base = tmp_path / "pg"
    base.mkdir()
    (base / "keep.txt").write_text("x")
    with pytest.raises(wpg.ThrowawayPgError, match="empty"):
        with wpg.throwaway_pg(
            base, ticket_id="t", runner=FakeRunner(), bin_dir=bins, enabled=True
        ):
            pass
    assert (base / "keep.txt").exists()


def _pg_env():
    return {
        "TEST_DB_HOST": "127.0.0.1",
        "TEST_DB_PORT": "54999",
        "TEST_DB_NAME": "conductor_worker_t1",
        "TEST_DB_USER": wpg.DEFAULT_USER,
        "TEST_DB_PASSWORD": "synthetic-run-password",
    }


def test_isolation_injects_test_db_env_and_opens_its_port(tmp_path, monkeypatch):
    from src.llms.native_cli import isolation as iso

    work = tmp_path / "workspace"
    work.mkdir()
    monkeypatch.setattr(iso.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(iso.shutil, "which", lambda name: "/usr/bin/sandbox-exec")
    with iso.worker_isolation(
        workspace_id="w",
        root=tmp_path / "state",
        workdir=work,
        env_names=set(),
        localhost_tcp_ports=(6379,),
        test_db_env=_pg_env(),
    ) as isolation:
        argv, env, _ = iso.prepare_process(
            ("/bin/true",), str(work), {"TEST_DB_NAME": "postgres"}
        )
    assert isolation.localhost_tcp_ports == (6379, 54999)
    assert {k: env[k] for k in _pg_env()} == _pg_env()
    assert '(allow network-outbound (remote tcp "localhost:54999"))' in argv[2]
    assert "synthetic-run-password" not in repr(isolation)


def test_isolation_without_test_db_env_passes_no_test_db_vars(tmp_path, monkeypatch):
    from src.llms.native_cli import isolation as iso

    work = tmp_path / "workspace"
    work.mkdir()
    monkeypatch.setenv("TEST_DB_NAME", "postgres")
    monkeypatch.setattr(iso.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(iso.shutil, "which", lambda name: "/usr/bin/sandbox-exec")
    with iso.worker_isolation(
        workspace_id="w", root=tmp_path / "state", workdir=work, env_names=set()
    ):
        _, env, _ = iso.prepare_process(("/bin/true",), str(work), None)
    assert not any(key.startswith("TEST_DB_") for key in env)


@pytest.mark.parametrize(
    "bad",
    [{"PGHOST": "x"}, {"TEST_DB_NAME": ""}, {"TEST_DB_HOST": "127.0.0.1", "TEST_DB_PORT": "x"}],
)
def test_isolation_refuses_other_names_or_bad_values(tmp_path, bad):
    from src.llms.native_cli import isolation as iso

    work = tmp_path / "workspace"
    work.mkdir()
    with pytest.raises(iso.NativeCliUnavailable):
        with iso.worker_isolation(
            workspace_id="w",
            root=tmp_path / "state",
            workdir=work,
            env_names=set(),
            test_db_env=bad,
        ):
            pass
