"""Migration 092: trigram retrieval for CJK memory index lines.

The live Postgres retrieval probe (docs/refactor/0005) measured the projection's
FTS path: latin queries retrieve (5/6 cases, the miss is ``simple`` having no
stemming), but **CJK retrieves nothing** — the lexer keeps a contiguous Chinese
run as one token, so a query can never match a longer line, spaced or not.

This migration adds the retrieval path for that half: ``pg_trgm`` and a GIN
trigram index over the same text the FTS vector covers. The search service keeps
FTS for latin queries and uses this index for queries containing CJK (contains
predicate for whitespace-separated terms, trigram similarity for a single
unspaced run). A trigram index is chosen over a tokenizer extension because the
deployment image does not ship one; ``pg_trgm`` is a trusted extension (1.6 on
the target Postgres) and needs no dictionary or segmenter.

The extension is created because the index type requires it; downgrade drops the
indexes but **keeps the extension** — an extension is database-wide and another
consumer (or a later re-upgrade) may rely on it.

Each statement commits on its own, so an upgrade cut short leaves some applied
with 091 still recorded: run it again, which is safe.

Rollback: ``alembic downgrade 091``. Drops the trigram index; retrieval falls
back to FTS (and CJK recall returns to zero, as measured).
"""

from alembic import op

revision = "092"
down_revision = "091"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    # One expression index for the combined text: the service splits a CJK query
    # into contains terms and a similarity probe, both over key + index line.
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_memory_entries_trgm
        ON memory_entries
        USING gin ((entry_key || ' ' || index_text) gin_trgm_ops)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_memory_entries_trgm")
    # pg_trgm is left installed on purpose: database-wide, possibly shared.
