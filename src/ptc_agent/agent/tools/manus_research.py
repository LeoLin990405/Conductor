"""Manus as an information-retrieval tool, not a model provider.

PTC calls this for deep multi-source research and browsing. The Manus task owns
its own tools and returns a cited answer; it never becomes a selectable model
provider or a native coding-agent runtime.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from langchain_core.tools import tool

from src.llms.manus import ManusAdapter, manus_api_key_available
from ptc_agent.utils.provider_usage import balance_state


def manus_research_enabled() -> bool:
    enabled = os.getenv("MANUS_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    return enabled and manus_api_key_available()


def create_manus_research_tool(*, thread_id: str) -> Any:
    """Build the main-agent-only ManusResearch tool for one thread."""

    @tool("ManusResearch", parse_docstring=True)
    async def manus_research(prompt: str, timeout_seconds: int = 600) -> str:
        """Delegate deep information retrieval to Manus.

        Use Manus when the task needs broad web research, many sources,
        browsing, long-form synthesis, or Manus-native research artifacts. Do
        not use it for a quick lookup, a single page fetch, coding, or anything
        a direct tool can finish. It consumes the deployment's Manus credits.

        Args:
            prompt: The complete research question and required output format.
                Ask for inline source URLs and dates.
            timeout_seconds: Overall budget in seconds (30-1800).
        """
        timeout = max(30.0, min(float(timeout_seconds or 600), 1800.0))
        from ptc_agent.utils.provider_roles import (
            ProviderRoleError,
            require_provider_role,
            route_research_provider,
        )
        from src.server.services.orchestration import (
            WorkerKind,
            finish_worker,
            register_cancel,
            start_worker,
            unregister_cancel,
        )

        if (
            route_research_provider(
                prompt, preferred_provider="manus", task_kind="research"
            )
            != "manus"
        ):
            return "ManusResearch is only available for research and information-retrieval tasks."
        try:
            require_provider_role("manus", "research", prompt, task_kind="research")
        except ProviderRoleError:
            return "ManusResearch is only available for research and information-retrieval tasks."

        try:
            adapter = ManusAdapter(
                timeout_seconds=min(timeout, 900.0), poll_timeout_seconds=timeout
            )
        except Exception as exc:  # noqa: BLE001 - do not expose exception contents
            return f"ManusResearch failed: {type(exc).__name__}."

        try:
            balance = await adapter.get_credit_balance()
        except Exception:  # noqa: BLE001 - unknown balance does not block research
            balance = None
        observed_balance_state = balance_state("manus", balance)
        if observed_balance_state == "exhausted":
            return "ManusResearch not started: Manus reports zero available credits."

        worker_id = ""
        try:
            worker_id = await start_worker(
                thread_id=thread_id,
                kind=WorkerKind.DIRECT,
                provider="manus",
                label="Manus · research",
                prompt=prompt,
                meta={"role": "research", "balance_state": observed_balance_state},
            )
        except Exception:  # noqa: BLE001 - orchestration telemetry must not block
            pass

        task = asyncio.create_task(adapter.run(prompt))

        def _cancel() -> None:
            if not task.done():
                task.cancel()

            async def _cancel_remote() -> None:
                try:
                    await adapter.cancel_remote()
                except Exception:  # noqa: BLE001 - remote cancellation is best effort
                    pass

            asyncio.create_task(_cancel_remote())

        if worker_id:
            register_cancel(worker_id, _cancel)
        try:
            result = await task
        except asyncio.CancelledError:
            if worker_id:
                try:
                    await finish_worker(
                        worker_id, status="cancelled", error="cancelled by user"
                    )
                except Exception:  # noqa: BLE001 - telemetry must not mask cancellation
                    pass
            return "Manus research cancelled by user."
        except Exception as exc:  # noqa: BLE001 - report failure without exception contents
            if worker_id:
                try:
                    await finish_worker(
                        worker_id,
                        status="failed",
                        error=f"{type(exc).__name__} during Manus research",
                    )
                except Exception:  # noqa: BLE001 - telemetry must not mask tool failure
                    pass
            return f"ManusResearch failed: {type(exc).__name__}."
        finally:
            if worker_id:
                try:
                    unregister_cancel(worker_id)
                except Exception:  # noqa: BLE001 - cleanup telemetry is best effort
                    pass

        source = adapter.last_task_url or (result.session_id or "")
        if worker_id:
            try:
                await finish_worker(worker_id, status="completed", result=result.text)
            except Exception:  # noqa: BLE001 - telemetry must not mask the answer
                pass
        return (
            f"Manus research result (task: {source or 'unknown'}):\n\n{result.text}"
        )
    return manus_research
