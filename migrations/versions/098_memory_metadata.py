"""Migration 098: structured, optional memory metadata.

The Markdown body remains canonical. These fields (kind, importance,
confidence, valid_from/valid_until, tags, entities, supersedes, source) let
retrieval respect validity/authority and let the inspector show why a memory is
trusted. Every field is optional; existing rows read as an empty object.

Informed by Graphiti's bi-temporal edges (valid_at/invalid_at) and mem0's
extracted-fact metadata, without adopting either system's storage.
"""

from alembic import op
import sqlalchemy as sa

revision = "098"
down_revision = "097"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "memory_entries",
        sa.Column("metadata", sa.JSON(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("memory_entries", "metadata")
