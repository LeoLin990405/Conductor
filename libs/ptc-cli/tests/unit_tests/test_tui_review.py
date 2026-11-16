"""The ``/review`` transcript rows: the layout contract, the states, the fixtures.

``tui_review`` is the pure renderer of ``docs/design/terminal-native/tui/SPEC.md``
§7.4. The tests below call its three renderers at the three contract widths in
both languages, compare the queue with the review rows of the design fixtures,
and pin the rules a narrow row must not break: the stack at 40 columns, the
glyph and the word of every verdict and decision (§4.2), the ASCII twins under
``PTC_ASCII=1``, the spinner frame :mod:`tui_theme` hands out, and the ladder of
§3.2 that lets a long subject cost the convenience columns their room but never
the state of the row.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from rich.cells import cell_len

_SRC = Path(__file__).resolve().parents[4] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ptc_cli.superagent.tui_i18n import check_parity  # noqa: E402
from ptc_cli.superagent.tui_review import (  # noqa: E402
    STRINGS,
    ReviewGate,
    progress_rows,
    result_rows,
    usage_rows,
)
from ptc_cli.superagent.tui_theme import (  # noqa: E402
    resolve_theme,
    spinner_frames,
    status_word,
)
from tests.unit_tests.tui_layout_contract import check_layout  # noqa: E402

# A test that only reads the design tree still has to find it from libs/ptc-cli.
_ROOT = Path(__file__).resolve().parents[4]
_FIXTURES = _ROOT / "docs/design/terminal-native/tui"
_WIDTHS = (40, 80, 120)
_LOCALES = ("en", "zh-CN")

# The subjects of the design mock's REVIEW_SUBJECTS, per language.
SUBJECTS = {
    "en": (
        "Add the review queue screen",
        "Fix the narrow layout",
        "Audit the token contrast",
    ),
    "zh-CN": ("添加评审队列屏幕", "修复窄屏布局", "审查令牌的对比度"),
}
# The design mock's REVIEWS: (line, reviewer, gate, fix, verdict, decision).
REVIEWS = (
    ("L3", "L1", "reviewed", "1", "MERGE", "approve"),
    ("L4", "L2", "build", "0", "BLOCK", "reject"),
    ("L5", "L1", "reviewed", "0", "—", "pending"),
)
# The G6 block of the design mock: the gate, the pinned commit, the command.
GATE = ReviewGate(line="L3", sha="4b2f7803", dispatch="7f3a91c2")

# §4.2: a verdict is a glyph and a word. ``none`` is a dash and nothing else,
# and its ASCII twin is the hyphen the muted ink falls back to.
VERDICTS = (
    ("merge", "✔", "+", {"en": "MERGE", "zh-CN": "合并"}),
    ("block", "✖", "x", {"en": "BLOCK", "zh-CN": "阻断"}),
    ("none", "—", "-", {"en": "—", "zh-CN": "—"}),
)
DECISIONS = (
    ("approve", "✔", "+", {"en": "approve", "zh-CN": "批准"}),
    ("reject", "✖", "x", {"en": "reject", "zh-CN": "驳回"}),
    ("pending", "◆", "?", {"en": "pending", "zh-CN": "待定"}),
)
# §4.2: the gate of a line is a word, and its dash is the same dash.
GATES = {
    "en": {"reviewed": "reviewed", "build": "build", "—": "—"},
    "zh-CN": {"reviewed": "已评审", "build": "构建中", "—": "—"},
}


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """No inherited locale, ASCII switch or motion flag changes the frames."""
    for name in (
        "CONDUCTOR_LOCALE",
        "LC_ALL",
        "LC_MESSAGES",
        "LANG",
        "PTC_ASCII",
        "PTC_I18N_PARITY",
        "PTC_REDUCED_MOTION",
        "CONDUCTOR_THEME",
        "NO_COLOR",
        "COLORFGBG",
    ):
        monkeypatch.delenv(name, raising=False)
    # pytest often inherits TERM=dumb, which freezes the spinner to one frame.
    monkeypatch.setenv("TERM", "xterm-256color")


def _theme():
    return resolve_theme({"CONDUCTOR_THEME": "dark", "LANG": "en_US.UTF-8"})


def _queue(locale: str) -> list[dict[str, object]]:
    """The design queue of one language: three reviews, three verdicts."""
    subjects = SUBJECTS[locale]
    rows = []
    for index, (line, reviewer, gate, fix, verdict, decision) in enumerate(REVIEWS):
        rows.append(
            {
                "line": line,
                "subject": subjects[index],
                "reviewer": reviewer,
                "gate": gate,
                "fix": fix,
                "verdict": verdict,
                "decision": decision,
            }
        )
    return rows


def _review(verdict: str = "merge", decision: str = "approve", **extra: object) -> dict[str, object]:
    row: dict[str, object] = {
        "line": "L3",
        "subject": "Add the review queue screen",
        "reviewer": "L1",
        "gate": "reviewed",
        "fix": "1",
        "verdict": verdict,
        "decision": decision,
    }
    row.update(extra)
    return row


def _fixture_body(locale: str, width: int) -> list[str]:
    """The review rows of one design fixture, between the chrome regions.

    The block starts at the queue title and ends at the command that resolves
    the gate; the identity bar, the command region, the composer and the status
    bar are chrome the renderer does not own.
    """
    lines = (_FIXTURES / f"tui-review-{locale}-w{width}.txt").read_text(encoding="utf-8").splitlines()
    title = STRINGS[locale]["title"]
    start = next(index for index, line in enumerate(lines) if line.strip() == title)
    end = next(index for index in range(start, len(lines)) if lines[index].strip() == "conductor dispatch approve 7f3a91c2")
    return lines[start : end + 1]


def test_the_strings_are_bilingual() -> None:
    check_parity(STRINGS)


@pytest.mark.parametrize("locale", _LOCALES)
@pytest.mark.parametrize("width", _WIDTHS)
def test_the_queue_matches_the_design_fixture(locale: str, width: int) -> None:
    """The fixture is the contract: same columns, same stack, same gate block."""
    assert result_rows(_queue(locale), GATE, locale, width) == _fixture_body(locale, width)


def test_the_english_queue_at_80_is_the_table_of_the_spec() -> None:
    """§7.4 written out: the title, the header, one row per review, the gate."""
    assert result_rows(_queue("en"), GATE, "en", 80) == [
        "  review queue",
        "  line  subject                      reviewer  gate      fix  verdict  decision",
        "  L3    Add the review queue screen  L1        reviewed  1    ✔ MERGE  ✔ approve",
        "  L4    Fix the narrow layout        L2        build     0    ✖ BLOCK  ✖ reject",
        "  L5    Audit the token contrast     L1        reviewed  0    —        ◆ pending",
        "",
        "  G6 human gate  approve L3",
        "  the reviewed commit is pinned at 4b2f7803",
        "  conductor dispatch approve 7f3a91c2",
    ]


@pytest.mark.parametrize("locale", _LOCALES)
def test_the_usage_error_is_the_usage_string(locale: str) -> None:
    rows = usage_rows(locale, 80)
    assert rows == [f"  ✖ {STRINGS[locale]['usage']}"]


@pytest.mark.parametrize("locale", _LOCALES)
def test_the_usage_error_keeps_the_detail_it_was_given(locale: str) -> None:
    rows = usage_rows(locale, 80, detail="a cross-review needs a reviewer line")
    assert rows[-1] == "  a cross-review needs a reviewer line"


@pytest.mark.parametrize("locale", _LOCALES)
def test_the_usage_error_passes_the_layout_contract(locale: str) -> None:
    """§3.4: the usage line is prose — it wraps and never loses a character."""
    detail = "a cross-review needs a reviewer line"
    check_layout(
        lambda width, language: usage_rows(language, width, detail=detail),
        prose=(STRINGS[locale]["usage"], detail),
        locales=(locale,),
    )


@pytest.mark.parametrize("locale", _LOCALES)
def test_the_usage_error_wraps_at_40_without_cutting_a_word(locale: str) -> None:
    rows = usage_rows(locale, 40, detail="a cross-review needs a reviewer line")
    assert all(cell_len(row) <= 40 for row in rows)
    squeezed = "".join("".join(rows).split())
    for needle in (STRINGS[locale]["usage"], "a cross-review needs a reviewer line"):
        assert "".join(needle.split()) in squeezed
    assert not any("…" in row for row in rows)


@pytest.mark.parametrize("locale", _LOCALES)
def test_a_queued_review_passes_the_layout_contract(locale: str) -> None:
    """The card keeps its subject, its state word and its reviewer at 40."""
    review = {
        "line": "L3",
        "subject": SUBJECTS[locale][0],
        "state": "queued",
        "reviewer": "L1",
        "provider": "codex",
        "head_sha": "4b2f7803",
    }
    parts = {
        "en": ("reviewer L1", "codex", "Queued", "head 4b2f7803"),
        "zh-CN": ("评审者 L1", "codex", "排队中", "提交 4b2f7803"),
    }[locale]
    check_layout(
        lambda width, language: progress_rows(review, language, width),
        keep=(SUBJECTS[locale][0], status_word("queued", locale)),
        prose=(parts[3],),
        columns=parts[:3],
        locales=(locale,),
    )


@pytest.mark.parametrize("locale", _LOCALES)
def test_a_running_review_passes_the_layout_contract(locale: str) -> None:
    review = {
        "line": "L4",
        "subject": SUBJECTS[locale][1],
        "state": "running",
        "reviewer": "L2",
        "provider": "deepseek",
        "head_sha": "9c1d0042",
        "note": "the reviewer CLI claimed the queue record",
    }
    check_layout(
        lambda width, language: progress_rows(review, language, width, frame=1),
        keep=(SUBJECTS[locale][1], status_word("running", locale)),
        prose=("the reviewer CLI claimed the queue record",),
        locales=(locale,),
    )


@pytest.mark.parametrize("locale", _LOCALES)
def test_the_queue_passes_the_layout_contract(locale: str) -> None:
    """§3.3: at 40 the row stacks every column instead of dropping one."""
    check_layout(
        lambda width, language: result_rows(_queue(language), GATE, language, width),
        keep=(STRINGS[locale]["title"],),
        prose=(
            STRINGS[locale]["gate"].format(line="L3"),
            STRINGS[locale]["pinned"].format(sha="4b2f7803"),
            STRINGS[locale]["command"].format(dispatch="7f3a91c2"),
        ),
        columns=(
            "✔ MERGE" if locale == "en" else "✔ 合并",
            "✖ BLOCK" if locale == "en" else "✖ 阻断",
            "◆ pending" if locale == "en" else "◆ 待定",
            "L1",
            "L2",
            "reviewed" if locale == "en" else "已评审",
            "build" if locale == "en" else "构建中",
        ),
        locales=(locale,),
    )


@pytest.mark.parametrize("locale", _LOCALES)
def test_every_verdict_and_decision_passes_the_layout_contract(locale: str) -> None:
    """§4.2 and §1.1: a result holding every state pair keeps all of them.

    The queue carries each verdict against each decision, so every glyph and
    word pair is on screen at 40 columns, where the row stacks them, and at 80
    and 120, where the row tables them.
    """
    reviews = [
        _review(verdict=verdict, decision=decision)
        for verdict, _glyph, _ascii, _words in VERDICTS
        for decision, _glyph2, _ascii2, _words2 in DECISIONS
    ]
    columns = [f"{glyph} {words[locale]}" if words[locale] != glyph else glyph for _ident, glyph, _ascii, words in VERDICTS]
    columns += [f"{glyph} {words[locale]}" for _ident, glyph, _ascii, words in DECISIONS]
    check_layout(
        lambda width, language: result_rows(reviews, GATE, language, width),
        keep=(STRINGS[locale]["title"], STRINGS[locale]["col.line"]),
        columns=columns,
        locales=(locale,),
    )


@pytest.mark.parametrize("locale", _LOCALES)
@pytest.mark.parametrize("width", _WIDTHS)
def test_every_verdict_fits_the_terminal(locale: str, width: int) -> None:
    rows = result_rows(_queue(locale), GATE, locale, width)
    assert rows
    assert all(cell_len(row) <= width for row in rows)


@pytest.mark.parametrize("locale", _LOCALES)
@pytest.mark.parametrize("width", [41, 60, 80, 100, 120])
def test_a_long_subject_never_pushes_a_column_out(locale: str, width: int) -> None:
    """§3.2 and §4.2: the convenience columns go, then the subject is cut.

    The verdict and the decision are the state of the row, so they outrank the
    subject on the ladder of §3.2: a subject that no longer fits is cut with an
    ellipsis before either state is dropped, and the reviewer, the gate and the
    fix count — the convenience columns — are the first things to go.
    """
    subject = "Ship the terminal-native review queue screen with every gate green"
    rows = result_rows([_review(subject=subject)], GATE, locale, width)
    assert all(cell_len(row) <= width for row in rows)
    line = rows[2]
    # The line column is pinned, so the row always says which line it is.
    assert line.startswith("  L3   ")
    # Both states of the row survive, at every width.
    assert ("✔ MERGE" if locale == "en" else "✔ 合并") in line
    assert ("✔ approve" if locale == "en" else "✔ 批准") in line
    if "…" in line:
        assert subject not in line
    else:
        assert subject in line


@pytest.mark.parametrize("width", [41, 48, 60, 80, 100, 120])
def test_a_long_subject_never_drops_the_verdict_or_the_decision(width: int) -> None:
    """§1.1 and §4.2: the state columns outrank the subject, so they stay.

    A subject longer than the whole row used to push the verdict and the
    decision off the screen, which left a queue that named its lines and their
    subjects and said nothing about either state. The subject is the flexible
    column, so it is the one that gives up its room.
    """
    rows = result_rows(
        [{"line": "L3", "subject": "X" * 80, "verdict": "BLOCK", "decision": "reject"}],
        locale="en",
        width=width,
    )
    assert any("✖ BLOCK" in row and "✖ reject" in row for row in rows), rows
    assert all(cell_len(row) <= width for row in rows)


def test_a_long_subject_is_cut_with_an_ellipsis_not_a_hard_edge() -> None:
    """The subject gives up its room, so the cut is marked with an ellipsis."""
    rows = result_rows([_review(subject="X" * 80, verdict="BLOCK", decision="reject")], None, "en", 80)
    line = rows[2]
    assert line.endswith("✖ BLOCK  ✖ reject")
    assert line.count("…") == 1


def test_the_convenience_columns_go_before_the_subject_is_cut() -> None:
    """§3.2: the reviewer, the gate and the fix count are dropped first.

    The subject is the flexible column, so a subject too long for the row costs
    the convenience columns their room before it costs the subject a character:
    at 120 columns this 90-cell subject keeps every cell and the row sheds the
    reviewer, the gate and the fix count instead.
    """
    subject = "Ship the terminal-native review queue screen with every gate green and a reviewer per line"
    assert cell_len(subject) == 90
    rows = result_rows([_review(subject=subject)], None, "en", 120)
    assert rows[2] == f"  L3    {subject}  ✔ MERGE  ✔ approve"


@pytest.mark.parametrize("verdict", [item[0] for item in VERDICTS])
@pytest.mark.parametrize("locale", _LOCALES)
def test_every_verdict_shows_its_glyph_and_word(verdict: str, locale: str) -> None:
    """§4.2: a verdict is a glyph and a word, on the row it belongs to."""
    _ident, glyph, _ascii, words = next(item for item in VERDICTS if item[0] == verdict)
    rows = result_rows([_review(verdict=verdict)], None, locale, 80)
    cell = glyph if words[locale] == glyph else f"{glyph} {words[locale]}"
    assert any(cell in row for row in rows), rows
    stacked = result_rows([_review(verdict=verdict)], None, locale, 40)
    assert any(cell in row for row in stacked), stacked


@pytest.mark.parametrize("decision", [item[0] for item in DECISIONS])
@pytest.mark.parametrize("locale", _LOCALES)
def test_every_decision_shows_its_glyph_and_word(decision: str, locale: str) -> None:
    _ident, glyph, _ascii, words = next(item for item in DECISIONS if item[0] == decision)
    rows = result_rows([_review(decision=decision)], None, locale, 80)
    cell = f"{glyph} {words[locale]}"
    assert any(cell in row for row in rows), rows
    stacked = result_rows([_review(decision=decision)], None, locale, 40)
    assert any(cell in row for row in stacked), stacked


@pytest.mark.parametrize("locale", _LOCALES)
def test_the_gate_of_a_line_is_a_word(locale: str) -> None:
    words = GATES[locale]
    for gate, word in words.items():
        rows = result_rows([_review(gate=gate)], None, locale, 80)
        assert any(word in row for row in rows), rows
        stacked = result_rows([_review(gate=gate)], None, locale, 40)
        assert any(word in row for row in stacked), stacked


@pytest.mark.parametrize("verdict", [item[0] for item in VERDICTS])
@pytest.mark.parametrize("decision", [item[0] for item in DECISIONS])
@pytest.mark.parametrize("locale", _LOCALES)
def test_the_ascii_glyphs_replace_the_unicode_ones(monkeypatch: pytest.MonkeyPatch, verdict: str, decision: str, locale: str) -> None:
    """``PTC_ASCII=1`` swaps the glyph for its twin and keeps the word."""
    monkeypatch.setenv("PTC_ASCII", "1")
    rows = result_rows([_review(verdict=verdict, decision=decision)], None, locale, 80)
    flat = "\n".join(rows)
    for table, ident in ((VERDICTS, verdict), (DECISIONS, decision)):
        _ident, glyph, ascii_glyph, words = next(item for item in table if item[0] == ident)
        assert ascii_glyph in flat, rows
        assert glyph not in flat, rows
        if words[locale] != glyph:
            # "none yet" is the dash itself, so its twin stands for the word.
            assert words[locale] in flat
    # The ASCII row keeps the columns and the words it had.
    assert STRINGS[locale]["title"] in flat
    assert STRINGS[locale]["col.line"] in flat


@pytest.mark.parametrize("locale", _LOCALES)
def test_a_missing_gate_falls_back_to_the_ascii_dash(monkeypatch: pytest.MonkeyPatch, locale: str) -> None:
    """§4.2: an absent gate is the shared dash, and ``PTC_ASCII=1`` gives it ``-``.

    The gate dash is the same :data:`_NONE_MARK` a verdict with no word yet
    uses, so it swaps for the hyphen in ASCII on the table row and on the
    stacked meta line alike — never the Unicode em dash.
    """
    monkeypatch.setenv("PTC_ASCII", "1")
    rows = result_rows([_review(gate=None)], None, locale, 80)
    assert "—" not in "\n".join(rows)
    assert rows[2] == {
        "en": "  L3    Add the review queue screen  L1        -         1    + MERGE  + approve",
        "zh-CN": "  L3    Add the review queue screen  L1        -         1    + 合并   + 批准",
    }[locale]
    stacked = result_rows([_review(gate=None)], None, locale, 40)
    assert "—" not in "\n".join(stacked)
    assert stacked[-1] == {
        "en": "    + MERGE · + approve · 1 · - · L1",
        "zh-CN": "    + 合并 · + 批准 · 1 · - · L1",
    }[locale]


def test_the_ascii_error_mark_and_spinner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PTC_ASCII", "1")
    assert usage_rows("en", 80) == ["  x usage: /review run <line> --by <line> | /review status [line]"]
    review = {"line": "L3", "subject": "Add the review queue screen", "state": "queued"}
    rows = progress_rows(review, "en", 80, frame=2)
    assert rows[0] == "  * L3    Add the review queue screen"


@pytest.mark.parametrize("locale", _LOCALES)
def test_an_ascii_theme_also_uses_the_ascii_glyphs(locale: str) -> None:
    theme = resolve_theme({"PTC_ASCII": "1", "CONDUCTOR_THEME": "dark", "LANG": "en_US.UTF-8"})
    rows = result_rows(_queue(locale), GATE, locale, 80, theme=theme)
    flat = "\n".join(rows)
    assert "+ " + ("MERGE" if locale == "en" else "合并") in flat
    assert "x " + ("BLOCK" if locale == "en" else "阻断") in flat
    assert "? " + ("pending" if locale == "en" else "待定") in flat
    assert "✔" not in flat
    assert "✖" not in flat
    assert "◆" not in flat


def test_the_spinner_frame_comes_from_the_theme() -> None:
    """§4.3: the card's mark is the spinner frame, not a state glyph."""
    frames = spinner_frames()
    assert frames == ("♪", "♫", "♬", "♫")
    review = {"line": "L3", "subject": "Add the review queue screen", "state": "running"}
    assert progress_rows(review, "en", 80, frame=2)[0] == "  ♬ L3    Add the review queue screen"
    assert progress_rows(review, "en", 80, frame=0)[0] == "  ♪ L3    Add the review queue screen"
    # The frames cycle, and the index is taken modulo their count.
    assert progress_rows(review, "en", 80, frame=5)[0] == "  ♫ L3    Add the review queue screen"


def test_reduced_motion_freezes_the_spinner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PTC_REDUCED_MOTION", "1")
    assert spinner_frames() == ("●",)
    review = {"line": "L3", "subject": "Add the review queue screen", "state": "running"}
    assert progress_rows(review, "en", 80, frame=2)[0] == "  ● L3    Add the review queue screen"


def test_an_ascii_theme_freezes_the_frame_to_its_ascii_twin(monkeypatch: pytest.MonkeyPatch) -> None:
    """§4.3: ``TERM=dumb`` freezes the frame, and an ASCII theme keeps it ASCII.

    ``tui_theme.spinner_frames`` reads the locale, not the theme, so a frozen
    frame set is the Unicode ``●`` of the running state whatever theme the
    caller resolved. An ASCII theme still gets that state's ASCII twin, so the
    mark of a queued or running row matches the rest of the screen instead of
    painting one Unicode glyph into an ASCII frame.
    """
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    theme = resolve_theme({"PTC_ASCII": "1"})
    assert theme.glyphs.done == "+"
    review = {"line": "L3", "subject": "Review", "state": "running"}
    assert progress_rows(review, "en", 80, theme=theme)[0] == "  * L3    Review"
    # Every frame index draws the same frozen mark.
    assert progress_rows(review, "en", 80, theme=theme, frame=3)[0] == "  * L3    Review"


def test_a_unicode_theme_freezes_the_frame_to_the_running_glyph(monkeypatch: pytest.MonkeyPatch) -> None:
    """The twin of the freeze is only for ASCII: a Unicode theme keeps ``●``."""
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    theme = resolve_theme()
    review = {"line": "L3", "subject": "Review", "state": "running"}
    assert progress_rows(review, "en", 80, theme=theme)[0] == "  ● L3    Review"


def test_an_ascii_theme_also_uses_the_ascii_error_mark() -> None:
    """The usage error reads its mark from the theme it was handed."""
    theme = resolve_theme({"PTC_ASCII": "1", "CONDUCTOR_THEME": "dark", "LANG": "en_US.UTF-8"})
    assert usage_rows("en", 80, theme=theme) == ["  x usage: /review run <line> --by <line> | /review status [line]"]


def test_a_resolved_theme_still_honors_reduced_motion(monkeypatch: pytest.MonkeyPatch) -> None:
    """§4.3: a theme carries an animated spinner but not the motion switch.

    ``tui_theme.spinner_frames`` is the one place reduced motion is written, and
    a resolved theme bakes the four-frame set into its glyphs without consulting
    it. Handing that theme to :func:`progress_rows` must still freeze the frame,
    so every frame index draws the same static mark.
    """
    monkeypatch.setenv("PTC_REDUCED_MOTION", "1")
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    theme = resolve_theme()
    # The theme itself is animated — reduced motion is not baked into it.
    assert theme.glyphs.spinner == ("♪", "♫", "♬", "♫")
    review = {"line": "L3", "subject": "Add the review queue screen", "state": "running"}
    frozen = progress_rows(review, "en", 80, theme=theme, frame=0)
    assert frozen == progress_rows(review, "en", 80, theme=theme, frame=1)
    assert frozen == progress_rows(review, "en", 80, theme=theme, frame=3)
    assert frozen[0] == "  ● L3    Add the review queue screen"


@pytest.mark.parametrize("locale", _LOCALES)
def test_the_meta_line_order_is_verdict_decision_fix_gate_reviewer(locale: str) -> None:
    """§3.3 and the stack plan: verdict, decision, fix, gate, reviewer."""
    rows = result_rows(_queue(locale), GATE, locale, 40)
    meta = [row.strip() for row in rows if row.startswith("    ")]
    words = {
        "en": (
            ("✔ MERGE", "✔ approve", "1", "reviewed", "L1"),
            ("✖ BLOCK", "✖ reject", "0", "build", "L2"),
            ("—", "◆ pending", "0", "reviewed", "L1"),
        ),
        "zh-CN": (
            ("✔ 合并", "✔ 批准", "1", "已评审", "L1"),
            ("✖ 阻断", "✖ 驳回", "0", "构建中", "L2"),
            ("—", "◆ 待定", "0", "已评审", "L1"),
        ),
    }[locale]
    parts = [part.strip() for row in meta for part in row.split(" · ")]
    assert parts == [part for review in words for part in review]


def test_the_english_meta_line_wraps_the_reviewer_under_the_row() -> None:
    """The 40-column frame of the design mock: the reviewer is the part that wraps."""
    rows = result_rows(_queue("en"), GATE, "en", 40)
    meta = [row for row in rows if row.startswith("    ")]
    assert meta == [
        "    ✔ MERGE · ✔ approve · 1 · reviewed",
        "    L1",
        "    ✖ BLOCK · ✖ reject · 0 · build · L2",
        "    — · ◆ pending · 0 · reviewed · L1",
    ]


@pytest.mark.parametrize("locale", _LOCALES)
def test_the_reviewer_cell_carries_the_line_and_the_model(locale: str) -> None:
    rows = result_rows([_review(provider="codex")], None, locale, 120)
    assert any("L1 codex" in row for row in rows), rows
    stacked = result_rows([_review(provider="codex")], None, locale, 40)
    assert any("L1 codex" in row for row in stacked), stacked


def test_the_reviewer_model_is_optional() -> None:
    rows = result_rows([_review()], None, "en", 80)
    assert "  L1        " in rows[2]


def test_an_empty_queue_says_so_in_both_languages() -> None:
    assert result_rows([], None, "en", 80) == ["  review queue", "  no cross-review is queued"]
    assert result_rows([], None, "zh-CN", 80) == ["  评审队列", "  没有排队中的交叉评审"]


def test_the_gate_block_is_optional() -> None:
    rows = result_rows(_queue("en"), None, "en", 80)
    assert not any("G6" in row for row in rows)
    assert not any("pinned" in row for row in rows)
    assert not any("conductor dispatch" in row for row in rows)


def test_the_gate_block_takes_a_mapping_as_well_as_a_record() -> None:
    rows = result_rows(
        _queue("en"),
        {"line": "L5", "sha": "0d41c7aa", "dispatch": "2b81e0d4"},
        "en",
        80,
    )
    assert rows[-3:] == [
        "  G6 human gate  approve L5",
        "  the reviewed commit is pinned at 0d41c7aa",
        "  conductor dispatch approve 2b81e0d4",
    ]


def test_a_review_record_from_the_server_is_accepted() -> None:
    """The wire shape of ``review status``: provider, head_sha, state, verdict."""
    rows = result_rows(
        [
            {
                "id": "3f0c9a11",
                "line": "L3",
                "subject": "Add the review queue screen",
                "reviewer": "L1",
                "provider": "codex",
                "state": "completed",
                "verdict": "merge",
                "head_sha": "4b2f7803",
            }
        ],
        None,
        "en",
        80,
    )
    # The gate cell is the dash padded to its 8 columns, then the 2-column gap.
    assert rows[2] == "  L3    Add the review queue screen  L1 codex  —" + " " * 9 + "0    ✔ MERGE  ◆ pending"


def test_objects_as_well_as_mappings_are_accepted() -> None:
    class Review:
        line = "L3"
        subject = "Add the review queue screen"
        reviewer = "L1"
        gate = "reviewed"
        fix = 2
        verdict = "block"
        decision = "reject"

    rows = result_rows([Review()], None, "en", 80)
    assert rows[2] == "  L3    Add the review queue screen  L1        reviewed  2    ✖ BLOCK  ✖ reject"


def test_an_unknown_verdict_is_the_dash_of_the_spec() -> None:
    rows = result_rows([_review(verdict="something-new", decision="pending")], None, "en", 80)
    assert rows[2].endswith("—        ◆ pending")
