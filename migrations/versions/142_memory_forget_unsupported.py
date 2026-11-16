"""Migration 142: a forget notice may say that a provider does not support it.

A provider notice records what happened to one copy of a forgotten memory.
The vocabulary it could carry was ``deleted``, ``read_only``, ``unavailable``
and ``not_configured``. ``unavailable`` says that a copy could not be erased
*right now*, which is what an outage looks like; it does not say that the
provider has no such operation at all.

The remote MemFW of contract v2 is exactly that case: it offers ``/search``,
``/stage`` and ``/related``, and no forget endpoint whatsoever. Reporting it
as ``unavailable`` would have told the operator that a remote copy might
still be there, when the truth is that the remote service never keeps a
per-entry copy this client could erase.

``unsupported`` says that, and this revision widens the CHECK on
``memory_forget_provider_notices.status`` to hold it. The rows that already
exist keep their status: the new value is one the old vocabulary did not
have, not a replacement for one it had.

The status says *that* the provider cannot forget; it does not say *why*.
The Planner's ruling on the notice is a reason of its own, and this revision
adds ``memfw_reason`` to hold it beside the status it explains. It is
nullable on purpose: the write path keeps the notice even when the ruling it
carries is outside the vocabulary, and stores NULL rather than dropping the
notice or failing the forget. Its CHECK is the *forget* vocabulary, spelled
out verbatim, so a database that has applied this revision refuses a reason
no forget ever makes.

The vocabulary is the forget one, and it is small on purpose:
``no_forget_endpoint`` -- the remote MemFW has no forget endpoint, which is
what contract v2 is; ``not_supported`` -- the remote says so itself;
``remote_rejected`` -- the remote refused. It is emphatically *not* the
contract's ``/related`` reasons, ``link`` and ``lexical`` and the rest. Those
say why one memory sits next to another; they have nothing to do with a
forget, and a CHECK admitting them would accept a ruling no forget ever
makes while refusing the one this whole revision exists to record.

``downgrade`` is a redeploy of the previous build, and the previous build
cannot read ``unsupported``. So the rows that carry it become ``unavailable``
-- the nearest status the old vocabulary has -- *before* the old CHECK is
restored. The order is the whole point: restoring the CHECK first would
fail on the rows that carry the new status.

The column goes *first* of all: dropping it drops the reason CHECK with it,
so the rewrite below is not constrained by the new vocabulary. Restoring the
status CHECK before that rewrite is the one order this revision must not be
written in.

The check constraint of migration 133 is not touched; 133 stays as it was
written.

142 follows the single head 141; no earlier revision is renumbered.
"""

from alembic import op

revision = "142"
down_revision = "141"
branch_labels = None
depends_on = None

_STATUS_CHECK = "memory_forget_provider_notices_status_check"
_REASON_CHECK = "memory_forget_provider_notices_reason_check"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(f"""
        ALTER TABLE memory_forget_provider_notices
            DROP CONSTRAINT IF EXISTS {_STATUS_CHECK},
            ADD CONSTRAINT {_STATUS_CHECK} CHECK (status IN (
                'deleted', 'read_only', 'unavailable', 'not_configured',
                'unsupported'
            ))
    """)
    # The ruling rides with the notice it explains, one column beside the
    # status, so a read-back of the row says why as well as what.
    op.execute(
        "ALTER TABLE memory_forget_provider_notices"
        " ADD COLUMN IF NOT EXISTS memfw_reason TEXT"
    )
    # The vocabulary is the forget one, spelled verbatim; it is not this
    # revision's to widen. Only a code reaches here -- the write path maps the
    # federation's wording onto one of these three before it binds it.
    op.execute(f"""
        ALTER TABLE memory_forget_provider_notices
            DROP CONSTRAINT IF EXISTS {_REASON_CHECK},
            ADD CONSTRAINT {_REASON_CHECK} CHECK (
                memfw_reason IS NULL OR memfw_reason IN (
                    'no_forget_endpoint', 'not_supported', 'remote_rejected'
                )
            )
    """)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # The column goes first: dropping it takes the reason CHECK with it, so
    # the rewrite below is not constrained by the new vocabulary.
    op.execute(
        "ALTER TABLE memory_forget_provider_notices"
        " DROP COLUMN IF EXISTS memfw_reason"
    )
    # The rows come back to a status the previous build can read, and they do
    # so before the CHECK that would refuse them is restored.
    op.execute(
        "UPDATE memory_forget_provider_notices SET status = 'unavailable' "
        "WHERE status = 'unsupported'"
    )
    op.execute(f"""
        ALTER TABLE memory_forget_provider_notices
            DROP CONSTRAINT IF EXISTS {_STATUS_CHECK},
            ADD CONSTRAINT {_STATUS_CHECK} CHECK (status IN (
                'deleted', 'read_only', 'unavailable', 'not_configured'
            ))
    """)
