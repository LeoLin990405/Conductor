"""The ``/raw`` prefix: the shell's split, its dispatch, and the wire flag.

A leading ``/raw`` runs the request exactly as written, skipping the server's
prompt refiner for that one submission. The server side of the flag lives in
``tests/unit/server/services/autonomy/test_prompt_refiner.py``; this module
covers the ptc-agent half — the parser, the shell's routing, and the flag the
client puts on the wire.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

# This project's lock resolves the unrelated PyPI ``conductor-core`` (a 2020
# package that ships ``conductor``), so the root project's ``conductor_cli`` —
# which ``ptc_cli.superagent.shell`` imports — is not in this venv. The root
# ``src`` is the only place it lives; put it on the path so the shell imports.
_ROOT_SRC = next(
    (
        parent / "src"
        for parent in Path(__file__).resolve().parents
        if (parent / "src" / "conductor_cli" / "terminal_text.py").is_file()
    ),
    None,
)
if _ROOT_SRC is not None and str(_ROOT_SRC) not in sys.path:
    sys.path.insert(0, str(_ROOT_SRC))

from ptc_cli.superagent.client import SuperAgentClient  # noqa: E402
from ptc_cli.superagent.shell import (  # noqa: E402
    SuperAgentError,
    SuperAgentShell,
    strip_raw_prefix,
)


class FakeClient:
    """Records create_task instead of reaching a server."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def create_task(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("create", prompt, kwargs))
        return {"ticket_id": "ticket-new", "status": "queued", "title": prompt}

    async def home_workspace(self) -> str:
        return "home"


def _shell() -> SuperAgentShell:
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    shell.client = FakeClient()  # type: ignore[assignment]
    return shell


# ------------------------------------------------------------------- parser


def test_a_real_raw_prefix_splits_and_strips():
    assert strip_raw_prefix("/raw fix the save button") == ("fix the save button", True)
    assert strip_raw_prefix("/rawfix the save button") == ("/rawfix the save button", False)
    assert strip_raw_prefix("fix the save button") == ("fix the save button", False)


def test_a_bare_prefix_is_raw_with_an_empty_request():
    assert strip_raw_prefix("/raw") == ("", True)
    assert strip_raw_prefix("/raw   ") == ("", True)
    assert strip_raw_prefix("/rawfix") == ("/rawfix", False)
    assert strip_raw_prefix("fix it") == ("fix it", False)


def test_a_prefix_that_is_only_part_of_a_command_is_not_raw():
    assert strip_raw_prefix("/resume abc") == ("/resume abc", False)


# -------------------------------------------------------------------- shell


@pytest.mark.asyncio
async def test_the_shell_sends_raw_requests_without_refinement():
    shell = _shell()
    await shell.handle("/raw fix the save button")
    kind, prompt, kwargs = shell.client.calls[0]  # type: ignore[attr-defined]
    assert (kind, prompt) == ("create", "fix the save button")
    assert kwargs["no_refine"] is True


@pytest.mark.asyncio
async def test_the_shell_sends_plain_requests_for_refinement():
    shell = _shell()
    await shell.handle("fix the save button")
    kind, prompt, kwargs = shell.client.calls[0]  # type: ignore[attr-defined]
    assert (kind, prompt) == ("create", "fix the save button")
    assert kwargs["no_refine"] is False


@pytest.mark.asyncio
async def test_the_shell_refuses_a_bare_raw_prefix():
    shell = _shell()
    with pytest.raises(SuperAgentError, match="usage: /raw"):
        await shell.handle("/raw")
    assert shell.client.calls == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_the_shell_keeps_unknown_slash_commands_unknown():
    shell = _shell()
    with pytest.raises(SuperAgentError, match="unknown command"):
        await shell.handle("/rawfix it")
    assert shell.client.calls == []  # type: ignore[attr-defined]


# ------------------------------------------------------------------- client


@pytest.mark.asyncio
async def test_the_client_puts_no_refine_on_the_wire_only_when_set():
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200, json={"ticket_id": "ticket-new", "status": "queued", "title": "Fix"}
        )

    transport = httpx.MockTransport(handler)
    client = SuperAgentClient(base_url="http://test", user_id="u1")
    client._client = httpx.AsyncClient(
        base_url="http://test", transport=transport, timeout=1.0
    )

    try:
        await client.create_task("fix the save button", workspace_id="w1")
        await client.create_task("fix the save button", workspace_id="w1", no_refine=True)
    finally:
        await client.close()
    assert bodies[0] == {"prompt": "fix the save button", "workspace_id": "w1"}
    assert bodies[1] == {
        "prompt": "fix the save button",
        "workspace_id": "w1",
        "no_refine": True,
    }
