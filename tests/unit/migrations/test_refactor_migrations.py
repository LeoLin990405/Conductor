"""Phase 1 migrations: revision chain, rollback shape, and contract binding.

The migrations spell their CHECK vocabularies out in SQL because Alembic must
run without importing the application stack. That is exactly how the Python
contract and the schema drift apart silently, so this test reads the migration
SQL back and asserts it enumerates ``src/server/contracts/refactor.py``'s
vocabularies — the same pattern ``test_status.py`` uses for migration 020.

No database is touched: the chain is inspected through Alembic's script
directory and the DDL through the module source.
"""

from __future__ import annotations

import ast
import importlib.util
import textwrap
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from src.server.contracts import refactor

REPO_ROOT = Path(__file__).resolve().parents[3]
VERSIONS = REPO_ROOT / "migrations" / "versions"


def _load(filename: str) -> tuple[object, str]:
    path = VERSIONS / filename
    assert path.is_file(), f"{filename} is missing"
    spec = importlib.util.spec_from_file_location(f"migration_{filename[:3]}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, path.read_text(encoding="utf-8")


def _script_directory() -> ScriptDirectory:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return ScriptDirectory.from_config(config)


def test_revision_chain_extends_the_previous_head():
    script = _script_directory()
    # integrate/r2 was authoritative through 128 (context scopes); the endpoint
    # chain adds 129 (local policy version) and 130 (signed lease grants), this
    # line's ticket migrations 131/132 continue it, memory P0 owns the allocated
    # tail 133-138, this line adds 139 (review queue + merge audit), 140 adds the
    # daemon output sessions, 141 the dispatch gates, 142 the forget
    # notice status, the retired domain-table drop, full Worker report bodies, the
    # computer_data_tokens retirement, and structured Docker git_check, so the
    # single merged head is 150.
    assert script.get_heads() == ["151"]
    assert script.get_revision("151").down_revision == "150"
    assert script.get_revision("150").down_revision == "149"
    assert script.get_revision("149").down_revision == "148"
    assert script.get_revision("146").down_revision == "145"
    assert script.get_revision("145").down_revision == "144"
    assert script.get_revision("144").down_revision == "143"
    assert script.get_revision("142").down_revision == "141"
    assert script.get_revision("141").down_revision == "140"
    assert script.get_revision("140").down_revision == "139"
    assert script.get_revision("139").down_revision == "138"
    assert script.get_revision("138").down_revision == "137"
    assert script.get_revision("137").down_revision == "136"
    assert script.get_revision("136").down_revision == "135"
    assert script.get_revision("135").down_revision == "134"
    assert script.get_revision("134").down_revision == "133"
    assert script.get_revision("133").down_revision == "132"
    assert script.get_revision("130").down_revision == "129"
    assert script.get_revision("129").down_revision == "128"
    assert script.get_revision("131").down_revision == "130"
    assert script.get_revision("128").down_revision == "127"
    assert script.get_revision("127").down_revision == "126"
    assert script.get_revision("126").down_revision == "125"
    assert script.get_revision("125").down_revision == "124"
    assert script.get_revision("124").down_revision == "123"
    assert script.get_revision("123").down_revision == "122"
    assert script.get_revision("122").down_revision == "121"
    assert script.get_revision("121").down_revision == "120"
    assert script.get_revision("120").down_revision == "119"
    assert script.get_revision("119").down_revision == "118"
    assert script.get_revision("118").down_revision == "117"
    assert script.get_revision("116").down_revision == "115"
    assert script.get_revision("115").down_revision == "114"
    assert script.get_revision("114").down_revision == "113"
    assert script.get_revision("113").down_revision == "112"
    assert script.get_revision("112").down_revision == "111"
    assert script.get_revision("111").down_revision == "110"
    assert script.get_revision("114").down_revision == "113"
    assert script.get_revision("115").down_revision == "114"
    assert script.get_revision("110").down_revision == "109"
    assert script.get_revision("099").down_revision == "098"
    assert script.get_revision("109").down_revision == "108"
    assert script.get_revision("108").down_revision == "107"
    assert script.get_revision("107").down_revision == "106"
    assert script.get_revision("106").down_revision == "105"
    assert script.get_revision("105").down_revision == "104"
    assert script.get_revision("104").down_revision == "103"
    assert script.get_revision("103").down_revision == "102"
    assert script.get_revision("098").down_revision == "097"
    assert script.get_revision("097").down_revision == "096"
    assert script.get_revision("096").down_revision == "095"
    assert script.get_revision("095").down_revision == "094"
    assert script.get_revision("094").down_revision == "093"
    assert script.get_revision("093").down_revision == "092"
    assert script.get_revision("092").down_revision == "091"
    assert script.get_revision("091").down_revision == "090"
    assert script.get_revision("090").down_revision == "089"
    assert script.get_revision("089").down_revision == "088"
    assert script.get_revision("088").down_revision == "087"
    assert script.get_revision("087").down_revision == "086"
    assert script.get_revision("086").down_revision == "085"
    # The chain still has exactly one head (test_single_head.py owns that, but
    # this pins that the refactor did not fork it).
    assert len(script.get_heads()) == 1


def _revision_graph() -> tuple[dict[str, str | tuple[str, ...] | None], dict[str, str]]:
    """Read revision ids from the migration files themselves.

    Alembic keeps one script per revision id and only warns when a second file
    reuses it, so a duplicated ``119`` does not show up as two heads. Two
    different ids that both name ``118`` as ``down_revision`` do: ``alembic
    upgrade head`` then refuses to run. Walking the files catches both.
    """
    revisions: dict[str, str | tuple[str, ...] | None] = {}
    owners: dict[str, str] = {}
    for path in sorted(VERSIONS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        values: dict[str, object] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                targets = node.targets
                value = node.value
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
                value = node.value
            else:
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id in {
                    "revision",
                    "down_revision",
                }:
                    values[target.id] = ast.literal_eval(value)
        if values.get("revision") is None:
            continue
        revision = str(values["revision"])
        assert revision not in owners, (
            f"revision {revision} is claimed by {owners[revision]} and {path.name}"
        )
        down = values.get("down_revision")
        if isinstance(down, tuple):
            revisions[revision] = tuple(str(item) for item in down)
        elif isinstance(down, str) or down is None:
            revisions[revision] = down
        else:
            raise AssertionError(f"{path.name} has a non-literal down_revision")
        owners[revision] = path.name
    return revisions, owners


def test_revision_files_have_one_head_and_do_not_reuse_119():
    """Linear chain: 119 → 120 (close) → 121 (dispatch) → … → 128 → 129 → 130 → 131 → 132.

    integrate/r2 is authoritative through 128 (ticket switches, handoffs,
    subscription lifecycle, context scopes). The endpoint chain is 129 (local
    policy version) then 130 (signed lease grants), and ob-tickets' 131/132
    close the chain. 121's ``down_revision`` must stay ``"120"``, or 119 forks.
    """
    revisions, owners = _revision_graph()
    parents = {
        parent
        for down_revision in revisions.values()
        for parent in (
            down_revision
            if isinstance(down_revision, tuple)
            else (down_revision,)
            if down_revision is not None
            else ()
        )
    }
    heads = set(revisions) - parents
    assert owners["119"] == "119_channel_reply_approval.py"
    assert owners["120"] == "120_task_close.py"
    assert owners["121"] == "121_dispatch.py"
    assert owners["122"] == "122_router_shard_peer_channel.py"
    assert owners["123"] == "123_router_snapshot_metrics.py"
    assert owners["124"] == "124_verified_stop.py"
    assert owners["125"] == "125_ticket_switches.py"
    assert owners["126"] == "126_ticket_handoffs.py"
    assert owners["127"] == "127_subscription_lifecycle.py"
    assert owners["128"] == "128_context_scopes_a2a.py"
    assert owners["131"] == "131_ticket_lines_ownership.py"
    assert owners["132"] == "132_ticket_event_actions.py"
    assert owners["133"] == "133_memory_forget.py"
    assert owners["134"] == "134_memory_origin.py"
    assert owners["135"] == "135_memory_change_kind.py"
    assert owners["136"] == "136_memory_policy_metadata.py"
    assert owners["137"] == "137_memory_consolidation.py"
    assert owners["138"] == "138_memory_tool_denials.py"
    assert owners["139"] == "139_review_merge_audit.py"
    assert owners["140"] == "140_daemon_output_sessions.py"
    assert owners["141"] == "141_dispatch_gates.py"
    assert owners["142"] == "142_memory_forget_unsupported.py"
    assert revisions["119"] == "118"
    assert revisions["120"] == "119"
    assert revisions["121"] == "120"
    assert revisions["122"] == "121"
    assert revisions["123"] == "122"
    assert revisions["124"] == "123"
    assert owners["129"] == "129_daemon_policy_version.py"
    assert owners["130"] == "130_daemon_lease_grants.py"
    assert revisions["125"] == "124"
    assert revisions["126"] == "125"
    assert revisions["127"] == "126"
    assert revisions["128"] == "127"
    assert revisions["129"] == "128"
    assert revisions["130"] == "129"
    assert revisions["131"] == "130"
    assert revisions["132"] == "131"
    # Memory P0 owns the allocated tail 133-138, and 139 adds the review queue
    # and merge audit, so 132 stays an ancestor and the AST-discovered head is
    # 139.
    assert revisions["133"] == "132"
    assert revisions["134"] == "133"
    assert revisions["135"] == "134"
    assert revisions["136"] == "135"
    assert revisions["137"] == "136"
    assert revisions["138"] == "137"
    assert revisions["139"] == "138"
    assert revisions["140"] == "139"
    assert revisions["141"] == "140"
    assert revisions["142"] == "141"
    assert revisions["143"] == "142"
    assert revisions["144"] == "143"
    assert revisions["145"] == "144"
    assert revisions["146"] == "145"
    assert revisions["147"] == "146"
    assert revisions["149"] == "148"
    assert revisions["150"] == "149"
    assert revisions["151"] == "150"
    assert heads == {"151"}



def test_daemon_assignment_migration_guards_source_and_schedule_ownership():
    _module, ddl = _load("110_source_schedule_daemons.py")
    assert "REFERENCES autonomy_daemons(daemon_id) ON DELETE RESTRICT" in ddl
    assert "autonomy_source_daemon_owner_guard" in ddl
    assert "autonomy_schedule_daemon_owner_guard" in ddl
    assert "FOR KEY SHARE" in ddl
    assert "DROP FUNCTION IF EXISTS autonomy_source_daemon_owner_guard()" in ddl
    assert "DROP FUNCTION IF EXISTS autonomy_schedule_daemon_owner_guard()" in ddl


def test_daemon_ticket_lease_migration_adds_fencing_and_durable_claim_receipts():
    module, ddl = _load("111_daemon_ticket_leases.py")
    assert module.revision == "111"
    assert module.down_revision == "110"
    assert "worker_lease_id UUID" in ddl
    assert "worker_lease_claim_id UUID" in ddl
    assert "worker_lease_expires_at TIMESTAMPTZ" in ddl
    assert "autonomy_daemon_claims" in ddl
    assert "claim_id UUID PRIMARY KEY" in ddl
    assert "lease_id UUID NOT NULL UNIQUE" in ddl
    assert "DROP TABLE IF EXISTS autonomy_daemon_claims" in ddl
    assert "DROP COLUMN IF EXISTS worker_lease_id" in ddl


def test_verified_stop_migration_adds_stop_requests_and_last_heartbeat():
    module, ddl = _load("124_verified_stop.py")
    assert module.revision == "124"
    assert module.down_revision == "123"
    assert "worker_lease_heartbeat_at TIMESTAMPTZ" in ddl
    assert "CREATE TABLE IF NOT EXISTS autonomy_stop_requests" in ddl
    assert "deadline_at TIMESTAMPTZ NOT NULL" in ddl
    assert "idx_autonomy_stop_requests_pending" in ddl
    assert "DROP TABLE IF EXISTS autonomy_stop_requests" in ddl
    assert "DROP COLUMN IF EXISTS worker_lease_heartbeat_at" in ddl


def test_daemon_concurrency_migration_defaults_and_bounds_capacity():
    module, ddl = _load("112_daemon_concurrency.py")
    assert module.revision == "112"
    assert module.down_revision == "111"
    assert "ADD COLUMN IF NOT EXISTS" in ddl
    assert "max_concurrency INTEGER NOT NULL DEFAULT 1" in ddl
    assert "CHECK (max_concurrency BETWEEN 1 AND 32)" in ddl
    assert "DROP COLUMN IF EXISTS max_concurrency" in ddl


def test_main_agent_migration_uses_durable_owner_and_alert_fences():
    module, ddl = _load("113_main_agent_runs.py")
    assert module.revision == "113"
    assert module.down_revision == "112"
    assert "CREATE TABLE main_agent_runs" in ddl
    assert "payload JSONB NOT NULL" in ddl
    assert "CREATE TABLE main_agent_alerts" in ddl
    assert "PRIMARY KEY (run_id, task_id, alert_key)" in ddl
    assert "DROP TABLE IF EXISTS main_agent_alerts" in ddl


def test_daemon_claim_replay_migration_persists_delivered_inputs():
    module, ddl = _load("116_daemon_claim_input_replay.py")
    assert module.revision == "116"
    assert module.down_revision == "115"
    assert "input_snapshot JSONB NOT NULL DEFAULT '[]'::jsonb" in ddl
    assert "DROP COLUMN IF EXISTS input_snapshot" in ddl


def test_every_refactor_migration_has_upgrade_and_downgrade():
    for filename in (
        "086_agent_goals_plans.py",
        "087_agent_run_contracts.py",
        "088_agent_events.py",
        "089_memory_entries.py",
        "090_agent_tasks.py",
        "091_agent_tasks_todo_identity.py",
        "092_memory_trgm.py",
        "093_agent_thread_permissions.py",
        "094_agent_memory_proposals.py",
    ):
        module, _ = _load(filename)
        assert callable(module.upgrade), filename
        assert callable(module.downgrade), filename


def test_goals_migration_creates_and_drops_its_tables():
    _, source = _load("086_agent_goals_plans.py")
    for table in ("agent_goals", "agent_plans"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in source
        assert f"DROP TABLE IF EXISTS {table}" in source
        assert table in textwrap.dedent(
            (REPO_ROOT / "docs/refactor/0004-data-model.md").read_text(encoding="utf-8")
        )


def test_goals_migration_pins_the_contract_vocabulary():
    _, source = _load("086_agent_goals_plans.py")
    for status in refactor.GOAL_STATUSES:
        assert f"'{status}'" in source, (
            f"goal status {status!r} missing from migration 086"
        )
    for status in refactor.PLAN_STATUSES:
        assert f"'{status}'" in source, (
            f"plan status {status!r} missing from migration 086"
        )
    # The goal's completion test must be persisted, not inferred from messages.
    assert "stop_conditions JSONB NOT NULL" in source
    assert "verifier_verdict JSONB" in source
    # One live goal per thread is a database invariant, not just a service rule.
    assert "uq_agent_goals_active" in source


def test_run_contract_migration_pins_permission_modes_and_columns():
    _, source = _load("087_agent_run_contracts.py")
    assert "CREATE TABLE IF NOT EXISTS agent_run_contracts" in source
    assert "DROP TABLE IF EXISTS agent_run_contracts" in source
    for mode in refactor.PERMISSION_MODES:
        assert f"'{mode}'" in source, (
            f"permission mode {mode!r} missing from migration 087"
        )
    for column in (
        "contract_version",
        "context_budget",
        "injected_memory",
        "verifier_verdict",
        "tool_allowlist",
        "result",
        "verification",
    ):
        assert column in source, f"column {column!r} missing from migration 087"
    assert "ALTER TABLE subagent_runs" in source
    assert "DROP COLUMN IF EXISTS result" in source


def test_new_tables_are_documented_in_the_data_model():
    doc = (REPO_ROOT / "docs/refactor/0004-data-model.md").read_text(encoding="utf-8")
    for table in (
        "agent_goals",
        "agent_plans",
        "agent_run_contracts",
        "agent_events",
        "memory_entries",
        "agent_tasks",
        "uq_agent_tasks_todo_identity",
    ):
        assert table in doc, f"{table} is missing from docs/refactor/0004-data-model.md"


def test_events_migration_creates_and_drops_the_stream():
    _, source = _load("088_agent_events.py")
    assert "CREATE TABLE IF NOT EXISTS agent_events" in source
    assert "DROP TABLE IF EXISTS agent_events" in source
    for column in (
        "event_id BIGSERIAL PRIMARY KEY",
        "seq INTEGER NOT NULL",
        "payload JSONB",
    ):
        assert column in source
    assert "uq_agent_events_seq" in source
    assert "ix_agent_events_thread" in source


def test_memory_projection_pins_tiers_status_and_generated_search():
    _, source = _load("089_memory_entries.py")
    assert "CREATE TABLE IF NOT EXISTS memory_entries" in source
    assert "DROP TABLE IF EXISTS memory_entries" in source
    for tier in refactor.MEMORY_TIERS:
        assert f"'{tier}'" in source, f"memory tier {tier!r} missing from migration 089"
    for status in refactor.MEMORY_ENTRY_STATUSES:
        assert f"'{status}'" in source, (
            f"memory status {status!r} missing from migration 089"
        )
    # Retrieval works without copying bodies: the index line is what is searched.
    assert "index_text TEXT NOT NULL" in source
    assert "search TSVECTOR GENERATED ALWAYS AS" in source
    assert "USING gin (search)" in source
    assert "UNIQUE (user_id, tier, entry_key, status)" in source


def test_task_tree_pins_status_and_source_vocabulary():
    _, source = _load("090_agent_tasks.py")
    assert "CREATE TABLE IF NOT EXISTS agent_tasks" in source
    assert "DROP TABLE IF EXISTS agent_tasks" in source
    for status in refactor.TASK_STATUSES:
        assert f"'{status}'" in source, (
            f"task status {status!r} missing from migration 090"
        )
    for kind in refactor.TASK_SOURCES:
        assert f"'{kind}'" in source, f"task source {kind!r} missing from migration 090"
    assert "parent_node_id UUID" in source
    assert "subagent_task_ref TEXT" in source
    assert "ON DELETE CASCADE" in source


def test_todo_identity_migration_adds_partial_unique_index():
    _, source = _load("091_agent_tasks_todo_identity.py")
    assert "uq_agent_tasks_todo_identity" in source
    assert "ON agent_tasks (thread_id, title)" in source
    assert "WHERE source = 'todo'" in source
    # A task re-added later must reuse the same row, so the index is not
    # restricted to open statuses.
    assert (
        "status NOT IN"
        not in source.split("CREATE UNIQUE INDEX", 1)[1].split(");", 1)[0]
    )
    # Pre-index duplicates keep the earliest row (the one with the history).
    assert "a.created_at > b.created_at" in source
    assert "DROP INDEX IF EXISTS uq_agent_tasks_todo_identity" in source
    assert "ix_agent_tasks_thread_source_status" in source


def test_memory_trgm_migration_adds_cjk_retrieval_index():
    _, source = _load("092_memory_trgm.py")
    assert "CREATE EXTENSION IF NOT EXISTS pg_trgm" in source
    # The service queries the combined key + index text, so the index must be on
    # that expression, not on index_text alone.
    assert "USING gin ((entry_key || ' ' || index_text) gin_trgm_ops)" in source
    assert "ix_memory_entries_trgm" in source
    # Downgrade drops the index but keeps the database-wide extension.
    downgrade = source.split("def downgrade", 1)[1]
    assert "DROP INDEX IF EXISTS ix_memory_entries_trgm" in downgrade
    assert "DROP EXTENSION" not in downgrade


def test_trgm_migration_is_documented_in_the_data_model():
    repo = Path(__file__).resolve().parents[3]
    doc = (repo / "docs" / "refactor" / "0004-data-model.md").read_text()
    assert "ix_memory_entries_trgm" in doc
    assert "pg_trgm" in doc


def test_permissions_migration_adds_one_row_per_thread():
    _, source = _load("093_agent_thread_permissions.py")
    assert "CREATE TABLE IF NOT EXISTS agent_thread_permissions" in source
    assert "thread_id UUID PRIMARY KEY" in source
    assert "REFERENCES conversation_threads(conversation_thread_id)" in source
    assert "mode IN ('manual', 'auto')" in source
    assert "DROP TABLE IF EXISTS agent_thread_permissions" in source
    # skip is deliberately absent: only enforceable modes may be stored.
    assert "'skip'" not in source


def test_permissions_table_is_documented_in_the_data_model():
    repo = Path(__file__).resolve().parents[3]
    doc = (repo / "docs" / "refactor" / "0004-data-model.md").read_text()
    assert "agent_thread_permissions" in doc
    assert "mode IN ('manual', 'auto')" in doc


def test_memory_proposals_migration_has_identity_and_lifecycle():
    _, source = _load("094_agent_memory_proposals.py")
    assert "CREATE TABLE IF NOT EXISTS agent_memory_proposals" in source
    assert "proposal_id UUID PRIMARY KEY" in source
    assert "UNIQUE (task_run_id, proposal_type, text_hash)" in source
    assert "status IN ('proposed', 'approved', 'rejected')" in source
    assert "DROP TABLE IF EXISTS agent_memory_proposals" in source


def test_memory_proposals_table_is_documented_in_the_data_model():
    repo = Path(__file__).resolve().parents[3]
    doc = (repo / "docs" / "refactor" / "0004-data-model.md").read_text()
    assert "agent_memory_proposals" in doc
    assert "text_hash" in doc
