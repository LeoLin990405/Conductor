"""Refactor deployment status: flags, schema revision and projection row counts.

Canary/rollback needs one read that answers "which refactor surfaces is this
process running, is the schema migrated, and is the projection actually being
written". It reads the same flag catalog and refactor flag service the runtime
uses, so it cannot disagree with the process it reports on.

Usage:
    python -m scripts.refactor_status                 # flags + schema head (static)
    python -m scripts.refactor_status --database      # also row counts (DB env)
    python -m scripts.refactor_status --json /tmp/status.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from psycopg.rows import dict_row
from typing import Any

from src.config.settings import get_refactor_flags, get_refactor_flag_envs

PROJECTION_TABLES: tuple[str, ...] = (
    "agent_goals",
    "agent_plans",
    "agent_events",
    "agent_run_contracts",
    "memory_entries",
    "agent_tasks",
    "agent_thread_permissions",
    "agent_memory_proposals",
)


class StatusError(RuntimeError):
    """The status read could not be completed."""


def repo_schema_head() -> str:
    """The migration head this checkout defines (``090`` style revision id)."""
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        script = ScriptDirectory.from_config(config)
        heads = script.get_heads()
    except Exception as exc:  # alembic missing or bad config: report, don't crash
        raise StatusError(f"cannot read migration head: {exc}") from exc
    if len(heads) != 1:
        raise StatusError(f"migration graph has {len(heads)} heads, expected 1")
    return heads[0]


def schema_state(*, current: str | None, head: str) -> str:
    """Human/enum state: migrated, behind, ahead or unknown."""
    if current is None:
        return "unknown"
    if current == head:
        return "migrated"
    try:
        return "behind" if int(current) < int(head) else "ahead"
    except ValueError:
        return "unknown"


async def database_status() -> dict[str, Any]:
    """Schema revision + one count per projection table from the app database."""
    from src.server.database.pool import get_or_create_pool

    pool = get_or_create_pool()
    await pool.open()
    counts: dict[str, int | None] = {}
    current: str | None = None
    try:
        async with pool.connection() as conn:
            # Named access is load-bearing: the pool's raw connection yields
            # tuples, so a cursor without dict_row fails on a real deployment
            # (found by the first canary run).
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT version_num FROM alembic_version")
                row = await cur.fetchone()
                current = str(row["version_num"]) if row is not None else None
                for table in PROJECTION_TABLES:
                    try:
                        await cur.execute(f"SELECT count(*) AS count FROM {table}")
                        counts[table] = int((await cur.fetchone())["count"])
                    except Exception:
                        # An unmigrated deployment reports the table as absent
                        # instead of failing the whole status read.
                        counts[table] = None
    finally:
        await pool.close()
    return {"schema_revision": current, "row_counts": counts}


def render_status(
    *,
    flags: dict[str, bool],
    envs: dict[str, str],
    head: str | None,
    database: dict[str, Any] | None = None,
) -> str:
    lines = ["| flag | env | state |", "| --- | --- | --- |"]
    for flag in sorted(flags):
        state = "ON" if flags[flag] else "off"
        lines.append(f"| {flag} | `{envs.get(flag, '?')}` | {state} |")
    if head is not None:
        if database is None:
            lines += ["", f"schema head (repo): **{head}** (database not read)"]
        else:
            current = database.get("schema_revision")
            state = schema_state(current=current, head=head)
            lines += ["", f"schema: database **{current or 'unknown'}** · repo head **{head}** · {state}"]
            lines.append("")
            lines.append("| projection table | rows |")
            lines.append("| --- | --- |")
            for table in PROJECTION_TABLES:
                count = (database.get("row_counts") or {}).get(table)
                lines.append(f"| {table} | {'absent' if count is None else count} |")
    return "\n".join(lines)


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="refactor-status", description=__doc__)
    parser.add_argument("--database", action="store_true", help="read schema revision + row counts")
    parser.add_argument("--json", help="write the machine-readable status here")
    args = parser.parse_args(argv)
    flags = get_refactor_flags()
    envs = get_refactor_flag_envs()
    try:
        head = repo_schema_head()
    except StatusError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    database: dict[str, Any] | None = None
    if args.database:
        try:
            database = await database_status()
        except Exception as exc:
            print(f"FAIL: database status read failed: {exc}", file=sys.stderr)
            return 2
    print(render_status(flags=flags, envs=envs, head=head, database=database))
    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "flags": flags,
                    "env": envs,
                    "schema_head": head,
                    "database": database,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
