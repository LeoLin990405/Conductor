"""The palette of the TUI: its commands, its matcher, its arguments and its frames.

The palette is the one surface the TUI shares with the web palette of
``web/src/features/palette``, so this file checks the TUI copy against the
same token set the web copy is checked against:
``docs/design/terminal-native/system/tokens.json`` holds the 28 commands, the
idle order, the fuzzy examples and the completion limit, and
``docs/design/terminal-native/tui`` holds the frames the palette draws.

The frames are the contract of the layout: the query line, the columns of a
command row, the columns of an argument row and the footer, at 40, 80 and 120
columns in English and zh-CN.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from rich.cells import cell_len

_SRC = Path(__file__).resolve().parents[4] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ptc_cli.superagent.tui_completion import (  # noqa: E402
    SLASH_COMMANDS,
    AtFileCompleter,
    SlashCompleter,
    TuiCompleter,
    expand_mentions,
    slash_help_lines,
)
from ptc_cli.superagent.tui_i18n import parity_problems  # noqa: E402
from ptc_cli.superagent.tui_palette import (  # noqa: E402
    COMMANDS,
    COMPLETION_LIMIT,
    IDLE_ORDER,
    STRINGS,
    ArgOption,
    PaletteState,
    argument_rows,
    canonical_command,
    command_rows,
    complete_arguments,
    fuzzy_match,
    phase_state,
    prefix_match,
    rank_commands,
    render_palette,
    truncate,
    unique_prefix_insert,
    wrap,
)
from tests.unit_tests.tui_layout_contract import check_layout  # noqa: E402

WIDTHS = (40, 80, 120)
LOCALES = ("en", "zh-CN")
# The marks of the frames: the query line, the marker of the selected row and
# the rails the composer draws.
QUERY_LABELS = ("palette ›", "命令面板 ›")  # noqa: RUF001
COMPOSER_RAILS = ("╭", "│", "╰", "❯")  # noqa: RUF001

# The roster of the D2 cases, the same one the web palette is checked with.
ROSTER = (
    ArgOption(
        "7f3a91c2", "awaiting_human", "Ship the terminal-native review queue screen"
    ),
    ArgOption("4b8e0d17", "secondary_review", "Write the operator digest"),
    ArgOption("91cc6a40", "g2", "Land the merge rehearsal"),
    ArgOption("7c77be05", "fixing", "Fix the narrow layout"),
)
# Two dispatches that wait for a person and share a prefix.
WAITING = (
    ArgOption(
        "7f3a91c2", "awaiting_human", "Ship the terminal-native review queue screen"
    ),
    ArgOption("7f9e02b1", "awaiting_human", "Fix the narrow layout"),
)


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "docs/design/terminal-native/system/tokens.json").is_file():
            return parent
    pytest.fail("the design tree is not above this test file")


def _tokens() -> dict:
    path = _repo_root() / "docs/design/terminal-native/system/tokens.json"
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _frames(name: str) -> list[str]:
    """The palette region of a reference frame: the query line to the composer."""
    path = _repo_root() / "docs/design/terminal-native/tui" / name
    rows: list[str] = []
    inside = False
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith(QUERY_LABELS):
            inside = True
            rows.append(line)
            continue
        if inside:
            if stripped.startswith(COMPOSER_RAILS):
                break
            rows.append(line)
    return rows


def _completions(completer, text: str) -> list[str]:
    return [
        item.text
        for item in completer.get_completions(Document(text, len(text)), CompleteEvent())
    ]


# ---------------------------------------------------------------------------
# The command table (tokens.json palette.commands)
# ---------------------------------------------------------------------------


def test_command_table_is_the_token_set() -> None:
    """The TUI table is tokens.json palette.commands, command for command."""
    palette = _tokens()["palette"]
    assert len(COMMANDS) == 28
    assert len(palette["commands"]) == 28
    for command, expected in zip(COMMANDS, palette["commands"], strict=True):
        assert command.name == expected["name"]
        assert command.slash == expected["slash"]
        assert list(command.aliases) == expected["aliases"]
        assert command.hint == expected["hint"]
        assert command.primary == expected["primary"]
        # A command cannot run bare when no form of it runs without a step.
        forms = expected["forms"]
        assert command.requires_arg == all(form["steps"] for form in forms)


def test_idle_order_is_the_token_set() -> None:
    """The empty query answers the idle order of the token set, unchanged."""
    idle = _tokens()["palette"]["idleOrder"]
    assert list(IDLE_ORDER) == idle
    assert len(IDLE_ORDER) == 28
    assert [row.command.name for row in rank_commands("")] == idle
    # The table keeps its idle order however it is handed over.
    assert [row.command.name for row in rank_commands("", COMMANDS)] == idle


def test_approve_and_reject_have_no_slash_name() -> None:
    """The two decisions are named, not slashed: there is no ``/approve``."""
    for name in ("approve", "reject"):
        command = next(row for row in COMMANDS if row.name == name)
        assert command.slash is None
        assert command.requires_arg
    # The canonical name is still a haystack, so the palette finds the
    # command; what it has is no slash name of its own.
    assert canonical_command("approve") == "approve"
    assert canonical_command("/approve") == "approve"
    assert _completions(SlashCompleter(), "/apv") == ["approve"]


def test_the_aliases_of_tickets() -> None:
    """``/jobs``, ``/j`` and ``jobs`` are one command."""
    for token in ("/jobs", "/j", "jobs", "/attach", "tickets"):
        assert canonical_command(token) == "tickets"
    tickets = next(row for row in COMMANDS if row.name == "tickets")
    assert tickets.slash == "/jobs"
    assert set(tickets.aliases) == {"/j", "jobs", "/attach"}


def test_the_shipped_slash_table_agrees_with_the_palette() -> None:
    """The shipped ``/help`` table and the palette name the same commands."""
    for command in COMMANDS:
        if command.slash is None:
            continue
        assert command.slash in SLASH_COMMANDS
        assert SLASH_COMMANDS[command.slash] == command.hint


# ---------------------------------------------------------------------------
# D1: the matcher
# ---------------------------------------------------------------------------


def test_the_fuzzy_examples_of_the_token_set() -> None:
    """Every example of palette.fuzzy.examples answers what it promises."""
    examples = _tokens()["palette"]["fuzzy"]["examples"]
    assert examples
    for example in examples:
        rows = rank_commands(example["query"])
        if example["match"] is None:
            assert rows == ()
            continue
        assert rows[0].command.name == example["match"]
        if "indexes" in example:
            score = fuzzy_match(example["query"], example["haystack"])
            assert score is not None
            assert list(score.indexes) == example["indexes"]
            assert score.gap == example["gapCost"]


def test_the_d1_list() -> None:
    """The list of the design: dsp, DSP, jobs, Disp, tkt, apv, zzz."""
    assert rank_commands("dsp")[0].command.name == "dispatch"
    assert rank_commands("DSP")[0].command.name == "dispatch"
    assert rank_commands("jobs")[0].command.name == "tickets"
    assert rank_commands("jobs")[0].on_name is False
    assert rank_commands("Disp")[0].command.name == "dispatch"
    assert rank_commands("tkt")[0].command.name == "tickets"
    assert rank_commands("apv")[0].command.name == "approve"
    assert rank_commands("zzz") == ()
    # The gap cost of dsp in dispatch is 1: d, s and p with one gap.
    score = fuzzy_match("dsp", "dispatch")
    assert score is not None
    assert score.gap == 1
    assert list(score.indexes) == [0, 2, 3]


def test_a_leading_slash_is_stripped_once() -> None:
    """``/dsp`` and ``dsp`` find the same command."""
    assert rank_commands("/dsp")[0].command.name == "dispatch"
    assert rank_commands("//dsp") == ()


def test_spaces_stay_in_the_needle() -> None:
    """A space is not an empty query, and it is not trimmed off the needle.

    The needle is the query with one leading slash removed, the same rule as
    the web matcher. Trimming it would turn a space into the idle order and
    a trailing space into a match.
    """
    assert [row.command.name for row in rank_commands("/")] == list(IDLE_ORDER)
    assert rank_commands(" ") == ()
    assert rank_commands(" dsp") == ()
    assert rank_commands("/dsp ") == ()


def test_the_match_is_never_more_than_sixty_rows() -> None:
    """The list is capped by the completion limit of the token set."""
    palette = _tokens()["palette"]
    assert COMPLETION_LIMIT == palette["completionLimit"] == 60
    assert len(rank_commands("")) == 28
    assert len(rank_commands("a")) <= COMPLETION_LIMIT
    assert len(rank_commands("")) <= COMPLETION_LIMIT


def test_slash_completer_answers_with_the_matcher() -> None:
    """``/DSP`` completes ``/dispatch``: the composer shares the matcher."""
    completer = SlashCompleter()
    assert _completions(completer, "/DSP")[0] == "/dispatch"
    assert _completions(completer, "/jo") == ["/jobs"]
    assert _completions(completer, "/jobs ") == []
    # The case-insensitive match reaches the aliases too.
    assert "/jobs" in _completions(completer, "/Jo")


def test_slash_completer_localizes_the_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """The menu hint is the locale's hint, read through the string table.

    A zh-CN session must not keep the English wording of the command row.
    A caller that brought its own table keeps that wording: the table is
    not a shipped command, so it has no translation of its own.
    """
    monkeypatch.setenv("CONDUCTOR_LOCALE", "zh-CN")
    completion = next(
        SlashCompleter().get_completions(Document("/DSP"), CompleteEvent())
    )
    assert "派发名册" in completion.display_meta_text
    own = next(
        SlashCompleter({"/extra": "bring your own hint"}).get_completions(
            Document("/extra"), CompleteEvent()
        )
    )
    assert own.display_meta_text == "bring your own hint"
    monkeypatch.setenv("CONDUCTOR_LOCALE", "en")
    english = next(
        SlashCompleter().get_completions(Document("/jo"), CompleteEvent())
    )
    assert "list Tickets" in english.display_meta_text


# ---------------------------------------------------------------------------
# D2: the argument completion
# ---------------------------------------------------------------------------


def _provider(command: str) -> tuple[ArgOption, ...]:
    del command
    return ROSTER


def _waiting_provider(command: str) -> tuple[ArgOption, ...]:
    """The provider of two dispatches that wait for a person."""
    del command
    return WAITING


def test_dispatch_lists_every_active_id() -> None:
    """``dispatch`` offers the ids of every phase."""
    result = complete_arguments("dispatch", "", provider=_provider)
    assert [option.id for option in result.options] == [
        "7f3a91c2",
        "4b8e0d17",
        "91cc6a40",
        "7c77be05",
    ]
    assert result.needs_id is False


def test_approve_and_reject_list_only_the_ones_that_wait() -> None:
    """``approve`` and ``reject`` offer only the ids in phase awaiting_human."""
    for command in ("approve", "reject"):
        result = complete_arguments(command, "", provider=_provider)
        assert [option.id for option in result.options] == ["7f3a91c2"]
        assert result.needs_id is False


def test_a_unique_prefix_inserts_and_an_ambiguous_one_does_not() -> None:
    """The prefix match is case-sensitive, and never fuzzy."""
    options = complete_arguments("dispatch", "", provider=_provider).options
    assert unique_prefix_insert("91", options) == "91cc6a40"
    assert unique_prefix_insert("7", options) is None
    assert [option.id for option in prefix_match("7", options)] == [
        "7f3a91c2",
        "7c77be05",
    ]
    # A hex id is matched case-sensitively, so the other case names nothing:
    # a rule that folded the case would answer here and pass the rest.
    assert prefix_match("7F", options) == []
    assert unique_prefix_insert("7F", options) is None
    assert [option.id for option in prefix_match("7f", options)] == ["7f3a91c2"]
    assert prefix_match("DSP-9", options) == []
    assert unique_prefix_insert("DSP-9", options) is None


def test_the_completer_matches_an_id_case_sensitively() -> None:
    """The composer matches a hex id the way the palette does."""
    completer = TuiCompleter(id_provider=_provider)
    assert _completions(completer, "/dispatch 7f") == ["7f3a91c2"]
    assert _completions(completer, "/dispatch 7F") == []


def test_an_ambiguous_prefix_inserts_nothing() -> None:
    """A prefix that names two ids lists the matches and inserts neither."""
    completer = TuiCompleter(id_provider=_waiting_provider)
    line = "/approve 7f"
    buffer = Buffer(document=Document(line, len(line)))
    completions = list(
        completer.get_completions(buffer.document, CompleteEvent())
    )
    # The menu shows the matches...
    assert [item.display_text for item in completions] == ["7f3a91c2", "7f9e02b1"]
    # ...and taking one of them leaves the line as it was.
    buffer.apply_completion(completions[0])
    assert buffer.text == line


def test_an_ambiguous_prefix_asks_for_more_of_the_id() -> None:
    """The footer of an ambiguous prefix does not promise an insertion."""
    state = PaletteState(
        query="approve 7f",
        rows=argument_rows(WAITING),
        selected=0,
        kind="arguments",
        command="approve",
        count=2,
    )
    en = render_palette(state, 80, "en")
    zh = render_palette(state, 80, "zh-CN")
    assert "2 ids match" in en[-1]
    assert "type more of the id" in en[-1]
    assert "插入 id" not in en[-1]
    assert "2 个 id 匹配" in zh[-1]
    assert "请输入更多 id" in zh[-1]


def test_a_unique_prefix_still_promises_the_insertion() -> None:
    """A prefix that names one id inserts it, and the footer says so."""
    state = PaletteState(
        query="approve 7f3",
        rows=argument_rows(WAITING[:1]),
        selected=0,
        kind="arguments",
        command="approve",
        count=1,
    )
    en = render_palette(state, 80, "en")
    assert "inserts the id" in en[-1]
    completer = TuiCompleter(id_provider=_waiting_provider)
    assert _completions(completer, "/approve 7f3") == ["7f3a91c2"]


def test_reply_offers_nothing() -> None:
    """The argument of ``reply`` is free text, so the provider is not asked."""

    def provider(command: str) -> tuple[ArgOption, ...]:
        raise AssertionError(command)

    result = complete_arguments("reply", "", provider=provider)
    assert result.options == ()
    assert result.free_text is True
    # An empty reply is a reply that wants its text.
    assert result.needs_id is True
    assert complete_arguments("reply", "yes", provider=provider).needs_id is False


def test_the_provider_is_asked_by_canonical_name() -> None:
    """``jobs``, ``/j`` and ``/DISPATCH`` resolve before the provider is asked.

    The command token is an exact folded haystack match, and the provider
    hears the canonical name. A roster keyed by the typed spelling would
    miss the ids, so this test returns them only for that name.
    """
    seen: list[str] = []

    def provider(command: str) -> tuple[ArgOption, ...]:
        seen.append(command)
        if command == "tickets":
            return (ArgOption("7f782add", "running", "Read the adapter registry"),)
        if command == "dispatch":
            return ROSTER
        return ()

    assert [row.id for row in complete_arguments("jobs", provider=provider).options] == [
        "7f782add"
    ]
    assert [row.id for row in complete_arguments("/j", provider=provider).options] == [
        "7f782add"
    ]
    assert [
        row.id for row in complete_arguments("/DISPATCH", "91", provider=provider).options
    ] == ["91cc6a40"]
    assert seen == ["tickets", "tickets", "dispatch"]


def test_an_incomplete_form_needs_an_id() -> None:
    """``approve`` without an id is incomplete, and says so."""
    result = complete_arguments("approve", "", provider=lambda command: ())
    assert result.needs_id is True
    assert result.options == ()


def test_the_completer_routes_the_argument_text() -> None:
    """The composer answers the text after a command and a space with ids.

    ``approve ``, ``/dispatch `` and ``jobs `` all resolve, with or without
    the slash, and the provider hears the canonical name of each.
    """
    seen: list[str] = []

    def provider(command: str) -> tuple[ArgOption, ...]:
        seen.append(command)
        if command == "tickets":
            return (ArgOption("7f782add", "running", "Read the adapter registry"),)
        return ROSTER

    completer = TuiCompleter(id_provider=provider)
    assert _completions(completer, "/dispatch ") == [
        "7f3a91c2",
        "4b8e0d17",
        "91cc6a40",
        "7c77be05",
    ]
    assert _completions(completer, "approve ") == ["7f3a91c2"]
    assert _completions(completer, "jobs ") == ["7f782add"]
    assert _completions(completer, "/dispatch 91") == ["91cc6a40"]
    assert _completions(completer, "/reply ") == []
    # A line that names no command is not an argument request.
    assert _completions(completer, "hello world ") == []
    assert seen == ["dispatch", "approve", "tickets", "dispatch"]


# ---------------------------------------------------------------------------
# The names the shipped TUI reads
# ---------------------------------------------------------------------------


def test_the_public_names_keep_their_names() -> None:
    """The names the TUI and its tests read are still there."""
    assert set(SLASH_COMMANDS)
    assert SlashCompleter is not None
    assert TuiCompleter is not None
    assert AtFileCompleter is not None
    assert callable(expand_mentions)
    lines = slash_help_lines()
    assert lines
    assert all(len(line) == 2 for line in lines)
    assert lines[0][0] in SLASH_COMMANDS


# ---------------------------------------------------------------------------
# The renderer
# ---------------------------------------------------------------------------


def _command_state(query: str, locale: str, count: int) -> PaletteState:
    ranked = rank_commands(query)
    return PaletteState(
        query=query,
        rows=command_rows(ranked, locale),
        selected=0,
        kind="commands",
        count=count if count >= 0 else len(ranked),
    )


def _argument_state(
    options: tuple[ArgOption, ...], command: str
) -> PaletteState:
    return PaletteState(
        query=f"{command} ",
        rows=argument_rows(options),
        selected=0,
        kind="arguments",
        command=command,
        count=len(options),
    )


def test_the_strings_table_is_bilingual() -> None:
    """Every string the palette draws exists in both languages."""
    assert parity_problems(STRINGS) == []


def test_a_phase_is_drawn_as_the_state_it_is() -> None:
    """A dispatch is drawn as the state of its phase, not as a guess."""
    assert phase_state("awaiting_human") == "needs-you"
    assert phase_state("approval") == "needs-you"
    assert phase_state("implementing") == "running"
    assert phase_state("secondary_review") == "running"
    assert phase_state("fixing") == "running"
    assert phase_state("blocked") == "blocked"
    assert phase_state("merged") == "completed"
    # A phase the palette does not know is a dispatch that has not started.
    assert phase_state("something_new") == "queued"


def _typed_query(rows: list[str], locale: str) -> str:
    """The query the frame drew, without the label, the rail or the caret."""
    label = STRINGS[locale]["label"]
    prefix = f"{label} › "  # noqa: RUF001
    head = " " * 2 + prefix
    rail = " " * (2 + cell_len(prefix))
    chunks: list[str] = []
    for index, row in enumerate(rows):
        body = row.removeprefix(head if index == 0 else rail)
        if body.endswith("▏"):
            chunks.append(body[:-1])
            break
        chunks.append(body)
    return "".join(chunks)


def test_a_space_in_the_query_still_draws() -> None:
    """A query of only spaces is a row. The caret still has a line to sit on."""
    assert wrap(" ", 10) == [" "]
    assert "".join(wrap(" " * 5, 2)) == " " * 5
    for query in (" ", " " * 80):
        state = PaletteState(query=query, kind="commands")
        for locale in LOCALES:
            check_layout(
                _renderer(state),
                widths=WIDTHS,
                locales=(locale,),
                keep=("▏",),
            )
            for width in WIDTHS:
                rows = render_palette(state, width, locale)
                assert _typed_query(rows, locale) == query


def test_mixed_whitespace_in_the_query_still_draws() -> None:
    """A leading space stays in the row. It is why the matcher missed.

    `` /dsp`` is not ``/dsp``. The space is part of the needle, so the
    matcher answers nothing; drawing the row without that space would hide
    the character responsible. A word after the spaces, a tab in the word,
    and spaces on both ends are the same rule.
    """
    query = " /dsp"
    assert rank_commands(query) == ()
    row = render_palette(PaletteState(query=query), 80, "en")[0]
    assert row == f"  palette › {query}▏"  # noqa: RUF001
    for mixed in (query, "  /dsp", " /dsp ", " \t/dsp"):
        assert rank_commands(mixed) == ()
        assert "".join(wrap(mixed, 1)) == mixed
        assert "".join(wrap(mixed, 3)) == mixed
        state = PaletteState(query=mixed, kind="commands")
        for locale in LOCALES:
            check_layout(
                _renderer(state),
                widths=WIDTHS,
                locales=(locale,),
                keep=("▏",),
            )
            for width in WIDTHS:
                rows = render_palette(state, width, locale)
                assert _typed_query(rows, locale) == mixed


def test_the_caret_rides_at_the_end_of_the_query() -> None:
    """The caret stands where the cursor is: at the end of the query."""
    state = PaletteState(query="dsp", rows=(), selected=0, kind="commands", count=0)
    rows = render_palette(state, 80, "en")
    assert rows[0] == "  palette › dsp▏"  # noqa: RUF001
    # A query that wraps puts the caret at the end of the last line.
    long_query = "dispatch " + "x" * 90
    rows = render_palette(PaletteState(query=long_query, kind="commands"), 40, "en")
    query_rows = rows[:-1]
    assert len(query_rows) > 1
    assert not query_rows[0].endswith("▏")
    assert query_rows[-1].endswith("▏")


def _renderer(state: PaletteState):
    """The renderer of a state, in the shape the layout contract calls."""
    return lambda width, locale: render_palette(state, width, locale)


def test_a_query_that_fills_the_line_leaves_room_for_the_caret() -> None:
    """The caret has a cell of its own, so the query line still fits."""
    state = PaletteState(query="x" * 28, kind="commands")
    check_layout(
        _renderer(state),
        widths=(40,),
        locales=LOCALES,
        keep=("▏",),
    )
    rows = render_palette(state, 40, "en")
    # The query wraps, and the caret rides at the end of the last line.
    query_rows = rows[:-1]
    assert len(query_rows) > 1
    assert query_rows[-1].endswith("▏")
    assert all(len(row) <= 40 for row in rows)


def test_cjk_truncation_stays_within_the_cell_budget() -> None:
    """A wide character stays out when it cannot sit beside the ellipsis.

    ``truncate("中文", 2)`` used to return ``中…``, three cells. The subject
    column beside a 19-character id is two cells wide at a 40-column
    terminal, so that cut made the argument row 41 cells in both languages.
    """
    assert cell_len(truncate("中文", 2)) <= 2
    assert truncate("中文", 2) == "…"
    assert truncate("中文", 3) == "中…"
    assert cell_len(truncate("中文", 3)) <= 3
    option = ArgOption("7f3a91c200000000000", "awaiting_human", "中文")
    state = PaletteState(
        rows=argument_rows((option,)),
        kind="arguments",
        command="approve",
    )
    check_layout(_renderer(state))
    for locale in LOCALES:
        rows = render_palette(state, 40, locale)
        assert all(cell_len(row) <= 40 for row in rows)
        assert option.id in "\n".join(rows)


def test_an_id_longer_than_the_column_is_never_cut() -> None:
    """A production-length id takes the room it needs; the state stays."""
    option = ArgOption(
        "7f3a91c2" + "0" * 24, "awaiting_human", "Ship the review queue screen"
    )
    state = PaletteState(
        query="approve ",
        rows=argument_rows((option,)),
        selected=0,
        kind="arguments",
        command="approve",
        count=1,
    )
    check_layout(
        _renderer(state),
        widths=WIDTHS,
        locales=LOCALES,
        keep=("▏", "▸"),
        states=("needs-you",),
        columns=(option.id,),
    )
    rows = render_palette(state, 40, "en")
    # The id is whole, on a row of its own, and the state follows under it.
    assert option.id in rows[1]
    assert any("◆ Needs you" in row for row in rows)


def test_the_window_follows_the_selection() -> None:
    """A selected row below the window scrolls the list into view."""
    ranked = rank_commands("")
    state = PaletteState(
        rows=command_rows(ranked, "en"),
        selected=8,
        kind="commands",
        count=len(ranked),
    )
    rows = render_palette(state, 80, "en")
    marked = [row for row in rows if "▸" in row]
    assert len(marked) == 1
    # The ninth command of the idle order is the one marked.
    assert IDLE_ORDER[8] in marked[0]
    # The window scrolled: it shows the eight commands that end with it.
    names = [row[4:16].strip() for row in rows[1:9]]
    assert names == list(IDLE_ORDER[1:9])


def test_the_layout_holds_at_every_width_and_language() -> None:
    """The palette holds the layout contract in both languages."""
    for locale in LOCALES:
        idle = _command_state("", locale, 28)
        check_layout(
            _renderer(idle),
            widths=WIDTHS,
            locales=(locale,),
            keep=("▏",),
        )
        found = _command_state("/dsp", locale, 1)
        check_layout(
            _renderer(found),
            widths=WIDTHS,
            locales=(locale,),
            keep=("▏", "▸"),
        )
        waiting = _argument_state(ROSTER[:2], "approve")
        check_layout(
            _renderer(waiting),
            widths=WIDTHS,
            locales=(locale,),
            keep=("▏", "▸"),
            states=("needs-you",),
            columns=("7f3a91c2",),
        )
        incomplete = _argument_state((), "approve")
        check_layout(
            _renderer(incomplete),
            widths=WIDTHS,
            locales=(locale,),
            keep=("▏",),
        )
        empty = _command_state("zzz", locale, 0)
        check_layout(
            _renderer(empty),
            widths=WIDTHS,
            locales=(locale,),
            keep=("▏",),
        )


def test_the_empty_query_shows_the_idle_order() -> None:
    """The idle palette shows the idle order, the window of it that fits."""
    for locale in LOCALES:
        rows = render_palette(_command_state("", locale, 28), 80, locale)
        # The name column starts after the indent and the marker.
        names = [row[4:16].strip() for row in rows[1:9]]
        assert names == list(IDLE_ORDER[:8])
        # The window holds at most 8 rows, and the footer counts them all.
        assert len(rows) == 1 + 8 + 1
        assert "28" in rows[-1]


def test_no_match_says_so_in_both_languages() -> None:
    """``zzz`` answers with the empty state, in the language of the screen."""
    en = render_palette(_command_state("zzz", "en", 0), 80, "en")
    zh = render_palette(_command_state("zzz", "zh-CN", 0), 80, "zh-CN")
    assert "no command matches" in en[-1]
    assert "没有匹配的命令" in zh[-1]


def test_an_incomplete_form_shows_the_hint() -> None:
    """``approve`` without an id shows the hint, in the language of the screen."""
    en = render_palette(_argument_state((), "approve"), 80, "en")
    zh = render_palette(_argument_state((), "approve"), 80, "zh-CN")
    assert "approve needs a dispatch id" in en[-1]
    assert "approve 需要一个 dispatch id" in zh[-1]
    reject_en = render_palette(_argument_state((), "reject"), 80, "en")
    assert "reject needs a dispatch id" in reject_en[-1]


def test_the_frames_are_the_frames() -> None:
    """The palette draws the frames of the design, at every width."""
    for locale, tag in (("en", "en"), ("zh-CN", "zh-CN")):
        for width in WIDTHS:
            state = _command_state("/dsp", locale, 1)
            assert render_palette(state, width, locale) == _frames(
                f"tui-completion-{tag}-w{width}.txt"
            )
    # The argument frames carry the subjects of the dispatches, which are the
    # data of the screen and not a translation of the palette.
    subjects = {
        "en": (
            ArgOption("7f3a91c2", "awaiting_human", "Ship the terminal-native review queue screen"),
            ArgOption("7f9e02b1", "awaiting_human", "Fix the narrow layout"),
        ),
        "zh-CN": (
            ArgOption("7f3a91c2", "awaiting_human", "把终端原生的评审队列屏幕上线"),
            ArgOption("7f9e02b1", "awaiting_human", "修复窄屏布局"),
        ),
    }
    for locale, tag in (("en", "en"), ("zh-CN", "zh-CN")):
        for width in WIDTHS:
            state = _argument_state(subjects[locale], "approve")
            assert render_palette(state, width, locale) == _frames(
                f"tui-completion-args-{tag}-w{width}.txt"
            )
