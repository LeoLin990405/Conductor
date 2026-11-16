#!/usr/bin/env python3
"""BYOM local daemon: pull Tickets and run them inside workspace isolation.

Each Ticket requires a trusted local policy entry for its workspace. The policy
maps workspace ids to distinct roots, approved environment variable names, and
code-only native CLI runtime read paths. The policy file must be owned by the
daemon user, mode 0600 or stricter, and outside workspace and isolation roots.
The isolation state root is created with mode 0700. Provider credentials are
available to a Worker only when their environment variable names are allowed
for that workspace; the daemon token authenticates the daemon transport.

Run after registering a daemon token::

    CONDUCTOR_DAEMON_TOKEN=<token> \
      .venv/bin/python scripts/conductor_local_daemon.py \
      --base-url http://127.0.0.1:8000 --provider codex \
      --workspace-policy /absolute/path/outside-workspaces/workspaces.json \
      --isolation-root /absolute/path/outside-workspaces/worker-state

The native CLI must be installed on this machine. Its runtime directory may be
listed in a workspace's policy only as trusted code; never list credential or
user-home directories there. Missing policy entries or unsupported OS
isolation fail closed. See docs/ports/worker-isolation.md for the policy format
and supported boundary. The isolation root comes from --isolation-root or
CONDUCTOR_ISOLATION_ROOT; the daemon refuses to start when neither is set.
A claim with a Ticket worktree spec creates the checkout on this host from
the claimed base_sha under --worktree-root (never a server absolute path).
A legacy Ticket cwd, when set without a worktree spec, must already exist
under that root or inside the workspace policy root. Blocked/waiting
reports keep a dirty checkout for resume. Once the server accepted a
terminal report or a verified stop, the daemon removes the checkout and
posts the true outcome to the worktree cleanup route; a failed removal or
an unsent outcome stays in a local journal that later polls retry.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import platform
import random
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.conductor_cli.daemon_capability import GrantVerifier, OperationCapability
    from src.conductor_cli.daemon_grant import OriginCheck
    from src.conductor_cli.daemon_policy import PolicyStore
    from src.conductor_cli.provider_breaker import Permit, ProviderCircuitBreaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.conductor_cli import daemon_ticket_worktree as _hw  # noqa: E402

logger = logging.getLogger(__name__)
# A daemon started as a bare script has no logging configuration, and Python's
# default then drops INFO records — which would hide the circuit-close lines
# (cooldown expiry, success reset) that the app-server transport promises to
# operators. Give this module's logger its own stderr handler at INFO. Only
# this logger: dependency libraries keep their defaults, because their debug
# output can carry run text.
if not any(
    getattr(_handler, "_conductor_daemon_circuit", False)
    for _handler in logger.handlers
):
    _circuit_handler = logging.StreamHandler(sys.stderr)
    _circuit_handler.setLevel(logging.INFO)
    _circuit_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    _circuit_handler._conductor_daemon_circuit = True  # type: ignore[attr-defined]
    logger.addHandler(_circuit_handler)
logger.setLevel(logging.INFO)

_NATIVE_CLI_FAILURE_PREFIX = "native CLI failure: "
_SAFE_NATIVE_FAILURE_MESSAGES = {
    "auth_failed": "Worker authentication failed. Check its configured credentials.",
    "rate_limited": "The provider rate limit was reached.",
    "overloaded": "The provider is temporarily overloaded.",
    "network": "The provider could not be reached.",
    "quota_exhausted": "The provider account quota is exhausted.",
    "context_exhausted": "The request exceeded the provider context limit.",
    "turn_budget_exhausted": "The CLI used its whole turn budget before it finished.",
    "tool_blocked": "The CLI's safety gate blocked a tool call it will not run unattended.",
}
_SAFE_NATIVE_FAILURE_REASONS = {
    "deepseek_runtime_dependency_missing": "The DSH CLI runtime is missing a required library.",
    "minimax_runtime_dependency_missing": "The MiniMax CLI runtime is missing a required library.",
    "zcode_model_selection_missing": "ZCode has no model selected in its local profile.",
    "zcode_plan_quota_exhausted": "ZCode plan usage limit reached. Wait for the provider's reset window.",
}


def _failure_detail(exc: BaseException, *, where: str) -> str:
    """The exception type, a scrubbed and bounded sentence, and a correlation id.

    ``RuntimeError: <scrubbed message> (correlation_id=…)``. The message goes
    through ``scrub_public_message``, whose hardening passes drop vendor
    token shapes, secret query parameters, DSN userinfo, SQL bound values,
    key=value secrets, the home directory, and IPv4 addresses. The
    traceback is written to this module's logger. A daemon started by
    cond has stderr pointed at its log file, so that line is the daemon log.
    An id already attached (the native-CLI catch logs the original traceback
    before it wraps the error) is kept, so the report and the log match.
    """
    from src.server.public_errors import (
        log_failure,
        new_correlation_id,
        public_exception_message,
        usable_correlation_id,
    )

    correlation_id = usable_correlation_id(getattr(exc, "correlation_id", None))
    if correlation_id is None:
        correlation_id = new_correlation_id()
        log_failure(correlation_id, exc, log=logger, where=where)
    message = public_exception_message(exc, fallback="no message")
    return f"{type(exc).__name__}: {message} (correlation_id={correlation_id})"


def _classified_native_cli_failure(provider: str, exc: Exception) -> str:
    """Return a bounded cause without carrying vendor diagnostics forward."""
    from src.llms.native_cli.failure import classify_failure
    from src.llms.native_cli.isolation import redact_diagnostic

    diagnostic = str(exc)
    classification = classify_failure(provider, diagnostic)
    # A safe outer error wins. Only fall through a hidden cause when the
    # outer wrapper is unknown, and only accept a provider-specific reason.
    # In particular, a minified ZCode stack can contain words like
    # "Unauthorized" that do not describe the actual failure.
    specific_prefix = {
        "deepseek-dsh": "deepseek_",
        "zcode": "zcode_",
    }.get(provider)
    if classification.reason_code == "worker_unknown" and specific_prefix:
        seen = {id(exc)}
        current = exc.__cause__
        if current is None and not exc.__suppress_context__:
            current = exc.__context__
        for _ in range(7):
            if current is None or id(current) in seen:
                break
            seen.add(id(current))
            candidate = classify_failure(provider, str(current))
            if candidate.reason_code.startswith(specific_prefix):
                classification = candidate
                diagnostic = str(current)
                break
            next_cause = current.__cause__
            if next_cause is not None:
                current = next_cause
            elif not current.__suppress_context__:
                current = current.__context__
            else:
                current = None
    failure_class = classification.failure_class.value
    safe_message = (
        "No API key found."
        if failure_class == "auth_failed" and "no api key found" in diagnostic.lower()
        else _SAFE_NATIVE_FAILURE_REASONS.get(classification.reason_code)
        or _SAFE_NATIVE_FAILURE_MESSAGES.get(failure_class)
    )
    if safe_message is None:
        safe_message = f"CLI failed ({type(exc).__name__})."
    detail = (
        f"{failure_class}/{classification.reason_code}: {safe_message}"
    )
    return redact_diagnostic(detail, None)[:299]

# Providers whose adapter can guarantee a read-only run: Codex, Grok, StepFun
# and ZCode take the flag, Claude Code and Cursor are always in plan mode.
# A run that must not change the device is refused for every other provider.
_READ_ONLY_CAPABLE_PROVIDERS = {
    "claude-code",
    "codex",
    "cursor",
    "grok",
    "stepfun",
    "zcode",
}
_RETRYABLE_STATUS = {429}
_BACKOFF_BASE = 0.25
_BACKOFF_MAX = 5.0
# A server's Retry-After is honoured up to this bound; larger, negative or
# unparseable values fall back to the daemon's own backoff (or the bound).
_RETRY_AFTER_MAX = 300.0
_CANCEL_DRAIN_MAX = 30.0
# Local proof that the process tree is gone, then a bounded retry against a
# server that may still be persisting the stop.
_PROCESS_GONE_GRACE = 3.0
_STOP_ACK_MAX = 10.0


def _persist_breaker_status(
    path: str, provider: str, breaker: ProviderCircuitBreaker
) -> bool:
    if not path:
        return False
    target = Path(path)
    # Reject paths with .. components to prevent directory traversal
    if ".." in target.parts:
        return False
    # Resolve parent to absolute path without following the final symlink component
    target = target.parent.resolve() / target.name
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                {
                    "provider": provider,
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    **breaker.status(),
                },
                stream,
                sort_keys=True,
            )
            stream.write("\n")
        os.replace(temporary, target)
        return True
    finally:
        temporary.unlink(missing_ok=True)


async def _claim_with_breaker(
    api: Api,
    breaker: ProviderCircuitBreaker,
    claim_id: str,
    *,
    capture_start: list[float] | None = None,
    on_cancel_result: Callable[..., Any] | None = None,
    idle_heartbeat_interval: float = 10.0,
    manager: LeaseManager | None = None,
    policy_version: str = "",
) -> tuple[dict[str, Any], Permit, list[float]]:
    """Reserve provider capacity before claiming work; API retry stays separate."""
    next_heartbeat = time.monotonic() + max(0.1, idle_heartbeat_interval)
    while True:
        if manager is not None and manager.fatal_error is not None:
            raise manager.fatal_error
        permit = breaker.acquire()
        if permit is not None:
            started = capture_start if capture_start is not None else []
            try:
                payload = await _retry_call(
                    api.claim,
                    claim_id,
                    policy_version=policy_version,
                    capture_start=started,
                    on_cancel_result=on_cancel_result,
                )
            except BaseException:
                breaker.release(permit)
                raise
            if not payload.get("ticket"):
                breaker.release(permit)
            return payload, permit, started
        if time.monotonic() >= next_heartbeat:
            await _retry_call(api.heartbeat, None)
            next_heartbeat = time.monotonic() + max(0.1, idle_heartbeat_interval)
        if manager is not None and manager.fatal_error is not None:
            raise manager.fatal_error
        retry_in = max(0.05, float(breaker.status()["retry_in_seconds"]))
        until_heartbeat = max(0.05, next_heartbeat - time.monotonic())
        sleep_for = min(retry_in, until_heartbeat, 0.1)
        await asyncio.sleep(sleep_for)


def _cancelled_report() -> dict[str, str]:
    return {
        "state": "failed",
        "summary": "Local daemon was cancelled before completing this Ticket.",
        "question": "",
    }


class ApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        status: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        # Seconds the server asked the daemon to wait (Retry-After), bounded.
        self.retry_after = retry_after


def _parse_retry_after(value: Any, *, now: float | None = None) -> float | None:
    """Seconds from a Retry-After header (delta-seconds or HTTP-date).

    None for a missing, negative or unparseable value; capped at
    ``_RETRY_AFTER_MAX`` so a hostile or broken server cannot park the daemon.
    """
    text = str(value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"[0-9]+", text):
        seconds = _RETRY_AFTER_MAX if len(text) > 6 else float(int(text))
    else:
        from email.utils import parsedate_to_datetime

        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = when.timestamp() - (time.time() if now is None else now)
        if seconds < 0:
            seconds = 0.0
    return min(seconds, _RETRY_AFTER_MAX)


class LeaseLost(RuntimeError):
    pass


async def _drain_task(task: asyncio.Task[Any], timeout: float) -> tuple[bool, Any]:
    """Wait briefly for an in-flight request without letting cancellation stop it."""
    deadline = time.monotonic() + max(0.0, timeout)
    while not task.done():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return False, None
        try:
            done, _ = await asyncio.wait({task}, timeout=remaining)
            if not done:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                return False, None
        except asyncio.CancelledError:
            # A second cancellation must not strand the asyncio wrapper. The
            # worker thread itself is bounded by urllib's per-request timeout.
            continue
    try:
        return True, task.result()
    except asyncio.CancelledError:
        return False, None
    except Exception:  # noqa: BLE001 - task failures go to the supervising caller
        return False, None


#: The completion-report protocol this daemon announces on every claim.
COMPLETION_CONTRACT_VERSION = 1

_LEGACY_STATE = re.compile(
    r"^AUTONOMY_STATE:\s*(ready|blocked|failed|dropped|waiting)\s*$", re.MULTILINE
)
_LEGACY_QUESTION = re.compile(r"^AUTONOMY_QUESTION:\s*(.+?)\s*$", re.MULTILINE)


def _claim_has_completion_contract(ticket: dict[str, Any]) -> bool:
    """Whether the server sent the JSON completion-report prompt for this claim."""
    try:
        return int(ticket.get("completion_contract") or 0) >= COMPLETION_CONTRACT_VERSION
    except (TypeError, ValueError):
        return False


def parse_legacy_report(text: str) -> dict[str, str]:
    """The marker-line report an older server's prompt asked for.

    Used only when the claim did not echo the completion contract (a server
    deployed before it); that prompt names these lines, not the JSON report.
    """
    state_match = _LEGACY_STATE.search(text or "")
    question_match = _LEGACY_QUESTION.search(text or "")
    summary = _LEGACY_QUESTION.sub("", _LEGACY_STATE.sub("", text or ""))
    summary = "\n".join(
        line for line in summary.splitlines() if not line.startswith("AUTONOMY_")
    ).strip()
    return {
        "state": state_match.group(1) if state_match else "ready",
        "body": text or "",
        "summary": (summary or "Native daemon completed the Ticket.")[:20000],
        "question": question_match.group(1)[:4000] if question_match else "",
    }


def parse_report(
    text: str | None,
    *,
    git_summary: str | None = None,
    commit_delivery: str = "not_checked",
    speculative: bool = False,
    baseline_clean: bool = False,
) -> dict[str, str]:
    """Validate and reconcile the Worker's final message as a completion report.

    ``text`` is the final assistant message only. A missing or invalid report
    settles as waiting for a person; prose is never read as success. The
    Git checks (workspace state and new-commit verdict) win over the claim.
    """
    from src.server.services.autonomy.completion import settle_native_completion

    outcome = settle_native_completion(
        text,
        git_summary=git_summary,
        commit_delivery=commit_delivery,
        speculative=speculative,
        baseline_clean=baseline_clean,
    )
    report = {
        "state": outcome["state"],
        "body": outcome["body"],
        "summary": (
            outcome["summary"] or "Native daemon completed the Ticket."
        )[:20000],
        "question": (outcome["question"] or "")[:4000],
    }
    if outcome["attention_reason"]:
        report["attention_reason"] = outcome["attention_reason"]
    return report


def _delivery_baseline(
    baseline: Any,
    host_worktree: dict[str, Any],
    ticket: dict[str, Any],
    *,
    baseline_cached: bool,
) -> Any:
    """The baseline the commit check counts from, across restarts and retries.

    A Ticket checkout counts from its server-pinned base. Without one the
    baseline lives only in this process: when it was taken fresh for a Ticket
    that already ran (its claim counts more than one attempt: a restart, or a
    retry on another daemon), commits the earlier run made would read as
    missing, so the check is not made.
    """
    from src.server.services.autonomy.git_delivery import with_delivery_origin

    base_sha = str(host_worktree.get("base_sha") or "")
    if base_sha:
        return with_delivery_origin(
            baseline, {"version": 1, "known": True, "head": base_sha}
        )
    try:
        attempts = int(ticket.get("attempts") or 0)
    except (TypeError, ValueError):
        attempts = 0
    if not baseline_cached and attempts > 1:
        return with_delivery_origin(baseline, {"version": 1, "known": False})
    return baseline


def _final_assistant_message(result: Any, text: Any) -> Any:
    """The final assistant message of a turn: the completion report.

    A Codex turn can carry progress messages before the report. The daemon's
    streaming runners return ``final_text``; an adapter result keeps its raw
    Codex events, whose last agent message is the report. Any other result
    is one message already.
    """
    final_text = getattr(result, "final_text", None)
    if final_text is not None:
        return final_text
    raw = getattr(result, "raw", None)
    if getattr(result, "provider", None) == "codex" and isinstance(raw, tuple):
        from src.llms.native_cli.codex import codex_final_agent_message

        return codex_final_agent_message(
            [event for event in raw if isinstance(event, dict)]
        )
    return text


def supported_providers() -> set[str]:
    from src.llms.native_cli.registry import list_adapters

    return {str(row["provider"]).lower() for row in list_adapters()}


class Api:
    def __init__(self, base_url: str, token: str, *, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "X-Daemon-Token": self.token},
        )
        try:
            with urllib.request.urlopen(
                req, timeout=min(timeout, self.timeout) if timeout else self.timeout
            ) as resp:
                if resp.status == 204:
                    return None
                return json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as exc:
            # Response bodies can echo request data. Keep status/path only.
            retryable = exc.code in _RETRYABLE_STATUS or exc.code >= 500
            retry_after = None
            if retryable:
                headers = getattr(exc, "headers", None)
                retry_after = _parse_retry_after(
                    headers.get("Retry-After") if headers is not None else None
                )
            raise ApiError(
                f"{method} {path} -> HTTP {exc.code}",
                exc.code,
                retryable,
                retry_after=retry_after,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ApiError(
                f"{method} {path} transport error: {exc}", retryable=True
            ) from exc

    def claim(
        self,
        claim_id: str,
        *,
        policy_version: str = "",
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return (
            self._call(
                "POST",
                "/api/v1/autonomy/daemon/claim",
                {
                    "claim_id": claim_id,
                    "policy_version": policy_version,
                    # This daemon parses the JSON completion report; a server
                    # that echoes it on the ticket sent that prompt.
                    "completion_contract": COMPLETION_CONTRACT_VERSION,
                },
                timeout=timeout,
            )
            or {}
        )

    def heartbeat(
        self,
        leases: list[dict[str, str]] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        body = {"leases": leases} if leases else None
        return self._call(
            "POST", "/api/v1/autonomy/daemon/heartbeat", body, timeout=timeout
        )

    def ticket_status(
        self, ticket_id: str, *, timeout: float | None = None
    ) -> dict[str, Any]:
        return self._call(
            "GET",
            f"/api/v1/autonomy/daemon/tickets/{ticket_id}/status",
            timeout=timeout,
        )

    def lease_status(
        self, lease_id: str, *, timeout: float | None = None
    ) -> dict[str, Any]:
        # One shot. A retry that eventually succeeds would hide a server the
        # daemon could not reach, and the device fails closed instead.
        return (
            self._call(
                "GET",
                f"/api/v1/autonomy/daemon/lease/{lease_id}/status",
                timeout=timeout,
            )
            or {}
        )

    def report(
        self,
        ticket_id: str,
        lease_id: str,
        report: dict[str, str],
        *,
        timeout: float | None = None,
    ) -> Any:
        return self._call(
            "POST",
            f"/api/v1/autonomy/daemon/tickets/{ticket_id}/report",
            {**report, "lease_id": lease_id},
            timeout=timeout,
        )

    def output(
        self,
        ticket_id: str,
        lease_id: str,
        run_id: str,
        events: list[dict[str, Any]],
        *,
        epoch: int | None = None,
        batch_seq: int = 1,
        timeout: float | None = None,
    ) -> Any:
        body: dict[str, Any] = {
            "lease_id": lease_id,
            "run_id": run_id,
            "batch_seq": batch_seq,
            "events": events,
        }
        if epoch is not None:
            # Only ever the epoch the server allocated; a first batch has none.
            body["epoch"] = epoch
        return self._call(
            "POST",
            f"/api/v1/autonomy/daemon/tickets/{ticket_id}/output",
            body,
            timeout=timeout,
        )

    def stop_ack(
        self,
        ticket_id: str,
        lease_id: str,
        *,
        process_gone: bool,
        evidence: dict[str, Any],
        timeout: float | None = None,
    ) -> Any:
        return self._call(
            "POST",
            f"/api/v1/autonomy/daemon/tickets/{ticket_id}/stop/ack",
            {
                "lease_id": lease_id,
                "process_gone": process_gone,
                **evidence,
            },
            timeout=timeout,
        )

    def bind_worktree(
        self,
        ticket_id: str,
        lease_id: str,
        *,
        worktree_path: str,
        branch: str,
        base_sha: str,
        timeout: float | None = None,
    ) -> Any:
        # DaemonWorktreeBind has no base_branch: the server pinned the base at
        # claim time, so the daemon only echoes the base_sha it was cut from.
        return self._call(
            "POST",
            f"/api/v1/autonomy/daemon/tickets/{ticket_id}/worktree",
            {
                "lease_id": lease_id,
                "worktree_path": worktree_path,
                "branch": branch,
                "base_sha": base_sha,
            },
            timeout=timeout,
        )

    def worktree_cleanup(
        self,
        ticket_id: str,
        *,
        lease_id: str,
        worktree_path: str,
        worktree_generation: int,
        outcome: str,
        detail: str = "",
        timeout: float | None = None,
    ) -> Any:
        return self._call(
            "POST",
            f"/api/v1/autonomy/daemon/tickets/{ticket_id}/worktree/cleanup",
            {
                "lease_id": lease_id,
                "worktree_path": worktree_path,
                "worktree_generation": worktree_generation,
                "outcome": outcome,
                "detail": detail,
            },
            timeout=timeout,
        )


async def _retry_call(
    fn: Callable[..., Any],
    *args: Any,
    deadline: float | Callable[[], float] | None = None,
    **kwargs: Any,
) -> Any:
    attempt = 0
    timeout = kwargs.pop("timeout", None)
    capture_start = kwargs.pop("capture_start", None)
    on_cancel_result = kwargs.pop("on_cancel_result", None)
    api_timeout = getattr(getattr(fn, "__self__", None), "timeout", None)
    while True:
        server_wait: float | None = None
        limit = deadline() if callable(deadline) else deadline
        attempt_timeout = timeout if timeout is not None else api_timeout
        if limit is not None:
            remaining = limit - time.monotonic()
            if remaining <= 0:
                raise LeaseLost("lease expired before request completed")
            attempt_timeout = min(attempt_timeout or remaining, remaining)
        try:
            request_started = time.monotonic()
            request = asyncio.create_task(
                asyncio.to_thread(
                    fn,
                    *args,
                    **kwargs,
                    **(
                        {"timeout": attempt_timeout}
                        if attempt_timeout is not None
                        else {}
                    ),
                )
            )
            try:
                result = await asyncio.shield(request)
            except asyncio.CancelledError:
                drain_for = min(attempt_timeout or _CANCEL_DRAIN_MAX, _CANCEL_DRAIN_MAX)
                if limit is not None:
                    drain_for = min(drain_for, max(0.0, limit - time.monotonic()))
                settled, result = await _drain_task(request, drain_for)
                if settled and on_cancel_result is not None:
                    try:
                        cleanup = asyncio.create_task(
                            on_cancel_result(result, request_started)
                        )
                        await _drain_task(cleanup, drain_for)
                    except (
                        Exception
                    ):  # noqa: BLE001, S110 - preserve cancellation during best-effort cleanup
                        pass
                raise
            if capture_start is not None:
                capture_start[:] = [request_started]
            return result
        except ApiError as exc:
            if not exc.retryable:
                raise
            server_wait = exc.retry_after
        delay = min(
            _BACKOFF_MAX, _BACKOFF_BASE * (2 ** min(attempt, 8))
        ) * random.uniform(0.5, 1.0)
        if server_wait is not None:
            # A 503/429 that names its wait: never come back sooner.
            delay = max(delay, min(float(server_wait), _RETRY_AFTER_MAX))
        attempt += 1
        limit = deadline() if callable(deadline) else deadline
        if limit is not None:
            remaining = limit - time.monotonic()
            if remaining <= 0:
                raise LeaseLost("lease expired during request retry")
            delay = min(delay, remaining)
        await asyncio.sleep(delay)


_OUTPUT_STREAM_FLUSH_S = 0.2
_OUTPUT_STREAM_BATCH_EVENTS = 32
_OUTPUT_STREAM_MAX_EVENTS = 256
_OUTPUT_STREAM_MAX_CHARS = 128 * 1024
_OUTPUT_STREAM_TEXT_MAX = 2000
_OUTPUT_STREAM_DISABLED_STATUS = frozenset({401, 403, 404, 405, 501})
# One output POST's ceiling. Small on purpose: the final flush must stay a
# bounded delay even when the server is unreachable.
_OUTPUT_STREAM_REQUEST_TIMEOUT_S = 2.0
# The whole final flush, however many batches are queued and however slowly an
# alive server answers each one: the report waits for it, and so does a stop
# acknowledgement behind it. A timeout cannot cut a request short (it applies to
# each socket operation, not to the request), so the budget is on the flush.
_OUTPUT_STREAM_FINAL_FLUSH_S = 3.0
# A single vendor message longer than this is not streamed: truncating it would
# publish a prefix, so the run's text channel stops instead.
_OUTPUT_STREAM_TEXT_LIMIT = 64 * 1024


# Shorter than this a fragment of an environment value is not worth a stop; the
# longest head of a value that can be forwarded is one character less.
_ENV_PREFIX_MIN = 6


class _EnvContinuity:
    """Device-side continuity for credential values split between messages.

    The consumer concatenates only the messages that were actually forwarded,
    so a withheld fragment cannot be retracted: a message that ends inside a
    value must be withheld *before* the rest arrives, or all but the last
    character of it is already stored. Once any segment is withheld or
    redacted, the run's text channel is tainted: no later text is forwarded (a
    later tail could otherwise complete a split credential by itself). Tool
    names carry no text and may still flow.
    """

    def __init__(self, values: tuple[str, ...]) -> None:
        self._values = tuple(value for value in values if value)
        longest = max((len(value) for value in self._values), default=0)
        self._limit = max(longest + 256, 1024)
        self._window = ""
        self._tainted = False

    @property
    def tainted(self) -> bool:
        return self._tainted

    def taint(self) -> None:
        self._tainted = True

    def observe(self, visible_text: str) -> bool:
        """Return True only when this visible text may be forwarded."""
        if self._tainted:
            return False
        combined = self._window + visible_text
        if any(value in combined for value in self._values) or any(
            self._ends_inside(combined, value) for value in self._values
        ):
            self._tainted = True
            return False
        self._window = combined[-self._limit :]
        return True

    @staticmethod
    def _ends_inside(combined: str, value: str) -> bool:
        """Whether ``combined`` ends with the first characters of ``value``.

        At least ``_ENV_PREFIX_MIN`` of them, and not all: the next message can
        bring the rest, and by then the head would already be shown.
        """
        if len(value) <= _ENV_PREFIX_MIN:
            return False
        first = value[0]
        at = combined.find(first, max(0, len(combined) - len(value) + 1))
        while at != -1:
            tail = combined[at:]
            if len(tail) >= _ENV_PREFIX_MIN and value.startswith(tail):
                return True
            at = combined.find(first, at + 1)
        return False


def _cap_stream_message(text: str) -> str:
    """Bound one streamed message without cutting a token in half.

    A cap boundary that splits a credential token would publish its prefix
    even though the full-message scan never saw a match. Cut at the last
    whitespace before the cap instead; a cap-sized single token is dropped
    entirely (it cannot be safely split).
    """
    if len(text) <= _OUTPUT_STREAM_TEXT_LIMIT:
        return text
    cut = text[:_OUTPUT_STREAM_TEXT_LIMIT]
    boundary = max(cut.rfind(" "), cut.rfind("\n"))
    return cut[:boundary] if boundary > 0 else ""


def _split_delta_pieces(text: str, limit: int) -> list[str] | None:
    """Split a delta at whitespace without adding display newlines.

    Unlike ``_split_stream_message`` every returned piece is the text exactly
    as the model produced it; the sink marks a piece's final newline synthetic
    only when the piece itself does not end with one. A run that cannot be cut
    is refused rather than published as a prefix.
    """
    if len(text) <= limit:
        return [text]
    pieces: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = max(rest.rfind(" ", 0, limit), rest.rfind("\n", 0, limit))
        if cut <= 0:
            return None
        pieces.append(rest[: cut + 1])
        rest = rest[cut + 1 :]
    if rest:
        pieces.append(rest)
    return pieces


def _split_stream_message(
    text: str, limit: int = _OUTPUT_STREAM_TEXT_MAX
) -> list[tuple[str, bool]] | None:
    """Split one message into transport events as ``(text, join)`` pairs.

    Every event but the last is cut at whitespace and ends in it. A message that
    ends mid-word gets a newline so it ends on a display line break, and that
    final event is marked ``join``: the newline is synthetic, so for credential
    continuity the message runs straight into the next one (a token split across
    two messages is still one token). ``None`` means the message holds an
    unbroken run longer than one event, which cannot be split safely.
    """
    if not text:
        return []
    join = not text[-1].isspace()
    if join:
        text += "\n"
    chunks: list[tuple[str, bool]] = []
    rest = text
    while len(rest) > limit:
        cut = -1
        for index in range(limit - 1, 0, -1):
            if rest[index].isspace():
                cut = index
                break
        if cut < 0:
            return None
        chunks.append((rest[: cut + 1], False))
        rest = rest[cut + 1 :]
    chunks.append((rest, join))
    return chunks


def _token_stream_enabled() -> bool:
    """Whether this daemon may post live output (default on, best effort).

    ``CONDUCTOR_DAEMON_TOKEN_STREAM=0`` turns it off for operators that do not
    want the extra traffic; the server also accepts deployments without the
    endpoint (the sink disables itself on the first 404/405/501).
    """
    value = os.environ.get("CONDUCTOR_DAEMON_TOKEN_STREAM", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


class _DaemonOutputStream:
    """Best-effort live output for one claimed Worker run.

    The vendor CLI's visible text/tool names are buffered and posted in small,
    fenced batches: the first carries no epoch, the server allocates one, and
    every later batch echoes it with an incremented ``batch_seq``. Text is
    scanned here with the same :mod:`output_guard` the server runs, so the
    daemon never sends what the server would taint, and the first sign of
    trouble stops the stream (text and tool names alike) for good:

    * a local scan refusal or a ``discontinuity`` (a skipped/oversized record),
    * a buffer overflow or any transport/HTTP error (including
      ``stale_output_epoch``),
    * an acknowledgement that is not exactly ``stored: true, tainted: false``
      (a missing or non-boolean field is unknown, not clean),
    * an acknowledgement whose epoch is not the one this run was given.

    Tool names are scanned in the same windows as text, so they stop with it:
    names that outlived the text would form a projection no window tracks. The
    local guard advances only past events that were queued, so it always equals
    what the server will have received. A stop is announced to the server (a
    ``stop`` event) when it may hold events.
    Nothing here may fail, slow, or block the Worker beyond the final bounded
    flush before its report.
    """

    def __init__(self, api: Api, ticket_id: str, lease_id: str, run_id: str) -> None:
        self._api = api
        self._ticket_id = ticket_id
        self._lease_id = lease_id
        self._run_id = run_id
        self._pending: list[dict[str, Any]] = []
        self._pending_chars = 0
        self._task: asyncio.Task[None] | None = None
        self._disabled = False
        self._text_disabled = False
        self._closed = False
        self._lock = asyncio.Lock()
        self._add_lock = asyncio.Lock()
        # The same continuity guard the server runs on every live-output event
        # (imported lazily: only a streaming run pays for it), so the daemon
        # never sends text the server would taint and the rules cannot drift.
        from src.server.utils import output_guard

        self._output_guard = output_guard
        self._guard = output_guard.GuardState()
        self._epoch: int | None = None
        self._batch_seq = 0
        # Set the moment a batch is taken for sending: during the first request
        # the epoch is still unknown and the batch has left the queue, yet the
        # server may already hold text, so a stop must still be announced.
        self._attempted = False
        self.stop_reason = ""

    @property
    def disabled(self) -> bool:
        return self._disabled

    @property
    def text_disabled(self) -> bool:
        return self._disabled or self._text_disabled

    @property
    def epoch(self) -> int | None:
        return self._epoch

    def start(self) -> None:
        if self._task is None and not self._disabled:
            self._task = asyncio.create_task(self._flush_loop())

    def stop_text(
        self, reason: str, *, drop_pending: bool = False, notify: bool = True
    ) -> None:
        """Permanently stop the stream (text and tool names alike).

        Whatever is already queued was scanned in order and is a complete, clean
        prefix, so it is still sent unless ``drop_pending`` (the server will
        never store more for this epoch, or the queue overflowed and a hole is
        no prefix). Nothing is queued afterwards: names that outlived the text
        would form a projection of their own that no window tracks. Unless
        ``notify`` is off, a ``stop`` event follows whatever is queued so the server taints
        the epoch and watchers receive an ``output_reset`` instead of an
        unexplained end.
        """
        first = not self._text_disabled
        if first:
            self._text_disabled = True
            self.stop_reason = self.stop_reason or reason
        # Decide before dropping anything: the server may already hold text from
        # this run (an attempted batch, or text that was queued).
        announce = (
            first
            and notify
            and not self._disabled
            and not self._closed
            and (
                self._epoch is not None
                or self._attempted
                # Whatever stays queued will reach the server (queued text unless
                # it is about to be dropped; tool names always) and watchers
                # would otherwise see it simply stop.
                or any(
                    event.get("kind") == "tool"
                    or (
                        not drop_pending
                        and event.get("kind") in {"text", "reasoning"}
                    )
                    for event in self._pending
                )
            )
        )
        if drop_pending:
            self._pending = [
                event for event in self._pending if event.get("kind") == "stop"
            ]
            self._pending_chars = 0
        if announce:
            # Not through _push: the stop marker must survive a full buffer.
            self._pending.append({"kind": "stop"})

    def discontinuity(self, reason: str) -> None:
        """The CLI skipped bytes of its output: text continuity is unprovable."""
        self.stop_text(f"discontinuity:{reason}")

    def _disable(self, reason: str) -> None:
        self._disabled = True
        self._text_disabled = True
        self.stop_reason = self.stop_reason or reason
        self._pending.clear()
        self._pending_chars = 0

    async def add(self, event: dict[str, Any]) -> None:
        """Buffer one visible event; never awaits the network.

        One event at a time: the text path judges a message against a snapshot
        of the guard and yields between its chunks, so a second caller that
        slipped in would have its guard advance overwritten while its event
        stays queued ahead of the text, and this copy of the state would no
        longer be the one the server holds. Only the event loop is waited for,
        so the lock is never held across a request.
        """
        async with self._add_lock:
            await self._add(event)

    async def _add(self, event: dict[str, Any]) -> None:
        if self._disabled or self._closed:
            return
        kind = str(event.get("kind") or "")
        if kind == "tool":
            # One stdout read can hold a thousand tool records, each scanned
            # against full windows: yield per record, as the text path does, so
            # the flood cannot starve cancellation, lease renewal or flushing.
            # First, so the checks, the scan and the push below stay one
            # uninterrupted step a stop from the flusher cannot land inside.
            await asyncio.sleep(0)
            names = [str(name)[:64] for name in event.get("tools") or () if name]
            if not names or self._text_disabled:
                # After a stop the stream carries nothing more: names that
                # outlive the text would form a projection no window tracks.
                return
            # Tool names are rendered beside the text, so the server counts them
            # in the same continuity windows; mirror that here so this sink
            # never sends what the server would refuse.
            decision = self._output_guard.scan(
                self._guard,
                "\n".join(names[:8]) + "\n",
                channel=self._output_guard.CHANNEL_TOOL,
            )
            if not decision.ok:
                self._guard = decision.state
                self.stop_text(f"guard:{decision.reason}")
                return
            # The guard moves past a name only once it is queued: a name the
            # buffer dropped never reaches the server, whose windows must stay
            # the ones this sink judges the next event against.
            if self._push({"kind": "tool", "tools": names[:8]}, 0):
                self._guard = decision.state
            return
        if event.get("delta"):
            # App-server token deltas are transport-sized pieces of one message.
            # Scan them with the guard's synthetic newline so continuity is
            # judged exactly as a completed message, but store the text without
            # that newline: a delta boundary is not a display line boundary.
            # A completed-message fallback can arrive as one delta larger than
            # the guard's per-event cap; split it at whitespace first.
            text = str(event.get("text") or "")
            if kind not in {"text", "reasoning"} or not text:
                return
            if self._text_disabled:
                return
            pieces = _split_delta_pieces(text, _OUTPUT_STREAM_TEXT_MAX)
            if pieces is None:
                self.stop_text("unsplittable_run")
                return
            state = self._guard
            accepted: list[tuple[str, bool]] = []
            for piece in pieces:
                synthetic = not piece.endswith("\n")
                scan_text = piece + "\n" if synthetic else piece
                decision = self._output_guard.scan(
                    state, scan_text, channel=kind, join=synthetic
                )
                if not decision.ok:
                    self._guard = decision.state
                    self.stop_text(f"guard:{decision.reason}")
                    return
                stored = decision.text
                if synthetic and stored.endswith("\n"):
                    stored = stored[:-1]
                if stored:
                    accepted.append((stored, synthetic))
                state = decision.state
            self._guard = state
            for stored, synthetic in accepted:
                row: dict[str, Any] = {
                    "kind": kind,
                    "text": stored,
                    "delta": True,
                }
                if synthetic:
                    row["join"] = True
                if self._push(row, len(stored)):
                    self._guard = state
            return
        text = str(event.get("text") or "")
        if kind not in {"text", "reasoning"} or not text:
            return
        if self._text_disabled:
            return
        if len(text) > _OUTPUT_STREAM_TEXT_LIMIT:
            self.stop_text("message_too_large")
            return
        chunks = _split_stream_message(text)
        if chunks is None:
            self.stop_text("unsplittable_run")
            return
        # Scan every chunk before queueing any: a refusal must not leave the
        # message's earlier chunks queued as a published prefix.
        state = self._guard
        accepted: list[tuple[str, bool]] = []
        for chunk, join in chunks:
            # One scan is a few milliseconds; yield between them so a 64 KiB
            # message cannot hold up cancellation, lease renewal or flushing.
            await asyncio.sleep(0)
            decision = self._output_guard.scan(state, chunk, channel=kind, join=join)
            if not decision.ok:
                self._guard = decision.state
                self.stop_text(f"guard:{decision.reason}")
                return
            state = decision.state
            if decision.text:
                accepted.append((decision.text, join))
        self._guard = state
        for chunk, join in accepted:
            row: dict[str, Any] = {"kind": kind, "text": chunk}
            if join:
                row["join"] = True
            self._push(row, len(chunk))
            if self._disabled or self._text_disabled:
                # An overflow (or any stop) mid-message: the rest of the message
                # is a suffix after a hole and must not be queued.
                return

    def _push(self, event: dict[str, Any], chars: int) -> bool:
        """Queue one event; False when it was not queued."""
        if self._disabled or self._closed:
            return False
        if self._text_disabled and event.get("kind") != "tool":
            # Once text has stopped nothing but tool names is ever queued, so a
            # later chunk of the same message cannot slip in behind the stop.
            return False
        if (
            len(self._pending) >= _OUTPUT_STREAM_MAX_EVENTS
            or self._pending_chars + chars > _OUTPUT_STREAM_MAX_CHARS
        ):
            if event.get("kind") == "tool":
                # A name is a row of its own, not part of any prose: dropping it
                # leaves no hole in the text, only a missing display row.
                return False
            # Dropping buffered text would leave a hole between what the server
            # holds and what comes next, so text stops for good: the queued
            # text is discarded, a stop marker follows what is already in
            # flight.
            self.stop_text("buffer_overflow", drop_pending=True)
            return False
        self._pending.append(event)
        self._pending_chars += chars
        return True

    async def _flush_loop(self) -> None:
        while not self._closed and not self._disabled:
            await asyncio.sleep(_OUTPUT_STREAM_FLUSH_S)
            await self._flush_once()

    async def _flush_once(self) -> bool:
        """Send at most one batch. Returns False when the channel stopped.

        The lock covers the request too: shutdown and the background flusher
        can never have two batches in flight, so the stream cannot reorder and
        ``batch_seq`` is strictly sequential.
        """
        async with self._lock:
            if self._disabled or not self._pending:
                return True
            batch = self._pending[:_OUTPUT_STREAM_BATCH_EVENTS]
            del self._pending[:_OUTPUT_STREAM_BATCH_EVENTS]
            self._pending_chars = max(
                0,
                self._pending_chars
                - sum(len(str(event.get("text") or "")) for event in batch),
            )
            self._batch_seq += 1
            self._attempted = True
            try:
                ack = await asyncio.to_thread(
                    self._api.output,
                    self._ticket_id,
                    self._lease_id,
                    self._run_id,
                    batch,
                    epoch=self._epoch,
                    batch_seq=self._batch_seq,
                    timeout=_OUTPUT_STREAM_REQUEST_TIMEOUT_S,
                )
            except ApiError:
                # Any rejection (a stale lease, ``stale_output_epoch``, a
                # missing endpoint) and any transport error below may mean the
                # server stored this batch (or not) with a context this client
                # can no longer track; stop the channel rather than publish
                # fragments without continuity.
                self._disable("api_error")
                return False
            except Exception:  # noqa: BLE001 - streaming is never a Worker failure
                self._disable("transport_error")
                return False
            return self._accept_ack(ack)

    def _accept_ack(self, ack: Any) -> bool:
        """Apply the server's answer; any doubt stops the channel."""
        if not isinstance(ack, dict):
            self._disable("bad_ack")
            return False
        stored, tainted = ack.get("stored"), ack.get("tainted")
        # Identity, not truthiness: 0 == False and a missing key is not "no".
        # The channel stays open only on exactly stored:true, tainted:false.
        if tainted is not True and (stored is not True or tainted is not False):
            # stored:false means the server dropped this batch (for example the
            # store is down): the next one could be joined to a prefix whose
            # context is gone. Anything else is an answer this client cannot
            # read, which is no proof the stream is clean.
            self._disable("not_stored" if stored is False else "bad_ack")
            return False
        epoch = ack.get("epoch")
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
            self._disable("bad_epoch")
            return False
        if self._epoch is None:
            self._epoch = epoch
        elif epoch != self._epoch:
            self._disable("epoch_mismatch")
            return False
        if tainted is True:
            # The server will never store text for this epoch again.
            self.stop_text(
                f"server_taint:{ack.get('reason') or ''}"[:80],
                drop_pending=True,
                notify=False,
            )
            return False
        return True

    async def _flush_all(self) -> None:
        # One failing batch stops the drain instead of retrying every buffered
        # batch: an outage must not multiply the report's delay.
        while self._pending and not self._disabled:
            if not await self._flush_once():
                return

    async def finalize(self, *, flush: bool = True) -> None:
        """Stop the flusher and flush the tail without swallowing cancellation.

        The loop is asked to stop and is awaited (not cancelled) so its
        in-flight request is the only one that can land; cancellation of this
        coroutine is re-raised after the bounded cleanup instead of being
        swallowed into a normal report path. The whole call is bounded by
        ``_OUTPUT_STREAM_FINAL_FLUSH_S``: a server that answers, but slowly,
        cannot hold the report back, and when time runs out the channel is
        disabled (the server keeps the clean prefix it has).

        ``flush=False`` publishes nothing more: the lease is gone or the run is
        being torn down, and a stop acknowledgement must not wait behind a flush.
        """
        self._closed = True
        task, self._task = self._task, None
        if not flush:
            self._pending.clear()
            self._pending_chars = 0
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            return
        try:
            await asyncio.wait_for(self._drain(task), _OUTPUT_STREAM_FINAL_FLUSH_S)
        except TimeoutError:
            if task is not None and not task.done():
                task.cancel()
            self._disable("flush_timeout")

    async def _drain(self, task: asyncio.Task[None] | None) -> None:
        cancelled = False
        if task is not None and not task.done():
            try:
                await task
            except asyncio.CancelledError:
                cancelled = True
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001 - the flusher only logs
                pass
        if cancelled:
            raise asyncio.CancelledError()
        try:
            await self._flush_all()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - streaming is never a Worker failure
            pass


class _StreamPolicyGate:
    """Withhold live output the operation capability would refuse on report.

    The daemon already refuses a result whose text names an operation outside
    the lease capability; the live channel must apply the same policy *before*
    publication, otherwise an unauthorized operation would stream even though
    the report is rejected. A small rolling window catches an operation split
    across two model items; once refused, the run publishes nothing further.
    """

    _WINDOW_CHARS = 4096

    def __init__(
        self,
        policy: Any,
        capability: OperationCapability | None,
        **result_options: Any,
    ) -> None:
        self._policy = policy
        self._capability = capability
        # N71: lease / mode / provider / audit for ``_result_refusal``.
        self._options = result_options
        # A true trailing-character window: an operation can straddle two model
        # messages, so keeping whole-message entries would evict the tail that
        # matters before the next item is checked.
        self._window = ""
        self._blocked = False

    @property
    def blocked(self) -> bool:
        return self._blocked

    def allow_text(self, text: str) -> bool:
        if self._capability is None:
            return True
        if self._blocked:
            return False
        if not text:
            return True
        if _result_refusal(self._policy, self._capability, text, **self._options):
            self._blocked = True
            return False
        # Check the whole overlap plus the incoming item BEFORE trimming it to
        # the window: trimming first would let a long message push the first
        # half of an operation out of the check.
        combined = self._window + text
        if _result_refusal(
            self._policy, self._capability, combined, **self._options
        ):
            self._blocked = True
            return False
        self._window = combined[-self._WINDOW_CHARS :]
        return True


async def _run_codex_streaming(
    adapter: Any,
    prompt: str,
    cwd: str,
    streamer: _DaemonOutputStream | None,
    gate: _StreamPolicyGate | None = None,
) -> Any:
    """Run a Codex turn while forwarding visible output to ``streamer``.

    The transcript is still collected (bounded) and parsed by the provider's
    normal result parser, so the Worker's report path is unchanged. Display
    text passes through the isolation redactor before it is buffered.

    Codex reports completed items, not token deltas, so the channel is
    line-incremental: tool/progress items appear as the CLI emits them and each
    completed agent message appears in full. The daemon still posts only the
    vendor CLI's *visible* output, never its prompt, tool arguments or results.
    """
    from src.llms.native_cli.codex import (
        codex_final_agent_message,
        no_final_answer_error,
        parse_codex_events,
        parse_codex_output_events,
    )
    from src.llms.native_cli.isolation import redact_diagnostic
    from src.llms.native_cli.process import stream_process

    from src.server.utils import output_guard
    from src.server.utils.sanitize import sanitize_output_text

    invocation = adapter.build_invocation(prompt, cwd=cwd)
    from src.llms.native_cli.isolation import credential_values

    continuity = _EnvContinuity(credential_values(None))
    lines: list[str] = []
    size = 0

    def discontinuity(reason: str) -> None:
        # The CLI skipped bytes of its transcript (an oversized record): a
        # credential boundary may have been in them, so no later text can be
        # proven to continue what was already sent.
        continuity.taint()
        if streamer is not None:
            streamer.discontinuity(reason)

    async for line in stream_process(invocation, on_discontinuity=discontinuity):
        if size < 16 * 1024 * 1024:
            lines.append(line)
            size += len(line) + 1
        malformed: list[bool] = []
        events = parse_codex_output_events(
            line, on_malformed=lambda: malformed.append(True)
        )
        if malformed:
            # A line that is not a JSON object is corruption, not filtering:
            # whatever it carried is not in the stream, so stop text here.
            continuity.taint()
            if streamer is not None:
                streamer.discontinuity("malformed_record")
        for event in events:
            if streamer is None or streamer.disabled:
                continue
            if gate is not None and gate.blocked:
                # Policy already refused this run's text; the sink stops (and
                # announces it) rather than let later events follow a hole.
                streamer.stop_text("policy_refused")
                continue
            if event.get("kind") in {"text", "reasoning"}:
                if continuity.tainted or streamer.text_disabled:
                    continue
                # Forward the vendor's raw visible text (minus environment
                # credential values); the sink scans it with the shared
                # continuity guard and the server scans it again against the
                # persisted stream state. A redaction or a transport-cap
                # truncation is a boundary the daemon cannot model, so it
                # stops the run's text channel.
                original = str(event.get("text") or "")
                capped = _cap_stream_message(original)
                raw = redact_diagnostic(capped, None)
                if output_guard.has_control(raw):
                    # A terminal control (or hidden character) ends the text
                    # stream. Refuse it *before* any per-character processing:
                    # sanitizing repeated unterminated introducers is not linear.
                    continuity.taint()
                    streamer.stop_text("control_character")
                    continue
                # Control-free text reads the same sanitized or not for the two
                # checks below (env credential values, policy), and skips a
                # pass over a message that can be 64 KiB.
                visible = raw
                if raw != capped or capped != original:
                    continuity.taint()
                    streamer.stop_text("redaction_boundary")
                    continue
                if not continuity.observe(visible):
                    streamer.stop_text("credential_value")
                    continue
                if gate is not None and not gate.allow_text(visible):
                    streamer.stop_text("policy_refused")
                    continue
                event = {"kind": event["kind"], "text": raw}
            elif event.get("kind") == "tool":
                names = [
                    sanitize_output_text(name, limit=64).strip()
                    for name in event.get("tools") or ()
                ]
                names = [name for name in names if name]
                if not names:
                    continue
                event = {"kind": "tool", "tools": names[:8]}
            await streamer.add(event)
    text, session_id, usage, events = parse_codex_events("\n".join(lines))
    if not text:
        # A completed turn without an agent message has no completion report
        # (settled as waiting); a failed turn stays a CLI failure.
        raise no_final_answer_error("codex produced no final answer", events)
    # ``text`` joins every agent message (progress updates included) and is
    # what the result policy scans; the completion report is the last one.
    return SimpleNamespace(
        text=text,
        final_text=codex_final_agent_message(events),
        session_id=session_id,
        usage=usage,
        events=len(events),
    )


def _codex_app_server_enabled() -> bool:
    """Whether a Codex streaming run uses the app-server token transport.

    The app-server path is the only one that can yield token deltas. It can be
    disabled for one host with ``CONDUCTOR_CODEX_APP_SERVER=0`` while the exec
    path remains available as an operational fallback.
    """
    value = os.environ.get("CONDUCTOR_CODEX_APP_SERVER", "1").strip().lower()
    return value not in {"0", "false", "no", "off", "disabled"}


_APP_SERVER_FAILURES = 0
_APP_SERVER_OPEN_UNTIL = 0.0
_APP_SERVER_OPEN = False

# A server-reported ``error`` event says the model or task failed, not the
# transport, so those exceptions carry this marker and the circuit ignores
# them. The marker rides on the instance because a ``NativeCliError`` subclass
# cannot be defined at module level without importing ``src`` at daemon start.
_TURN_FAILURE_ATTR = "app_server_turn_failure"


def _app_server_turn_error(message: str) -> Exception:
    """Build the failure for one app-server ``error`` event (a turn failure)."""
    from src.llms.native_cli.base import NativeCliError

    error = NativeCliError(message)
    setattr(error, _TURN_FAILURE_ATTR, True)
    return error


def _app_server_failure_counts(exc: BaseException) -> bool:
    """Pure: whether one app-server failure counts toward the local circuit.

    Only transport failures count: a subprocess that will not start, an
    ``initialize``/``thread/*``/``turn/start`` RPC that times out or errors, a
    reader that dies unexpectedly, a protocol violation (an approval request in
    a non-interactive run), or a turn that ends without a final answer. A
    failure the server reported for the model or task itself (an ``error``
    event) and the user's cancellation say nothing about the transport and
    never count.
    """
    if isinstance(exc, asyncio.CancelledError):
        return False
    from src.llms.native_cli.base import NativeCliNoFinalAnswer

    if isinstance(exc, NativeCliNoFinalAnswer):
        # A completed turn without a message is not a transport failure.
        return False
    return not getattr(exc, _TURN_FAILURE_ATTR, False)


def _app_server_failure_limit() -> int:
    raw = os.environ.get("CONDUCTOR_CODEX_APP_SERVER_FAILURE_LIMIT", "3").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 3


def _app_server_cooldown_seconds() -> float:
    raw = os.environ.get("CONDUCTOR_CODEX_APP_SERVER_COOLDOWN", "300").strip()
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 300.0


def _reset_app_server_circuit() -> None:
    global _APP_SERVER_FAILURES, _APP_SERVER_OPEN_UNTIL, _APP_SERVER_OPEN
    _APP_SERVER_FAILURES = 0
    _APP_SERVER_OPEN_UNTIL = 0.0
    _APP_SERVER_OPEN = False


def _app_server_available() -> bool:
    """False while repeated app-server failures have opened the local circuit."""
    global _APP_SERVER_OPEN
    if _APP_SERVER_OPEN and time.monotonic() >= _APP_SERVER_OPEN_UNTIL:
        _APP_SERVER_OPEN = False
        logger.info(
            "codex app-server circuit closed after the cooldown; "
            "codex turns use the app-server transport again"
        )
    return time.monotonic() >= _APP_SERVER_OPEN_UNTIL


def _record_app_server_failure() -> None:
    global _APP_SERVER_FAILURES, _APP_SERVER_OPEN_UNTIL, _APP_SERVER_OPEN
    _APP_SERVER_FAILURES += 1
    limit = _app_server_failure_limit()
    if _APP_SERVER_FAILURES >= limit:
        cooldown = _app_server_cooldown_seconds()
        failures = _APP_SERVER_FAILURES
        _APP_SERVER_OPEN_UNTIL = time.monotonic() + cooldown
        _APP_SERVER_OPEN = True
        _APP_SERVER_FAILURES = 0
        re_enable_at = datetime.now(timezone.utc) + timedelta(seconds=cooldown)
        logger.warning(
            "codex app-server circuit open after %d consecutive transport "
            "failures: codex turns use codex exec for %.0fs (re-enable at %s)",
            failures,
            cooldown,
            re_enable_at.isoformat(timespec="seconds"),
        )


def _record_app_server_success() -> None:
    global _APP_SERVER_FAILURES, _APP_SERVER_OPEN_UNTIL, _APP_SERVER_OPEN
    was_degraded = _APP_SERVER_FAILURES > 0 or _APP_SERVER_OPEN
    _APP_SERVER_FAILURES = 0
    _APP_SERVER_OPEN_UNTIL = 0.0
    _APP_SERVER_OPEN = False
    if was_degraded:
        logger.info(
            "codex app-server circuit closed after a successful turn; "
            "codex turns use the app-server transport again"
        )


def _codex_app_server_fallback_enabled() -> bool:
    """Allow an early app-server failure to fall back to the exec transport."""
    value = os.environ.get("CONDUCTOR_CODEX_APP_SERVER_FALLBACK", "1").strip().lower()
    return value not in {"0", "false", "no", "off", "disabled"}


async def _run_codex_app_server_streaming(
    adapter: Any,
    prompt: str,
    cwd: str,
    streamer: "_DaemonOutputStream | None",
    gate: "_StreamPolicyGate | None" = None,
) -> Any:
    """Run one Codex turn on the app-server transport and forward deltas.

    Unlike ``codex exec`` (completed items only), app-server emits
    ``item/agentMessage/delta``. Text goes through the same redaction, policy
    and continuity checks as the exec path; the daemon still sends only visible
    text and tool names.
    """
    from src.llms.native_cli.base import NativeCliError, NativeCliUsage
    from src.llms.native_cli.isolation import credential_values, redact_diagnostic
    from src.server.services.native_runtime.codex_app_server import (
        CodexAppServerRuntime,
    )
    from src.server.utils import output_guard
    from src.server.utils.sanitize import sanitize_output_text

    outer_sandboxed = bool(getattr(adapter, "_outer_sandboxed", False))
    read_only = bool(getattr(adapter, "_read_only", True))
    runtime = CodexAppServerRuntime(
        binary=adapter.binary,
        cwd=cwd,
        model=getattr(adapter, "_model", None),
        mcp_servers=list(getattr(adapter, "_mcp_servers", []) or []),
        timeout_seconds=float(getattr(adapter, "_timeout_seconds", 300.0)),
        approval_policy="never",
        sandbox="danger-full-access" if outer_sandboxed else (
            "read-only" if read_only else "workspace-write"
        ),
    )
    continuity = _EnvContinuity(credential_values(None))
    text_parts: list[str] = []
    # Agent messages by item id, in order: the final one is the report.
    message_parts: list[tuple[str, list[str]]] = []
    session_id = ""
    usage = NativeCliUsage()
    event_count = 0
    published = False
    turn_completed = False

    async def accept_text(
        text: str, *, reasoning: bool, item_id: str = ""
    ) -> None:
        nonlocal continuity
        if not text:
            return
        capped = _cap_stream_message(text)
        raw = redact_diagnostic(capped, None)
        # The report is independent of live publication and its live size cap:
        # a stream that has stopped (or was never available) must not truncate
        # what the Worker reports, including a trailing state/question marker.
        # The outer result path redacts the assembled report again.
        if not reasoning:
            redacted = redact_diagnostic(text, None)
            text_parts.append(redacted)
            if not message_parts or message_parts[-1][0] != item_id:
                message_parts.append((item_id, []))
            message_parts[-1][1].append(redacted)
        if streamer is None or streamer.disabled:
            return
        if gate is not None and gate.blocked:
            streamer.stop_text("policy_refused")
            return
        if output_guard.has_control(raw):
            continuity.taint()
            streamer.discontinuity("control_character")
            return
        if raw != capped or capped != text:
            continuity.taint()
            streamer.discontinuity("redaction_boundary")
            return
        if not continuity.observe(raw):
            streamer.stop_text("credential_value")
            return
        if gate is not None and not gate.allow_text(raw):
            streamer.stop_text("policy_refused")
            return
        nonlocal published
        await streamer.add(
            {
                "kind": "reasoning" if reasoning else "text",
                "text": raw,
                "delta": True,
            }
        )
        published = True

    try:
        async for event in runtime.stream_turn(prompt, cwd=cwd):
            event_count += 1
            if event.session_id:
                session_id = event.session_id
            if event.kind in {"text_delta", "thinking_delta"}:
                await accept_text(
                    str(event.text or ""),
                    reasoning=event.kind == "thinking_delta",
                    item_id=str((event.data or {}).get("item_id") or ""),
                )
            elif event.kind == "tool_started":
                if streamer is None or streamer.disabled:
                    continue
                name = sanitize_output_text(str(event.tool_name or ""), limit=64).strip()
                if name:
                    await streamer.add({"kind": "tool", "tools": [name]})
                    published = True
            elif event.kind == "usage":
                usage = NativeCliUsage.from_mapping(event.data)
            elif event.kind == "approval_requested":
                raise NativeCliError(
                    "codex app-server requested approval in a non-interactive run"
                )
            elif event.kind == "error":
                raise _app_server_turn_error(
                    str(event.text or "codex app-server turn failed")
                )
            elif event.kind == "turn_completed":
                turn_completed = True
                break
        text = "".join(text_parts).strip()
        if not text and not turn_completed:
            # A stream that ended without a completed turn or any visible
            # character is a transport failure: judge it here, inside the
            # try, so it is counted and may fall back like any other.
            raise NativeCliError("codex app-server produced no final answer")
    except BaseException as exc:
        if _app_server_failure_counts(exc):
            _record_app_server_failure()
        # A transport that fails before publishing anything has not changed the
        # visible stream; retry once on the exec fallback rather than failing a
        # Worker. After a published event, falling back could duplicate text or
        # tools, so the failure propagates. Whether it counted was decided by
        # the classification above, not by whether a fallback happens.
        if (
            not published
            and not isinstance(exc, asyncio.CancelledError)
            and _codex_app_server_fallback_enabled()
        ):
            return await _run_codex_streaming(
                adapter, prompt, cwd, streamer, gate=gate
            )
        raise
    finally:
        await runtime.cancel()
    _record_app_server_success()
    if not text:
        # The turn completed without a final message: the transport worked
        # and the Worker's run happened, so it neither counts against the
        # circuit nor re-runs on exec. It has no completion report and
        # settles as waiting.
        from src.llms.native_cli.base import NativeCliNoFinalAnswer

        raise NativeCliNoFinalAnswer("codex app-server produced no final answer")
    return SimpleNamespace(
        text=text,
        final_text="".join(message_parts[-1][1]).strip() if message_parts else "",
        session_id=session_id or None,
        usage=usage,
        events=event_count,
    )


@dataclass
class ActiveLease:
    ticket_id: str
    lease_id: str
    expires_at: float
    duration: float
    lost: asyncio.Event

    @property
    def deadline(self) -> float:
        return min(self.expires_at, time.monotonic() + self.duration)


class LeaseManager:
    def __init__(self, api: Api, interval: float = 10.0) -> None:
        self.api = api
        self.interval = max(0.1, interval)
        self.active: dict[str, ActiveLease] = {}
        self.lock = asyncio.Lock()
        self.stopping = asyncio.Event()
        self.changed = asyncio.Event()
        self.fatal_error: ApiError | None = None

    async def add(self, lease: ActiveLease) -> None:
        async with self.lock:
            self.active[lease.ticket_id] = lease
        self.changed.set()

    async def remove(self, ticket_id: str) -> None:
        async with self.lock:
            self.active.pop(ticket_id, None)
        self.changed.set()

    async def run(self) -> None:
        while not self.stopping.is_set():
            self.changed.clear()
            async with self.lock:
                active = list(self.active.values())
            ttls = [
                lease.deadline - time.monotonic()
                for lease in active
                if not lease.lost.is_set()
            ]
            wait_for = min([self.interval, *(max(0.05, ttl / 3) for ttl in ttls)])
            stop_wait = asyncio.create_task(self.stopping.wait())
            change_wait = asyncio.create_task(self.changed.wait())
            try:
                done, _ = await asyncio.wait(
                    {stop_wait, change_wait},
                    timeout=wait_for,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for task in (stop_wait, change_wait):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(stop_wait, change_wait, return_exceptions=True)
            if stop_wait in done and self.stopping.is_set():
                break
            async with self.lock:
                leases = list(self.active.values())
            if not leases:
                continue
            live = [lease for lease in leases if time.monotonic() < lease.deadline]
            for lease in leases:
                if lease not in live:
                    lease.lost.set()
            if not live:
                continue
            try:
                request_started: list[float] = []
                heartbeat_leases = tuple(live)
                result = await _retry_call(
                    self.api.heartbeat,
                    [{"ticket_id": l.ticket_id, "lease_id": l.lease_id} for l in live],
                    deadline=lambda leases=heartbeat_leases: min(
                        lease.deadline for lease in leases
                    ),
                    capture_start=request_started,
                )
                rows = (result or {}).get("leases", [])
                renewed = {
                    (str(r.get("ticket_id")), str(r.get("lease_id"))): bool(
                        r.get("renewed")
                    )
                    for r in rows
                }
                for lease in live:
                    row = next(
                        (
                            r
                            for r in rows
                            if str(r.get("ticket_id")) == lease.ticket_id
                            and str(r.get("lease_id")) == lease.lease_id
                        ),
                        {},
                    )
                    if not renewed.get((lease.ticket_id, lease.lease_id), False):
                        lease.lost.set()
                    else:
                        if row.get("lease_duration_seconds") is not None:
                            lease.duration = max(
                                0.0, float(row["lease_duration_seconds"])
                            )
                        lease.expires_at = (
                            request_started[0] if request_started else time.monotonic()
                        ) + lease.duration
            except ApiError as exc:
                if not exc.retryable:
                    self.fatal_error = exc
                    for lease in live:
                        lease.lost.set()
                    await self.close()
                    return
                for lease in live:
                    if time.monotonic() >= lease.deadline:
                        lease.lost.set()
            except (
                Exception
            ):  # noqa: BLE001 - heartbeat teardown must not mask shutdown
                for lease in live:
                    if time.monotonic() >= lease.deadline:
                        lease.lost.set()

    async def close(self) -> None:
        self.stopping.set()
        self.changed.set()


async def _await_with_lease(task: asyncio.Task[Any], lease: ActiveLease) -> Any:
    lost = asyncio.create_task(lease.lost.wait())
    expired = asyncio.create_task(_watch_expiry(lease))
    try:
        done, _ = await asyncio.wait(
            {task, lost, expired}, return_when=asyncio.FIRST_COMPLETED
        )
        if lost in done or expired in done:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise LeaseLost(f"lease lost for ticket {lease.ticket_id}")
        return await task
    finally:
        for watcher in (lost, expired):
            watcher.cancel()
        await asyncio.gather(lost, expired, return_exceptions=True)
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def _watch_expiry(lease: ActiveLease) -> None:
    while not lease.lost.is_set():
        remaining = lease.deadline - time.monotonic()
        if remaining <= 0:
            lease.lost.set()
            return
        await asyncio.sleep(min(remaining, 0.2))


async def _watch_remote_cancel(
    api: Api, ticket_id: str, interval: float, cli: asyncio.Task[Any]
) -> dict[str, Any] | None:
    """Stop the local CLI when its owned Ticket leaves the running state."""
    while not cli.done():
        await asyncio.sleep(max(0.1, interval))
        try:
            status = await asyncio.to_thread(api.ticket_status, ticket_id)
        except (
            Exception
        ):  # noqa: BLE001, S112 - a polling blip must not stop valid work
            continue
        if str(status.get("status") or "") != "running":
            return status
    return None


# The reason a quarantine kept under a live straggler starts with. Removal
# of the checkout already waits for those processes (deferred journal), and
# the kept quarantine is marked so no sweep moves it while they live.
_STRAGGLER_KEPT = "a Worker process is still running (kept at "


async def _migrate_run_quarantine(
    object_quarantine: list[tuple[Any, tuple[Path, Path] | None]],
    tracker: Any,
    host_worktree: Mapping[str, Any],
) -> str:
    """Move this run's quarantine once the Worker's processes are gone (N67).

    Returns '' when moved, else why (the quarantine is then kept). A live
    straggler could still be writing into it, so it is never moved under one.
    """
    if not object_quarantine:
        return ""
    from src.server.services.autonomy import object_quarantine as _oq

    quarantine, dirs = object_quarantine[-1]
    if not await _process_gone_within(tracker, _PROCESS_GONE_GRACE):
        groups = [
            int(row["pgid"])
            for row in _hw.journal_processes(
                (tracker.evidence() or {}).get("processes")
            )
            if row.get("pgid")
        ]
        await asyncio.to_thread(_oq.mark_live, quarantine.path, groups)
        object_quarantine.pop()
        return f"{_STRAGGLER_KEPT}{quarantine.path})"
    object_quarantine.pop()
    return await asyncio.to_thread(
        migrate_object_quarantine,
        quarantine,
        branch=str(host_worktree.get("branch") or ""),
        admin=dirs[1] if dirs else None,
    )


async def _process_gone_within(tracker: Any, seconds: float) -> bool:
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        if tracker.gone():
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.05)


def _stop_ack_budget(stop: dict[str, Any]) -> float:
    budget = _STOP_ACK_MAX
    deadline = str(stop.get("deadline_at") or "")
    if not deadline:
        return budget
    try:
        target = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
    except ValueError:
        return budget
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    remaining = (target - datetime.now(timezone.utc)).total_seconds()
    return max(0.5, min(budget, remaining))


async def _acknowledge_stop(
    api: Api,
    ticket_id: str,
    lease_id: str,
    tracker: Any,
    status: dict[str, Any],
    outcome: dict[str, bool] | None = None,
) -> bool:
    """Prove the CLI tree is gone and send the server the stop evidence.

    The return value only says the acknowledgement is settled for this
    attempt. ``outcome['verified']`` is set only when the server accepted a
    process-gone acknowledgement: the one signal that may remove the checkout.
    """
    stop = status.get("stop")
    if not isinstance(stop, dict) or stop.get("verification") != "pending":
        return False
    gone = await _process_gone_within(tracker, _PROCESS_GONE_GRACE)
    evidence: dict[str, Any] = {
        "signal": "SIGKILL",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        **tracker.evidence(),
    }
    processes = evidence.get("processes") or []
    evidence["pid"] = processes[0].get("pid") if processes else None
    evidence["pgid"] = processes[0].get("pgid") if processes else None
    deadline = time.monotonic() + _stop_ack_budget(stop)
    while True:
        # The HTTP call must fit inside the acknowledgement budget; otherwise a
        # hung request can outlive the deadline the loop only checks afterward.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        try:
            await asyncio.to_thread(
                api.stop_ack,
                ticket_id,
                lease_id,
                process_gone=gone,
                evidence=evidence,
                timeout=remaining,
            )
            if outcome is not None and gone:
                outcome["verified"] = True
            return True
        except ApiError as exc:
            # A stale lease, an already-recorded stop, or a raced heartbeat is
            # final for this attempt; the server sweep escalates on timeout.
            if exc.status in {401, 403, 404, 409}:
                return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        await asyncio.sleep(min(0.2, remaining))


async def _acknowledge_pending_stop(
    api: Api,
    ticket_id: str,
    lease_id: str,
    tracker: Any,
    outcome: dict[str, bool] | None = None,
) -> bool:
    """A lease loss can win the race with the status poll; re-read the stop."""
    try:
        status = await asyncio.to_thread(api.ticket_status, ticket_id)
    except Exception:  # noqa: BLE001 - the server sweep escalates on timeout
        return False
    if not isinstance(status, dict):
        return False
    try:
        if outcome is None:
            return await _acknowledge_stop(api, ticket_id, lease_id, tracker, status)
        return await _acknowledge_stop(
            api, ticket_id, lease_id, tracker, status, outcome
        )
    except Exception as exc:  # noqa: BLE001 - the server sweep escalates
        print(
            f"Stop acknowledgement for ticket {ticket_id} failed: "
            f"{type(exc).__name__}",
            file=sys.stderr,
        )
        return False


# Ticket worktree Git operations on this host (src.* is available to the
# daemon script, not to the conductor_cli wheel).


async def _ticket_git(
    repo: Path, cwd: Path, worktrees_root: Path, argv: list[str]
) -> tuple[int, str, str]:
    from src.tools.auto_dispatch.execution import (
        _isolated_git_mutation_command,
        _isolated_git_readonly_command,
        _task6_local_mutation_git_argv,
        _task6_readonly_git_argv,
    )

    if _task6_readonly_git_argv(argv):
        return await _isolated_git_readonly_command(argv, cwd)
    if _task6_local_mutation_git_argv(argv):
        return await _isolated_git_mutation_command(repo, cwd, worktrees_root, argv)
    raise _hw.DaemonTicketWorktreeError(
        "refusing an unclassified Ticket worktree Git command"
    )


async def _registered_ticket_worktree(
    repo: Path, worktrees_root: Path, path: Path, branch: str | None
) -> bool:
    """Whether ``path`` is a registered worktree on ``branch``.

    ``branch=None`` asks for a registered worktree on a detached HEAD.
    """
    code, output, _ = await _ticket_git(
        repo, repo, worktrees_root, ["worktree", "list", "--porcelain"]
    )
    if code != 0:
        return False
    expected_path = f"worktree {path.resolve()}"
    expected_branch = "detached" if branch is None else f"branch refs/heads/{branch}"
    return any(
        expected_path in block.splitlines() and expected_branch in block.splitlines()
        for block in output.strip().split("\n\n")
    )


def linked_worktree_git_paths(
    worktree: Path, *, repo_root: str | Path | None = None
) -> tuple[Path, ...]:
    """Git metadata directories a linked worktree must read under isolation."""
    from src.tools.auto_dispatch.execution import ScriptLauncher
    from src.tools.auto_dispatch.protocols import LaunchError

    repo = _hw.resolve_repo_root(repo_root)
    if repo is None:
        return ()
    try:
        common, _admin = ScriptLauncher._repository_gitdirs(repo)
        gitdir = ScriptLauncher._worktree_gitdir(repo, Path(worktree))
    except LaunchError:
        return ()
    paths = [common, gitdir]
    if gitdir.parent != common:
        paths.append(gitdir.parent)
    return tuple(dict.fromkeys(paths))


def linked_worktree_git_dirs(
    worktree: Path, *, repo_root: str | Path | None = None
) -> tuple[Path, Path] | None:
    """``(common, admin)`` of a linked Ticket worktree, or None.

    Captured before the Worker runs: the Worker can rewrite
    ``<worktree>/.git``, so the post-run object migration and tip check use
    these, never a fresh read of the checkout's gitfile.
    """
    from src.tools.auto_dispatch.execution import ScriptLauncher
    from src.tools.auto_dispatch.protocols import LaunchError

    repo = _hw.resolve_repo_root(repo_root)
    if repo is None:
        return None
    try:
        common, _admin = ScriptLauncher._repository_gitdirs(repo)
        gitdir = ScriptLauncher._worktree_gitdir(repo, Path(worktree))
    except LaunchError:
        return None
    if gitdir.parent != common / "worktrees":
        return None
    return common, gitdir


def prepare_object_quarantine(
    worktree: Path, *, repo_root: str | Path | None = None
) -> Any:
    """This run's object quarantine for a linked Ticket worktree (N67).

    The Worker writes new objects only there (``GIT_OBJECT_DIRECTORY``) and
    reads the shared store as an alternate; ``<common>/objects`` itself stays
    read-only, so no pack, loose object or ``objects/info/alternates`` of
    ``main`` can be rewritten or deleted from inside the sandbox.
    """
    from src.server.services.autonomy import object_quarantine as _oq

    dirs = linked_worktree_git_dirs(worktree, repo_root=repo_root)
    if dirs is None:
        raise _hw.DaemonTicketWorktreeError(
            "the Ticket worktree Git metadata could not be located"
        )
    common, admin = dirs
    try:
        quarantine = _oq.prepare(common, admin)
    except (OSError, _oq.ObjectQuarantineError) as exc:
        raise _hw.DaemonTicketWorktreeError(
            f"the Ticket object quarantine could not be prepared: {exc}"
        ) from exc
    for note in quarantine.notes:
        logger.warning("ticket object quarantine: %s", note)
    return quarantine


def migrate_object_quarantine(
    quarantine: Any, *, branch: str, admin: Path | None = None
) -> str:
    """Move the run's quarantined objects into the store; '' or why not.

    The tip checked afterwards is read from the Ticket ref file itself (or a
    plain detached HEAD in ``admin``), never by a ref lookup that could
    follow a symref; callers run ``ticket_ref_tampering`` first.
    """
    from src.server.services.autonomy import object_quarantine as _oq

    tips = [_hw.ticket_ref_oid(quarantine.common, branch)]
    if admin is not None:
        tips.append(_hw.detached_head_commit(admin))
    result = _oq.migrate(
        quarantine.common, quarantine.path, tips=tuple(tip for tip in tips if tip)
    )
    if result.ok:
        if result.reason:
            logger.warning("ticket object quarantine: %s", result.reason)
        return ""
    _oq._record_failure(quarantine.path)
    return f"{result.reason} (kept at {quarantine.path})"


# Per-worktree files ``git add``/``commit``/``reset`` replace in the admin dir.
_WORKTREE_ADMIN_WRITE_FILES = (
    "HEAD",
    "index",
    "ORIG_HEAD",
    "AUTO_MERGE",
    "COMMIT_EDITMSG",
    "logs/HEAD",
)


def linked_worktree_git_write_files(
    worktree: Path, *, branch: str, repo_root: str | Path | None = None
) -> tuple[Path, ...]:
    """The individual Git files a commit on ``branch`` writes, plus locks.

    ``refs/heads/<branch>`` (+ ``.lock``), its reflog, and the worktree admin
    files above (+ ``.lock``). Missing parent directories are created here,
    outside the sandbox, so no directory grant is needed for them.
    """
    from src.tools.auto_dispatch.execution import ScriptLauncher
    from src.tools.auto_dispatch.protocols import LaunchError

    clean = _hw.validate_ticket_branch(branch)
    repo = _hw.resolve_repo_root(repo_root)
    if repo is None:
        return ()
    try:
        common, _admin = ScriptLauncher._repository_gitdirs(repo)
        gitdir = ScriptLauncher._worktree_gitdir(repo, Path(worktree))
    except LaunchError:
        return ()
    ref = common / "refs" / "heads" / clean
    reflog = common / "logs" / "refs" / "heads" / clean
    files: list[Path] = []
    for path in (ref, reflog):
        _ensure_git_metadata_parent(common, path)
        files.append(path)
    files.append(Path(str(ref) + ".lock"))
    for name in _WORKTREE_ADMIN_WRITE_FILES:
        path = gitdir / name
        _ensure_git_metadata_parent(gitdir, path)
        files.extend((path, Path(str(path) + ".lock")))
    return tuple(dict.fromkeys(files))


def _ensure_git_metadata_parent(base: Path, path: Path) -> None:
    parent = path.parent
    relative = parent.relative_to(base)
    current = base
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise _hw.DaemonTicketWorktreeError(
                "ticket worktree Git metadata path follows a symlink"
            )
    parent.mkdir(parents=True, exist_ok=True)
    if parent.resolve() != parent:
        raise _hw.DaemonTicketWorktreeError(
            "ticket worktree Git metadata path is not canonical"
        )


def ticket_ref_tampering(
    worktree: Path,
    *,
    branch: str,
    repo_root: str | Path | None = None,
    allow_detached: bool = False,
    require_ref: bool = False,
) -> str:
    """After a sandboxed run: why the Ticket ref must not be trusted, or ''.

    lstat of ``refs/heads/<branch>``, its reflog and the worktree HEAD. A
    symlink, a directory or a hard link there would make host-side Git write
    through to another ref (``refs/heads/main``).
    """
    from src.tools.auto_dispatch.execution import ScriptLauncher
    from src.tools.auto_dispatch.protocols import LaunchError

    repo = _hw.resolve_repo_root(repo_root)
    if repo is None:
        return "the dispatch repository is not configured"
    try:
        common, _admin = ScriptLauncher._repository_gitdirs(repo)
        gitdir = ScriptLauncher._worktree_gitdir(repo, Path(worktree))
    except LaunchError:
        return "the worktree Git metadata could not be located"
    return _hw.ticket_ref_tamper_reason(
        common, gitdir, branch, allow_detached=allow_detached, require_ref=require_ref
    )


def ticket_checkout_detached_head(
    worktree: Path, *, repo_root: str | Path | None = None
) -> str:
    """The commit a plain detached worktree HEAD names, or '' (see _hw)."""
    from src.tools.auto_dispatch.execution import ScriptLauncher
    from src.tools.auto_dispatch.protocols import LaunchError

    repo = _hw.resolve_repo_root(repo_root)
    if repo is None:
        return ""
    try:
        gitdir = ScriptLauncher._worktree_gitdir(repo, Path(worktree))
    except LaunchError:
        return ""
    return _hw.detached_head_commit(gitdir)


def _ticket_ref_store_tampering(repo: Path, branch: str) -> str:
    """lstat the Ticket ref and reflog in the dispatch repository ('' if sound).

    A branch an earlier run swapped for a symlink (to ``refs/heads/main``), a
    directory or a hard link must not be resumed: host-side ``worktree add``
    would follow it and check the other ref out as the Ticket's.
    """
    from src.tools.auto_dispatch.execution import ScriptLauncher
    from src.tools.auto_dispatch.protocols import LaunchError

    try:
        common, _admin = ScriptLauncher._repository_gitdirs(repo)
    except LaunchError:
        return "the dispatch repository Git metadata could not be located"
    return _hw.ticket_ref_tamper_reason(common, None, branch)


async def _validated_existing_checkout(
    *, ticket_id: str, existing: Path, repo: Path, root: Path, branch: str, base_sha: str
) -> None:
    from src.tools.auto_dispatch.execution import ScriptLauncher
    from src.tools.auto_dispatch.protocols import LaunchError

    try:
        ScriptLauncher._validate_worktree_gitfile(repo, existing)
    except LaunchError as exc:
        raise _hw.DaemonTicketWorktreeError(str(exc)) from exc
    detached = await asyncio.to_thread(
        ticket_checkout_detached_head, existing, repo_root=repo
    )
    if detached and await _registered_ticket_worktree(
        repo, root, existing, None
    ):
        # This host's registered checkout, left on a detached HEAD: block on
        # the operator's switch back rather than fail or reuse it.
        raise _hw.TicketCheckoutDetachedError(ticket_id, branch, detached)
    if not await _registered_ticket_worktree(repo, root, existing, branch):
        raise _hw.DaemonTicketWorktreeError(
            f"existing worktree for ticket {ticket_id} is not registered to its branch"
        )
    code, current, _ = await _ticket_git(
        repo, existing, root, ["branch", "--show-current"]
    )
    if code != 0 or current.strip() != branch:
        raise _hw.DaemonTicketWorktreeError(
            f"existing worktree for ticket {ticket_id} has a different branch"
        )
    pinned = _hw.read_base_sha(existing)
    if pinned and pinned != base_sha:
        raise _hw.DaemonTicketWorktreeError(
            f"existing worktree for ticket {ticket_id} was cut from another base"
        )


def _hw_is_object_id(value: str) -> bool:
    try:
        _hw.validate_base_sha(value)
    except _hw.DaemonTicketWorktreeError:
        return False
    return True


async def _require_branch_descends_from_base(
    repo: Path, root: Path, ticket_id: str, *, branch: str, tip: str, base_sha: str
) -> None:
    if tip == base_sha:
        return
    # Commits reachable from base_sha but not from the tip: zero exactly when
    # base_sha is an ancestor of the tip.
    code, missing, _ = await _ticket_git(
        repo, repo, root, ["rev-list", "--count", f"{tip}..{base_sha}"]
    )
    if code != 0 or missing.strip() != "0":
        raise _hw.TicketBranchDivergedError(ticket_id, branch, base_sha)


async def _ticket_branch_carries(
    repo: Path, root: Path, branch: str, commit: str
) -> bool:
    """Whether the (untampered) Ticket branch already contains ``commit``."""
    try:
        clean = _hw.validate_ticket_branch(branch)
    except _hw.DaemonTicketWorktreeError:
        return False
    if await asyncio.to_thread(_ticket_ref_store_tampering, repo, clean):
        return False
    code, tip, _ = await _ticket_git(
        repo, repo, root, ["rev-parse", "--verify", f"refs/heads/{clean}"]
    )
    tip = tip.strip().lower()
    if code != 0 or not _hw_is_object_id(tip):
        return False
    if tip == commit:
        return True
    code, missing, _ = await _ticket_git(
        repo, repo, root, ["rev-list", "--count", f"{tip}..{commit}"]
    )
    return code == 0 and missing.strip() == "0"


async def _worktree_is_dirty(repo: Path, path: Path, root: Path) -> bool:
    code, dirty, _ = await _ticket_git(
        repo, path, root, ["status", "--porcelain", "--untracked-files=all"]
    )
    if code != 0:
        raise _hw.DaemonTicketWorktreeError("could not inspect the ticket worktree")
    return bool(dirty.strip())


async def prepare_host_worktree(
    *,
    ticket_id: str,
    spec: dict[str, Any],
    persisted_path: str = "",
    worktrees_root: str | Path,
    repo_root: str | Path | None = None,
) -> _hw.HostTicketWorktree | None:
    """Create or resume the Ticket checkout under this daemon's root.

    Returns None when no dispatch repository is configured on this host (the
    caller fails the launch; it never falls back to a shared directory).
    """
    from src.tools.auto_dispatch.execution import _ensure_owned_directory
    from src.tools.auto_dispatch.protocols import LaunchError

    clean_id = str(ticket_id or "").strip()
    if not clean_id or str(spec.get("ticket_id") or "").strip() != clean_id:
        raise _hw.DaemonTicketWorktreeError("ticket worktree spec names another Ticket")
    base_branch = (
        str(spec.get("base_branch") or _hw.DEFAULT_BASE_BRANCH).strip()
        or _hw.DEFAULT_BASE_BRANCH
    )
    branch = _hw.validate_ticket_branch(str(spec.get("branch") or ""), base_branch=base_branch)
    base_sha = _hw.validate_base_sha(str(spec.get("base_sha") or ""))
    repo = _hw.resolve_repo_root(repo_root)
    if repo is None:
        return None
    root = _hw.resolve_worktrees_root(worktrees_root)
    if root == repo or repo in root.parents:
        raise _hw.DaemonTicketWorktreeError("ticket worktree root must be outside repo_root")

    worktree = _hw.ticket_worktree_path(root, repo, clean_id, branch)
    tampered = await asyncio.to_thread(_ticket_ref_store_tampering, repo, branch)
    if tampered:
        raise _hw.DaemonTicketWorktreeError(
            f"{tampered} in the dispatch repository; ticket {clean_id} is not resumed on it"
        )
    # A persisted path is this daemon's own earlier checkout only when it is
    # exactly the path this host derives for this Ticket and branch. Anything
    # else (a server path, another host's path, a user or dispatch checkout
    # under this root, another Ticket's checkout) is ignored, and the
    # checkout below is cut at the derived path instead.
    if persisted_path:
        existing = Path(persisted_path).expanduser()
        if existing.is_absolute() and existing.exists():
            existing = existing.resolve()
            if existing == worktree:
                await _validated_existing_checkout(
                    ticket_id=clean_id,
                    existing=existing,
                    repo=repo,
                    root=root,
                    branch=branch,
                    base_sha=base_sha,
                )
                # Dirty files are the blocked Ticket's work in progress.
                return _hw.HostTicketWorktree(
                    clean_id, existing, branch, base_branch, base_sha, created=False
                )

    try:
        _ensure_owned_directory(root, worktree.parent.relative_to(root))
    except LaunchError as exc:
        raise _hw.DaemonTicketWorktreeError(str(exc)) from exc
    if worktree.exists():
        await _validated_existing_checkout(
            ticket_id=clean_id,
            existing=worktree,
            repo=repo,
            root=root,
            branch=branch,
            base_sha=base_sha,
        )
        # The server has no binding for this path, so dirty state here is
        # unknown work; refuse to adopt it rather than run on top of it.
        if await _worktree_is_dirty(repo, worktree, root):
            raise _hw.DaemonTicketWorktreeError(
                f"unbound worktree for ticket {clean_id} has uncommitted changes"
            )
        return _hw.HostTicketWorktree(
            clean_id, worktree, branch, base_branch, base_sha, created=False
        )

    code, resolved, err = await _ticket_git(
        repo, repo, root, ["rev-parse", "--verify", f"{base_sha}^{{commit}}"]
    )
    if code != 0 or resolved.strip().lower() != base_sha:
        raise _hw.DaemonTicketWorktreeError(
            f"base_sha for ticket {clean_id} is not present in the dispatch repository"
        )
    await _ticket_git(repo, repo, root, ["worktree", "prune"])
    code, _, err = await _ticket_git(
        repo, repo, root, ["worktree", "add", "-b", branch, str(worktree), base_sha]
    )
    if code != 0:
        # The branch survives an earlier removed checkout; resume it, but only
        # when it still carries the pinned base (its tip is base_sha or a
        # descendant). A branch cut from elsewhere is not this Ticket's work.
        code, tip, _ = await _ticket_git(
            repo, repo, root, ["rev-parse", "--verify", f"refs/heads/{branch}"]
        )
        tip = tip.strip().lower()
        if code == 0 and _hw_is_object_id(tip):
            await _require_branch_descends_from_base(
                repo, root, clean_id, branch=branch, tip=tip, base_sha=base_sha
            )
            code, _, err = await _ticket_git(
                repo, repo, root, ["worktree", "add", str(worktree), branch]
            )
        else:
            code = code or 1
    if code != 0:
        raise _hw.DaemonTicketWorktreeError(
            f"could not create worktree for ticket {clean_id}: {err.strip()}"
        )
    try:
        _hw.write_base_sha(worktree, base_sha)
    except OSError as exc:
        raise _hw.DaemonTicketWorktreeError(
            f"could not pin base_sha for ticket {clean_id}"
        ) from exc
    return _hw.HostTicketWorktree(
        clean_id, worktree, branch, base_branch, base_sha, created=True
    )


async def remove_ticket_worktree(
    *,
    ticket_id: str,
    worktree_path: str | Path,
    worktrees_root: str | Path,
    repo_root: str | Path | None = None,
    branch: str = "",
) -> None:
    """Remove one Ticket checkout under this root; raise when it is not gone.

    Dirty files are archived under ``recovery-artifacts`` first.
    """
    from src.tools.auto_dispatch.execution import (
        ScriptLauncher,
        _archive_dirty_worktree,
    )
    from src.tools.auto_dispatch.protocols import LaunchError

    repo = _hw.resolve_repo_root(repo_root)
    if repo is None:
        raise _hw.DaemonTicketWorktreeError("dispatch repository is not configured")
    root = _hw.resolve_worktrees_root(worktrees_root)
    path = Path(worktree_path).expanduser().resolve()
    if not path.is_relative_to(root) or path == root:
        raise _hw.DaemonTicketWorktreeError(
            "ticket worktree path is outside the worktree root"
        )
    if not path.exists():
        await _ticket_git(repo, repo, root, ["worktree", "prune"])
        _hw.clear_base_sha(path)
        return
    try:
        ScriptLauncher._validate_worktree_gitfile(repo, path)
    except LaunchError as exc:
        raise _hw.DaemonTicketWorktreeError(str(exc)) from exc
    detached = await asyncio.to_thread(
        ticket_checkout_detached_head, path, repo_root=repo
    )
    if detached and not await _ticket_branch_carries(repo, root, branch, detached):
        # Commits made on a detached HEAD live only in this checkout's HEAD:
        # removing it would leave them unreachable. Keep it unless the
        # Ticket branch already carries that commit.
        raise _hw.DaemonTicketWorktreeError(
            f"ticket worktree {ticket_id} is on a detached HEAD at "
            f"{detached[:12]} that the Ticket branch does not carry; it is "
            "kept so those commits are not lost"
        )
    force = await _worktree_is_dirty(repo, path, root)
    if force:
        try:
            await _archive_dirty_worktree(
                repo,
                path,
                root / "recovery-artifacts",
                lambda argv, cwd: _ticket_git(repo, cwd, root, argv),
                {"ticket_id": ticket_id},
            )
        except LaunchError as exc:
            raise _hw.DaemonTicketWorktreeError(str(exc)) from exc
    remove_args = ["worktree", "remove", *(["--force"] if force else []), str(path)]
    code, _out, err = await _ticket_git(repo, repo, root, remove_args)
    if code != 0:
        raise _hw.DaemonTicketWorktreeError(
            f"could not remove ticket worktree {ticket_id}: {err.strip()}"
        )
    await _ticket_git(repo, repo, root, ["worktree", "prune"])
    _hw.clear_base_sha(path)


_WORKTREE_CLEANUP_TIMEOUT = 30.0
_NON_ENDING_STATES = frozenset({"blocked", "waiting"})


async def _post_worktree_cleanup(
    api: Api, entry: dict[str, Any], *, outcome: str, detail: str
) -> str:
    """Report one removal outcome; return 'accepted', 'superseded' or 'retry'.

    One attempt per call: the journal is the retry, so a slow server never
    holds a Ticket task or the poll loop.
    """
    try:
        await asyncio.to_thread(
            api.worktree_cleanup,
            str(entry["ticket_id"]),
            lease_id=str(entry.get("lease_id") or ""),
            worktree_path=str(entry["worktree_path"]),
            worktree_generation=int(entry["worktree_generation"]),
            outcome=outcome,
            detail=detail[:4000],
            timeout=_WORKTREE_CLEANUP_TIMEOUT,
        )
    except ApiError as exc:
        if exc.status == 409:
            # A newer generation owns the binding (or it is already cleared).
            return "superseded"
        logger.warning(
            "ticket worktree cleanup report failed ticket=%s status=%s",
            entry["ticket_id"],
            exc.status,
        )
        return "retry"
    except Exception:  # noqa: BLE001 - transport failure keeps the journal
        logger.warning(
            "ticket worktree cleanup report failed ticket=%s",
            entry["ticket_id"],
            exc_info=True,
        )
        return "retry"
    return "accepted"


async def _settle_journaled_cleanup(
    api: Api, worktree_root: str, entry: dict[str, Any]
) -> str:
    """Remove one journaled checkout and report the true outcome.

    The journal entry exists only because the server accepted the ending.
    It is dropped once the server took 'removed', or answered 409 (a newer
    generation owns the binding); a failed removal or an unsent report keeps
    it, with backoff, for the next drain.
    """
    from src.conductor_cli.daemon_ticket_worktree import (
        cleanup_superseded,
        clear_host_ownership,
        defer_cleanup_journal,
        drop_cleanup_journal,
        host_ticket_lock,
        journal_processes_alive,
        read_cleanup_journal,
        read_host_ownership,
    )

    ticket_id = str(entry["ticket_id"])
    path = str(entry["worktree_path"])
    generation = int(entry["worktree_generation"])
    async with host_ticket_lock(ticket_id):
        current = read_cleanup_journal(worktree_root, ticket_id, generation)
        if current is None or str(current.get("worktree_path") or "") != path:
            # Settled (or replaced) by another task while this one waited.
            return "superseded"
        entry = current
        ownership = read_host_ownership(worktree_root, ticket_id)
        if cleanup_superseded(ownership, worktree_path=path, generation=generation):
            # A newer bind on this host owns the checkout; never touch it.
            drop_cleanup_journal(worktree_root, ticket_id, generation=generation)
            return "superseded"
        if journal_processes_alive(entry):
            # A Worker process deferred this removal and is still running:
            # ``worktree remove --force`` must not race it. Retry later.
            logger.warning(
                "ticket worktree removal deferred ticket=%s: a Worker process "
                "is still running",
                ticket_id,
            )
            defer_cleanup_journal(worktree_root, entry)
            return "deferred"
        outcome, detail = "removed", ""
        try:
            await remove_ticket_worktree(
                ticket_id=ticket_id,
                worktree_path=path,
                worktrees_root=worktree_root,
                branch=str(entry.get("branch") or ""),
            )
        except Exception as exc:  # noqa: BLE001 - reported as 'failed'
            outcome = "failed"
            detail = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "ticket worktree removal failed ticket=%s: %s", ticket_id, detail
            )
        if outcome == "removed":
            clear_host_ownership(worktree_root, ticket_id, generation=generation)
        verdict = await _post_worktree_cleanup(
            api, entry, outcome=outcome, detail=detail
        )
        if verdict == "superseded" or (verdict == "accepted" and outcome == "removed"):
            drop_cleanup_journal(worktree_root, ticket_id, generation=generation)
            return outcome if verdict == "accepted" else "superseded"
        defer_cleanup_journal(worktree_root, entry)
        return outcome if verdict == "accepted" else "unreported"


async def _drain_pending_worktree_cleanups(
    api: Api, worktree_root: str, *, now: float | None = None
) -> int:
    """Retry journaled removals that are due; return how many were settled."""
    from src.conductor_cli.daemon_ticket_worktree import list_cleanup_journal

    settled = 0
    current = time.time() if now is None else now
    try:
        entries = list_cleanup_journal(worktree_root)
    except OSError:
        logger.warning("could not read the ticket worktree cleanup journal", exc_info=True)
        return 0
    for entry in entries:
        try:
            if float(entry.get("next_attempt_at") or 0.0) > current:
                continue
        except (TypeError, ValueError):
            pass
        try:
            if await _settle_journaled_cleanup(api, worktree_root, entry) in {
                "removed",
                "superseded",
            }:
                settled += 1
        except Exception:  # noqa: BLE001 - one bad entry must not stop the drain
            logger.warning(
                "ticket worktree cleanup drain failed ticket=%s",
                entry.get("ticket_id"),
                exc_info=True,
            )
    return settled


async def _cleanup_after_accepted_ending(
    api: Api,
    worktree_root: str,
    ticket_id: str,
    binding: dict[str, Any],
    *,
    tracker: Any = None,
) -> str:
    """Journal, remove and report a bound checkout the server saw end.

    Call only after the server accepted a terminal report or a verified
    (process-gone) stop for the lease that bound ``binding``. The journal is
    written before removal so a crash in between is retried by the drain.

    ``worktree remove --force`` must not run under a Worker that is still
    alive (a cancelled or failed CLI can leave its tree exiting, or a
    straggler writing into the checkout). With the run's ``tracker`` the
    removal waits for the tree to be gone; if it outlives the grace, the
    removal is deferred to the journal drain instead of racing it.
    """
    from src.conductor_cli.daemon_ticket_worktree import (
        defer_cleanup_journal,
        write_cleanup_journal,
    )

    entry = write_cleanup_journal(
        worktree_root,
        ticket_id=ticket_id,
        worktree_path=str(binding["path"]),
        worktree_generation=int(binding["generation"]),
        lease_id=str(binding["lease_id"]),
        branch=str(binding.get("branch") or ""),
    )
    if tracker is not None and not await _process_gone_within(
        tracker, _PROCESS_GONE_GRACE
    ):
        logger.warning(
            "ticket worktree removal deferred ticket=%s: the Worker process "
            "tree is still running",
            ticket_id,
        )
        # The drain has no tracker: record the processes it must wait for.
        try:
            evidence = tracker.evidence() or {}
        except Exception:  # noqa: BLE001 - deferral itself must not fail
            evidence = {}
        defer_cleanup_journal(
            worktree_root,
            {**entry, "processes": evidence.get("processes") or ()},
        )
        return "deferred"
    return await _settle_journaled_cleanup(api, worktree_root, entry)


def _objects_not_moved_report(reason: str) -> dict[str, str]:
    return {
        "state": "blocked",
        "summary": (
            "Local daemon kept the Ticket checkout but did not deliver the "
            "Worker result: the Git objects it wrote could not be moved into "
            f"the repository: {reason}. Nothing was removed."
        ),
        "question": (
            "Inspect the kept object quarantine under conductor-objects/ in the "
            "dispatch repository's Git directory on the daemon host (or wait for "
            "the Worker process to exit), then resume this Ticket; the resume "
            "moves it first."
        ),
    }


def _detached_head_report(branch: str, head_sha: str) -> dict[str, str]:
    return {
        "state": "blocked",
        "summary": (
            "Local daemon kept the Ticket checkout but did not deliver the "
            f"Worker result: the checkout is on a detached HEAD at {head_sha[:12]} "
            f"instead of the Ticket branch {branch}. Nothing was removed; the "
            "operator returns the checkout to the branch, then the Ticket resumes."
        ),
        "question": _hw.detached_head_question(branch, head_sha),
    }


def _failed_launch(summary: str) -> dict[str, str]:
    return {"state": "failed", "summary": summary, "question": ""}


async def _prepare_and_bind_host_worktree(
    api: Api,
    ticket: dict[str, Any],
    spec: dict[str, Any],
    lease: ActiveLease,
    *,
    worktree_root: str,
    binding: dict[str, Any],
) -> dict[str, str] | None:
    """Cut or resume the Ticket checkout here and bind it on the server.

    Returns None with ``binding`` filled (path, generation, lease_id, branch),
    or the report for the refused launch: failed, or blocked when an operator
    must repair the Ticket branch first. Never reports and never raises for a
    prepare or bind failure, so the caller sends exactly one report.
    """
    from src.conductor_cli.daemon_ticket_worktree import (
        host_ticket_lock,
        write_host_ownership,
    )

    ticket_id = str(ticket["ticket_id"])
    lease_id = str(ticket["lease_id"])
    # The persisted path is only a hint: prepare reuses it when it is this
    # host's own checkout under this root, and ignores it otherwise.
    persisted = str(
        ticket.get("worktree_path") or spec.get("worktree_path") or ""
    ).strip()
    async with host_ticket_lock(ticket_id):
        try:
            prepared = await prepare_host_worktree(
                ticket_id=ticket_id,
                spec=spec,
                persisted_path=persisted,
                worktrees_root=worktree_root,
            )
        except _hw.TicketBranchDivergedError as exc:
            # Not a transient failure: every retry would refuse the same way.
            # Block the Ticket with the operator's fix instead.
            logger.warning(
                "ticket worktree branch diverged ticket=%s: %s", ticket_id, exc
            )
            return {
                "state": "blocked",
                "summary": f"Local daemon could not resume the Ticket worktree: {exc}",
                "question": _hw.diverged_branch_question(exc.branch),
            }
        except _hw.TicketCheckoutDetachedError as exc:
            # The checkout keeps the Worker's work; the operator returns it
            # to the Ticket branch (saving detached commits) and resumes.
            logger.warning(
                "ticket worktree checkout detached ticket=%s: %s", ticket_id, exc
            )
            return _detached_head_report(exc.branch, exc.head_sha)
        except Exception as exc:  # noqa: BLE001 - a failed install fails the launch
            return _failed_launch(
                "Local daemon could not prepare the Ticket worktree on this host: "
                f"{_failure_detail(exc, where='ticket worktree prepare failed')}"
            )
        if prepared is None:
            return _failed_launch(
                "Local daemon could not prepare the Ticket worktree on this host: "
                "no dispatch repository is configured."
            )
        path = str(prepared.path)
        generation = 0
        failure = ""
        try:
            response = await _retry_call(
                api.bind_worktree,
                ticket_id,
                lease_id,
                worktree_path=path,
                branch=prepared.branch,
                base_sha=prepared.base_sha,
                deadline=lambda: lease.deadline,
            )
            generation = int((response or {}).get("worktree_generation") or 0)
            bound = str((response or {}).get("worktree_path") or "")
            if generation < 1 or not bound or Path(bound).resolve() != prepared.path:
                failure = "the server returned a different binding"
        except Exception as exc:  # noqa: BLE001 - a failed bind fails the launch
            failure = _failure_detail(exc, where="ticket worktree bind failed")
        if failure:
            logger.warning(
                "ticket worktree bind failed ticket=%s: %s", ticket_id, failure
            )
            if prepared.created:
                # Nothing ran in a checkout cut a moment ago; a resumed one
                # keeps its files for the next claim.
                try:
                    await remove_ticket_worktree(
                        ticket_id=ticket_id,
                        worktree_path=path,
                        worktrees_root=worktree_root,
                    )
                except Exception:  # noqa: BLE001 - the next claim reuses it
                    logger.warning(
                        "could not remove unbound ticket worktree ticket=%s",
                        ticket_id,
                        exc_info=True,
                    )
            return _failed_launch(
                "Local daemon could not bind the Ticket worktree for this lease "
                f"({failure})."
            )
        binding.update(
            path=path,
            generation=generation,
            lease_id=lease_id,
            branch=prepared.branch,
            # The server-pinned base the Ticket branch was cut from: the
            # completion contract's commit check counts from it.
            base_sha=prepared.base_sha,
        )
        try:
            write_host_ownership(
                worktree_root,
                ticket_id=ticket_id,
                worktree_path=path,
                lease_id=lease_id,
                generation=generation,
            )
        except OSError as exc:
            # Bound on the server: keep ``binding`` so the accepted failed
            # report still removes it and reports the outcome.
            return _failed_launch(
                "Local daemon could not record Ticket worktree ownership: "
                f"{_failure_detail(exc, where='ticket worktree ownership failed')}"
            )
    return None


def _task_finished(tasks: set[asyncio.Task[bool]], task: asyncio.Task[bool]) -> None:
    tasks.discard(task)
    if not task.cancelled():
        try:
            task.result()
        except (
            Exception
        ) as exc:  # noqa: BLE001 - report unexpected worker-task failures
            print(f"Ticket task ended: {type(exc).__name__}", file=sys.stderr)


def _task_finished_with_breaker(
    tasks: set[asyncio.Task[bool]],
    task: asyncio.Task[bool],
    breaker: ProviderCircuitBreaker,
    permit: Permit,
) -> None:
    breaker.release(permit)
    _task_finished(tasks, task)


def _ticket_request_text(ticket: dict[str, Any]) -> str:
    """The Ticket's own request: its title and its scope.

    Both are the Ticket's own words. The launch prompt is the cloud's, and
    it carries its own verdict below.
    """
    parts = [
        str(ticket.get("title") or ""),
        str(ticket.get("scope") or ""),
    ]
    return "\n".join(part for part in parts if part.strip())


async def _revocation_rejection(
    api: Api, lease_id: str, *, expected_daemon_id: str = ""
) -> str:
    """Empty when the server says the lease is live.

    Anything else — revoked, malformed, a status that names another daemon,
    or unreachable — refuses the lease. The poll is not retried: a later
    success would run a grant the device could not confirm.
    """
    try:
        status = await asyncio.to_thread(api.lease_status, lease_id)
    except Exception:  # noqa: BLE001 - unreachable is a refusal, not a retry
        return (
            "Local daemon refused the lease: revocation status could not "
            "be confirmed."
        )
    if not isinstance(status, dict):
        return (
            "Local daemon refused the lease: revocation status could not "
            "be confirmed."
        )
    if status.get("revoked") is True or status.get("revoked_at"):
        return "Local daemon refused the lease: the server revoked it."
    if status.get("revoked") is not False:
        return (
            "Local daemon refused the lease: revocation status could not "
            "be confirmed."
        )
    status_daemon = str(status.get("daemon_id") or "")
    if expected_daemon_id and status_daemon and status_daemon != expected_daemon_id:
        return (
            "Local daemon refused the lease: revocation status names a "
            "different daemon."
        )
    return ""


async def _origin_rejection(
    origin: OriginCheck | None,
    api: Api,
    ticket: dict[str, Any],
    grant: Any,
    *,
    expected_daemon_id: str = "",
) -> tuple[str, Any]:
    """Why this grant must not run, and the payload when the signature held.

    The signature, expiry, nonce and local daemon identity are local. Revocation
    is the server's word, and a missing word is a refusal. The nonce is
    recorded only after the server says the lease is live, so a poll the device
    could not finish can be asked again. Expiry is checked again after the
    poll: a grant that expired while the poll was in flight must not run.
    """
    if origin is None:
        return "", None
    from src.conductor_cli.daemon_grant import (
        GrantError,
        GrantExpired,
        GrantMismatch,
        GrantReplayed,
        bind_grant,
        refusal_reason,
        verify_grant,
    )

    verified = None
    try:
        verified = verify_grant(grant, origin.keys, now=int(time.time()))
        if expected_daemon_id and verified.daemon_id != expected_daemon_id:
            raise GrantMismatch("signed grant names another daemon")
        bind_grant(
            verified,
            ticket_id=str(ticket.get("ticket_id") or ""),
            lease_id=str(ticket.get("lease_id") or ""),
            policy_version=str(ticket.get("policy_version") or ""),
        )
        if origin.nonces.seen(verified.nonce, now=int(time.time())):
            raise GrantReplayed("signed grant was already used")
    except GrantError as exc:
        return refusal_reason(exc), None
    reason = await _revocation_rejection(
        api,
        str(ticket.get("lease_id") or ""),
        expected_daemon_id=expected_daemon_id,
    )
    if reason:
        return reason, verified
    # The poll is an await: the grant can expire while it is in flight, and an
    # expired nonce must not be dropped silently as if the run were allowed.
    now = int(time.time())
    if verified.expires_at <= now:
        return (
            refusal_reason(GrantExpired("signed grant has expired")),
            verified,
        )
    try:
        origin.nonces.consume(verified.nonce, verified.expires_at, now=now)
    except GrantError as exc:
        return refusal_reason(exc), verified
    return "", verified


def _executed_request(payload: dict[str, Any], ticket: dict[str, Any]) -> str:
    """The text the native CLI actually runs.

    The cloud sends the launch prompt next to the Ticket, so the prompt is a
    request of its own: it is handed to the CLI unchanged and checked as one.
    """
    return str(payload.get("prompt") or ticket.get("scope") or "")


def _policy_verdict(
    policy: Any,
    ticket: dict[str, Any],
    provider: str,
    speculative: bool,
    executed_request: str,
) -> tuple[str, bool]:
    """Why this Ticket must not run (or an empty string), and whether it is read-only.

    A lease is bound to the policy version it was granted under, so a policy
    the device replaced after the grant refuses the lease instead of running
    it under rules the cloud never saw.

    The read-only flag is the effective local restriction: the policy file,
    the provider's own entry, or a speculative Ticket, whichever is strictest.
    The adapter has to run under that restriction, not only under the
    Ticket's own flag.
    """
    if policy is None:
        return "", speculative
    from src.conductor_cli.daemon_policy import executable_request_rejection

    lease_version = str(ticket.get("policy_version") or "")
    if not lease_version:
        return (
            "Local daemon refused the lease: it carries no local policy version.",
            speculative,
        )
    if lease_version != policy.version:
        return (
            "Local daemon refused the lease: the local policy changed "
            f"({lease_version} -> {policy.version}) after the lease was granted.",
            speculative,
        )
    read_only = policy.is_read_only(provider, speculative=speculative)
    request_text = _ticket_request_text(ticket)
    decision = policy.evaluate(provider, request_text, speculative=speculative)
    if not decision.allowed:
        return f"Local daemon refused the Ticket: {decision.reason}.", read_only
    request_executable = executable_request_rejection(
        policy, provider, request_text, speculative=speculative
    )
    if request_executable:
        return (
            f"Local daemon refused the Ticket: the request carries {request_executable}.",
            read_only,
        )
    # The CLI runs the launch prompt, which the cloud supplies next to the
    # Ticket, so the prompt carries its own verdict under the same full rules:
    # an operation the policy does not permit, or executable text it cannot
    # classify, never reaches the adapter.
    prompt_decision = policy.evaluate(
        provider, executed_request, speculative=speculative
    )
    if not prompt_decision.allowed:
        return (
            "Local daemon refused the Ticket: the launch prompt is out of scope: "
            f"{prompt_decision.reason}.",
            read_only,
        )
    prompt_executable = executable_request_rejection(
        policy, provider, executed_request, speculative=speculative
    )
    if prompt_executable:
        return (
            f"Local daemon refused the Ticket: the launch prompt carries {prompt_executable}.",
            read_only,
        )
    return "", read_only


def _writable_mount_rejection(
    path: Path,
    label: str,
    workdir: str,
    isolation_root: str,
    runtime_write_paths: Iterable[str] = (),
) -> str:
    """Why this authority path may not live under a Worker-writable mount.

    ``path`` is an already-resolved, pinned authority location (policy, trusted
    keys or replay record): it is compared as-is and never dereferenced again,
    so replacing the file or an ancestor with a symlink after loading cannot
    move the authorized location. The writable mounts are resolved now.
    """
    from src.llms.native_cli.isolation import paths_overlap_canonical

    canonical = Path(path).expanduser()
    mounts = [Path(workdir).expanduser()]
    if isolation_root:
        mounts.append(Path(isolation_root).expanduser())
    mounts.extend(Path(item).expanduser() for item in runtime_write_paths)
    for mount in mounts:
        try:
            if paths_overlap_canonical(canonical, mount):
                return (
                    f"Local daemon refused the Ticket: the {label} is inside a "
                    "worker-writable path. Keep it outside every workspace, the "
                    "isolation root and every runtime write path."
                )
        except (OSError, ValueError):
            # A mount the daemon cannot classify is a refusal, not a pass.
            return (
                f"Local daemon refused the Ticket: a worker-writable path could "
                f"not be checked against the {label} location."
            )
    return ""


def _policy_location_rejection(
    policy_path: Path,
    workdir: str,
    isolation_root: str,
    runtime_write_paths: Iterable[str] = (),
) -> str:
    """The local policy may not live where a Worker can rewrite it."""
    return _writable_mount_rejection(
        policy_path, "local policy", workdir, isolation_root, runtime_write_paths
    )


def _origin_location_rejection(
    origin: OriginCheck | None,
    workdir: str,
    isolation_root: str,
    runtime_write_paths: Iterable[str] = (),
) -> str:
    """The trusted keys and replay record are authority state too.

    A Worker that can rewrite the trusted-key file can trust its own key after
    a restart; one that can reset the replay record can present a captured
    grant twice. Both are checked against every writable mount like the policy.
    """
    if origin is None:
        return ""
    # The replay store pins its location once; authorization must check that
    # same location, not a fresh resolution of the original pathname.
    replay_path = getattr(origin.nonces, "resolved_path", None)
    if replay_path is None:
        replay_path = origin.nonces.path
    keys_path = getattr(origin.keys, "resolved_path", None)
    if keys_path is None:
        keys_path = origin.keys.path
    files: list[tuple[str, Path | None]] = [
        ("trusted server keys", keys_path),
        ("replay record", replay_path),
    ]
    for label, path in files:
        if path is None:
            continue
        reason = _writable_mount_rejection(
            path, label, workdir, isolation_root, runtime_write_paths
        )
        if reason:
            return reason
    return ""


def _capability_gate(
    policy: Any,
    ticket: dict[str, Any],
    provider: str,
    speculative: bool,
    daemon_id: str,
    grant_verifier: GrantVerifier | None,
    executed_request: str = "",
    claim_grant: Any = None,
    verifier_mode: str | None = None,
    audit: dict[str, Any] | None = None,
) -> tuple[OperationCapability | None, str]:
    """The capability this Ticket may run under, or why it must not run.

    ``(None, "")`` means this daemon has no policy, so a capability is not
    required. Any other refusal is fail closed: a missing grant, a verifier
    that is not installed, a grant the verifier rejects, or a capability that
    is expired, bound to something else, or wider than this run.

    The live claim format carries a top-level signed lease ``grant`` and no
    ``ticket.capability``. The verifier authenticates that lease grant; when
    the grant does not name a narrower operation set, this gate uses the
    device's allowed operations for the run.

    N58: the signature check has a mode (``CONDUCTOR_DAEMON_GRANT_VERIFIER_MODE``,
    default ``shadow``). Only ``enforce`` refuses on a missing, rejected or
    unverifiable grant; ``shadow`` logs what it would have refused and runs
    under a capability made from the local policy allowlist; ``off`` does the
    same without the log. The policy version bind and the operation check
    below run in every mode. A shadow refusal is also written into ``audit``
    (``{mode, would_refuse, reason}``) when one is passed: the daemon sends it
    with the Ticket's report so the server can show what enforce would have
    refused.
    """
    if policy is None:
        return None, ""
    from src.conductor_cli.daemon_capability import (
        OperationCapability,
        grant_verifier_mode,
    )

    # The same immutable snapshot that admitted the Ticket authorizes the
    # capability: a policy replaced between those checks cannot pair a weak
    # read-only verdict with a fresh capability.
    if str(ticket.get("policy_version") or "") != policy.version:
        return None, (
            "Local daemon refused the Ticket: the operation capability was "
            "bound to a policy version that no longer matches this daemon's "
            "snapshot."
        )
    mode = grant_verifier_mode() if verifier_mode is None else verifier_mode
    capability, refusal = (
        (None, "")
        if mode == "off"
        else _verified_capability(ticket, grant_verifier, claim_grant)
    )
    if refusal and mode == "enforce":
        return None, refusal
    allowed = policy.allowed_operations(provider, speculative=speculative)
    if allowed is None:
        return None, (
            "Local daemon refused the Ticket: the operation capability is not "
            "covered by the local policy."
        )
    if capability is None:
        if refusal:
            logger.warning(
                "grant verifier (shadow, not enforced) would refuse ticket %s: %s",
                str(ticket.get("ticket_id") or "?"),
                refusal,
            )
            if audit is not None:
                audit.update({"mode": mode, "would_refuse": True, "reason": refusal})
        # Shadow / off: the local allowlist is the capability, bound to this
        # Ticket, daemon and policy snapshot, as a verified lease grant is.
        capability = OperationCapability(
            ticket_id=str(ticket.get("ticket_id") or ""),
            daemon_id=daemon_id,
            policy_version=policy.version,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            operations=frozenset(allowed),
        )
    return _authorize_capability(
        policy, ticket, daemon_id, executed_request, capability, allowed
    )


def _verified_capability(
    ticket: dict[str, Any],
    grant_verifier: GrantVerifier | None,
    claim_grant: Any,
) -> tuple[OperationCapability | None, str]:
    """The capability the signed grant authenticates, or the refusal (N44)."""
    from src.conductor_cli.daemon_capability import GrantError, OperationCapability

    if grant_verifier is None:
        return None, (
            "Local daemon refused the Ticket: no grant verifier is configured, "
            "so an operation capability cannot be accepted."
        )
    # Prefer an explicit per-Ticket capability when present; otherwise use the
    # claim's signed lease grant — the format the server actually emits.
    grant = ticket.get("capability")
    if not isinstance(grant, dict) or not grant:
        grant = claim_grant if isinstance(claim_grant, dict) and claim_grant else None
    if not isinstance(grant, dict) or not grant:
        return None, (
            "Local daemon refused the Ticket: it carries no operation capability."
        )
    try:
        capability = grant_verifier.verify(grant)
    except GrantError as exc:
        return None, (
            "Local daemon refused the Ticket: the operation capability was "
            f"rejected ({exc})."
        )
    except Exception:  # noqa: BLE001 - a verifier fault refuses the Ticket
        return None, (
            "Local daemon refused the Ticket: the operation capability was rejected."
        )
    if not isinstance(capability, OperationCapability):
        return None, (
            "Local daemon refused the Ticket: the operation capability was rejected."
        )
    # A signed lease grant names the lease it was minted for. Prefer that bind
    # over trusting an unsigned capability body that omitted lease_id.
    grant_lease = str(grant.get("lease_id") or "").strip()
    ticket_lease = str(ticket.get("lease_id") or "").strip()
    if grant_lease and grant_lease != ticket_lease:
        return None, (
            "Local daemon refused the Ticket: the operation capability is "
            "for a different lease."
        )
    return capability, ""


def _authorize_capability(
    policy: Any,
    ticket: dict[str, Any],
    daemon_id: str,
    executed_request: str,
    capability: OperationCapability,
    allowed: Any,
) -> tuple[OperationCapability | None, str]:
    """Hold a capability to the local policy and to what this run executes."""
    from src.conductor_cli.daemon_capability import OperationCapability, authorize
    from src.conductor_cli.daemon_policy import executable_free_operations

    # A verified lease grant never carries authenticated operations (unsigned
    # ones are refused in the verifier). The device's allowlist is then the
    # capability for this run. A capability that already names operations
    # (test doubles / a future signed form) keeps that set and is checked
    # against the allowlist below.
    if not capability.operations:
        capability = OperationCapability(
            ticket_id=capability.ticket_id,
            daemon_id=capability.daemon_id,
            policy_version=capability.policy_version,
            expires_at=capability.expires_at,
            operations=frozenset(allowed),
        )
    # The capability must cover the request AND the text the CLI actually
    # executes, with normalized command operations: a raw regex miss on
    # `git "status"` must not let an operation outside the capability run.
    ticket_operations = executable_free_operations(
        policy.operations, _ticket_request_text(ticket)
    )
    prompt_operations = executable_free_operations(policy.operations, executed_request)
    if ticket_operations is None or prompt_operations is None:
        return None, (
            "Local daemon refused the Ticket: the request or launch prompt carries "
            "executable content the local policy cannot classify against an "
            "operation capability."
        )
    requested = ticket_operations | prompt_operations
    try:
        decision = authorize(
            capability,
            ticket_id=str(ticket.get("ticket_id") or ""),
            daemon_id=daemon_id,
            policy_version=policy.version,
            allowed_operations=allowed,
            requested=requested,
        )
    except Exception:  # noqa: BLE001 - a malformed capability refuses the Ticket
        return None, (
            "Local daemon refused the Ticket: the operation capability could "
            "not be authorized."
        )
    if not decision.allowed:
        return None, f"Local daemon refused the Ticket: {decision.reason}."
    return capability, ""


def _lease_bound_capability(
    capability: OperationCapability, lease: ActiveLease | None
) -> OperationCapability:
    """The capability with its expiry carried forward by the live lease (N71).

    A signed lease grant expires with the lease it was minted for, but the
    lease itself is renewed by every accepted heartbeat. While the lease is
    held, the capability lives as long as the lease does; a lost or expired
    lease leaves the original expiry in place.
    """
    if lease is None or lease.lost.is_set():
        return capability
    remaining = lease.deadline - time.monotonic()
    if remaining <= 0:
        return capability
    lease_expiry = datetime.now(timezone.utc) + timedelta(seconds=remaining)
    if lease_expiry <= capability.expires_at:
        return capability
    from dataclasses import replace

    return replace(capability, expires_at=lease_expiry)


_RESULT_AUDIT_REASON_LIMIT = 2000


def _record_result_audit(
    audit: dict[str, Any] | None, mode: str, reason: str
) -> None:
    """Record a result check enforce would have refused (N71 shadow)."""
    logger.warning("result check (%s, not enforced) would refuse: %s", mode, reason)
    if audit is None:
        return
    entry = f"result: {reason}"
    prior = str(audit.get("reason") or "")
    if entry in prior:
        return
    combined = f"{prior}; {entry}" if prior else entry
    audit.update(
        {
            "mode": str(audit.get("mode") or mode),
            "would_refuse": True,
            "reason": combined[:_RESULT_AUDIT_REASON_LIMIT],
        }
    )


def _result_refusal(
    policy: Any,
    capability: OperationCapability | None,
    text: Any,
    *,
    lease: ActiveLease | None = None,
    mode: str | None = None,
    provider: str = "",
    audit: dict[str, Any] | None = None,
) -> str:
    """Why this CLI result must not be reported, or an empty string.

    The result text is classified with the policy's operation patterns. An
    operation outside the capability is dropped here, and the reason that is
    reported does not include the result text.

    N71: only ``enforce`` (``CONDUCTOR_DAEMON_GRANT_VERIFIER_MODE``) refuses;
    ``shadow`` and ``off`` record what enforce would have refused in
    ``audit`` and report the result. Prose never makes a result
    unclassifiable; executable content the policy cannot classify refuses
    only in enforce and only when the provider does not set
    ``allow_unclassified``. The capability's expiry follows the live
    ``lease``, so a slow run whose lease heartbeats are accepted does not
    outlive its capability.
    """
    if policy is None or capability is None:
        return ""
    from src.conductor_cli.daemon_capability import (
        grant_verifier_mode,
        refuse_result,
    )
    from src.conductor_cli.daemon_policy import classify_result

    current_mode = grant_verifier_mode() if mode is None else mode
    enforce = current_mode == "enforce"
    if not isinstance(text, str):
        reason = (
            "Local daemon refused the result: the native CLI result could not "
            "be checked against the operation capability."
        )
        if enforce:
            return reason
        _record_result_audit(audit, current_mode, reason)
        return ""
    result = classify_result(policy.operations, text)
    if result.unclassified:
        rules = policy.provider_policy(provider) if provider else None
        allow_unclassified = bool(rules is not None and rules.allow_unclassified)
        if not allow_unclassified:
            # Unresolved shell spelling means the operation set is
            # incomplete: enforce refuses rather than forward it.
            reason = (
                "Local daemon refused the result: the result carries "
                "executable content the local policy cannot classify."
            )
            if enforce:
                return reason
            _record_result_audit(audit, current_mode, reason)
    reason = refuse_result(
        _lease_bound_capability(capability, lease),
        result.operations,
        policy_version=policy.version,
    )
    if not reason:
        return ""
    refusal = f"Local daemon refused the result: {reason}."
    if enforce:
        return refusal
    _record_result_audit(audit, current_mode, refusal)
    return ""


async def run_claimed(
    api: Api,
    provider: str,
    cwd: str,
    timeout: float,
    payload: dict[str, Any],
    manager: LeaseManager,
    claim_started: float | None = None,
    *,
    cancel_before_start: bool = False,
    poll_interval: float = 2.0,
    breaker: ProviderCircuitBreaker | None = None,
    permit: Permit | None = None,
    workspace_policy: str = "",
    isolation_root: str = "",
    worktree_root: str = "~/.conductor/worktrees",
    policy_store: PolicyStore | None = None,
    daemon_id: str = "",
    grant_verifier: GrantVerifier | None = None,
    origin: OriginCheck | None = None,
    expected_daemon_id: str = "",
) -> bool:
    from src.config.models import WORKTREE_ROOT_DEFAULT, conductor_worktree_root

    if not worktree_root or worktree_root == WORKTREE_ROOT_DEFAULT:
        worktree_root = conductor_worktree_root()
    workspace_policy = workspace_policy or os.environ.get(
        "CONDUCTOR_WORKSPACE_POLICY", ""
    )
    isolation_root = isolation_root or os.environ.get("CONDUCTOR_ISOLATION_ROOT", "")
    ticket = payload.get("ticket")
    if not ticket:
        if breaker is not None and permit is not None:
            breaker.release(permit)
        return False
    ticket_id = str(ticket["ticket_id"])
    lease_id = str(ticket["lease_id"])
    duration = float(ticket.get("lease_duration_seconds", 0))
    if duration <= 0:
        if breaker is not None and permit is not None:
            breaker.release(permit)
        return True
    expires = (
        claim_started if claim_started is not None else time.monotonic()
    ) + duration
    if expires <= time.monotonic():
        if breaker is not None and permit is not None:
            breaker.release(permit)
        return True
    if ticket.get("lease_expires_at"):
        # Use the returned duration as the conservative bound; wall-clock parsing is avoided.
        expires = min(expires, time.monotonic() + duration)
    lease = ActiveLease(ticket_id, lease_id, expires, duration, asyncio.Event())
    breaker_recorded = False
    manager_added = False
    # The checkout this lease bound on the server: path, generation, lease_id.
    host_worktree: dict[str, Any] = {}
    # N67: this run's object quarantine (and the run's Git dirs), set once
    # the Worker's isolation is prepared; the final block moves what is left.
    object_quarantine: list[tuple[Any, tuple[Path, Path] | None]] = []
    # Set when the run left the Ticket ref in a shape host Git would follow
    # elsewhere; the Worker's result is then refused.
    ref_refusal: list[str] = []
    ending_accepted = False
    stop_outcome: dict[str, bool] = {}
    # The Worker's process tree, once the CLI starts; removal waits for it.
    tracker: Any = None
    try:
        await manager.add(lease)
        manager_added = True
        # Retry host cleanups whose ending the server already accepted. Only
        # after the lease is registered: the manager renews it while a slow
        # removal or cleanup report runs, so the drain cannot expire it.
        await _drain_pending_worktree_cleanups(api, worktree_root)
        verified = None
        # N44 S2: a shadow-mode would-refuse decision rides on the report.
        capability_audit: dict[str, Any] = {}

        async def finish(report: dict[str, str]) -> None:
            nonlocal ending_accepted
            body: dict[str, Any] = dict(report)
            if capability_audit:
                body["capability_audit"] = dict(capability_audit)
            if verified is not None:
                # The identity the grant bound. Reading the policy file again
                # would report a version replaced during the run, and the
                # server would reject the result until the lease expired.
                body["daemon_id"] = verified.daemon_id
                body["policy_version"] = verified.policy_version
            elif body.get("state") not in {"failed", "dropped"}:
                # No trusted keys, so the grant was not verified. The older
                # path still reports the lease the claim named. A refusal or
                # an early cancel leaves these empty: the token is the daemon.
                claimed_daemon = str(ticket.get("daemon_id") or "")
                claimed_policy = str(ticket.get("policy_version") or "")
                if claimed_daemon:
                    body["daemon_id"] = claimed_daemon
                if claimed_policy:
                    body["policy_version"] = claimed_policy
            try:
                await _retry_call(
                    api.report,
                    ticket_id,
                    lease_id,
                    body,
                    deadline=lambda: lease.deadline,
                )
            except ApiError as exc:
                if exc.status in {401, 403}:
                    manager.fatal_error = exc
                    await manager.close()
                raise
            # Only an accepted ending may remove the checkout, and only after
            # this run is done with it (the final block below). Blocked and
            # waiting Tickets keep it for a dirty resume.
            if body.get("state") not in _NON_ENDING_STATES:
                ending_accepted = True

        if cancel_before_start:
            await finish(_cancelled_report())
            return True

        if origin is not None:
            reason, verified = await _origin_rejection(
                origin,
                api,
                ticket,
                payload.get("grant"),
                expected_daemon_id=expected_daemon_id,
            )
            if reason:
                await finish({"state": "failed", "summary": reason, "question": ""})
                return True

        ticket_provider = str(ticket.get("worker_provider") or "").strip().lower()
        configured_provider = provider.strip().lower()
        if ticket_provider != configured_provider:
            await finish(
                {
                    "state": "failed",
                    "summary": f"Local daemon provider mismatch: Ticket requires {ticket_provider or 'no provider'}, daemon is configured for {configured_provider}.",
                    "question": "",
                }
            )
            return True
        speculative = bool(ticket.get("speculative", payload.get("speculative", False)))
        executed_request = _executed_request(payload, ticket)
        policy_snapshot = None
        if policy_store is not None:
            from src.conductor_cli.daemon_policy import PolicyError

            try:
                policy_snapshot = policy_store.current()
            except PolicyError as exc:
                await finish(
                    {
                        "state": "failed",
                        "summary": (
                            "Local daemon refused the Ticket: the local policy is "
                            f"unusable ({exc})."
                        ),
                        "question": "",
                    }
                )
                return True
        rejection, read_only_run = _policy_verdict(
            policy_snapshot,
            ticket,
            configured_provider,
            speculative,
            executed_request,
        )
        if rejection:
            await finish(
                {
                    "state": "failed",
                    "summary": rejection,
                    "question": "",
                }
            )
            return True
        capability, capability_rejection = _capability_gate(
            policy_snapshot,
            ticket,
            configured_provider,
            speculative,
            daemon_id,
            grant_verifier,
            executed_request,
            claim_grant=payload.get("grant"),
            audit=capability_audit,
        )
        # N71: the result checks share the gate's mode, the live lease that
        # carries the capability's expiry, and the audit the report sends.
        from src.conductor_cli.daemon_capability import grant_verifier_mode

        result_options: dict[str, Any] = {
            "lease": lease,
            "mode": grant_verifier_mode(),
            "provider": configured_provider,
            "audit": capability_audit,
        }
        if capability_rejection:
            await finish(
                {
                    "state": "failed",
                    "summary": capability_rejection,
                    "question": "",
                }
            )
            return True
        if read_only_run and configured_provider not in _READ_ONLY_CAPABLE_PROVIDERS:
            await finish(
                {
                    "state": "failed",
                    "summary": f"Local daemon cannot enforce read-only execution for provider {configured_provider}.",
                    "question": "",
                }
            )
            return True
        ticket_cwd = ticket.get("cwd")
        selected_cwd: str | None = None
        ticket_owned_checkout = False
        worktree_spec = ticket.get("worktree")
        if isinstance(worktree_spec, dict):
            # The checkout is cut on this host, under this daemon's own root,
            # from the base_sha the server pinned; a server path is never used.
            # Prepare and bind run under the host lock so a drain cannot remove
            # the checkout between them. Nothing in this block reports: the one
            # failed report is sent below, outside every handler, so a
            # LeaseLost/ApiError from finish() can never lead to a second one.
            if not read_only_run and platform.system() != "Darwin":
                # Committing in a linked worktree needs exact per-file Git
                # write grants (the ref, its reflog, the admin files), which
                # only the macOS sandbox can express. Refuse before a checkout
                # is cut, not after the Worker has run and cannot commit.
                await finish(
                    _failed_launch(
                        "Local daemon refused the Ticket: a writable Ticket "
                        f"worktree needs per-file Git write grants, which the "
                        f"{platform.system() or 'this'} sandbox cannot enforce "
                        "(only the macOS sandbox can). Run this Ticket on a "
                        "macOS daemon."
                    )
                )
                return True
            launch_failure = await _prepare_and_bind_host_worktree(
                api,
                ticket,
                worktree_spec,
                lease,
                worktree_root=worktree_root,
                binding=host_worktree,
            )
            if launch_failure is not None:
                await finish(launch_failure)
                return True
            selected_cwd = str(host_worktree["path"])
            ticket_owned_checkout = True
        elif ticket_cwd is not None:
            try:
                from src.conductor_cli.worktree import resolve_ticket_worktree

                selected_cwd = str(resolve_ticket_worktree(ticket_cwd, worktree_root))
            except (OSError, RuntimeError, TypeError, ValueError):
                await finish(
                    {
                        "state": "failed",
                        "summary": "Local daemon rejected Ticket cwd: it must be an existing directory under the configured worktree root.",
                        "question": "",
                    }
                )
                return True
        from src.llms.native_cli.isolation import (
            read_workspace_policy,
            redact_diagnostic,
            worker_isolation,
        )

        try:
            workspace_id = ticket.get("workspace_id")
            if not isinstance(workspace_id, str) or not workspace_id.strip():
                raise RuntimeError("ticket has no workspace identity")
            if not workspace_policy or not isolation_root:
                raise RuntimeError("workspace isolation policy is not configured")
            policy = read_workspace_policy(
                workspace_policy, workspace_id, isolation_root
            )
            workdir = str(Path(str(policy["root"])).expanduser().resolve())
        except Exception:
            await finish(
                {
                    "state": "failed",
                    "summary": "Local daemon has no valid OS isolation policy for this workspace.",
                    "question": "",
                }
            )
            return True
        if selected_cwd is None:
            run_cwd = workdir
        else:
            resolved_cwd = Path(selected_cwd).resolve()
            under_workspace = resolved_cwd.is_relative_to(Path(workdir))
            under_worktree_root = False
            try:
                under_worktree_root = resolved_cwd.is_relative_to(
                    Path(worktree_root).expanduser().resolve()
                )
            except (OSError, RuntimeError):
                under_worktree_root = False
            # Only a validated Ticket-owned host checkout may sit beside the
            # workspace policy root under --worktree-root. Legacy cwd stays
            # confined to the workspace isolation root.
            allowed = under_workspace or (
                ticket_owned_checkout and under_worktree_root
            )
            if not allowed:
                await finish(
                    {
                        "state": "failed",
                        "summary": "Local daemon rejected Ticket cwd: it must be an existing directory inside the workspace isolation root (or a validated Ticket worktree under the configured worktree root).",
                        "question": "",
                    }
                )
                return True
            run_cwd = str(resolved_cwd)
        runtime_write_paths = tuple(policy.get("runtime_write_paths", ()))
        if policy_store is not None:
            policy_location = getattr(policy_store, "resolved_path", policy_store.path)
            location_rejection = _policy_location_rejection(
                policy_location,
                workdir,
                isolation_root,
                runtime_write_paths,
            )
            if location_rejection:
                await finish(
                    {
                        "state": "failed",
                        "summary": location_rejection,
                        "question": "",
                    }
                )
                return True
        origin_location_rejection = _origin_location_rejection(
            origin, workdir, isolation_root, runtime_write_paths
        )
        if origin_location_rejection:
            await finish(
                {
                    "state": "failed",
                    "summary": origin_location_rejection,
                    "question": "",
                }
            )
            return True
        git_summary = None
        kwargs: dict[str, Any] = {
            "cwd": run_cwd,
            "timeout_seconds": timeout,
            "model": str(ticket.get("worker_model") or "").strip() or None,
        }
        if configured_provider == "codex":
            kwargs["isolate_user_instructions"] = True
            # The local restriction reaches the sandbox: a read-only policy or
            # provider entry must not launch a writable run.
            kwargs["read_only"] = read_only_run
            # Runs only inside worker_isolation below; Codex cannot nest its
            # own sandbox-exec there, the outer isolation enforces the policy.
            kwargs["outer_sandboxed"] = True
        elif configured_provider in {"claude", "claude-code"}:
            kwargs["safe_mode"] = True
        elif configured_provider in {"grok", "zcode"}:
            kwargs["read_only"] = True
        elif configured_provider in {"cursor", "stepfun"}:
            kwargs["read_only"] = read_only_run
        from src.llms.native_cli.process import ProcessTracker, track_processes

        remote_cancelled = False
        detached_head: list[str] = []
        # N67: why this run's object quarantine was not moved.
        objects_refusal: list[str] = []
        cli: asyncio.Task[Any] | None = None
        leased_cli: asyncio.Task[Any] | None = None
        cancel_watch: asyncio.Task[dict[str, Any] | None] | None = None
        tracker = ProcessTracker()

        async def stop_supervised_cli() -> None:
            children = (cancel_watch, leased_cli, cli)
            for child in children:
                if child is not None and not child.done():
                    child.cancel()
            pending = tuple(child for child in children if child is not None)
            if pending:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*pending, return_exceptions=True), timeout=2.0
                    )
                except TimeoutError:
                    print(
                        f"Local CLI cancellation exceeded cleanup timeout for ticket {ticket_id}",
                        file=sys.stderr,
                    )

        # Best-effort live output for this claimed run. Only the provider with
        # an incremental JSONL transcript takes the streaming path; every other
        # provider (and an operator opt-out) keeps the unchanged run_process
        # behavior. Nothing below fails the Worker if the channel is unavailable.
        streamer: _DaemonOutputStream | None = None
        stream_gate: _StreamPolicyGate | None = None
        if configured_provider == "codex" and _token_stream_enabled():
            streamer = _DaemonOutputStream(api, ticket_id, lease_id, lease_id)
            # The same operation capability that withholds a rejected report
            # must withhold the live text before it is published.
            stream_gate = _StreamPolicyGate(
                policy_snapshot, capability, **result_options
            )

        try:

            async def run_isolated_cli():
                # Linked worktrees keep gitdir/commondir outside the checkout;
                # isolation must read the common store and write the worktree
                # admin plus objects/refs or status/add/commit fail closed.
                isolation_reads = list(policy.get("runtime_read_paths") or ())
                isolation_writes = list(policy.get("runtime_write_paths") or ())
                git_write_files: tuple[Path, ...] = ()
                git_env: dict[str, str] = {}
                if host_worktree.get("path"):
                    git_root = Path(host_worktree["path"])
                    isolation_reads.extend(
                        str(path) for path in linked_worktree_git_paths(git_root)
                    )
                    if not read_only_run:
                        # N67: never the shared objects/ directory; only this
                        # run's quarantine, migrated by the host afterwards.
                        git_write_files = linked_worktree_git_write_files(
                            git_root, branch=str(host_worktree.get("branch") or "")
                        )
                        if git_write_files:
                            git_dirs = linked_worktree_git_dirs(git_root)
                            quarantine = await asyncio.to_thread(
                                prepare_object_quarantine, git_root
                            )
                            object_quarantine.append((quarantine, git_dirs))
                            isolation_writes.append(str(quarantine.path))
                            isolation_reads.extend(str(p) for p in quarantine.kept)
                            git_env = quarantine.worker_env()
                from src.server.services.autonomy.worker_test_pg import (
                    async_throwaway_pg,
                    entered_in_async,
                    throwaway_pg_base_dir,
                )

                # N19: an opt-in throwaway test PG (CONDUCTOR_WORKER_TEST_PG=on),
                # injected as TEST_DB_*; off by default, so test_pg is None.
                # initdb/pg_ctl run in a thread so lease heartbeats keep going;
                # worker_isolation stays a sync manager entered inline.
                async with async_throwaway_pg(
                    throwaway_pg_base_dir(isolation_root, ticket_id),
                    ticket_id=ticket_id,
                ) as test_pg, entered_in_async(worker_isolation(
                    workspace_id=workspace_id,
                    root=isolation_root,
                    workdir=run_cwd,
                    test_db_env=test_pg.env() if test_pg is not None else None,
                    env_names=policy["env"],
                    runtime_read_paths=tuple(dict.fromkeys(isolation_reads)),
                    localhost_tcp_ports=tuple(policy.get("localhost_tcp_ports", ())),
                    keychain=bool(policy.get("keychain", False)),
                    cfprefs=bool(policy.get("cfprefs", False)),
                    fsevents=bool(policy.get("fsevents", False)),
                    user_info=bool(policy.get("user_info", False)),
                    runtime_write_paths=tuple(dict.fromkeys(isolation_writes)),
                    # Exact single-file grants (macOS ``literal`` rules): the
                    # Ticket ref, its reflog and the worktree admin files,
                    # never the ref store they live in.
                    runtime_write_files=tuple(str(path) for path in git_write_files),
                    git_env=git_env,
                    read_only=read_only_run,
                    # The full policy root, not the Ticket's cwd: a read-only
                    # run must also refuse a write grant next to the cwd.
                    workspace_root=workdir,
                )):
                    from src.server.services.autonomy.git_delivery import (
                        AsyncWorkspaceDeliveryLock,
                        GitDeliveryError,
                        safe_git_commit_delivery,
                        safe_git_workspace_summary,
                        ticket_git_baseline,
                        ticket_git_baseline_cached,
                    )
                    from src.server.services.autonomy.worker import (
                        bind_ticket_scratch_prompt,
                    )

                    # Scratch must sit under private HOME (writable in the
                    # sandbox), not an API-server path outside the mount set.
                    run_prompt = executed_request
                    if not read_only_run and not speculative:
                        run_prompt, _ = bind_ticket_scratch_prompt(
                            executed_request, ticket_id
                        )

                    baseline = None
                    baseline_error = False
                    git_summary = None
                    commit_delivery = "not_checked"
                    baseline_clean = False
                    baseline_cached = ticket_git_baseline_cached(
                        str(ticket.get("ticket_id") or ""), run_cwd
                    )
                    if not read_only_run and not speculative:
                        try:
                            # The OS sandbox contains Git configuration and
                            # filesystem effects. The lock only makes each
                            # snapshot internally consistent; never serialize
                            # concurrent CLI work in the same workspace.
                            async with AsyncWorkspaceDeliveryLock(
                                run_cwd, timeout=30.0, workspace_root=workdir
                            ):
                                baseline, baseline_error = await asyncio.to_thread(
                                    ticket_git_baseline,
                                    str(ticket.get("ticket_id") or ""),
                                    run_cwd,
                                )
                        except GitDeliveryError:
                            baseline_error = True
                    with track_processes(tracker):
                        from src.llms.native_cli.registry import get_adapter

                        isolated_adapter = get_adapter(configured_provider, **kwargs)
                        # Stream only through a real adapter that can build the
                        # invocation; a wrapper/test double without that surface
                        # keeps the plain run path.
                        streaming = streamer is not None and callable(
                            getattr(isolated_adapter, "build_invocation", None)
                        )
                        try:
                            if streaming:
                                if _codex_app_server_enabled() and _app_server_available():
                                    result = await _run_codex_app_server_streaming(
                                        isolated_adapter,
                                        run_prompt,
                                        run_cwd,
                                        streamer,
                                        gate=stream_gate,
                                    )
                                else:
                                    result = await _run_codex_streaming(
                                        isolated_adapter,
                                        run_prompt,
                                        run_cwd,
                                        streamer,
                                        gate=stream_gate,
                                    )
                            else:
                                result = await isolated_adapter.run(
                                    run_prompt,
                                    cwd=run_cwd,
                                )
                        except Exception as exc:
                            from src.llms.native_cli.base import (
                                NativeCliNoFinalAnswer,
                            )

                            if not isinstance(exc, NativeCliNoFinalAnswer):
                                cause = _classified_native_cli_failure(
                                    configured_provider, exc
                                )
                                from src.server.public_errors import (
                                    log_failure,
                                    new_correlation_id,
                                )

                                correlation_id = new_correlation_id()
                                log_failure(
                                    correlation_id,
                                    exc,
                                    log=logger,
                                    where=(
                                        "Local native CLI failure for ticket "
                                        f"{ticket_id}"
                                    ),
                                    detail=cause,
                                )
                                wrapped = RuntimeError(
                                    f"{_NATIVE_CLI_FAILURE_PREFIX}{cause}"
                                )
                                wrapped.correlation_id = correlation_id  # type: ignore[attr-defined]
                                raise wrapped from None
                            # The turn succeeded without a final message: no
                            # completion report, which settles as waiting
                            # below, not as a CLI failure.
                            logger.warning(
                                "Local native CLI turn for ticket %s ended "
                                "without a final message",
                                ticket_id,
                            )
                            result = SimpleNamespace(text="", final_text="")
                        text = getattr(result, "text", result)
                        final_text = _final_assistant_message(result, text)
                    if git_write_files:
                        # The sandbox granted the ref by exact name; a Worker
                        # can still swap that name for a symlink, directory or
                        # hard link. Check before any host-side Git runs here.
                        tampered = await asyncio.to_thread(
                            ticket_ref_tampering,
                            Path(host_worktree["path"]),
                            branch=str(host_worktree.get("branch") or ""),
                            allow_detached=True,
                            require_ref=True,
                        )
                        if tampered:
                            ref_refusal.append(tampered)
                            return (
                                redact_diagnostic(str(text or ""), None),
                                redact_diagnostic(str(final_text or ""), None),
                                None,
                                "not_checked",
                                False,
                            )
                        if object_quarantine:
                            not_moved = await _migrate_run_quarantine(
                                object_quarantine, tracker, host_worktree
                            )
                            if not_moved:
                                objects_refusal.append(not_moved)
                                return (
                                    redact_diagnostic(str(text or ""), None),
                                    redact_diagnostic(str(final_text or ""), None),
                                    None,
                                    "not_checked",
                                    False,
                                )
                        # A plain detached HEAD (HEAD holds a commit) is the
                        # Worker's own checkout state, not tampering: keep the
                        # checkout and block on the operator's switch back.
                        detached = await asyncio.to_thread(
                            ticket_checkout_detached_head,
                            Path(host_worktree["path"]),
                        )
                        if detached:
                            detached_head.append(detached)
                            return (
                                redact_diagnostic(str(text or ""), None),
                                redact_diagnostic(str(final_text or ""), None),
                                None,
                                "not_checked",
                                False,
                            )
                    if not read_only_run and not speculative and (
                        baseline is not None or baseline_error
                    ):
                        if baseline_error:
                            git_summary = await asyncio.to_thread(
                                safe_git_workspace_summary,
                                run_cwd,
                                None,
                                baseline_unavailable=True,
                            )
                            commit_delivery = "unknown"
                        else:
                            try:
                                async with AsyncWorkspaceDeliveryLock(
                                    run_cwd, timeout=30.0, workspace_root=workdir
                                ):
                                    git_summary = await asyncio.to_thread(
                                        safe_git_workspace_summary,
                                        run_cwd,
                                        baseline,
                                    )
                                    commit_delivery = await asyncio.to_thread(
                                        safe_git_commit_delivery,
                                        run_cwd,
                                        _delivery_baseline(
                                            baseline,
                                            host_worktree,
                                            ticket,
                                            baseline_cached=baseline_cached,
                                        ),
                                    )
                                    baseline_clean = (
                                        baseline.root_id is not None
                                        and not baseline.entries
                                    )
                            except GitDeliveryError:
                                git_summary = await asyncio.to_thread(
                                    safe_git_workspace_summary,
                                    run_cwd,
                                    None,
                                    baseline_unavailable=True,
                                )
                                commit_delivery = "unknown"
                    return (
                        redact_diagnostic(str(text or ""), None),
                        redact_diagnostic(str(final_text or ""), None),
                        git_summary,
                        commit_delivery,
                        baseline_clean,
                    )

            if verified is not None:
                from src.conductor_cli.daemon_grant import (
                    GrantExpired,
                    refusal_reason,
                )

                if int(verified.expires_at) <= int(time.time()):
                    # Last look before the CLI: the workspace-policy read and
                    # the isolation setup above are awaits too.
                    await finish(
                        {
                            "state": "failed",
                            "summary": refusal_reason(
                                GrantExpired("signed grant has expired")
                            ),
                            "question": "",
                        }
                    )
                    return True
            if streamer is not None:
                streamer.start()
            abandoned = False
            try:
                cli = asyncio.create_task(run_isolated_cli())
                leased_cli = asyncio.create_task(_await_with_lease(cli, lease))
                cancel_watch = asyncio.create_task(
                    _watch_remote_cancel(api, ticket_id, poll_interval, cli)
                )
                done, _ = await asyncio.wait(
                    {leased_cli, cancel_watch}, return_when=asyncio.FIRST_COMPLETED
                )
                stop_status = cancel_watch.result() if cancel_watch in done else None
                if stop_status is not None:
                    remote_cancelled = True
                    cli.cancel()
                    await asyncio.gather(cli, return_exceptions=True)
                    leased_cli.cancel()
                    await asyncio.gather(leased_cli, return_exceptions=True)
                    try:
                        # Only an accepted process-gone acknowledgement (in
                        # stop_outcome) lets the final block remove the checkout.
                        await _acknowledge_stop(
                            api, ticket_id, lease_id, tracker, stop_status, stop_outcome
                        )
                    except (
                        Exception
                    ) as exc:  # noqa: BLE001 - the server sweep escalates
                        print(
                            f"Stop acknowledgement for ticket {ticket_id} failed: "
                            f"{type(exc).__name__}",
                            file=sys.stderr,
                        )
                    return True
                cancel_watch.cancel()
                await asyncio.gather(cancel_watch, return_exceptions=True)
                (
                    result_text,
                    final_text,
                    git_summary,
                    commit_delivery,
                    baseline_clean,
                ) = await leased_cli
            except (LeaseLost, asyncio.CancelledError):
                # Nothing more can be published (the lease is gone) or should be
                # (the run is being torn down), and the stop acknowledgement
                # below must not wait behind a flush.
                abandoned = True
                raise
            finally:
                # The final flush happens before the report: streaming must
                # never delay or fail the Worker's outcome path.
                if streamer is not None:
                    await streamer.finalize(flush=not abandoned)
            completion_contract = _claim_has_completion_contract(ticket)
            if completion_contract:
                # Only the final assistant message is the completion report;
                # the Git checks are reconciled into it here, so the shared
                # guard below must not prefix them a second time.
                report = parse_report(
                    final_text,
                    git_summary=git_summary,
                    commit_delivery=commit_delivery,
                    speculative=speculative,
                    baseline_clean=baseline_clean,
                )
                reconciled_git_summary, git_summary = git_summary, None
            else:
                # An older server sent the marker-line prompt: parse what it
                # asked for, and let the shared guard apply the Git check.
                report = parse_legacy_report(result_text)
                reconciled_git_summary = git_summary
            if capability is None:
                if breaker is not None and permit is not None:
                    breaker.record_success(permit)
                    breaker_recorded = True
            else:
                result_rejection = _result_refusal(
                    policy_snapshot, capability, result_text, **result_options
                )
                if not result_rejection:
                    # Decoded JSON values can join a normalized command split
                    # across escapes in the raw text; the structured summary
                    # and question, and every decoded report string the body
                    # carries, are checked as well.
                    from src.server.services.autonomy.completion import (
                        completion_report_from_text,
                        completion_report_strings,
                    )

                    decoded = completion_report_from_text(final_text)
                    values = [
                        str(report.get("summary") or ""),
                        str(report.get("question") or ""),
                        *(
                            completion_report_strings(decoded)
                            if decoded is not None
                            else ()
                        ),
                    ]
                    for value in values:
                        if not value:
                            continue
                        result_rejection = _result_refusal(
                            policy_snapshot, capability, value, **result_options
                        )
                        if result_rejection:
                            break
                if result_rejection:
                    report = {
                        "state": "failed",
                        "summary": result_rejection,
                        "question": "",
                    }
                    git_summary = reconciled_git_summary
                elif breaker is not None and permit is not None:
                    breaker.record_success(permit)
                    breaker_recorded = True
        except LeaseLost:
            # The heartbeat watcher can notice the cancelled lease before the
            # status poll does; the stop still needs its acknowledgement.
            # Without a verified process-gone stop, keep the host checkout.
            await _acknowledge_pending_stop(
                api, ticket_id, lease_id, tracker, stop_outcome
            )
            return True
        except asyncio.CancelledError:
            await stop_supervised_cli()
            acked = await _acknowledge_pending_stop(
                api, ticket_id, lease_id, tracker, stop_outcome
            )
            if (
                not acked
                and not remote_cancelled
                and not lease.lost.is_set()
                and time.monotonic() < lease.deadline
            ):
                report_task = asyncio.create_task(finish(_cancelled_report()))
                try:
                    await asyncio.shield(report_task)
                except (
                    Exception
                ) as exc:  # noqa: BLE001 - cancellation reporting is best effort
                    print(
                        f"Cancellation report for ticket {ticket_id} failed: "
                        f"{type(exc).__name__}",
                        file=sys.stderr,
                    )
            raise
        except (
            Exception
        ) as exc:  # noqa: BLE001 - CLI failure becomes this Ticket's report.
            if breaker is not None and permit is not None:
                breaker.record_failure(permit)
                breaker_recorded = True
            summary = (
                f"Local daemon {provider} failed: "
                f"{_failure_detail(exc, where=f'Local daemon {provider} failed')}"
            )
            report = {
                "state": "failed",
                "summary": summary[:20000],
                "question": "",
            }
            # A CLI failure can carry the same operation text a result would.
            # Check it before it is reported, and do not forward it when it
            # names an operation outside the capability.
            if capability is not None:
                refusal = _result_refusal(
                    policy_snapshot, capability, report["summary"], **result_options
                )
                if refusal:
                    report = {"state": "failed", "summary": refusal, "question": ""}
        finally:
            # Keep this idempotent with the cancellation path above so every
            # exit, including adapter setup failures, releases child tasks.
            await stop_supervised_cli()
        if object_quarantine and not ref_refusal:
            # The run ended before the post-run checks (a CLI failure): check
            # the Ticket ref here too (N68), then move the objects (a resume
            # builds on them); a quarantine not moved is kept and retried.
            tampered = await asyncio.to_thread(
                ticket_ref_tampering,
                Path(host_worktree["path"]),
                branch=str(host_worktree.get("branch") or ""),
                allow_detached=True,
                require_ref=True,
            )
            if tampered:
                ref_refusal.append(tampered)
                object_quarantine.clear()
            else:
                not_moved = await _migrate_run_quarantine(
                    object_quarantine, tracker, host_worktree
                )
                if not_moved and not not_moved.startswith(_STRAGGLER_KEPT):
                    objects_refusal.append(not_moved)
                elif not_moved:
                    logger.warning(
                        "ticket object quarantine kept ticket=%s: %s",
                        ticket_id,
                        not_moved,
                    )
        if ref_refusal:
            report = {
                "state": "failed",
                "summary": (
                    "Local daemon refused the Worker result: "
                    f"{ref_refusal[0]} inside the sandbox, so the branch "
                    f"{host_worktree.get('branch') or ''} cannot be trusted. "
                    "Inspect the Ticket branch in the dispatch repository on the "
                    "daemon host and delete it before the work is retried."
                ),
                "question": "",
            }
            git_summary = None
        elif objects_refusal:
            # 'blocked', not an ending: the checkout (and a detached HEAD's
            # commits) stay with the kept quarantine, no cleanup is journaled,
            # and a resume retries the move first.
            report = _objects_not_moved_report(objects_refusal[0])
            git_summary = None
        elif detached_head:
            # 'blocked' is not an ending: the checkout and its commits stay,
            # no cleanup is journaled, and a resume re-checks the branch.
            report = _detached_head_report(
                str(host_worktree.get("branch") or ""), detached_head[0]
            )
            git_summary = None
        if git_summary:
            from src.server.services.autonomy.git_delivery import apply_git_delivery_guard

            report = apply_git_delivery_guard(report, git_summary)
        if lease.lost.is_set() or time.monotonic() >= lease.deadline:
            await _acknowledge_pending_stop(
                api, ticket_id, lease_id, tracker, stop_outcome
            )
            return True
        report_task = asyncio.create_task(finish(report))
        try:
            await _await_with_lease(report_task, lease)
        except LeaseLost:
            await _acknowledge_pending_stop(
                api, ticket_id, lease_id, tracker, stop_outcome
            )
            return True
        except ApiError as exc:
            if not exc.retryable:
                if exc.status == 409:
                    # The stop transaction won the race with this report.
                    await _acknowledge_pending_stop(
                api, ticket_id, lease_id, tracker, stop_outcome
            )
                lease.lost.set()
                if exc.status in {401, 403}:
                    manager.fatal_error = exc
                    await manager.close()
                    raise
            return True
        return True
    finally:
        if breaker is not None and permit is not None and not breaker_recorded:
            breaker.release(permit)
        if manager_added:
            await manager.remove(ticket_id)
        keep_for_objects = False
        # A refused (tampered) run's quarantine is not moved here: it is left
        # on disk, and the orphan sweep of a later prepare moves it (fsck'd,
        # no tip check). Elsewhere the move reads the tip by plain oid
        # (ticket_ref_oid), never through a symref, so no ref check is needed.
        if object_quarantine and ref_refusal:
            object_quarantine.clear()
        if object_quarantine and host_worktree.get("path"):
            # A stop or lost lease left the run's objects unmoved: move them
            # now, or keep the checkout with them (N67).
            try:
                not_moved = await _migrate_run_quarantine(
                    object_quarantine, tracker, host_worktree
                )
                # A live straggler already defers the removal itself.
                keep_for_objects = bool(not_moved) and not not_moved.startswith(
                    _STRAGGLER_KEPT
                )
            except Exception:  # noqa: BLE001 - keep the checkout on doubt
                keep_for_objects = True
            if keep_for_objects:
                logger.warning(
                    "ticket object quarantine kept with its checkout ticket=%s",
                    ticket_id,
                )
        if host_worktree.get("path") and not keep_for_objects and (
            ending_accepted or stop_outcome.get("verified")
        ):
            # The run is over and the server accepted the ending (a terminal
            # report or a verified stop) for the lease that bound this
            # checkout. A failed report never reaches here, so a Ticket the
            # server may still resume keeps its checkout.
            try:
                await _cleanup_after_accepted_ending(
                    api, worktree_root, ticket_id, host_worktree, tracker=tracker
                )
            except Exception:  # noqa: BLE001 - the journal retries it
                logger.warning(
                    "ticket worktree cleanup failed ticket=%s",
                    ticket_id,
                    exc_info=True,
                )


async def run_one(
    api: Api,
    provider: str,
    cwd: str,
    timeout: float,
    manager: LeaseManager | None = None,
    claim_id: str | None = None,
    poll_interval: float = 2.0,
    breaker: ProviderCircuitBreaker | None = None,
    workspace_policy: str = "",
    isolation_root: str = "",
    worktree_root: str = "~/.conductor/worktrees",
    policy_store: PolicyStore | None = None,
    daemon_id: str = "",
    grant_verifier: GrantVerifier | None = None,
    origin: OriginCheck | None = None,
    expected_daemon_id: str = "",
) -> bool:
    from src.config.models import WORKTREE_ROOT_DEFAULT, conductor_worktree_root

    if not worktree_root or worktree_root == WORKTREE_ROOT_DEFAULT:
        worktree_root = conductor_worktree_root()
    own_manager = manager is None
    manager = manager or LeaseManager(api)
    heartbeat_task = asyncio.create_task(manager.run()) if own_manager else None
    claim_id = claim_id or str(uuid.uuid4())
    claim_started: list[float] = []
    if breaker is None:
        from src.conductor_cli.provider_breaker import ProviderCircuitBreaker

        breaker = ProviderCircuitBreaker()
    permit: Permit | None = None

    async def report_cancelled_claim(payload: dict[str, Any], started: float) -> None:
        if payload.get("ticket"):
            await run_claimed(
                api,
                provider,
                cwd,
                timeout,
                payload,
                manager,
                started,
                cancel_before_start=True,
                poll_interval=poll_interval,
                workspace_policy=workspace_policy,
                isolation_root=isolation_root,
                worktree_root=worktree_root,
                policy_store=policy_store,
                daemon_id=daemon_id,
                grant_verifier=grant_verifier,
                origin=origin,
                expected_daemon_id=expected_daemon_id,
            )

    try:
        payload, permit, _ = await _claim_with_breaker(
            api,
            breaker,
            claim_id,
            capture_start=claim_started,
            on_cancel_result=report_cancelled_claim,
            idle_heartbeat_interval=max(0.1, poll_interval),
            manager=manager,
            policy_version=_policy_version(policy_store),
        )
        if not payload.get("ticket"):
            # No claim: still drain host cleanups left by terminal Tickets so
            # recovery does not wait for the next successful claim.
            await _drain_pending_worktree_cleanups(api, worktree_root)
            await _retry_call(api.heartbeat, None)
            return False
        return await run_claimed(
            api,
            provider,
            cwd,
            timeout,
            payload,
            manager,
            claim_started[0] if claim_started else None,
            poll_interval=poll_interval,
            breaker=breaker,
            permit=permit,
            workspace_policy=workspace_policy,
            isolation_root=isolation_root,
            worktree_root=worktree_root,
            policy_store=policy_store,
            daemon_id=daemon_id,
            grant_verifier=grant_verifier,
            origin=origin,
            expected_daemon_id=expected_daemon_id,
        )
    finally:
        if permit is not None:
            breaker.release(permit)
        if heartbeat_task:
            await manager.close()
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)


def _policy_version(store: PolicyStore | None) -> str:
    """The version a lease is bound to; empty when no policy is configured."""
    if store is None:
        return ""
    from src.conductor_cli.daemon_policy import PolicyError

    try:
        return store.current().version
    except PolicyError:
        return ""


def _open_policy_store(args: argparse.Namespace) -> tuple[PolicyStore | None, str]:
    """Resolve this device's policy file; an unusable policy stops the daemon."""
    from src.conductor_cli.daemon_policy import (
        DEFAULT_POLICY_PATH,
        PolicyError,
        PolicyStore,
    )

    configured = str(getattr(args, "policy", "") or "").strip()
    required = bool(getattr(args, "require_policy", False))
    path = Path(configured).expanduser() if configured else DEFAULT_POLICY_PATH
    store = PolicyStore(path)
    if not path.exists():
        if configured or required:
            return None, (
                f"No local daemon policy at {path}. Write one, or drop "
                "--require-policy to run without a policy."
            )
        print(
            f"No local daemon policy at {path}: this daemon runs every operation "
            "the cloud asks for.",
            file=sys.stderr,
        )
        return None, ""
    try:
        store.load()
    except PolicyError as exc:
        return None, f"Local daemon policy is unusable: {exc}"
    return store, ""


def _open_origin(args: argparse.Namespace) -> tuple[OriginCheck | None, str]:
    """Resolve this device's trusted server keys. An unusable file stops it."""
    from src.conductor_cli.daemon_grant import (
        DEFAULT_NONCE_FILE,
        DEFAULT_TRUSTED_KEYS,
        GrantError,
        NonceStore,
        OriginCheck,
        TrustedKeys,
    )

    configured = str(getattr(args, "trusted_keys", "") or "").strip()
    required = bool(getattr(args, "require_origin", False))
    path = Path(configured).expanduser() if configured else DEFAULT_TRUSTED_KEYS
    if not path.exists():
        if configured or required:
            return None, (
                f"No trusted server keys at {path}. Write one, or drop "
                "--require-origin to run without origin verification."
            )
        print(
            f"No trusted server keys at {path}: this daemon does not verify "
            "that a lease was signed by the server.",
            file=sys.stderr,
        )
        return None, ""
    try:
        keys = TrustedKeys.load(path)
    except (GrantError, UnicodeError) as exc:
        return None, f"Trusted server keys are unusable: {exc}"
    nonce_configured = str(getattr(args, "nonce_file", "") or "").strip()
    nonce_path = (
        Path(nonce_configured).expanduser() if nonce_configured else DEFAULT_NONCE_FILE
    )
    return OriginCheck(keys, NonceStore(nonce_path)), ""


def _resolve_isolation_root(args: argparse.Namespace) -> str:
    """Isolation root from --isolation-root, falling back to CONDUCTOR_ISOLATION_ROOT."""
    return (
        str(getattr(args, "isolation_root", "") or "").strip()
        or os.environ.get("CONDUCTOR_ISOLATION_ROOT", "").strip()
    )


async def main_async(args: argparse.Namespace) -> int:
    from src.conductor_cli.provider_breaker import ProviderCircuitBreaker

    from src.config.models import WORKTREE_ROOT_DEFAULT, conductor_worktree_root

    worktree_root = str(getattr(args, "worktree_root", "") or "")
    if not worktree_root or worktree_root == WORKTREE_ROOT_DEFAULT:
        worktree_root = conductor_worktree_root()
    if not 1 <= getattr(args, "max_concurrency", 1) <= 32:
        print("--max-concurrency must be between 1 and 32", file=sys.stderr)
        return 2
    isolation_root = _resolve_isolation_root(args)
    if not isolation_root:
        print(
            "An isolation root is required (--isolation-root or CONDUCTOR_ISOLATION_ROOT)",
            file=sys.stderr,
        )
        return 2
    policy_store, policy_error = _open_policy_store(args)
    if policy_error:
        print(policy_error, file=sys.stderr)
        return 2
    origin, origin_error = _open_origin(args)
    if origin_error:
        print(origin_error, file=sys.stderr)
        return 2
    expected_daemon_id = str(getattr(args, "daemon_id", "") or "").strip()
    daemon_id = str(getattr(args, "daemon_id", "") or "").strip()
    if policy_store is not None and not daemon_id:
        print(
            "A local daemon policy is loaded, but no --daemon-id was given. "
            "An operation capability cannot be bound to this daemon.",
            file=sys.stderr,
        )
        return 2
    grant_verifier = _grant_verifier(args)
    status_file = str(getattr(args, "status_file", "") or "")
    configured_provider = str(args.provider or "").strip().lower()
    last_persisted_status: dict[str, Any] | None = None

    def persist_status(current: ProviderCircuitBreaker) -> None:
        nonlocal last_persisted_status
        current_status = current.status()
        if current_status == last_persisted_status:
            return
        try:
            if _persist_breaker_status(status_file, configured_provider, current):
                last_persisted_status = current_status
        except OSError as exc:
            print(
                f"Provider breaker status write failed: {type(exc).__name__}",
                file=sys.stderr,
            )

    try:
        breaker = ProviderCircuitBreaker(
            failure_threshold=getattr(args, "breaker_failure_threshold", 3),
            reset_timeout=getattr(args, "breaker_reset_timeout", 30.0),
            backoff_max=getattr(args, "breaker_backoff_max", 300.0),
            jitter=getattr(args, "breaker_jitter", 0.2),
            recovery_success_threshold=getattr(
                args, "breaker_recovery_success_threshold", 1
            ),
            on_change=persist_status,
        )
    except ValueError as exc:
        print(f"Invalid provider breaker configuration: {exc}", file=sys.stderr)
        return 2
    token = _take_daemon_token(args.token)
    if not token:
        print("A daemon token is required (--token or CONDUCTOR_DAEMON_TOKEN)")
        return 2
    provider = str(args.provider or "").strip().lower()
    if provider not in supported_providers():
        print(f"Unsupported native CLI provider: {provider or '(empty)'}")
        return 2
    cwd = str(Path(args.cwd).expanduser().resolve())
    workspace_policy = str(getattr(args, "workspace_policy", "") or "")
    Path(cwd).mkdir(parents=True, exist_ok=True)
    api = Api(args.base_url, token)
    concurrency = 1 if args.once else max(1, args.max_concurrency)
    manager = LeaseManager(api, interval=args.heartbeat_interval)
    heartbeat_task = asyncio.create_task(manager.run())
    tasks: set[asyncio.Task[bool]] = set()
    try:
        while True:
            persist_status(breaker)
            if manager.fatal_error is not None:
                return 2
            while len(tasks) < concurrency:
                claim_id = str(uuid.uuid4())

                async def report_cancelled_claim(
                    payload: dict[str, Any], started: float
                ) -> None:
                    if payload.get("ticket"):
                        await run_claimed(
                            api,
                            provider,
                            cwd,
                            args.timeout,
                            payload,
                            manager,
                            started,
                            cancel_before_start=True,
                            poll_interval=args.interval,
                            workspace_policy=workspace_policy,
                            isolation_root=isolation_root,
                            worktree_root=worktree_root,
                            policy_store=policy_store,
                            daemon_id=daemon_id,
                            grant_verifier=grant_verifier,
                            origin=origin,
                            expected_daemon_id=expected_daemon_id,
                        )

                try:
                    claim_started: list[float] = []
                    payload, permit, _ = await _claim_with_breaker(
                        api,
                        breaker,
                        claim_id,
                        capture_start=claim_started,
                        on_cancel_result=report_cancelled_claim,
                        idle_heartbeat_interval=args.heartbeat_interval,
                        manager=manager,
                        policy_version=_policy_version(policy_store),
                    )
                except ApiError as exc:
                    if exc.status in {401, 403} or manager.fatal_error is not None:
                        print(str(exc), file=sys.stderr)
                        return 2
                    raise
                if not payload.get("ticket"):
                    await _drain_pending_worktree_cleanups(api, worktree_root)
                    if args.once:
                        await _retry_call(api.heartbeat, None)
                        return 0
                    break
                task = asyncio.create_task(
                    run_claimed(
                        api,
                        provider,
                        cwd,
                        args.timeout,
                        payload,
                        manager,
                        claim_started[0] if claim_started else None,
                        poll_interval=args.interval,
                        breaker=breaker,
                        permit=permit,
                        workspace_policy=workspace_policy,
                        isolation_root=isolation_root,
                        worktree_root=worktree_root,
                        policy_store=policy_store,
                        daemon_id=daemon_id,
                        grant_verifier=grant_verifier,
                        origin=origin,
                        expected_daemon_id=expected_daemon_id,
                    )
                )
                tasks.add(task)
                task.add_done_callback(
                    lambda done, task_permit=permit: _task_finished_with_breaker(
                        tasks, done, breaker, task_permit
                    )
                )
                if args.once:
                    break
            if args.once:
                if tasks:
                    try:
                        await asyncio.gather(*tasks)
                    except ApiError as exc:
                        if exc.status in {401, 403} or manager.fatal_error is not None:
                            return 2
                        raise
                if manager.fatal_error is not None:
                    return 2
                return 0
            if tasks:
                await asyncio.wait(
                    tasks, timeout=args.interval, return_when=asyncio.FIRST_COMPLETED
                )
            else:
                await asyncio.sleep(args.interval)
            persist_status(breaker)
    finally:
        await manager.close()
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        active_tasks = list(tasks)
        for task in active_tasks:
            task.cancel()
        await asyncio.gather(*active_tasks, return_exceptions=True)


class _SignedLeaseCapabilityVerifier:
    """Authenticate the claim's signed lease grant as an operation capability.

    The live claim format puts a lease grant at the top level and does not
    emit ``ticket.capability``. ``verify_grant`` checks the signature against
    the device's trusted keys; the signed payload binds the daemon id, the
    lease and the policy version. Lease grants do not sign operations — an
    ``operations`` field on the body is unsigned and is refused. The
    capability gate then fills operations from the local policy allowlist.
    """

    def __init__(self, keys: Any, *, daemon_id: str) -> None:
        bound = str(daemon_id or "").strip()
        if not bound:
            raise ValueError(
                "a signed lease capability verifier requires a daemon id"
            )
        self.keys = keys
        self.daemon_id = bound

    def verify(self, grant: Mapping[str, Any]) -> OperationCapability:
        from datetime import datetime, timezone

        from src.conductor_cli.daemon_capability import (
            GrantError,
            OperationCapability,
        )
        from src.conductor_cli.daemon_grant import (
            GrantError as OriginGrantError,
            verify_grant,
        )

        if not self.daemon_id:
            raise GrantError(
                "this daemon has no id to bind an operation capability to"
            )
        # operations is not in daemon_grant's signed fields. Trusting it would
        # let a relay widen or strip a restriction without resigning.
        if "operations" in grant:
            raise GrantError(
                "the lease grant carries unsigned operation restrictions"
            )
        try:
            verified = verify_grant(grant, self.keys, now=int(time.time()))
            if verified.daemon_id != self.daemon_id:
                raise OriginGrantError(
                    "the operation capability is for a different daemon"
                )
        except OriginGrantError as exc:
            raise GrantError(
                str(exc) or "the operation capability was rejected"
            ) from exc
        return OperationCapability(
            ticket_id=verified.ticket_id,
            daemon_id=verified.daemon_id,
            policy_version=verified.policy_version,
            expires_at=datetime.fromtimestamp(verified.expires_at, tz=timezone.utc),
            operations=frozenset(),
        )


def _grant_verifier(args: argparse.Namespace) -> GrantVerifier | None:
    """The verifier a policy-backed daemon uses to authenticate a capability.

    Built from the same trusted server keys as origin verification. The
    signature is checked against those keys and binds the daemon id, the
    lease and the policy version. With no usable keys or no daemon id there
    is no verifier, and a policy-backed daemon then refuses every Ticket.
    """
    from src.conductor_cli.daemon_grant import (
        DEFAULT_TRUSTED_KEYS,
        GrantError,
        TrustedKeys,
    )

    daemon_id = str(getattr(args, "daemon_id", "") or "").strip()
    if not daemon_id:
        # Fail closed: an unbound verifier would accept a grant minted for
        # any daemon. Policy-backed startup already requires --daemon-id.
        return None
    configured = str(getattr(args, "trusted_keys", "") or "").strip()
    path = Path(configured).expanduser() if configured else DEFAULT_TRUSTED_KEYS
    if not path.exists():
        return None
    try:
        keys = TrustedKeys.load(path)
    except (GrantError, UnicodeError):
        # Non-UTF-8 contents raise UnicodeDecodeError before GrantError.
        return None
    return _SignedLeaseCapabilityVerifier(keys, daemon_id=daemon_id)


def _take_daemon_token(explicit_token: str | None) -> str:
    """Keep the API credential in memory but out of native CLI child envs."""
    token = explicit_token or os.getenv("CONDUCTOR_DAEMON_TOKEN", "")
    os.environ.pop("CONDUCTOR_DAEMON_TOKEN", None)
    return token


def main(argv: list[str] | None = None) -> int:
    def positive_int(value: str) -> int:
        parsed = int(value)
        if not 1 <= parsed <= 32:
            raise argparse.ArgumentTypeError("must be between 1 and 32")
        return parsed

    def positive_count(value: str) -> int:
        parsed = int(value)
        if parsed < 1:
            raise argparse.ArgumentTypeError("must be positive")
        return parsed

    def positive_float(value: str) -> float:
        parsed = float(value)
        if parsed <= 0:
            raise argparse.ArgumentTypeError("must be positive")
        return parsed

    def unit_float(value: str) -> float:
        parsed = float(value)
        if not 0 <= parsed <= 1:
            raise argparse.ArgumentTypeError("must be between 0 and 1")
        return parsed

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--token", default="")
    parser.add_argument(
        "--daemon-id",
        default="",
        help=(
            "this daemon's id: an operation capability is bound to it, and when "
            "trusted keys are configured a signed grant must name it too"
        ),
    )
    parser.add_argument("--provider", required=True)
    parser.add_argument("--cwd", default=".")
    parser.add_argument(
        "--workspace-policy",
        default="",
        help="trusted local JSON mapping workspace ids to private roots and env-name allowlists",
    )
    parser.add_argument("--isolation-root", default="")
    parser.add_argument(
        "--worktree-root",
        default="~/.conductor/worktrees",
        help="allowed root for Ticket-selected worktrees",
    )
    parser.add_argument(
        "--policy",
        default="",
        help=(
            "path to this device's daemon policy file "
            "(default: ~/.config/conductor/daemon_policy.yaml when it exists)"
        ),
    )
    parser.add_argument(
        "--require-policy",
        action="store_true",
        help="refuse to start unless a local policy file is available",
    )
    parser.add_argument(
        "--trusted-keys",
        default="",
        help=(
            "path to the server public keys this device trusts "
            "(default: ~/.config/conductor/daemon_trusted_keys.json when it exists)"
        ),
    )
    parser.add_argument(
        "--require-origin",
        action="store_true",
        help="refuse to start unless a trusted server-key file is available",
    )
    parser.add_argument(
        "--nonce-file",
        default="",
        help=(
            "path to the replay record for signed grants "
            "(default: ~/.local/state/conductor/daemon-grant-nonces.json)"
        ),
    )
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--heartbeat-interval", type=float, default=10.0)
    parser.add_argument(
        "--max-concurrency",
        "--concurrency",
        dest="max_concurrency",
        type=positive_int,
        default=1,
        help="maximum tickets this daemon runs concurrently, from 1 to 32 (default: 1)",
    )
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--status-file", default="")
    parser.add_argument("--breaker-failure-threshold", type=positive_count, default=3)
    parser.add_argument(
        "--breaker-recovery-success-threshold", type=positive_count, default=1
    )
    parser.add_argument("--breaker-reset-timeout", type=positive_float, default=30.0)
    parser.add_argument("--breaker-backoff-max", type=positive_float, default=300.0)
    parser.add_argument("--breaker-jitter", type=unit_float, default=0.2)
    parser.add_argument("--once", action="store_true")
    return asyncio.run(main_async(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
