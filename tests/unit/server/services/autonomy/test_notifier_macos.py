"""macOS notification channel. The runner is injected, so osascript never starts."""

from __future__ import annotations

from types import SimpleNamespace

from src.server.services.autonomy import notifier_macos


def _bare_quotes(script: str) -> int:
    """Quotes that are not escaped by a preceding backslash."""
    count = 0
    index = 0
    while index < len(script):
        if script[index] == "\\" and index + 1 < len(script):
            index += 2
            continue
        if script[index] == '"':
            count += 1
        index += 1
    return count


def test_applescript_escapes_backslash_before_quote_and_flattens_controls():
    assert notifier_macos.escape_applescript('a\\b"c') == 'a\\\\b\\"c'
    assert notifier_macos.escape_applescript("line\n\tnext\x00") == "line  next"
    assert notifier_macos.escape_applescript("a\rb") == "a b"

    payload = '"; do shell script "echo pwned'
    script = (
        'display notification "'
        + notifier_macos.escape_applescript(payload)
        + '" with title "t"'
    )
    assert _bare_quotes(script) == 4
    assert "\n" not in script


def test_deliver_runs_osascript_only_on_darwin_with_an_injected_runner():
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> int:
        calls.append(list(argv))
        return 0

    assert (
        notifier_macos.deliver("hi", "there", runner=runner, platform="linux") is None
    )
    assert calls == []

    assert (
        notifier_macos.deliver(
            "Title",
            'say "hello"\nnext',
            runner=runner,
            platform="darwin",
        )
        is True
    )
    assert calls[0][:2] == ["osascript", "-e"]
    assert len(calls[0]) == 3
    script = calls[0][2]
    assert script.startswith("display notification ")
    assert "\n" not in script
    assert '\\"hello\\"' in script
    assert _bare_quotes(script) == 4

    assert (
        notifier_macos.deliver("Title", "body", runner=lambda argv: 7, platform="darwin")
        is False
    )

    def broken(argv: list[str]) -> int:
        raise OSError("osascript missing")

    assert notifier_macos.deliver("Title", "body", runner=broken, platform="darwin") is False


def test_deliver_clips_plain_text_before_escaping(monkeypatch):
    seen: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["kwargs"] = kwargs
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(notifier_macos.subprocess, "run", fake_run)
    # The real runner refuses under pytest; this test fakes subprocess.run.
    monkeypatch.setattr(notifier_macos, "banners_enabled", lambda: True)
    title = ("a" * 119) + "\\" + ("b" * 50)
    assert notifier_macos.deliver(title, "Body", platform="darwin") is True
    argv = seen["argv"]
    assert isinstance(argv, list)
    assert argv[0:2] == ["osascript", "-e"]
    script = str(argv[2])
    assert "\\" not in script
    assert script.endswith('"')
    kwargs = seen["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["check"] is False
    assert kwargs["timeout"] == 5
    assert "shell" not in kwargs

    def refuse(*_args, **_kwargs):
        raise AssertionError("osascript must not run off darwin")

    monkeypatch.setattr(notifier_macos.subprocess, "run", refuse)
    assert notifier_macos.deliver("Title", "Body", platform="linux") is None
