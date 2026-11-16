"""Self-dispatch for project workers.

The scheduler runs inside the API process, so it drives workers through the
same HTTP doors a client uses: create a thread, then POST its first turn with
the background-dispatch header. That reuses the whole admission, run-ledger,
sandbox and SSE pipeline instead of a second agent runtime.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import aiohttp

from src.config.settings import HOST_MODE, background_dispatch_requires_token

logger = logging.getLogger(__name__)


class DispatchError(RuntimeError):
    """A worker thread could not be created or started."""


@dataclass(frozen=True)
class DispatchReceipt:
    thread_id: str
    run_id: str | None
    # ``dispatched`` starts a turn, ``steered`` rides a running one, and
    # ``not_running`` was refused because the thread had no turn to ride.
    status: str = "dispatched"


def last_assistant_text(sse_text: str) -> str | None:
    """The last assistant message content in a replay SSE stream.

    Replay emits cumulative or final chunks as ``message_chunk`` frames; a
    native CLI's output rides those frames after its graph projects them. The
    last non-empty assistant chunk is the best available closing summary for
    a Worker that has no report tool of its own. A native turn whose chunk
    carries ``final_text`` (text came before a tool call or an earlier
    message) contributes only that last message: it is the completion report.
    Limitation: replay rows that do not come from the checkpoint, and runtimes
    that mark no message items, carry no ``final_text``; such a turn's joined
    text is not a valid report and settles waiting (it fails safe).
    """
    event: str | None = None
    last: str | None = None
    for line in sse_text.splitlines():
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
            continue
        if not line.startswith("data:"):
            continue
        if event != "message_chunk":
            continue
        try:
            data = json.loads(line[len("data:"):].strip())
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("role") != "assistant":
            continue
        final_text = data.get("final_text")
        if isinstance(final_text, str):
            last = final_text.strip() or None
            continue
        content = data.get("content")
        if isinstance(content, str) and content.strip():
            last = content.strip()
    return last


def _base_url() -> str:
    return (
        os.getenv("PROJECT_ORCHESTRATOR_BASE_URL", "").strip()
        or os.getenv("CONDUCTOR_FLOW_BASE_URL", "")
        or os.getenv("GINLIXFLOW_BASE_URL", "").strip()  # legacy alias, one version
        or os.getenv("SERVER_BASE_URL", "").strip()
        or "http://127.0.0.1:8000"
    ).rstrip("/")


def stable_request_key(value: str) -> str:
    """The message door requires a UUID; normalize a stable logical key to one."""
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError):
        return str(uuid5(NAMESPACE_URL, str(value)))


def _headers(user_id: str) -> dict[str, str]:
    token = os.getenv("INTERNAL_SERVICE_TOKEN", "").strip()
    headers = {
        "X-User-Id": user_id,
        "X-Dispatch": "background",
        "Content-Type": "application/json",
    }
    if token:
        headers["X-Service-Token"] = token
    return headers


def preflight() -> None:
    """Refuse a platform deployment with no service token, before any task runs."""
    if background_dispatch_requires_token() and HOST_MODE != "oss":
        raise DispatchError(
            "INTERNAL_SERVICE_TOKEN is not configured; the project scheduler "
            "cannot open worker turns."
        )


class ProjectDispatcher:
    """Minimal HTTP client for the thread doors the scheduler needs."""

    def __init__(self, *, timeout_s: int = 60) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout_s, connect=10, sock_read=30)

    async def create_thread(
        self,
        *,
        user_id: str,
        workspace_id: str,
        title: str,
        first_query: str,
        native_provider: str | None = None,
        platform: str = "project_orchestrator",
    ) -> str:
        payload = {
            "workspace_id": workspace_id,
            "title": title[:255],
            "first_query": first_query[:4000],
            "platform": platform,
        }
        if native_provider:
            payload["native_provider"] = native_provider
        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            try:
                async with session.post(
                    f"{_base_url()}/api/v1/threads",
                    json=payload,
                    headers=_headers(user_id),
                ) as resp:
                    if resp.status >= 400:
                        body = await resp.text()
                        raise DispatchError(
                            f"create thread failed ({resp.status}): {body[:300]}"
                        )
                    data = await resp.json()
            except aiohttp.ClientError as exc:
                raise DispatchError(f"create thread transport error: {exc}") from exc
        thread_id = data.get("thread_id") if isinstance(data, dict) else None
        if not isinstance(thread_id, str) or not thread_id:
            raise DispatchError("create thread returned no thread_id")
        return thread_id

    async def dispatch_turn(
        self,
        *,
        user_id: str,
        thread_id: str,
        workspace_id: str,
        prompt: str,
        request_key: str,
        model: str | None = None,
        platform: str = "project_orchestrator",
        steer_only: bool = False,
        origin: dict[str, Any] | None = None,
    ) -> DispatchReceipt:
        """Post one turn to a thread, or steer the one already running there.

        ``steer_only=True`` asks for a steer and nothing else: the thread's
        admission decides, so a request that finds no running turn is refused
        (``not_running``) instead of starting a turn of its own.
        ``origin`` names who sent the message, which is what lets a dispatched
        message ride a running turn as a hand-off.
        """
        payload: dict[str, Any] = {
            "messages": [{"role": "user", "content": prompt}],
            "workspace_id": workspace_id,
            "platform": platform,
            "request_key": stable_request_key(request_key),
            "timezone": "UTC",
        }
        if model:
            payload["llm_model"] = model
        if steer_only:
            payload["steer_only"] = True
        if origin:
            payload["origin"] = origin
        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            try:
                async with session.post(
                    f"{_base_url()}/api/v1/threads/{thread_id}/messages",
                    json=payload,
                    headers=_headers(user_id),
                ) as resp:
                    if resp.status >= 400:
                        body = await resp.text()
                        # A retransmit of the same request key after a crash
                        # adopts the run the earlier copy already started.
                        if resp.status == 409:
                            try:
                                detail = (await resp.json()).get("detail")
                            except Exception:
                                detail = None
                            if isinstance(detail, dict):
                                if detail.get("run_id"):
                                    return DispatchReceipt(
                                        thread_id=str(thread_id),
                                        run_id=str(detail["run_id"]),
                                    )
                                if detail.get("code") == "not_running":
                                    # The thread had no running turn to steer
                                    # and the request may not start one.
                                    return DispatchReceipt(
                                        thread_id=str(thread_id),
                                        run_id=None,
                                        status="not_running",
                                    )
                        raise DispatchError(
                            f"dispatch turn failed ({resp.status}): {body[:300]}"
                        )
                    # Background dispatch answers a JSON receipt once the run's
                    # START commits; a non-background path would stream SSE.
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        data = {}
            except aiohttp.ClientError as exc:
                raise DispatchError(f"dispatch turn transport error: {exc}") from exc
        if not isinstance(data, dict):
            data = {}
        return DispatchReceipt(
            thread_id=str(thread_id),
            run_id=str(data["run_id"]) if data.get("run_id") else None,
            status=str(data.get("status") or "dispatched"),
        )

    async def thread_run_status(self, *, user_id: str, thread_id: str) -> str | None:
        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            try:
                async with session.get(
                    f"{_base_url()}/api/v1/threads/{thread_id}",
                    headers=_headers(user_id),
                ) as resp:
                    if resp.status >= 400:
                        return None
                    data = await resp.json()
            except aiohttp.ClientError:
                return None
        if not isinstance(data, dict):
            return None
        status = data.get("run_status") or data.get("current_status")
        return str(status) if status else None

    async def thread_last_assistant_text(
        self, *, user_id: str, thread_id: str
    ) -> str | None:
        """Read a thread replay's last assistant text, or None when unavailable.

        Native CLI Workers have no structured-report tool; their closing
        assistant message is the only report they produce. Uses the same
        checkpoint replay a client reads, at one-turn page size.
        """
        url = (
            f"{_base_url()}/api/v1/threads/{thread_id}/messages/replay"
            "?source=auto&limit=1"
        )
        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            try:
                async with session.get(url, headers=_headers(user_id)) as resp:
                    if resp.status >= 400:
                        return None
                    body = await resp.text()
            except aiohttp.ClientError:
                return None
        return last_assistant_text(body)

    async def cancel_thread(self, *, user_id: str, thread_id: str, run_id: str | None) -> bool:
        """Cancel the worker run. False when the request failed or a run is still active.

        ``already_finished`` and ``no_active_run`` are success: there is nothing
        left to stop. ``another_run_active`` is failure: execution is still live.
        Callers that record a terminal task state must not do that until this
        returns True, or a later retry will see the task as finished and skip
        the stop.
        """
        url = f"{_base_url()}/api/v1/threads/{thread_id}/cancel"
        if run_id:
            url += f"?run_id={run_id}"
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.post(url, headers=_headers(user_id)) as resp:
                    if resp.status >= 400:
                        await resp.read()
                        logger.warning(
                            "[dispatch] cancel failed thread=%s status=%s",
                            thread_id,
                            resp.status,
                        )
                        return False
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:  # noqa: BLE001 - a non-JSON 2xx still stopped the run
                        data = None
        except aiohttp.ClientError as exc:
            logger.warning(
                "[dispatch] cancel transport error thread=%s: %s", thread_id, exc
            )
            return False
        if isinstance(data, dict) and data.get("state") == "another_run_active":
            logger.warning(
                "[dispatch] cancel left another run active thread=%s", thread_id
            )
            return False
        return True
