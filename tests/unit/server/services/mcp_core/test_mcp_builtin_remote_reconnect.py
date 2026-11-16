"""A built-in remote server still down after boot heals in the background.

The worker keeps retrying it, and once it answers publishes a successor of the
global registry with its tools and without its failure, which is what turns
the health check back to 200. Shutdown cancels the retry without leaving a
connection task behind.
"""

import asyncio
from types import SimpleNamespace

import pytest

import src.server.services.mcp_core.registry as mcp_registry
from src.config.runtime.core import MCPServerConfig
from src.server.services.mcp_core.registry import (
    MCPRegistry,
    MCPServerConnector,
    get_global_registry,
    reconnect_builtin_remote,
    set_global_registry,
)
from src.server.services.mcp_core.schema import MCPToolInfo
from src.server.app.utilities import health_check

pytestmark = pytest.mark.asyncio

_DOWN = ("server unavailable (HTTP 503)", True)


@pytest.fixture
def scripted(monkeypatch):
    """Each reconnect attempt takes the next scripted outcome for its server."""
    monkeypatch.setattr(mcp_registry, "HEAL_INITIAL_DELAY_S", 0.0)
    monkeypatch.setattr(mcp_registry, "_GLOBAL_REGISTRY", None)
    scripts: dict[str, list] = {}

    async def fake_connect(self, timeout=None):
        outcome = scripts[self.config.name].pop(0)
        if outcome is None:
            self.tools = [
                MCPToolInfo(
                    name="get",
                    description="",
                    input_schema={},
                    server_name=self.config.name,
                )
            ]
            return
        self.failure_reason, self.failure_transient = outcome
        self._connection_error = RuntimeError(outcome[0])
        raise self._connection_error

    async def fake_exit(self, *exc):
        self.session = None

    monkeypatch.setattr(MCPServerConnector, "connect", fake_connect)
    monkeypatch.setattr(MCPServerConnector, "__aexit__", fake_exit)
    return scripts


def _http(name: str) -> MCPServerConfig:
    return MCPServerConfig(
        name=name, transport="http", url=f"http://127.0.0.1:1/{name}", source="builtin"
    )


def _booted(servers: list[MCPServerConfig], failed: list[str]) -> MCPRegistry:
    """A frozen registry as boot leaves it: the failed servers dropped and recorded."""
    registry = MCPRegistry(SimpleNamespace(mcp=SimpleNamespace(servers=servers)))
    for server in servers:
        if server.name in failed:
            registry.failed_builtin_remote[server.name] = _DOWN[0]
        else:
            registry.connectors[server.name] = MCPServerConnector(server)
    registry._frozen = True
    set_global_registry(registry)
    return registry


async def test_failed_servers_heal_and_leave_failed_builtin_remote(scripted):
    scripted["inventory"] = [_DOWN, None]
    scripted["docs_search"] = [_DOWN, _DOWN, _DOWN, None]
    booted = _booted(
        [_http("docs_search"), _http("weather"), _http("inventory")],
        ["docs_search", "inventory"],
    )
    assert (await health_check()).status_code == 503

    await reconnect_builtin_remote()

    healed = get_global_registry()
    assert healed is not booted
    assert healed.frozen
    assert healed.failed_builtin_remote == {}
    # Config order, which the prompt summary and wrappers are rendered in.
    assert list(healed.connectors) == ["docs_search", "weather", "inventory"]
    assert [t.name for t in healed.get_all_tools()["docs_search"]] == ["get"]
    assert (await health_check())["status"] == "healthy"
    # What a session or tool view already holds is left as it was.
    assert list(booted.connectors) == ["weather"]
    assert set(booted.failed_builtin_remote) == {"docs_search", "inventory"}


async def test_a_server_still_down_keeps_the_check_failing(scripted, monkeypatch):
    monkeypatch.setattr(mcp_registry, "HEAL_MAX_DELAY_S", 0.0)
    scripted["inventory"] = [None]
    scripted["issue_tracker"] = [_DOWN] * 50
    _booted([_http("inventory"), _http("issue_tracker")], ["inventory", "issue_tracker"])
    task = asyncio.create_task(reconnect_builtin_remote())

    while "inventory" in get_global_registry().failed_builtin_remote:
        await asyncio.sleep(0)

    response = await health_check()
    assert response.status_code == 503
    assert '"failed_servers":["issue_tracker"]' in response.body.decode()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_shutdown_cancels_an_attempt_in_flight(monkeypatch):
    """Cancelling mid-attempt tears the connection task down with it."""
    monkeypatch.setattr(mcp_registry, "HEAL_INITIAL_DELAY_S", 0.0)
    monkeypatch.setattr(mcp_registry, "_GLOBAL_REGISTRY", None)
    attempts: list[MCPServerConnector] = []
    in_flight = asyncio.Event()

    async def hang(self):
        attempts.append(self)
        in_flight.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(MCPServerConnector, "_run_connection", hang)
    _booted([_http("macro")], ["macro"])
    task = asyncio.create_task(reconnect_builtin_remote())
    await in_flight.wait()

    task.cancel()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=1)

    assert task.cancelled()
    assert attempts[0]._connection_task.done()
    assert "macro" in get_global_registry().failed_builtin_remote
