"""HTTP contract for SuperAgentClient.progress."""

from __future__ import annotations

import httpx
import pytest

from ptc_cli.superagent.client import SuperAgentClient, SuperAgentError


def _client(handler) -> SuperAgentClient:
    client = SuperAgentClient("http://test", "u1")
    client._client = httpx.AsyncClient(
        base_url="http://test",
        headers={"X-User-Id": "u1"},
        transport=httpx.MockTransport(handler),
    )
    return client


@pytest.mark.asyncio
async def test_progress_returns_the_payload_on_200():
    seen: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["user"] = request.headers.get("x-user-id", "")
        return httpx.Response(
            200,
            json={
                "ticket_id": "t1",
                "status": "running",
                "phase": "running",
                "elapsed_seconds": 31.2,
                "steps": [
                    {"key": "created", "label": "任务已创建", "state": "done"},
                    {"key": "worker", "label": "执行中", "state": "active"},
                ],
                "summary": "working",
                "question": "",
                "error": "",
            },
        )

    client = _client(handler)
    payload = await client.progress("t1")
    await client.close()

    assert seen == {"path": "/api/v1/autonomy/tickets/t1/progress", "user": "u1"}
    assert payload is not None
    assert payload["phase"] == "running"
    assert payload["elapsed_seconds"] == 31.2
    assert payload["steps"][1]["state"] == "active"


@pytest.mark.asyncio
async def test_progress_returns_none_on_404():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Ticket not found"})

    client = _client(handler)
    assert await client.progress("missing") is None
    await client.close()


@pytest.mark.asyncio
async def test_progress_raises_on_network_error_and_other_http_failures():
    async def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = _client(down)
    with pytest.raises(SuperAgentError, match="cannot reach"):
        await client.progress("t1")
    await client.close()

    async def failed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="unavailable")

    client = _client(failed)
    with pytest.raises(SuperAgentError, match="500"):
        await client.progress("t1")
    await client.close()
