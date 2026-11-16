"""Migration 148: the memory ontology columns ``class`` and ``about``.

The shared-memory ontology gives every memory two things the projection could
not hold before: a ``class`` -- what kind of claim the memory is (a
``Preference``, a ``Constraint``, a ``Lesson``) -- and an ``about`` -- the
concept id of the thing the memory is about, such as ``project:conductor``.
Both are nullable on purpose: a memory written before the ontology existed has
neither, and the projection never invents one for it.

The index on ``(class, about)`` is what a browse-by-concept page reads: a
class filter, an ``about`` filter, and the pair that names one concept.
"""

from alembic import op

revision = "148"
down_revision = "147"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        ALTER TABLE memory_entries
            ADD COLUMN IF NOT EXISTS class TEXT,
            ADD COLUMN IF NOT EXISTS about TEXT
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_memory_entries_class_about
            ON memory_entries (class, about)
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP INDEX IF EXISTS ix_memory_entries_class_about")
    op.execute(
        """
        ALTER TABLE memory_entries
            DROP COLUMN IF EXISTS class,
            DROP COLUMN IF EXISTS about
        """
    )
