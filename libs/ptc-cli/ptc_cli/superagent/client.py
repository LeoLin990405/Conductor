"""Minimal async HTTP client for the Super-agent endpoints."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

import httpx


class SuperAgentError(RuntimeError):
    """A server response that the shell can show without a traceback."""


class ActivityStreamUnavailableError(SuperAgentError):
    """The server has no live Ticket activity stream to attach to.

    Raised when the route is absent (an older deployment), answers with a
    non-SSE body, or the transport fails; the shell answers it by polling the
    existing activity endpoint instead.
    """


@dataclass(frozen=True, slots=True)
class ActivityStreamEvent:
    """One Server-Sent Event from the Ticket activity stream."""

    event: str
    data: dict[str, Any]
    event_id: str | None = None


_SSE_MISSING_STATUS = frozenset({404, 405, 501})
_HTTP_BAD_REQUEST = 400


def next_sse_block(buffer: str) -> tuple[str | None, str]:
    """Split the first SSE frame off ``buffer``, accepting LF or CRLF framing.

    Runs on the accumulated buffer, never on a transport chunk: a CRLF frame
    delimiter split across two chunks must still be recognized. Returns the
    frame (without its delimiter) and the unread remainder, or None when the
    buffer does not yet hold a complete frame.
    """
    lf = buffer.find("\n\n")
    crlf = buffer.find("\r\n\r\n")
    if crlf != -1 and (lf == -1 or crlf < lf):
        return buffer[:crlf], buffer[crlf + 4 :]
    if lf != -1:
        return buffer[:lf], buffer[lf + 2 :]
    return None, buffer


def parse_sse_block(block: str) -> ActivityStreamEvent | None:
    """Parse one SSE frame. A comments-only frame (keepalive) parses to None."""
    event = "message"
    event_id: str | None = None
    data_lines: list[str] = []
    # Split on LF only: ``str.splitlines`` also splits on U+2028/U+2029/NEL,
    # which JSON string content may legitimately contain.
    for raw_line in block.split("\n"):
        line = raw_line.removesuffix("\r")
        if not line or line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "id":
            event_id = value.strip() or event_id
        elif field == "event":
            event = value.strip() or event
        elif field == "data":
            data_lines.append(value)
    if not data_lines:
        return None
    text = "\n".join(data_lines)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ActivityStreamUnavailableError(
            "Ticket activity stream sent a bad frame"
        ) from exc
    return ActivityStreamEvent(
        event=event,
        data=data if isinstance(data, dict) else {"value": data},
        event_id=event_id,
    )


class SuperAgentClient:
    def __init__(self, base_url: str, user_id: str, *, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.user_id = user_id
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={"X-User-Id": user_id},
        )

    async def __aenter__(self) -> "SuperAgentClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise SuperAgentError(f"cannot reach {self.base_url}: {exc}") from exc
        if response.status_code >= _HTTP_BAD_REQUEST:
            detail = response.text[:500].strip()
            raise SuperAgentError(f"{method} {path} -> {response.status_code}: {detail}")
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise SuperAgentError(
                f"{method} {path} returned non-JSON content ({response.status_code})"
            ) from exc

    async def roles(self) -> dict[str, Any]:
        """R6 L1: the role registry."""
        return await self._request("GET", "/api/v1/roles")

    async def set_role(self, role: str, body: dict[str, Any]) -> dict[str, Any]:
        """R6 L1: change one role (the server validates and audits it)."""
        return await self._request("PUT", f"/api/v1/roles/{role}", json=body)

    async def overview(self) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/autonomy/overview")

    async def models(self) -> dict[str, Any]:
        """Providers and model names the server has configured."""
        return await self._request("GET", "/api/v1/models")

    async def tickets(self, **params: Any) -> dict[str, Any]:
        return await self._request(
            "GET", "/api/v1/autonomy/tickets", params={k: v for k, v in params.items() if v not in (None, "")}
        )

    async def ticket(self, ticket_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/autonomy/tickets/{ticket_id}")

    async def progress(self, ticket_id: str) -> dict[str, Any] | None:
        """Supervisor progress for one Ticket.

        HTTP 200 returns the parsed object. HTTP 404 (the feature is off, or
        the Ticket is not owned by this user) returns None so the caller can
        keep its stream-derived view. Transport failures and any other HTTP
        status raise ``SuperAgentError``, the same way ``_request`` does.
        """
        path = f"/api/v1/autonomy/tickets/{ticket_id}/progress"
        try:
            response = await self._client.request("GET", path)
        except httpx.HTTPError as exc:
            raise SuperAgentError(f"cannot reach {self.base_url}: {exc}") from exc
        if response.status_code == 404:
            return None
        if response.status_code >= _HTTP_BAD_REQUEST:
            detail = response.text[:500].strip()
            raise SuperAgentError(f"GET {path} -> {response.status_code}: {detail}")
        if response.status_code == 204 or not response.content:
            return None
        try:
            payload = response.json()
        except ValueError as exc:
            raise SuperAgentError(
                f"GET {path} returned non-JSON content ({response.status_code})"
            ) from exc
        if not isinstance(payload, dict):
            raise SuperAgentError(f"GET {path} returned a non-object progress payload")
        return payload

    async def dispatch(self, dispatch_id: str) -> dict[str, Any] | None:
        """One dispatch summary. HTTP 404 is an empty view, not an exception."""
        path = f"/api/v1/dispatch/{dispatch_id}"
        try:
            response = await self._client.request("GET", path)
        except httpx.HTTPError as exc:
            raise SuperAgentError(f"cannot reach {self.base_url}: {exc}") from exc
        if response.status_code == 404:
            return None
        if response.status_code >= _HTTP_BAD_REQUEST:
            detail = response.text[:500].strip()
            error = SuperAgentError(f"GET {path} -> {response.status_code}: {detail}")
            setattr(error, "status_code", response.status_code)
            raise error
        if response.status_code == 204 or not response.content:
            return None
        try:
            payload = response.json()
        except ValueError as exc:
            raise SuperAgentError(
                f"GET {path} returned non-JSON content ({response.status_code})"
            ) from exc
        if not isinstance(payload, dict):
            raise SuperAgentError(f"GET {path} returned a non-object dispatch payload")
        return payload

    async def active_dispatches(self) -> list[dict[str, Any]]:
        """Active dispatches. A missing route is an empty list."""
        path = "/api/v1/dispatch/status/active"
        try:
            response = await self._client.request("GET", path)
        except httpx.HTTPError as exc:
            raise SuperAgentError(f"cannot reach {self.base_url}: {exc}") from exc
        if response.status_code == 404:
            return []
        if response.status_code >= _HTTP_BAD_REQUEST:
            detail = response.text[:500].strip()
            error = SuperAgentError(f"GET {path} -> {response.status_code}: {detail}")
            setattr(error, "status_code", response.status_code)
            raise error
        if response.status_code == 204 or not response.content:
            return []
        try:
            payload = response.json()
        except ValueError as exc:
            raise SuperAgentError(
                f"GET {path} returned non-JSON content ({response.status_code})"
            ) from exc
        rows: Any
        if isinstance(payload, dict):
            rows = payload.get("dispatches")
            if not isinstance(rows, list):
                rows = payload.get("active")
        elif isinstance(payload, list):
            rows = payload
        else:
            rows = []
        if not isinstance(rows, list):
            return []
        return [row for row in rows if isinstance(row, dict)]

    async def activity(self, ticket_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/autonomy/tickets/{ticket_id}/activity")

    async def stream_activity(
        self, ticket_id: str, *, last_event_id: str | None = None
    ) -> AsyncGenerator[ActivityStreamEvent]:
        """Follow a Ticket's live activity stream.

        Raises ``ActivityStreamUnavailableError`` when the deployment has no stream
        (404/405/501), answers with something other than SSE, or cannot be
        reached, so the caller can fall back to ``activity()`` polling. A
        mid-stream transport failure raises the same error; the caller
        reconnects with the last event id it saw.
        """
        path = f"/api/v1/autonomy/tickets/{ticket_id}/activity/stream"
        headers = {"Accept": "text/event-stream", "Cache-Control": "no-cache"}
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        try:
            async with self._client.stream("GET", path, headers=headers) as response:
                if response.status_code in _SSE_MISSING_STATUS:
                    raise ActivityStreamUnavailableError(
                        f"live ticket activity is not served here (HTTP {response.status_code})"
                    )
                if response.status_code >= _HTTP_BAD_REQUEST:
                    body = await response.aread()
                    detail = body.decode("utf-8", "replace")[:500].strip()
                    raise SuperAgentError(
                        f"GET {path} -> {response.status_code}: {detail}"
                    )
                content_type = response.headers.get("content-type", "")
                if "text/event-stream" not in content_type.lower():
                    raise ActivityStreamUnavailableError(
                        "live ticket activity is not served here "
                        f"(content-type {content_type or 'missing'})"
                    )
                buffer = ""
                async for chunk in response.aiter_text():
                    buffer += chunk
                    while True:
                        block, buffer = next_sse_block(buffer)
                        if block is None:
                            break
                        parsed = parse_sse_block(block)
                        if parsed is not None:
                            yield parsed
        except httpx.HTTPError as exc:
            raise ActivityStreamUnavailableError(
                f"live ticket activity transport failed: {exc}"
            ) from exc

    async def home_workspace(self) -> str:
        row = await self._request("POST", "/api/v1/workspaces/home")
        if not isinstance(row, dict):
            raise SuperAgentError("Home workspace response was not a JSON object")
        workspace_id = row.get("workspace_id")
        if not isinstance(workspace_id, str) or not workspace_id.strip():
            raise SuperAgentError("Home workspace response did not contain a workspace id")
        return workspace_id

    async def create_task(
        self,
        prompt: str,
        *,
        title: str = "",
        workspace_id: str,
        worker_daemon_id: str = "",
        no_refine: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"prompt": prompt, "workspace_id": workspace_id}
        if title:
            body["title"] = title
        if worker_daemon_id:
            body["worker_daemon_id"] = worker_daemon_id
        if no_refine:
            body["no_refine"] = True
        return await self._request("POST", "/api/v1/autonomy/tasks", json=body)

    async def stop(self, ticket_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/autonomy/tickets/{ticket_id}/stop")

    async def retry(self, ticket_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/v1/autonomy/tickets/{ticket_id}/retry")

    async def reply(self, ticket_id: str, text: str) -> dict[str, Any]:
        return await self._request(
            "POST", f"/api/v1/autonomy/tickets/{ticket_id}/reply", json={"text": text}
        )

    async def sources(self) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/autonomy/sources")

    async def daemons(self) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/autonomy/daemons")

    async def fleet(self) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/agents/fleet")

    async def fleet_logs(self, agent_id: str, *, lines: int = 80) -> dict[str, Any]:
        return await self._request(
            "GET", f"/api/v1/agents/{agent_id}/logs", params={"lines": lines}
        )

    async def native_agents(self) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/native-agents")
