"""Migration 097: optional embedding column for hybrid memory retrieval.

The projection remains the source of truth for index text and lifecycle; this
adds the vector payload used by the API-embedding path. ``JSONB`` is deliberate:
the local Postgres image does not ship pgvector, and a personal memory bank is
small enough to score candidates in Python. A later pgvector migration can move
the payload to a vector column without changing the write/search contract.

No value is computed here. Embeddings are filled lazily by the memory service
(and can be backfilled from the API) only when an embedding provider is
configured.
"""

from alembic import op
import sqlalchemy as sa

revision = "097"
down_revision = "096"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("memory_entries", sa.Column("embedding", sa.JSON(), nullable=True))
    op.add_column("memory_entries", sa.Column("embedding_model", sa.Text(), nullable=True))
    op.add_column(
        "memory_entries",
        sa.Column("embedding_content_hash", sa.Text(), nullable=True),
    )
    op.add_column(
        "memory_entries",
        sa.Column("embedding_updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("memory_entries", "embedding_updated_at")
    op.drop_column("memory_entries", "embedding_model")
    op.drop_column("memory_entries", "embedding")
