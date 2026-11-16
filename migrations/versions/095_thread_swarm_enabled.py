"""Migration 086: each thread says whether Swarm mode is on.

``conversation_threads.swarm_enabled`` is the user's Swarm switch for the
thread: the turn-level mode that asks the agent to decompose work into parallel
subagent tasks. Off by default; a thread with no value runs as every thread did
before Swarm existed.

A plain nullable-free boolean with a default means the ALTER rewrites no rows on
PostgreSQL 11+. The column is read once per turn to inject the Swarm directive,
never on the model-call hot path.

Rollback: ``alembic stamp 085`` first if the previous build cannot start from a
revision it does not ship, then redeploy it; it never reads the column.
``downgrade`` drops the column and with it every thread's Swarm switch.
"""

from alembic import op

revision = "095"
down_revision = "094"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        op.execute(
            "ALTER TABLE conversation_threads "
            "ADD COLUMN IF NOT EXISTS swarm_enabled BOOLEAN NOT NULL DEFAULT FALSE"
        )
        op.execute("RESET lock_timeout")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE conversation_threads DROP COLUMN IF EXISTS swarm_enabled"
    )
