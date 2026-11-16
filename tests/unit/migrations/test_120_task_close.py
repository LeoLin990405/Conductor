"""120 stamps a task when a person closes it.

Revision 119 belongs to the channel-reply approval migration. Reusing it
gives Alembic two heads once that revision is on the same branch.
"""

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
    / "120_task_close.py"
)


@pytest.fixture
def migration(monkeypatch):
    spec = importlib.util.spec_from_file_location("migration_120", _VERSION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    events: list[str] = []
    op = MagicMock()
    op.execute.side_effect = lambda sql: events.append(" ".join(str(sql).split()))
    monkeypatch.setattr(module, "op", op)
    return module, events


def test_migration_follows_119_and_adds_the_close_stamp(migration):
    module, events = migration
    assert (module.revision, module.down_revision) == ("120", "119")
    module.upgrade()
    sql = " ".join(events)
    assert events[0] == "SET LOCAL lock_timeout = '5s'"
    assert (
        "ALTER TABLE autonomy_tickets ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ"
        in sql
    )
    assert "ADD COLUMN IF NOT EXISTS close_reason TEXT NOT NULL DEFAULT ''" in sql


def test_downgrade_drops_the_close_stamp(migration):
    module, events = migration
    module.downgrade()
    assert events == [
        "ALTER TABLE autonomy_tickets DROP COLUMN IF EXISTS close_reason",
        "ALTER TABLE autonomy_tickets DROP COLUMN IF EXISTS closed_at",
    ]


def test_migration_graph_chains_120_after_119_and_before_121():
    """120 is the close stamp between channel-reply approval and dispatch."""
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
    assert revisions["119"] == "118"
    assert revisions["120"] == "119"
    assert revisions["121"] == "120"
    # integrate/r2 carried the chain through 122/123 to sa-verified-stop (124)
    # and on through 128 (context scopes); the endpoint chain adds 129 (local
    # policy version) and 130 (signed lease grants), and this line's ticket
    # migrations 131/132 continue it; memory P0 owns 133-138 and 139 adds the
    # review queue + merge audit, so the single head is 142.
    assert revisions["122"] == "121"
    assert revisions["123"] == "122"
    assert revisions["124"] == "123"
    assert revisions["125"] == "124"
    assert revisions["126"] == "125"
    assert revisions["127"] == "126"
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
