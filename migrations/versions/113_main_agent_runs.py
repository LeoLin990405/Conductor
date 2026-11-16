"""Store main-agent orchestration runs and watchdog alerts."""

from alembic import op

revision = "113"
down_revision = "112"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE main_agent_runs (
            run_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            revision BIGINT NOT NULL DEFAULT 0 CHECK (revision >= 0),
            payload JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CHECK (payload->>'id' = run_id),
            CHECK (payload->>'user_id' = user_id),
            CHECK ((payload->>'revision')::BIGINT = revision)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_main_agent_runs_user_updated "
        "ON main_agent_runs (user_id, updated_at DESC, run_id)"
    )
    op.execute(
        "CREATE INDEX idx_main_agent_runs_active_updated "
        "ON main_agent_runs (updated_at DESC, run_id) "
        "WHERE payload->>'status' IN ('queued', 'running')"
    )
    op.execute(
        """
        CREATE TABLE main_agent_alerts (
            run_id TEXT NOT NULL REFERENCES main_agent_runs(run_id) ON DELETE CASCADE,
            task_id TEXT NOT NULL,
            alert_key TEXT NOT NULL,
            user_id TEXT NOT NULL,
            payload JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (run_id, task_id, alert_key)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_main_agent_alerts_user_created "
        "ON main_agent_alerts (user_id, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS main_agent_alerts")
    op.execute("DROP TABLE IF EXISTS main_agent_runs")
