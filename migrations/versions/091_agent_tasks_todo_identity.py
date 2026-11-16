"""Migration 091: one todo task row per (thread, title).

TodoWrite posts a whole snapshot, not deltas. Reconciling that into the task
tree needs a stable identity: the model rewrites the list in place and gives
each item a ``content`` line, so ``(thread_id, title)`` is the identity a
snapshot can be matched against. The partial unique index below lets the sync
upsert update a row's status instead of duplicating it, and lets a title re-added
later reuse the same row.

Duplicates can only exist if a staging deployment enabled the task-tree flag
before this index existed; the dedupe keeps the earliest row per identity
(migration 090's tree is new, so there is no history to preserve beyond it).

Rollback: ``alembic downgrade 090``. Drops both indexes; rows are unaffected.

Note: migrations must stay runnable without importing the application stack, so
the title bound the service applies (500 chars) is not repeated here — a title
long enough to exceed the btree limit fails the insert, which is the same
contract as a service-side rejection.
"""

from alembic import op

revision = "091"
down_revision = "090"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # Keep the earliest row per (thread, title) todo identity; later copies are
    # snapshots of the same task under the pre-index writer.
    op.execute("""
        DELETE FROM agent_tasks a
        USING agent_tasks b
        WHERE a.source = 'todo'
          AND b.source = 'todo'
          AND a.thread_id = b.thread_id
          AND a.title = b.title
          AND a.created_at > b.created_at
    """)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_tasks_todo_identity
        ON agent_tasks (thread_id, title)
        WHERE source = 'todo'
    """)
    # The sync's cancellation sweep filters todo rows that are still open.
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_agent_tasks_thread_source_status
        ON agent_tasks (thread_id, source, status)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_agent_tasks_thread_source_status")
    op.execute("DROP INDEX IF EXISTS uq_agent_tasks_todo_identity")
