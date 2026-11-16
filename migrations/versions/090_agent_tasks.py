"""Migration 090: a persisted task tree for main-agent work.

Today the main agent's todo list is emitted as ``todo_update`` SSE frames only;
the database has no durable task tree, while ``subagent_tasks`` covers delegated
work only. Phase 1 adds ``agent_tasks`` as the single tree the UI, the audit and
the differential harness read: rows from the TodoWrite middleware (``source =
'todo'``) and mirror rows for delegated work (``source = 'subagent'``, linked by
``subagent_task_ref`` rather than duplicating that table's run state).

Status and source vocabularies are mirror-checked against
``src/server/contracts/refactor.py`` by the unit suite; they are spelled out here
because migrations must run without importing the application stack.

Each statement commits on its own, so an upgrade cut short leaves some of them
applied with 089 still recorded: run it again, which is safe.

Rollback: ``alembic downgrade 089``. Drops the tree (Phase-1-only addition);
``subagent_tasks`` keeps owning delegated-run state.
"""

from alembic import op

revision = "090"
down_revision = "089"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("""
        CREATE TABLE IF NOT EXISTS agent_tasks (
            task_node_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL,
            thread_id UUID NOT NULL
                REFERENCES conversation_threads(conversation_thread_id)
                ON DELETE CASCADE,
            parent_node_id UUID
                REFERENCES agent_tasks(task_node_id)
                ON DELETE CASCADE,
            title TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'in_progress', 'completed', 'blocked', 'cancelled')),
            source TEXT NOT NULL DEFAULT 'todo'
                CHECK (source IN ('todo', 'subagent')),
            -- For source='subagent', the (thread_id, task_id) pair from
            -- subagent_tasks. No FK: that row can be pruned while the tree keeps
            -- the history of what the turn did.
            subagent_task_ref TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    # Tree rendering and status filters per thread.
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_agent_tasks_thread_status
        ON agent_tasks (thread_id, status)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_agent_tasks_parent
        ON agent_tasks (thread_id, parent_node_id)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS agent_tasks")
