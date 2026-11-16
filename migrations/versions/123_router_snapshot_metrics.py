"""Persist compact Router baselines and bounded decision observations."""

from alembic import op

revision = "123"
down_revision = "122"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE autonomy_router_baseline_snapshots (
            user_id TEXT PRIMARY KEY,
            snapshot_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            integrity_sha256 TEXT NOT NULL DEFAULT '',
            generation BIGINT NOT NULL DEFAULT 1 CHECK (generation >= 1),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            invalidated_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE TABLE autonomy_router_decision_metrics (
            decision_id UUID PRIMARY KEY,
            user_id TEXT NOT NULL,
            eval_id UUID NOT NULL UNIQUE,
            stable_decision_key TEXT NOT NULL,
            trigger_ids TEXT[] NOT NULL DEFAULT '{}',
            batch_size INTEGER NOT NULL CHECK (batch_size >= 0),
            dispatch_status TEXT NOT NULL DEFAULT 'reserved'
                CHECK (dispatch_status IN ('reserved', 'submitted', 'failed')),
            decision_latency_ms DOUBLE PRECISION CHECK (
                decision_latency_ms IS NULL OR decision_latency_ms >= 0
            ),
            latency_ms DOUBLE PRECISION CHECK (latency_ms IS NULL OR latency_ms >= 0),
            model_tokens INTEGER CHECK (model_tokens IS NULL OR model_tokens >= 0),
            first_candidate_id TEXT,
            routed_trigger_id TEXT,
            route_decision TEXT CHECK (
                route_decision IS NULL OR route_decision IN ('accept', 'attach', 'ignore', 'stop')
            ),
            accepted_candidate_id TEXT,
            first_candidate_accepted BOOLEAN,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            decided_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE INDEX autonomy_router_metrics_user_created_idx "
        "ON autonomy_router_decision_metrics (user_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX autonomy_router_metrics_recent_batch_idx "
        "ON autonomy_router_decision_metrics "
        "(user_id, stable_decision_key, decided_at DESC) "
        "WHERE route_decision IS NOT NULL"
    )
    op.execute(
        "COMMENT ON COLUMN autonomy_router_decision_metrics.latency_ms IS "
        "'Dispatch HTTP acknowledgement latency; not model or decision latency.'"
    )
    op.execute(
        "COMMENT ON COLUMN autonomy_router_decision_metrics.decision_latency_ms IS "
        "'Reservation-to-canonical-route elapsed time in milliseconds.'"
    )
    # Mutation invalidation lives in Postgres so concurrent API workers and
    # non-HTTP writers advance the same generation in the mutation transaction.
    op.execute(
        """
        CREATE FUNCTION invalidate_router_snapshot_for_source_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE affected_user TEXT;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                affected_user := OLD.user_id;
                INSERT INTO autonomy_router_baseline_snapshots (
                    user_id, snapshot_json, integrity_sha256, generation,
                    created_at, invalidated_at
                ) VALUES (affected_user, '{}'::jsonb, '', 1, NOW(), NOW())
                ON CONFLICT (user_id) DO UPDATE
                SET generation = autonomy_router_baseline_snapshots.generation + 1,
                    invalidated_at = NOW();
            ELSIF TG_OP = 'INSERT' THEN
                affected_user := NEW.user_id;
                INSERT INTO autonomy_router_baseline_snapshots (
                    user_id, snapshot_json, integrity_sha256, generation,
                    created_at, invalidated_at
                ) VALUES (affected_user, '{}'::jsonb, '', 1, NOW(), NOW())
                ON CONFLICT (user_id) DO UPDATE
                SET generation = autonomy_router_baseline_snapshots.generation + 1,
                    invalidated_at = NOW();
            ELSIF OLD.user_id IS DISTINCT FROM NEW.user_id
               OR OLD.kind IS DISTINCT FROM NEW.kind
               OR OLD.config IS DISTINCT FROM NEW.config
               OR OLD.enabled IS DISTINCT FROM NEW.enabled THEN
                INSERT INTO autonomy_router_baseline_snapshots (
                    user_id, snapshot_json, integrity_sha256, generation,
                    created_at, invalidated_at
                ) VALUES (OLD.user_id, '{}'::jsonb, '', 1, NOW(), NOW())
                ON CONFLICT (user_id) DO UPDATE
                SET generation = autonomy_router_baseline_snapshots.generation + 1,
                    invalidated_at = NOW();
                IF OLD.user_id IS DISTINCT FROM NEW.user_id THEN
                    INSERT INTO autonomy_router_baseline_snapshots (
                        user_id, snapshot_json, integrity_sha256, generation,
                        created_at, invalidated_at
                    ) VALUES (NEW.user_id, '{}'::jsonb, '', 1, NOW(), NOW())
                    ON CONFLICT (user_id) DO UPDATE
                    SET generation = autonomy_router_baseline_snapshots.generation + 1,
                        invalidated_at = NOW();
                END IF;
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER autonomy_sources_router_snapshot_invalidate
        AFTER INSERT OR UPDATE OR DELETE ON autonomy_sources
        FOR EACH ROW EXECUTE FUNCTION invalidate_router_snapshot_for_source_change()
        """
    )
    op.execute(
        """
        CREATE FUNCTION invalidate_router_snapshot_for_policy_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE affected_user TEXT;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                affected_user := OLD.user_id;
                INSERT INTO autonomy_router_baseline_snapshots (
                    user_id, snapshot_json, integrity_sha256, generation,
                    created_at, invalidated_at
                ) VALUES (affected_user, '{}'::jsonb, '', 1, NOW(), NOW())
                ON CONFLICT (user_id) DO UPDATE
                SET generation = autonomy_router_baseline_snapshots.generation + 1,
                    invalidated_at = NOW();
            ELSIF TG_OP = 'INSERT' THEN
                affected_user := NEW.user_id;
                INSERT INTO autonomy_router_baseline_snapshots (
                    user_id, snapshot_json, integrity_sha256, generation,
                    created_at, invalidated_at
                ) VALUES (affected_user, '{}'::jsonb, '', 1, NOW(), NOW())
                ON CONFLICT (user_id) DO UPDATE
                SET generation = autonomy_router_baseline_snapshots.generation + 1,
                    invalidated_at = NOW();
            ELSIF OLD.user_id IS DISTINCT FROM NEW.user_id
               OR OLD.version IS DISTINCT FROM NEW.version
               OR OLD.rules IS DISTINCT FROM NEW.rules
               OR OLD.accept_mode IS DISTINCT FROM NEW.accept_mode
               OR OLD.admission IS DISTINCT FROM NEW.admission
               OR OLD.is_current IS DISTINCT FROM NEW.is_current THEN
                INSERT INTO autonomy_router_baseline_snapshots (
                    user_id, snapshot_json, integrity_sha256, generation,
                    created_at, invalidated_at
                ) VALUES (OLD.user_id, '{}'::jsonb, '', 1, NOW(), NOW())
                ON CONFLICT (user_id) DO UPDATE
                SET generation = autonomy_router_baseline_snapshots.generation + 1,
                    invalidated_at = NOW();
                IF OLD.user_id IS DISTINCT FROM NEW.user_id THEN
                    INSERT INTO autonomy_router_baseline_snapshots (
                        user_id, snapshot_json, integrity_sha256, generation,
                        created_at, invalidated_at
                    ) VALUES (NEW.user_id, '{}'::jsonb, '', 1, NOW(), NOW())
                    ON CONFLICT (user_id) DO UPDATE
                    SET generation = autonomy_router_baseline_snapshots.generation + 1,
                        invalidated_at = NOW();
                END IF;
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER autonomy_policies_router_snapshot_invalidate
        AFTER INSERT OR UPDATE OR DELETE ON autonomy_policies
        FOR EACH ROW EXECUTE FUNCTION invalidate_router_snapshot_for_policy_change()
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP TRIGGER IF EXISTS autonomy_policies_router_snapshot_invalidate ON autonomy_policies")
    op.execute("DROP TRIGGER IF EXISTS autonomy_sources_router_snapshot_invalidate ON autonomy_sources")
    op.execute("DROP FUNCTION IF EXISTS invalidate_router_snapshot_for_policy_change()")
    op.execute("DROP FUNCTION IF EXISTS invalidate_router_snapshot_for_source_change()")
    op.execute("DROP TABLE IF EXISTS autonomy_router_decision_metrics")
    op.execute("DROP TABLE IF EXISTS autonomy_router_baseline_snapshots")
