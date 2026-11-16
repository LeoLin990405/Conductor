"""The palette of the Super-agent TUI: its commands, its matcher, its arguments.

The palette is the one surface the TUI shares with the web palette of
``web/src/features/palette``: the same 28 commands, the same matcher and the
same argument rules, so a command is found the same way wherever the operator
types it. The TUI copy of the table is written out instead of read from
``docs/design/terminal-native/system/tokens.json`` at import time, because the
TUI ships as an installed package and the design tree does not travel with it;
``test_tui_palette.py`` loads the JSON and fails when this copy drifts from it.

Three parts:

* the command table (:data:`COMMANDS` and :data:`IDLE_ORDER`) — the 28
  commands of ``palette.commands``, their slash names, their aliases and
  whether the bare form of the command can run;
* the matcher (D1) — a case-insensitive subsequence match over the canonical
  name, the slash name and the aliases, ranked exact > prefix > gap cost >
  start > name;
* the argument completion (D2) — a unique, case-sensitive prefix of an id,
  never fuzzy, with the ids of a command coming from an injectable provider.

:func:`render_palette` draws the region — the query line, the rows and the
footer — as a pure function of a :class:`PaletteState`, at any width and in
both locales.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

from rich.cells import cell_len

from .tui_i18n import DEFAULT_LOCALE, normalize_locale, t
from .tui_theme import status_glyph, status_word

# The row cap of the whole list (tokens.json palette.completionLimit) and the
# number of rows the palette shows at once (SPEC 6.2).
COMPLETION_LIMIT = 60
WINDOW_ROWS = 8

# The columns of a command row (SPEC 6.2): the marker, the canonical name,
# the slash name and the hint. The name is what the command is, so it is
# never cut; the slash name drops at a narrow terminal and the hint is cut.
INDENT = 2
MARKER_WIDTH = 2
NAME_WIDTH = 12
SLASH_WIDTH = 10
STATE_WIDTH = 9
COLUMN_GAP = 2
NARROW_WIDTH = 40

# The dispatch phases the palette knows, drawn as the Ticket state they are.
# The vocabulary is the one of the dispatcher (src/conductor_cli/
# dispatch_timeline.py): the positions of the axis and the stages in them.
# A phase the palette does not know is a dispatch that has not started.
PHASE_STATES = {
    # The human gate: a dispatch that waits for a person.
    "awaiting_human": "needs-you",
    "approval": "needs-you",
    "g6": "needs-you",
    # A dispatch in flight: planning, building, testing, reviewing, merging.
    "planning": "running",
    "spec_review": "running",
    "dag_ready": "running",
    "assigned": "running",
    "implementing": "running",
    "focused_tests": "running",
    "g2": "running",
    "primary_review": "running",
    "secondary_review": "running",
    "integration_checks": "running",
    "g5": "running",
    "fixing": "running",
    # A dispatch that stopped.
    "blocked": "blocked",
    "replanning": "blocked",
    "cancelled": "archived",
    "merged": "completed",
}
DEFAULT_PHASE_STATE = "queued"

# The phase a dispatch is in when a person has to decide it.
AWAITING_HUMAN = "awaiting_human"
# The commands whose argument is a dispatch id, and the phase that id must be
# in: ``dispatch`` reads a dispatch of any phase, ``approve`` and ``reject``
# only one that waits for a person. A command the table does not name has no
# rule of its own, and the provider answers for it.
ARGUMENT_RULES: Mapping[str, str] = {
    "dispatch": "",
    "approve": AWAITING_HUMAN,
    "reject": AWAITING_HUMAN,
}
# The commands that cannot run without an id: Enter never runs them bare.
INCOMPLETE_COMMANDS = ("approve", "reject")

KIND_COMMANDS = "commands"
KIND_ARGUMENTS = "arguments"

ELLIPSIS = "…"
CARET = "▏"
MARKER = "▸"
NO_SLASH = "—"


@dataclass(frozen=True, slots=True)
class PaletteCommand:
    """One command of the palette.

    ``name`` is the canonical name the palette shows; ``slash`` is the name
    the composer knows it by, and ``None`` for the commands that have no
    slash name of their own (``approve`` and ``reject``). ``aliases`` are the
    other spellings, with or without a leading slash. ``requires_arg`` says
    that the bare form of the command cannot run, so the palette inserts a
    trailing space after it.
    """

    name: str
    slash: str | None
    aliases: tuple[str, ...]
    hint: str
    primary: bool = True
    requires_arg: bool = False


# The 28 commands of tokens.json palette.commands, in the order of that file.
COMMANDS: tuple[PaletteCommand, ...] = (
    PaletteCommand(
        "workers",
        "/worker",
        ("worker",),
        "show which worker has the current Ticket",
    ),
    PaletteCommand(
        "dispatch",
        "/dispatch",
        (),
        "dispatch roster; /dispatch <id> reads one",
    ),
    PaletteCommand(
        "approve",
        None,
        (),
        "approve a dispatch that is waiting for a person",
        requires_arg=True,
    ),
    PaletteCommand(
        "reject",
        None,
        (),
        "reject a dispatch that is waiting for a person",
        requires_arg=True,
    ),
    PaletteCommand(
        "tickets",
        "/jobs",
        ("/j", "jobs", "/attach"),
        "list Tickets; /jobs <id> shows one; /jobs watch <id> follows it",
    ),
    PaletteCommand(
        "retry",
        "/retry",
        (),
        "retry a failed or waiting Ticket",
    ),
    PaletteCommand(
        "review",
        "/review",
        (),
        "queue a local cross-review (/review run <line> --by <line>)",
        requires_arg=True,
    ),
    PaletteCommand(
        "memory",
        "/memory",
        (),
        "browse remembered memory",
    ),
    PaletteCommand(
        "workspace",
        "/workspace",
        (),
        "switch the Home workspace",
        primary=False,
    ),
    PaletteCommand(
        "reply",
        "/reply",
        (),
        "answer the Ticket that needs you",
        primary=False,
        requires_arg=True,
    ),
    PaletteCommand(
        "cancel",
        "/cancel",
        (),
        "stop a running Ticket (/cancel <id|index>)",
        primary=False,
    ),
    PaletteCommand(
        "resume",
        "/resume",
        (),
        "list saved sessions; /resume <id> reopens one",
        primary=False,
    ),
    PaletteCommand(
        "status",
        "/status",
        (),
        "reread Supervisor progress for the current Ticket",
        primary=False,
    ),
    PaletteCommand(
        "help",
        "/help",
        ("?",),
        "show the TUI key map and commands",
        primary=False,
    ),
    PaletteCommand(
        "quit",
        "/quit",
        ("/exit", "/q", "exit"),
        "quit the TUI",
        primary=False,
    ),
    PaletteCommand(
        "skills",
        "/skills",
        (),
        "local skills status",
        primary=False,
    ),
    PaletteCommand(
        "mcp",
        "/mcp",
        (),
        "local MCP server status",
        primary=False,
    ),
    PaletteCommand(
        "agents",
        "/agents",
        (),
        "local fleet, native runtimes, daemons",
        primary=False,
    ),
    PaletteCommand(
        "merge",
        "/merge",
        (),
        "merge a line locally after its tests (/merge <line> --into <branch>)",
        primary=False,
        requires_arg=True,
    ),
    PaletteCommand(
        "stop",
        "/stop",
        (),
        "stop a running Ticket (/stop <id|index>)",
        primary=False,
        requires_arg=True,
    ),
    PaletteCommand(
        "signals",
        "/signals",
        (),
        "Sources and job counts",
        primary=False,
    ),
    PaletteCommand(
        "clear",
        "/clear",
        (),
        "clear the conversation",
        primary=False,
    ),
    PaletteCommand(
        "details",
        "/details",
        (),
        "toggle Worker details (default: Supervisor view)",
        primary=False,
    ),
    PaletteCommand(
        "model",
        "/model",
        (),
        "show the Router model and configured providers",
        primary=False,
    ),
    PaletteCommand(
        "compact",
        "/compact",
        (),
        "drop activity noise and duplicate rows from the transcript",
        primary=False,
    ),
    PaletteCommand(
        "expand",
        "/expand",
        (),
        "expand every tool block",
        primary=False,
    ),
    PaletteCommand(
        "collapse",
        "/collapse",
        (),
        "collapse every tool block",
        primary=False,
    ),
    PaletteCommand(
        "new",
        "/new",
        (),
        "start a fresh session (persisted)",
        primary=False,
    ),
)

# The order the palette shows when the query is empty (tokens.json
# palette.idleOrder). It is the order the operator reads, not the order of
# the table above.
IDLE_ORDER: tuple[str, ...] = (
    "workers",
    "dispatch",
    "approve",
    "reject",
    "tickets",
    "workspace",
    "reply",
    "retry",
    "cancel",
    "resume",
    "status",
    "help",
    "quit",
    "skills",
    "mcp",
    "memory",
    "agents",
    "review",
    "merge",
    "stop",
    "signals",
    "clear",
    "details",
    "model",
    "compact",
    "expand",
    "collapse",
    "new",
)

COMMAND_BY_NAME: Mapping[str, PaletteCommand] = {row.name: row for row in COMMANDS}


def command_by_name(name: str) -> PaletteCommand | None:
    """The command of the table with that canonical name, or ``None``."""
    return COMMAND_BY_NAME.get(name)


# ---------------------------------------------------------------------------
# D1: the matcher
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FuzzyScore:
    """How well a needle matched a haystack, and how it was aligned.

    ``exact`` and ``prefix`` are 0 when the haystack is equal to the needle
    or starts with it; ``gap`` is the cost of the greedy alignment and
    ``start`` the index the alignment began at. The rank reads them in that
    order.
    """

    indexes: tuple[int, ...]
    exact: int
    prefix: int
    gap: int
    start: int


@dataclass(frozen=True, slots=True)
class RankedCommand:
    """A command that matched, with the haystack that matched it best."""

    command: PaletteCommand
    score: FuzzyScore | None
    key: str
    on_name: bool


def _fold(text: str) -> str:
    """The Unicode casefold of a string: the fold the token set names."""
    return text.casefold()


def _strip_slash(text: str) -> str:
    """A text without one leading slash."""
    return text.removeprefix("/")


def haystacks(command: PaletteCommand) -> tuple[str, ...]:
    """The texts a command is found by: its name, its slash name, its aliases."""
    stacks = [command.name]
    if command.slash:
        stacks.append(_strip_slash(command.slash))
    stacks.extend(_strip_slash(alias) for alias in command.aliases)
    return tuple(stacks)


def fuzzy_match(needle: str, haystack: str) -> FuzzyScore | None:
    """Match a needle against a haystack as a subsequence, or return ``None``.

    The needle and the haystack are folded, so ``DSP`` matches ``dispatch``.
    The alignment is greedy from the left: every character of the needle
    takes the leftmost later character of the haystack that still fits.
    """
    folded_needle = _fold(needle)
    folded_haystack = _fold(haystack)
    if not folded_needle:
        return FuzzyScore((), 0 if not folded_haystack else 1, 0, 0, 0)
    indexes: list[int] = []
    cursor = 0
    for index, character in enumerate(folded_haystack):
        if character == folded_needle[cursor]:
            indexes.append(index)
            cursor += 1
            if cursor == len(folded_needle):
                break
    if cursor < len(folded_needle):
        return None
    gap = indexes[-1] - indexes[0] - (len(folded_needle) - 1)
    return FuzzyScore(
        tuple(indexes),
        0 if folded_haystack == folded_needle else 1,
        0 if folded_haystack.startswith(folded_needle) else 1,
        gap,
        indexes[0],
    )


def _score_key(score: FuzzyScore) -> tuple[int, int, int, int]:
    """The rank of a score: exact, prefix, gap cost, start."""
    return (score.exact, score.prefix, score.gap, score.start)


def best_match(
    query: str, command: PaletteCommand
) -> tuple[FuzzyScore, str] | None:
    """The best haystack of a command for a query, with its score."""
    best: FuzzyScore | None = None
    key = ""
    for haystack in haystacks(command):
        score = fuzzy_match(query, haystack)
        if score is None:
            continue
        if best is None or _score_key(score) < _score_key(best):
            best = score
            key = haystack
    if best is None:
        return None
    return best, key


def idle_commands(
    commands: Sequence[PaletteCommand] | None = None,
) -> tuple[PaletteCommand, ...]:
    """The commands of the idle order, or the table's own order.

    The palette table is shown in the idle order of the token set however it
    is passed; a table of another set is shown in its own order, because it
    has no idle order of its own.
    """
    rows = COMMANDS if commands is None else tuple(commands)
    if rows == COMMANDS:
        by_name = COMMAND_BY_NAME
        return tuple(by_name[name] for name in IDLE_ORDER if name in by_name)
    return rows


def rank_commands(
    query: str,
    commands: Sequence[PaletteCommand] | None = None,
    limit: int = COMPLETION_LIMIT,
) -> tuple[RankedCommand, ...]:
    """Rank the commands for a query.

    The needle is the query with one leading slash removed, then folded.
    An empty needle answers the idle order unchanged. A space stays in the
    needle: it is not an empty query, and it is not trimmed off a match.
    Otherwise every command whose name, slash name or an alias holds the
    needle as a subsequence is ranked by the score and then by its canonical
    name, and at most ``limit`` of them are returned.
    """
    rows = tuple(commands) if commands is not None else COMMANDS
    needle = _fold(_strip_slash(query))
    if not needle:
        return tuple(
            RankedCommand(command=command, score=None, key=command.name, on_name=True)
            for command in idle_commands(commands)[:limit]
        )
    matched: list[RankedCommand] = []
    for command in rows:
        hit = best_match(needle, command)
        if hit is None:
            continue
        score, key = hit
        matched.append(
            RankedCommand(
                command=command,
                score=score,
                key=key,
                on_name=key == command.name,
            )
        )
    matched.sort(key=lambda row: (*_score_key(row.score), row.command.name))  # type: ignore[arg-type]
    return tuple(matched[:limit])


def canonical_command(token: str) -> str | None:
    """The canonical name of a typed token, or ``None`` when it names nothing.

    The token is matched exactly, with one leading slash removed and the case
    folded, against the canonical names, the slash names and the aliases.
    """
    folded = _fold(_strip_slash(token.strip()))
    if not folded:
        return None
    for command in COMMANDS:
        if _fold(command.name) == folded:
            return command.name
        if command.slash and _fold(_strip_slash(command.slash)) == folded:
            return command.name
        for alias in command.aliases:
            if _fold(_strip_slash(alias)) == folded:
                return command.name
    return None


# ---------------------------------------------------------------------------
# D2: the argument completion
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ArgOption:
    """One argument the palette can offer: an id, its phase and its subject."""

    id: str
    phase: str = ""
    subject: str = ""


@dataclass(frozen=True, slots=True)
class ArgumentResult:
    """What the argument completion answered for one command.

    ``options`` are the ids the command can take, already filtered by the
    phase the command allows and by the prefix that was typed. ``needs_id``
    says that the command still wants an argument and none is on offer.
    ``free_text`` says that the argument is free text and nothing is offered.
    """

    options: tuple[ArgOption, ...] = ()
    needs_id: bool = False
    free_text: bool = False
    command: str = ""


#: The provider of the ids of a command: it is called with the canonical
#: command name and answers the ids that command can take. A provider that
#: does not know a command answers nothing for it, so the palette never
#: offers the ids of another command. The TUI wires the real rosters in
#: later; until then the palette offers nothing.
IdProvider = Callable[[str], Iterable[ArgOption]]


def no_ids(command: str) -> tuple[ArgOption, ...]:
    """The provider of a TUI that has no roster yet: nothing to offer."""
    del command
    return ()


def prefix_match(
    partial: str, options: Sequence[ArgOption]
) -> list[ArgOption]:
    """The options whose id starts with the partial, case-sensitively.

    An empty partial keeps every option. The match is a prefix and never a
    fuzzy one: a hex id is matched the way ``_matched_target`` matches it.
    """
    if not partial:
        return list(options)
    return [option for option in options if option.id.startswith(partial)]


def unique_prefix_insert(
    partial: str, options: Sequence[ArgOption]
) -> str | None:
    """The id a unique prefix names, or ``None``.

    An ambiguous prefix names more than one id, so it inserts nothing.
    """
    hits = prefix_match(partial, options)
    if len(hits) == 1:
        return hits[0].id
    return None


def complete_arguments(
    command: str,
    partial: str = "",
    *,
    provider: IdProvider | None = None,
    limit: int = COMPLETION_LIMIT,
) -> ArgumentResult:
    """The arguments a command can take after its token and a space.

    The provider answers the roster of that command: ``dispatch`` offers the
    ids of every phase, ``approve`` and ``reject`` only the ids that wait for
    a person, and ``reply`` nothing at all, because its argument is free
    text. A command the table has no rule for is answered by the provider as
    it is, which is how a command with an id of its own — a Ticket, a line —
    takes one.
    """
    canonical = canonical_command(command)
    if canonical is None:
        return ArgumentResult()
    if canonical == "reply":
        return ArgumentResult(
            free_text=True, needs_id=not partial, command=canonical
        )
    rows = list(provider(canonical) if provider is not None else no_ids(canonical))
    phase = ARGUMENT_RULES.get(canonical, "")
    if phase:
        rows = [row for row in rows if row.phase == phase]
    options = tuple(prefix_match(partial, rows)[:limit])
    needs_id = canonical in INCOMPLETE_COMMANDS and not options
    return ArgumentResult(options=options, needs_id=needs_id, command=canonical)


# ---------------------------------------------------------------------------
# The renderer
# ---------------------------------------------------------------------------

# Every string the palette draws, in the two languages it speaks. The table
# goes through tui_i18n, so a missing translation is a bug in this module and
# not something an operator sees half-translated.
STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "label": "palette",
        "noMatch": "no command matches",
        "needsId": "{command} needs a dispatch id",
        "replyNeedsText": "reply needs text",
        "noDispatch": "no active dispatch",
        "matchCount": "{count} match",
        "matchesCount": "{count} matches",
        "matchKeys": "tab accepts · enter runs",
        "argCount": "{count} waiting for a person",
        "dispatchCount": "{count} active dispatches",
        "argKeys": "tab or enter inserts the id",
        "ambiguousCount": "{count} ids match",
        "ambiguousKeys": "type more of the id",
        "hint.workers": "show which worker has the current Ticket",
        "hint.dispatch": "dispatch roster; /dispatch <id> reads one",
        "hint.approve": "approve a dispatch that is waiting for a person",
        "hint.reject": "reject a dispatch that is waiting for a person",
        "hint.tickets": "list Tickets; /jobs <id> shows one; /jobs watch <id> follows it",
        "hint.retry": "retry a failed or waiting Ticket",
        "hint.review": "queue a local cross-review (/review run <line> --by <line>)",
        "hint.memory": "browse remembered memory",
        "hint.workspace": "switch the Home workspace",
        "hint.reply": "answer the Ticket that needs you",
        "hint.cancel": "stop a running Ticket (/cancel <id|index>)",
        "hint.resume": "list saved sessions; /resume <id> reopens one",
        "hint.status": "reread Supervisor progress for the current Ticket",
        "hint.help": "show the TUI key map and commands",
        "hint.quit": "quit the TUI",
        "hint.skills": "local skills status",
        "hint.mcp": "local MCP server status",
        "hint.agents": "local fleet, native runtimes, daemons",
        "hint.merge": "merge a line locally after its tests (/merge <line> --into <branch>)",
        "hint.stop": "stop a running Ticket (/stop <id|index>)",
        "hint.signals": "Sources and job counts",
        "hint.clear": "clear the conversation",
        "hint.details": "toggle Worker details (default: Supervisor view)",
        "hint.model": "show the Router model and configured providers",
        "hint.compact": "drop activity noise and duplicate rows from the transcript",
        "hint.expand": "expand every tool block",
        "hint.collapse": "collapse every tool block",
        "hint.new": "start a fresh session (persisted)",
    },
    "zh-CN": {
        "label": "命令面板",
        "noMatch": "没有匹配的命令",
        "needsId": "{command} 需要一个 dispatch id",
        "replyNeedsText": "reply 需要回复内容",
        "noDispatch": "没有活跃的派发",
        "matchCount": "{count} 个匹配",
        "matchesCount": "{count} 个匹配",
        "matchKeys": "tab 接受 · enter 运行",
        "argCount": "{count} 个等待人工",
        "dispatchCount": "{count} 个活跃派发",
        "argKeys": "tab 或 enter 插入 id",
        "ambiguousCount": "{count} 个 id 匹配",
        "ambiguousKeys": "请输入更多 id",
        "hint.workers": "显示当前 Ticket 的 Worker",
        "hint.dispatch": "派发名册；/dispatch <id> 查看一个",  # noqa: RUF001
        "hint.approve": "批准等待人工的派发",
        "hint.reject": "驳回等待人工的派发",
        "hint.tickets": "列出 Ticket；/jobs <id> 查看一个；/jobs watch <id> 持续跟踪",  # noqa: RUF001
        "hint.retry": "重试失败或等待中的 Ticket",
        "hint.review": "排入本地交叉评审（/review run <line> --by <line>）",  # noqa: RUF001
        "hint.memory": "浏览记忆",
        "hint.workspace": "切换 Home 工作区",
        "hint.reply": "回复需要你处理的 Ticket",
        "hint.cancel": "停止运行中的 Ticket（/cancel <id|index>）",  # noqa: RUF001
        "hint.resume": "列出已保存的会话；/resume <id> 重新打开",  # noqa: RUF001
        "hint.status": "重读当前 Ticket 的 Supervisor 进度",
        "hint.help": "显示 TUI 按键与命令",
        "hint.quit": "退出 TUI",
        "hint.skills": "本地技能状态",
        "hint.mcp": "本地 MCP 服务器状态",
        "hint.agents": "本地舰队、原生运行时、守护进程",
        "hint.merge": "测试通过后在本地合并某条线（/merge <line> --into <branch>）",  # noqa: RUF001
        "hint.stop": "停止运行中的 Ticket（/stop <id|index>）",  # noqa: RUF001
        "hint.signals": "来源与任务计数",
        "hint.clear": "清空对话",
        "hint.details": "切换 Worker 详情（默认：Supervisor 视图）",  # noqa: RUF001
        "hint.model": "显示 Router 模型与已配置的 provider",
        "hint.compact": "从记录中丢弃活动噪声与重复行",
        "hint.expand": "展开所有工具块",
        "hint.collapse": "折叠所有工具块",
        "hint.new": "开启新会话（持久化）",  # noqa: RUF001
    },
}


@dataclass(frozen=True, slots=True)
class PaletteRow:
    """One row of the palette: a command, or an argument of a command."""

    name: str
    slash: str = ""
    hint: str = ""
    state: str = ""
    subject: str = ""


@dataclass(frozen=True, slots=True)
class PaletteState:
    """What the palette shows: the query, the rows, the selection, the footer.

    ``kind`` says whether the rows are commands or arguments, and ``command``
    names the command the arguments belong to. ``count`` is the number of
    matches the footer counts, which is more than the rows the palette shows
    when the list is long; ``-1`` counts the rows on screen. ``note`` is a
    footer message that replaces the count — the hint of an incomplete form,
    or the note of an ambiguous prefix.
    """

    query: str = ""
    rows: tuple[PaletteRow, ...] = ()
    selected: int = 0
    kind: str = KIND_COMMANDS
    command: str = ""
    count: int = -1
    note: str = ""


def phase_state(phase: str) -> str:
    """The Ticket state a dispatch phase is drawn as."""
    return PHASE_STATES.get(phase, DEFAULT_PHASE_STATE)


def command_rows(
    ranked: Sequence[RankedCommand], locale: str = "en"
) -> tuple[PaletteRow, ...]:
    """The rows of a list of ranked commands, in the locale's language."""
    resolved = normalize_locale(locale)
    rows: list[PaletteRow] = []
    for row in ranked:
        command = row.command
        rows.append(
            PaletteRow(
                name=command.name,
                slash=command.slash or "",
                hint=command_hint(command, resolved),
            )
        )
    return tuple(rows)


def argument_rows(options: Sequence[ArgOption]) -> tuple[PaletteRow, ...]:
    """The rows of a list of arguments.

    The subject of an argument is the dispatch's own text, not a translation,
    so nothing here is localized.
    """
    return tuple(
        PaletteRow(
            name=option.id,
            state=phase_state(option.phase),
            subject=option.subject,
        )
        for option in options
    )


def command_hint(command: PaletteCommand, locale: str | None = None) -> str:
    """The hint of a command in the locale's language.

    The shipped commands are read through :func:`t`, so the palette and the
    completion menu say the same thing in whichever language
    ``CONDUCTOR_LOCALE`` (or an explicit locale) names. A command the table
    does not carry, or a caller that brought its own wording, keeps the hint
    on the row. A missing translation is raised by :func:`t` instead of
    quietly answered in English.
    """
    key = f"hint.{command.name}"
    english = STRINGS[DEFAULT_LOCALE].get(key)
    if english is None or command.hint != english:
        return command.hint
    return t(STRINGS, key, locale)


def _pad(text: str, width: int) -> str:
    """A text padded to a width in cells, never cut."""
    missing = width - cell_len(text)
    return text + " " * missing if missing > 0 else text


def _cut_cells(text: str, room: int) -> tuple[str, str]:
    """Cut a text into the longest prefix of ``room`` cells and the rest.

    A wide character is never split in half: when it does not fit in the room
    on its own it is still drawn, because a dropped character is worse than a
    wide one.
    """
    if room <= 0:
        return "", text
    used = 0
    cut = 0
    for index, character in enumerate(text):
        width = cell_len(character)
        if used + width > room:
            break
        used += width
        cut = index + 1
    if not cut and text:
        cut = 1
    return text[:cut], text[cut:]


def truncate(text: str, room: int) -> str:
    """Cut a text to ``room`` cells, on a word boundary, with an ellipsis.

    A text that fits is returned as it is. A text that does not is cut where
    a word ends, so the reader sees whole words, and the ellipsis says that
    something was left out. The result never occupies more cells than
    ``room``. A wide character that cannot sit in the cells left beside the
    ellipsis is left out, and the ellipsis stands on its own: drawing the
    character anyway would push the row past the terminal.
    """
    if room <= 0:
        return ""
    if cell_len(text) <= room:
        return text
    mark = cell_len(ELLIPSIS)
    budget = room - mark
    head = ""
    if budget > 0:
        for word in text.split(" "):
            candidate = f"{head} {word}" if head else word
            if cell_len(candidate) > budget:
                break
            head = candidate
    if head and cell_len(head) <= budget:
        return f"{head}{ELLIPSIS}"
    cut, _ = _cut_cells(text, budget)
    # ``_cut_cells`` keeps a wide character that does not fit on its own.
    # Beside the ellipsis that character would exceed the room, so it stays
    # out and the ellipsis is the whole cut.
    if cut and cell_len(cut) <= budget:
        return f"{cut}{ELLIPSIS}"
    if mark <= room:
        return ELLIPSIS
    return ""


def wrap(text: str, room: int) -> list[str]:
    """Wrap a prose text into lines of at most ``room`` cells.

    English breaks on a word boundary. A word that is longer than the line
    fills it and is cut across as many lines as it needs; no character of
    that word is dropped. The space where a line breaks is not drawn on the
    next line. A run of spaces at the start of the text is part of the text:
    a query's leading space is why the matcher missed, so those spaces stay,
    wrapped, even when a word follows them. A query of only spaces splits
    into empty words and would otherwise come back as no lines at all: the
    spaces stay, wrapped, so the query row and its caret still have
    somewhere to stand.
    """
    if not text:
        return [""]
    if room <= 0:
        return [text]
    # Spaces are the whole query. ``split`` would throw them away and leave
    # the caller with nothing to draw.
    if text.strip(" ") == "":
        return [text[index : index + room] for index in range(0, len(text), room)]
    lines: list[str] = []
    current = ""
    # Leading spaces are the text, not a break between words. Peel them off
    # before ``split``, which would otherwise treat an empty first word as
    # nothing and drop the space that made the query miss.
    leading_len = len(text) - len(text.lstrip(" "))
    if leading_len:
        leading = text[:leading_len]
        if cell_len(leading) <= room:
            current = leading
        else:
            chunks = [
                leading[index : index + room] for index in range(0, len(leading), room)
            ]
            lines.extend(chunks[:-1])
            current = chunks[-1]
        text = text[leading_len:]
    first_word = True
    for word in text.split(" "):
        # The leading run already holds the space in front of the first word,
        # so that word is joined without a second space.
        lead = first_word and current.strip(" ") == ""
        if lead:
            candidate = f"{current}{word}"
        elif current:
            candidate = f"{current} {word}"
        else:
            candidate = word
        first_word = False
        if cell_len(candidate) <= room:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ""
        if not word:
            # The space after the previous word did not fit on that line.
            # It is a space the operator typed, so it starts the next one.
            current = " "
            continue
        rest = word
        while cell_len(rest) > room:
            head, rest = _cut_cells(rest, room)
            lines.append(head)
        current = rest
    if current:
        lines.append(current)
    if not lines:
        return [text[index : index + room] for index in range(0, len(text), room)]
    return lines


def _query_rows(query: str, width: int, locale: str) -> list[str]:
    """The query line: the label, the query and the caret.

    The query wraps into the room the label leaves, and the caret rides at the
    end of the last line, because that is where the cursor stands. The caret
    has a cell of its own: the room the query wraps into leaves it, so a query
    that fills the line still fits the terminal.
    """
    label = t(STRINGS, "label", locale)
    prefix = f"{label} › "  # noqa: RUF001
    room = max(1, width - INDENT - cell_len(prefix) - cell_len(CARET))
    lines = wrap(query, room)
    rows = [" " * INDENT + prefix + lines[0]]
    rows.extend(" " * (INDENT + cell_len(prefix)) + line for line in lines[1:])
    rows[-1] += CARET
    return rows


def _command_row(row: PaletteRow, width: int, *, selected: bool) -> str:
    """One command row: the marker, the name, the slash name and the hint."""
    marker = MARKER if selected else " "
    name = _pad(row.name, NAME_WIDTH)
    if width > NARROW_WIDTH:
        slash = _pad(row.slash or NO_SLASH, SLASH_WIDTH)
        head = f"{' ' * INDENT}{marker} {name}{' ' * COLUMN_GAP}{slash}{' ' * COLUMN_GAP}"
        room = width - INDENT - MARKER_WIDTH - NAME_WIDTH - COLUMN_GAP - SLASH_WIDTH - COLUMN_GAP
    else:
        head = f"{' ' * INDENT}{marker} {name}{' ' * COLUMN_GAP}"
        room = width - INDENT - MARKER_WIDTH - NAME_WIDTH - COLUMN_GAP
    return f"{head}{truncate(row.hint, room)}"


def _state_cell(row: PaletteRow, locale: str) -> str:
    """The state of an argument row: its glyph and its word, in a column."""
    if not row.state:
        return ""
    glyph = status_glyph(row.state)
    word = status_word(row.state, locale)
    return _pad(f"{glyph} {word}", 1 + 1 + STATE_WIDTH)


def _argument_row(
    row: PaletteRow, width: int, locale: str, *, selected: bool
) -> list[str]:
    """One argument row: the marker, the id, the state and the subject.

    The id is never cut: it is what the operator copies. An id longer than
    the column takes the room it needs and the state moves under it, so the
    row still fits the terminal and nothing is lost.
    """
    marker = MARKER if selected else " "
    state = _state_cell(row, locale)
    id_width = max(NAME_WIDTH, cell_len(row.name))
    prefix = f"{' ' * INDENT}{marker} "
    rail = " " * (INDENT + MARKER_WIDTH)
    columns = [_pad(row.name, id_width)]
    if state:
        columns.append(state)
    head = prefix + f"{' ' * COLUMN_GAP}".join(columns)
    room = width - cell_len(head) - COLUMN_GAP
    if room >= 0:
        return [f"{head}{' ' * COLUMN_GAP}{truncate(row.subject, room)}"]
    # The id does not fit beside the state: it takes the line to itself and
    # the state follows under it, where the id stood.
    lines = wrap(row.name, max(1, width - INDENT - MARKER_WIDTH))
    rows = [f"{prefix}{lines[0]}"]
    rows.extend(f"{rail}{line}" for line in lines[1:])
    tail = f"{rail}{state}" if state else rail
    room = width - cell_len(tail) - COLUMN_GAP
    rows.append(f"{tail}{' ' * COLUMN_GAP}{truncate(row.subject, room)}")
    return rows


def _empty_argument_footer(state: PaletteState, locale: str) -> str:
    """The footer of an argument list that offers nothing.

    A command that cannot run without an id says which id it needs; a command
    with no roster says that there is nothing to choose.
    """
    if state.command in INCOMPLETE_COMMANDS:
        return t(STRINGS, "needsId", locale).format(command=state.command)
    if state.command == "reply":
        return t(STRINGS, "replyNeedsText", locale)
    return t(STRINGS, "noDispatch", locale)


def _typed_argument(state: PaletteState) -> str:
    """The argument the operator has typed so far."""
    return state.query.rpartition(" ")[2].strip()


def _ambiguous_matches(state: PaletteState) -> int:
    """How many of the offered ids the typed prefix names.

    A prefix that names two ids is ambiguous: the palette lists the matches
    and inserts nothing, because the operator has to type more of the id
    before one of them is the answer.
    """
    partial = _typed_argument(state)
    if not partial:
        return 0
    return sum(1 for row in state.rows if row.name.startswith(partial))


def _footer_text(state: PaletteState, locale: str) -> str:
    """The text of the footer: the count and the keys, or the error of a form."""
    if state.note:
        return state.note
    count = state.count if state.count >= 0 else len(state.rows)
    if state.kind == KIND_ARGUMENTS:
        if not state.rows:
            return _empty_argument_footer(state, locale)
        matches = _ambiguous_matches(state)
        if matches > 1:
            headline = t(STRINGS, "ambiguousCount", locale).format(count=matches)
            return f"{headline} · {t(STRINGS, 'ambiguousKeys', locale)}"
        key = "dispatchCount" if state.command == "dispatch" else "argCount"
        headline = t(STRINGS, key, locale).format(count=count)
        return f"{headline} · {t(STRINGS, 'argKeys', locale)}"
    if not state.rows:
        return t(STRINGS, "noMatch", locale)
    key = "matchCount" if count == 1 else "matchesCount"
    headline = t(STRINGS, key, locale).format(count=count)
    return f"{headline} · {t(STRINGS, 'matchKeys', locale)}"


def _visible_rows(state: PaletteState) -> tuple[int, tuple[PaletteRow, ...]]:
    """The window of rows the palette shows, and where it starts.

    The window follows the selection: a selected row that is below the
    window scrolls the list, so the marker is never on a row the operator
    cannot see.
    """
    start = 0
    if state.selected >= WINDOW_ROWS:
        start = state.selected - WINDOW_ROWS + 1
    return start, tuple(state.rows[start : start + WINDOW_ROWS])


def render_palette(
    state: PaletteState, width: int, locale: str = "en"
) -> list[str]:
    """Draw the palette region: the query line, the rows and the footer.

    The region is a pure function of its state, so the same state draws the
    same frame at every width and in both languages. A row is never wider
    than the terminal: the hint and the subject are cut, and the footer
    wraps.

    The footer of an ambiguous prefix is the hold ink of SPEC 6.4: it says
    how many ids match and asks for more of the id, because a prefix that
    names two of them inserts neither.
    """
    resolved = normalize_locale(locale)
    rows = _query_rows(state.query, width, resolved)
    start, visible = _visible_rows(state)
    for offset, row in enumerate(visible):
        selected = start + offset == state.selected
        if state.kind == KIND_ARGUMENTS:
            rows.extend(_argument_row(row, width, resolved, selected=selected))
        else:
            rows.append(_command_row(row, width, selected=selected))
    rows.extend(
        f"{' ' * INDENT}{line}"
        for line in wrap(_footer_text(state, resolved), max(1, width - INDENT))
    )
    return rows


__all__ = [
    "ARGUMENT_RULES",
    "COMMANDS",
    "COMMAND_BY_NAME",
    "COMPLETION_LIMIT",
    "IDLE_ORDER",
    "INCOMPLETE_COMMANDS",
    "KIND_ARGUMENTS",
    "KIND_COMMANDS",
    "STRINGS",
    "WINDOW_ROWS",
    "ArgOption",
    "ArgumentResult",
    "FuzzyScore",
    "IdProvider",
    "PaletteCommand",
    "PaletteRow",
    "PaletteState",
    "RankedCommand",
    "argument_rows",
    "best_match",
    "canonical_command",
    "command_by_name",
    "command_hint",
    "command_rows",
    "complete_arguments",
    "fuzzy_match",
    "haystacks",
    "idle_commands",
    "no_ids",
    "phase_state",
    "prefix_match",
    "rank_commands",
    "render_palette",
    "truncate",
    "unique_prefix_insert",
    "wrap",
]
