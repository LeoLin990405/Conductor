"""Migration 139: durable review records and the review/merge audit log.

Merge is a host-local, irreversible operation: it lands a real commit after a
Codex review verdict and a green test run. Both halves of that decision must
survive a restart, so 139 adds two tables rather than keeping the queue in
worktree-local files:

``autonomy_review_records`` — the cross-review queue and its verdict. Rows pin
the reviewed commit (``head_sha``) and the reviewer's provider, so a verdict
cannot be replayed against a different head. The queue is durable review
provenance only; the actual merge gate is the server-run Codex review on the
exact rehearsal tree, not a caller-asserted queue verdict.
``CHECK (line <> reviewer)`` makes self-review impossible at the storage layer
in addition to the route's same-directory check.

``autonomy_review_audit`` — append-only rows for review requests/verdicts and
every merge refusal, failure and result. A merge writes ``merge_started``
*before* it touches git and ``merge_merged`` after, so a crash in between
still leaves a durable record to reconcile against the target branch's history.
The detail JSONB holds structured fields (code, message, remediation, SHAs) and
never raw output or secret material.

``downgrade`` removes both tables for a redeploy of the previous build, but
refuses while a landed or in-flight merge row exists (``merge_started`` or
``merge_merged``): those rows are the only durable record that a real merge
landed (or may have landed), and silently deleting them on rollback would erase
exactly the history this migration exists to keep.

139 follows the current single head 138; no earlier revision is renumbered.
"""

from alembic import op

revision = "139"
down_revision = "138"
branch_labels = None
depends_on = None

_AUDIT_ACTIONS = (
    "'review_requested', 'review_completed', "
    "'merge_refused', 'merge_started', 'merge_merged', 'merge_failed'"
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("""
        CREATE TABLE IF NOT EXISTS autonomy_review_records (
            review_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id TEXT NOT NULL,
            line TEXT NOT NULL CHECK (char_length(line) BETWEEN 1 AND 200),
            reviewer TEXT NOT NULL CHECK (char_length(reviewer) BETWEEN 1 AND 200),
            reviewer_provider TEXT NOT NULL DEFAULT 'codex'
                CHECK (char_length(reviewer_provider) BETWEEN 1 AND 64),
            state TEXT NOT NULL DEFAULT 'queued'
                CHECK (state IN ('queued', 'completed', 'failed')),
            verdict TEXT CHECK (verdict IN ('merge', 'block')),
            head_sha TEXT NOT NULL DEFAULT ''
                CHECK (head_sha = '' OR head_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
            note TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at TIMESTAMPTZ,
            CONSTRAINT autonomy_review_records_not_self
                CHECK (line <> reviewer),
            CONSTRAINT autonomy_review_records_terminal
                CHECK (
                    (state = 'queued' AND verdict IS NULL AND completed_at IS NULL)
                    OR (state = 'completed' AND verdict IS NOT NULL
                        AND completed_at IS NOT NULL)
                    OR (state = 'failed' AND completed_at IS NOT NULL)
                )
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_autonomy_review_records_user_line
            ON autonomy_review_records (user_id, line, created_at DESC)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_autonomy_review_records_open
            ON autonomy_review_records (user_id, state, created_at DESC)
            WHERE state = 'queued'
    """)
    op.execute(f"""
        CREATE TABLE IF NOT EXISTS autonomy_review_audit (
            audit_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id TEXT NOT NULL,
            action TEXT NOT NULL CHECK (action IN ({_AUDIT_ACTIONS})),
            line TEXT NOT NULL DEFAULT '',
            reviewer TEXT NOT NULL DEFAULT '',
            target_branch TEXT NOT NULL DEFAULT '',
            outcome TEXT NOT NULL DEFAULT 'ok'
                CHECK (outcome IN ('ok', 'refused', 'failed')),
            detail JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_autonomy_review_audit_user
            ON autonomy_review_audit (user_id, created_at DESC)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_autonomy_review_audit_user_line
            ON autonomy_review_audit (user_id, line, created_at DESC)
    """)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS ("
        "SELECT 1 FROM autonomy_review_audit WHERE action IN "
        "('merge_started', 'merge_merged')"
        ") THEN "
        "RAISE EXCEPTION "
        "'review/merge audit holds merged rows; archive them before downgrading 139'; "
        "END IF; END $$"
    )
    op.execute("DROP TABLE IF EXISTS autonomy_review_audit")
    op.execute("DROP TABLE IF EXISTS autonomy_review_records")
