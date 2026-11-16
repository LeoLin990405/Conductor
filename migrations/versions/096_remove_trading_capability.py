"""Migration 096: remove the order ledger and trading-permission tables.

This build has no trading capability: no order is placed, changed, cancelled,
confirmed, staged or reconciled, and no trading-permission level is stored or
read. The three tables that only existed for that capability are dropped:

* ``order_attempts`` -- the per-order ledger, which also carried the order
  wakeup's baseline and outcome;
* ``trading_permission_events`` -- the append-only agreement audit;
* ``trading_permissions`` -- the per-user level above a connection's approval
  switches.

Armed order wakeups reference an attempt id, so they are deleted first; a
wakeup watching an order that can no longer be placed or read back would sit
armed until its seven-day expiry. The ``wakeups`` columns those rows used are
left in place (all nullable, no longer written or read) so this migration does
not have to touch 082's check constraints; the table itself is still the same
one a timer or price wakeup uses.

``user_mcp_servers.order_approval`` also goes: it stored the per-connection
approval switches that only an order tool could read. It is dropped as a plain
column with no rows rewritten, and no reader in this build names it.

The older migrations (043 and 061) stay in the chain untouched: a fresh
database still walks through them, and this one removes what they created.

Downgrade is deliberately a no-op. Recreating empty tables would restore the
schema but not the orders, attempts or agreements they held, and an empty
``order_attempts`` would look to an older build like a working ledger whose
history had vanished. A rollback that needs those tables must restore a backup
taken before this migration, not rebuild them empty.
"""

from alembic import op

revision = "096"
down_revision = "095"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Order wakeups first: attempt_id is not a foreign key, so the drops below
    # would otherwise leave armed rows pointing at nothing.
    op.execute("DELETE FROM wakeups WHERE kind = 'order'")
    op.execute("DROP INDEX IF EXISTS idx_wakeups_armed_order")
    op.execute("DROP TABLE IF EXISTS trading_permission_events")
    op.execute("DROP TABLE IF EXISTS trading_permissions")
    op.execute("DROP TABLE IF EXISTS order_attempts CASCADE")
    op.execute(
        "ALTER TABLE user_mcp_servers DROP COLUMN IF EXISTS order_approval"
    )


def downgrade() -> None:
    # No-op on purpose: see the module docstring. The dropped tables held the
    # user's actual order attempts and agreement evidence, which cannot be
    # reconstructed; recreating them empty would misrepresent both.
    pass
