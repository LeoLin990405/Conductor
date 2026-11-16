"""122 shards Router sessions and adds the bounded peer Worker channel."""

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
    / "122_router_shard_peer_channel.py"
)


@pytest.fixture
def migration(monkeypatch):
    spec = importlib.util.spec_from_file_location("migration_122", _VERSION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    events: list[str] = []
    op = MagicMock()
    op.execute.side_effect = lambda sql: events.append(" ".join(str(sql).split()))
    monkeypatch.setattr(module, "op", op)
    return module, events


def test_migration_follows_121_and_shards_router_sessions(migration):
    module, events = migration
    assert (module.revision, module.down_revision) == ("122", "121")
    module.upgrade()
    sql = " ".join(events)
    assert events[0] == "SET LOCAL lock_timeout = '5s'"
    assert (
        "ALTER TABLE autonomy_router_sessions ADD COLUMN IF NOT EXISTS "
        "shard INTEGER NOT NULL DEFAULT 0"
    ) in sql
    assert (
        "ALTER TABLE autonomy_router_sessions DROP CONSTRAINT IF EXISTS "
        "autonomy_router_sessions_pkey"
    ) in sql
    assert (
        "ALTER TABLE autonomy_router_sessions ADD CONSTRAINT "
        "autonomy_router_sessions_pkey PRIMARY KEY (user_id, shard)"
    ) in sql
    assert "CREATE TABLE IF NOT EXISTS autonomy_peer_messages" in sql
    assert "sender_ticket_id TEXT NOT NULL" in sql
    assert "receiver_ticket_id TEXT NOT NULL" in sql
    assert "read_at TIMESTAMPTZ" in sql
    assert (
        "CREATE INDEX IF NOT EXISTS ix_autonomy_peer_messages_receiver "
        "ON autonomy_peer_messages (receiver_ticket_id, created_at)"
    ) in sql


def test_downgrade_drops_the_channel_and_restores_the_single_key(migration):
    module, events = migration
    module.downgrade()
    assert events == [
        "DROP TABLE IF EXISTS autonomy_peer_messages",
        "DELETE FROM autonomy_router_sessions WHERE (user_id, shard) NOT IN "
        "(SELECT user_id, MIN(shard) FROM autonomy_router_sessions GROUP BY user_id)",
        "ALTER TABLE autonomy_router_sessions DROP CONSTRAINT IF EXISTS "
        "autonomy_router_sessions_pkey",
        "ALTER TABLE autonomy_router_sessions DROP COLUMN IF EXISTS shard",
        "ALTER TABLE autonomy_router_sessions ADD CONSTRAINT "
        "autonomy_router_sessions_pkey PRIMARY KEY (user_id)",
    ]


def test_migration_graph_keeps_124_and_ends_at_142():
    """122 follows 121 and 124 follows 123. integrate/r2's 125/126/127/128 continue after 124; 129 (local policy version) and 130 (signed lease grants) plus this line's ticket migrations 131/132 continue it; memory P0 owns 133-138, so 142 is the only head."""
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
            else (down_revision,) if down_revision is not None else ()
        )
    }
    heads = set(revisions) - parents
    # integrate/r2 pinned this line at 124. feat/sa-a2a continues the chain
    # through 128 (context scopes); 124 stays an ancestor, the endpoint chain
    # adds 129 (local policy version) + 130 (signed lease grants), and this
    # line's ticket migrations 131/132 continue it; memory P0's 133-138 close
    # the chain at 139.
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
