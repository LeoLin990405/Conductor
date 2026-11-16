"""Migration 147: per-Ticket worktree path, attempt/generation, and cleanup retry.

Each Ticket owns a linked git worktree. The path must survive across workers, the
attempt/generation fences a stale cleanup against a newer bind of the same path,
and the cleanup-pending columns make a failed remove durable for the scheduler
retry sweep (attempts + next-retry time) instead of a mandate JSON flag.

The binding itself (branch, durable base_sha, base branch, repo, which host
owns the checkout, and the daemon lease that bound it) lives in columns too, so
a SQL fence can compare it and a whole-mandate write cannot drop it.
worktree_base_sha is written once and survives cleanup: a recreated checkout
starts from the same commit.

worktree_stale_cleanups holds the daemon-host bindings that a new owner
replaced after the old daemon lost the Ticket (its lease gone): one note per
replaced checkout ({daemon_id, worktree_path, worktree_generation, lease_id}),
so the old daemon's later cleanup report can still be matched and settled.
"""

from alembic import op

revision = "147"
down_revision = "146"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        ALTER TABLE autonomy_tickets
            ADD COLUMN IF NOT EXISTS worktree_path TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS worktree_generation INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS worktree_cleanup_pending BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS worktree_cleanup_attempts INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS worktree_cleanup_next_retry_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS worktree_branch TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS worktree_base_sha TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS worktree_base_branch TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS worktree_repo TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS worktree_host TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS worktree_daemon_id UUID,
            ADD COLUMN IF NOT EXISTS worktree_lease_id UUID,
            ADD COLUMN IF NOT EXISTS worktree_stale_cleanups JSONB NOT NULL DEFAULT '[]'::jsonb
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        ALTER TABLE autonomy_tickets
            DROP COLUMN IF EXISTS worktree_stale_cleanups,
            DROP COLUMN IF EXISTS worktree_lease_id,
            DROP COLUMN IF EXISTS worktree_daemon_id,
            DROP COLUMN IF EXISTS worktree_host,
            DROP COLUMN IF EXISTS worktree_repo,
            DROP COLUMN IF EXISTS worktree_base_branch,
            DROP COLUMN IF EXISTS worktree_base_sha,
            DROP COLUMN IF EXISTS worktree_branch,
            DROP COLUMN IF EXISTS worktree_cleanup_next_retry_at,
            DROP COLUMN IF EXISTS worktree_cleanup_attempts,
            DROP COLUMN IF EXISTS worktree_cleanup_pending,
            DROP COLUMN IF EXISTS worktree_generation,
            DROP COLUMN IF EXISTS worktree_path
        """
    )
