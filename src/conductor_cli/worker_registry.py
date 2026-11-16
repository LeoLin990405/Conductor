"""The worker facts the CLI needs, kept next to the CLI that needs them.

The wheel that ships ``conductor`` contains this package and **not**
``src.llms``: the native-CLI adapters, the session manager and the failure
classifier are the server's business. A CLI that imports them works in a
checkout and breaks in an installation, and it breaks in the worst way --
a module that is not there at all, so the command dies before it can say
anything:

    ModuleNotFoundError: No module named 'src'

So the few facts the CLI needs about a worker live here, in this package:

* **which names are one worker** (:data:`WORKER_ALIASES`) -- the CodexBar
  snapshot calls GLM ``zai``, the native-CLI registry calls it ``glm`` and
  ``zhipu`` and the dispatch pool calls it ``zcode``; those are one provider
  and the CLI counts them once;
* **how large that worker's context window is**
  (:data:`CONTEXT_WINDOW_TOKENS`) -- without a window there is no "how much
  of the window is used" to report;
* **how the counters of two spellings of one worker are merged**
  (:func:`merge_worker_records`) -- the successes add up and the newer quota
  marker wins.

These are **copies** of facts that
``src/llms/native_cli/identity.py`` and ``src/llms/native_cli/registry.py``
own, and a copy is only safe while it is held in step with the original.
Two tests hold it in step: one walks every alias of the original table and
one compares the window of every provider the original registry knows. Both
live in ``tests/unit/conductor_cli/`` and may import ``src`` -- they run in a
checkout, where the original is importable.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

__all__ = [
    "CONTEXT_WINDOW_TOKENS",
    "WORKER_ALIASES",
    "canonical_worker_id",
    "context_window_tokens",
    "merge_worker_records",
]

#: The canonical id of every spelling Conductor accepts.
#:
#: A copy of ``src.llms.native_cli/identity.py``'s table. The two are held
#: in step by ``test_worker_registry.py``; when the original grows a name,
#: that test fails until this table grows it too.
WORKER_ALIASES: Mapping[str, str] = MappingProxyType(
    {
        # Doubao's Ark CLI.
        "arkcli": "doubao",
        "doubao": "doubao",
        # Claude Code.
        "claude": "claude",
        "claude-code": "claude",
        "claudedev": "claude",
        # Codex.
        "codex": "codex",
        "codexrev": "codex",
        "openai": "codex",
        "openai-codex": "codex",
        "cursor": "cursor",
        # DeepSeek's DSH harness.
        "deepseek": "deepseek",
        "deepseek-dsh": "deepseek",
        "dsh": "deepseek",
        # GLM. The registry registers zcode and zhipu as two adapters for the
        # same worker, and the snapshot calls it zai.
        "glm": "glm",
        "zai": "glm",
        "zcode": "glm",
        "zhipu": "glm",
        "grok": "grok",
        "kimi": "kimi",
        "moonshot": "kimi",
        "manus": "manus",
        "mimo": "mimo",
        "opencode": "opencode",
        # MiniMax Code and the mmx CLI.
        "minimax": "minimax",
        "minimax-mmx": "minimax",
        "mmx": "minimax",
        # StepFun.
        "step": "stepfun",
        "stepfun": "stepfun",
    }
)

#: The context window of every provider whose window is configured, in tokens.
#:
#: A copy of what ``src/llms/native_cli/registry.py`` answers with, written
#: out for the names the registry resolves. A name that is not here has no
#: configured window, and a window that is not configured is reported as
#: "unknown" (``None``) and never as zero.
CONTEXT_WINDOW_TOKENS: Mapping[str, int] = MappingProxyType(
    {
        "arkcli": 128_000,
        "claude": 1_000_000,
        "claude-code": 1_000_000,
        "codex": 272_000,
        "deepseek": 1_000_000,
        "deepseek-dsh": 1_000_000,
        "doubao": 128_000,
        "glm": 1_000_000,
        "grok": 500_000,
        "kimi": 262_144,
        "mimo": 1_048_576,
        "minimax": 1_000_000,
        "step": 262_144,
        "stepfun": 262_144,
        "zcode": 1_000_000,
        "zhipu": 1_000_000,
    }
)


def canonical_worker_id(name: str | None) -> str:
    """The one id a worker has, however the files spell it.

    A known spelling answers with its canonical id; a name nobody knows
    answers with itself, stripped and lowercased, so the function is total
    and never refuses a name. A name that is not text at all, and a name that
    is only whitespace, name no worker and answer with the empty id.

    The spelling is matched case-insensitively and without its surrounding
    whitespace: the names come from files on disk and from command lines,
    and neither is careful about either.
    """
    if not isinstance(name, str):
        return ""
    text = name.strip().lower()
    if not text:
        return ""
    return WORKER_ALIASES.get(text, text)


def context_window_tokens(provider: str | None) -> int | None:
    """The provider's context window in tokens, or None when it is not configured.

    An unknown provider is not an error here: the caller decides what an
    unconfigured window means (a worker without one has no "how much of the
    window is used" to report).
    """
    if not isinstance(provider, str):
        return None
    key = provider.strip().lower()
    if not key:
        return None
    return CONTEXT_WINDOW_TOKENS.get(key)


def merge_worker_records(
    records: Mapping[str, Mapping[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Merge worker records that are really the same worker under two names.

    The counters of the aliases add up and the quota marker of the newer
    generation wins. Returns a mapping keyed by canonical id.

    The old state format kept the counters under the literal provider name,
    so a file written before the canonical id existed may hold a ``step``
    record and a ``stepfun`` record separately. They are one worker, so their
    counters are one worker's counters: the successes of both add up, and the
    quota failure of the newer generation is the one that counts. Taking the
    **larger** of the two successes instead would drop the successes of one
    spelling and report a worker that is working again as out of quota.

    The marker is the generation the quota failure was recorded at, so the
    larger marker is the newer one. A spelling without a marker has no quota
    failure to contribute and does not clear the marker of its alias.

    The input is only read: the caller's records are not touched.
    """
    merged: dict[str, dict[str, Any]] = {}
    for spelling, record in records.items():
        if not isinstance(record, Mapping):
            continue
        canonical = canonical_worker_id(spelling)
        target = merged.get(canonical)
        if target is None:
            target = {"success_seq": 0, "quota_failed_at_seq": None}
            merged[canonical] = target
        target["success_seq"] = int(target["success_seq"]) + int(
            record.get("success_seq") or 0
        )
        marker = record.get("quota_failed_at_seq")
        if marker is None:
            continue
        marker = int(marker)
        current = target["quota_failed_at_seq"]
        target["quota_failed_at_seq"] = (
            marker if current is None else max(int(current), marker)
        )
    return merged
