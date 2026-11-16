"""Migration 093: a per-thread permission mode.

D3 (docs/refactor/0008) unified the approval policy over the checks that already
exist. The one behaviour this layer can safely change today is whether the
user's standing approvals (hand-offs, workspace creation) are honoured: that is
the existing `StandingApprovalMiddleware` path, and turning it off for a thread
is strictly stricter than asking.

`auto` (honour the user's standing approvals) is the current behaviour and the
default; `manual` asks for every approval card regardless of the user setting.
`skip` is not a legal value: suppressing approval cards safely needs an
approval-capable gate that does not exist yet (see 0008).

Each statement commits on its own, so an upgrade cut short leaves the table
applied with 092 still recorded: run it again, which is safe.

Rollback: ``alembic downgrade 092``. Drops the table; missing rows mean the
default mode on read.
"""

from alembic import op

revision = "093"
down_revision = "092"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("""
        CREATE TABLE IF NOT EXISTS agent_thread_permissions (
            thread_id UUID PRIMARY KEY
                REFERENCES conversation_threads(conversation_thread_id)
                ON DELETE CASCADE,
            mode TEXT NOT NULL DEFAULT 'auto'
                CHECK (mode IN ('manual', 'auto')),
            updated_by TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS agent_thread_permissions")
