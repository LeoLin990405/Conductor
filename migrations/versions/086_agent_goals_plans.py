"""Migration 086: goals and plans for the agent runtime.

The deep refactor makes "what is this thread trying to finish" explicit. A goal
names an objective, the stop conditions a verifier checks, its budget and its
status; a plan is a versioned, reviewable step list attached to a goal. Both are
new tables layered on top of ``conversation_threads`` — no existing table is
altered, so an old reader (one that never selects them) is unaffected.

Status transitions are not enforced by the database; they are decided in
``src/server/contracts/refactor.py`` (the one vocabulary both this CHECK and the
service layer must agree on). The CHECKs here pin the values, not the moves: a
late event that would move a completed goal is rejected by the transition
helper, not by a trigger, because the helper can say *why*.

Each statement commits on its own, so an upgrade cut short leaves some of them
applied with 085 still recorded: run it again, which is safe.

Rollback: ``alembic downgrade 085``. Both tables are new; dropping them loses
goal/plan rows only.
"""

from alembic import op

revision = "086"
down_revision = "085"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # New empty tables, but ``agent_goals`` takes a FK lock on the hot
    # ``conversation_threads`` table; fail fast rather than queue every writer.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("""
        CREATE TABLE IF NOT EXISTS agent_goals (
            goal_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL,
            thread_id UUID NOT NULL
                REFERENCES conversation_threads(conversation_thread_id)
                ON DELETE CASCADE,
            workspace_id UUID,
            title TEXT NOT NULL DEFAULT '',
            objective TEXT NOT NULL DEFAULT '',
            stop_conditions JSONB NOT NULL DEFAULT '[]'::jsonb,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'paused', 'completed', 'cancelled', 'blocked')),
            budget JSONB NOT NULL DEFAULT '{}'::jsonb,
            verifier_config JSONB NOT NULL DEFAULT '{}'::jsonb,
            verifier_verdict JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at TIMESTAMPTZ
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_agent_goals_thread
        ON agent_goals (thread_id, status)
    """)
    # One live goal per thread. History is kept: cancellations and completions
    # leave the partial index, so a thread can start a new goal later.
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_goals_active
        ON agent_goals (thread_id)
        WHERE status IN ('active', 'paused')
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS agent_plans (
            plan_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            goal_id UUID NOT NULL
                REFERENCES agent_goals(goal_id)
                ON DELETE CASCADE,
            version INTEGER NOT NULL,
            steps JSONB NOT NULL DEFAULT '[]'::jsonb,
            status TEXT NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft', 'awaiting_approval', 'approved', 'rejected', 'superseded')),
            feedback TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            decided_at TIMESTAMPTZ,
            UNIQUE (goal_id, version)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS agent_plans")
    op.execute("DROP TABLE IF EXISTS agent_goals")
