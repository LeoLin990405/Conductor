"""Code-assembled inputs for one Super-agent assignment call.

Nothing here asks a model, reads a file, or writes an outcome. Task text
is normalized, then redacted, then cut, so a fullwidth credential is the
form the redactor knows and a credential is never sliced in half.
Area buckets and difficulty are features of the line the planner already
declared. Success rates are a read of the same ``dispatches.outcomes``
rows ``MemoryP0OutcomeStore`` uses, through ``MemoryPort.search`` only.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from src.llms.native_cli.failure import redact_credentials
from src.tools.auto_dispatch.outcomes import COLLECTION
from src.tools.auto_dispatch.types import Line

# The planner refuses a line task past this many bytes. Cutting here, after
# redaction, keeps a line the planner accepted whole and still bounds a
# caller that did not go through the planner.
TASK_TEXT_LIMIT = 8_000
# One scope entry is a path, not a paragraph. Same ceiling the planner uses.
_SCOPE_ENTRY_LIMIT = 512
# ``DispatchRowMemoryPort.search`` and ``MemoryPort.search`` both stop at
# the newest 500 rows. A rollup past that cap is a later change.
_OUTCOME_SEARCH_LIMIT = 500

# Top-level trees that are one area. ``src`` is not here: a path under it
# buckets by the first three directories, so ``src/tools/auto_dispatch`` and
# ``src/llms/native_cli`` stay apart. The trailing slash matches the way
# those trees are named in the assignment notes.
_INVISIBLE_CATEGORIES = frozenset({"Cf", "Mn", "Me"})
# Braille blank, Hangul filler, and halfwidth Hangul filler. NFKC does not
# remove them, and any of them can sit inside a word the redactor should see.
_INVISIBLE_CHARS = frozenset("\u2800\u3164\u115f\u1160\uffa0")

_TOP_LEVEL_AREAS = frozenset(
    {
        "web",
        "desktop",
        "scripts",
        "migrations",
        "docs",
        "tests",
        "deploy",
        "plugins",
        "extensions",
        "config",
        "contrib",
        "workflows",
        "skills",
        "prototypes",
        "reverse-engineering",
        "mcp_servers",
        "libs",
    }
)


@dataclass(frozen=True, slots=True)
class DifficultyFeatures:
    """Features computed from the line, not from a model.

    ``scope_entries`` is how many write_scope entries the line declared.
    It is not a count of files on disk: this package does not read the tree.
    ``dag_depth`` is the longest dependency chain under the line. A line
    with no dependencies is 0. A cycle edge adds nothing, so the walk stops.
    """

    scope_entries: int
    directories: tuple[str, ...]
    risk_bearing: bool
    dag_depth: int
    planner_large: bool


@dataclass(frozen=True, slots=True)
class LineInput:
    """One line, after redaction and bucketing, ready to render.

    ``acceptance_count`` is how many ``cmd:`` commands G2 would run. A prose
    assertion is not a command and is not counted.
    """

    line_id: str
    task_text: str
    task_truncated: bool
    areas: tuple[str, ...]
    write_scope: tuple[str, ...]
    acceptance_count: int
    depends_on: tuple[str, ...]
    difficulty: DifficultyFeatures


@dataclass(frozen=True, slots=True)
class ProviderRate:
    """Pass rate for one model, over the outcome rows the port returned."""

    model: str
    runs: int
    passed: int

    @property
    def rate(self) -> float:
        return self.passed / self.runs


@dataclass(frozen=True, slots=True)
class AreaRate:
    """Pass rate for one model in one write_scope area.

    A row counts here only when it carries an area. Today's
    ``Outcome.to_dict()`` has none, so those rows stay in
    ``success_by_provider`` and this table stays empty for them.
    """

    model: str
    area: str
    runs: int
    passed: int

    @property
    def rate(self) -> float:
        return self.passed / self.runs


@dataclass(frozen=True, slots=True)
class AssignmentInputs:
    """Everything the assignment call is allowed to see.

    Built by code from the lines and one search of outcome rows. No tool
    results and no file contents.
    """

    lines: tuple[LineInput, ...]
    success_by_provider: tuple[ProviderRate, ...]
    success_rate_by_area: tuple[AreaRate, ...]
    #: N73 L4: trusted, code-built facts -- providers that can launch now and
    #: their headroom, the role registry's preferred order and avoid reasons,
    #: the runtimes the launcher supports. Empty keeps the L1 prompt as it was.
    hints: tuple[tuple[str, str], ...] = ()
    #: line id -> reason code of a first choice that was infeasible (re-ask).
    infeasible: tuple[tuple[str, str], ...] = ()


def truncate_utf8(text: str, limit: int) -> tuple[str, bool]:
    """Cut ``text`` to ``limit`` bytes without splitting a code point."""
    if limit < 0:
        limit = 0
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text, False
    return raw[:limit].decode("utf-8", errors="ignore"), True


def normalize_untrusted(text: str) -> str:
    """NFKC, then drop format and filler characters.

    Fullwidth letters become ASCII here, before credential redaction.
    A bearer token written in fullwidth is the same string the redactor
    already matches. Folding after redaction would reveal it again.
    """
    folded = unicodedata.normalize("NFKC", text or "")
    return "".join(
        char
        for char in folded
        if unicodedata.category(char) not in _INVISIBLE_CATEGORIES
        and char not in _INVISIBLE_CHARS
    )


def bound_task_text(task: str, *, limit: int = TASK_TEXT_LIMIT) -> tuple[str, bool]:
    """Normalize a task, redact it, then cut it.

    Normalization runs first so a fullwidth credential is visible to the
    redactor. Redaction runs before the cut so the cut cannot leave the
    tail of a credential the redactor would have removed whole.
    """
    redacted = str(redact_credentials(normalize_untrusted(task or "")))
    return truncate_utf8(redacted, limit)


def _redact_label(text: str) -> str:
    """One redacted label, or ``""`` when nothing remains.

    Area names and model labels are copied into the trusted half of the
    prompt. This is a last pass on a finished label. A path is redacted
    before it is split; see ``_scope_directories``.
    """
    redacted = str(redact_credentials(normalize_untrusted(text or "")))
    clipped, _truncated = truncate_utf8(" ".join(redacted.split()), _SCOPE_ENTRY_LIMIT)
    return clipped


def area_bucket(scope: str) -> str:
    """The area bucket for one write_scope entry, or ``""`` when it has none.

    ``web/**`` is ``web/``. ``src/tools/auto_dispatch/machine.py`` is
    ``src/tools/auto_dispatch``. ``src/llms/native_cli/...`` is
    ``src/llms/native_cli``. A filename is not part of the bucket. A glob
    is stripped before the split, and a ``..`` segment is not an area.
    The raw scope is redacted before that split, so a credential in a URL
    does not become part of the label.
    """
    parts = _scope_directories(scope)
    if not parts:
        return ""
    if parts[0] in _TOP_LEVEL_AREAS:
        bucket = parts[0] + "/"
    else:
        bucket = "/".join(parts[:3])
    return _redact_label(bucket)


def area_buckets(scopes: Sequence[str]) -> tuple[str, ...]:
    """Sorted unique area buckets for a line's write_scope."""
    found = {bucket for scope in scopes if (bucket := area_bucket(scope))}
    return tuple(sorted(found))


def dependency_depths(lines: Sequence[Line]) -> dict[str, int]:
    """Longest dependency chain under each line.

    A missing dependency is one hop onto an empty chain. Edges inside a
    cycle add nothing: the lines in one strongly connected component share
    a depth, and that depth comes only from components outside the cycle.
    A walk that stopped at the first back edge would give the two lines of
    a cycle different depths depending on which one it saw first.
    """
    by_id: dict[str, Line] = {}
    for line in lines:
        by_id[line.id] = line
    if not by_id:
        return {}

    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    comp_of: dict[str, int] = {}
    comp_count = 0

    def visit(node: str) -> None:
        nonlocal index, comp_count
        indices[node] = lowlink[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for dep in by_id[node].depends_on:
            dep_id = str(dep)
            if dep_id not in by_id:
                continue
            if dep_id not in indices:
                visit(dep_id)
                lowlink[node] = min(lowlink[node], lowlink[dep_id])
            elif dep_id in on_stack:
                lowlink[node] = min(lowlink[node], indices[dep_id])
        if lowlink[node] != indices[node]:
            return
        while True:
            item = stack.pop()
            on_stack.remove(item)
            comp_of[item] = comp_count
            if item == node:
                break
        comp_count += 1

    for node in by_id:
        if node not in indices:
            visit(node)

    # The missing-dependency target is not a line. It has depth 0, and an
    # edge to it still counts as one hop.
    missing = comp_count
    edges: list[set[int]] = [set() for _ in range(comp_count + 1)]
    for node, line in by_id.items():
        here = comp_of[node]
        for dep in line.depends_on:
            dep_id = str(dep)
            if dep_id not in comp_of:
                edges[here].add(missing)
                continue
            other = comp_of[dep_id]
            if other != here:
                edges[here].add(other)

    memo: dict[int, int] = {missing: 0}

    def comp_depth(comp_id: int, seen: frozenset[int]) -> int:
        cached = memo.get(comp_id)
        if cached is not None and comp_id not in seen:
            return cached
        if comp_id in seen:
            return 0
        best = 0
        nxt = seen | {comp_id}
        for dep_comp in edges[comp_id]:
            best = max(best, 1 + comp_depth(dep_comp, nxt))
        memo[comp_id] = best
        return best

    for comp_id in range(comp_count):
        comp_depth(comp_id, frozenset())
    return {node: memo[comp_of[node]] for node in by_id}


def _scope_directories(scope: str) -> list[str]:
    """Directory parts of one scope entry, with the filename removed.

    Fold and redact the raw scope before ``/`` is rewritten or split.
    URL userinfo is only recognized while ``scheme://user:password@host``
    is still intact. Splitting first turns the password into its own
    segment, and that segment would be copied into the assignment prompt.
    A credential value may swallow the slash that follows it. The
    ``[redacted]`` segment that remains is dropped with the filename, so
    the secret still does not become a directory label. Folding first
    also puts a fullwidth tree name, or a name with an invisible
    character in it, in the same bucket as the ASCII path.
    """
    cleaned = str(redact_credentials(normalize_untrusted(scope or ""))).strip()
    cleaned = cleaned.replace("\\", "/").replace("**", "").replace("*", "")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    parts = [part for part in cleaned.split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        return []
    # A dotted name is a file. A trailing redacted segment was a file too:
    # the credential redactor can swallow the suffix along with the secret.
    if "." in parts[-1] or "[redacted]" in parts[-1]:
        parts = parts[:-1]
    return parts


def _one_line(text: str, limit: int) -> str:
    """A redacted single line, cut to ``limit`` bytes.

    Newlines are flattened so a scope or an id cannot start a new prompt
    section. The task body is not flattened; it stays inside its block.
    """
    normalized = normalize_untrusted(text or "")
    redacted = str(redact_credentials(" ".join(normalized.split())))
    clipped, _truncated = truncate_utf8(redacted, limit)
    return clipped


def acceptance_command_count(acceptance: Sequence[object]) -> int:
    """How many acceptance items are ``cmd:`` commands.

    The same prefix ``acceptance_command_body`` uses. An empty body is not
    a command. Anything else is prose for a reviewer.
    """
    count = 0
    for item in acceptance:
        if not isinstance(item, str) or not item.startswith("cmd:"):
            continue
        if item[4:].strip():
            count += 1
    return count


def _declared_scopes(line: Line) -> tuple[str, ...]:
    """Non-blank write_scope entries, redacted and flattened.

    A star-only scope such as ``**`` stays in the count. It has no area
    bucket, because there is no directory left once the glob is stripped.
    """
    declared: list[str] = []
    for scope in line.write_scope:
        if not str(scope or "").strip():
            continue
        declared.append(_one_line(scope, _SCOPE_ENTRY_LIMIT))
    return tuple(declared)


def _line_input(line: Line, depth: int) -> LineInput:
    task_text, truncated = bound_task_text(line.task)
    declared = _declared_scopes(line)
    scopes = tuple(scope for scope in declared if scope)
    directories = []
    for scope in scopes:
        parts = _scope_directories(scope)
        if not parts:
            continue
        label = _redact_label("/".join(parts))
        if label:
            directories.append(label)
    depends = []
    for dep in line.depends_on:
        cleaned = _one_line(dep, 128)
        if cleaned:
            depends.append(cleaned)
    return LineInput(
        line_id=_one_line(line.id, 128) or "line",
        task_text=task_text,
        task_truncated=truncated,
        areas=area_buckets(scopes),
        write_scope=scopes,
        acceptance_count=acceptance_command_count(line.acceptance),
        depends_on=tuple(depends),
        difficulty=DifficultyFeatures(
            scope_entries=len(declared),
            directories=tuple(sorted(set(directories))),
            risk_bearing=bool(line.risk_bearing),
            dag_depth=depth,
            planner_large=str(line.estimated_size or "").strip().lower() == "large",
        ),
    )


def _row_areas(row: Mapping[str, object]) -> tuple[str, ...]:
    """Areas a stored outcome row already names, else buckets of its scope.

    An explicit ``area`` or ``areas`` is the recorder's label and wins.
    Otherwise the row's ``write_scope`` is bucketed the same way as a line.
    A row with neither is not an area sample.
    """
    explicit = row.get("area")
    if isinstance(explicit, str) and explicit.strip():
        bucket = area_bucket(explicit)
        return (bucket,) if bucket else ()
    named = row.get("areas")
    if isinstance(named, (list, tuple)):
        buckets = {area_bucket(str(item)) for item in named if str(item).strip()}
        buckets.discard("")
        if buckets:
            return tuple(sorted(buckets))
    scope = row.get("write_scope")
    if isinstance(scope, str):
        scope = (scope,)
    if isinstance(scope, (list, tuple)):
        return area_buckets([str(item) for item in scope])
    return ()


def _outcome_samples(
    rows: Sequence[object],
) -> list[tuple[str, bool, tuple[str, ...]]]:
    """``(model, passed, areas)`` for rows that are real line outcomes.

    The same filter as ``MemoryP0OutcomeStore.success_rate``: a row counts
    when it has a model and a ``passed`` key. The rollup stored under
    ``_models`` has neither at the top level, so it is not a second sample.
    """
    samples: list[tuple[str, bool, tuple[str, ...]]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        model = _redact_label(str(row.get("model") or ""))
        if not model or "passed" not in row:
            continue
        samples.append((model, bool(row.get("passed")), _row_areas(row)))
    return samples


def provider_rates(rows: Sequence[object]) -> tuple[ProviderRate, ...]:
    """Per-model pass counts over one search result."""
    runs: dict[str, list[int]] = {}
    for model, passed, _areas in _outcome_samples(rows):
        bucket = runs.setdefault(model, [0, 0])
        bucket[0] += 1
        bucket[1] += 1 if passed else 0
    return tuple(
        ProviderRate(model=model, runs=counts[0], passed=counts[1])
        for model, counts in sorted(runs.items())
    )


def area_rates(rows: Sequence[object]) -> tuple[AreaRate, ...]:
    """Per-model, per-area pass counts over one search result.

    A line that touched two areas counts once in each. It still counts
    once in the provider total.
    """
    runs: dict[tuple[str, str], list[int]] = {}
    for model, passed, areas in _outcome_samples(rows):
        for area in areas:
            bucket = runs.setdefault((model, area), [0, 0])
            bucket[0] += 1
            bucket[1] += 1 if passed else 0
    return tuple(
        AreaRate(model=model, area=area, runs=counts[0], passed=counts[1])
        for (model, area), counts in sorted(runs.items())
    )


async def _search_rows(port: object) -> list[object]:
    """One read of the outcome collection. ``put`` is never called.

    A search that fails is raised. An empty history and a broken read are
    different facts, and this function does not turn the second into the
    first.
    """
    search = getattr(port, "search", None)
    if search is None:
        raise TypeError("outcome port has no search")
    rows = await search(COLLECTION, limit=_OUTCOME_SEARCH_LIMIT)
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise TypeError("outcome search did not return a list")
    return list(rows[:_OUTCOME_SEARCH_LIMIT])


async def success_rate_by_area(port: object) -> tuple[AreaRate, ...]:
    """Read-only provider × area rates over ``dispatches.outcomes`` rows.

    ``port`` is a ``MemoryPort``. Only ``search`` is called, on the outcome
    collection, capped at the same 500 rows the outcome store reads.
    """
    return area_rates(await _search_rows(port))


async def assemble_inputs(lines: Sequence[Line], port: object) -> AssignmentInputs:
    """Assemble one call's inputs from lines and a single outcome search."""
    rows = await _search_rows(port)
    depths = dependency_depths(lines)
    return AssignmentInputs(
        lines=tuple(_line_input(line, depths.get(line.id, 0)) for line in lines),
        success_by_provider=provider_rates(rows),
        success_rate_by_area=area_rates(rows),
    )
