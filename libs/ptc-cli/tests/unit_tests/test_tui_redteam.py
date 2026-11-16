"""Red-team probes for the Super-agent TUI renderer and theme.

A probe passes when untrusted text cannot drive the terminal, escape a frame,
or touch the filesystem. It fails when the product is vulnerable.
"""

from __future__ import annotations

import contextlib
import re
import sys
import threading
import time
from pathlib import Path

import pytest

# tui imports conductor_cli, which lives in the repo src tree; the ptc-cli
# lockfile's conductor-core is a different PyPI project.
_SRC = Path(__file__).resolve().parents[4] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ptc_cli.superagent.tui import SuperAgentTui, fit_status_line, row_text  # noqa: E402
from ptc_cli.superagent.tui_render import (  # noqa: E402
    render_markdown,
    render_plain,
    strip_protocol_trailers,
    wrap_words,
)
from ptc_cli.superagent.tui_sessions import SessionStore  # noqa: E402
from ptc_cli.superagent.tui_theme import resolve_theme, style_for  # noqa: E402
from tests.unit_tests.test_superagent_tui_app import FakeClient  # noqa: E402

_HEX = re.compile(r"#[0-9A-Fa-f]{3,8}")
_HOSTILE_TITLE = "legit title\nINJECTED-ROW\r\nCARRIAGE"


def _joined(rows: list[str]) -> str:
    return "\n".join(rows)


def _style_blob(style: object) -> str:
    rules = getattr(style, "style_rules", None)
    if rules is None:
        rules = getattr(style, "_style_rules", ())
    return repr(rules)


def _theme_paint(theme: object) -> str:
    fields = (
        "fg",
        "muted",
        "faint",
        "baton",
        "tempo",
        "ok",
        "hold",
        "error",
        "rule",
        "selection_bg",
        "accent",
        "user",
        "tool",
        "warn",
        "danger",
    )
    parts = [str(getattr(theme, name, "") or "") for name in fields]
    parts.extend(style_for(theme, name) for name in ("fg", "baton", "error", "hold"))
    return "\n".join(parts)


def _bounded(fn, *args, limit: float = 2.0):
    """Run ``fn`` and fail if it is still going after ``limit`` seconds."""
    box: dict[str, object] = {}

    def run() -> None:
        try:
            box["value"] = fn(*args)
        except Exception as exc:  # noqa: BLE001 — the caller decides which errors are allowed
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(limit)
    if worker.is_alive():
        pytest.fail(f"{getattr(fn, '__name__', fn)} did not finish within {limit}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def test_render_plain_strips_escapes_osc8_and_sgr():
    """ESC, OSC 8, and raw SGR in a result body are not executable escapes."""
    body = (
        "answer line\n"
        "\x1b[31mred\x1b[0m\n"
        "\x1b]8;;http://evil.example/phish\x07click\x1b]8;;\x07\n"
        "\x1b[1;4;31mSGR\x1b[0m\n"
        "[click](http://evil.example/phish)\n"
    )
    rendered = _joined(render_plain(body, 72))
    assert "\x1b" not in rendered
    assert "answer line" in rendered
    assert "click" in rendered
    assert "http://evil.example/phish" not in rendered or "\x1b" not in rendered


def test_autonomy_trailers_drop_and_near_misses_stay():
    """Whole AUTONOMY_* trailer lines go. A near miss is ordinary text."""
    body = "\n".join(
        [
            "The answer is 42.",
            "AUTONOMY_STATE: ready",
            "still here",
            "AUTONOMY_QUESTION: which branch?",
            "AUTONOMY_STATE ready",
            "autonomy_state: ready",
            "autonomy_question: kept",
        ]
    )
    rendered = _joined(render_plain(body, 80))
    stripped = strip_protocol_trailers(body)
    for shown in (rendered, stripped):
        assert "The answer is 42." in shown
        assert "still here" in shown
        assert "AUTONOMY_STATE:" not in shown
        assert "AUTONOMY_QUESTION:" not in shown
        assert "AUTONOMY_STATE ready" in shown
        assert "autonomy_state: ready" in shown
        assert "autonomy_question: kept" in shown


def test_huge_width_and_width_one_do_not_raise_and_zero_does_not_hang():
    sample = "hello 你好 **bold** `code`"
    for width in (1, 100_000):
        assert _bounded(render_plain, sample, width) is not None
        assert _bounded(render_markdown, sample, width) is not None
        assert _bounded(wrap_words, sample, width) is not None
    # Width 0 is clamped or rejected. Either is safe if it returns at once.
    for fn in (render_plain, render_markdown, wrap_words):
        with contextlib.suppress(ValueError, OverflowError):
            _bounded(fn, sample, 0, limit=2.0)


def test_wrap_words_handles_a_long_cjk_line_without_splitting_ascii():
    atom = ("你" * 8) + " hello "
    line = atom * ((200_000 + len(atom) - 1) // len(atom))
    assert len(line) >= 200_000
    assert "\n" not in line
    started = time.perf_counter()
    wrapped = wrap_words(line, 32)
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, elapsed
    assert wrapped
    for row in wrapped:
        for word in re.findall(r"[A-Za-z]+", row):
            assert word == "hello", word


def test_skill_paths_are_rendered_not_resolved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A skill-like path and a symlink label stay text. Nothing is opened."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    payload = "skill ../../.ssh/id_rsa\nlabel owned -> ../../.ssh/id_rsa"
    resolved: list[str] = []
    opened: list[str] = []
    real_resolve = Path.resolve
    real_open = open

    def tracking_resolve(self: Path, *args, **kwargs):
        resolved.append(str(self))
        return real_resolve(self, *args, **kwargs)

    def tracking_open(file, *args, **kwargs):
        text = str(file)
        opened.append(text)
        if "id_rsa" in text or ".ssh" in text:
            raise AssertionError(f"renderer opened {text}")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", tracking_resolve)
    monkeypatch.setattr("builtins.open", tracking_open)
    before = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
    rendered = _joined(render_plain(payload, 80))
    after = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
    assert after == before
    assert "id_rsa" in rendered
    assert "../../.ssh/id_rsa" in rendered or "owned ->" in rendered
    assert "OPENSSH PRIVATE KEY" not in rendered
    assert not any("id_rsa" in item or ".ssh" in item for item in resolved)
    assert not any("id_rsa" in item or ".ssh" in item for item in opened)
    assert "\x1b" not in rendered


def test_table_cells_cannot_break_out_with_markdown_or_toml():
    """A cell cannot close a link or open a TOML table outside the frame."""
    link_break = '" ](http://evil)'
    toml_break = 'name = "x"\n[mcp]'
    markdown = (
        "| name | value |\n"
        "| --- | --- |\n"
        f"| {link_break} | {toml_break} |\n"
    )
    frame = _joined(render_plain(markdown, 80))
    assert "](" not in frame
    assert "\x1b" not in frame
    for line in frame.splitlines():
        stripped = line.strip()
        assert stripped != "[mcp]"
        assert not stripped.startswith("[mcp]")
    assert "name" in frame


def test_malicious_ticket_title_does_not_add_status_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Newlines and carriage returns in a ticket title stay on one status row."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "conductor"))
    tui = SuperAgentTui(
        server_url="http://test",
        user_id="u1",
        client=FakeClient(),
        session_store=SessionStore(tmp_path / "sessions"),
        history_path=tmp_path / "history",
        local_root=tmp_path,
        worktree_root=tmp_path,
        no_splash=True,
    )
    tui._recent_tickets = [
        {
            "ticket_id": "abcdefab",
            "status": "running",
            "title": _HOSTILE_TITLE,
        }
    ]
    fragments = list(tui._status_fragments())
    footer = "".join(text for _style, text in fragments)
    assert footer
    assert "\n" not in footer
    assert "\r" not in footer
    assert len(footer.splitlines()) <= 1
    assert "INJECTED-ROW" not in footer
    assert "CARRIAGE" not in footer

    # The footer helper is what joins the status row. A title is one segment.
    fitted = fit_status_line([_HOSTILE_TITLE, "auto", "1 online"], 80)
    cr_only = fit_status_line(["legit\rtitle", "auto"], 80)
    for line in (fitted, cr_only):
        assert "\n" not in line
        assert "\r" not in line
        assert len(line.splitlines()) <= 1

    home = tui._home_rows()
    title_rows = [
        row_text(row)
        for row in home
        if "legit title" in row_text(row) or "INJECTED-ROW" in row_text(row)
    ]
    assert title_rows
    assert len(title_rows) == 1
    assert "\n" not in title_rows[0]
    assert "\r" not in title_rows[0]


def test_render_markdown_is_safe_across_threads():
    source = "# Title\n\nhello **world** and `code`\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n"
    barrier = threading.Barrier(8)
    outputs: list[object] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            rendered = render_markdown(source, 72)
        except BaseException as exc:  # noqa: BLE001 — the probe records every failure
            with lock:
                errors.append(exc)
            return
        with lock:
            outputs.append(rendered)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert len(outputs) == 8
    assert all(item == outputs[0] for item in outputs)


def test_none_and_no_color_themes_have_no_hex():
    for environ in (
        {"CONDUCTOR_THEME": "none"},
        {"NO_COLOR": "1"},
        {"CONDUCTOR_THEME": "none", "NO_COLOR": "1"},
    ):
        theme = resolve_theme(environ)
        assert theme.name == "none"
        assert _HEX.search(_theme_paint(theme)) is None
        from ptc_cli.superagent.tui import build_default_style

        assert _HEX.search(_style_blob(build_default_style(environ))) is None


def test_unknown_conductor_theme_falls_back_to_dark():
    """Unknown ``CONDUCTOR_THEME`` does not raise.

    Fallback, when ``COLORFGBG`` is unset, is auto detection and that returns
    ``dark``. ``NO_COLOR`` is not set in this environ, so it does not preempt
    auto.
    """
    theme = resolve_theme({"CONDUCTOR_THEME": "not-a-real-theme"})
    assert theme.name == "dark"
    # The same call with a second unknown value still resolves, it does not throw.
    other = resolve_theme({"CONDUCTOR_THEME": "'; [mcp]"})
    assert other.name == "dark"
