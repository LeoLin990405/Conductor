"""Persist auto-dispatch runs and their human approval gate.

One row per dispatch, with the lines and outcomes kept as JSONB: the running
orchestrator is the only writer, and every other surface (the CLI, the
read-only API, the deploy preflight) reads the row it last saved.
"""

from alembic import op

revision = "121"
down_revision = "120"
branch_labels = None
depends_on = None

_STATES = (
    "SUBMITTED",
    "PLANNING",
    "ASSIGNED",
    "LAUNCHING",
    "MONITORING",
    "REVIEW",
    "APPROVAL",
    "MERGE",
    "COMPLETE",
    "ABORT",
    "ERROR",
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    states = ", ".join(f"'{state}'" for state in _STATES)
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS dispatches (
            dispatch_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id TEXT NOT NULL,
            goal TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'SUBMITTED'
                CHECK (state IN ({states})),
            integrate_branch TEXT NOT NULL DEFAULT '',
            approval_token TEXT,
            lines JSONB NOT NULL DEFAULT '[]'::jsonb,
            outcomes JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            message TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_dispatches_user_state "
        "ON dispatches (user_id, state, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_dispatches_state_created "
        "ON dispatches (state, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_dispatches_state_created")
    op.execute("DROP INDEX IF EXISTS idx_dispatches_user_state")
    op.execute("DROP TABLE IF EXISTS dispatches")
