"""Budgeted memory-context assembly over the memory projection.

Phase 2's injection rule: retrieve per tier, rank, deduplicate by content, then
fill a token budget with a per-tier quota so one tier cannot crowd out the rest.
The result is an index block (keys + index lines), never bodies: the agent reads
a detail file on demand, which is what keeps the injected context small.

Token counts are **estimates** (CJK ≈ 1 token/char, other ≈ 4 chars/token) —
injection accounting uses the same estimate the compactor uses for budgets, and
the audit row records it as an estimate, not provider-reported usage.

Selection is deterministic: ties break by tier order, then retrieval rank. That
is what the memory eval pins, and what makes a selection reproducible from the
same candidates.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from src.server.contracts.refactor import ContractError
from src.server.contracts.memory_concept import row_class, row_concepts
from src.server.contracts.memory_validity import (
    confidence_value,
    is_current,
    is_low_confidence,
)
from src.server.contracts.memory_origin import normalize_memory_metadata
from src.server.contracts.memory_policy_metadata import (
    normalize_memory_policy_metadata_layers,
)
from src.server.services.refactor_flags import refactor_flag_enabled

logger = logging.getLogger(__name__)

#: Specificity order used for quota order and tie-breaking: what the turn needs
#: now first? No — preferences and constraints must survive pressure, so user
#: comes first, then workspace/project facts, then session/task state.
TIER_ORDER: tuple[str, ...] = ("user", "project", "workspace", "session", "task")

#: Per-tier share of the budget in the first pass; the second pass spends what
#: is left over any tier, so an empty tier never wastes its quota.
TIER_WEIGHTS: Mapping[str, float] = {
    "user": 0.25,
    "project": 0.15,
    "workspace": 0.25,
    "session": 0.20,
    "task": 0.15,
}

#: Default injected budget for a turn while the render switch is off (Phase 2
#: shadow mode) and for the eval; one turn should not spend the context window
#: on index lines.
DEFAULT_MEMORY_BUDGET = 800

#: Search callable: (user_id, tier, query, limit, thread_id, workspace_id).
MemorySearch = Callable[
    [str, str, str, int, str | None, str | None],
    Awaitable[Sequence[Mapping[str, Any]]],
]

#: Optional caller gate over raw recall rows: (tier, row) -> keep. A Ticket
#: uses it to keep private and unrelated memories out of a prompt that leaves
#: the machine; a gate that raises drops the row (fail closed).
RowFilter = Callable[[str, Mapping[str, Any]], bool]

_ENTRY_FIELDS = ("entry_key", "index_text", "content_hash", "status")

#: The index block is one line per entry. A source's index text is truncated to
#: the projection's own limit and newlines are collapsed, so an external body can
#: neither forge extra prompt lines nor spend the budget as a full document.
MAX_INDEX_LINE_CHARS = 500

#: How many entries a Ticket's memory audit names. The concept list is never
#: truncated (it is what the audit is for); the per-entry records are, because
#: the event payload is bounded and a Ticket's selection is not.
_MAX_TICKET_AUDIT_RECORDS = 32
#: Room left in an event payload for the keys that describe the split itself
#: (``chunk`` and ``chunks``), so a chunk measured against the cap still fits
#: once those keys are written.
_TICKET_AUDIT_ENVELOPE_BYTES = 64


def estimate_tokens(text: str) -> int:
    """Conservative token estimate for a mixed CJK/latin string."""
    cjk = 0
    for char in text:
        code = ord(char)
        if (
            0x3400 <= code <= 0x9FFF
            or 0xF900 <= code <= 0xFAFF
            or 0x3040 <= code <= 0x30FF
            or 0xAC00 <= code <= 0xD7AF
        ):
            cjk += 1
    other = len(text) - cjk
    return max(1, cjk + (other + 3) // 4)


@dataclass(frozen=True)
class ContextEntry:
    tier: str
    entry_key: str
    index_text: str
    content_hash: str
    tokens: int
    rank: int
    source: str = "conductor_pg"
    provenance: Mapping[str, Any] = field(default_factory=dict)
    confidence: float | None = None
    low_confidence: bool = False
    origin: str = "assistant_inference"
    evidence_count: int = 0
    counts_as_evidence: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)
    #: The ontology fields of the memory: the class it belongs to, and the
    #: concept id of the thing it is about. Both are None for a memory written
    #: before the ontology existed -- nothing is invented for it.
    memory_class: str | None = None
    concept_id: str | None = None
    #: Every concept id the memory declares, in the order it declares them. A
    #: memory may name more than one, and a memory that names none has none:
    #: an unnamed concept is not a concept, and recording one would make the
    #: audit claim a link the memory never made.
    concepts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # The primary concept is the first one the memory names, so a caller
        # that hands over the full list does not have to name the primary
        # again -- and the two can never disagree.
        if self.concept_id is None and self.concepts:
            object.__setattr__(self, "concept_id", self.concepts[0])

    def all_concepts(self) -> tuple[str, ...]:
        """Every concept this memory declares.

        ``concepts`` holds the full list and ``concept_id`` the primary one;
        a memory that declares a single concept reports it either way.
        """
        if self.concepts:
            return self.concepts
        return (self.concept_id,) if self.concept_id else ()

    def render(self) -> str:
        line = (
            f"- [{self.tier}]"
            f"{f'[{self.source}] ' if self.source != 'conductor_pg' else ' '}"
            f"{self.entry_key}: {self.index_text}"
        )
        if self.origin == "assistant_inference":
            line = f"{line} [inference; low confidence; not evidence]"
        else:
            line = f"{line} [origin: {self.origin}]"
        if self.low_confidence and self.origin != "assistant_inference":
            line = f"{line} [low confidence]"
        applies_when = self.metadata.get("applies_when")
        if applies_when:
            line = f"{line} [applies when: {applies_when}]"
        exceptions = self.metadata.get("exceptions")
        if exceptions:
            line = f"{line} [except: {exceptions}]"
        quote_ref = self.metadata.get("evidence_quote_ref")
        if isinstance(quote_ref, Mapping):
            line = (
                f"{line} [source: {quote_ref['message_id']} "
                f"{quote_ref['start']}:{quote_ref['end']}]"
            )
        return line

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "entry_key": self.entry_key,
            "index_text": self.index_text,
            "content_hash": self.content_hash,
            "tokens": self.tokens,
            "rank": self.rank,
            "source": self.source,
            "provenance": dict(self.provenance),
            "confidence": self.confidence,
            "low_confidence": self.low_confidence,
            "origin": self.origin,
            "evidence_count": self.evidence_count,
            "counts_as_evidence": self.counts_as_evidence,
            "metadata": dict(self.metadata),
            "class": self.memory_class,
            "concept_id": self.concept_id,
            "concepts": list(self.all_concepts()),
        }


@dataclass(frozen=True)
class MemoryContext:
    """What a turn injects, plus enough accounting to audit the decision."""

    query: str
    budget_tokens: int
    entries: tuple[ContextEntry, ...]
    dropped: int = 0
    per_tier: Mapping[str, int] = field(default_factory=dict)

    @property
    def tokens_used(self) -> int:
        return sum(entry.tokens for entry in self.entries)

    @property
    def keys(self) -> tuple[tuple[str, str], ...]:
        return tuple((entry.tier, entry.entry_key) for entry in self.entries)

    @property
    def concept_ids(self) -> tuple[str, ...]:
        """The concepts this selection is about, in the order they appear.

        Every concept a memory declares is here: a memory that names two
        things is about both, and an audit that reported only the first would
        answer "what did this Ticket know about?" with half the truth. A
        memory that names no concept contributes none: an unnamed concept is
        not a concept, and recording one would make the audit claim a link the
        memory never made.
        """
        seen: list[str] = []
        for entry in self.entries:
            for concept in entry.all_concepts():
                if concept and concept not in seen:
                    seen.append(concept)
        return tuple(seen)

    def audit_records(self) -> tuple[dict[str, Any], ...]:
        """Identity + provenance for the run contract; never the index text.

        ``tier:key`` alone collides across sources (an external memory can share a
        Conductor key), so the audit keeps the source and the provider provenance.
        The concept ids are the ontology's -- the things the memory is about --
        so an audit says *which concepts* the memory spoke for, not only which
        key it was filed under. A memory may name several, and the audit
        carries every one of them.
        """
        return tuple(
            {
                "tier": entry.tier,
                "key": entry.entry_key,
                "source": entry.source,
                "concept_id": entry.concept_id,
                "concepts": list(entry.all_concepts()),
                "provenance": dict(entry.provenance),
            }
            for entry in self.entries
        )

    def render_index_block(self) -> str:
        """The injected block: one line per entry, no bodies."""
        if not self.entries:
            return ""
        return "\n".join(entry.render() for entry in self.entries)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "budget_tokens": self.budget_tokens,
            "tokens_used": self.tokens_used,
            "dropped": self.dropped,
            "per_tier": dict(self.per_tier),
            "entries": [entry.to_dict() for entry in self.entries],
        }


async def _default_search(
    user_id: str,
    tier: str,
    query: str,
    limit: int,
    thread_id: str | None = None,
    workspace_id: str | None = None,
) -> Sequence[Mapping[str, Any]]:
    # Same retrieval contract as the inspector: lexical, plus optional API
    # embeddings when configured. Imported lazily to avoid a service cycle.
    from src.server.services.refactor.memory_embeddings import hybrid_search

    scoped_thread = thread_id if tier in {"session", "task"} else None
    scoped_workspace = workspace_id if tier in {"workspace", "project"} else None
    return await hybrid_search(
        user_id=user_id,
        query=query,
        tier=tier,
        thread_id=scoped_thread,
        workspace_id=scoped_workspace,
        limit=limit,
    )


def _row_concepts(row: Mapping[str, Any]) -> tuple[str, ...]:
    """Every concept a row declares, from the columns or from the metadata.

    The projection keeps the concepts in its own columns, but a federated
    source supplies them in the metadata alone -- the provider's own
    annotation of the memory it returned. Reading only the columns would
    drop the ontology of every federated memory, and the audit would then
    say that a memory which names a concept names none.
    """
    return row_concepts(row)


def _normalize_rows(rows: Sequence[Mapping[str, Any]], tier: str, limit: int) -> list[ContextEntry]:
    normalized: list[ContextEntry] = []
    for rank, row in enumerate(rows):
        if rank >= limit:
            break
        status = str(row.get("status") or "active")
        if status != "active":
            continue
        if not is_current(row.get("metadata") if isinstance(row, Mapping) else None):
            continue
        metadata_value = row.get("metadata")
        provenance_value = row.get("provenance")
        if metadata_value is not None and not isinstance(metadata_value, Mapping):
            continue
        if provenance_value is not None and not isinstance(provenance_value, Mapping):
            continue
        raw_metadata = metadata_value if isinstance(metadata_value, Mapping) else None
        metadata = normalize_memory_metadata(raw_metadata)
        policy_layers = [
            normalize_memory_policy_metadata_layers(value)
            for value in (metadata_value, provenance_value)
            if isinstance(value, Mapping)
        ]
        policy_metadata: dict[str, Any] = {"sensitivity": "private"}
        for layer in policy_layers:
            policy_metadata.update(layer)
        if any(layer["sensitivity"] == "secret" for layer in policy_layers):
            policy_metadata["sensitivity"] = "secret"
        if policy_metadata["sensitivity"] == "secret":
            continue
        key = str(row.get("entry_key") or "").strip()
        text = " ".join(str(row.get("index_text") or "").split())
        text = text[:MAX_INDEX_LINE_CHARS]
        if not key or not text:
            continue
        source = str(row.get("source") or "conductor_pg")
        concepts = _row_concepts(row)
        entry = ContextEntry(
            tier=tier,
            entry_key=key,
            index_text=text,
            content_hash=str(row.get("content_hash") or ""),
            tokens=0,
            rank=rank,
            source=source,
            provenance=(
                dict(row.get("provenance") or {})
                if isinstance(row.get("provenance") or {}, Mapping)
                else {}
            ),
            confidence=confidence_value(metadata),
            low_confidence=is_low_confidence(metadata),
            origin=metadata["origin"],
            evidence_count=metadata["evidence_count"],
            counts_as_evidence=metadata["counts_as_evidence"],
            metadata=policy_metadata,
            memory_class=row_class(row),
            concept_id=concepts[0] if concepts else None,
            concepts=concepts,
        )
        normalized.append(replace(entry, tokens=estimate_tokens(entry.render() + "\n")))
    return normalized


async def select_memory_context(
    *,
    user_id: str,
    query: str,
    budget_tokens: int,
    search: MemorySearch | None = None,
    per_tier_limit: int = 20,
    thread_id: str | None = None,
    workspace_id: str | None = None,
    row_filter: RowFilter | None = None,
) -> MemoryContext:
    """Select an index block under ``budget_tokens``.

    Pass ``search`` to inject a store (the eval uses an in-memory one); the
    default reads the Postgres projection. Flag gating stays with the caller —
    this function never touches config.
    """
    if budget_tokens < 1:
        raise ContractError("memory budget must be positive")
    if per_tier_limit < 1 or per_tier_limit > 100:
        raise ContractError("per-tier memory limit must be between 1 and 100")
    text = (query or "").strip()
    if not text:
        return MemoryContext(query="", budget_tokens=budget_tokens, entries=())

    searcher = search or _default_search
    candidates: dict[str, list[ContextEntry]] = {}
    for tier in TIER_ORDER:
        rows = await searcher(
            user_id, tier, text, per_tier_limit, thread_id, workspace_id
        )
        if row_filter is not None:
            rows = [row for row in rows if _row_passes(row_filter, tier, row)]
        candidates[tier] = _normalize_rows(rows, tier, per_tier_limit)

    selected: list[ContextEntry] = []
    seen_hashes: set[str] = set()
    spent = 0
    per_tier: dict[str, int] = {tier: 0 for tier in TIER_ORDER}
    leftovers: list[ContextEntry] = []

    def try_add(entry: ContextEntry, cap: int | None) -> bool:
        nonlocal spent
        if entry.content_hash and entry.content_hash in seen_hashes:
            return False
        if cap is not None and per_tier[entry.tier] + entry.tokens > cap:
            return False
        if spent + entry.tokens > budget_tokens:
            return False
        selected.append(entry)
        spent += entry.tokens
        per_tier[entry.tier] += entry.tokens
        if entry.content_hash:
            seen_hashes.add(entry.content_hash)
        return True

    # First pass: quota per tier, so no tier can crowd the others out.
    for tier in TIER_ORDER:
        quota = max(1, int(budget_tokens * TIER_WEIGHTS[tier]))
        for entry in candidates[tier]:
            if not try_add(entry, quota):
                leftovers.append(entry)
    # Second pass: spend remaining budget on more-specific workspace facts
    # before broader project guidance when per-tier quotas were too small.
    for tier in ("user", "workspace", "project", "session", "task"):
        for entry in leftovers:
            if entry.tier == tier and entry not in selected:
                try_add(entry, None)
    dropped = sum(len(rows) for rows in candidates.values()) - len(selected)
    return MemoryContext(
        query=text,
        budget_tokens=budget_tokens,
        entries=tuple(selected),
        dropped=dropped,
        per_tier={tier: used for tier, used in per_tier.items() if used},
    )


def _row_passes(row_filter: RowFilter, tier: str, row: Mapping[str, Any]) -> bool:
    try:
        return bool(row_filter(tier, row))
    except Exception:
        return False


def _entry_id(entry: ContextEntry) -> str:
    return f"{entry.tier}:{entry.entry_key}"


def selected_ids(context: MemoryContext) -> tuple[str, ...]:
    """Stable ``tier:key`` ids, for eval/audit comparisons."""
    return tuple(_entry_id(entry) for entry in context.entries)


def _ticket_audit_payload(
    *,
    ticket_id: str,
    context: MemoryContext,
    records: list[dict[str, Any]],
    concepts: list[str],
    chunk: int,
    chunks: int,
    audit_id: str = "",
) -> dict[str, Any]:
    """One chunk of the Ticket memory audit."""
    return {
        "ticket_id": ticket_id,
        # Shared by every chunk of one audit, so the chunks of two launches of
        # the same Ticket are not concatenated into one.
        "audit_id": audit_id,
        # The number of memories the Ticket was given in total. It is the
        # same in every chunk: the chunks are one audit, not several.
        "entries": len(context.entries),
        "tokens": context.tokens_used,
        "concepts": concepts,
        "audit": records,
        "chunk": chunk,
        "chunks": chunks,
    }


def _ticket_audit_payloads(
    *,
    ticket_id: str,
    context: MemoryContext,
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The Ticket memory audit, split into payloads that fit an event.

    An event carries at most a bounded payload, and a valid ontology can be
    large: a memory may name sixteen concepts and a concept id may be 256
    characters. Two such memories were 17,375 bytes against the 16,384-byte
    cap, so the whole audit was rejected and the Ticket kept no record of
    what it knew.

    The audit is split rather than truncated. Every concept id survives: the
    records carry their own concepts and the summary of a chunk names the
    concepts of that chunk, so the union over the chunks is the audit in
    full. ``chunk`` and ``chunks`` say how the audit is spread, and a reader
    concatenates the chunks in order.
    """
    import uuid

    from src.server.services.refactor import events as svc_events

    limit = svc_events.MAX_PAYLOAD_BYTES - _TICKET_AUDIT_ENVELOPE_BYTES
    audit_id = str(uuid.uuid4())
    chunks: list[dict[str, list[Any]]] = []

    def size_of(chunk_records: list[dict[str, Any]], chunk_concepts: list[str]) -> int:
        return svc_events.event_payload_bytes(
            _ticket_audit_payload(
                ticket_id=ticket_id,
                context=context,
                records=chunk_records,
                concepts=chunk_concepts,
                chunk=0,
                chunks=1,
                audit_id=audit_id,
            )
        )

    def fitted(record: dict[str, Any]) -> dict[str, Any]:
        # The provider provenance is the one unbounded part of a record. A
        # record too big for an event on its own keeps its identity and its
        # concepts and says that its provenance was left out, rather than
        # costing its whole chunk.
        if size_of([record], []) <= limit:
            return record
        return {**record, "provenance": {"omitted": "too_large"}}

    # The records first: a chunk takes as many as fit.
    current: list[dict[str, Any]] = []
    for record in map(fitted, records):
        candidate = current + [record]
        if current and size_of(candidate, []) > limit:
            chunks.append({"records": current, "concepts": []})
            current = [record]
        else:
            current = candidate
    chunks.append({"records": current, "concepts": []})

    # Then the concepts: a chunk takes as many as fit, and the ones that do
    # not fit anywhere get a chunk of their own.
    remaining = list(context.concept_ids)
    for chunk in chunks:
        while remaining and size_of(chunk["records"], chunk["concepts"] + [remaining[0]]) <= limit:
            chunk["concepts"].append(remaining.pop(0))
    while remaining:
        chunk = {"records": [], "concepts": []}
        while remaining and size_of(chunk["records"], chunk["concepts"] + [remaining[0]]) <= limit:
            chunk["concepts"].append(remaining.pop(0))
        if remaining:
            # A concept id that does not fit an empty chunk cannot be stored
            # at all. The ontology bounds a concept id to 256 characters, so
            # this is unreachable; the guard keeps the split from looping.
            logger.warning(
                "[refactor] ticket memory audit dropped a concept that does not fit "
                "an event payload ticket=%s",
                ticket_id,
            )
            remaining.pop(0)
        chunks.append(chunk)

    return [
        _ticket_audit_payload(
            ticket_id=ticket_id,
            context=context,
            records=chunk["records"],
            concepts=chunk["concepts"],
            chunk=index,
            chunks=len(chunks),
            audit_id=audit_id,
        )
        for index, chunk in enumerate(chunks)
    ]


async def record_ticket_memory_audit(
    *,
    user_id: str,
    ticket_id: str,
    context: MemoryContext,
    thread_id: str | None = None,
) -> dict[str, Any] | None:
    """Audit the memory a Ticket's Worker was given, by concept.

    A Ticket acts on what it knows, so the memory it was handed is part of the
    Ticket's record: the audit says which concepts the Worker read, which is
    what an operator needs to answer "what did this Ticket know about?". The
    concept id is the ontology's oid, and the entry identity and its provider
    provenance come with it, so an audit row names a memory as precisely as the
    turn-level audit does.

    The write is best-effort. A missing audit row must never fail a launch, so
    a failing write is logged and dropped, and an empty selection is not
    recorded at all: there was no memory to audit.

    A valid ontology can be bigger than one event payload, so the audit is
    split into as many events as it needs and every concept id is kept. One
    chunk failing to write costs that chunk, not the whole audit.
    """
    records = list(context.audit_records())
    if not records:
        return None
    try:
        from src.server.services.refactor import events as svc_events

        payloads = _ticket_audit_payloads(
            ticket_id=ticket_id,
            context=context,
            records=records[:_MAX_TICKET_AUDIT_RECORDS],
        )
    except Exception:
        logger.warning(
            "[refactor] ticket memory audit could not be prepared ticket=%s",
            ticket_id,
            exc_info=True,
        )
        return None
    written: dict[str, Any] | None = None
    for payload in payloads:
        try:
            event = await svc_events.record_event(
                user_id=user_id,
                event_type="memory.injected",
                payload=payload,
                thread_id=thread_id,
            )
        except Exception:
            logger.warning(
                "[refactor] ticket memory audit chunk write failed ticket=%s",
                ticket_id,
                exc_info=True,
            )
            continue
        if written is None:
            written = event
    return written


async def build_turn_memory_block(
    *,
    user_id: str,
    query: str,
    budget_tokens: int = DEFAULT_MEMORY_BUDGET,
    search: MemorySearch | None = None,
    thread_id: str | None = None,
    workspace_id: str | None = None,
    row_filter: RowFilter | None = None,
) -> tuple[MemoryContext, str] | None:
    """Phase-2 render switch: the selected block to send instead of the indices.

    ``None`` when the ``memory_index`` flag is off (caller keeps the legacy
    baseline). When on, always returns a block — possibly an empty string — so
    the caller can suppress the legacy per-tier indices instead of falling back
    to them by accident.
    """
    from src.server.services.refactor.memory_runtime_controls import (
        automatic_memory_enabled,
    )

    if not await automatic_memory_enabled(user_id):
        return (
            MemoryContext(
                query=(query or "").strip(),
                budget_tokens=budget_tokens,
                entries=(),
            ),
            "",
        )
    if not refactor_flag_enabled("memory_index"):
        return None
    if search is None:
        from src.config import settings

        try:
            binding = settings.get_memory_federation_binding(user_id)
        except Exception:
            logger.debug(
                "[refactor] memory federation config unavailable; using Conductor memory",
                exc_info=True,
            )
            binding = None
        if binding is not None:
            from src.server.services.refactor.memory_federation.integration import (
                configured_memory_search,
            )
            try:
                from src.config.features import system_flag
                from src.server.services.features import user_feature_enabled

                if system_flag("memory_federation").enabled and await user_feature_enabled(
                    user_id, "memory_federation"
                ):
                    search = await configured_memory_search(
                        user_id=user_id,
                        binding=binding,
                        workspace_id=workspace_id,
                        thread_id=thread_id,
                    )
            except Exception:
                logger.debug(
                    "[refactor] federated memory unavailable; using Conductor memory",
                    exc_info=True,
                )
    try:
        context = await select_memory_context(
            user_id=user_id,
            query=query,
            budget_tokens=budget_tokens,
            search=search,
            thread_id=thread_id,
            workspace_id=workspace_id,
            row_filter=row_filter,
        )
    except Exception:
        # Once the render switch is on, returning None would make callers fall
        # back to raw memory.md and bypass temporal/lifecycle filtering, so the
        # turn keeps an empty block instead. The drop is counted under a fixed
        # reason -- never the exception text, which may carry a query or store
        # detail -- because a selection that failed looks exactly like one that
        # found nothing.
        from src.server.services.refactor.memory_federation.health import (
            REASON_MEMORY_SELECTION_FAILED,
            record_memory_failure,
        )

        record_memory_failure(REASON_MEMORY_SELECTION_FAILED)
        logger.warning(
            "[refactor] memory selection failed; "
            "suppressing legacy index reason=%s",
            REASON_MEMORY_SELECTION_FAILED,
        )
        context = MemoryContext(query=(query or "").strip(), budget_tokens=budget_tokens, entries=())
    return context, context.render_index_block()
