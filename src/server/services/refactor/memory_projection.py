"""Pure mapping rules shared by the memory projection writers.

The projection is a function of the store: which store keys become rows, what
``index_text`` a row carries, and how content is hashed. Both the one-off
backfill job and the write-path dual write call these, so a rule cannot drift
between the repair path and the live path.

``memory.md`` is the index and is never a row: each pointer line
(``- [Title](file.md) — hook``) supplies the index text of the detail it names. A
detail without a pointer falls back to its frontmatter ``description``, else the
first meaningful body line, and is marked ``pointed: false``.
"""

from __future__ import annotations

import hashlib
import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

import yaml

from src.server.contracts.memory_concept import (
    MAX_CONCEPT_VALUE as _MAX_CONCEPT_VALUE,
    MAX_CONCEPT_VALUES as _MAX_CONCEPT_VALUES,
    concept_class as _concept_class,
    concept_values as _concept_values,
)
from src.server.contracts.memory_origin import normalize_memory_metadata
from src.server.contracts.memory_policy_metadata import (
    normalize_memory_policy_metadata_layers,
)
from src.server.contracts.memory_validity import is_current as is_current

MEMORY_INDEX_KEY = "memory.md"
#: Proposals are not memory until approved. Their files live under this prefix
#: (the approval path promotes them to the memory root); the projection, search
#: and context injection must never see them while they await a decision.
PROPOSED_PREFIX = "proposed/"
MAX_INDEX_TEXT = 500

#: Bound on one ontology value: a class name or a concept id.
MAX_CONCEPT_VALUE = _MAX_CONCEPT_VALUE

#: How many concepts one memory may name. The ontology allows a memory to be
#: about more than one thing; the projection's own column holds the first one,
#: and the full list lives in the metadata, which is what browsing and the
#: audit read.
MAX_CONCEPT_VALUES = _MAX_CONCEPT_VALUES

POINTER_RE = re.compile(
    r"^\s*-\s*\[(?P<title>(?:(?!\]\().)*?)\]\((?P<path>[^)\s]+)\)"
    r"\s*(?P<rest>(?!.*\]\().*)$"
)
#: Frontmatter may be empty (``---`` immediately followed by ``---``); the
#: optional newline before the closing delimiter accepts that shape as well as
#: the blank-line body YAML also treats as empty.
FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(?:(?P<body>.*?)\r?\n)?---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)
#: An opening delimiter on the first line. A document can match this without
#: matching FRONTMATTER_RE when the closing delimiter is missing; that shape is
#: malformed rather than plain body text, and it must fail closed.
FRONTMATTER_OPENER_RE = re.compile(r"\A---[ \t]*\r?\n")
#: A closing delimiter line (or the end of the text right after it).
_FRONTMATTER_CLOSE_RE = re.compile(r"---[ \t]*(?:\r?\n|\Z)")


def frontmatter_is_unterminated(content: str) -> bool:
    """An opening delimiter with no closing one is malformed, not body text.

    ``---\\n---\\nbody`` is a valid empty block: its closing delimiter is the
    line after the opener. Only a document where no closing delimiter line
    follows the opener is treated as malformed.
    """
    text = content or ""
    opener = FRONTMATTER_OPENER_RE.match(text)
    if opener is None or FRONTMATTER_RE.match(text):
        return False
    return _FRONTMATTER_CLOSE_RE.match(text, opener.end()) is None
DESCRIPTION_RE = re.compile(r"^description:\s*(?P<value>.+)$", re.MULTILINE)
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class IndexPointer:
    path: str
    title: str
    hook: str
    line: str


def normalize_key(key: str) -> str:
    while key.startswith("./"):
        key = key[2:]
    return key


def parse_memory_index(markdown: str) -> dict[str, IndexPointer]:
    pointers: dict[str, IndexPointer] = {}
    for raw in (markdown or "").splitlines():
        match = POINTER_RE.match(raw)
        if not match:
            continue
        path = normalize_key(match.group("path").strip())
        if not path or path.startswith(("http://", "https://", "#")):
            continue
        title = match.group("title").strip()
        rest = match.group("rest").strip().lstrip("—-–").strip()
        pointers[path] = IndexPointer(path=path, title=title, hook=rest, line=raw.strip())
    return pointers


def fallback_index_text(content: str) -> str:
    text = content or ""
    if frontmatter_is_unterminated(text):
        # An unterminated metadata block could be hiding a secret body; do not
        # turn its first line into recall text.
        return ""
    frontmatter = FRONTMATTER_RE.match(text)
    if frontmatter:
        if parse_memory_metadata(text).get("sensitivity") == "secret":
            # A secret detail contributes no index text at all, even when its
            # metadata carries a description.
            return ""
        description = DESCRIPTION_RE.search(frontmatter.group("body") or "")
        if description:
            return description.group("value").strip()[:MAX_INDEX_TEXT]
        # No description: scan the file body after the closing delimiter, not
        # the metadata block itself.
        text = text[frontmatter.end() :]
    for line in text.splitlines():
        cleaned = line.strip().lstrip("#").strip()
        if cleaned:
            return cleaned[:MAX_INDEX_TEXT]
    return ""


def _bounded_number(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return max(0.0, min(1.0, number))


def _string_list(value: Any, *, limit: int = 24) -> list[str]:
    if isinstance(value, str):
        values = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple)):
        values = [str(part).strip() for part in value]
    else:
        return []
    return [part for part in values if part][:limit]


def concept_values(value: Any) -> list[str]:
    """The concept ids a memory names, in the order it names them.

    A concept id is a short token (``project:conductor``). The list is
    deduplicated and bounded, and a value that is not a usable token is
    dropped rather than stored: an ontology field that names nothing must not
    become a concept of its own.
    """
    return _concept_values(value)


def concept_class(value: Any) -> str | None:
    """The class of a memory -- what kind of claim it is -- or ``None``."""
    return _concept_class(value)


def primary_concept(metadata: Mapping[str, Any] | None) -> str | None:
    """The concept the ``about`` column holds: the first one the memory names.

    A memory may name several concepts, and the projection's own ``about``
    column holds one of them -- the one the pair index leads with. The full
    list stays in the metadata, and everything that browses or audits a
    memory reads the list from there, so choosing the first here loses
    nothing: the second concept is still a concept of that memory.
    """
    values = (metadata or {}).get("about")
    if isinstance(values, str):
        text = values.strip()
        return text or None
    if isinstance(values, (list, tuple)):
        for item in values:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


def parse_memory_metadata(content: str) -> dict[str, Any]:
    """Read the optional structured frontmatter that shapes retrieval and review.

    The Markdown body stays the canonical memory; these fields are metadata
    around it (the same split mem0/Graphiti make between an extracted fact and
    its valid time). Unknown fields are ignored, malformed numbers clamp rather
    than refusing a memory write, and every field is optional so existing files
    keep working.
    """
    content_text = content or ""
    match = FRONTMATTER_RE.match(content_text)
    if not match:
        if frontmatter_is_unterminated(content_text):
            # Opening delimiter with no closing one: fail closed so a malformed
            # secret document is not reclassified as ordinary private memory.
            return {**normalize_memory_metadata({}), "sensitivity": "secret"}
        return normalize_memory_metadata({})
    try:
        raw = yaml.safe_load(match.group("body") or "") or {}
    except Exception:  # noqa: BLE001 - a malformed block falls back to description only
        return {**normalize_memory_metadata({}), "sensitivity": "secret"}
    if not isinstance(raw, Mapping):
        return {**normalize_memory_metadata({}), "sensitivity": "secret"}
    metadata: dict[str, Any] = {}
    kind = str(raw.get("kind") or "").strip()
    if kind:
        metadata["kind"] = kind[:64]
    # The ontology fields of the memory: what kind of claim it is, and the
    # concepts it is about. They are kept in the metadata as well as in their
    # own columns, because the columns are only the first concept while the
    # metadata holds every one of them.
    memory_class = concept_class(raw.get("class"))
    if memory_class is not None:
        metadata["class"] = memory_class
    concepts = concept_values(raw.get("about"))
    if concepts:
        metadata["about"] = concepts
    importance = _bounded_number(raw.get("importance"))
    if importance is not None:
        metadata["importance"] = importance
    confidence_raw = raw.get("confidence")
    if not isinstance(confidence_raw, bool):
        confidence = _bounded_number(confidence_raw)
        if confidence is not None:
            metadata["confidence"] = confidence
    for field in ("valid_from", "valid_until"):
        value = raw.get(field)
        text = str(value).strip() if value is not None else ""
        if _ISO_DATE.match(text):
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                metadata[field] = parsed.isoformat()
            except ValueError:
                pass
    lifecycle_raw = raw.get("lifecycle")
    if "lifecycle" in raw and lifecycle_raw is not None:
        # Keep unknown lifecycle states in the projection metadata. The shared
        # validity rule rejects every value except ``active``; dropping an
        # unfamiliar state here would make it look current by default. Preserve
        # whitespace too: baseline validity accepts only the exact string.
        metadata["lifecycle"] = str(lifecycle_raw)[:64]
    change_kind = str(raw.get("change_kind") or "").strip()
    if change_kind in ("long_term", "correction"):
        metadata["change_kind"] = change_kind
    for field in (
        "replaces_entry_key",
        "restores_entry_key",
        "reverted_to_entry_key",
    ):
        value = str(raw.get(field) or "").strip()
        if (
            value
            and len(value) <= 512
            and value not in (".", "..", MEMORY_INDEX_KEY)
            and "/" not in value
            and "\\" not in value
        ):
            metadata[field] = value
    for field in ("reason", "restore_reason", "lifecycle_reason"):
        value = str(raw.get(field) or "").strip()
        if value and len(value) <= 1000:
            metadata[field] = value
    tags = _string_list(raw.get("tags"))
    if tags:
        metadata["tags"] = tags
    entities = _string_list(raw.get("entities"))
    if entities:
        metadata["entities"] = entities
    supersedes = _string_list(raw.get("supersedes"))
    if supersedes:
        metadata["supersedes"] = supersedes
    supersedes_ids = _string_list(raw.get("supersedes_entry_ids"), limit=16)
    if supersedes_ids:
        normalized_ids: list[str] = []
        for value in supersedes_ids:
            try:
                normalized_ids.append(str(uuid.UUID(value)))
            except (ValueError, TypeError, AttributeError):
                normalized_ids = []
                break
        if normalized_ids:
            metadata["supersedes_entry_ids"] = normalized_ids
    source = str(raw.get("source") or "").strip()
    if source:
        metadata["source"] = source[:200]
    for field in (
        "source_entry_id",
        "restores_entry_id",
        "source_thread_id",
        "source_response_id",
        "source_task_run_id",
    ):
        source_id = raw.get(field)
        if source_id is None:
            continue
        try:
            metadata[field] = str(uuid.UUID(str(source_id)))
        except (ValueError, TypeError, AttributeError):
            pass
    origin = raw.get("origin")
    if origin is not None:
        metadata["origin"] = origin
    evidence_refs = raw.get("evidence_refs")
    if evidence_refs is not None:
        metadata["evidence_refs"] = evidence_refs
    normalized = normalize_memory_metadata(metadata)
    policy_metadata = normalize_memory_policy_metadata_layers(raw)
    return {**normalized, **policy_metadata}


def content_hash(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def pointer_index_text(pointer: IndexPointer) -> str:
    if pointer.title and pointer.hook:
        return f"{pointer.title} — {pointer.hook}"[:MAX_INDEX_TEXT]
    return (pointer.title or pointer.hook or pointer.line)[:MAX_INDEX_TEXT]


def find_pointer(
    pointer_map: Mapping[str, IndexPointer], key: str
) -> IndexPointer | None:
    pointer = pointer_map.get(key)
    if pointer is not None:
        return pointer
    # A pointer may name the detail relative to its own directory; match by a
    # unique basename in that case only.
    basename = key.rsplit("/", 1)[-1]
    matches = [p for p in pointer_map.values() if p.path.rsplit("/", 1)[-1] == basename]
    return matches[0] if len(matches) == 1 else None


def index_text_for(key: str, content: str, pointer_map: Mapping[str, IndexPointer]) -> str:
    pointer = find_pointer(pointer_map, key)
    return pointer_index_text(pointer) if pointer is not None else fallback_index_text(content)


def projectable_rows(
    *,
    user_id: str,
    tier: str,
    namespace: Sequence[str],
    items: Iterable[tuple[str, Mapping[str, Any]]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Projection rows for one store namespace, plus skipped-key reasons."""
    contents: dict[str, str] = {}
    skipped: list[str] = []
    for raw_key, value in items:
        key = normalize_key(str(raw_key))
        if key.startswith(PROPOSED_PREFIX):
            skipped.append(f"{key}: pending approval")
            continue
        content = value.get("content") if isinstance(value, Mapping) else None
        if not isinstance(content, str):
            skipped.append(f"{key}: non-string content")
            continue
        contents[key] = content
    pointer_map = parse_memory_index(contents.pop(MEMORY_INDEX_KEY, ""))

    rows: list[dict[str, Any]] = []
    for key in sorted(contents):
        content = contents[key]
        index_text = index_text_for(key, content, pointer_map)
        if not index_text:
            # Nothing can be projected for this key now. If an earlier version
            # of it was projected, that row must be deactivated rather than
            # left searchable; the backfill's apply path handles the marker.
            skipped.append(
                f"{key}: no pointer line or usable description; "
                "stale projection deactivated"
            )
            rows.append(
                {
                    "user_id": user_id,
                    "tier": tier,
                    "namespace": list(namespace),
                    "entry_key": key,
                    "content_hash": content_hash(content),
                    "size": len(content.encode("utf-8")),
                    "index_text": "",
                    "status": "superseded",
                    "metadata": parse_memory_metadata(content),
                    "source_entry_id": None,
                    "provenance": {
                        "source": "store_backfill",
                        "store_namespace": ".".join(str(part) for part in namespace),
                        "store_key": key,
                        "pointed": False,
                    },
                    "deactivate": True,
                }
            )
            continue
        metadata = parse_memory_metadata(content)
        rows.append(
            {
                "user_id": user_id,
                "tier": tier,
                "namespace": list(namespace),
                "entry_key": key,
                "content_hash": content_hash(content),
                "size": len(content.encode("utf-8")),
                "index_text": index_text,
                "status": (
                    "active"
                    if metadata.get("lifecycle") in (None, "active")
                    else "superseded"
                ),
                "metadata": metadata,
                "source_entry_id": metadata.get("source_entry_id"),
                "provenance": {
                    "source": "store_backfill",
                    "store_namespace": ".".join(str(part) for part in namespace),
                    "store_key": key,
                    "pointed": find_pointer(pointer_map, key) is not None,
                },
            }
        )
    return rows, skipped
