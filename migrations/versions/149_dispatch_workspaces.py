"""Migration 149: the dispatch workspace registry.

An auto-dispatch run executes against one git checkout. Until now the checkout
was whatever ``CONDUCTOR_DISPATCH_REPO`` named for the whole backend, so every
operator and every repository shared one path. A workspace row names a checkout
once: ``path`` is its repo root, ``git_remote`` the remote a ``git fetch``
should update, ``sandbox_policy`` the sandbox setting the workers inherit, and
``default_worker`` the provider a dispatch starts with when it does not name
one.

``name`` is unique per user, so ``conductor workspace show <name>`` is enough to
find a row; the UUID primary key is what a dispatch records and what the API
addresses.

The same migration copies the selected workspace onto the dispatch row. A live
run and its recovery must keep executing in the repository, policy and default
worker it started with: editing or deleting a registry row must not move work
that is already running. The columns are a snapshot, not a foreign key, for the
same reason.
"""

from alembic import op

revision = "149"
down_revision = "148"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dispatch_workspaces (
            workspace_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            path TEXT NOT NULL,
            git_remote TEXT NOT NULL DEFAULT '',
            sandbox_policy TEXT NOT NULL DEFAULT '',
            default_worker TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_dispatch_workspaces_user_name
            ON dispatch_workspaces (user_id, name)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dispatch_workspaces_user
            ON dispatch_workspaces (user_id, updated_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dispatch_workspaces_user_path
            ON dispatch_workspaces (user_id, path)
        """
    )
    op.execute(
        """
        ALTER TABLE dispatches
            ADD COLUMN IF NOT EXISTS workspace_id TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS repo_root TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS sandbox_policy TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS default_worker TEXT NOT NULL DEFAULT ''
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        ALTER TABLE dispatches
            DROP COLUMN IF EXISTS workspace_id,
            DROP COLUMN IF EXISTS repo_root,
            DROP COLUMN IF EXISTS sandbox_policy,
            DROP COLUMN IF EXISTS default_worker
        """
    )
    op.execute("DROP INDEX IF EXISTS idx_dispatch_workspaces_user_path")
    op.execute("DROP INDEX IF EXISTS idx_dispatch_workspaces_user")
    op.execute("DROP INDEX IF EXISTS uq_dispatch_workspaces_user_name")
    op.execute("DROP TABLE IF EXISTS dispatch_workspaces")
