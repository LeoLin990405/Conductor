"""P0: a Worker-role Codex app-server run never loads the operator's memories.

Every native Codex thread used to start ``codex app-server`` under the
operator's real ``~/.codex``: memories, global AGENTS.md and config MCP
servers. A Worker (Ticket) run now turns the memories feature off and gets an
isolated CODEX_HOME that keeps the login link and drops the rest. Every home
here is synthetic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.server.services.native_runtime import codex_app_server as mod


def _operator_home(root: Path) -> Path:
    home = root / "operator-codex"
    (home / "memories").mkdir(parents=True)
    (home / "memories" / "memory_summary.md").write_text("private topics\n")
    (home / "AGENTS.md").write_text("Always do Fleet Memory lookups.\n")
    (home / "auth.json").write_text('{"synthetic": "not-a-credential"}\n')
    (home / "config.toml").write_text(
        '[features]\nmemories = true\n\n[mcp_servers.fleet-memory]\ncommand = "x"\n'
    )
    return home


class _FakeClient:
    instances: list["_FakeClient"] = []

    def __init__(self, argv, *, cwd=None, env=None):
        self.argv = list(argv)
        self.env = dict(env or {})
        self.isolated_home = None
        self.home_seen: dict[str, object] = {}
        _FakeClient.instances.append(self)

    async def start(self):
        home = self.env.get("CODEX_HOME")
        if home:
            self.home_seen = {
                "entries": sorted(p.name for p in Path(home).iterdir()),
                "memories": (Path(home) / "memories").exists(),
                "agents": (Path(home) / "AGENTS.md").exists(),
            }

    async def request(self, method, params=None, timeout=None):
        return {}

    async def notify(self, method, params=None):
        return None

    async def close(self):
        # The real client removes the home on close; mirror its contract.
        if self.isolated_home is not None:
            from src.llms.native_cli.personal_memory import remove_isolated_codex_home

            remove_isolated_codex_home(self.isolated_home)
            self.isolated_home = None


@pytest.fixture
def fake_client(monkeypatch, tmp_path):
    _FakeClient.instances = []
    monkeypatch.setattr(mod, "_AppServerClient", _FakeClient)
    monkeypatch.setenv("CODEX_HOME", str(_operator_home(tmp_path)))
    monkeypatch.setenv("NATIVE_CLI_CODEX_BIN", "/bin/false")
    return _FakeClient


def _has_override(argv, key, value) -> bool:
    return any(argv[i] == "-c" and argv[i + 1] == f"{key}={value}" for i in range(len(argv) - 1))


@pytest.mark.asyncio
async def test_a_worker_app_server_run_has_memories_off_and_an_isolated_home(fake_client):
    runtime = mod.CodexAppServerRuntime(isolate_user_instructions=True)
    await runtime._ensure_client()
    client = fake_client.instances[-1]

    for key in ("features.memories", "memories.use_memories", "memories.generate_memories"):
        assert _has_override(client.argv, key, "false"), key
    assert client.argv.index("app-server") > client.argv.index("features.memories=false")
    assert client.env["CODEX_HOME"] != str(Path(client.env["CODEX_HOME"]).parent / "operator-codex")
    assert client.home_seen["memories"] is False
    assert client.home_seen["agents"] is False
    assert "auth.json" in client.home_seen["entries"]

    home = Path(client.env["CODEX_HOME"])
    await runtime._client.close()
    assert not home.exists()


@pytest.mark.asyncio
async def test_a_non_worker_app_server_run_is_unchanged(fake_client):
    runtime = mod.CodexAppServerRuntime()
    await runtime._ensure_client()
    client = fake_client.instances[-1]
    assert "CODEX_HOME" not in client.env
    assert not any("memories" in arg for arg in client.argv)


@pytest.mark.asyncio
async def test_the_real_client_removes_its_isolated_home_on_close(tmp_path):
    home = tmp_path / "conductor-codex-home-x"
    home.mkdir()
    client = mod._AppServerClient(["codex"], cwd=None)
    client.isolated_home = home
    await client.close()
    assert not home.exists()
