"""What long-term memory may ride along in a prompt that leaves the machine.

P0 privacy, 2026-11-14. Ticket Workers, the Router and reviewers are other
vendors' models (DeepSeek, Kimi, GLM, MiniMax, Grok, OpenAI). The memory
block appended to their prompts used to carry whatever recall returned,
including an entry titled ``[private]``, course memories and other projects'
memories. This module is the gate between recall and such a prompt. An entry
gets through only when

* it is not marked private. Text is NFKC-normalized and casefolded before
  matching, so fullwidth spellings count: ``[private]``, ``［private］``,
  ``<private>``, ``【私密】``, a ``private:``/``私密：`` prefix, ``#private``,
  ``[sensitive]`` and the like. Any metadata mapping, at any depth, that says
  ``sensitivity``/``visibility``/``privacy`` private (or an unreadable value),
  a truthy ``private``/``is_private``, or a private tag, keeps it out;
* it is not a course, notes or personal memory (``kind``/``category``);
* it is shareable: its normalized ``sensitivity`` is ``public``, or it is
  filed under this Ticket's own workspace or thread namespace. A declared
  project/repo the Ticket names replaces the lexical relevance check, but
  still needs ``public``. The repo's contract
  (``contracts/memory_policy_metadata.py``) reads a missing sensitivity as
  private, so an unlabelled memory without that scope stays out;
* it is relevant: scoped as above, or a public memory whose lexical overlap
  with the Ticket's terms reaches the thresholds (generic project words such
  as "memory" or "conductor" do not count). An entry that declares a
  *different* project or repo is dropped whatever its overlap;
* it is confident: an assistant inference is dropped unless the operator
  allows inferences, and the confidence (explicit, or 1.0 only for a memory
  backed by a verified user message) must reach the threshold;
* it fits the count and byte caps.

Fail closed: when relevance cannot be determined (no terms and no scope),
when the configuration is invalid, or when a check raises, nothing is
injected. Every knob is an environment variable. Drops are logged per
Ticket/turn as counts by reason, never with memory text.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

logger = logging.getLogger(__name__)

ENV_ENABLED = "CONDUCTOR_TICKET_MEMORY"
ENV_MIN_CONFIDENCE = "CONDUCTOR_TICKET_MEMORY_MIN_CONFIDENCE"
ENV_MIN_RELEVANCE = "CONDUCTOR_TICKET_MEMORY_MIN_RELEVANCE"
ENV_MIN_OVERLAP = "CONDUCTOR_TICKET_MEMORY_MIN_OVERLAP"
ENV_MAX_ENTRIES = "CONDUCTOR_TICKET_MEMORY_MAX_ENTRIES"
ENV_MAX_BYTES = "CONDUCTOR_TICKET_MEMORY_MAX_BYTES"
ENV_ALLOW_INFERENCE = "CONDUCTOR_TICKET_MEMORY_ALLOW_INFERENCE"

DEFAULT_MIN_CONFIDENCE = 0.7
DEFAULT_MIN_RELEVANCE = 0.3
DEFAULT_MIN_OVERLAP = 3
DEFAULT_MAX_ENTRIES = 8
DEFAULT_MAX_BYTES = 2048

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}

_PRIVATE_WORDS = (
    "private|personal|secret|confidential|sensitive|restricted|"
    "私密|隐私|私人|个人|机密|敏感|"
    "隱私|個人|機密"
)
#: Markers that make an entry private wherever they appear in its key or text,
#: matched on NFKC-normalized, casefolded text.
_PRIVATE_MARKER_RE = re.compile(
    r"[\[(<{【〔「『〈《]\s*(?:" + _PRIVATE_WORDS + r")\s*[\])>}】〕」』〉》]"
    r"|(?:^|[\s|/>\-:\[(])(?:" + _PRIVATE_WORDS + r")\s*:"
    r"|#(?:" + _PRIVATE_WORDS + r")(?![a-z0-9_])"
)
#: Metadata keys that carry a privacy decision, and the values that mean "keep out".
_PRIVACY_FIELDS = frozenset({"sensitivity", "visibility", "privacy", "access", "scope_visibility"})
_PRIVATE_FLAG_FIELDS = frozenset({"private", "is_private", "personal", "is_personal"})
_PRIVATE_VALUES = frozenset(
    {
        "private",
        "personal",
        "secret",
        "confidential",
        "restricted",
        "sensitive",
        "owner_only",
        "self",
        "internal",
    }
)
_PRIVATE_TAGS = _PRIVATE_VALUES | {
    "pii", "私密", "隐私", "私人", "个人", "机密", "敏感", "隱私", "個人", "機密",
}
#: Memory kinds that never belong in a work prompt.
_KIND_FIELDS = frozenset({"kind", "category", "memory_kind", "memory_category", "domain"})
_BLOCKED_KINDS = frozenset(
    {
        "course",
        "courses",
        "course_notes",
        "course-notes",
        "coursework",
        "notes",
        "note",
        "study",
        "personal",
        "课程",
        "笔记",
        "个人",
    }
)
#: Metadata keys that name the project or repository a memory is about.
_PROJECT_FIELDS = frozenset(
    {"project", "project_id", "repo", "repository", "repo_name", "workspace", "workspace_id"}
)
_TAG_FIELDS = frozenset({"tags", "labels"})
_MAX_WALK = 256

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_.\-]*[a-z0-9]|[a-z0-9]")
_CJK_RE = re.compile(r"[㐀-鿿豈-﫿]+")
_STOPWORDS = frozenset(
    """
    a an and are as at be been but by can do does for from has have how if in
    into is it its may must no not of on or our should so than that the their
    them then there these this those to too use used uses using via was we were
    what when where which while who why will with without you your
    add adds added fix fixes fixed make makes new old one two all any each every
    task tasks work works run runs file files code change changes update updates
    only also more less
    memory memories conductor agent agents dispatch dispatches ticket tickets
    line lines review reviews reviewer reviewers test tests worker workers
    """.split()
)

#: Drop reasons, for the per-Ticket counts.
REASON_PRIVATE = "private"
REASON_KIND = "kind"
REASON_OTHER_PROJECT = "other_project"
REASON_NOT_PUBLIC = "not_public"
REASON_IRRELEVANT = "irrelevant"
REASON_ERROR = "error"
REASON_CONFIDENCE = "confidence"
REASON_CAP = "cap"


class TicketMemoryConfigError(ValueError):
    """An environment value that the policy cannot use safely."""


@dataclass(frozen=True)
class TicketMemoryPolicy:
    enabled: bool = True
    min_confidence: float = DEFAULT_MIN_CONFIDENCE
    min_relevance: float = DEFAULT_MIN_RELEVANCE
    min_overlap: int = DEFAULT_MIN_OVERLAP
    max_entries: int = DEFAULT_MAX_ENTRIES
    max_bytes: int = DEFAULT_MAX_BYTES
    allow_inference: bool = False

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "TicketMemoryPolicy":
        """Read the knobs; an unusable value raises (the caller injects nothing)."""
        source = os.environ if environ is None else environ

        def flag(name: str, default: bool) -> bool:
            raw = str(source.get(name, "")).strip().lower()
            if not raw:
                return default
            if raw in _TRUE:
                return True
            if raw in _FALSE:
                return False
            raise TicketMemoryConfigError(f"{name} must be on or off")

        def number(name: str, default: float, low: float, high: float) -> float:
            raw = str(source.get(name, "")).strip()
            if not raw:
                return default
            try:
                value = float(raw)
            except ValueError:
                raise TicketMemoryConfigError(f"{name} must be a number") from None
            if not low <= value <= high:
                raise TicketMemoryConfigError(f"{name} must be between {low} and {high}")
            return value

        return cls(
            enabled=flag(ENV_ENABLED, True),
            min_confidence=number(ENV_MIN_CONFIDENCE, DEFAULT_MIN_CONFIDENCE, 0.0, 1.0),
            min_relevance=number(ENV_MIN_RELEVANCE, DEFAULT_MIN_RELEVANCE, 0.0, 1.0),
            min_overlap=int(number(ENV_MIN_OVERLAP, DEFAULT_MIN_OVERLAP, 1, 64)),
            max_entries=int(number(ENV_MAX_ENTRIES, DEFAULT_MAX_ENTRIES, 0, 64)),
            max_bytes=int(number(ENV_MAX_BYTES, DEFAULT_MAX_BYTES, 0, 65536)),
            allow_inference=flag(ENV_ALLOW_INFERENCE, False),
        )


@dataclass(frozen=True)
class TicketScope:
    """What a Ticket (or turn) is about, for the relevance check."""

    terms: frozenset[str]
    workspace_id: str | None = None
    thread_ids: frozenset[str] = frozenset()
    projects: frozenset[str] = frozenset()

    @property
    def determinable(self) -> bool:
        return bool(self.terms or self.workspace_id or self.thread_ids)


def normalize_text(text: Any) -> str:
    """NFKC, format characters (Unicode Cf: zero-width and the like) removed, casefolded.

    Fullwidth and compatibility spellings match their plain form, and a
    zero-width joiner inside ``[pri\u200bvate]`` cannot hide the marker.
    """
    folded = unicodedata.normalize("NFKC", str(text or ""))
    return "".join(ch for ch in folded if unicodedata.category(ch) != "Cf").casefold()


def terms_of(text: str) -> frozenset[str]:
    """Distinctive terms: latin words (3+ chars, no stopwords) and CJK bigrams."""
    lowered = normalize_text(text)
    terms: set[str] = set()
    for match in _WORD_RE.finditer(lowered):
        word = match.group(0).strip("._-")
        if len(word) >= 3 and word not in _STOPWORDS:
            terms.add(word)
        # "memory_context.py" also speaks for its parts; a generic part
        # ("memory") is still a stopword.
        for part in re.split(r"[._\-]", word):
            if len(part) >= 3 and part not in _STOPWORDS:
                terms.add(part)
    for run in _CJK_RE.findall(lowered):
        if len(run) == 1:
            continue
        terms.update(run[i : i + 2] for i in range(len(run) - 1))
    return frozenset(terms)


def _norm_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9㐀-鿿]+", "", normalize_text(value))


def _project_name(value: str) -> str:
    return _norm_name(value.strip().rstrip("/").split("/")[-1].removesuffix(".git"))


def ticket_scope(ticket: Mapping[str, Any]) -> TicketScope:
    mandate = ticket.get("mandate") if isinstance(ticket.get("mandate"), Mapping) else {}
    texts = [str(ticket.get("title") or ""), str(ticket.get("scope") or "")]
    projects: set[str] = set()
    for source in (ticket, mandate):
        for key in ("project", "project_name", "repo", "repository", "repo_name", "workspace_name"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                texts.append(value)
                if _project_name(value):
                    projects.add(_project_name(value))
    workspace = str(ticket.get("workspace_id") or "").strip() or None
    if workspace:
        projects.add(_norm_name(workspace))
    threads = {
        str(ticket.get(key)).strip()
        for key in ("worker_thread_id", "thread_id")
        if str(ticket.get(key) or "").strip()
    }
    return TicketScope(
        terms=terms_of("\n".join(texts)),
        workspace_id=workspace,
        thread_ids=frozenset(threads),
        projects=frozenset(projects),
    )


def turn_scope(*, query: str, workspace_id: str | None, thread_id: str | None) -> TicketScope:
    """The scope of one chat turn (Router or reviewer lane): its text and its place."""
    workspace = str(workspace_id or "").strip() or None
    return TicketScope(
        terms=terms_of(query),
        workspace_id=workspace,
        thread_ids=frozenset({str(thread_id)} if thread_id else set()),
        projects=frozenset({_norm_name(workspace)} if workspace else set()),
    )


def _mappings(row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Every mapping of a row (top level, metadata, provenance), at any depth (bounded)."""
    # The row's own top-level fields count too: a federated source may put
    # ``visibility`` or ``tags`` on the row itself.
    top = {key: value for key, value in row.items() if key not in {"metadata", "provenance"}}
    found: list[Mapping[str, Any]] = []
    pending: list[Any] = [row.get("metadata"), row.get("provenance"), top]
    seen: set[int] = set()
    visited = 0
    while pending and visited < _MAX_WALK:
        value = pending.pop()
        visited += 1
        if id(value) in seen:
            continue
        seen.add(id(value))
        if isinstance(value, Mapping):
            found.append(value)
            pending.extend(value.values())
        elif isinstance(value, (list, tuple)):
            pending.extend(value)
    return found


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield from (part.strip() for part in re.split(r"[,;]", value))
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            if isinstance(item, str):
                yield item.strip()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return normalize_text(value).strip() in _TRUE
    return value is not None


def text_is_private(text: Any) -> bool:
    return bool(_PRIVATE_MARKER_RE.search(normalize_text(text)))


def row_is_private(row: Mapping[str, Any]) -> bool:
    for name in ("entry_key", "index_text", "title"):
        if text_is_private(row.get(name)):
            return True
    for layer in _mappings(row):
        for key, value in layer.items():
            name = normalize_text(key)
            if name in _PRIVACY_FIELDS:
                if value is None:
                    continue
                if not isinstance(value, str):
                    return True  # unreadable privacy decision: keep it out
                if normalize_text(value).strip() in _PRIVATE_VALUES:
                    return True
            elif name in _PRIVATE_FLAG_FIELDS and _truthy(value):
                return True
            elif name in _TAG_FIELDS:
                for tag in _strings(value):
                    if normalize_text(tag).lstrip("#").strip() in _PRIVATE_TAGS:
                        return True
    return False


def row_kind_blocked(row: Mapping[str, Any]) -> bool:
    for layer in _mappings(row):
        for key, value in layer.items():
            if normalize_text(key) in _KIND_FIELDS:
                for kind in _strings(value):
                    if normalize_text(kind).strip() in _BLOCKED_KINDS:
                        return True
    return False


def row_is_public(row: Mapping[str, Any]) -> bool:
    """Missing sensitivity is private (the repo contract): every stated one must be public."""
    stated = [
        value
        for layer in _mappings(row)
        for key, value in layer.items()
        if normalize_text(key) == "sensitivity"
    ]
    return bool(stated) and all(
        isinstance(value, str) and normalize_text(value).strip() == "public" for value in stated
    )


def _row_projects(row: Mapping[str, Any]) -> set[str]:
    names: set[str] = set()
    for layer in _mappings(row):
        for key, value in layer.items():
            if normalize_text(key) in _PROJECT_FIELDS:
                for item in _strings(value):
                    if _project_name(item):
                        names.add(_project_name(item))
    return names


def _scoped_to_ticket(tier: str, row: Mapping[str, Any], scope: TicketScope) -> bool:
    """A Conductor row filed under this Ticket's own workspace or thread namespace."""
    namespace = row.get("namespace")
    if not isinstance(namespace, (list, tuple)) or len(namespace) < 3:
        return False
    owner_id = str(namespace[2] or "")
    if tier in {"session", "task"}:
        return owner_id in scope.thread_ids
    if tier in {"workspace", "project"}:
        return scope.workspace_id is not None and owner_id == scope.workspace_id
    return False


def relevance(row: Mapping[str, Any], scope: TicketScope) -> tuple[float, int]:
    """Share of the entry's terms the Ticket also names, and how many there are."""
    entry_terms = terms_of(f"{row.get('entry_key') or ''} {row.get('index_text') or ''}")
    if not entry_terms or not scope.terms:
        return 0.0, 0
    overlap = len(entry_terms & scope.terms)
    return overlap / len(entry_terms), overlap


def row_decision(
    tier: str, row: Mapping[str, Any], scope: TicketScope, policy: TicketMemoryPolicy
) -> str | None:
    """None to admit the raw recall row, else the drop reason."""
    if not isinstance(row, Mapping) or not scope.determinable:
        return REASON_IRRELEVANT
    if row_is_private(row):
        return REASON_PRIVATE
    if row_kind_blocked(row):
        return REASON_KIND
    if _scoped_to_ticket(tier, row, scope):
        return None
    declared = _row_projects(row)
    if declared:
        # A memory that says what project it is about speaks for that project
        # only. Declaring this Ticket's project is not enough on its own: a
        # missing sensitivity is private, so it must also say public, and
        # then the declared project stands in for the lexical relevance.
        if not declared & scope.projects:
            return REASON_OTHER_PROJECT
        return None if row_is_public(row) else REASON_NOT_PUBLIC
    if not row_is_public(row):
        return REASON_NOT_PUBLIC
    score, overlap = relevance(row, scope)
    if overlap >= policy.min_overlap and score >= policy.min_relevance:
        return None
    return REASON_IRRELEVANT


def row_admissible(
    tier: str, row: Mapping[str, Any], scope: TicketScope, policy: TicketMemoryPolicy
) -> bool:
    return row_decision(tier, row, scope, policy) is None


@dataclass
class RowGate:
    """A ``(tier, row) -> bool`` for ``select_memory_context`` that counts its drops."""

    scope: TicketScope
    policy: TicketMemoryPolicy
    dropped: Counter = field(default_factory=Counter)
    admitted: int = 0

    def __call__(self, tier: str, row: Mapping[str, Any]) -> bool:
        try:
            reason = row_decision(tier, row, self.scope, self.policy)
        except Exception:
            logger.debug("[memory-gate] row check failed; dropping row", exc_info=True)
            reason = REASON_ERROR
        if reason is None:
            self.admitted += 1
            return True
        self.dropped[reason] += 1
        return False


def make_row_filter(scope: TicketScope, policy: TicketMemoryPolicy) -> RowGate:
    return RowGate(scope, policy)


def _effective_confidence(entry: Any) -> float:
    if entry.confidence is not None:
        return float(entry.confidence)
    # No stated confidence: only a memory corroborated by a verified user
    # message counts as certain. Anything else is unknown, which is not enough.
    return 1.0 if entry.counts_as_evidence else 0.0


def entry_admissible(entry: Any, policy: TicketMemoryPolicy) -> bool:
    if entry.origin == "assistant_inference" and not policy.allow_inference:
        return False
    if entry.low_confidence:
        return False
    return _effective_confidence(entry) >= policy.min_confidence


def apply_entry_policy(context: Any, policy: TicketMemoryPolicy, gate: RowGate | None = None) -> Any:
    """Confidence, privacy re-check, count and byte caps over a selected ``MemoryContext``."""
    kept: list[Any] = []
    used = 0
    for entry in context.entries:
        reason = None
        if len(kept) >= policy.max_entries:
            reason = REASON_CAP
        elif text_is_private(entry.entry_key) or text_is_private(entry.index_text):
            reason = REASON_PRIVATE
        elif not entry_admissible(entry, policy):
            reason = REASON_CONFIDENCE
        else:
            size = len(entry.render().encode("utf-8")) + 1
            if used + size > policy.max_bytes:
                reason = REASON_CAP
            else:
                kept.append(entry)
                used += size
        if reason is not None and gate is not None:
            gate.dropped[reason] += 1
    dropped = context.dropped + len(context.entries) - len(kept)
    per_tier: dict[str, int] = {}
    for entry in kept:
        per_tier[entry.tier] = per_tier.get(entry.tier, 0) + entry.tokens
    return replace(context, entries=tuple(kept), dropped=dropped, per_tier=per_tier)


def log_gate(label: str, ident: str, gate: RowGate, kept: int) -> None:
    """Counts only: how many memories the gate kept and dropped, by reason."""
    reasons = " ".join(f"{name}={count}" for name, count in sorted(gate.dropped.items()))
    logger.info(
        "[memory-gate] %s=%s kept=%d dropped=%d %s",
        label,
        ident,
        kept,
        sum(gate.dropped.values()),
        reasons,
    )


async def gated_turn_memory_block(
    *,
    user_id: str,
    query: str,
    scope: TicketScope,
    thread_id: str | None,
    workspace_id: str | None,
    label: str,
    ident: str,
) -> tuple[Any, str] | None:
    """``build_turn_memory_block`` behind this gate, for any prompt that leaves the machine.

    Returns what ``build_turn_memory_block`` returns (None when the render
    switch is off), with the context and block already gated. An invalid
    policy, a disabled policy or an undeterminable scope yields an empty block.
    """
    from src.server.services.refactor.memory_context import (
        MemoryContext,
        build_turn_memory_block,
    )

    empty = (MemoryContext(query=(query or "").strip(), budget_tokens=1, entries=()), "")
    try:
        policy = TicketMemoryPolicy.from_env()
    except TicketMemoryConfigError:
        logger.warning("[memory-gate] policy is invalid; injecting no memory %s=%s", label, ident)
        return empty
    if not policy.enabled or policy.max_entries < 1 or not scope.determinable:
        return empty
    gate = make_row_filter(scope, policy)
    planned = await build_turn_memory_block(
        user_id=user_id,
        query=query,
        thread_id=thread_id,
        workspace_id=workspace_id,
        row_filter=gate,
    )
    if planned is None:
        return None
    context, _unfiltered = planned
    try:
        context = apply_entry_policy(context, policy, gate)
    except Exception:
        logger.debug("[memory-gate] entry policy failed; injecting no memory", exc_info=True)
        return empty
    log_gate(label, ident, gate, len(context.entries))
    return context, context.render_index_block()
