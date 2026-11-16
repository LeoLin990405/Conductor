"""Migration 143: drop the finance tables the code no longer reads or writes.

The finance vertical is gone from this build. Nothing under ``src/`` reads or
writes a watchlist, a holding, a market insight, a price target or a wakeup,
and the chart-annotation chain went with W10b-2: the projector still replays
an old ``draw_chart_annotation`` tool message as a marked placeholder, but
that is history text, not a row in the table 014 created. What the vertical
left in the database is seven tables and one column, and this revision drops
them:

* ``chart_annotations`` (014) -- one agent-drawn annotation per chart
  instance, keyed by ``(workspace_id, chart_id, annotation_id)``;
* ``watchlist_items`` (001) -- one watched symbol, with the notes, alert
  settings and metadata that went with it;
* ``watchlists`` (001) -- the named list a watched symbol belongs to;
* ``user_portfolios`` (001) -- one holding, with its quantity and cost;
* ``market_insights`` (001, widened by 005 and 007) -- the generated brief;
* ``market_watch_targets`` (077) -- a price level on a thread's watch;
* ``wakeups`` (082) -- the agent's own follow-up on a thread;
* ``user_preferences.investment_preference`` (001) -- the investment style.

The column is the one 001 created beside ``risk_preference`` and
``agent_preference`` on the preferences row; ``users`` itself has no such
column, and the table the column sits on stays, because the rest of
``user_preferences`` -- the agent preference, the model selection, the BYOK
flags -- is still read and written by this build.

**The data is destroyed and cannot be restored.** No other table holds a copy
of a watchlist, a holding, an insight, a target or a wakeup, nothing is
archived before the drop, and no backup is taken here. A ``downgrade``
rebuilds the tables and the column *empty*: it restores the shape 142 had and
none of the rows, so a rolled-back database is a working schema with a history
that has vanished. A rollback that needs those rows restores a backup taken
before this migration (``conductor rollback``, which does not run
``alembic downgrade``); it does not rebuild them from nothing.

The shape is asserted, not assumed. Replaying only the migrations that created
these objects misses what later revisions took away -- 096 dropped
``idx_wakeups_armed_order`` together with ``order_attempts``, so the downgrade
below does not recreate that index -- so
``tests/integration/test_143_drop_finance_tables.py`` compares a fresh
``0 -> 142`` catalog against ``143 -> 142``, index by index and constraint by
constraint. The one difference it tolerates is disclosed: ``ADD COLUMN`` puts
``investment_preference`` at the end of ``user_preferences`` instead of 001's
ordinal position.

It is safe to drop them because this build no longer references any of them:
no SQL under ``src/`` names one of the seven tables, and no reader or writer
names ``investment_preference`` either. ``home_fold.py`` still folds
``chart_annotations`` rows into Home, which is the one reference left; it is
removed with the rest of the chart-annotation chain, and the fold of a table
that is gone would be a statement against nothing.

The drops run in one transaction, children before parents:
``watchlist_items`` references ``watchlists``, so it goes first, and nothing
else references a table this revision drops -- ``market_watch_targets`` and
``wakeups`` reference ``conversation_threads``, and ``chart_annotations``
references ``workspaces``, both of which stay. Each ``DROP TABLE`` takes its
indexes, its constraints and its ``updated_at`` trigger with it, so they are
not dropped separately. Every statement is ``IF EXISTS``, so a re-run against
a database where one of them is already gone is a no-op rather than a failure.

The downgrade is structural only. It recreates each table with the columns,
indexes and constraints its creating migration gave it -- 001 for the
watchlist, holding and insight tables, 014 for ``chart_annotations``, 077 for
``market_watch_targets`` and 082 for ``wakeups`` -- in the reverse order,
parents before children, and puts ``investment_preference`` back on
``user_preferences`` with the JSONB default 001 gave it. Every statement is
``IF NOT EXISTS`` and each trigger is dropped before it is created, so
re-running the downgrade, or running it against a database where a later
revision already restored one of these, leaves what is there alone instead of
failing. It cannot and does not restore a single row: the guarantee is that
the schema 142 had is back, not that the data is. The one thing that cannot
come back exactly is the column's place: ``ADD COLUMN`` appends, so
``investment_preference`` returns at the end of ``user_preferences`` rather
than the position 001 gave it, with its type, its default and its nullability
intact and no reader naming it by position.

The foreign keys the downgrade creates hold ``SHARE ROW EXCLUSIVE`` on
``users``, ``watchlists`` and ``conversation_threads`` until the command's
transaction commits, as 077 and 082 describe for a table they create. The
whole downgrade is one transaction (the range MIGRATIONS.md pins), so those
locks are held for the length of it; run it with the backend stopped, as the
recovery notes there say.

The older migrations (001, 005, 007, 014, 077, 082) stay in the chain
untouched: a fresh database still walks through them, and this revision
removes what they created.

143 follows the single head 142; no earlier revision is renumbered.
"""

from alembic import op

revision = "143"
down_revision = "142"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # Children before parents: watchlist_items references watchlists, so it
    # goes first. Nothing references the rest, and none of them references a
    # table this revision drops, so the order below is the only one that
    # matters. Each drop takes the table's indexes, constraints and
    # updated_at trigger with it.
    op.execute("DROP TABLE IF EXISTS watchlist_items")
    op.execute("DROP TABLE IF EXISTS watchlists")
    op.execute("DROP TABLE IF EXISTS user_portfolios")
    op.execute("DROP TABLE IF EXISTS market_insights")
    op.execute("DROP TABLE IF EXISTS market_watch_targets")
    op.execute("DROP TABLE IF EXISTS wakeups")
    op.execute("DROP TABLE IF EXISTS chart_annotations")
    # The column goes with the table it sits on, and that table stays: only
    # the investment style leaves user_preferences, the rest of the row is
    # still read and written. The default goes with the column.
    op.execute(
        "ALTER TABLE user_preferences"
        " DROP COLUMN IF EXISTS investment_preference"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # Parents before children, the reverse of the upgrade. Every statement is
    # IF NOT EXISTS and every trigger is dropped before it is created, so a
    # downgrade that is re-run -- or one run against a database where a later
    # revision already put one of these back -- leaves what is there alone
    # instead of failing. The rows are gone either way: this rebuilds the
    # shape 142 had, empty.
    op.execute(
        "ALTER TABLE user_preferences"
        " ADD COLUMN IF NOT EXISTS investment_preference JSONB"
        " DEFAULT '{}'::jsonb"
    )
    # 014: one annotation per (workspace, chart instance), cascading with the
    # workspace. The trigger function is 001's and is never dropped.
    op.execute("""
        CREATE TABLE IF NOT EXISTS chart_annotations (
            workspace_id UUID NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
            chart_id VARCHAR(128) NOT NULL,
            symbol VARCHAR(32) NOT NULL,
            timeframe VARCHAR(16) NOT NULL,
            annotation_id VARCHAR(64) NOT NULL,
            payload JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (workspace_id, chart_id, annotation_id)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_chart_annotations_view
        ON chart_annotations(workspace_id, symbol, timeframe)
    """)
    op.execute("DROP TRIGGER IF EXISTS update_chart_annotations_updated_at ON chart_annotations")
    op.execute("""
        CREATE TRIGGER update_chart_annotations_updated_at
            BEFORE UPDATE ON chart_annotations
            FOR EACH ROW EXECUTE FUNCTION update_updated_at_column()
    """)
    # 082: one table for every wakeup kind, one firing shape. The CHECKs are
    # 082's, spelled out verbatim, so the restored table refuses a row no
    # wakeup ever made.
    op.execute("""
        CREATE TABLE IF NOT EXISTS wakeups (
            wakeup_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            short_id VARCHAR(12) NOT NULL,
            conversation_thread_id UUID NOT NULL
                REFERENCES conversation_threads(conversation_thread_id)
                ON DELETE CASCADE,
            user_id VARCHAR(255) NOT NULL,
            label VARCHAR(40),
            kind VARCHAR(10) NOT NULL
                CHECK (kind IN ('at', 'cron', 'price', 'order')),
            -- at / cron
            timezone VARCHAR(64),
            cron VARCHAR(120),
            until_at TIMESTAMPTZ,
            schedule TIMESTAMPTZ[] NOT NULL DEFAULT '{}',
            stop_reason VARCHAR(8) CHECK (stop_reason IN ('24', '7day', 'until')),
            next_fire_at TIMESTAMPTZ,
            fires_done INTEGER NOT NULL DEFAULT 0,
            last_fired_seq INTEGER NOT NULL DEFAULT 0,
            delivered_seq INTEGER NOT NULL DEFAULT 0,
            last_fired_at TIMESTAMPTZ,
            delivered_at TIMESTAMPTZ,
            delivered_run_id UUID,
            -- price
            symbol VARCHAR(20),
            market VARCHAR(10) CHECK (market IN ('stock', 'index')),
            price_kind VARCHAR(10)
                CHECK (price_kind IN ('above', 'below', 'pct_up', 'pct_down')),
            price_value NUMERIC CHECK (price_value > 0),
            pct_from VARCHAR(20) CHECK (pct_from IN ('previous_close', 'armed')),
            armed_price NUMERIC,
            fired_price NUMERIC,
            warn_at TIMESTAMPTZ,
            warned_at TIMESTAMPTZ,
            lapse_delivered_at TIMESTAMPTZ,
            lapse_delivered_run_id UUID,
            -- price + order
            expires_at TIMESTAMPTZ,
            -- order
            attempt_id UUID,
            vendor_order_id VARCHAR(255),
            baseline_filled_qty NUMERIC,
            order_outcome JSONB,
            -- lifecycle
            status VARCHAR(12) NOT NULL DEFAULT 'armed'
                CHECK (status IN ('armed', 'fired', 'finished', 'expired', 'cancelled')),
            cancelled_by VARCHAR(12)
                CHECK (cancelled_by IN (
                    'agent', 'user', 'setting_off', 'disabled', 'archive', 'workspace'
                )),
            cancelled_at TIMESTAMPTZ,
            -- clock_timestamp(): rows armed by one call keep the order they
            -- were written in, which NOW() (the statement's start) would tie.
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT wakeups_short_id_unique UNIQUE (conversation_thread_id, short_id),
            CONSTRAINT wakeups_cancelled
                CHECK ((status = 'cancelled') = (cancelled_by IS NOT NULL)),
            CONSTRAINT wakeups_timed CHECK (kind NOT IN ('at', 'cron')
                OR (timezone IS NOT NULL AND cardinality(schedule) BETWEEN 1 AND 24)),
            CONSTRAINT wakeups_cron CHECK ((kind = 'cron') = (cron IS NOT NULL)),
            CONSTRAINT wakeups_price CHECK (kind <> 'price' OR (
                symbol IS NOT NULL AND price_kind IS NOT NULL
                AND price_value IS NOT NULL AND expires_at IS NOT NULL
                AND warn_at IS NOT NULL)),
            CONSTRAINT wakeups_pct_from_iff_pct_kind CHECK (kind <> 'price'
                OR (price_kind IN ('pct_up', 'pct_down')) = (pct_from IS NOT NULL)),
            CONSTRAINT wakeups_order CHECK (kind <> 'order' OR (
                attempt_id IS NOT NULL AND vendor_order_id IS NOT NULL
                AND expires_at IS NOT NULL))
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_wakeups_due ON wakeups (next_fire_at)
        WHERE status = 'armed' AND next_fire_at IS NOT NULL
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_wakeups_lapse ON wakeups (warn_at)
        WHERE status = 'armed' AND kind = 'price' AND warned_at IS NULL
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_wakeups_expiry ON wakeups (expires_at)
        WHERE status = 'armed' AND kind IN ('price', 'order')
    """)
    # No idx_wakeups_armed_order: 096 dropped it along with order_attempts, so
    # a faithful 142 has only the six indexes below.
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_wakeups_thread
        ON wakeups (conversation_thread_id, created_at DESC)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_wakeups_user_cancel
        ON wakeups (conversation_thread_id)
        WHERE cancelled_by IN ('user', 'setting_off')
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_wakeups_user_open ON wakeups (user_id)
        WHERE status = 'armed' OR last_fired_seq > delivered_seq
    """)
    # 077: a level or a % move on a watched symbol, one armed target per
    # (thread, listing, kind).
    op.execute("""
        CREATE TABLE IF NOT EXISTS market_watch_targets (
            target_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            conversation_thread_id UUID NOT NULL
                REFERENCES conversation_threads(conversation_thread_id)
                ON DELETE CASCADE,
            user_id VARCHAR(255) NOT NULL,
            symbol VARCHAR(20) NOT NULL,
            market VARCHAR(10) NOT NULL DEFAULT 'stock'
                CHECK (market IN ('stock', 'index')),
            kind VARCHAR(20) NOT NULL
                CHECK (kind IN ('above', 'below', 'pct_up', 'pct_down')),
            value NUMERIC NOT NULL CHECK (value > 0),
            pct_from VARCHAR(20)
                CHECK (pct_from IN ('previous_close', 'armed')),
            armed_price NUMERIC,
            status VARCHAR(20) NOT NULL DEFAULT 'armed'
                CHECK (status IN ('armed', 'fired', 'expired', 'cancelled')),
            expires_at TIMESTAMPTZ NOT NULL,
            warn_at TIMESTAMPTZ NOT NULL,
            warned_at TIMESTAMPTZ,
            fired_at TIMESTAMPTZ,
            fired_price NUMERIC,
            hit_delivered_at TIMESTAMPTZ,
            hit_delivered_run_id UUID,
            warn_delivered_at TIMESTAMPTZ,
            warn_delivered_run_id UUID,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT market_watch_targets_pct_from_iff_pct_kind
                CHECK ((kind IN ('pct_up', 'pct_down')) = (pct_from IS NOT NULL))
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_market_watch_targets_armed
        ON market_watch_targets (warn_at)
        WHERE status = 'armed'
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_market_watch_targets_thread
        ON market_watch_targets (conversation_thread_id, created_at DESC)
    """)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_market_watch_targets_armed_kind
        ON market_watch_targets (conversation_thread_id, symbol, market, kind)
        WHERE status = 'armed'
    """)
    # 001 as 007 left it: user_id is VARCHAR(255), the shape every other
    # table's user_id has. No foreign key, as 001 wrote it.
    op.execute("""
        CREATE TABLE IF NOT EXISTS market_insights (
            market_insight_id UUID PRIMARY KEY,
            user_id VARCHAR(255),
            type VARCHAR(30) NOT NULL DEFAULT 'daily_brief',
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            headline TEXT,
            summary TEXT,
            content JSONB,
            topics JSONB,
            sources JSONB,
            model VARCHAR(10),
            error_message TEXT,
            generation_time_ms INTEGER,
            metadata JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at TIMESTAMPTZ
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_market_insights_latest
        ON market_insights (type, created_at DESC)
        WHERE status = 'completed' AND user_id IS NULL
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_market_insights_user
        ON market_insights (user_id, type, created_at DESC)
        WHERE status = 'completed' AND user_id IS NOT NULL
    """)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_market_insights_user_generating
        ON market_insights (user_id)
        WHERE status = 'generating'
    """)
    # 001: a holding, one per (user, symbol, type, account).
    op.execute("""
        CREATE TABLE IF NOT EXISTS user_portfolios (
            user_portfolio_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id VARCHAR(255) NOT NULL
                REFERENCES users(user_id) ON DELETE CASCADE,
            symbol VARCHAR(50) NOT NULL,
            instrument_type VARCHAR(30) NOT NULL,
            exchange VARCHAR(50),
            name VARCHAR(255),
            quantity DECIMAL(18, 8) NOT NULL,
            average_cost DECIMAL(18, 4),
            currency VARCHAR(10) DEFAULT 'USD',
            account_name VARCHAR(100),
            notes TEXT,
            metadata JSONB DEFAULT '{}'::jsonb,
            first_purchased_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT unique_user_holding
                UNIQUE (user_id, symbol, instrument_type, account_name)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_portfolios_user_id ON user_portfolios(user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_portfolios_symbol ON user_portfolios(symbol)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_portfolios_instrument_type ON user_portfolios(instrument_type)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_portfolios_user_instrument ON user_portfolios(user_id, symbol, instrument_type)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_portfolios_account ON user_portfolios(account_name)")
    op.execute("DROP TRIGGER IF EXISTS trg_user_portfolios_updated_at ON user_portfolios")
    op.execute("""
        CREATE TRIGGER trg_user_portfolios_updated_at
            BEFORE UPDATE ON user_portfolios
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at_column()
    """)
    # 001: the named list, before the items that belong to it.
    op.execute("""
        CREATE TABLE IF NOT EXISTS watchlists (
            watchlist_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id VARCHAR(255) NOT NULL
                REFERENCES users(user_id) ON DELETE CASCADE,
            name VARCHAR(100) NOT NULL,
            description TEXT,
            is_default BOOLEAN NOT NULL DEFAULT FALSE,
            display_order INTEGER DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT unique_user_watchlist_name UNIQUE (user_id, name)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_watchlists_user_id ON watchlists(user_id)")
    op.execute("DROP TRIGGER IF EXISTS trg_watchlists_updated_at ON watchlists")
    op.execute("""
        CREATE TRIGGER trg_watchlists_updated_at
            BEFORE UPDATE ON watchlists
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at_column()
    """)
    # 001: the watched symbol, last because it references watchlists.
    op.execute("""
        CREATE TABLE IF NOT EXISTS watchlist_items (
            watchlist_item_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            watchlist_id UUID NOT NULL
                REFERENCES watchlists(watchlist_id) ON DELETE CASCADE,
            user_id VARCHAR(255) NOT NULL
                REFERENCES users(user_id) ON DELETE CASCADE,
            symbol VARCHAR(50) NOT NULL,
            instrument_type VARCHAR(30) NOT NULL,
            exchange VARCHAR(50),
            name VARCHAR(255),
            notes TEXT,
            alert_settings JSONB DEFAULT '{}'::jsonb,
            metadata JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT unique_watchlist_item
                UNIQUE (watchlist_id, symbol, instrument_type)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_watchlist_items_watchlist_id ON watchlist_items(watchlist_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_watchlist_items_user_id ON watchlist_items(user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_watchlist_items_symbol ON watchlist_items(symbol)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_watchlist_items_user_symbol ON watchlist_items(user_id, symbol, instrument_type)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_watchlist_items_created_at ON watchlist_items(created_at DESC)")
    op.execute("DROP TRIGGER IF EXISTS trg_watchlist_items_updated_at ON watchlist_items")
    op.execute("""
        CREATE TRIGGER trg_watchlist_items_updated_at
            BEFORE UPDATE ON watchlist_items
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at_column()
    """)
