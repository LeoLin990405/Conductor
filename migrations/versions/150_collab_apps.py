"""Migration 150: durable state for the common collaboration protocol.

The protocol is a thin layer over the existing Ticket object, but the layer
still needs its own durable identity and audit trail. This migration adds six
tables and nothing else:

``collab_apps``      one row per application, holding only the SHA-256 digest
                     of its bearer token. A digest is not a credential and can
                     never be replayed; the plaintext is written once to the
                     user's vault/keychain by the service.
``collab_leases``    a Ticket or dispatch line claimed by an app, with a
                     revision for compare-and-swap, a 15-minute default lease,
                     a 4-hour ceiling and the exclusive worktree the app edits.
``collab_handoffs``  a pending transfer of a lease, including the untrusted
                     ``self_report`` the previous holder supplied. The system
                     section is generated, not stored here as authority.
``collab_messages``  app-to-app messages. They are always untrusted data and
                     never change a recipient's Ticket state.
``collab_reviews``   review requests and verdicts. A storage CHECK makes
                     self-review by the same app impossible even if a service
                     path forgets the rule.
``collab_audit``     one row per call, including every refusal; parameters are
                     stored only as a SHA-256 digest, never in the clear.

150 follows 149 and is the single head at the time this migration was written.
"""

from alembic import op

revision = "150"
down_revision = "149"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS collab_apps (
            app_id TEXT PRIMARY KEY
                CHECK (app_id ~ '^[a-z][a-z0-9-]{0,63}$'),
            user_id TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            token_sha256 TEXT NOT NULL UNIQUE
                CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
            scopes TEXT[] NOT NULL DEFAULT ARRAY['read']::TEXT[]
                CHECK (scopes <@ ARRAY[
                    'read', 'propose', 'claim', 'report', 'ask', 'review'
                ]::TEXT[]),
            can_write BOOLEAN NOT NULL DEFAULT FALSE,
            max_leases SMALLINT NOT NULL DEFAULT 1
                CHECK (max_leases BETWEEN 1 AND 3),
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            revoked_at TIMESTAMPTZ,
            last_used_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_collab_apps_user "
        "ON collab_apps (user_id, created_at DESC)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS collab_leases (
            lease_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            app_id TEXT NOT NULL
                REFERENCES collab_apps(app_id) ON DELETE CASCADE,
            agent TEXT NOT NULL DEFAULT ''
                CHECK (char_length(agent) <= 64),
            target_ref TEXT NOT NULL
                CHECK (char_length(target_ref) BETWEEN 1 AND 200),
            target_kind TEXT NOT NULL
                CHECK (target_kind IN ('ticket', 'line')),
            ticket_id UUID
                REFERENCES autonomy_tickets(ticket_id) ON DELETE CASCADE,
            line_id TEXT NOT NULL DEFAULT '',
            revision INTEGER NOT NULL CHECK (revision >= 0),
            state TEXT NOT NULL DEFAULT 'active'
                CHECK (state IN (
                    'active', 'handoff_pending', 'released', 'expired',
                    'completed'
                )),
            worktree_path TEXT NOT NULL DEFAULT '',
            worktree_branch TEXT NOT NULL DEFAULT '',
            worktree_base_sha TEXT NOT NULL DEFAULT '',
            heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            max_lease_until TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            ended_at TIMESTAMPTZ,
            end_reason TEXT NOT NULL DEFAULT ''
        )
        """
    )
    # One live holder per target. The CAS and this partial unique index agree:
    # a second app cannot insert a second active lease for the same task.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_collab_leases_live_target "
        "ON collab_leases (target_ref) "
        "WHERE state IN ('active', 'handoff_pending')"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_collab_leases_app_state "
        "ON collab_leases (app_id, state, expires_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_collab_leases_expiry "
        "ON collab_leases (state, expires_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_collab_leases_ticket "
        "ON collab_leases (ticket_id, created_at DESC)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS collab_handoffs (
            handoff_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            lease_id UUID NOT NULL
                REFERENCES collab_leases(lease_id) ON DELETE CASCADE,
            from_app_id TEXT NOT NULL
                REFERENCES collab_apps(app_id) ON DELETE CASCADE,
            to_target TEXT NOT NULL DEFAULT 'router',
            reason TEXT NOT NULL
                CHECK (reason IN (
                    'quota_exhausted', 'context_full', 'stuck',
                    'end_of_session', 'other'
                )),
            note TEXT NOT NULL DEFAULT ''
                CHECK (char_length(note) <= 1000),
            self_report JSONB NOT NULL DEFAULT '{}'::jsonb,
            -- Only {app, session_id}; conversation bodies never enter this
            -- database. The receiving app fetches them from memfw itself.
            conversation_ref JSONB,
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'accepted', 'expired', 'cancelled')),
            expires_at TIMESTAMPTZ NOT NULL,
            accepted_by TEXT
                REFERENCES collab_apps(app_id) ON DELETE SET NULL,
            accepted_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_collab_handoffs_pending "
        "ON collab_handoffs (to_target, state, expires_at)"
    )
    # At most one pending handoff per lease, so a stale request can never be
    # accepted, expired or declined against a later handoff of the same lease.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_collab_handoffs_one_pending "
        "ON collab_handoffs (lease_id) WHERE state = 'pending'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_collab_handoffs_from "
        "ON collab_handoffs (from_app_id, created_at DESC)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS collab_messages (
            message_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            from_app_id TEXT NOT NULL
                REFERENCES collab_apps(app_id) ON DELETE CASCADE,
            from_agent TEXT NOT NULL DEFAULT ''
                CHECK (char_length(from_agent) <= 64),
            to_target TEXT NOT NULL
                CHECK (char_length(to_target) BETWEEN 1 AND 200),
            text TEXT NOT NULL
                CHECK (char_length(text) BETWEEN 1 AND 2000),
            about_ref TEXT NOT NULL DEFAULT '',
            reply_to TEXT NOT NULL DEFAULT '',
            read_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_collab_messages_inbox "
        "ON collab_messages (to_target, read_at, created_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_collab_messages_from "
        "ON collab_messages (from_app_id, created_at DESC)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS collab_reviews (
            review_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            requester_app_id TEXT NOT NULL
                REFERENCES collab_apps(app_id) ON DELETE CASCADE,
            author_app_id TEXT NOT NULL
                REFERENCES collab_apps(app_id) ON DELETE CASCADE,
            reviewer_target TEXT NOT NULL DEFAULT 'native',
            reviewer_app_id TEXT
                REFERENCES collab_apps(app_id) ON DELETE SET NULL,
            target_ref TEXT NOT NULL
                CHECK (char_length(target_ref) BETWEEN 1 AND 200),
            lease_id UUID
                REFERENCES collab_leases(lease_id) ON DELETE SET NULL,
            commit_sha TEXT NOT NULL
                CHECK (commit_sha ~ '^[0-9a-f]{7,40}$'),
            focus TEXT NOT NULL DEFAULT ''
                CHECK (char_length(focus) <= 1000),
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'completed', 'expired', 'cancelled')),
            expires_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '24 hours'),
            verdict JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at TIMESTAMPTZ,
            CONSTRAINT collab_reviews_not_self
                CHECK (reviewer_app_id IS NULL OR reviewer_app_id <> author_app_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_collab_reviews_pending "
        "ON collab_reviews (reviewer_target, state, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_collab_reviews_author "
        "ON collab_reviews (author_app_id, created_at DESC)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS collab_audit (
            audit_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            app_id TEXT
                REFERENCES collab_apps(app_id) ON DELETE SET NULL,
            agent TEXT NOT NULL DEFAULT ''
                CHECK (char_length(agent) <= 64),
            tool TEXT NOT NULL
                CHECK (char_length(tool) BETWEEN 1 AND 100),
            target TEXT NOT NULL DEFAULT ''
                CHECK (char_length(target) <= 200),
            params_sha256 TEXT NOT NULL DEFAULT repeat('0', 64)
                CHECK (params_sha256 ~ '^[0-9a-f]{64}$'),
            code TEXT NOT NULL DEFAULT 'ok'
                CHECK (char_length(code) BETWEEN 1 AND 64),
            http_status SMALLINT NOT NULL DEFAULT 200
                CHECK (http_status BETWEEN 100 AND 599),
            rate_limited BOOLEAN NOT NULL DEFAULT FALSE,
            redacted INTEGER NOT NULL DEFAULT 0 CHECK (redacted >= 0),
            duration_ms INTEGER NOT NULL DEFAULT 0 CHECK (duration_ms >= 0),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_collab_audit_app "
        "ON collab_audit (app_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_collab_audit_code "
        "ON collab_audit (code, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP TABLE IF EXISTS collab_audit")
    op.execute("DROP TABLE IF EXISTS collab_reviews")
    op.execute("DROP TABLE IF EXISTS collab_messages")
    op.execute("DROP TABLE IF EXISTS collab_handoffs")
    op.execute("DROP TABLE IF EXISTS collab_leases")
    op.execute("DROP TABLE IF EXISTS collab_apps")
