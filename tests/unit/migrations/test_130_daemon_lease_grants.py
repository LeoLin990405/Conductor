"""129 stores the signed daemon grant and the moment the lease was revoked."""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_VERSION = (
    Path(__file__).resolve().parents[3]
    / "migrations"
    / "versions"
    / "130_daemon_lease_grants.py"
)
_DAEMONS = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "server"
    / "database"
    / "autonomy"
    / "daemons.py"
)
_GRANT_SQL_FUNCTIONS = frozenset(
    {"read_lease_grant", "store_lease_grant", "lease_binding"}
)
# Column names Postgres rejects unless quoted. GRANT is the one this
# migration almost shipped. The list is the reserved category from the
# PostgreSQL keyword appendix, not every keyword a statement uses as syntax.
_RESERVED_COLUMNS = frozenset(
    {
        "all", "analyse", "analyze", "and", "any", "array", "as", "asc",
        "asymmetric", "authorization", "both", "case", "cast", "check",
        "collate", "column", "constraint", "create", "current_catalog",
        "current_date", "current_role", "current_time", "current_timestamp",
        "current_user", "default", "deferrable", "desc", "distinct", "do",
        "else", "end", "except", "false", "fetch", "for", "foreign", "from",
        "grant", "group", "having", "in", "initially", "intersect", "into",
        "lateral", "leading", "limit", "localtime", "localtimestamp", "not",
        "null", "offset", "on", "only", "or", "order", "placing", "primary",
        "references", "returning", "select", "session_user", "some",
        "symmetric", "table", "then", "to", "trailing", "true", "union",
        "unique", "user", "using", "variadic", "when", "where", "window",
        "with",
    }
)
_IDENT = r'(?:"[^"]+"|[A-Za-z_][\w]*)'
_COLUMN_PATTERNS = (
    re.compile(
        rf"\bADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>{_IDENT})", re.I
    ),
    re.compile(
        rf"\bDROP\s+COLUMN\s+(?:IF\s+EXISTS\s+)?(?P<name>{_IDENT})", re.I
    ),
    re.compile(rf"\bSET\s+(?P<name>{_IDENT})\s*=", re.I),
    re.compile(rf"\bRETURNING\s+(?P<name>{_IDENT})", re.I),
    re.compile(rf"\(\(\s*(?P<name>{_IDENT})\s*->>", re.I),
    re.compile(rf"\b(?:WHERE|AND|OR)\s+(?P<name>{_IDENT})\s+IS\b", re.I),
    re.compile(r"\bSELECT\s+(?P<cols>.+?)\s+FROM\b", re.I | re.S),
)


def _strip_sql_strings(sql: str) -> str:
    return re.sub(r"'(?:''|[^'])*'", "''", sql)


def unquoted_reserved_columns(sql: str) -> list[str]:
    """Reserved words used as unquoted column names in this migration's SQL.

    Quoted identifiers are legal. Keywords that introduce syntax (`SELECT`,
    `WHERE`) are not in column position, so they are not reported.
    """
    text = _strip_sql_strings(sql)
    names: list[str] = []
    for pattern in _COLUMN_PATTERNS:
        for match in pattern.finditer(text):
            if "cols" in match.groupdict() and match.group("cols") is not None:
                pieces = match.group("cols").split(",")
            else:
                pieces = [match.group("name")]
            for piece in pieces:
                ident = re.match(rf"\s*({_IDENT})", piece)
                if ident is None:
                    continue
                token = ident.group(1)
                if token.startswith('"'):
                    continue
                if token.lower() in _RESERVED_COLUMNS:
                    names.append(token.lower())
    return names


def _function_sql(path: Path, names: frozenset[str]) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef) or node.name not in names:
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                statement = child.value.upper()
                if "SELECT" in statement or "UPDATE" in statement:
                    found.append(child.value)
    return found


@pytest.fixture
def migration(monkeypatch):
    spec = importlib.util.spec_from_file_location("migration_130", _VERSION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    events: list[str] = []
    op = MagicMock()
    op.execute.side_effect = lambda sql: events.append(" ".join(str(sql).split()))
    monkeypatch.setattr(module, "op", op)
    return module, events


def test_migration_follows_129_and_adds_the_grant_and_revocation(migration):
    module, events = migration
    assert (module.revision, module.down_revision) == ("130", "129")
    module.upgrade()
    assert events[0] == "SET LOCAL lock_timeout = '5s'"
    assert (
        "ALTER TABLE autonomy_daemon_claims "
        "ADD COLUMN IF NOT EXISTS lease_grant JSONB"
    ) in events
    assert (
        "ALTER TABLE autonomy_daemon_claims "
        "ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ"
    ) in events
    assert any(
        "idx_autonomy_daemon_claims_lease_grant_nonce" in event for event in events
    )
    assert any("lease_grant->>'nonce'" in event for event in events)


def test_downgrade_drops_the_grant_and_revocation(migration):
    module, events = migration
    module.downgrade()
    assert events == [
        "DROP INDEX IF EXISTS idx_autonomy_daemon_claims_lease_grant_nonce",
        "ALTER TABLE autonomy_daemon_claims DROP COLUMN IF EXISTS revoked_at",
        "ALTER TABLE autonomy_daemon_claims DROP COLUMN IF EXISTS lease_grant",
    ]


def test_unquoted_grant_is_the_reserved_word_the_check_rejects():
    flagged = unquoted_reserved_columns(
        "ALTER TABLE autonomy_daemon_claims "
        "ADD COLUMN IF NOT EXISTS grant JSONB"
    )
    assert flagged == ["grant"]
    assert (
        unquoted_reserved_columns(
            "ALTER TABLE autonomy_daemon_claims "
            'ADD COLUMN IF NOT EXISTS "grant" JSONB'
        )
        == []
    )
    assert (
        unquoted_reserved_columns(
            "ALTER TABLE autonomy_daemon_claims "
            "ADD COLUMN IF NOT EXISTS lease_grant JSONB"
        )
        == []
    )


def test_lease_sql_does_not_use_an_unquoted_reserved_column(migration):
    module, events = migration
    module.upgrade()
    module.downgrade()
    statements = events + _function_sql(_DAEMONS, _GRANT_SQL_FUNCTIONS)
    flagged = [
        name for sql in statements for name in unquoted_reserved_columns(sql)
    ]
    assert flagged == []
    assert _function_sql(_DAEMONS, _GRANT_SQL_FUNCTIONS)
    assert any("lease_grant" in sql for sql in statements)


def test_migration_graph_has_142_as_its_only_ast_discovered_head():
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
