"""Widen ticket-event actions for forced stops and mandate refusal.

``stop_acknowledged`` records an operator force that does not change the
ticket status. ``mandate_revoked`` records a resume or reassign that stayed
cancelled because the mandate no longer holds. The previous check would
reject both, so the audit write could not commit with the stamp.

``downgrade`` deletes those rows before restoring the old check. The previous
build cannot store them, and mapping them onto ``stopped`` would claim a
status change that did not happen.
"""

from alembic import op

revision = "132"
down_revision = "131"
branch_labels = None
depends_on = None

_ACTIONS = (
    "'claimed', 'stopped', 'resumed', 'reassigned', 'archived', "
    "'stop_acknowledged', 'mandate_revoked'"
)
_PREVIOUS_ACTIONS = "'claimed', 'stopped', 'resumed', 'reassigned', 'archived'"
_CHECK = "autonomy_ticket_events_action_check"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        f"ALTER TABLE autonomy_ticket_events DROP CONSTRAINT IF EXISTS {_CHECK}"
    )
    op.execute(
        f"""
        ALTER TABLE autonomy_ticket_events
            ADD CONSTRAINT {_CHECK}
            CHECK (action IN ({_ACTIONS}))
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        DELETE FROM autonomy_ticket_events
        WHERE action IN ('stop_acknowledged', 'mandate_revoked')
        """
    )
    op.execute(
        f"ALTER TABLE autonomy_ticket_events DROP CONSTRAINT IF EXISTS {_CHECK}"
    )
    op.execute(
        f"""
        ALTER TABLE autonomy_ticket_events
            ADD CONSTRAINT {_CHECK}
            CHECK (action IN ({_PREVIOUS_ACTIONS}))
        """
    )
