"""From the head down to 116, ``alembic downgrade`` is one transaction.

alembic runs a whole command in the one transaction ``migrations/env.py`` opens,
and ending it part-way (``autocommit_block()``, or a ``COMMIT`` statement) leaves
the schema half-undone with ``alembic_version`` at the last revision before the
break. 131's downgrade used to end in a block (reproduced on PostgreSQL 16 with a
view that depends on ``autonomy_daemon_claims.lease_grant``: ``alembic downgrade
128`` failed at 130 and the database stayed at 131 with 140 to 132 already
undone). MIGRATIONS.md says which revisions this covers; this pins it.

The pin never tries to understand SQL. Two earlier versions did, one by counting
lines of the rendered SQL and one with a lexer, and each was beaten by a spelling
it did not know (``COMMIT; BEGIN`` in one string; a ``--`` comment ended by ``\\r``;
``standard_conforming_strings`` off, so a backslash escapes in an ordinary string). It
is an over-approximation that fails closed, in three parts:

* alembic's own account of what it emitted. Rendering ``alembic downgrade
  140:115 --sql`` emits one ``BEGIN`` and one ``COMMIT`` (``emit_begin`` and
  ``emit_commit``, which ``env.py`` calls around the command and ``autocommit_block``
  calls around its block), whatever SQL strings a revision builds.
* a whole-word search, with no knowledge of quotes or comments, of the Python
  source of every downgrade from the head to 116 (the function and every
  module-level function or constant it names, transitively) and of the SQL that
  downgrade renders. It looks for the words that end or begin a transaction,
  change how SQL text is read or reach for the connection, and for what changes a
  setting past the end of the revision: ``lock_timeout`` in any form but the one
  statement ``SET LOCAL lock_timeout = '5s'``, any ``SET`` that is not ``SET
  LOCAL``, ``RESET``, ``DISCARD``, ``set_config``. A word that appears has to be
  inside an *allowed text*: ``_ALLOWED_TEXTS`` lists, per revision and per place
  (source or SQL), the exact whitespace-collapsed text of each construct that
  legitimately holds such a word (a ``DO $$ BEGIN .. END $$`` guard, an
  ``UPDATE .. SET``, a comment) and how often it occurs. Each is removed from the
  text first and must be there exactly that often; the words that remain are the
  hits, and any hit fails. A hit is never allowed by *number*: counting the
  ``BEGIN`` and ``END`` of a guard lets ``END; BEGIN; SELECT 1; -- END`` stand in
  for it, which has the same counts.
* the shape of each module: one import (``from alembic import op``), no decorator,
  no call outside a function, so nothing is reached that the search does not see.

What the pin guarantees, for every downgrade from the head to 116: alembic emits
one transaction for the range, and no spelling of a ``COMMIT``, ``END``,
``ABORT``, ``ROLLBACK``, ``BEGIN``, ``START TRANSACTION``, ``PREPARE``, savepoint
or ``RELEASE``, autocommit block, ``standard_conforming_strings`` change, ``SET`` that is not
``SET LOCAL``, ``RESET``, ``DISCARD``, ``set_config`` or ``lock_timeout`` other
than the one allowed statement appears outside the allowed texts, in its source
or in the SQL it renders. Because the search is by word on the text itself, a
different layout, comment or case does not hide one (a quoted name that is
written with a Unicode escape is the exception, listed below). The tests at the end
copy the real tree, put each example the reviews produced into a downgrade, change
a legitimate guard in each of the ways listed there (the review's
``END; BEGIN; SELECT 1; -- END`` in place of the guard among them), and require the
pin to fail; and require it to pass on the tree as it is.

What it cannot see:

* a downgrade that behaves differently online than offline (a branch on
  ``is_offline_mode()``) and avoids every word;
* SQL that builds a keyword at run time (PostgreSQL refuses ``COMMIT`` run from
  ``EXECUTE`` or ``DO`` inside a transaction block);
* a name written as a Unicode-escaped quoted identifier (``U&"se\0074_config"``),
  which the search does not decode, so it can hide a callable such as
  ``set_config`` (the word list is a search of the text as written);
* session state that is not a setting (temporary tables, advisory locks,
  ``LISTEN``), which a downgrade could leave behind;
* what an allowed text does beyond the words in it: each is exact and was read
  when it was listed. None holds a transaction statement except the ``BEGIN`` and
  ``END`` of a plpgsql ``DO`` body, in which PostgreSQL refuses ``COMMIT`` inside
  a transaction block;
* the downgrades below 116, which MIGRATIONS.md describes from the derived lists
  and the one reproduction for 115 only.

``tests/integration/test_131_downgrade_atomic.py`` runs the real command from the
head to 116 against PostgreSQL and fails it at the last step.

MIGRATIONS.md carries a block derived here (``<!-- derived:begin -->``) that holds
the sentences about the range and the lists the test relies on; the test
regenerates it and requires it to be identical, so the text cannot drift from the
files, and the prose around it says less than the block does.
"""

from __future__ import annotations

import ast
import collections
import functools
import io
import re
import shutil
import sys
from pathlib import Path
from unittest import mock

import pytest
from alembic import command
from alembic.config import Config
from alembic.ddl.impl import DefaultImpl
from alembic.script import ScriptDirectory

_ROOT = Path(__file__).resolve().parents[3]
_MIGRATIONS = _ROOT / "migrations"

#: The oldest revision whose downgrade may not end the command's transaction.
#: Below it, 115's downgrade still validates its CHECK in an autocommit block.
_ONE_TRANSACTION_FROM = 116

#: The only way a downgrade in the range may mention ``lock_timeout``.
_BOUND = "SET LOCAL lock_timeout = '5s'"

#: MIGRATIONS.md's lock_timeout lists cover the downgrades from the head down to
#: the one a command with this target (094) undoes last, which is 095's.
_LOCK_TIMEOUT_TARGET = "094"

#: Words searched for, whole and case-insensitively, with no regard for quotes or
#: comments. ``start`` is not here: ``START TRANSACTION`` has ``transaction``.
_WORDS = (
    # what ends or begins a transaction, and how
    "commit", "rollback", "abort", "begin", "end", "prepare", "savepoint",
    "release", "transaction", "isolation_level", "execution_options",
    "autocommit_block", "emit_begin", "emit_commit",
    # what changes the meaning of the SQL text that follows
    "standard_conforming_strings", "escape_string_warning",
    # what changes lock_timeout, or sets a setting from a call
    "lock_timeout", "set_config", "reset", "discard", "session",
    # ways to reach the connection, run text, or build a name at run time
    "import", "__import__", "importlib", "getattr", "eval", "exec", "globals",
    "connection", "get_context", "get_bind", "impl",
)  # fmt: skip
_WORD = {word: re.compile(rf"\b{word}\b", re.IGNORECASE | re.ASCII) for word in _WORDS}


def _squash(text: str) -> str:
    """Collapse runs of ASCII whitespace: PostgreSQL's whitespace.

    ``str.split()`` also splits on a no-break space and other Unicode spaces, which
    PostgreSQL reads as identifier characters, so it would turn ``LOCAL\u00a0.x``
    into ``LOCAL .x``.
    """
    return re.sub(r"[ \t\r\n\f\v]+", " ", text).strip(" ")


#: A ``SET`` that is not followed by whitespace and ``LOCAL``: what ``SET`` does
#: outside a transaction block outlives the revision. ``UPDATE .. SET`` is the same
#: text and is let through only as an allowed text. Nothing is required to follow
#: ``SET``: PostgreSQL reads a comment as whitespace (``SET/**/x = 1``), so a
#: pattern that needed the next character to be whitespace would not see it, and
#: ``SET/**/LOCAL x`` is refused too (an over-approximation, on purpose). ``LOCAL``
#: is the keyword only when no identifier character follows it. PostgreSQL's
#: identifier characters are letters, digits, ``_``, ``$`` and every byte above
#: 0x7f, so ``SET LOCAL$x.y`` and ``SET LOCAL\u00a9.x`` set a custom parameter for
#: the session; ``\b`` and Python's ``\w`` (which leaves out ``$`` and symbols such
#: as ``\u00a9``) would not see that, so the class is spelled out. The patterns are
#: ASCII-only (``re.ASCII``) so ``\s`` does not read a no-break space, which
#: PostgreSQL does not treat as whitespace, as a separator.
_SET = re.compile(
    r"\bSET\b(?!\s+LOCAL(?![A-Za-z0-9_$]|[^\x00-\x7f]))", re.IGNORECASE | re.ASCII
)


def _config(script_location: Path) -> Config:
    config = Config(str(_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(script_location))
    # The placeholder never connects: offline mode renders SQL only.
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://u:p@localhost/none")
    # Keeps alembic from reconfiguring the logging of the whole test session.
    config.config_file_name = None
    return config


def _script(script_location: Path = _MIGRATIONS) -> ScriptDirectory:
    return ScriptDirectory.from_config(_config(script_location))


def _head() -> str:
    (head,) = _script().get_heads()
    return head


def _render(
    direction: str, span: str, script_location: Path = _MIGRATIONS
) -> tuple[str, int, int]:
    """The SQL ``alembic <direction> <span> --sql`` writes, and how many times
    alembic emitted ``BEGIN`` and ``COMMIT`` doing it."""
    config = _config(script_location)
    config.output_buffer = out = io.StringIO()
    emitted = collections.Counter()
    real_begin, real_commit = DefaultImpl.emit_begin, DefaultImpl.emit_commit

    def begin(self) -> None:
        emitted["begin"] += 1
        real_begin(self)

    def commit(self) -> None:
        emitted["commit"] += 1
        real_commit(self)

    # alembic writes `<statement>;` after stripping the statement, so one that ends
    # in a `--` comment would take the terminator into the comment. A terminator on
    # a line of its own cannot be swallowed; nothing else about the text changes.
    with (
        mock.patch.object(DefaultImpl, "command_terminator", "\n;"),
        mock.patch.object(DefaultImpl, "emit_begin", begin),
        mock.patch.object(DefaultImpl, "emit_commit", commit),
    ):
        getattr(command, direction)(config, span, sql=True)
    return out.getvalue(), emitted["begin"], emitted["commit"]


def _emitted(
    direction: str, span: str, script_location: Path = _MIGRATIONS
) -> tuple[int, int]:
    _, begins, commits = _render(direction, span, script_location)
    return begins, commits


# --- one downgrade, as source and as SQL ------------------------------------


@functools.cache
def _rendered(revision: str, down: str, script_location: Path = _MIGRATIONS):
    """``(sql, begins, commits)`` of one revision's downgrade rendered alone, or
    ``None`` if it cannot be rendered offline (it reads the database)."""
    try:
        return _render("downgrade", f"{revision}:{down}", script_location)
    except Exception:  # which error it is does not matter: it cannot be rendered
        return None


def _body(revision: str, down: str, script_location: Path = _MIGRATIONS) -> str:
    """The SQL of one rendered downgrade without alembic's own framing (the
    ``BEGIN``, the comment, the ``alembic_version`` update and the ``COMMIT``),
    removed by exact match so that a change in how alembic writes it fails here
    instead of hiding something."""
    rendered = _rendered(revision, down, script_location)
    assert rendered is not None, f"{revision} cannot be rendered offline"
    sql = rendered[0]
    start = f"BEGIN\n;\n\n-- Running downgrade {revision} -> {down}\n\n"
    end = (
        f"UPDATE alembic_version SET version_num='{down}' "
        f"WHERE alembic_version.version_num = '{revision}'\n;\n\nCOMMIT\n;\n\n"
    )
    assert sql.startswith(start) and sql.endswith(end), f"alembic's framing: {sql!r}"
    return sql[len(start) : -len(end)]


def _path(revision: str, script_location: Path = _MIGRATIONS) -> Path:
    (path,) = (script_location / "versions").glob(f"{revision}_*.py")
    return path


def _reachable_source(revision: str, script_location: Path = _MIGRATIONS) -> str:
    """The source of ``downgrade`` and of every module-level function or constant
    it names, and what those name in turn: more than it can call, by design."""
    source = _path(revision, script_location).read_text(encoding="utf-8")
    definitions: dict[str, ast.AST] = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef):
            definitions[node.name] = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    definitions[target.id] = node
    assert "downgrade" in definitions, f"{revision} has no downgrade"
    seen: set[str] = set()
    pending = ["downgrade"]
    segments = []
    while pending:
        name = pending.pop()
        if name in seen or name not in definitions:
            continue
        seen.add(name)
        segments.append(ast.get_source_segment(source, definitions[name]))
        pending += [
            n.id for n in ast.walk(definitions[name]) if isinstance(n, ast.Name)
        ]
    return "\n".join(segments)


def _shape_problems(revision: str, script_location: Path = _MIGRATIONS) -> list[str]:
    """What would let a revision reach something the word search does not read."""
    problems = []
    tree = ast.parse(_path(revision, script_location).read_text(encoding="utf-8"))
    for node in tree.body:
        text = ast.unparse(node)
        if isinstance(node, ast.FunctionDef):
            if node.decorator_list:
                problems.append(f"{revision}: {node.name} has a decorator")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            if any(isinstance(n, (ast.Call, ast.Lambda)) for n in ast.walk(node)):
                problems.append(f"{revision}: a call at module level: {text[:60]!r}")
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            pass  # the docstring
        elif text != "from alembic import op":
            problems.append(f"{revision}: module level: {text[:60]!r}")
    return problems


def _hits(text: str) -> tuple[dict[str, int], int]:
    """How often each word occurs in ``text``, and how often the one statement
    ``lock_timeout`` is allowed in does. Whitespace is collapsed first so that the
    allowed statement is recognised over several lines; it is then removed, so any
    ``lock_timeout`` left is a hit."""
    normal = _squash(text)
    bounds = normal.count(_BOUND)
    normal = normal.replace(_BOUND, " ")
    found = {word: len(pattern.findall(normal)) for word, pattern in _WORD.items()}
    found["SET (not LOCAL)"] = len(_SET.findall(normal))
    return {word: n for word, n in found.items() if n}, bounds


def _construct(
    revision: str, *pieces: str, why: str
) -> list[tuple[str, str, str, int, str]]:
    """The allowed texts for a statement written as Python string pieces: the
    pieces as they stand in the source, and as the SQL alembic renders them."""
    source = " ".join(f'"{piece}"' for piece in pieces)
    return [
        (revision, "source", source, 1, why),
        (revision, "sql", "".join(pieces), 1, why),
    ]


#: ``(revision, "source" | "sql", text, times, reason)``. The exact text, whitespace
#: collapsed and case kept, of each construct from the head down to 116 that holds a
#: word the search looks for, with the number of times it occurs and why it is
#: harmless. Each is removed before the search and must occur exactly ``times``
#: times: a text that is altered, duplicated or gone fails, and so does one that
#: names a revision that is not scanned. A hit is allowed by its text, never by a
#: count of words (see the module docstring).
_ALLOWED_TEXTS: list[tuple[str, str, str, int, str]] = [
    *_construct(
        "139",
        "DO $$ BEGIN ",
        "IF EXISTS (",
        "SELECT 1 FROM autonomy_review_audit WHERE action IN ",
        "('merge_started', 'merge_merged')",
        ") THEN ",
        "RAISE EXCEPTION ",
        "'review/merge audit holds merged rows; archive them before downgrading 139'; ",
        "END IF; END $$",
        why="plpgsql guard: refuses while merged rows exist",
    ),
    *_construct(
        "138",
        "DO $$ BEGIN ",
        "IF EXISTS (SELECT 1 FROM memory_tool_denials WHERE active IS TRUE) THEN ",
        "RAISE EXCEPTION 'retire active memory tool denials before downgrading 138'; ",
        "END IF; END $$",
        why="plpgsql guard: refuses while a tool denial is active",
    ),
    *_construct(
        "137",
        "DO $$ BEGIN ",
        "IF EXISTS (SELECT 1 FROM agent_memory_proposals ",
        "WHERE producer_kind = 'memory_consolidation') THEN ",
        "RAISE EXCEPTION 'remove consolidation proposals before downgrading 137'; ",
        "END IF; END $$",
        why="plpgsql guard: refuses while consolidation proposals exist",
    ),
    *_construct(
        "137",
        "ALTER COLUMN thread_id SET NOT NULL, ",
        "ALTER COLUMN task_run_id SET NOT NULL, ",
        why="`ALTER COLUMN .. SET NOT NULL`, a table change and no session setting",
    ),
    (
        "131",
        "source",
        '""" UPDATE autonomy_tickets SET status = \'cancelled\' '
        'WHERE status = \'archived\' """',
        1,
        "`UPDATE .. SET`, a row change and no session setting",
    ),
    (
        "131",
        "sql",
        "UPDATE autonomy_tickets SET status = 'cancelled' WHERE status = 'archived'",
        1,
        "`UPDATE .. SET`, a row change and no session setting",
    ),
    (
        "131",
        "source",
        "# Added validated, in this transaction: see the module docstring.",
        1,
        "a comment",
    ),
    *_construct(
        "142",
        "UPDATE memory_forget_provider_notices SET status = 'unavailable' ",
        "WHERE status = 'unsupported'",
        why="`UPDATE .. SET`, a row change and no session setting",
    ),
    *_construct(
        "119",
        "UPDATE autonomy_outbox SET status = 'dropped', updated_at = NOW() ",
        "WHERE status IN ('pending_approval', 'rejected')",
        why="`UPDATE .. SET`, a row change and no session setting",
    ),
    (
        "122",
        "source",
        "# Collapse to one session per user before the single-column key returns.",
        1,
        "a comment",
    ),
]


def _strip_allowed(revision: str, where: str, text: str) -> tuple[str, list[str]]:
    """``text`` whitespace-collapsed with the allowed texts of this revision and
    place removed, and the problems with the table: a text that does not occur
    exactly as often as it says."""
    normal = _squash(text)
    problems = []
    for rev, place, allowed, times, _ in _ALLOWED_TEXTS:
        if (rev, place) != (revision, where):
            continue
        allowed = _squash(allowed)
        seen = normal.count(allowed)
        if seen != times:
            problems.append(
                f"{revision} {where}: allowed text occurs {seen} times, "
                f"_ALLOWED_TEXTS says {times}: {allowed[:70]!r}"
            )
        normal = normal.replace(allowed, " ")
    return normal, problems


def _revisions(
    script_location: Path, newest: str, target: str
) -> list[tuple[str, str]]:
    """``(revision, its parent)`` for what ``alembic downgrade <target>`` undoes
    from ``newest``, newest first."""
    return [
        (r.revision, r.down_revision or "base")
        for r in _script(script_location).iterate_revisions(newest, target)
    ]


def _pin_problems(
    script_location: Path = _MIGRATIONS, only: tuple[str, ...] | None = None
) -> list[str]:
    """Everything that stops the downgrades from the head to 116 being one
    transaction, or that the pin cannot vouch for. Empty when none does.
    ``only`` limits the per-revision search (a mutated copy changes one revision;
    each is searched on its own, so the rest would add time and nothing else)."""
    script = _script(script_location)
    (head,) = script.get_heads()
    problems = []

    begins, commits = _emitted(
        "downgrade", f"{head}:{_ONE_TRANSACTION_FROM - 1}", script_location
    )
    if (begins, commits) != (1, 1):
        problems.append(
            f"alembic emitted {begins} BEGIN and {commits} COMMIT for the range "
            "(an autocommit block, or a revision that calls emit_begin/emit_commit)"
        )

    scanned = _revisions(script_location, head, str(_ONE_TRANSACTION_FROM - 1))
    covered = set()
    for revision, down in scanned:
        if only is not None and revision not in only:
            continue
        covered.add(revision)
        problems += _shape_problems(revision, script_location)
        sources = {
            "source": _reachable_source(revision, script_location),
            "sql": _body(revision, down, script_location),
        }
        for where, text in sources.items():
            remaining, table_problems = _strip_allowed(revision, where, text)
            problems += table_problems
            hits, _ = _hits(remaining)
            problems += [
                f"{revision} {where}: {word!r} x{n} outside every allowed text"
                for word, n in hits.items()
            ]

    if only is None:
        for revision in sorted({entry[0] for entry in _ALLOWED_TEXTS} - covered):
            problems.append(f"_ALLOWED_TEXTS names {revision}, which is not scanned")
    return problems


# --- derived facts, for MIGRATIONS.md ---------------------------------------


def _derived_block() -> str:
    """Lists MIGRATIONS.md quotes, computed from the files."""
    head = _head()
    low = _revisions(_MIGRATIONS, head, _LOCK_TIMEOUT_TARGET)

    begins_in, commits_in = _emitted("downgrade", f"{head}:{_ONE_TRANSACTION_FROM - 1}")
    begins_out, commits_out = _emitted(
        "downgrade", f"{head}:{_ONE_TRANSACTION_FROM - 2}"
    )

    begins_with, names_otherwise = [], []
    for revision, down in low:
        normal = _squash(_body(revision, down))
        other = _hits(normal)[0].get("lock_timeout", 0)
        uses = normal.count(_BOUND)
        if normal.startswith(_BOUND):
            begins_with.append(revision)
        if other or uses > 1 or (uses and not normal.startswith(_BOUND)):
            names_otherwise.append(revision)

    ends, unrenderable = [], []
    for revision, down in _revisions(_MIGRATIONS, head, "base"):
        rendered = _rendered(revision, down)
        if rendered is None:
            unrenderable.append(revision)
            if "autocommit_block" in _reachable_source(revision):
                ends.append(revision)
        elif rendered[2] > 1:
            ends.append(revision)

    def ids(revisions: list[str]) -> str:
        return ", ".join(revisions) or "none"

    return "\n".join(
        [
            "<!-- derived:begin -->",
            "Generated from the migration files by `tests/unit/migrations/"
            "test_downgrades_one_transaction.py`, which fails when this block "
            "differs from what the files give; the text around it says less than "
            "the block does.",
            "",
            f"- One transaction: a hand-run `alembic downgrade` is one transaction "
            f"from {head} down to {_ONE_TRANSACTION_FROM} (a target of "
            f"{_ONE_TRANSACTION_FROM - 1} or higher). alembic emits {begins_in} "
            f"`BEGIN` and {commits_in} `COMMIT` for `alembic downgrade "
            f"{head}:{_ONE_TRANSACTION_FROM - 1} --sql` and {begins_out} and "
            f"{commits_out} for `{head}:{_ONE_TRANSACTION_FROM - 2}`, and no "
            "downgrade in that range holds a word that ends or begins a "
            "transaction, changes a setting past the revision, changes how SQL "
            "text is read or reaches the connection, outside the exact texts the "
            "test allows.",
            "- A command whose steps all lie in that range is rolled back whole "
            "when one of them fails, and `alembic current` still prints the "
            "revision it started from: `tests/integration/"
            f"test_131_downgrade_atomic.py` fails `alembic downgrade "
            f"{_ONE_TRANSACTION_FROM - 1}` at {_ONE_TRANSACTION_FROM}, its last "
            "step, on PostgreSQL and requires the database unchanged.",
            f"- Below {_ONE_TRANSACTION_FROM} a downgrade can end the command's "
            f"transaction; from {head} down to 001 these do (they contain an "
            f"`autocommit_block`): {ids(ends)}. A command that fails after one "
            "of them leaves the database at the revision of the last one it "
            "passed, and one that fails before the first it reaches leaves it "
            "where it started (reproduced for 115 only).",
            f"- Downgrades from {head} down to 001 that cannot be rendered "
            f"offline, so that their source was read: {ids(unrenderable)}.",
            f"- Downgrades from {head} down to 095 that begin with `{_BOUND}`: "
            f"{ids(begins_with)}.",
            f"- Downgrades from {head} down to 095 that name `lock_timeout` in "
            f"any other way, or use that statement anywhere but first: "
            f"{ids(names_otherwise)}.",
            "<!-- derived:end -->",
        ]
    )


# --- the pin on the real revisions ------------------------------------------


def test_alembic_emits_one_transaction_for_the_head_down_to_116():
    head = _head()
    sql, begins, commits = _render("downgrade", f"{head}:{_ONE_TRANSACTION_FROM - 1}")

    # The range was rendered, not skipped: 131's own update is in it.
    assert "UPDATE autonomy_tickets SET status = 'cancelled'" in sql
    assert (begins, commits) == (1, 1), (
        "a downgrade ended the command's transaction part-way (an autocommit "
        "block); MIGRATIONS.md says the command is one transaction from "
        f"{head} down to {_ONE_TRANSACTION_FROM}"
    )


def test_the_downgrades_from_the_head_to_116_pass_the_pin():
    assert _pin_problems() == []


def test_the_bound_is_real_115_is_the_next_downgrade_that_commits():
    """Without this the bound above could drift up unnoticed: if 115 stops
    committing, MIGRATIONS.md can name a lower bound."""
    assert _emitted("downgrade", f"{_head()}:{_ONE_TRANSACTION_FROM - 2}") == (2, 2)


def test_an_upgrade_only_autocommit_block_is_not_counted():
    """127 validates its CHECK in an autocommit block in ``upgrade`` only; that
    is the upgrade direction's own, documented break and not this one."""
    assert _emitted("upgrade", "126:127") == (2, 2)
    assert _emitted("downgrade", "127:126") == (1, 1)


def test_migrations_md_derived_block_is_what_the_files_give():
    text = (_ROOT / "MIGRATIONS.md").read_text(encoding="utf-8")
    found = re.search(r"<!-- derived:begin -->.*?<!-- derived:end -->", text, re.DOTALL)
    assert found, "MIGRATIONS.md has no derived block"
    expected = _derived_block()
    assert found.group(0) == expected, (
        "the derived block in MIGRATIONS.md is not what the files give; it "
        f"should read:\n{expected}"
    )


def test_source_and_render_agree_on_which_downgrades_commit():
    """Two ways to find a block, so a revision cannot hide it from one."""
    disagree = []
    for revision, down in _revisions(_MIGRATIONS, _head(), "base"):
        rendered = _rendered(revision, down)
        if rendered is None:
            continue
        in_source = "autocommit_block" in _reachable_source(revision)
        if in_source != (rendered[2] > 1):
            disagree.append(revision)
    assert disagree == []


def test_the_unrenderable_downgrades_are_the_ones_read_from_source():
    """039 and 072 query the database through ``op.get_bind()``, which is ``None``
    offline. 072's downgrade goes through a helper that opens a block, and 039's
    has none: what MIGRATIONS.md says about them rests on this."""
    unrenderable = [
        revision
        for revision, down in _revisions(_MIGRATIONS, _head(), "base")
        if _rendered(revision, down) is None
    ]
    assert unrenderable == ["072", "039"]
    assert "autocommit_block" in _reachable_source("072")
    assert "autocommit_block" not in _reachable_source("039")


# --- the pin fails for what it is meant to refuse ---------------------------

#: ``(lines in the body of 130's downgrade, lines added to the module)``: what a
#: revision could do to end the transaction or to change how a later statement
#: ends it. The first group is what the reviews produced; each ends the
#: transaction, or changes ``lock_timeout`` or how SQL text reads, in a spelling
#: an earlier version of this pin did not see.
_MUTATIONS: dict[str, tuple[list[str], list[str]]] = {
    "COMMIT; BEGIN in one string": (['op.execute("COMMIT; BEGIN")'], []),
    "lower case, a comment between": (['op.execute("commit; -- x\\nbegin")'], []),
    "ROLLBACK": (['op.execute("ROLLBACK; BEGIN")'], []),
    "an autocommit block": (
        [
            "with op.get_context().autocommit_block():",
            '    op.execute("SELECT 1")',
        ],
        [],
    ),
    "a comment ended by a carriage return": (
        ['op.execute("SELECT 1; -- comment\\rCOMMIT; BEGIN")'],
        [],
    ),
    "standard_conforming_strings off, then a backslash string": (
        [
            'op.execute("SET standard_conforming_strings=off")',
            "op.execute(r\"SELECT '\\' x '; COMMIT; BEGIN; SELECT '\\' x '\")",
        ],
        [],
    ),
    "two set_config calls, the session one first": (
        [
            "op.execute(\"SELECT set_config('lock_timeout','0',false), "
            "set_config('application_name','review',true)\")",
        ],
        [],
    ),
    # Further spellings.
    "a block looked up by name": (
        [
            'with getattr(op.get_context(), "autocommit" + "_block")():',
            '    op.execute("SELECT 1")',
        ],
        [],
    ),
    "a block opened by a helper two calls away": (
        ["_outer()"],
        [
            "def _inner():",
            "    with op.get_context().autocommit_block():",
            '        op.execute("SELECT 1")',
            "def _outer():",
            "    _inner()",
        ],
    ),
    "START TRANSACTION split by a comment": (
        ['op.execute("COMMIT; START /* c */ TRANSACTION")'],
        [],
    ),
    "a keyword built from two strings": (['op.execute("COM" + "MIT; BEG" + "IN")'], []),
    "a statement ending in a comment, then COMMIT": (
        ['op.execute("SELECT 1 -- x")', 'op.execute("COMMIT")'],
        [],
    ),
    "a plain SET lock_timeout": (['op.execute("SET lock_timeout = 0")'], []),
    "RESET lock_timeout": (['op.execute("RESET lock_timeout")'], []),
    "SET LOCAL with another value": (
        ["op.execute(\"SET LOCAL lock_timeout = '9s'\")"],
        [],
    ),
    "SET SESSION": (["op.execute(\"SET SESSION lock_timeout = '1s'\")"], []),
    "a session setting that is not lock_timeout": (
        ["op.execute(\"SET application_name = 'pin-review'\")"],
        [],
    ),
    "the same in lower case, over two lines": (
        ['op.execute("set\\n   search_path = public")'],
        [],
    ),
    "SET built from two strings": (['op.execute("SE" "T x = 1")'], []),
    "SET with a block comment as the whitespace": (
        ["op.execute(\"SET/**/application_name = 'pin-review'\")"],
        [],
    ),
    "SET with a line comment as the whitespace": (
        ['op.execute("SET--c\\napplication_name = 1")'],
        [],
    ),
    "SET LOCAL followed by a dollar sign, a session parameter name": (
        ["op.execute(\"SET LOCAL$x.y = 'pin-review'\")"],
        [],
    ),
    "SET LOCAL followed by a symbol that is an identifier character": (
        ["op.execute(\"SET LOCAL\\u00a9.x = 'pin-review'\")"],
        [],
    ),
    "SET LOCAL followed by a no-break space, which is not whitespace": (
        ["op.execute(\"SET LOCAL\\u00a0.x = 'pin-review'\")"],
        [],
    ),
    "SET LOCAL followed by an underscore": (
        ["op.execute(\"SET LOCAL_x = 'pin-review'\")"],
        [],
    ),
    "SET LOCAL with a comment between the words": (
        ["op.execute(\"SET/**/LOCAL application_name = 'x'\")"],
        [],
    ),
    "DISCARD ALL": (['op.execute("DISCARD ALL")'], []),
    "ALTER ROLE .. SET": (['op.execute("ALTER ROLE CURRENT_USER SET x.y = 1")'], []),
    "an eval": (["eval(\"op.execute('COMMIT')\")"], []),
    "a function-level import": (["import os"], []),
    "an import at module level": ([], ["import os"]),
    "a call at module level": ([], ["_x = print('hi')"]),
    "a statement at module level": ([], ["if True:", "    pass"]),
    "a decorated downgrade helper": (
        ["_h()"],
        ["@staticmethod", "def _h():", "    pass"],
    ),
}


def _copy_with_130_changed(tmp_path: Path, body: list[str], module: list[str]) -> Path:
    tree = tmp_path / "migrations"
    shutil.copytree(
        _MIGRATIONS, tree, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    path = _path("130", tree)
    text = path.read_text(encoding="utf-8")
    header = "def downgrade() -> None:\n"
    assert text.count(header) == 1, "130's downgrade is not the shape this edits"
    text = text.replace(header, header + "".join(f"    {line}\n" for line in body))
    path.write_text(text + "\n" + "\n".join(module) + "\n", encoding="utf-8")
    return tree


def test_the_copy_the_mutations_edit_passes_unchanged(tmp_path):
    tree = _copy_with_130_changed(tmp_path, [], [])

    assert _pin_problems(tree, only=("130",)) == []


@pytest.mark.parametrize("name", _MUTATIONS)
def test_the_pin_fails_for_a_copy_of_130_that_does(tmp_path, name):
    body, module = _MUTATIONS[name]
    tree = _copy_with_130_changed(tmp_path, body, module)

    problems = _pin_problems(tree, only=("130",))

    assert problems, f"the pin would accept: {name}"


#: Ways to change 139's legitimate guard (``DO $$ BEGIN .. END $$``), each a
#: function of the guard's source as written in the file. Counting the guard's
#: ``BEGIN`` and ``END`` once let the first of these through.
_GUARD_MUTATIONS = {
    "the guard replaced by END; BEGIN; SELECT 1; -- END": lambda guard: (
        '    op.execute("END; BEGIN; SELECT 1; -- END")\n'
    ),
    "a statement added inside the guard's text": lambda guard: guard.replace(
        '"END IF; END $$"', '"END IF; SELECT 1; END $$"'
    ),
    "a COMMIT added inside the guard's text": lambda guard: guard.replace(
        '"END IF; END $$"', '"END IF; COMMIT; END $$"'
    ),
    "one character of the guard's text changed": lambda guard: guard.replace(
        "downgrading 139", "downgrading 138"
    ),
    "the guard's text duplicated": lambda guard: guard + guard,
    "the guard intact, then END; BEGIN": lambda guard: (
        guard + '    op.execute("END; BEGIN")\n'
    ),
    "END; BEGIN added after the guard in the same string": lambda guard: guard.replace(
        '"END IF; END $$"', '"END IF; END $$; END; BEGIN"'
    ),
    "the guard intact, then a second DO block": lambda guard: (
        guard + '    op.execute("DO $$ BEGIN PERFORM 1; END $$")\n'
    ),
}


def _copy_with_139_guard_changed(tmp_path: Path, change) -> Path:
    tree = tmp_path / "migrations"
    shutil.copytree(
        _MIGRATIONS, tree, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    path = _path("139", tree)
    text = path.read_text(encoding="utf-8")
    start = text.index(
        '    op.execute(\n        "DO $$ BEGIN "', text.index("def downgrade")
    )
    marker = '"END IF; END $$"\n    )\n'
    end = text.index(marker, start) + len(marker)
    path.write_text(
        text[:start] + change(text[start:end]) + text[end:], encoding="utf-8"
    )
    return tree


def test_the_copy_the_guard_mutations_edit_passes_unchanged(tmp_path):
    tree = _copy_with_139_guard_changed(tmp_path, lambda guard: guard)

    assert _pin_problems(tree, only=("139",)) == []


@pytest.mark.parametrize("name", _GUARD_MUTATIONS)
def test_the_pin_fails_for_a_copy_of_139_whose_guard_is_changed(tmp_path, name):
    tree = _copy_with_139_guard_changed(tmp_path, _GUARD_MUTATIONS[name])

    assert _pin_problems(tree, only=("139",)), f"the pin would accept: {name}"


def test_the_pin_fails_for_an_allowed_text_that_is_not_there(monkeypatch):
    monkeypatch.setattr(
        sys.modules[__name__],
        "_ALLOWED_TEXTS",
        [*_ALLOWED_TEXTS, ("130", "sql", "SELECT 1", 1, "not in 130")],
    )
    assert _pin_problems(only=("130",)) != []


def test_the_pin_fails_for_an_allowed_text_that_occurs_a_different_number_of_times(
    monkeypatch,
):
    entry = next(e for e in _ALLOWED_TEXTS if e[:2] == ("139", "sql"))
    changed = [
        e if e is not entry else (*entry[:3], 2, entry[4]) for e in _ALLOWED_TEXTS
    ]
    monkeypatch.setattr(sys.modules[__name__], "_ALLOWED_TEXTS", changed)

    assert _pin_problems(only=("139",)) != []


def test_the_pin_fails_for_an_allowed_text_that_names_a_revision_not_scanned(
    monkeypatch,
):
    monkeypatch.setattr(
        sys.modules[__name__],
        "_ALLOWED_TEXTS",
        [*_ALLOWED_TEXTS, ("999", "sql", "END", 1, "no such revision")],
    )
    assert _pin_problems() != []


def test_the_allowed_bound_is_the_only_way_to_name_lock_timeout():
    assert _hits("SET LOCAL lock_timeout = '5s'") == ({}, 1)
    assert _hits("set local lock_timeout = '5s'")[0] == {"lock_timeout": 1}
    assert _hits("SET LOCAL lock_timeout = '5s'; SET lock_timeout = 0") == (
        {"lock_timeout": 1, "SET (not LOCAL)": 1},
        1,
    )
    # SET LOCAL of anything else lasts as long as the transaction and no longer
    assert _hits("SET LOCAL application_name = 'x'") == ({}, 0)
    assert _hits("SET application_name = 'x'")[0] == {"SET (not LOCAL)": 1}
    # a comment is whitespace to PostgreSQL, and nothing has to follow the word
    assert _hits("SET/**/application_name = 'x'")[0] == {"SET (not LOCAL)": 1}
    assert _hits("SET--c\napplication_name = 'x'")[0] == {"SET (not LOCAL)": 1}
    assert _hits("SET/**/LOCAL application_name = 'x'")[0] == {"SET (not LOCAL)": 1}
    # LOCAL is the keyword only when no identifier character follows
    assert _hits("SET LOCAL$x.y = 'x'")[0] == {"SET (not LOCAL)": 1}
    assert _hits("SET LOCAL_x = 'x'")[0] == {"SET (not LOCAL)": 1}
    # every byte above 0x7f is an identifier character, a no-break space included
    assert _hits("SET LOCAL\u00a9.x = 'x'")[0] == {"SET (not LOCAL)": 1}
    assert _hits("SET LOCAL\u00a0.x = 'x'")[0] == {"SET (not LOCAL)": 1}
    assert _hits("SET\u00a0LOCAL x = 'x'")[0] == {"SET (not LOCAL)": 1}
    assert _squash("a\u00a0 b\t\n c") == "a\u00a0 b c"
    assert _hits("SET LOCAL x = 'x'") == ({}, 0)
    # whitespace is not part of the statement
    assert _hits("SET   LOCAL\nlock_timeout\n=\n'5s'") == ({}, 1)
