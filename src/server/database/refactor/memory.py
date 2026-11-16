"""Memory-entry projection queries (SQL layer, migration 089).

The LangGraph store remains the content source of truth; this table is the
tier/index/lifecycle projection the refactor needs. ``upsert_entry`` is keyed by
``(user_id, tier, entry_key, status)`` so re-projecting the same store write is
idempotent, while a superseded/rejected row is a separate lifecycle record, not
an overwrite of the active one.

``search`` is the generated tsvector column (Postgres FTS, ``simple`` config):
the query binds ``websearch_to_tsquery`` and ranks with ``ts_rank``. The
``simple`` config does not segment Chinese text without spaces — the memory eval
(``docs/refactor/0005-memory-eval.md``) has a dedicated case and Phase 2 decides
whether a trigram index is needed.

The ontology columns ``class`` and ``about`` (migration 148) are read here
through the *effective* values rather than the raw columns: a memory may name
several concepts, and the full list lives in the row's metadata. Browsing and
the filters below therefore see every concept a memory declares, and a
federated row -- which keeps the ontology in its metadata alone -- sees it too.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Mapping, Sequence

from psycopg.rows import dict_row

from src.server.contracts.memory_concept import (
    MAX_CONCEPT_VALUES,
    UNSET,
    Unset,
    concept_filter,
    row_class,
    row_concepts,
)
from src.server.contracts.memory_origin import normalize_memory_metadata
from src.server.contracts.memory_policy_metadata import (
    normalize_memory_policy_metadata_layers,
)
from src.server.contracts.memory_validity import is_current
from src.server.database import pool
from src.server.utils.pg_sanitize import (
    SafeJson,
    normalize_uuid,
    refactor_owner_uuid,
    strip_pg_nul_str,
)

logger = logging.getLogger(__name__)

#: Trigram word-similarity floor for an unspaced CJK query. Measured on the M2
#: corpus: an unspaced query against a longer index line scores ~0.57 (0.89 for
#: a prefix-embedded one), while unrelated lines stay below 0.3; 0.45 keeps the
#: former and rejects the latter. Used through ``<%`` so the GIN index applies.
_WORD_SIMILARITY_THRESHOLD = 0.45

#: CJK ranges the ``simple`` FTS lexer cannot segment (Han, kana, hangul).
_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")

#: Index lines are one-liners; a longer edit is truncated rather than stored.
MAX_INDEX_TEXT = 500

#: Bound on one ontology field of a memory row (``class`` or ``about``). Both
#: name a concept, and a concept id is a short token, so a longer value is a
#: mistake rather than a fact worth keeping.
MAX_CONCEPT_FIELD_LENGTH = 256

#: Candidate memories awaiting approval. They are never active rows; the SQL
#: filters also hide legacy rows written before that rule existed.
PROPOSED_PATTERN = "proposed/%"

#: How many rows one read of the browse listing fetches. The rows that are no
#: longer current are dropped after the query, so a page is filled by reading
#: batches in order until it is full or the owner's rows run out; a page comes
#: back short only at the end of the listing.
_PAGE_BATCH = 500

_MEMORY_COLUMNS = (
    "entry_id, user_id, tier, namespace, entry_key, content_hash, size,"
    " index_text, status, provenance, source_thread_id, source_response_id,"
    ' metadata, "class", "about", created_at, updated_at'
)
_NOT_FORGOTTEN = (
    "NOT EXISTS (SELECT 1 FROM memory_forget_tombstones f"
    " WHERE f.user_id = memory_entries.user_id"
    " AND f.identity_hash = memory_entries.identity_hash)"
)

#: Internal retrieval payload; never returned to the browser directly.
_RETRIEVAL_COLUMNS = _MEMORY_COLUMNS + ", embedding, embedding_model, embedding_content_hash"

#: The metadata of a row, as jsonb. The column is ``json``; the jsonb
#: operators are the ones that can walk an array of concept ids.
_METADATA = "memory_entries.metadata::jsonb"

#: The class a row declares: the column first, then the metadata. The column
#: is the indexed field and the last thing a re-projection wrote; the metadata
#: is where a federated source keeps the class.
_EFFECTIVE_CLASS = f"COALESCE(memory_entries.\"class\", {_METADATA}->>'class')"

#: The concepts a row declares, as one jsonb array.
#:
#: The ``about`` column holds the primary one -- it is the indexed pair with
#: ``class`` -- but a memory may name several, and the full list lives in the
#: metadata. A row that names no concept yields one NULL concept, so it stays
#: browsable as the unnamed group instead of disappearing from it.
_EFFECTIVE_CONCEPTS = (
    "CASE"
    f" WHEN jsonb_typeof({_METADATA}->'about') = 'array'"
    f"  AND jsonb_array_length({_METADATA}->'about') > 0"
    f" THEN {_METADATA}->'about'"
    f" WHEN jsonb_typeof({_METADATA}->'about') = 'string'"
    f" THEN jsonb_build_array({_METADATA}->>'about')"
    " ELSE jsonb_build_array(memory_entries.\"about\")"
    " END"
)

#: The concept ids one row declares, one row per concept, in the order the
#: row declares them. A repeated id is one concept, not two, and the list is
#: capped at the number of concepts a memory may name.
_CONCEPT_IDS = (
    "SELECT concept FROM ("
    " SELECT d.concept FROM ("
    "  SELECT DISTINCT ON (e.value #>> '{}') e.value #>> '{}' AS concept, e.ord"
    "    FROM jsonb_array_elements("
    + _EFFECTIVE_CONCEPTS
    + ") WITH ORDINALITY AS e(value, ord)"
    "   WHERE jsonb_typeof(e.value) = 'string'"
    "   ORDER BY e.value #>> '{}', e.ord"
    " ) d ORDER BY d.ord LIMIT "
    + str(MAX_CONCEPT_VALUES)
    + ") cc"
)

#: The row declares the concept named by the parameter.
_CONCEPT_EQUALS = "EXISTS (" + _CONCEPT_IDS + " WHERE concept = %s)"

#: The row declares no concept at all.
_NO_CONCEPT = "NOT EXISTS (" + _CONCEPT_IDS + ")"


def _normalized_id(value: object, field: str) -> str | None:
    if value is None:
        return None
    normalized = normalize_uuid(value)
    if normalized is None:
        raise ValueError(f"{field} is not a UUID")
    return normalized


def _concept_field(value: object, field: str) -> str | None:
    """One ontology field of a memory row: bounded, NUL-free, ``None`` if empty.

    A field that names nothing is stored as NULL rather than as an empty
    string, so "this memory names no concept" and "this memory names the
    concept called nothing" cannot be confused.
    """
    if value is None:
        return None
    text = strip_pg_nul_str(str(value)).strip()
    if not text:
        return None
    return text[:MAX_CONCEPT_FIELD_LENGTH]


async def _lock_entry_identity(cur: Any, *, owner: str, tier: str, entry_key: str) -> None:
    """Serialize projection transitions with retries and backfill for one key."""
    await cur.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"{owner}\x1f{tier}\x1f{entry_key}",),
    )


async def upsert_entry(
    *,
    user_id: str,
    tier: str,
    namespace: Sequence[str],
    entry_key: str,
    content_hash: str,
    size: int = 0,
    index_text: str = "",
    status: str = "active",
    provenance: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    source_thread_id: str | None = None,
    source_response_id: str | None = None,
    source_entry_id: str | None = None,
    source_entry_ids: Sequence[str] | None = None,
    memory_class: str | None = None,
    about: str | None = None,
) -> dict[str, Any]:
    """Project one store entry; the idempotent write path the backfill also uses.

    ``source_entry_ids`` records multi-source lineage in the same transaction as
    the row itself, so a half-written projection can never commit a searchable
    row without the lineage forget walks.

    ``memory_class`` and ``about`` are the ontology fields of the memory: what
    kind of claim it is, and the concept id of the thing it is about. Both are
    written as they are given -- a memory that names no concept keeps NULL
    there, and a re-projection that no longer names one clears the old value
    rather than leaving a stale class behind.
    """
    owner = refactor_owner_uuid(user_id)
    if owner is None:
        raise ValueError("user_id is required")
    normalized_metadata = normalize_memory_metadata(metadata)
    normalized_key = strip_pg_nul_str(entry_key)
    concept_class = _concept_field(memory_class, "class")
    concept_about = _concept_field(about, "about")
    async with pool.get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await _lock_entry_identity(
                    cur, owner=owner, tier=tier, entry_key=normalized_key
                )
                await cur.execute(
                    f"""
                    INSERT INTO memory_entries
                        (user_id, tier, namespace, entry_key, identity_hash, content_hash, size,
                         index_text, status, provenance, metadata, source_thread_id,
                         source_response_id, source_entry_id, "class", "about")
                    SELECT v.user_id, v.tier, v.namespace, v.entry_key, v.identity_hash,
                           v.content_hash, v.size, v.index_text, v.status, v.provenance,
                           v.metadata, v.source_thread_id, v.source_response_id,
                           v.source_entry_id, v."class", v."about"
                      FROM (
                        SELECT %s::uuid AS user_id, %s::text AS tier,
                               %s::text[] AS namespace, %s::text AS entry_key,
                               md5(jsonb_build_array(
                                   %s::text, %s::text[], %s::text
                               )::text) AS identity_hash,
                               %s::text AS content_hash, %s::int AS size,
                               %s::text AS index_text, %s::text AS status,
                               %s::jsonb AS provenance, %s::jsonb AS metadata,
                               %s::uuid AS source_thread_id,
                               %s::uuid AS source_response_id,
                               %s::uuid AS source_entry_id,
                               %s::text AS "class",
                               %s::text AS "about"
                      ) AS v
                     WHERE NOT EXISTS (
                        SELECT 1 FROM memory_forget_tombstones f
                         WHERE f.user_id = v.user_id
                           AND f.identity_hash = v.identity_hash
                     )
                    ON CONFLICT (user_id, tier, entry_key, status) DO UPDATE
                       SET content_hash = EXCLUDED.content_hash,
                           size = EXCLUDED.size,
                           index_text = EXCLUDED.index_text,
                           namespace = EXCLUDED.namespace,
                           identity_hash = EXCLUDED.identity_hash,
                           provenance = EXCLUDED.provenance,
                           metadata = EXCLUDED.metadata,
                           source_thread_id = EXCLUDED.source_thread_id,
                           source_response_id = EXCLUDED.source_response_id,
                           source_entry_id = EXCLUDED.source_entry_id,
                           "class" = EXCLUDED."class",
                           "about" = EXCLUDED."about",
                           updated_at = NOW()
                    RETURNING {_MEMORY_COLUMNS}
                    """,
                    (
                        owner,
                        tier,
                        list(namespace),
                        normalized_key,
                        tier,
                        list(namespace),
                        normalized_key,
                        strip_pg_nul_str(content_hash),
                        int(size),
                        strip_pg_nul_str(index_text) or "",
                        status,
                        SafeJson(dict(provenance or {})),
                        SafeJson(normalized_metadata),
                        _normalized_id(source_thread_id, "source_thread_id"),
                        _normalized_id(source_response_id, "source_response_id"),
                        _normalized_id(source_entry_id, "source_entry_id"),
                        concept_class,
                        concept_about,
                    ),
                )
                row = await cur.fetchone()
                if row is None:
                    # The identity was forgotten while this projection write was
                    # in flight; never recreate its plaintext row.
                    return {}
                row = dict(row)
                lineage_ids = [
                    normalized
                    for raw in (source_entry_ids or ())
                    if (normalized := _normalized_id(raw, "source_entry_id"))
                    is not None
                ]
                if lineage_ids:
                    await cur.execute(
                        "INSERT INTO memory_entry_lineage"
                        " (user_id, entry_id, source_entry_id)"
                        " SELECT c.user_id, c.entry_id, s.entry_id"
                        " FROM memory_entries c JOIN memory_entries s"
                        " ON s.user_id = c.user_id"
                        " AND s.entry_id = ANY(%s::uuid[])"
                        " AND s.namespace = c.namespace"
                        " WHERE c.user_id = %s AND c.entry_id = %s"
                        " AND c.tier = 'user'"
                        " AND c.status IN ('active', 'superseded')"
                        " AND s.tier = 'user'"
                        " ON CONFLICT (user_id, entry_id, source_entry_id)"
                        " DO NOTHING",
                        (lineage_ids, owner, row["entry_id"]),
                    )
                return row


async def list_entries(
    *,
    user_id: str,
    tier: str | None = None,
    status: str = "active",
    limit: int = 100,
    offset: int = 0,
    memory_class: str | None | Unset = UNSET,
    about: str | None | Unset = UNSET,
) -> list[dict[str, Any]]:
    """The owner's projection rows, optionally narrowed to one concept.

    ``memory_class`` and ``about`` are the ontology filters of the browse page:
    a class names what kind of claim a memory is, an ``about`` names the
    concept it is about. Each filter has three states, and the third one is
    the point of this signature: an unset filter (``UNSET``) narrows nothing,
    a value narrows to the rows that name it, and ``None`` narrows to the
    rows that name *nothing* -- the unnamed group the browse page offers.
    A filter that names nothing is not the same as no filter at all, so the
    two are told apart here rather than guessed at from the value.

    ``offset`` continues a listing past its first page: the rows are ordered
    by when they changed, and the key breaks a tie so that two pages of the
    same listing never overlap or skip a row.
    """
    if limit < 1 or limit > 500:
        raise ValueError("memory page size must be between 1 and 500")
    if offset < 0:
        raise ValueError("memory page offset must not be negative")
    owner = refactor_owner_uuid(user_id)
    if owner is None:
        return []
    clauses = ["user_id = %s", "status = %s", "entry_key NOT LIKE %s", _NOT_FORGOTTEN]
    if status == "active":
        clauses.append("COALESCE(metadata->>'lifecycle', 'active') = 'active'")
    params: list[Any] = [owner, status, PROPOSED_PATTERN]
    if tier is not None:
        clauses.append("tier = %s")
        params.append(tier)
    class_is_null, class_value = concept_filter(memory_class)
    if class_is_null:
        clauses.append(f"{_EFFECTIVE_CLASS} IS NULL")
    elif class_value is not None:
        clauses.append(f"{_EFFECTIVE_CLASS} = %s")
        params.append(class_value)
    about_is_null, about_value = concept_filter(about)
    if about_is_null:
        clauses.append(_NO_CONCEPT)
    elif about_value is not None:
        clauses.append(_CONCEPT_EQUALS)
        params.append(about_value)
    # The rows that are no longer current are dropped after the query, so an
    # SQL offset cannot address a page of current rows. Read the ordered rows
    # in batches, skip the first ``offset`` current ones and keep the next
    # ``limit``: a page is short only when the listing has ended, which is
    # what the browse page takes a short page to mean.
    sql = (
        f"SELECT {_MEMORY_COLUMNS} FROM memory_entries"
        f" WHERE {' AND '.join(clauses)}"
        " ORDER BY updated_at DESC, entry_key ASC LIMIT %s OFFSET %s"
    )
    filtered = status == "active"
    page: list[dict[str, Any]] = []
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            if not filtered:
                await cur.execute(sql, (*params, limit, offset))
                page = [dict(row) for row in await cur.fetchall()]
            else:
                skip = offset
                read = 0
                while len(page) < limit:
                    await cur.execute(sql, (*params, _PAGE_BATCH, read))
                    batch = [dict(row) for row in await cur.fetchall()]
                    read += len(batch)
                    for row in batch:
                        metadata = row.get("metadata")
                        if not is_current(metadata if isinstance(metadata, Mapping) else None):
                            continue
                        if skip:
                            skip -= 1
                            continue
                        page.append(row)
                        if len(page) == limit:
                            break
                    if len(batch) < _PAGE_BATCH:
                        break
    # The row carries the class and the concepts it declares, as the filters
    # read them: a class kept only in the metadata is the row's class, and a
    # memory about two things says so.
    for row in page:
        row["class"] = row_class(row)
        row["concepts"] = list(row_concepts(row))
    return page


async def list_concepts(
    *,
    user_id: str,
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """The owner's memory concepts: one row per ``(class, about)`` pair.

    A memory that names several concepts appears under each of them, because
    the browse page groups by concept and a memory about two things belongs
    to both groups. A row with neither field is one memory of an unnamed kind
    about an unnamed concept, and it is reported as such rather than dropped:
    hiding it would make the page claim there is nothing left to browse.

    ``offset`` continues the listing past its first page, so an owner with
    more concepts than one page holds can still reach the rest.
    """
    if limit < 1 or limit > 1000:
        raise ValueError("memory concept page size must be between 1 and 1000")
    if offset < 0:
        raise ValueError("memory concept page offset must not be negative")
    owner = refactor_owner_uuid(user_id)
    if owner is None:
        return []
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_EFFECTIVE_CLASS} AS class, c.concept AS about,"
                " count(*) AS entries"
                " FROM memory_entries"
                f" LEFT JOIN LATERAL ({_CONCEPT_IDS}) AS c ON TRUE"
                " WHERE memory_entries.user_id = %s"
                " AND memory_entries.status = 'active'"
                " AND memory_entries.entry_key NOT LIKE %s"
                " AND COALESCE(memory_entries.metadata->>'lifecycle', 'active') = 'active'"
                f" AND {_NOT_FORGOTTEN}"
                f" GROUP BY {_EFFECTIVE_CLASS}, c.concept"
                f" ORDER BY entries DESC, {_EFFECTIVE_CLASS} NULLS FIRST,"
                " c.concept NULLS FIRST"
                " LIMIT %s OFFSET %s",
                (owner, PROPOSED_PATTERN, limit, offset),
            )
            return [dict(row) for row in await cur.fetchall()]


async def update_index_texts(
    *, user_id: str, tier: str, updates: Mapping[str, str]
) -> int:
    """Refresh the index lines of existing active rows in one round trip.

    The write-path dual write calls this when ``memory.md`` changes: the pointer
    lines are the only place a row's index text comes from, so one edit can
    change many rows. Rows the projection does not have yet are left to the
    backfill job (an index edit is not evidence a detail file exists).
    """
    if tier not in ("user", "project", "workspace", "session", "task"):
        raise ValueError(f"unknown memory tier {tier!r}")
    clean = {
        str(key): str(text)[:MAX_INDEX_TEXT]
        for key, text in (updates or {}).items()
        if str(key) and str(text)
    }
    if not clean:
        return 0
    normalized_user = refactor_owner_uuid(user_id)
    if normalized_user is None:
        raise ValueError("user_id is required")
    keys = list(clean)
    texts = [clean[key] for key in keys]
    async with pool.get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE memory_entries AS m
                   SET index_text = u.index_text, updated_at = NOW()
                  FROM unnest(%s::text[], %s::text[]) AS u(entry_key, index_text)
                 WHERE m.user_id = %s AND m.tier = %s AND m.status = 'active'
                   AND COALESCE(m.metadata->>'lifecycle', 'active') = 'active'
                   AND m.entry_key = u.entry_key
                   AND m.entry_key NOT LIKE %s
                   AND NOT EXISTS (
                       SELECT 1 FROM memory_forget_tombstones f
                        WHERE f.user_id = m.user_id
                          AND f.identity_hash = m.identity_hash
                   )
                """,
                (keys, texts, normalized_user, tier, PROPOSED_PATTERN),
            )
            return cur.rowcount or 0


def has_cjk(text: str) -> bool:
    """Whether the query contains script the ``simple`` FTS lexer cannot split."""
    return bool(_CJK_RE.search(text or ""))


def like_pattern(term: str) -> str:
    """A contains pattern for one term, with LIKE metacharacters escaped.

    The ESCAPE clause in the SQL is a single backslash, so the backslash itself
    is escaped first; ``%``/``_`` from a query must match literally.
    """
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _combined_text() -> str:
    return "(entry_key || ' ' || index_text)"


def _scope_clauses(
    *,
    tier: str | None,
    thread_id: str | None,
    workspace_id: str | None,
) -> tuple[list[str], list[Any]]:
    """Namespace predicates for a single tier or a mixed-tier retrieval.

    ``workspace`` and ``project`` rows share the workspace namespace, so both
    must be filtered in SQL (not after the per-tier ``LIMIT``). A mixed-tier
    query scopes only those two tiers, leaving user-level rows visible to a
    workspace-scoped recall. With no workspace the rows stay unscoped, matching
    the baseline Chief-of-Staff behaviour instead of dropping the tiers.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if thread_id:
        clauses.append("namespace[3] = %s")
        params.append(thread_id)
        return clauses, params
    if workspace_id and tier in {"workspace", "project"}:
        clauses.append("namespace[3] = %s")
        params.append(workspace_id)
    elif workspace_id and tier is None:
        clauses.append("(tier NOT IN ('workspace', 'project') OR namespace[3] = %s)")
        params.append(workspace_id)
    if tier in {"session", "task"} and not thread_id:
        clauses.append("FALSE")
    elif tier is None:
        clauses.append("tier NOT IN ('session', 'task')")
    return clauses, params



async def _search_fts(
    *,
    user_id: str,
    text: str,
    tier: str | None,
    limit: int,
    thread_id: str | None = None,
    workspace_id: str | None = None,
) -> list[dict[str, Any]]:
    clauses = [
        "user_id = %s",
        "status = 'active'",
        "COALESCE(metadata->>'lifecycle', 'active') = 'active'",
        "entry_key NOT LIKE %s",
        _NOT_FORGOTTEN,
        "search @@ websearch_to_tsquery('simple', %s)",
    ]
    params: list[Any] = [user_id, PROPOSED_PATTERN, text]
    if tier is not None:
        clauses.append("tier = %s")
        params.append(tier)
    scope_clauses, scope_params = _scope_clauses(
        tier=tier, thread_id=thread_id, workspace_id=workspace_id
    )
    clauses.extend(scope_clauses)
    params.extend(scope_params)
    params.extend([text, limit])
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_MEMORY_COLUMNS} FROM memory_entries"
                f" WHERE {' AND '.join(clauses)}"
                " ORDER BY ts_rank(search, websearch_to_tsquery('simple', %s)) DESC,"
                "          updated_at DESC"
                " LIMIT %s",
                tuple(params),
            )
            return [dict(row) for row in await cur.fetchall()]


async def _search_trigram(
    *,
    user_id: str,
    text: str,
    tier: str | None,
    limit: int,
    thread_id: str | None = None,
    workspace_id: str | None = None,
) -> list[dict[str, Any]]:
    """CJK retrieval: contains-per-term, or trigram similarity for one run.

    A spaced query (``项目 截止日期``) is AND over each term's contains match; an
    unspaced Chinese query (``我关注的项目``) cannot be split without a segmenter, so
    it goes through the trigram similarity operator (indexable, default 0.3
    threshold) instead of trying to match the whole run literally.
    """
    combined = _combined_text()
    terms = text.split()
    clauses = [
        "user_id = %s",
        "status = 'active'",
        "COALESCE(metadata->>'lifecycle', 'active') = 'active'",
        "entry_key NOT LIKE %s",
        _NOT_FORGOTTEN,
    ]
    params: list[Any] = [user_id, PROPOSED_PATTERN]
    if len(terms) > 1:
        for term in terms:
            clauses.append(f"{combined} ILIKE %s ESCAPE '\\'")
            params.append(like_pattern(term))
    else:
        # The operator takes the query on the left and the indexed line on the
        # right (``text1 <% text2`` = text1 is similar to a word of text2); the
        # doubled % in the f-string is psycopg's literal-percent escape.
        clauses.append(f"%s <%% {combined}")
        params.append(text)
    if tier is not None:
        clauses.append("tier = %s")
        params.append(tier)
    scope_clauses, scope_params = _scope_clauses(
        tier=tier, thread_id=thread_id, workspace_id=workspace_id
    )
    clauses.extend(scope_clauses)
    params.extend(scope_params)
    params.extend([text, limit])
    async with pool.get_db_connection() as conn:
        # The word-similarity threshold is a per-session GUC; SET LOCAL keeps it
        # scoped to this query (a plain SET would leak to the next borrower of a
        # pooled connection).
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT set_config('pg_trgm.word_similarity_threshold', %s, true)",
                    (str(_WORD_SIMILARITY_THRESHOLD),),
                )
                await cur.execute(
                    f"SELECT {_MEMORY_COLUMNS} FROM memory_entries"
                    f" WHERE {' AND '.join(clauses)}"
                    f" ORDER BY word_similarity(%s, {combined}) DESC, updated_at DESC"
                    " LIMIT %s",
                    tuple(params),
                )
                return [dict(row) for row in await cur.fetchall()]


async def list_retrieval_rows(
    *,
    user_id: str,
    tier: str | None = None,
    thread_id: str | None = None,
    workspace_id: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Active rows with their optional embedding, for hybrid ranking."""
    if tier in {"session", "task"} and not thread_id:
        return []
    owner = refactor_owner_uuid(user_id)
    if owner is None:
        return []
    clauses = [
        "user_id = %s",
        "status = 'active'",
        "COALESCE(metadata->>'lifecycle', 'active') = 'active'",
        "entry_key NOT LIKE %s",
        _NOT_FORGOTTEN,
    ]
    params: list[Any] = [owner, PROPOSED_PATTERN]
    if tier is not None:
        clauses.append("tier = %s")
        params.append(tier)
    scope_clauses, scope_params = _scope_clauses(
        tier=tier, thread_id=thread_id, workspace_id=workspace_id
    )
    clauses.extend(scope_clauses)
    params.extend(scope_params)
    params.append(max(1, min(int(limit), 500)))
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_RETRIEVAL_COLUMNS} FROM memory_entries"
                f" WHERE {' AND '.join(clauses)}"
                " ORDER BY updated_at DESC LIMIT %s",
                tuple(params),
            )
            rows = [dict(row) for row in await cur.fetchall()]
            return [
                row
                for row in rows
                if is_current(row.get("metadata"))
                and normalize_memory_policy_metadata_layers(row.get("metadata"))["sensitivity"]
                != "secret"
            ]


async def save_embedding(
    *,
    user_id: str,
    tier: str,
    entry_key: str,
    embedding: Sequence[float],
    model: str,
    content_hash: str,
) -> int:
    """Persist one vector only while the row's content hash still matches."""
    owner = refactor_owner_uuid(user_id)
    if owner is None:
        return 0
    async with pool.get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE memory_entries SET embedding = %s, embedding_model = %s,"
                " embedding_content_hash = %s, embedding_updated_at = NOW()"
                " WHERE user_id = %s AND tier = %s AND entry_key = %s"
                " AND status = 'active' AND content_hash = %s",
                (
                    SafeJson(list(embedding)),
                    model,
                    content_hash,
                    owner,
                    tier,
                    entry_key,
                    content_hash,
                ),
            )
            return cur.rowcount or 0


async def get_entry(
    *,
    user_id: str,
    tier: str,
    entry_key: str,
    status: str = "active",
) -> dict[str, Any] | None:
    """One projection row, owner-scoped and excluding candidates."""
    owner = refactor_owner_uuid(user_id)
    if owner is None:
        return None
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_MEMORY_COLUMNS} FROM memory_entries"
                " WHERE user_id = %s AND tier = %s AND entry_key = %s"
                " AND status = %s AND entry_key NOT LIKE %s AND "
                + _NOT_FORGOTTEN
                + " LIMIT 1",
                (owner, tier, entry_key, status, PROPOSED_PATTERN),
            )
            row = await cur.fetchone()
            result = dict(row) if row is not None else None
            if result is not None and status == "active" and not is_current(
                result.get("metadata")
                if isinstance(result.get("metadata"), Mapping)
                else None
            ):
                return None
            return result


async def get_active_entry_for_approval(
    *, user_id: str, tier: str, entry_key: str
) -> dict[str, Any] | None:
    """Read an active owner row without retrieval-time validity filtering.

    Manual approval may bind a future-dated policy. Its frozen validity window
    controls enforcement; projection retrieval must not hide the row here.
    """
    owner = refactor_owner_uuid(user_id)
    if owner is None:
        return None
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_MEMORY_COLUMNS} FROM memory_entries"
                " WHERE user_id = %s AND tier = %s AND entry_key = %s"
                " AND status = 'active' AND entry_key NOT LIKE %s AND "
                + _NOT_FORGOTTEN
                + " LIMIT 1",
                (owner, tier, entry_key, PROPOSED_PATTERN),
            )
            row = await cur.fetchone()
            return dict(row) if row is not None else None


async def get_entry_for_forget(*, user_id: str, entry_id: str) -> dict[str, Any] | None:
    """Return one owner-scoped row and its forget lineage without exposing text."""
    owner = refactor_owner_uuid(user_id)
    entry_uuid = _normalized_id(entry_id, "entry_id")
    if owner is None or entry_uuid is None:
        return None
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT entry_id::text AS entry_id, user_id::text AS user_id,"
                " tier, namespace, entry_key, content_hash, source_entry_id::text"
                " AS source_entry_id FROM memory_entries"
                " WHERE user_id = %s AND entry_id = %s",
                (owner, entry_uuid),
            )
            row = await cur.fetchone()
            return dict(row) if row is not None else None


async def get_forget_closure(*, user_id: str, entry_id: str) -> list[dict[str, Any]]:
    """Find the selected entry and its same-owner, explicitly linked descendants."""
    owner = refactor_owner_uuid(user_id)
    entry_uuid = _normalized_id(entry_id, "entry_id")
    if owner is None or entry_uuid is None:
        return []
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "WITH RECURSIVE closure AS ("
                " SELECT entry_id, user_id, tier, namespace, entry_key, content_hash,"
                " identity_hash, source_entry_id FROM memory_entries"
                " WHERE user_id = %s AND entry_id = %s"
                " UNION"
                " SELECT child.entry_id, child.user_id, child.tier, child.namespace,"
                " child.entry_key, child.content_hash, child.identity_hash,"
                " child.source_entry_id"
                " FROM memory_entries child JOIN closure parent"
                " ON child.source_entry_id = parent.entry_id OR EXISTS ("
                " SELECT 1 FROM memory_entry_lineage l"
                " WHERE l.user_id = child.user_id AND l.entry_id = child.entry_id"
                " AND l.source_entry_id = parent.entry_id) OR ("
                " child.user_id = parent.user_id AND child.tier = parent.tier"
                " AND child.namespace = parent.namespace"
                " AND child.entry_key = parent.entry_key)"
                " WHERE child.user_id = %s"
                " AND child.tier = parent.tier"
                " AND child.namespace = parent.namespace"
                ") SELECT entry_id::text AS entry_id, user_id::text AS user_id, tier,"
                " namespace, entry_key, content_hash, identity_hash, source_entry_id::text"
                " AS source_entry_id FROM closure LIMIT 257",
                (owner, entry_uuid, owner),
            )
            rows = [dict(row) for row in await cur.fetchall()]
            if len(rows) > 256:
                raise ValueError("memory forget lineage exceeds the safe bound")
            return rows


async def record_entry_lineage(
    *, user_id: str, entry_id: str, source_entry_ids: Sequence[str]
) -> int:
    """Persist validated same-owner parent links for multi-source revisions."""
    owner = refactor_owner_uuid(user_id)
    child = _normalized_id(entry_id, "entry_id")
    source_ids = [_normalized_id(value, "source_entry_id") for value in source_entry_ids]
    if (
        owner is None
        or child is None
        or not source_ids
        or len(source_ids) > 16
        or child in source_ids
        or len(set(source_ids)) != len(source_ids)
    ):
        raise ValueError("invalid memory entry lineage")
    async with pool.get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO memory_entry_lineage (user_id, entry_id, source_entry_id) "
                    "SELECT c.user_id, c.entry_id, s.entry_id "
                    "FROM memory_entries c JOIN memory_entries s "
                    "ON s.user_id = c.user_id AND s.entry_id = ANY(%s::uuid[]) "
                    "AND s.namespace = c.namespace "
                    "WHERE c.user_id = %s AND c.entry_id = %s AND c.tier = 'user' "
                    "AND c.status IN ('active', 'superseded') AND s.tier = 'user' "
                    "ON CONFLICT (user_id, entry_id, source_entry_id) DO NOTHING",
                    (source_ids, owner, child),
                )
                await cur.execute(
                    "SELECT count(*) FROM memory_entry_lineage "
                    "WHERE user_id = %s AND entry_id = %s "
                    "AND source_entry_id = ANY(%s::uuid[])",
                    (owner, child, source_ids),
                )
                row = await cur.fetchone()
                count = int(row[0] if row else 0)
                if count != len(source_ids):
                    raise RuntimeError("memory source lineage does not match owner rows")
                return count


async def record_forget_tombstones(
    *, user_id: str, rows: Sequence[Mapping[str, Any]], root_entry_id: str, reason: str
) -> int:
    """Hide a bounded owner-scoped closure before mutating canonical storage."""
    owner = refactor_owner_uuid(user_id)
    if owner is None or reason not in {"user_request", "correction", "duplicate", "other"}:
        raise ValueError("invalid memory forget request")
    root_id = _normalized_id(root_entry_id, "root_entry_id")
    if root_id is None:
        raise ValueError("memory forget root id is required")
    values = []
    for row in rows:
        entry_id = _normalized_id(row.get("entry_id"), "entry_id")
        digest = str(row.get("content_hash") or "").strip()
        identity_hash = str(row.get("identity_hash") or "").strip()
        if entry_id is None or not digest or not identity_hash:
            raise ValueError("memory forget row is missing its identity hash")
        values.append((owner, entry_id, root_id, identity_hash, digest, reason))
    if not values:
        return 0
    async with pool.get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO memory_forget_tombstones"
                    " (user_id, entry_id, root_entry_id, identity_hash, content_hash, reason)"
                    " VALUES (%s,%s,%s,%s,%s,%s)"
                    " ON CONFLICT (user_id, entry_id) DO UPDATE"
                    " SET root_entry_id = EXCLUDED.root_entry_id,"
                    " identity_hash = EXCLUDED.identity_hash,"
                    " content_hash = EXCLUDED.content_hash, reason = EXCLUDED.reason",
                    values,
                )
                return int(cur.rowcount or 0)


async def get_forget_tombstone(*, user_id: str, entry_id: str) -> dict[str, Any] | None:
    owner = refactor_owner_uuid(user_id)
    entry_uuid = _normalized_id(entry_id, "entry_id")
    if owner is None or entry_uuid is None:
        return None
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT entry_id::text AS entry_id, root_entry_id::text AS root_entry_id,"
                " content_hash, reason"
                " FROM memory_forget_tombstones WHERE user_id = %s AND entry_id = %s",
                (owner, entry_uuid),
            )
            row = await cur.fetchone()
            return dict(row) if row is not None else None


async def get_forget_tombstone_group(
    *, user_id: str, root_entry_id: str
) -> list[dict[str, Any]]:
    owner = refactor_owner_uuid(user_id)
    root_id = _normalized_id(root_entry_id, "root_entry_id")
    if owner is None or root_id is None:
        return []
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT entry_id::text AS entry_id, content_hash, reason"
                " FROM memory_forget_tombstones"
                " WHERE user_id = %s AND root_entry_id = %s"
                " ORDER BY entry_id LIMIT 257",
                (owner, root_id),
            )
            rows = [dict(row) for row in await cur.fetchall()]
            if len(rows) > 256:
                raise ValueError("memory forget tombstone group exceeds the safe bound")
            return rows


async def match_forget_tombstone_locators(
    *, user_id: str, root_entry_id: str, candidates: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Return scanned store locators whose canonical identity matches this tombstone group."""
    owner = refactor_owner_uuid(user_id)
    root_id = _normalized_id(root_entry_id, "root_entry_id")
    if owner is None or root_id is None or not candidates:
        return []
    if len(candidates) > 257:
        raise ValueError("memory forget locator scan exceeds the safe bound")
    values: list[tuple[Any, ...]] = []
    for candidate in candidates:
        tier = str(candidate.get("tier") or "")
        namespace = candidate.get("namespace")
        key = str(candidate.get("entry_key") or "")
        if (
            tier not in {"user", "workspace", "session"}
            or not isinstance(namespace, (list, tuple))
            or not key
        ):
            raise ValueError("memory forget locator candidate is malformed")
        values.append((tier, list(namespace), key))
    placeholders = ",".join(["(%s::text, %s::text[], %s::text)"] * len(values))
    params: list[Any] = [value for row in values for value in row]
    params.extend((owner, root_id))
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "WITH candidates(tier, namespace, entry_key) AS (VALUES "
                + placeholders
                + ") SELECT f.entry_id::text AS entry_id, c.tier, c.namespace, c.entry_key"
                " FROM candidates c JOIN memory_forget_tombstones f"
                " ON f.identity_hash = md5(jsonb_build_array(c.tier, c.namespace, c.entry_key)::text)"
                " WHERE f.user_id = %s AND f.root_entry_id = %s"
                " ORDER BY f.entry_id LIMIT 257",
                params,
            )
            rows = [dict(row) for row in await cur.fetchall()]
            if len(rows) > 256:
                raise ValueError("memory forget tombstone group exceeds the safe bound")
            return rows


async def get_projection_rows(
    *, user_id: str, tier: str, entry_keys: Sequence[str]
) -> list[dict[str, Any]]:
    """Current projection rows for the keys an activation is about to mutate."""
    owner = refactor_owner_uuid(user_id)
    keys = [str(key) for key in entry_keys if key]
    if owner is None or not keys:
        return []
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_MEMORY_COLUMNS}, identity_hash, source_entry_id"
                " FROM memory_entries"
                " WHERE user_id = %s AND tier = %s AND entry_key = ANY(%s::text[])",
                (owner, tier, keys[:256]),
            )
            return [dict(row) for row in await cur.fetchall()]


async def restore_projection_rows(
    *,
    user_id: str,
    tier: str,
    entry_keys: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
    remove_entry_ids: Sequence[str] = (),
) -> None:
    """Restore the projection exactly as the snapshot saw it (rollback path).

    Rows created or mutated by the failed activation are deleted (with their
    lineage), then the snapshotted rows are re-inserted with their original
    entry id, status, hash, identity and lineage, so the idempotent retry sees
    the pre-activation projection rather than a re-derived approximation.
    """
    owner = refactor_owner_uuid(user_id)
    keys = [str(key) for key in entry_keys if key]
    if owner is None or not keys:
        return
    extra_ids = [
        normalized
        for raw in remove_entry_ids
        if (normalized := _normalized_id(raw, "entry_id")) is not None
    ]
    snapshot_ids = [
        normalized
        for row in rows
        if (normalized := _normalized_id(row.get("entry_id"), "entry_id"))
        is not None
    ]
    async with pool.get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT entry_id::text, source_entry_id::text"
                    " FROM memory_entry_lineage"
                    " WHERE user_id = %s"
                    " AND (entry_id = ANY(%s::uuid[])"
                    "      OR source_entry_id = ANY(%s::uuid[]))",
                    (owner, snapshot_ids or [None], snapshot_ids or [None]),
                )
                snapshot_lineage = [
                    (str(row["entry_id"]), str(row["source_entry_id"]))
                    for row in await cur.fetchall()
                ]
                await cur.execute(
                    "SELECT entry_id::text FROM memory_entries"
                    " WHERE user_id = %s AND tier = %s AND entry_key = ANY(%s::text[])"
                    " FOR UPDATE",
                    (owner, tier, keys[:256]),
                )
                present_ids = [str(row["entry_id"]) for row in await cur.fetchall()]
                delete_ids = sorted(set(present_ids) | set(extra_ids))
                if delete_ids:
                    await cur.execute(
                        "DELETE FROM memory_entry_lineage WHERE user_id = %s"
                        " AND (entry_id = ANY(%s::uuid[])"
                        " OR source_entry_id = ANY(%s::uuid[]))",
                        (owner, delete_ids, delete_ids),
                    )
                await cur.execute(
                    "DELETE FROM memory_entries WHERE user_id = %s AND tier = %s"
                    " AND entry_key = ANY(%s::text[])",
                    (owner, tier, keys[:256]),
                )
                for row in rows:
                    await cur.execute(
                        "INSERT INTO memory_entries"
                        " (entry_id, user_id, tier, namespace, entry_key,"
                        "  identity_hash, content_hash, size, index_text, status,"
                        "  provenance, metadata, source_thread_id,"
                        "  source_response_id, source_entry_id, created_at,"
                        '  updated_at, "class", "about")'
                        " VALUES (%s, %s, %s, %s::text[], %s, %s, %s, %s, %s,"
                        " %s, %s::jsonb, %s::jsonb, %s::uuid, %s::uuid,"
                        ' %s::uuid, %s, %s, %s::text, %s::text)'
                        " ON CONFLICT DO NOTHING",
                        (
                            row.get("entry_id"),
                            owner,
                            tier,
                            list(row.get("namespace") or ()),
                            str(row.get("entry_key") or ""),
                            str(row.get("identity_hash") or ""),
                            str(row.get("content_hash") or ""),
                            int(row.get("size") or 0),
                            str(row.get("index_text") or ""),
                            str(row.get("status") or "active"),
                            SafeJson(dict(row.get("provenance") or {})),
                            SafeJson(dict(row.get("metadata") or {})),
                            row.get("source_thread_id"),
                            row.get("source_response_id"),
                            row.get("source_entry_id"),
                            row.get("created_at"),
                            row.get("updated_at"),
                            row.get("class"),
                            row.get("about"),
                        ),
                    )
                for entry_id, source_entry_id in snapshot_lineage:
                    # Recreate the edge only when both endpoints still exist:
                    # the child is a restored row, the parent is either
                    # restored or a surviving row outside this activation.
                    await cur.execute(
                        "INSERT INTO memory_entry_lineage"
                        " (user_id, entry_id, source_entry_id)"
                        " SELECT %s, c.entry_id, s.entry_id"
                        " FROM memory_entries c JOIN memory_entries s"
                        " ON c.user_id = %s AND s.user_id = %s"
                        " WHERE c.entry_id = %s AND s.entry_id = %s"
                        " ON CONFLICT (user_id, entry_id, source_entry_id)"
                        " DO NOTHING",
                        (owner, owner, owner, entry_id, source_entry_id),
                    )


async def get_entries_by_ids(
    *, user_id: str, entry_ids: Sequence[str]
) -> list[dict[str, Any]]:
    """Projected rows for forgotten entry ids, used to retry projection purge."""
    owner = refactor_owner_uuid(user_id)
    if owner is None or not entry_ids:
        return []
    ids = []
    for entry_id in entry_ids:
        try:
            normalized = _normalized_id(entry_id, "entry_id")
        except ValueError:
            # A tombstone row can only carry a normalized id in practice; skip
            # a malformed one rather than fail the retry purge.
            continue
        if normalized is not None:
            ids.append(normalized)
    if not ids:
        return []
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_MEMORY_COLUMNS} FROM memory_entries"
                " WHERE user_id = %s AND entry_id = ANY(%s::uuid[])"
                " LIMIT 257",
                (owner, ids[:256]),
            )
            return [dict(row) for row in await cur.fetchall()]


#: The vocabulary a forget notice's reason may carry, spelled as the CHECK of
#: migration 142 spells it. It is the *forget* vocabulary -- what the remote
#: answered when it was asked to forget -- and it is emphatically not the
#: contract's ``/related`` reasons (``link``, ``lexical`` and the rest), which
#: say why one memory sits next to another. A ruling outside these three is
#: stored as NULL, because the notice is worth more than the ruling it
#: carried and the forget must not fail over one. The code that logs the
#: refusal is stable, never the value itself.
_FORGET_NOTICE_REASONS = frozenset(
    ("no_forget_endpoint", "not_supported", "remote_rejected")
)

#: What the federation layer can hand over, mapped onto the code the column
#: admits. The federation knows the remote's words -- the runtime message it
#: answers with for an operator -- and this module knows the vocabulary, so
#: only the code crosses that boundary and never the raw text: a read-back of
#: the row says the same thing whichever wording the remote happened to use.
#: A new wording from the federation gets a line here, and nowhere else.
_FORGET_NOTICE_REASON_MAP = {
    # Contract v2 offers ``/search``, ``/stage`` and ``/related`` and no forget
    # endpoint whatsoever; this is the message the integration layer answers
    # with for a URL-bound MemFW.
    "remote memfw has no forget endpoint": "no_forget_endpoint",
    # A caller that already spells the vocabulary hands the code over as it is.
    "no_forget_endpoint": "no_forget_endpoint",
    "not_supported": "not_supported",
    "remote_rejected": "remote_rejected",
}


def _notice_reason(reason: object) -> str | None:
    """The notice's reason as a vocabulary code, else NULL and a stable code.

    The ruling is the federation's, and it arrives in the remote's own words;
    the row keeps only the code it maps to. A ruling the vocabulary does not
    have is still evidence about the provider, so the notice survives it as
    NULL rather than being dropped or failing the forget. Only the stable code
    is logged: the ruling itself is untrusted text, and a log line that
    carried it would put it somewhere no vocabulary governs.
    """
    if reason is None:
        return None
    text = " ".join(str(reason).split())
    if not text:
        return None
    # The vocabulary is the authority -- it is what the CHECK spells -- so a
    # map entry that drifted outside it is a refusal, not a new code.
    code = _FORGET_NOTICE_REASON_MAP.get(text)
    if code is None or code not in _FORGET_NOTICE_REASONS:
        logger.warning("[memory_db] forget notice reason_not_in_vocab")
        return None
    return code


async def record_forget_provider_notices(
    *, user_id: str, notices: Sequence[Mapping[str, str]]
) -> None:
    """Persist source identifiers, bounded read-only outcomes and their reasons."""
    owner = refactor_owner_uuid(user_id)
    if owner is None:
        raise ValueError("user_id is required")
    allowed = {
        "deleted",
        "read_only",
        "unavailable",
        "not_configured",
        # The provider does not offer the operation at all. It is not an
        # outage, so it is not reported as "unavailable".
        "unsupported",
    }
    values = []
    for notice in notices:
        entry_id = _normalized_id(notice.get("entry_id"), "entry_id")
        provider = str(notice.get("provider") or "")
        status = str(notice.get("status") or "")
        reason = _notice_reason(notice.get("reason"))
        if entry_id is None or not provider or len(provider) > 80 or status not in allowed:
            raise ValueError("invalid memory forget provider notice")
        # The reason rides with the status it explains, so a read-back of the
        # row says why as well as what; NULL is the vocabulary's own "none".
        values.append((owner, entry_id, provider, status, reason))
    if not values:
        return
    async with pool.get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO memory_forget_provider_notices"
                    " (user_id, entry_id, provider, status, memfw_reason)"
                    " VALUES (%s,%s,%s,%s,%s)"
                    " ON CONFLICT (user_id, entry_id, provider) DO UPDATE"
                    " SET status = EXCLUDED.status,"
                    " memfw_reason = EXCLUDED.memfw_reason,"
                    " created_at = NOW()",
                    values,
                )


async def get_forget_provider_notices(
    *, user_id: str, entry_id: str
) -> list[dict[str, Any]]:
    """The provider notices one forget left behind, with the reasons they kept.

    ``record_forget_provider_notices`` writes what the federation said while
    it was still talking; this reads what the row holds afterwards. It is the
    read-back of the notice: owner-scoped like every other read here, so a
    caller can only ever reach the notices of its own forget, and bounded the
    same way because one entry has one notice per provider.
    """
    owner = refactor_owner_uuid(user_id)
    entry_uuid = _normalized_id(entry_id, "entry_id")
    if owner is None or entry_uuid is None:
        return []
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT provider, status, memfw_reason"
                " FROM memory_forget_provider_notices"
                " WHERE user_id = %s AND entry_id = %s"
                " ORDER BY provider LIMIT 257",
                (owner, entry_uuid),
            )
            return [dict(row) for row in await cur.fetchall()]


async def forget_entry_rows(
    *,
    user_id: str,
    rows: Sequence[Mapping[str, Any]],
    proposal_keys: Sequence[str] = (),
) -> dict[str, int]:
    """Erase rows only when their owner-scoped identities and hashes still match."""
    owner = refactor_owner_uuid(user_id)
    expected_hashes = {
        _normalized_id(row.get("entry_id"), "entry_id"): str(
            row.get("content_hash") or ""
        )
        for row in rows
    }
    ids = list(dict.fromkeys(filter(None, expected_hashes)))
    if owner is None or not ids:
        return {"entries": 0, "proposals": 0}
    async with pool.get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT entry_id::text, content_hash FROM memory_entries"
                    " WHERE user_id = %s AND entry_id = ANY(%s::uuid[]) FOR UPDATE",
                    (owner, ids),
                )
                live_rows = await cur.fetchall()
                if len(live_rows) != len(ids):
                    raise RuntimeError("memory forget set changed before deletion")
                if any(
                    expected_hashes.get(str(row[0])) != str(row[1])
                    for row in live_rows
                ):
                    raise RuntimeError("memory forget content changed before deletion")
                await cur.execute(
                    "DELETE FROM memory_entries WHERE user_id = %s"
                    " AND entry_id = ANY(%s::uuid[])",
                    (owner, ids),
                )
                deleted = int(cur.rowcount or 0)
                if deleted != len(ids):
                    raise RuntimeError("memory forget did not remove the full projection set")
                deleted_proposals = 0
                if proposal_keys:
                    normalized_proposal_keys = list(dict.fromkeys(proposal_keys))
                else:
                    normalized_proposal_keys = []
                # Proposals for forgotten entries include the explicit
                # proposal_key matches and any producer proposal that copied
                # these entries' bodies (source_entry_ids overlap); otherwise a
                # consolidation proposal keeps the forgotten plaintext visible.
                await cur.execute(
                    "SELECT proposal_key FROM agent_memory_proposals"
                    " WHERE user_id = %s AND ("
                    "   proposal_key = ANY(%s::text[])"
                    "   OR source_entry_ids && %s::uuid[]"
                    ") FOR UPDATE",
                    (owner, normalized_proposal_keys, ids),
                )
                proposal_rows = await cur.fetchall()
                await cur.execute(
                    "DELETE FROM agent_memory_proposals WHERE user_id = %s AND ("
                    "   proposal_key = ANY(%s::text[])"
                    "   OR source_entry_ids && %s::uuid[]"
                    ")",
                    (owner, normalized_proposal_keys, ids),
                )
                deleted_proposals = int(cur.rowcount or 0)
                if deleted_proposals != len(proposal_rows):
                    raise RuntimeError(
                        "memory forget did not remove the full proposal set"
                    )
                await cur.execute(
                    "SELECT 1 FROM agent_memory_proposals"
                    " WHERE user_id = %s AND ("
                    "   proposal_key = ANY(%s::text[])"
                    "   OR source_entry_ids && %s::uuid[]"
                    ") LIMIT 1",
                    (owner, normalized_proposal_keys, ids),
                )
                if await cur.fetchone() is not None:
                    raise RuntimeError("memory forget left matching proposals behind")
                return {"entries": deleted, "proposals": deleted_proposals}


async def find_successors(
    *,
    user_id: str,
    tier: str,
    entry_key: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Active rows whose metadata names ``entry_key`` as superseded.

    This is the projection-side half of a supersession chain; the predecessor
    side is the ``supersedes`` list on the row itself. Metadata is JSON (not
    JSONB) on the table, so cast at the containment check.
    """
    owner = refactor_owner_uuid(user_id)
    if owner is None:
        return []
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_MEMORY_COLUMNS} FROM memory_entries"
                " WHERE user_id = %s AND tier = %s AND status = 'active'"
                " AND COALESCE(metadata->>'lifecycle', 'active') = 'active'"
                " AND metadata::jsonb @> %s::jsonb"
                " AND entry_key NOT LIKE %s"
                " ORDER BY updated_at DESC LIMIT %s",
                (
                    owner,
                    tier,
                    SafeJson({"supersedes": [entry_key]}),
                    PROPOSED_PATTERN,
                    max(1, min(int(limit), 100)),
                ),
            )
            return [dict(row) for row in await cur.fetchall()]


async def count_entries(
    *,
    user_id: str,
    tier: str | None = None,
    status: str = "active",
    include_proposed: bool = False,
    workspace_id: str | None = None,
    thread_id: str | None = None,
) -> int:
    """Count projection rows for one owner/tier, excluding candidates by default."""
    owner = refactor_owner_uuid(user_id)
    if owner is None:
        return 0
    clauses = ["user_id = %s", "status = %s"]
    params: list[Any] = [owner, status]
    if status == "active":
        clauses.append("COALESCE(metadata->>'lifecycle', 'active') = 'active'")
    if tier is not None:
        clauses.append("tier = %s")
        params.append(tier)
    if workspace_id:
        # Workspace namespace: [owner, "workspaces", workspace_id, "memory"].
        clauses.append("namespace[3] = %s")
        params.append(workspace_id)
    if thread_id:
        # Session namespace: [owner, "threads", thread_id, "memory"].
        clauses.append("namespace[3] = %s")
        params.append(thread_id)
    if not include_proposed:
        clauses.append("entry_key NOT LIKE %s")
        params.append(PROPOSED_PATTERN)
    async with pool.get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM memory_entries WHERE " + " AND ".join(clauses),
                tuple(params),
            )
            row = await cur.fetchone()
            return int(row[0] if row else 0)


async def count_proposed_entries(*, user_id: str) -> int:
    owner = refactor_owner_uuid(user_id)
    if owner is None:
        return 0
    async with pool.get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM memory_entries"
                " WHERE user_id = %s AND status = 'active' AND entry_key LIKE %s",
                (owner, PROPOSED_PATTERN),
            )
            row = await cur.fetchone()
            return int(row[0] if row else 0)


async def supersede_proposed_entries(*, user_id: str) -> int:
    """Make legacy proposed rows non-active; approval only promotes real rows."""
    owner = refactor_owner_uuid(user_id)
    if owner is None:
        return 0
    async with pool.get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE memory_entries SET status = 'superseded', updated_at = NOW()"
                " WHERE user_id = %s AND status = 'active' AND entry_key LIKE %s",
                (owner, PROPOSED_PATTERN),
            )
            return cur.rowcount or 0


async def search_entries(
    *,
    user_id: str,
    query: str,
    tier: str | None = None,
    thread_id: str | None = None,
    workspace_id: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Retrieval over the indexed lines: FTS for latin, trigram for CJK.

    The split is by script, not by language setting: one predicate cannot serve
    both, because the FTS lexer keeps CJK runs whole while latin benefits from
    term AND + ranking. Both paths share the active/tier filters and the same
    result columns.
    """
    if limit < 1 or limit > 100:
        raise ValueError("memory search size must be between 1 and 100")
    text = (query or "").strip()
    if not text:
        return []
    if tier in {"session", "task"} and not thread_id:
        return []
    owner = refactor_owner_uuid(user_id)
    if owner is None:
        return []
    # Time validity is evaluated in Python to tolerate legacy/malformed JSON
    # metadata without unsafe SQL casts. Overfetch before applying the bound so
    # expired hits do not usually consume the caller's whole result window.
    candidate_limit = min(500, limit * 5)
    if has_cjk(text):
        rows = await _search_trigram(
            user_id=owner,
            text=text,
            tier=tier,
            limit=candidate_limit,
            thread_id=thread_id,
            workspace_id=workspace_id,
        )
    else:
        rows = await _search_fts(
            user_id=owner,
            text=text,
            tier=tier,
            limit=candidate_limit,
            thread_id=thread_id,
            workspace_id=workspace_id,
        )
    return [
        row
        for row in rows
        if is_current(row.get("metadata"))
        and normalize_memory_policy_metadata_layers(row.get("metadata"))["sensitivity"]
        != "secret"
    ][:limit]


async def supersede_entry(
    *,
    user_id: str,
    tier: str,
    entry_key: str,
) -> int:
    """Hide an active entry, retaining its ID when a superseded snapshot exists."""
    owner = refactor_owner_uuid(user_id)
    if owner is None:
        return 0
    normalized_key = strip_pg_nul_str(entry_key)
    async with pool.get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await _lock_entry_identity(
                    cur, owner=owner, tier=tier, entry_key=normalized_key
                )
                await cur.execute(
                    # Keep this entry ID when a backfill already made the separate
                    # superseded snapshot. Lifecycle metadata hides it from recall;
                    # the next active projection restores it without losing refs.
                    "UPDATE memory_entries AS active"
                    " SET status = CASE WHEN EXISTS ("
                    "       SELECT 1 FROM memory_entries AS prior"
                    "        WHERE prior.user_id = active.user_id"
                    "          AND prior.tier = active.tier"
                    "          AND prior.entry_key = active.entry_key"
                    "          AND prior.status = 'superseded'"
                    "     ) THEN active.status ELSE 'superseded' END,"
                    "     metadata = CASE WHEN EXISTS ("
                    "       SELECT 1 FROM memory_entries AS prior"
                    "        WHERE prior.user_id = active.user_id"
                    "          AND prior.tier = active.tier"
                    "          AND prior.entry_key = active.entry_key"
                    "          AND prior.status = 'superseded'"
                    "     ) THEN (COALESCE(active.metadata::jsonb, '{}'::jsonb)"
                    "               || '{\"lifecycle\":\"superseded\"}'::jsonb)::json"
                    "          ELSE active.metadata END,"
                    "     updated_at = NOW()"
                    " WHERE active.user_id = %s AND active.tier = %s"
                    "   AND active.entry_key = %s AND active.status = 'active'",
                    (owner, tier, normalized_key),
                )
                return int(cur.rowcount or 0)
