"""Migration 099: first-class native-runtime binding for automations.

Automations previously bound a native runtime only through free-form metadata.
These columns make the choice explicit and let the executor stamp the same
thread metadata that POST /threads writes.
"""

from alembic import op
import sqlalchemy as sa

revision = "099"
down_revision = "098"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "automations", sa.Column("native_provider", sa.String(32), nullable=True)
    )
    op.add_column("automations", sa.Column("native_cwd", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("automations", "native_cwd")
    op.drop_column("automations", "native_provider")
