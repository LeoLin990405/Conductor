"""Route Sources and recurring schedules to registered local daemons."""

from alembic import op

revision = "110"
down_revision = "109"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE autonomy_schedules ADD COLUMN IF NOT EXISTS "
        "worker_daemon_id UUID REFERENCES autonomy_daemons(daemon_id) ON DELETE RESTRICT"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_schedules_worker_daemon "
        "ON autonomy_schedules (worker_daemon_id) WHERE worker_daemon_id IS NOT NULL"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION autonomy_source_daemon_owner_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE assigned_id UUID;
        BEGIN
            IF COALESCE(NEW.config->>'worker_daemon_id', '') = '' THEN
                RETURN NEW;
            END IF;
            BEGIN
                assigned_id := (NEW.config->>'worker_daemon_id')::UUID;
            EXCEPTION WHEN invalid_text_representation THEN
                RAISE EXCEPTION 'invalid worker_daemon_id in autonomy source'
                    USING ERRCODE = '23503';
            END;
            PERFORM 1 FROM autonomy_daemons
            WHERE daemon_id = assigned_id AND user_id = NEW.user_id
            FOR KEY SHARE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'autonomy source daemon must belong to its user'
                    USING ERRCODE = '23503';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER autonomy_source_daemon_owner_guard
        BEFORE INSERT OR UPDATE OF config, user_id ON autonomy_sources
        FOR EACH ROW EXECUTE FUNCTION autonomy_source_daemon_owner_guard()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION autonomy_schedule_daemon_owner_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.worker_daemon_id IS NULL THEN
                RETURN NEW;
            END IF;
            PERFORM 1 FROM autonomy_daemons
            WHERE daemon_id = NEW.worker_daemon_id AND user_id = NEW.user_id
            FOR KEY SHARE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'autonomy schedule daemon must belong to its user'
                    USING ERRCODE = '23503';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER autonomy_schedule_daemon_owner_guard
        BEFORE INSERT OR UPDATE OF worker_daemon_id, user_id ON autonomy_schedules
        FOR EACH ROW EXECUTE FUNCTION autonomy_schedule_daemon_owner_guard()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS autonomy_source_daemon_owner_guard ON autonomy_sources"
    )
    op.execute("DROP FUNCTION IF EXISTS autonomy_source_daemon_owner_guard()")
    op.execute(
        "DROP TRIGGER IF EXISTS autonomy_schedule_daemon_owner_guard ON autonomy_schedules"
    )
    op.execute("DROP FUNCTION IF EXISTS autonomy_schedule_daemon_owner_guard()")
    op.execute("DROP INDEX IF EXISTS idx_autonomy_schedules_worker_daemon")
    op.execute("ALTER TABLE autonomy_schedules DROP COLUMN IF EXISTS worker_daemon_id")
