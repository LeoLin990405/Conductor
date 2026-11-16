"""Explicit OS-isolation runtime switch for cloud autonomous Workers.

A cloud Worker runs through the shared PTC runtime, which has no per-workspace
OS boundary comparable to the local daemon's policy-enforced sandbox. Cloud
launches therefore stay refused unless a deployment deliberately names an
OS-isolated runtime *and* this build can verify that runtime is available.

Every state fails closed:

* unset, ``none``, ``off``, ``disabled``, or an empty value means no isolated
  runtime was requested; the default cloud-Worker refusal stands;
* any other value names a runtime and is accepted as a runtime id only. The id
  must be registered in ``ISOLATED_RUNTIME_VERIFIERS`` and its verifier must
  report the boundary available; a malformed, unknown, unavailable, or raising
  runtime is refused;
* a runtime is launchable only when it registers both a verifier and a
  launcher. This build registers ``docker`` (see
  ``docker_worker_runtime.py``) with both, so the switch can be enabled only
  after the engine, image, isolation flags and worker CLI verify; any
  verification or launch failure still fails the Ticket closed and never falls
  back to the shared PTC runtime. Other runtime ids stay fail-closed until a
  provider registers the same pair (``docs/CLOUD-WORKERS.md``).

The switch is read on every call: an environment override reflects a changed
environment immediately, while ``config.yaml`` goes through the cached
infrastructure loader, so a deployed YAML change needs a restart (or a config
cache clear).

No secret handling lives here: the switch names a runtime, never a credential.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from src.config.settings import get_autonomy_cloud_worker_runtime

logger = logging.getLogger(__name__)

#: Values that mean "off" rather than a runtime id. Unset is also off.
OFF_RUNTIME_VALUES = frozenset({"", "none", "off", "disabled"})

#: A runtime id used in configuration/logs: no paths, shell text, whitespace.
_RUNTIME_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")

#: Runtime id -> verifier. A verifier returns exactly ``(available: bool,
#: detail: str)`` and must prove the OS boundary is actually usable before a
#: launch could be allowed. This build registers the shipped ``docker``
#: runtime lazily; unknown ids stay refused.
ISOLATED_RUNTIME_VERIFIERS: dict[str, Callable[[], tuple[bool, str]]] = {}

#: Runtime id -> launcher. A launcher starts one Ticket's Worker inside the
#: isolated boundary and returns its settled report; the launch effect only
#: calls it when the verifier passed. Registration of a verifier without a
#: launcher can never start a Worker.
CloudWorkerLauncher = Callable[[dict[str, Any], str], Awaitable[dict[str, Any]]]
ISOLATED_RUNTIME_LAUNCHERS: dict[str, CloudWorkerLauncher] = {}

#: The default refusal, unchanged while no isolated runtime is configured.
DEFAULT_CLOUD_WORKER_REJECTION_ERROR = (
    "Cloud Worker execution is disabled until this workspace has an "
    "OS-isolated worker runtime. Assign the Ticket to a configured local daemon."
)
_REJECTION_PREFIX = "Cloud Worker execution is disabled"


@dataclass(frozen=True)
class CloudWorkerIsolation:
    """Resolved switch state: what was requested and whether it can be used."""

    #: Normalized runtime id, or None when the switch is off.
    requested: str | None
    #: The operator asked for an OS-isolated runtime.
    enabled: bool
    #: A registered runtime verified it can enforce the OS boundary.
    verified: bool
    #: A launcher is registered for this runtime (launch path wired).
    launch_wired: bool
    #: Operator-facing explanation of this state.
    reason: str

    @property
    def launch_allowed(self) -> bool:
        """True only when the switch is on, verified, and the path is wired."""
        return self.enabled and self.verified and self.launch_wired


class CloudWorkerLaunchError(RuntimeError):
    """The isolated runtime could not run the Worker; the message is safe.

    ``operator_message``, when set, is the Ticket's text for this failure: the
    message followed by the Worker's output, screened as that final string. It
    is the only form that carries the output; the message, ``repr`` and
    traceback of the exception do not, so log lines and class-name prefixes
    cannot reassemble anything from it. Read it with ``ticket_error``.
    """

    operator_message: str | None = None


def launch_failure_prefix(runtime_id: str) -> str:
    """How every launch failure of a runtime starts, here and in the runtime."""
    return f"OS-isolated cloud Worker runtime {runtime_id!r} failed: "


def _screened_by_the_launcher(exc: BaseException, prefix: str) -> str | None:
    final = getattr(exc, "operator_message", None)
    return final if isinstance(final, str) and final.startswith(prefix) else None


def ticket_error(exc: BaseException) -> str:
    """The text to store as the Ticket's error for a failed launch."""
    final = getattr(exc, "operator_message", None)
    return final if isinstance(final, str) and final else str(exc)


def _ensure_builtin_runtimes() -> None:
    """Import the runtimes this build ships. Import failure stays fail-closed."""
    from src.server.services.autonomy import docker_worker_runtime

    try:
        docker_worker_runtime.install()
    except Exception:  # noqa: BLE001 - a broken provider must not crash refusal
        logger.exception("[autonomy] could not register the docker Worker runtime")


def _shown(value: str, *, limit: int = 64) -> str:
    """Bound a raw config value for an error/log line: no control characters."""
    text = "".join(ch if ch.isprintable() else " " for ch in str(value)).strip()
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def register_cloud_worker_runtime(
    runtime_id: str,
    verifier: Callable[[], tuple[bool, str]],
    launcher: CloudWorkerLauncher | None = None,
) -> None:
    """Register an OS-isolated cloud Worker runtime (the extension point).

    A provider calls this at import time from a wiring module once it can
    verify its boundary and launch a Worker inside it. Ids are normalized and
    validated so a config value can never be shell text, a path, or a
    duplicate registration that silently replaces a verifier. A verifier
    alone is not launchable; the launcher is the separate half that may start
    a Worker, and both are registered by the same call.
    """
    normalized = runtime_id.strip().lower()
    if not normalized or normalized in OFF_RUNTIME_VALUES:
        raise ValueError(f"{runtime_id!r} is not a runtime id")
    if not _RUNTIME_ID_PATTERN.fullmatch(normalized):
        raise ValueError(f"{runtime_id!r} is not a valid runtime id")
    if normalized in ISOLATED_RUNTIME_VERIFIERS:
        raise ValueError(f"cloud Worker runtime {normalized!r} is already registered")
    if not callable(verifier):
        raise TypeError("cloud Worker runtime verifier must be callable")
    if launcher is not None and not callable(launcher):
        raise TypeError("cloud Worker runtime launcher must be callable")
    ISOLATED_RUNTIME_VERIFIERS[normalized] = verifier
    if launcher is not None:
        ISOLATED_RUNTIME_LAUNCHERS[normalized] = launcher
    logger.info(
        "[autonomy] registered OS-isolated cloud Worker runtime %s (launch=%s)",
        normalized,
        launcher is not None,
    )


def _verified_availability(result: object) -> tuple[bool, str] | None:
    """Parse a verifier result, or None when its shape is not the contract.

    A truthy non-boolean (a string, a number) must not read as verified: the
    verifier contract is exactly ``(bool, str)`` so a provider cannot enable
    the gate with an accident such as returning ``"ok"``.
    """
    if not isinstance(result, tuple) or len(result) != 2:
        return None
    available, detail = result
    if not isinstance(available, bool) or not isinstance(detail, str):
        return None
    return available, detail


def resolve_cloud_worker_isolation() -> CloudWorkerIsolation:
    """Resolve the switch and, when on, verify the requested runtime.

    Never raises for operator input: a malformed value, an unknown runtime, a
    verifier returning nonsense, or a raising verifier all resolve to a
    not-available state so the caller fails the launch closed.
    """
    _ensure_builtin_runtimes()
    launch_wired = False
    raw = get_autonomy_cloud_worker_runtime()
    if raw is None:
        return CloudWorkerIsolation(
            None,
            False,
            False,
            launch_wired,
            "no OS-isolated cloud Worker runtime is configured (off by default)",
        )
    runtime = raw.strip().lower()
    if runtime in OFF_RUNTIME_VALUES:
        return CloudWorkerIsolation(
            None,
            False,
            False,
            launch_wired,
            f"OS-isolated cloud Worker runtime is off ({_shown(raw)})",
        )
    if not _RUNTIME_ID_PATTERN.fullmatch(runtime):
        return CloudWorkerIsolation(
            _shown(raw),
            True,
            False,
            launch_wired,
            f"configured runtime {_shown(raw)!r} is not a valid runtime identifier",
        )
    verifier = ISOLATED_RUNTIME_VERIFIERS.get(runtime)
    launch_wired = runtime in ISOLATED_RUNTIME_LAUNCHERS
    if verifier is None:
        return CloudWorkerIsolation(
            runtime,
            True,
            False,
            launch_wired,
            f"runtime {runtime!r} is not implemented by this build",
        )
    try:
        result = verifier()
    except Exception as exc:  # verifier failure must fail closed
        logger.exception(
            "[autonomy] cloud Worker runtime verification failed runtime=%s", runtime
        )
        return CloudWorkerIsolation(
            runtime,
            True,
            False,
            launch_wired,
            f"runtime {runtime!r} verification raised {type(exc).__name__}",
        )
    checked = _verified_availability(result)
    if checked is None:
        logger.error(
            "[autonomy] cloud Worker runtime returned an invalid verification "
            "result runtime=%s",
            runtime,
        )
        return CloudWorkerIsolation(
            runtime,
            True,
            False,
            launch_wired,
            f"runtime {runtime!r} verifier returned an invalid result",
        )
    available, detail = checked
    detail_text = _shown(detail, limit=200)
    if not available:
        reason = f"runtime {runtime!r} is not available"
        if detail_text:
            reason += f": {detail_text}"
        return CloudWorkerIsolation(runtime, True, False, launch_wired, reason)
    reason = f"runtime {runtime!r} verified available"
    if detail_text:
        reason += f" ({detail_text})"
    return CloudWorkerIsolation(runtime, True, True, launch_wired, reason)


def cloud_worker_launch_allowed() -> bool:
    """True only when the isolated runtime is enabled, verified, and wired.

    Synchronous convenience for tests and synchronous callers; async request
    paths must use :func:`cloud_worker_launch_allowed_async` so verification
    never blocks the event loop.
    """
    return resolve_cloud_worker_isolation().launch_allowed


async def launch_cloud_worker(
    ticket: dict[str, Any], prompt: str
) -> dict[str, Any]:
    """Run one cloud Worker through the verified isolated runtime.

    The caller (the launch effect) has already settled the Ticket's thread and
    must fail the Ticket closed when this raises. There is deliberately no
    fallback to the shared PTC runtime: a missing/unverified switch, a missing
    launcher, or any launch error becomes ``CloudWorkerLaunchError``.
    """
    isolation = await resolve_cloud_worker_isolation_async()
    launcher = (
        ISOLATED_RUNTIME_LAUNCHERS.get(isolation.requested)
        if isolation.requested
        else None
    )
    if not isolation.launch_allowed or launcher is None:
        raise CloudWorkerLaunchError(refusal_error_for(isolation))
    try:
        return await launcher(ticket, prompt)
    except CloudWorkerLaunchError:
        raise
    except Exception as exc:  # noqa: BLE001 - launch failure is a closed Ticket
        logger.exception(
            "[autonomy] isolated cloud Worker launch failed runtime=%s",
            isolation.requested,
        )
        fixed = launch_failure_prefix(isolation.requested) + _shown(
            str(exc), limit=300
        )
        failure = CloudWorkerLaunchError(fixed)
        # The Docker runtime finishes (and screens) the Ticket error itself,
        # under this very prefix, and keeps only that string on its exception;
        # it is taken only if it starts exactly as this module would start it.
        # (No local holds it, so no frame of this traceback does.)
        failure.operator_message = _screened_by_the_launcher(exc, fixed + ": ")
        raise failure from exc


async def resolve_cloud_worker_isolation_async() -> CloudWorkerIsolation:
    """Resolve the switch off the event loop.

    Verification can run Docker probe commands (bounded but not instant), so
    every async caller must use this wrapper rather than blocking the loop.
    """
    return await asyncio.to_thread(resolve_cloud_worker_isolation)


async def cloud_worker_launch_allowed_async() -> bool:
    """Async form of :func:`cloud_worker_launch_allowed` for request paths."""
    return (await resolve_cloud_worker_isolation_async()).launch_allowed


def refusal_error_for(isolation: CloudWorkerIsolation) -> str:
    """The ticket-facing refusal for one already-resolved switch state."""
    if not isolation.enabled:
        return DEFAULT_CLOUD_WORKER_REJECTION_ERROR
    if isolation.launch_allowed:
        # Defensive: callers check launch_allowed first. Never tell a caller a
        # shared runtime is usable when the isolated path is the only legal one.
        return DEFAULT_CLOUD_WORKER_REJECTION_ERROR
    if isolation.verified and not isolation.launch_wired:
        detail = (
            f"runtime {isolation.requested!r} verified available, but no isolated "
            "cloud Worker launcher is registered for it"
        )
    elif isolation.verified:
        detail = isolation.reason
    else:
        detail = isolation.reason
    return (
        f"{_REJECTION_PREFIX}: an OS-isolated runtime is required but {detail}. "
        "Assign the Ticket to a configured local daemon."
    )


def cloud_worker_refusal_error() -> str:
    """Synchronous refusal message; async paths should resolve once."""
    return refusal_error_for(resolve_cloud_worker_isolation())
