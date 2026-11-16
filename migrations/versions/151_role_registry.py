"""Migration 151: the unified role registry (R6 L1, N2).

The four roles -- Super-agent (Router), planner, reviewer and worker -- used to
live in three places: ``config.yaml`` ``autonomy_router_model``,
``CONDUCTOR_DISPATCH_PLANNER`` and ``RolePolicy`` /
``CONDUCTOR_ROLE_POLICY_FILE``. This migration adds two tables and nothing
else:

``role_registry``        one row per deployment (``registry_id = 1``): the
                         registry document (roles, candidates, defaults,
                         constraints) and a revision for compare-and-swap.
                         No row means "use the legacy settings" -- the service
                         falls back to them, so an install that never edits the
                         registry behaves exactly as before.
``role_registry_audit``  append-only: one row per change with the actor, the
                         reason, the role and the role entry before and after.
``dispatch_assign_rollout`` one row: the N73 Q8 rollout stage of Super-agent
                         assignment (``shadow`` -> ``sampled`` -> ``full``,
                         forward only) and a sticky ``rolled_back`` flag, so a
                         sliding window of recent dispatches cannot move the
                         stage back or undo a rollback.

151 follows 150 and is the single head at the time this migration was written.
"""

from alembic import op

revision = "151"
down_revision = "150"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS role_registry (
            registry_id SMALLINT PRIMARY KEY DEFAULT 1
                CHECK (registry_id = 1),
            document JSONB NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
            updated_by TEXT NOT NULL DEFAULT ''
                CHECK (char_length(updated_by) <= 200),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS role_registry_audit (
            audit_id BIGSERIAL PRIMARY KEY,
            revision INTEGER NOT NULL CHECK (revision >= 1),
            role TEXT NOT NULL
                CHECK (role IN ('router', 'planner', 'reviewer', 'worker', '*')),
            actor TEXT NOT NULL DEFAULT ''
                CHECK (char_length(actor) <= 200),
            reason TEXT NOT NULL DEFAULT ''
                CHECK (char_length(reason) <= 2000),
            before JSONB,
            after JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_role_registry_audit_created "
        "ON role_registry_audit (created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_role_registry_audit_role "
        "ON role_registry_audit (role, created_at DESC)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dispatch_assign_rollout (
            rollout_id SMALLINT PRIMARY KEY DEFAULT 1
                CHECK (rollout_id = 1),
            stage TEXT NOT NULL
                CHECK (stage IN ('shadow', 'sampled', 'full')),
            rolled_back BOOLEAN NOT NULL DEFAULT FALSE,
            reasons TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP TABLE IF EXISTS dispatch_assign_rollout")
    op.execute("DROP TABLE IF EXISTS role_registry_audit")
    op.execute("DROP TABLE IF EXISTS role_registry")
