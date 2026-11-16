"""R6 L1: the /roles command reads and edits the role registry through the API."""

from __future__ import annotations

import json

import httpx
import pytest

from ptc_cli.superagent import roles_view
from ptc_cli.superagent.client import SuperAgentClient
from ptc_cli.superagent.tui_completion import SLASH_COMMANDS

PAYLOAD = {
    "source": "store",
    "revision": 2,
    "hard_rules": ["router_routes_never_executes", "merge_needs_human"],
    "roles": {
        "router": {"default": "deepseek-flash", "candidates": ["deepseek-flash"],
                   "constraints": {"read_only": True}},
        "planner": {"default": "claude", "candidates": ["claude"], "constraints": {}},
        "reviewer": {"default": "codex", "candidates": ["codex"], "constraints": {"read_only": True}},
        "worker": {"default": "stepfun", "candidates": ["stepfun", "kimi"],
                   "constraints": {"may_write": True}},
    },
}


def test_advertised() -> None:
    assert "/roles" in SLASH_COMMANDS


def test_parse() -> None:
    assert roles_view.parse("") == ("show", None, None)
    assert roles_view.parse("set worker default kimi reason quota low") == (
        "set",
        "worker",
        {"reason": "quota low", "default": "kimi"},
    )
    assert roles_view.parse("set worker candidates stepfun,kimi reason web")[2] == {
        "reason": "web",
        "candidates": ["stepfun", "kimi"],
    }
    for bad in ("set worker default kimi", "set boss default x reason y", "nope"):
        with pytest.raises(roles_view.RolesUsageError):
            roles_view.parse(bad)


def test_render() -> None:
    lines = roles_view.render(PAYLOAD)
    assert lines[0] == "role registry (store, revision 2)"
    assert any(line.strip().startswith("worker") and "stepfun, kimi" in line for line in lines)
    assert "merge_needs_human" in lines[-1]
    assert roles_view.render(None) == ["role registry unavailable"]


@pytest.mark.asyncio
async def test_client_routes() -> None:
    seen: list[tuple[str, str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, body))
        return httpx.Response(200, json=PAYLOAD)

    client = SuperAgentClient("http://test", "u1")
    client._client = httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handler))
    assert (await client.roles())["revision"] == 2
    await client.set_role("worker", {"default": "kimi", "reason": "r"})
    assert seen == [
        ("GET", "/api/v1/roles", None),
        ("PUT", "/api/v1/roles/worker", {"default": "kimi", "reason": "r"}),
    ]
