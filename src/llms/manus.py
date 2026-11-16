"""Manus API v2 adapter.

Manus is a remote agent, not a local CLI: ``task.create`` starts an async task,
``task.listMessages`` reports progress, and ``task.stop`` is the remote cancel.
The adapter keeps the one-shot result contract used by native agent providers.
"""

from __future__ import annotations

import asyncio
import os
import stat
import time
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

import httpx

from src.llms.native_cli.base import (
    NativeCliError,
    NativeCliResult,
    NativeCliUnavailable,
    NativeCliUsage,
)

_MANUS_KEY_MAX_BYTES = 16_384
_RESEARCH_ONLY_PREFIX = (
    "Research only: answer with sourced findings and uncertainty. Do not write or modify code, "
    "do not execute code, do not perform implementation or deployment, and do not create or modify files.\n\n"
)


def _secure_key_file(path: Path | None = None) -> Path | None:
    key_path = path or Path.home() / ".config" / "manus" / "env"
    try:
        metadata = key_path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            return None
    except OSError:
        return None
    return key_path


def manus_api_key_available() -> bool:
    """Check key-file presence and permissions without reading its contents."""
    return _secure_key_file() is not None


def load_manus_api_key(path: Path | None = None) -> str | None:
    """Read a private key file through one verified descriptor without executing shell syntax."""
    key_path = path or Path.home() / ".config" / "manus" / "env"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(key_path, flags)
        with os.fdopen(descriptor, "rb") as key_file:
            metadata = os.fstat(key_file.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size > _MANUS_KEY_MAX_BYTES
            ):
                return None
            raw = key_file.read(_MANUS_KEY_MAX_BYTES + 1)
            if len(raw) > _MANUS_KEY_MAX_BYTES:
                return None
            after_read = os.fstat(key_file.fileno())
            if (metadata.st_dev, metadata.st_ino, metadata.st_size) != (
                after_read.st_dev,
                after_read.st_ino,
                after_read.st_size,
            ):
                return None
        lines = raw.decode("utf-8").splitlines()
    except (OSError, UnicodeError):
        return None

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        name, separator, value = stripped.partition("=")
        if separator and name.strip() == "MANUS_API_KEY":
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            elif value.startswith(("'", '"')):
                return None
            value = value.strip()
            return value or None
    return None


def _message_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, Mapping):
        return ""
    for key in ("text", "content", "message", "result"):
        text = value.get(key)
        if isinstance(text, str) and text.strip():
            return text
    return ""


def _events(payload: Any) -> list[dict[str, Any]]:
    containers = []
    if isinstance(payload, Mapping):
        if isinstance(payload.get("data"), Mapping):
            containers.append(payload["data"])
        containers.append(payload)
    for container in containers:
        for key in ("messages", "events", "items"):
            items = container.get(key)
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
    if isinstance(payload, Mapping):
        if isinstance(payload.get("data"), list):
            return [item for item in payload["data"] if isinstance(item, dict)]
        if isinstance(payload.get("data"), Mapping):
            return [dict(payload["data"])]
        return [dict(payload)]
    return []


def _status(events: list[dict[str, Any]]) -> str:
    # listMessages is requested newest-first, so its first status event is current.
    for event in events:
        if event.get("type") == "status_update":
            body = event.get("status_update")
            value = body.get("agent_status") if isinstance(body, Mapping) else event.get("agent_status")
            if isinstance(value, str):
                return value.lower()
    return ""


def _assistant_text(events: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    # The API returns newest-first; assemble messages in their original order.
    for event in reversed(events):
        kind = str(event.get("type") or event.get("role") or "").lower()
        if kind not in {"assistant_message", "agent_message", "assistant", "message"}:
            continue
        body = event.get("assistant_message") or event.get("agent_message") or event
        text = _message_text(body)
        if text and (not parts or parts[-1] != text):
            parts.append(text)
    return "\n".join(parts).strip()


class ManusAdapter:
    provider = "manus"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        agent_profile: str | None = None,
        project_id: str | None = None,
        locale: str | None = None,
        timeout_seconds: float = 300.0,
        poll_interval: float = 2.0,
        poll_timeout_seconds: float = 1800.0,
    ) -> None:
        if base_url and api_key is None:
            raise ValueError("A custom Manus API URL requires explicit API key injection")
        self._api_key_injected = api_key is not None
        self._api_key = (api_key or "").strip() or None
        self._base_url = (base_url or "https://api.manus.ai").rstrip("/")
        self._agent_profile = agent_profile or "standard"
        self._project_id = project_id or ""
        self._locale = locale or "zh-CN"
        self._timeout_seconds = timeout_seconds
        self._poll_interval = max(0.5, float(poll_interval))
        self._poll_timeout_seconds = max(30.0, float(poll_timeout_seconds))
        self.last_task_id: str | None = None
        self.last_task_url: str | None = None
        self._stop_sent_task_id: str | None = None

    @property
    def binary(self) -> str:
        if not self._configured_api_key():
            raise NativeCliUnavailable("Manus API key is unavailable")
        return "manus-api"

    def _configured_api_key(self) -> str | None:
        if self._api_key_injected:
            return self._api_key
        if self._api_key is None:
            self._api_key = load_manus_api_key()
        return self._api_key

    async def _request(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        api_key = self._configured_api_key()
        if not api_key:
            raise NativeCliUnavailable("Manus API key is unavailable")
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                headers={"x-manus-api-key": api_key},
                timeout=min(60.0, self._timeout_seconds),
                trust_env=False,
            ) as client:
                response = await client.request(method, path, json=dict(payload) if payload is not None else None)
        except httpx.HTTPError as exc:
            raise NativeCliError(f"manus request failed: {type(exc).__name__}") from None
        try:
            body = response.json()
        except ValueError:
            raise NativeCliError(f"manus returned non-JSON ({response.status_code})") from None
        if response.status_code >= 400 or (isinstance(body, Mapping) and body.get("ok") is False):
            raise NativeCliError(f"manus request failed ({response.status_code})")
        return body if isinstance(body, Mapping) else {"data": body}

    async def _create_or_continue(self, prompt: str, session_id: str | None, deadline: float) -> str:
        message = {"content": _RESEARCH_ONLY_PREFIX + prompt}
        if session_id:
            body = await self._request_before(
                "POST", "/v2/task.sendMessage", deadline, {"task_id": session_id, "message": message}
            )
            return str(body.get("task_id") or session_id)
        payload: dict[str, Any] = {
            "message": message,
            "agent_profile": self._agent_profile,
            "locale": self._locale,
            "interactive_mode": False,
            "hide_in_task_list": True,
            "share_visibility": "private",
        }
        if self._project_id:
            payload["project_id"] = self._project_id
        body = await self._request_before("POST", "/v2/task.create", deadline, payload)
        task_id = str(body.get("task_id") or "").strip()
        if not task_id:
            raise NativeCliError("manus task.create returned no task_id")
        self.last_task_url = str(body.get("task_url") or "") or None
        return task_id

    async def _request_before(
        self, method: str, path: str, deadline: float, payload: Mapping[str, Any] | None = None
    ) -> Mapping[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise NativeCliError("manus task timed out")
        try:
            return await asyncio.wait_for(self._request(method, path, payload), timeout=remaining)
        except TimeoutError:
            raise NativeCliError("manus task timed out") from None

    async def _messages(self, task_id: str, deadline: float) -> list[dict[str, Any]]:
        cursor: str | None = None
        events: list[dict[str, Any]] = []
        while True:
            query = f"task_id={quote(task_id, safe='')}&order=desc&limit=100"
            if cursor:
                query += f"&cursor={quote(cursor, safe='')}"
            body = await self._request_before("GET", f"/v2/task.listMessages?{query}", deadline)
            events.extend(_events(body))
            if body.get("has_more") is not True:
                return events
            next_cursor = body.get("next_cursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                raise NativeCliError("manus message pagination returned no cursor")
            cursor = next_cursor

    async def cancel_remote(self) -> None:
        if self.last_task_id and self._stop_sent_task_id != self.last_task_id:
            task_id = self.last_task_id
            self._stop_sent_task_id = task_id
            try:
                await self._request("POST", "/v2/task.stop", {"task_id": task_id})
            except Exception:  # noqa: BLE001 - cancellation is best effort
                pass

    async def available_credits(self) -> Mapping[str, Any] | None:
        """Return normalized spendable credit balance or None when it is unknown."""
        try:
            body = await self._request("GET", "/v2/usage.availableCredits")
        except NativeCliError:
            return None  # Missing keys, transport errors, and provider errors are unknown.
        data = body.get("data")
        amount = data.get("total_credits") if isinstance(data, Mapping) else None
        if type(amount) is not int or amount < 0:
            return None
        return {"amount": amount, "currency": "CREDITS"}

    async def get_credit_balance(self) -> Mapping[str, Any] | None:
        """Compatibility name for callers that use a balance preflight."""
        return await self.available_credits()

    async def run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | None = None,
    ) -> NativeCliResult:
        from src.utils.provider_roles import require_provider_role

        require_provider_role("manus", "research", prompt, task_kind="research")
        started = time.monotonic()
        deadline = started + self._poll_timeout_seconds
        self.last_task_id = session_id
        self.last_task_url = None
        self._stop_sent_task_id = None
        try:
            task_id = await self._create_or_continue(prompt, session_id, deadline)
            self.last_task_id = task_id
            return await self._poll_task(task_id, started, deadline)
        except (NativeCliError, asyncio.CancelledError):
            if self.last_task_id:
                try:
                    await asyncio.wait_for(self.cancel_remote(), timeout=3.0)
                except Exception:  # noqa: BLE001 - remote stop is bounded best effort
                    pass
            raise

    async def _poll_task(self, task_id: str, started: float, deadline: float) -> NativeCliResult:
        while time.monotonic() < deadline:
            events = await self._messages(task_id, deadline)
            status = _status(events)
            if status == "error":
                raise NativeCliError("manus task failed")
            if status == "waiting":
                raise NativeCliError("manus task is waiting for user input; interactive HITL is not enabled yet")
            if status == "stopped":
                detail_response = await self._request_before(
                    "GET", f"/v2/task.detail?task_id={quote(task_id, safe='')}", deadline
                )
                detail = next(
                    (dict(detail_response[key]) for key in ("task", "data") if isinstance(detail_response.get(key), Mapping)),
                    dict(detail_response),
                )
                if detail.get("status") == "error":
                    raise NativeCliError("manus task failed")
                if detail.get("status") == "waiting":
                    raise NativeCliError("manus task is waiting for user input; interactive HITL is not enabled yet")
                if detail.get("status") != "stopped" or detail.get("has_running_background_jobs") is not False:
                    await asyncio.sleep(min(self._poll_interval, max(0.0, deadline - time.monotonic())))
                    continue
                text = _assistant_text(events)
                if not text:
                    text = str(detail.get("result") or "").strip()
                if not text:
                    raise NativeCliError("manus stopped without a text result")
                return NativeCliResult(
                    provider=self.provider,
                    text=text,
                    session_id=task_id,
                    usage=NativeCliUsage(),
                    duration_seconds=time.monotonic() - started,
                    events=len(events),
                )
            await asyncio.sleep(min(self._poll_interval, max(0.0, deadline - time.monotonic())))
        raise NativeCliError(f"manus task timed out after {int(self._poll_timeout_seconds)}s")
