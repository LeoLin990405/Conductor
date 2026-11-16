"""Persist production adapter identity for Main Agent deliberation participants."""

from alembic import op

revision = "117"
down_revision = "116"
branch_labels = None
depends_on = None

_CONSTRAINT = "autonomy_deliberation_participants_adapter_check"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(f"""
        ALTER TABLE autonomy_deliberation_participants
            DROP CONSTRAINT IF EXISTS {_CONSTRAINT},
            ADD CONSTRAINT {_CONSTRAINT}
                CHECK (adapter IN ('mock', 'cli', 'llm', 'daemon')) NOT VALID
    """)
    op.execute(
        f"ALTER TABLE autonomy_deliberation_participants VALIDATE CONSTRAINT {_CONSTRAINT}"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(f"""
        ALTER TABLE autonomy_deliberation_participants
            DROP CONSTRAINT IF EXISTS {_CONSTRAINT},
            ADD CONSTRAINT {_CONSTRAINT}
                CHECK (adapter IN ('mock', 'cli')) NOT VALID
    """)
    op.execute(
        f"ALTER TABLE autonomy_deliberation_participants VALIDATE CONSTRAINT {_CONSTRAINT}"
    )
