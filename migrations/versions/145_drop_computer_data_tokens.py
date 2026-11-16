"""Migration 145: drop the computer_data_tokens table.

The market-data token chain is gone from this build (W10b-1): nothing under
``src/`` mints, keeps, revokes or reads a computer's data token any more. The
server no longer writes a token into any sandbox, the background sweep that
kept running sandboxes supplied never starts, and the in-sandbox client no
longer presents a data token to anything. What the chain left in the database
is the one table 076 created and 080 widened:

* ``computer_data_tokens`` (076, widened by 080) -- the row each worker knew
  of a computer's market-data token: when the token in the sandbox ran out
  (``expires_at``), which sandbox held it (``held_by``), the fencing
  ``generation``, the sweep's ``attempted_at``, and -- 080's additions -- the
  still-owed revocation a stop recorded (``revoke_until``,
  ``revoke_generation``, ``revoke_users``, ``revoke_attempted_at``,
  ``tokens_until``).

080 created no revocations table of its own: the revocation travels in those
five columns on this same row, so this revision has one table to drop and
nothing else.

**The data is destroyed and cannot be restored.** No other table holds a copy
of a token row, nothing is archived before the drop, and no backup is taken
here. Every row is short-lived by nature -- a token expires within the hour
and a revocation is owed only until its last token runs out -- but a row that
still carried an owed revocation at upgrade time is lost with the table, and
the build no longer has anything that could settle it anyway. A ``downgrade``
recreates the table *empty*: it restores the shape the previous head (144)
had -- 076's table plus 080's five columns, spelled out verbatim so the
restored table refuses nothing the old one accepted -- and none of the rows,
so a rolled-back database is a working schema whose token history has
vanished. A rollback that needs those rows restores a backup taken before
this migration; it cannot rebuild them from nothing.

It is safe to drop because this build no longer references it: no module
under ``src/`` imports ``src.server.database.computer_data_tokens``, and the
services that read and wrote the table (``services/data_tokens.py``,
``services/data_token_sweep.py``) are deleted in the same change. The
historical migrations 076 and 080 stay in the chain untouched: a fresh
database still walks through them, and this revision removes what they
created.

The drop runs in one transaction. The table holds a foreign key to
``computers`` (ON DELETE CASCADE); ``computers`` stays, and nothing left in
the database references ``computer_data_tokens``, so the drop is a leaf and
takes only its own indexes with it. ``IF EXISTS`` makes a re-run against a
database where the table is already gone a no-op rather than a failure. The
downgrade is symmetric: ``IF NOT EXISTS`` throughout, so re-running it, or
running it where the table never left, changes nothing.

145 follows the single head 144; no earlier revision is renumbered.
"""

from alembic import op

revision = "145"
down_revision = "144"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP TABLE IF EXISTS computer_data_tokens")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # 076's table as 080 left it -- the shape the previous head had -- empty.
    op.execute("""
        CREATE TABLE IF NOT EXISTS computer_data_tokens (
            computer_id UUID PRIMARY KEY
                REFERENCES computers(computer_id) ON DELETE CASCADE,
            user_id VARCHAR(255) NOT NULL,
            expires_at TIMESTAMPTZ,
            held_by VARCHAR(255),
            generation BIGINT NOT NULL DEFAULT 1,
            attempted_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    # 080's columns, in its order.
    op.execute("""
        ALTER TABLE computer_data_tokens
            ADD COLUMN IF NOT EXISTS revoke_until TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS revoke_generation BIGINT,
            ADD COLUMN IF NOT EXISTS revoke_users TEXT[],
            ADD COLUMN IF NOT EXISTS revoke_attempted_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS tokens_until TIMESTAMPTZ
    """)
