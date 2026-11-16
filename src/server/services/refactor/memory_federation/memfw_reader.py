"""Read a MemFW vault without importing or constructing its mutable Repository."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import quote

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.server.contracts.memory_concept import concept_ontology

from .models import MemoryEntry, MemoryHit, MemoryProposal
from .provider import memory_hit_is_recallable

_GENESIS = "0" * 64
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_MAX_QUERY_CHARS = 512
_MAX_EVENT_BYTES = 32 * 1024 * 1024
_MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
_MAX_SIDECAR_BYTES = 1024 * 1024
_MAX_INDEX_CHARS = 500
#: How many records one local vault read may return. This is the *local*
#: reader's own bound on a vault scan, not the ``memfw-http/2`` limit: the
#: contract caps the ``limit`` of the HTTP ``/search`` and ``/related``
#: endpoints at 50, and the two clients enforce that cap in ``remote.py``
#: and in ``scripts/memfw_notes_mcp.py``.
_MAX_LOCAL_RESULTS = 100
_DEFAULT_READ_TIMEOUT_SECONDS = 1.0

#: A bounded pool keeps a slow vault scan off the event loop's shared default
#: executor, so repeated turns cannot starve unrelated ``to_thread`` work. A read
#: still checks its deadline between records and stops even when the awaiting
#: coroutine was already cancelled by the federation timeout.
_READ_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="memfw-read")

_T = TypeVar("_T")


def _read_bounded(path: Path, limit: int, *, label: str) -> bytes:
    """Read at most ``limit`` bytes, refusing the file before loading it whole."""
    try:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError as exc:
        raise MemfwReadError(f"cannot read {label}") from exc
    if len(data) > limit:
        raise MemfwReadError(f"{label} exceeds the configured read bound")
    return data


def _read_text_bounded(path: Path, *, label: str) -> str:
    try:
        return _read_bounded(path, _MAX_SIDECAR_BYTES, label=label).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MemfwReadError(f"{label} is not valid UTF-8") from exc


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() > deadline:
        raise MemfwReadError("vault read exceeded its time budget")


async def _run_read(
    function: Callable[..., _T], /, *args: Any, **kwargs: Any
) -> _T:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _READ_EXECUTOR, functools.partial(function, *args, **kwargs)
    )


def _one_line(value: str) -> str:
    return " ".join(value.split())


def _index_line(metadata: dict[str, Any], fallback: str) -> str:
    """A compact one-line index entry, never the document body.

    MemFW documents carry a title and a summary beside the body; the injected
    block is an index, so a multiline body here would both consume the budget and
    let a document forge extra prompt entries.
    """
    title = _one_line(str(metadata.get("title") or ""))
    summary = _one_line(str(metadata.get("summary") or ""))
    if title and summary and summary != title:
        line = f"{title}: {summary}"
    else:
        line = summary or title
    return (line or fallback)[:_MAX_INDEX_CHARS]


_EVENT_KINDS = frozenset(
    {
        "create",
        "update",
        "delete",
        "layer_change",
        "tier_change",
        "anchor_set",
        "agent_proposal",
        "human_decision",
        "simulated_decision",
        "external_edit",
        "external_delete",
        "memory_restore",
        "capture_wake",
        "capture_sleep",
        "capture_state_change",
        "capture_record",
        "forget",
        "memory_access",
        "consolidation_proposal",
        "config_change",
        "budget_unsatisfiable",
        "proposal_erase",
        "knowledge_card_create",
        "knowledge_source_missing",
        "knowledge_source_repoint",
    }
)
_ROW_COLUMNS = (
    "id",
    "title",
    "body",
    "search_body",
    "layer",
    "tier",
    "source",
    "provenance",
    "anchor",
    "tags",
    "version",
    "summary",
    "is_stub",
    "archive_ref",
    "archived_hash",
    "created_at",
    "updated_at",
)


class MemfwReadError(ValueError):
    """The configured vault cannot be read safely or verified."""


class _Event(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    kind: str
    memory_id: str
    timestamp: datetime
    actor: str
    before_hash: str | None = None
    after_hash: str | None = None
    version_ref: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    prev_event_hash: str = _GENESIS
    event_hash: str

    @field_validator("timestamp")
    @classmethod
    def timestamp_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must include a timezone")
        return value.astimezone(UTC)


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _unique_mapping(
    loader: yaml.SafeLoader, node: yaml.nodes.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    values: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in values:
            raise MemfwReadError(f"duplicate Markdown frontmatter key: {key}")
        values[key] = loader.construct_object(value_node, deep=deep)
    return values


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping
)


def _digest_event(event: _Event) -> str:
    payload = event.model_dump(mode="json", exclude={"event_hash"})
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _safe_path(root: Path, relative: str, *, required: bool = True) -> Path:
    path = root
    for part in Path(relative).parts:
        if part in {"", ".", ".."}:
            raise MemfwReadError("invalid vault-relative path")
        path = path / part
        try:
            if path.is_symlink():
                raise MemfwReadError(f"symlink in vault path: {relative}")
        except OSError as exc:
            raise MemfwReadError(f"cannot inspect vault path: {relative}") from exc
    try:
        resolved = path.resolve(strict=required)
    except FileNotFoundError:
        if required:
            raise MemfwReadError(f"missing vault file: {relative}") from None
        return path
    except OSError as exc:
        raise MemfwReadError(f"cannot resolve vault path: {relative}") from exc
    if not resolved.is_relative_to(root):
        raise MemfwReadError(f"vault path escapes configured root: {relative}")
    return resolved


def _parse_document(raw: bytes, expected_id: str) -> dict[str, Any]:
    try:
        markdown = raw.decode("utf-8").replace("\r\n", "\n")
        if not markdown.startswith("---\n"):
            raise MemfwReadError("Markdown is missing YAML frontmatter")
        _, frontmatter, body = markdown.split("---\n", 2)
        metadata = yaml.load(frontmatter, Loader=_UniqueKeyLoader)
    except (UnicodeDecodeError, ValueError, yaml.YAMLError) as exc:
        if isinstance(exc, MemfwReadError):
            raise
        raise MemfwReadError(f"invalid Markdown document {expected_id}") from exc
    if not isinstance(metadata, dict) or metadata.get("id") != expected_id:
        raise MemfwReadError(f"Markdown id does not match filename: {expected_id}")
    metadata["body"] = body.lstrip("\n").removesuffix("\n")
    for key in ("title", "layer", "tier", "source", "provenance", "body"):
        if not isinstance(metadata.get(key), str):
            raise MemfwReadError(f"invalid Markdown field {key}: {expected_id}")
    if metadata["layer"] not in {"current", "consolidated", "anchor"}:
        raise MemfwReadError(f"invalid memory layer: {expected_id}")
    if metadata["tier"] not in {"hot", "cold"}:
        raise MemfwReadError(f"invalid memory tier: {expected_id}")
    tags = metadata.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise MemfwReadError(f"invalid memory tags: {expected_id}")
    return metadata


class MemfwReader:
    """Read verified active memories from an existing MemFW vault.

    The legacy ``Repository`` constructor binds the mutable API and its lock paths;
    its ``init`` and recovery methods can create files. This adapter deliberately
    ports only its read contract and uses immutable SQLite connections.
    """

    def __init__(self, root: Path | str) -> None:
        try:
            configured = Path(root).expanduser()
            if configured.is_symlink():
                raise MemfwReadError("vault root cannot be a symlink")
            self.root = configured.resolve(strict=True)
        except FileNotFoundError:
            raise MemfwReadError("vault root does not exist") from None
        except OSError as exc:
            raise MemfwReadError("cannot resolve vault root") from exc
        if not self.root.is_dir():
            raise MemfwReadError("vault root must be a directory")

    def _events(self, *, deadline: float | None = None) -> list[_Event]:
        if (
            _safe_path(self.root, ".pending.json", required=False).exists()
            or _safe_path(
                self.root, ".event-append.pending.json", required=False
            ).exists()
        ):
            raise MemfwReadError(
                "vault has an unfinished write; refusing a partial view"
            )
        catalog = _safe_path(self.root, ".log/catalog.json", required=False)
        if catalog.exists():
            try:
                catalog_value = json.loads(
                    _read_text_bounded(catalog, label="event archive catalog")
                )
            except (OSError, ValueError) as exc:
                raise MemfwReadError("event archive catalog is invalid") from exc
            if not isinstance(catalog_value, dict) or not isinstance(
                catalog_value.get("segments"), list
            ):
                raise MemfwReadError("event archive catalog is malformed")
            if catalog_value.get("segments"):
                raise MemfwReadError(
                    "compacted event logs are not supported by this reader"
                )

        event_path = _safe_path(self.root, "events.jsonl", required=False)
        raw = (
            _read_bounded(event_path, _MAX_EVENT_BYTES, label="event log")
            if event_path.exists()
            else b""
        )
        if raw and not raw.endswith(b"\n"):
            raise MemfwReadError("event log must end with LF")
        if b"\r" in raw:
            raise MemfwReadError("event log must use LF line endings")
        events: list[_Event] = []
        previous = _GENESIS
        _check_deadline(deadline)
        for line_number, raw_line in enumerate(
            raw[:-1].split(b"\n") if raw else [], start=1
        ):
            if line_number % 256 == 0:
                _check_deadline(deadline)
            try:
                line = raw_line.decode("utf-8")
                event = _Event.model_validate_json(line)
            except Exception as exc:
                raise MemfwReadError(f"invalid event at line {line_number}") from exc
            if event.kind not in _EVENT_KINDS:
                raise MemfwReadError(f"unsupported event kind at line {line_number}")
            if event.kind == "human_decision" and event.actor != "human":
                raise MemfwReadError(
                    f"human decision has a non-human actor at line {line_number}"
                )
            if (
                event.kind == "simulated_decision"
                and event.actor != "simulated_approver"
            ):
                raise MemfwReadError(
                    f"simulated decision has an invalid actor at line {line_number}"
                )
            if line != event.model_dump_json():
                raise MemfwReadError(f"non-canonical event at line {line_number}")
            if (
                event.prev_event_hash != previous
                or _digest_event(event) != event.event_hash
            ):
                raise MemfwReadError(f"broken event chain at line {line_number}")
            if event.after_hash is not None and event.version_ref != event.after_hash:
                raise MemfwReadError(
                    f"event version reference mismatch at line {line_number}"
                )
            if event.before_hash is not None and not _HASH_RE.fullmatch(
                event.before_hash
            ):
                raise MemfwReadError(f"invalid before hash at line {line_number}")
            if event.after_hash is not None and not _HASH_RE.fullmatch(
                event.after_hash
            ):
                raise MemfwReadError(f"invalid after hash at line {line_number}")
            previous = event.event_hash
            events.append(event)

        expected_head = previous
        state_path = _safe_path(self.root, "events.state.json", required=False)
        if state_path.exists():
            try:
                state = json.loads(
                    _read_text_bounded(state_path, label="event state")
                )
                if not isinstance(state, dict):
                    raise MemfwReadError("event state is malformed")
                if state.get("last_hash") != expected_head or state.get(
                    "event_count"
                ) != len(events):
                    raise MemfwReadError("event state does not match event log")
                if state.get("offset") != len(raw):
                    raise MemfwReadError("event state offset does not match event log")
                cursor_path = _safe_path(
                    self.root, "events.cursor.json", required=False
                )
                if cursor_path.exists():
                    cursor = json.loads(
                        _read_text_bounded(cursor_path, label="legacy event cursor")
                    )
                    for key in ("catalog_hash", "offset", "event_count", "last_hash"):
                        if cursor.get(key) != state.get(key):
                            raise MemfwReadError(
                                "legacy cursor conflicts with event state"
                            )
            except (OSError, ValueError, TypeError) as exc:
                if isinstance(exc, MemfwReadError):
                    raise
                raise MemfwReadError("event state is invalid") from exc
        head_path = _safe_path(self.root, "events.head", required=False)
        if (
            head_path.exists()
            and _read_text_bounded(head_path, label="event head").strip()
            != expected_head
        ):
            raise MemfwReadError("event head does not match event log")
        cursor_path = _safe_path(self.root, "events.cursor.json", required=False)
        if cursor_path.exists():
            try:
                cursor = json.loads(
                    _read_text_bounded(cursor_path, label="legacy event cursor")
                )
                if any(
                    cursor.get(key) != value
                    for key, value in (
                        ("offset", len(raw)),
                        ("event_count", len(events)),
                        ("last_hash", expected_head),
                    )
                ):
                    raise MemfwReadError("legacy cursor does not match event log")
            except (OSError, ValueError, TypeError) as exc:
                if isinstance(exc, MemfwReadError):
                    raise
                raise MemfwReadError("legacy event cursor is invalid") from exc
        if events and not (state_path.exists() or head_path.exists()):
            raise MemfwReadError("event log has no committed head")
        return events

    @staticmethod
    def _active_hashes(events: list[_Event]) -> dict[str, str | None]:
        active: dict[str, str | None] = {}
        # ``forget`` is metadata-only in MemFW's own replay projection: it records
        # a soft cool/archive decision (the body stays in a cold stub and
        # ``memory_restore`` reverses it). A hard forget deletes the record and
        # emits ``delete``, handled below. Do not treat ``forget`` as a tombstone.
        metadata_only = {
            "config_change",
            "agent_proposal",
            "human_decision",
            "simulated_decision",
            "capture_wake",
            "capture_sleep",
            "capture_state_change",
            "capture_record",
            "forget",
            "memory_access",
            "consolidation_proposal",
            "budget_unsatisfiable",
            "proposal_erase",
            "knowledge_card_create",
            "knowledge_source_missing",
            "knowledge_source_repoint",
        }
        for event in events:
            if event.kind in metadata_only:
                continue
            if event.kind in {"delete", "external_delete"} or (
                event.kind == "external_edit"
                and event.details.get("quarantined") is True
            ):
                active[event.memory_id] = None
            elif event.after_hash is not None:
                active[event.memory_id] = event.after_hash
            elif event.memory_id not in active:
                active[event.memory_id] = None
        return active

    def _connection(self) -> sqlite3.Connection:
        index = _safe_path(self.root, "index.sqlite3")
        for sidecar in ("index.sqlite3-wal", "index.sqlite3-shm"):
            if _safe_path(self.root, sidecar, required=False).exists():
                raise MemfwReadError(
                    "SQLite index has WAL sidecars; immutable view may be stale"
                )
        uri = f"file:{quote(str(index), safe='/')}?mode=ro&immutable=1"
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(uri, uri=True)
            connection.execute("PRAGMA query_only=ON")
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if not {"memory_rows", "memory_fts"} <= tables:
                raise MemfwReadError("SQLite index is missing MemFW tables")
            columns = tuple(
                row[1] for row in connection.execute("PRAGMA table_info(memory_rows)")
            )
            if columns != _ROW_COLUMNS:
                raise MemfwReadError(
                    "SQLite index has an unsupported memory_rows schema"
                )
            return connection
        except (sqlite3.Error, OSError) as exc:
            if connection is not None:
                connection.close()
            raise MemfwReadError(
                "SQLite index cannot be opened as an immutable MemFW index"
            ) from exc
        except Exception:
            if connection is not None:
                connection.close()
            raise

    def _search(self, query: str, limit: int) -> list[tuple[str, float]]:
        needle = query.strip()
        if limit <= 0 or len(needle) < 2 or len(needle) > _MAX_QUERY_CHARS:
            return []
        connection = self._connection()
        try:
            if len(needle) == 2:
                rows = connection.execute(
                    "SELECT id,0.0 FROM memory_rows "
                    "WHERE instr(title, ?) OR instr(search_body, ?) OR instr(summary, ?) OR instr(tags, ?) "
                    "ORDER BY id LIMIT ?",
                    (needle, needle, needle, needle, limit),
                )
            else:
                schema_row = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE name='memory_fts'"
                ).fetchone()
                schema = str(schema_row[0]).lower() if schema_row else ""
                if "tokenize='trigram'" in schema or 'tokenize="trigram"' in schema:
                    match = '"' + needle.replace('"', '""') + '"'
                    rows = connection.execute(
                        "SELECT r.id,bm25(memory_fts) FROM memory_fts AS f "
                        "JOIN memory_rows AS r ON r.id=f.id "
                        "WHERE memory_fts MATCH ? ORDER BY bm25(memory_fts),r.id LIMIT ?",
                        (match, limit),
                    )
                else:
                    # MemFW's unicode61 fallback requires jieba for ranking. A literal
                    # substring scan keeps recall deterministic without importing it.
                    rows = connection.execute(
                        "SELECT id,0.0 FROM memory_rows "
                        "WHERE instr(title, ?) OR instr(search_body, ?) OR instr(summary, ?) OR instr(tags, ?) "
                        "ORDER BY id LIMIT ?",
                        (needle, needle, needle, needle, limit),
                    )
            return [(str(row[0]), float(row[1])) for row in rows]
        except sqlite3.Error as exc:
            raise MemfwReadError("SQLite FTS query failed") from exc
        finally:
            connection.close()

    def recall(
        self, query: str, limit: int = 20, *, deadline: float | None = None
    ) -> list[MemoryHit]:
        """Return hits whose index candidates still match active committed Markdown."""
        if limit <= 0 or not query.strip():
            return []
        if len(query) > _MAX_QUERY_CHARS:
            raise MemfwReadError("query exceeds the configured read bound")
        candidates = self._search(query, min(limit, _MAX_LOCAL_RESULTS))
        if not candidates:
            return []
        events = self._events(deadline=deadline)
        active = self._active_hashes(events)
        hits: list[MemoryHit] = []
        event_head = events[-1].event_hash if events else _GENESIS
        for memory_id, rank in candidates:
            _check_deadline(deadline)
            if not _ID_RE.fullmatch(memory_id):
                continue
            expected_hash = active.get(memory_id)
            if expected_hash is None:
                continue
            path = _safe_path(self.root, f"memories/{memory_id}.md", required=False)
            if not path.exists():
                continue
            raw = _read_bounded(
                path,
                _MAX_DOCUMENT_BYTES,
                label=f"memory document: {memory_id}",
            )
            content_hash = hashlib.sha256(raw).hexdigest()
            if content_hash != expected_hash:
                continue
            metadata = _parse_document(raw, memory_id)
            if metadata.get("valid_to") or metadata.get("superseded_by"):
                # MemFW keeps superseded Markdown for audit; federation recall is
                # current-state only.
                continue
            score = 1.0 / (1.0 + abs(rank)) if rank else 1.0
            hit = MemoryHit(
                source="memfw",
                key=memory_id,
                text=_index_line(metadata, memory_id),
                score=score,
                tier=str(metadata["tier"]),
                provenance={
                    "memory_id": memory_id,
                    "layer": metadata["layer"],
                    "version": metadata.get("version"),
                    "source": metadata["source"],
                    "document_hash": content_hash,
                    "event_head": event_head,
                    "memory_metadata": {
                        **{
                            key: metadata[key]
                            for key in (
                                "confidence",
                                "valid_from",
                                "valid_to",
                                "valid_until",
                                "superseded_by",
                                "tenant",
                                "related",
                                "status",
                                "superseded",
                                # Recall eligibility rejects secret sensitivity
                                # and non-active lifecycle; dropping them here
                                # would make every MemFW document look
                                # private/active. Policy conditions/refs are
                                # validated too, so an invalid condition must
                                # fail closed rather than disappear from the
                                # hit's metadata.
                                "sensitivity",
                                "lifecycle",
                                "applies_when",
                                "exceptions",
                                "evidence_quote_ref",
                                "metadata",
                                # Privacy and project labels: a Ticket
                                # prompt leaves the machine, so its gate
                                # must see a private tag or the project a
                                # memory is about (ticket_memory_policy).
                                "tags",
                                "visibility",
                                "project",
                                "repo",
                            )
                            if key in metadata
                        },
                        # The ontology of the document: what kind of claim it
                        # is and the concepts it is about. It is not in the
                        # whitelist above because it is normalized rather than
                        # copied -- an unusable value is dropped instead of
                        # reaching the Worker as a broken concept. A memory
                        # that names a concept must still name it when it is
                        # read back, or the audit would report a memory as
                        # naming nothing.
                        **concept_ontology(metadata),
                    },
                },
            )
            if memory_hit_is_recallable(hit):
                hits.append(hit)
        return hits

    def list_proposals(
        self, status: str | None = None, *, deadline: float | None = None
    ) -> list[MemoryProposal]:
        """Read signed proposal payloads and their event-derived approval state."""
        events = self._events(deadline=deadline)
        created: dict[str, _Event] = {}
        decisions: dict[str, _Event] = {}
        erased: set[str] = set()
        for event in events:
            _check_deadline(deadline)
            proposal_id = event.details.get("proposal_id")
            if not isinstance(proposal_id, str):
                continue
            if not _ID_RE.fullmatch(proposal_id):
                raise MemfwReadError("invalid proposal id in event log")
            if event.kind == "agent_proposal" and isinstance(
                event.details.get("source_hash"), str
            ):
                created[proposal_id] = event
            elif event.kind in {"human_decision", "simulated_decision"}:
                decisions[proposal_id] = event
            elif event.kind == "proposal_erase":
                erased.add(proposal_id)
        proposals: list[MemoryProposal] = []
        for proposal_id, event in created.items():
            if proposal_id in erased:
                continue
            path = _safe_path(self.root, f".proposals/{proposal_id}.md")
            raw = _read_bounded(
                path,
                _MAX_DOCUMENT_BYTES,
                label=f"proposal document: {proposal_id}",
            )
            source_hash = str(event.details["source_hash"])
            if not _HASH_RE.fullmatch(source_hash):
                raise MemfwReadError(f"invalid proposal source hash: {proposal_id}")
            if hashlib.sha256(raw).hexdigest() != source_hash:
                raise MemfwReadError(f"proposal source hash mismatch: {proposal_id}")
            prefix = b"# Erasable agent proposal\n\n```json\n"
            suffix = b"\n```\n"
            if not raw.startswith(prefix) or not raw.endswith(suffix):
                raise MemfwReadError(f"malformed proposal document: {proposal_id}")
            try:
                payload = json.loads(raw[len(prefix) : -len(suffix)])
                expected_fields = {
                    "proposal_id",
                    "kind",
                    "target_ids",
                    "target_hashes",
                    "agent_name",
                    "model_name",
                    "input_hash",
                    "output",
                    "reason",
                    "payload_hash",
                    "actor",
                    "signature",
                    "status",
                    "created_at",
                    "updated_at",
                }
                if not isinstance(payload, dict) or set(payload) != expected_fields:
                    raise ValueError("proposal fields do not match the legacy schema")
                output = payload["output"]
                if not isinstance(output, dict):
                    raise ValueError("proposal output must be an object")
                canonical_output = json.dumps(
                    output, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                if (
                    hashlib.sha256(canonical_output.encode("utf-8")).hexdigest()
                    != payload["payload_hash"]
                ):
                    raise ValueError("proposal payload hash mismatch")
                if payload["proposal_id"] != proposal_id or payload["kind"] not in {
                    "salience",
                    "merge",
                    "layer",
                    "forget",
                    "anchor",
                }:
                    raise ValueError("proposal identity or kind mismatch")
                if event.actor != payload["actor"]:
                    raise ValueError("proposal actor does not match its event")
                target_ids = payload["target_ids"]
                if not isinstance(target_ids, list) or not all(
                    isinstance(item, str) for item in target_ids
                ):
                    raise ValueError("invalid proposal targets")
                if not target_ids or len(set(target_ids)) != len(target_ids):
                    raise ValueError("proposal targets must be non-empty and unique")
                if not all(_ID_RE.fullmatch(item) for item in target_ids):
                    raise ValueError("invalid proposal target id")
                for required in (
                    "agent_name",
                    "model_name",
                    "input_hash",
                    "actor",
                    "signature",
                ):
                    if (
                        not isinstance(payload.get(required), str)
                        or not payload[required]
                    ):
                        raise ValueError(f"invalid proposal field: {required}")
                if not isinstance(
                    payload["payload_hash"], str
                ) or not _HASH_RE.fullmatch(payload["payload_hash"]):
                    raise ValueError("invalid proposal payload hash")
                target_hashes = payload["target_hashes"]
                if not isinstance(target_hashes, dict):
                    raise ValueError("proposal target_hashes must be an object")
                if target_hashes and set(target_hashes) != set(target_ids):
                    raise ValueError("proposal target hash keys do not match targets")
                if any(
                    not isinstance(value, str) or not _HASH_RE.fullmatch(value)
                    for value in target_hashes.values()
                ):
                    raise ValueError("invalid proposal target hash")
                if payload["actor"] != f"agent:{payload['agent_name']}" and not (
                    payload["actor"] == "human"
                    and payload["agent_name"] == "human-review"
                ):
                    raise ValueError("proposal actor does not match agent identity")
                if payload["status"] not in {
                    "pending",
                    "approved",
                    "rejected",
                    "applied",
                }:
                    raise ValueError("invalid stored proposal status")
                if not isinstance(payload["reason"], str):
                    raise ValueError("proposal reason must be a string")
                created_at = datetime.fromisoformat(payload["created_at"])
                if created_at.tzinfo is None or created_at.utcoffset() is None:
                    raise ValueError("proposal timestamp must include a timezone")
                updated_at = datetime.fromisoformat(payload["updated_at"])
                if updated_at.tzinfo is None or updated_at.utcoffset() is None:
                    raise ValueError("proposal timestamp must include a timezone")
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise MemfwReadError(
                    f"invalid proposal payload: {proposal_id}"
                ) from exc
            decision = decisions.get(proposal_id)
            decision_kind = decision.details.get("decision") if decision else None
            if decision and decision_kind not in {"approve", "reject", "applied"}:
                raise MemfwReadError(f"unsupported proposal decision: {proposal_id}")
            if decision and decision.kind == "human_decision":
                if decision.actor != "human":
                    raise MemfwReadError(
                        f"human decision has a non-human actor: {proposal_id}"
                    )
                if decision.details.get("target_hash") != payload["payload_hash"]:
                    raise MemfwReadError(
                        f"decision payload hash mismatch: {proposal_id}"
                    )
                if decision.details.get("source_hash") != source_hash:
                    raise MemfwReadError(
                        f"decision source hash mismatch: {proposal_id}"
                    )
            if decision is None:
                projected_status = "pending"
            elif decision_kind == "approve":
                projected_status = (
                    "approved"
                    if decision.kind == "human_decision"
                    else "simulated_approved"
                )
            elif decision_kind == "reject":
                projected_status = (
                    "rejected"
                    if decision.kind == "human_decision"
                    else "simulated_rejected"
                )
            elif decision_kind == "applied":
                projected_status = (
                    "applied"
                    if decision.kind == "human_decision"
                    else "simulated_applied"
                )
            else:
                projected_status = "pending"
            if status is not None and projected_status != status:
                continue
            proposals.append(
                MemoryProposal(
                    proposal_id=proposal_id,
                    kind=payload["kind"],
                    target_ids=tuple(target_ids),
                    status=projected_status,
                    actor=payload.get("actor"),
                    created_at=created_at,
                    decision_at=decision.timestamp if decision else None,
                    provenance={
                        "proposal_event_hash": event.event_hash,
                        "decision_event_hash": decision.event_hash
                        if decision
                        else None,
                        "source_hash": source_hash,
                    },
                )
            )
        return sorted(
            proposals,
            key=lambda item: (
                item.created_at or datetime.min.replace(tzinfo=UTC),
                item.proposal_id,
            ),
        )


class MemfwMemory:
    """Async MemoryProvider adapter over one explicitly configured local vault."""

    def __init__(
        self,
        root: Path | str,
        owner_id: str,
        *,
        read_timeout_seconds: float = _DEFAULT_READ_TIMEOUT_SECONDS,
    ) -> None:
        if not owner_id.strip():
            raise ValueError("MemFW adapter needs a non-empty owner_id")
        if read_timeout_seconds <= 0:
            raise ValueError("MemFW read timeout must be positive")
        self.owner_id = owner_id
        self.read_timeout_seconds = read_timeout_seconds
        self.reader = MemfwReader(root)

    async def recall(
        self,
        user_id: str,
        query: str,
        k: int,
        tiers: Sequence[str],
        workspace_id: str | None = None,
        thread_id: str | None = None,
    ) -> list[MemoryHit]:
        del workspace_id, thread_id
        if user_id != self.owner_id:
            raise PermissionError("MemFW vault is bound to a different user")
        if "user" not in tiers:
            return []
        text = (query or "").strip()
        if not text or k <= 0:
            return []
        # Bound at the provider boundary instead of raising: a long prompt must
        # not take the whole federated block down with it.
        text = text[:_MAX_QUERY_CHARS]
        deadline = time.monotonic() + self.read_timeout_seconds
        hits = await _run_read(
            self.reader.recall, text, min(k, _MAX_LOCAL_RESULTS), deadline=deadline
        )
        return [
            MemoryHit(
                source=hit.source,
                key=hit.key,
                text=hit.text,
                score=hit.score,
                provenance={**hit.provenance, "memfw_tier": hit.tier},
                tier="user",
            )
            for hit in hits
        ]

    async def propose(self, user_id: str, entry: MemoryEntry) -> str:
        """A read-only vault never accepts writes or proposal mutations."""
        if user_id != self.owner_id:
            raise PermissionError("MemFW vault is bound to a different user")
        del entry
        from .provider import ReadOnlyMemoryProviderError

        raise ReadOnlyMemoryProviderError("MemFW retrieval is read-only")

    async def notify_forget(
        self, *, user_id: str, entry_id: str, content_hash: str
    ) -> str:
        """Acknowledge a forget request without changing the source vault."""
        if user_id != self.owner_id:
            return "unavailable"
        if not entry_id or not content_hash:
            return "unavailable"
        return "read_only"
