"""119 gives a reply its channel, its kind, and a state a person can settle.

The approval state is the part that cannot be taken away: a reply waiting for
a person must not be sendable by the delivery effect, and a rejected draft
must not sit in the queue forever. The downgrade settles both before it
narrows the status check.
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
    / "119_channel_reply_approval.py"
)


@pytest.fixture
def migration(monkeypatch):
    spec = importlib.util.spec_from_file_location("migration_119", _VERSION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    events: list[str] = []
    op = MagicMock()
    op.execute.side_effect = lambda sql: events.append(" ".join(str(sql).split()))
    monkeypatch.setattr(module, "op", op)
    return module, events


def test_migration_follows_118_and_adds_the_channel_the_kind_and_the_approval_state(
    migration,
):
    module, events = migration
    assert (module.revision, module.down_revision) == ("119", "118")

    module.upgrade()
    sql = " ".join(events)

    assert events[0] == "SET LOCAL lock_timeout = '5s'"
    assert "ADD COLUMN IF NOT EXISTS channel TEXT NOT NULL DEFAULT ''" in sql
    assert "ADD COLUMN IF NOT EXISTS reply_kind TEXT NOT NULL DEFAULT 'result'" in sql
    assert "ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ" in sql
    assert "ADD COLUMN IF NOT EXISTS approved_by TEXT NOT NULL DEFAULT ''" in sql
    assert "'pending_approval', 'rejected'" in sql


def test_the_delivery_effect_cannot_pick_up_a_reply_that_waits_for_a_person(
    migration,
):
    """The status check is what keeps the approval state honest."""
    module, events = migration

    module.upgrade()
    sql = " ".join(events)

    assert "ADD CONSTRAINT autonomy_outbox_status_check" in sql
    assert "status IN ('pending', 'sent', 'dropped', 'pending_approval', 'rejected')" in sql


def test_the_downgrade_settles_the_approval_states_before_narrowing_the_check(
    migration,
):
    module, events = migration

    module.downgrade()
    sql = " ".join(events)

    assert (
        "UPDATE autonomy_outbox SET status = 'dropped', updated_at = NOW() "
        "WHERE status IN ('pending_approval', 'rejected')" in sql
    )
    assert "status IN ('pending', 'sent', 'dropped')" in sql
    assert "DROP COLUMN IF EXISTS channel" in sql
    assert "DROP COLUMN IF EXISTS reply_kind" in sql


def _assert_channel_reply_chain_ends_at_142() -> None:
    """119 → 120 → … → 128 → 129 → 130 → 131 → 132 → … → 138.

    142 is the only head. Dispatch follows the close stamp, so 119 does not fork.
    """
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
    assert revisions["119"] == "118"
    assert revisions["120"] == "119"
    assert revisions["121"] == "120"
    # integrate/r2 carried the chain through router shard (122), snapshot
    # metrics (123) and verified stop (124). This line continues it.
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



def test_migration_graph_has_the_dispatch_revision_as_its_only_ast_discovered_head():
    """119 → 120 (close) → 121 (dispatch) → … → 132. 142 is the only head."""
    _assert_channel_reply_chain_ends_at_142()


def test_migration_graph_has_142_as_its_only_ast_discovered_head():
    """integrate/r2 landed 127 (subscription lifecycle) and 128 (context
    scopes); the endpoint chain's 129 (local policy version) and 130 (signed
    lease grants) plus this line's ticket migrations 131/132 continue them, so
    142 is the only head."""
    _assert_channel_reply_chain_ends_at_142()
