"""Socket tripwire: unit tests must not reach real network endpoints.

A mock that silently stops intercepting (e.g. after a module move breaks a
patch target) fails OPEN — the test goes green against live I/O. Blocking
socket creation turns that failure mode into a loud error. AF_UNIX stays
allowed: asyncio's event loop self-pipe is a unix socketpair.
"""

import contextlib
from pathlib import Path

import pytest
import pytest_socket


@pytest.fixture(autouse=True)
def _no_real_sockets(request):
    if request.node.get_closest_marker("enable_socket"):
        yield
        return
    pytest_socket.disable_socket(allow_unix_socket=True)
    yield
    pytest_socket.enable_socket()


@pytest.fixture(scope="session")
def _judgment_records_path(tmp_path_factory):
    return tmp_path_factory.mktemp("judgment") / "records.jsonl"


@pytest.fixture(autouse=True)
def _judgment_records_in_tmp(monkeypatch, _judgment_records_path):
    """N61: shadow judgments from any unit test land in a tmp JSONL, never in HOME."""
    monkeypatch.setenv("CONDUCTOR_JUDGMENT_RECORDS", str(_judgment_records_path))


@pytest.fixture(scope="session")
def _shadow_decisions_path(tmp_path_factory):
    return tmp_path_factory.mktemp("shadow-decisions") / "shadow-decisions.jsonl"


@pytest.fixture(autouse=True)
def _shadow_decisions_in_tmp(monkeypatch, _shadow_decisions_path):
    """N58/T4: shadow decision records from any unit test land in tmp, never in HOME."""
    monkeypatch.setenv("CONDUCTOR_SHADOW_LOG_PATH", str(_shadow_decisions_path))


@pytest.fixture(scope="session")
def _codex_homes_path(tmp_path_factory):
    return tmp_path_factory.mktemp("codex-homes")


@pytest.fixture(autouse=True)
def _codex_homes_in_tmp(monkeypatch, _codex_homes_path):
    """P0: isolated reviewer/worker CODEX_HOMEs from any unit test land in tmp, never in HOME."""
    import os

    os.chmod(_codex_homes_path, 0o700)
    monkeypatch.setenv("CONDUCTOR_CODEX_HOMES_DIR", str(_codex_homes_path))


@pytest.fixture(autouse=True)
def _no_real_memfw(monkeypatch):
    """M2c: a quota handoff in any unit test never runs the real memfw CLI."""
    monkeypatch.setenv("CONDUCTOR_MEMFW_BIN", "")


@pytest.fixture(autouse=True)
def _reader_pool_follows_cache_client(monkeypatch):
    """Alias the dedicated stream-reader pool onto the injected cache client.

    Blocking XREADs moved off the cache pool in production, but unit tests
    inject their fakes through ``get_cache_client``. Without this the readers
    would build a real ``BlockingConnectionPool`` and block inside XREAD; the
    accessor deliberately has no cache fallback of its own.

    One seam, and it follows whichever client the caller resolved, because the
    accessor is handed that client rather than looking one up. A test wanting
    real reader-pool behavior overrides this single symbol.
    """
    from src.utils.cache import stream_pool

    async def _reader(cache):
        return getattr(cache, "client", None)

    monkeypatch.setattr(stream_pool, "get_stream_reader_client", _reader)


@pytest.fixture(autouse=True)
def _empty_user_skill_bundle(monkeypatch):
    """Stub the per-turn user-skill bundle to empty for unit tests.

    ``resolve_llm_config`` loads it from Postgres on every turn; unit tests
    have no pool. Both binding sites are patched: the package attribute the
    lazy imports resolve, and the defining module's global that
    ``sandbox_skill_sync_params`` calls. Tests *about* the bundle itself put
    the real function back — see ``server/services/user_skills/conftest.py``.
    """
    from src.server.services import user_skills
    from src.server.services.user_skills import materialize

    async def _empty(user_id, workspace_id=None):
        return user_skills.EMPTY_USER_SKILL_BUNDLE

    monkeypatch.setattr(user_skills, "load_user_skill_bundle", _empty)
    monkeypatch.setattr(materialize, "load_user_skill_bundle", _empty)


@pytest.fixture(autouse=True)
def _no_user_subagents(monkeypatch):
    """Stub each user's subagent rows, and their plugins' subagents, to none
    for unit tests.

    ``resolve_llm_config`` reads them from Postgres on every PTC turn, and
    unit tests have no pool. A user with no rows is also the case whose turn
    must match a build that never read any.
    """
    from src.server.database import plugin_subagents, user_subagents

    async def _none(user_id, *, conn=None):
        return []

    @contextlib.asynccontextmanager
    async def _no_snapshot():
        yield None

    monkeypatch.setattr(user_subagents, "list_user_subagents", _none)
    monkeypatch.setattr(plugin_subagents, "list_user_plugin_subagents", _none)
    monkeypatch.setattr(plugin_subagents, "subagents_snapshot", _no_snapshot)


@pytest.fixture
def isolated_daemon_home(tmp_path, monkeypatch):
    """Point every home-derived local-daemon path at a private, resolved HOME.

    ``daemon_policy`` and ``daemon_grant`` compute their defaults from
    ``Path.home()`` at import, so a developer's real
    ``~/.config/conductor/daemon_policy.yaml`` / ``daemon_trusted_keys.json``
    would otherwise decide what ``main_async`` loads. The Git delivery lock
    and probe roots read ``Path.home()`` at call time and refuse a root with a
    symlinked ancestor, so the fake HOME is resolved (macOS ``/var`` and
    ``/tmp`` are symlinks) or every Git baseline reads as unavailable.
    """
    from src.conductor_cli import daemon_grant, daemon_policy

    home = tmp_path.resolve() / "home"
    config = home / ".config"
    cache = home / ".cache"
    state = home / ".local" / "state"
    for directory in (config / "conductor", cache, state):
        directory.mkdir(parents=True, mode=0o700)
    gitconfig = home / ".gitconfig"
    gitconfig.write_text("")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv(
        "CONDUCTOR_WORKER_LOCK_ROOT",
        str(cache / "conductor" / "worker-workspace-locks"),
    )
    monkeypatch.setenv(
        "CONDUCTOR_WORKER_ISOLATION_ROOT",
        str(cache / "conductor" / "native-cli" / "git-probe"),
    )
    for name in ("CONDUCTOR_DAEMON_POLICY", "CONDUCTOR_DAEMON_TRUSTED_KEYS"):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setattr(
        daemon_policy,
        "DEFAULT_POLICY_PATH",
        config / "conductor" / "daemon_policy.yaml",
    )
    monkeypatch.setattr(
        daemon_grant,
        "DEFAULT_TRUSTED_KEYS",
        config / "conductor" / "daemon_trusted_keys.json",
    )
    monkeypatch.setattr(
        daemon_grant,
        "DEFAULT_NONCE_FILE",
        state / "conductor" / "daemon-grant-nonces.json",
    )
    return home


@pytest.fixture
def daemon_claims_have_completion_contract(monkeypatch):
    """Daemon test claims stand for a current server, which echoes the contract.

    A ticket dict without ``completion_contract`` is read as a current claim;
    a test of the mixed-deploy path sets it to 0 explicitly.
    """
    from scripts import conductor_local_daemon as daemon

    monkeypatch.setattr(
        daemon,
        "_claim_has_completion_contract",
        lambda ticket: int(ticket.get("completion_contract", 1) or 0) >= 1,
    )


def completion_report_json(
    status: str = "done",
    summary: str = "Implemented.",
    *,
    next_step: str = "",
    commits: tuple[str, ...] = (),
) -> str:
    """A valid native Worker completion report, as its final message."""
    import json

    return json.dumps(
        {
            "status": status,
            "summary": summary,
            "artifacts": {"commits": list(commits), "files": [], "pr": None},
            "verification": [],
            "next": next_step,
            "handbacks": [],
        }
    )


def assert_daemon_home_isolated(home: Path) -> None:
    """Shared proof that the local daemon's home-derived defaults are private."""
    import os

    from src.conductor_cli import daemon_grant, daemon_policy
    from src.server.services.autonomy.git_delivery import WorkspaceDeliveryLock

    real_home = Path(os.path.expanduser("~")).resolve()
    assert real_home == home
    assert Path.home() == home
    for path in (
        daemon_policy.DEFAULT_POLICY_PATH,
        daemon_grant.DEFAULT_TRUSTED_KEYS,
        daemon_grant.DEFAULT_NONCE_FILE,
        WorkspaceDeliveryLock(str(home)).lock_root,
    ):
        assert path.is_relative_to(home), path
        # A symlinked ancestor makes the Git delivery lock refuse to run.
        assert path == path.resolve(), path


@pytest.fixture(scope="session")
def shipped_skill_md():
    """Locate a shipped skill's SKILL.md on disk, by name.

    A registry entry's ``skill_md_path`` is where the *agent* reaches the file
    inside a sandbox, not where this repo keeps it: the shipped skills live in
    the bundle that declares them, and the sync flattens every source into one
    directory. Joining that suffix onto the repo root reads as correct and is
    not, so the lookup asks the bundles instead.
    """
    from src.config.runtime.plugins import bundled_skill_dirs

    def find(name: str) -> Path:
        for root in bundled_skill_dirs():
            candidate = root / name / "SKILL.md"
            if candidate.is_file():
                return candidate
        raise AssertionError(f"no bundle ships a {name!r} skill")

    return find


# Refactor rollout flags are a developer/local .env concern, not a test input:
# the flag-off contracts below assume "unset", so a local .env that enables the
# rollout must not leak into the suite. Tests that need a flag on set it in
# their own body/fixture (after this autouse default), so both directions stay
# explicit.
_REFACTOR_FLAGS = (
    "REFACTOR_GOALS_ENABLED",
    "REFACTOR_EVENTS_ENABLED",
    "REFACTOR_MEMORY_INDEX_ENABLED",
    "REFACTOR_TASK_TREE_ENABLED",
    "REFACTOR_PERMISSIONS_ENABLED",
    "REFACTOR_SUBAGENT_CONTRACT_ENABLED",
)


@pytest.fixture(autouse=True)
def _refactor_flags_unset(monkeypatch):
    for flag in _REFACTOR_FLAGS:
        monkeypatch.delenv(flag, raising=False)



@pytest.fixture(autouse=True)
def _no_real_claude_planner(monkeypatch):
    """The Claude Code dispatch planner only ever starts an allowlisted fake.

    A live ``DispatchService`` drive defaults to the Claude Code planner, whose
    binary is the ``claude`` on PATH. The planner's spawn seam is replaced by a
    guard that starts a binary only when its resolved absolute path is in the
    set this fixture returns; a test adds its fake script there by hand. Every
    other binary (PATH lookup, an absolute install path, anything) is refused
    as if it were missing, so the drive parks ``planner_failed`` instead of
    spending a model call.
    """
    from src.tools.auto_dispatch import claude_planner

    allowed: set[Path] = set()
    real_spawn = claude_planner._spawn

    async def _guarded(*argv, **kwargs):
        binary = Path(argv[0])
        if not binary.is_absolute() or binary.resolve() not in allowed:
            raise FileNotFoundError("only an allowlisted fake claude runs in unit tests")
        return await real_spawn(*argv, **kwargs)

    monkeypatch.setattr(claude_planner, "_spawn", _guarded)
    return allowed


@pytest.fixture(autouse=True)
def _no_real_codexbar_snapshot(monkeypatch, tmp_path):
    """N30: the cost ledger's quota read never opens the operator's snapshot.

    The ledger is on by default, so every Stage-Gate machine test reads a
    CodexBar quota at launch. Point the call-time path at a file that does
    not exist; tests about the reading pass ``path=`` explicitly.
    """
    from src.tools.auto_dispatch import cost

    missing = tmp_path / "no-snapshot.json"
    monkeypatch.setattr(cost, "default_snapshot_path", lambda: missing)


@pytest.fixture(autouse=True)
def _reset_role_registry_provider():
    """R6: a test that installs a stored role registry must not leak it.

    ``role_types._provider`` is process-wide; a leaked provider changes
    ``load_role_policy`` for every later test.
    """
    from src.server.services.autonomy import role_types

    role_types.set_registry_provider(None)
    yield
    role_types.set_registry_provider(None)
