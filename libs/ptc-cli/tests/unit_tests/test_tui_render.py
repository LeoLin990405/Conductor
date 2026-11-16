"""Markdown rendering, trailer stripping, and CJK wrap."""

from __future__ import annotations

from ptc_cli.superagent.tui_render import (
    render_markdown,
    render_plain,
    strip_protocol_trailers,
    wrap_words,
)
from ptc_cli.superagent.tui_theme import resolve_theme


def test_autonomy_trailers_drop_and_near_misses_stay() -> None:
    body = "answer\nAUTONOMY_STATE: idle\nkeep AUTONOMY_STATE in a sentence\nautonomy_state: no\n"
    cleaned = strip_protocol_trailers(body)
    assert "AUTONOMY_STATE:" not in cleaned
    assert "keep AUTONOMY_STATE in a sentence" in cleaned
    assert "autonomy_state: no" in cleaned


def test_kinsoku_stays_on_the_previous_token_and_the_next_word_is_whole() -> None:
    wrapped = wrap_words("intro " + ("a" * 72) + "，abcdef", 78)  # noqa: RUF001
    assert wrapped == ["intro", "  " + ("a" * 72) + "，", "  abcdef"]  # noqa: RUF001


def test_markdown_it_keeps_code_literals_and_table_links() -> None:
    theme = resolve_theme({"CONDUCTOR_THEME": "dark"})
    fence = "```\n| literal ]( marker\nnext\n```\n"
    indented = "    ]( inside\n"
    linked = "| a |\n| --- |\n| [My label](/Users/example/test.py) |\n"
    fence_rows = render_markdown(fence, 80, theme)
    fence_flat = "\n".join("".join(text for _style, text in row) for row in fence_rows)
    assert "](" in fence_flat
    assert "next" in fence_flat
    assert fence_flat.count("```") == 0
    indented_flat = "\n".join(render_plain(indented, 80))
    assert "](" in indented_flat
    assert indented_flat.splitlines()[0].startswith("│")
    linked_flat = "\n".join(render_plain(linked, 80))
    assert "My label" in linked_flat
    assert "/Users/example/test.py" in linked_flat
    assert "](" not in linked_flat


def test_kinsoku_moves_the_whole_word() -> None:
    wrapped = wrap_words("intro " + ("a" * 72) + "，尾", 78)
    assert any("a" * 72 in line for line in wrapped)
    assert not any(0 < line.count("a") < 72 for line in wrapped)
    assert wrap_words("abcd  e", 4) == ["abcd", "  e"]


def test_code_and_link_text_keep_paths_and_markers() -> None:
    theme = resolve_theme({"CONDUCTOR_THEME": "dark", "LANG": "en_US.UTF-8"})
    fence = "```\n| literal ]( marker |\n```\n"
    table = "| cell |\n| --- |\n| `a](b` |\n"
    code = "See `(/Users/example/test.py)` in prose /Users/example/test.py.\n"
    for source, needle in (
        (fence, "]("),
        (table, "a](b"),
        (code, "(/Users/example/test.py)"),
    ):
        rows = render_markdown(source, 80, theme)
        flat = "\n".join("".join(text for _style, text in row) for row in rows)
        assert needle in flat
    prose = render_markdown("Path /Users/example/test.py stays.", 80, theme)
    flat = "\n".join("".join(text for _style, text in row) for row in prose)
    assert "/Users/example/test.py" in flat


def test_ascii_words_are_not_split_and_kinsoku_moves_back() -> None:
    wrapped = wrap_words("hello world", 8)
    assert all("hello" not in line or line.strip() == "hello" or "hello world" in line for line in wrapped)
    assert not any(line.startswith("world") and "hello" in "".join(wrapped) and "hell" == line[:4] for line in wrapped)
    joined = " ".join(line.strip() for line in wrapped)
    assert "hello" in joined and "world" in joined
    cjk = wrap_words("读取文件，然后列出名字", 8, hanging=0)
    for line in cjk[1:]:
        assert not line.startswith(tuple("，。、；：？！）」』"))


def test_markdown_table_has_no_raw_markers() -> None:
    source = (
        "See **names** in `registry.py`.\n\n"
        "| Name | Adapter |\n| --- | --- |\n| deepseek | DeepSeek |\n| zhipu | Zhipu |\n\n"
        "Done. [docs](/tmp/registry.py:29)\n"
        "AUTONOMY_QUESTION: should this run?\n"
    )
    theme = resolve_theme({"CONDUCTOR_THEME": "dark"})
    rows = render_markdown(source, 60, theme)
    flat = "\n".join("".join(text for _token, text in row) for row in rows)
    assert "AUTONOMY_" not in flat
    assert "**" not in flat
    assert "`" not in flat
    assert "](" not in flat
    assert "deepseek" in flat
    assert "zhipu" in flat
    assert not any(line.strip().startswith("|") for line in flat.splitlines())


def test_plain_render_matches_layout_without_styles() -> None:
    plain = render_plain("hello **there**", 40)
    assert any("there" in line for line in plain)
    assert all("**" not in line for line in plain)


def test_markdown_spans_blanks_gutter_and_code_literals() -> None:
    """Headings, inline code, and fences keep different classes and literals."""
    source = (
        "# Heading\n\n"
        "See `a](b ** c` and **strong**.\n\n"
        "```python\n"
        "x = 2 ** 3  # ](\n"
        "```\n\n"
        "After.\n"
    )
    theme = resolve_theme({"CONDUCTOR_THEME": "dark", "LANG": "en_US.UTF-8"})
    rows = render_markdown(source, 60, theme)
    classes: list[set[str]] = []
    texts: list[str] = []
    for row in rows:
        styles = {style for style, text in row}
        classes.append(styles)
        texts.append("".join(text for _style, text in row))
        for style, _text in row:
            assert style.startswith("class:"), style
            assert "#" not in style
    flat = "\n".join(texts)
    assert {"class:tui-heading"} in classes
    assert any("class:tui-inline" in styles for styles in classes)
    assert any("class:tui-code" in styles for styles in classes)
    assert any("class:tui-strong" in styles for styles in classes)
    heading = next(styles for styles in classes if "class:tui-heading" in styles)
    code = next(styles for styles in classes if "class:tui-code" in styles)
    assert heading.isdisjoint(code)
    assert "" in texts
    assert not any(left == "" and right == "" for left, right in zip(texts, texts[1:]))
    assert any(line.startswith("│") and "2 ** 3" in line and "](" in line for line in texts)
    assert "a](b ** c" in flat
    assert "Heading" in flat
    assert "After." in flat
    plain = "\n".join(render_plain(source, 60))
    assert "status: completed" not in plain
    assert "2 ** 3" in plain
    assert "│" in plain
    assert "\n\n" in plain
