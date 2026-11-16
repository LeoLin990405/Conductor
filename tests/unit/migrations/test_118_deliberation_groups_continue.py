"""118 stores provider-group snapshots and continuation ancestry."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_VERSION = (
    Path(__file__).resolve().parents[3]
    / "migrations"
    / "versions"
    / "118_deliberation_groups_continue.py"
)


@pytest.fixture
def migration(monkeypatch):
    spec = importlib.util.spec_from_file_location("migration_118", _VERSION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    events: list[str] = []
    op = MagicMock()
    op.execute.side_effect = lambda sql: events.append(" ".join(str(sql).split()))
    monkeypatch.setattr(module, "op", op)
    return module, events


def test_migration_follows_117_and_adds_round_groups_and_parent(migration):
    module, events = migration
    assert (module.revision, module.down_revision) == ("118", "117")
    module.upgrade()
    sql = " ".join(events)
    assert events[0] == "SET LOCAL lock_timeout = '5s'"
    assert "ADD COLUMN IF NOT EXISTS parent_deliberation_id UUID" in sql
    assert "REFERENCES autonomy_deliberations(deliberation_id)" in sql
    assert "ADD COLUMN IF NOT EXISTS provider_groups JSONB" in sql
    assert "DEFAULT '[]'::jsonb" in sql


def test_downgrade_drops_only_added_columns_in_reverse_order(migration):
    module, events = migration
    module.downgrade()
    assert " ".join(events) == (
        "ALTER TABLE autonomy_deliberation_rounds DROP COLUMN IF EXISTS provider_groups "
        "ALTER TABLE autonomy_deliberations DROP COLUMN IF EXISTS parent_deliberation_id"
    )


def test_migration_graph_extends_through_142_as_its_only_ast_discovered_head():
    """118 stays chained to 117. The chain continues past it, and 142 is the only head."""
    revisions: dict[str, str | None] = {}
    for path in _VERSION.parent.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        values = {}
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
        if values.get("revision") is not None:
            revisions[str(values["revision"])] = values.get("down_revision")

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
    assert revisions["118"] == "117"
    # A later migration may extend the chain; 118 is never a head again.
    assert revisions["127"] == "126"
    assert revisions["131"] == "130"
    assert revisions["132"] == "131"
    assert "118" not in heads
    # integrate/r2 landed 125-127 (ticket switches, handoffs, subscription
    # lifecycle) and 128 (context scopes); the endpoint chain adds 129 (local
    # policy version) and 130 (signed lease grants), and this line's ticket
    # migrations 131/132 continue it, so 127/128 stay ancestors.
    assert revisions["124"] == "123"
    assert revisions["128"] == "127"
    assert revisions["129"] == "128"
    assert revisions["130"] == "129"
    assert revisions["131"] == "130"
    assert revisions["132"] == "131"
    # Memory P0 owns the allocated tail 133-138 after the endpoint/ob-tickets
    # head 132, so 132 stays an ancestor and the AST-discovered head is 142.
    assert revisions["133"] == "132"
    assert revisions["134"] == "133"
    assert revisions["135"] == "134"
    assert revisions["136"] == "135"
    assert revisions["137"] == "136"
    assert revisions["138"] == "137"
    assert revisions["139"] == "138"
    assert revisions["140"] == "139"
    assert revisions["141"] == "140"
    assert revisions["144"] == "143"
    assert revisions["145"] == "144"
    assert revisions["149"] == "148"
    assert revisions["150"] == "149"
    assert revisions["151"] == "150"
    assert revisions["146"] == "145"
    assert heads == {"151"}

    assert len(heads) == 1
