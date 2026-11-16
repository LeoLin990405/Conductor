"""Migration 141: Stage-Gate snapshots and the SPEC reference on a dispatch.

Wave 2 runs each auto-dispatch line through the Stage-Gate and archives the
SPEC the planner wrote. Both belong to the dispatch row, beside ``lines`` and
``outcomes``, so a state transition stays one UPDATE:

``dispatches.gates`` -- JSONB, the Stage-Gate ``to_dict()`` snapshot of every
line, keyed by line id. ``'{}'`` for a dispatch that has not reached a gate,
and for every row written before 141.

``dispatches.spec_ref`` -- TEXT, the reference to the archived SPEC. ``''``
when there is none.

Both columns are additive with defaults, so rows that exist keep loading.
``downgrade`` drops them; the snapshots and references they hold are lost.

141 follows the current single head 140; no earlier revision is renumbered.
"""

from alembic import op

revision = "141"
down_revision = "140"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE dispatches "
        "ADD COLUMN IF NOT EXISTS gates JSONB NOT NULL DEFAULT '{}'::jsonb"
    )
    op.execute(
        "ALTER TABLE dispatches "
        "ADD COLUMN IF NOT EXISTS spec_ref TEXT NOT NULL DEFAULT ''"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE dispatches DROP COLUMN IF EXISTS spec_ref")
    op.execute("ALTER TABLE dispatches DROP COLUMN IF EXISTS gates")
