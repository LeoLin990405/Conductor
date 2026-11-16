"""Migration 138: durable owner-approved memory tool denials."""

from alembic import op

revision = "138"
down_revision = "137"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "CREATE TABLE memory_tool_denials ("
        " user_id UUID NOT NULL,"
        " entry_id UUID NOT NULL,"
        " denied_tools JSONB NOT NULL,"
        " approved_content_hash TEXT NOT NULL,"
        " valid_from TIMESTAMPTZ,"
        " valid_until TIMESTAMPTZ,"
        " approval_source TEXT NOT NULL DEFAULT 'manual',"
        " active BOOLEAN NOT NULL DEFAULT TRUE,"
        " approved_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),"
        " PRIMARY KEY (user_id, entry_id),"
        " CONSTRAINT ck_memory_tool_denials_nonempty_array CHECK ("
        "  jsonb_typeof(denied_tools) = 'array' AND"
        "  jsonb_array_length(denied_tools) BETWEEN 1 AND 64),"
        " CONSTRAINT ck_memory_tool_denials_hash CHECK ("
        "  approved_content_hash ~ '^sha256:[0-9a-f]{64}$'),"
        " CONSTRAINT ck_memory_tool_denials_window CHECK ("
        "  valid_from IS NULL OR valid_until IS NULL OR valid_from < valid_until),"
        " CONSTRAINT ck_memory_tool_denials_manual CHECK ("
        "  approval_source = 'manual')"
        ")"
    )
    op.execute(
        "CREATE INDEX ix_memory_tool_denials_active_owner "
        "ON memory_tool_denials (user_id, entry_id) WHERE active IS TRUE"
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM memory_tool_denials WHERE active IS TRUE) THEN "
        "RAISE EXCEPTION 'retire active memory tool denials before downgrading 138'; "
        "END IF; END $$"
    )
    op.execute("DROP TABLE IF EXISTS memory_tool_denials")
