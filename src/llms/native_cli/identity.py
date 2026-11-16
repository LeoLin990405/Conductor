"""One id per worker, however the files spell it.

A worker is one provider, and three layers spell it differently: the
CodexBar snapshot calls GLM "zai", the native-CLI registry calls it "glm" and
"zhipu", and the dispatch pool calls it "zcode". The state file therefore
holds the same worker under two names, and the counters of one worker end up
split across two records.

The split is invisible in the counters and visible in the answer: the status
line folds the aliases into one row and takes the **largest** of their
counters, so a worker that got a turn through under its other name still
reads as out of quota. ``step`` succeeding five times, a quota failure, and
``stepfun`` succeeding once is exactly that case -- the worker is working
again, but the folded row still says exhausted.

This module owns the one answer to "which worker is this?". Everything that
writes, reads, migrates or shows a worker goes through
:func:`canonical_worker_id` first, so the counters of one worker are counted
once, and the merge of two old records (:func:`merge_worker_records`) adds
them up instead of picking one of them.

The spellings come from two places: the native-CLI registry
(``src/llms/native_cli/registry.py``) and the dispatch pool, which calls the
same workers by other names. The table below is the single source of truth --
the registry's own names are a subset of it -- and a test keeps the two in
step: a provider the registry adds and the table does not know would split
one worker in two, so that test fails.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

#: The canonical id of every spelling Conductor accepts.
#:
#: The registry's own names are here (``step`` and ``stepfun`` are both
#: registered adapters), and so are the names the dispatch layer and the
#: snapshot use for the same workers. Every canonical id is itself one of the
#: spellings, so a canonical id is always a name the table knows.
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


def _group_spellings() -> dict[str, tuple[str, ...]]:
    """The spellings of every canonical id, sorted.

    The grouping is derived from the table rather than written out a second
    time, so the two can never disagree about which names are one worker.
    """
    grouped: dict[str, list[str]] = {}
    for spelling, canonical in WORKER_ALIASES.items():
        grouped.setdefault(canonical, []).append(spelling)
    return {
        canonical: tuple(sorted(spellings))
        for canonical, spellings in grouped.items()
    }


_SPELLINGS_BY_CANONICAL: dict[str, tuple[str, ...]] = _group_spellings()


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


def known_worker_ids() -> frozenset[str]:
    """The canonical ids this module knows."""
    return frozenset(_SPELLINGS_BY_CANONICAL)


def worker_ids_for(name: str | None) -> tuple[str, ...]:
    """Every spelling that belongs to the same worker as ``name``.

    This is what a write path needs to find an old record of a worker that
    was written under another spelling. A name nobody knows is a worker of
    its own, and answers with its own canonical id alone.
    """
    canonical = canonical_worker_id(name)
    spellings = _SPELLINGS_BY_CANONICAL.get(canonical)
    if spellings:
        return spellings
    return (canonical,)


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
