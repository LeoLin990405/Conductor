"""Give outbox replies a channel, a kind, and a human-approval state.

Article §4.2 asks for per-channel delivery and approval rules. A reply that
waits for a person to approve it needs a durable state of its own, and the
delivery path needs to know which channel the reply was written for — so the
row records the resolved channel and the kind of the reply.

``pending_approval`` is a reply that was drafted and is waiting for a person;
``rejected`` is one they read and did not want. Both are terminal for the
delivery effect: the effect only ever sends a ``pending`` row.
"""

from alembic import op

revision = "119"
down_revision = "118"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE autonomy_outbox ADD COLUMN IF NOT EXISTS "
        "channel TEXT NOT NULL DEFAULT ''"
    )
    op.execute(
        "ALTER TABLE autonomy_outbox ADD COLUMN IF NOT EXISTS "
        "reply_kind TEXT NOT NULL DEFAULT 'result'"
    )
    op.execute(
        "ALTER TABLE autonomy_outbox ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ"
    )
    op.execute(
        "ALTER TABLE autonomy_outbox ADD COLUMN IF NOT EXISTS "
        "approved_by TEXT NOT NULL DEFAULT ''"
    )
    op.execute(
        "ALTER TABLE autonomy_outbox DROP CONSTRAINT IF EXISTS autonomy_outbox_status_check"
    )
    op.execute(
        "ALTER TABLE autonomy_outbox ADD CONSTRAINT autonomy_outbox_status_check "
        "CHECK (status IN ('pending', 'sent', 'dropped', 'pending_approval', 'rejected'))"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # The narrow status check has no room for the approval states, so a reply
    # that is still waiting for a person is settled as dropped on the way down.
    op.execute(
        "UPDATE autonomy_outbox SET status = 'dropped', updated_at = NOW() "
        "WHERE status IN ('pending_approval', 'rejected')"
    )
    op.execute(
        "ALTER TABLE autonomy_outbox DROP CONSTRAINT IF EXISTS autonomy_outbox_status_check"
    )
    op.execute(
        "ALTER TABLE autonomy_outbox ADD CONSTRAINT autonomy_outbox_status_check "
        "CHECK (status IN ('pending', 'sent', 'dropped'))"
    )
    op.execute("ALTER TABLE autonomy_outbox DROP COLUMN IF EXISTS approved_by")
    op.execute("ALTER TABLE autonomy_outbox DROP COLUMN IF EXISTS approved_at")
    op.execute("ALTER TABLE autonomy_outbox DROP COLUMN IF EXISTS reply_kind")
    op.execute("ALTER TABLE autonomy_outbox DROP COLUMN IF EXISTS channel")
