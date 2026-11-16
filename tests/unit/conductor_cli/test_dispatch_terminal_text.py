"""CLI and TUI must keep the full display sanitizer on gate text.

These assertions failed on ``472a8c89a``: a lone surrogate aborted the human
status path, and a weakened cleaner left bidi controls and control-sequence
payloads on the terminal.
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout

import pytest

from conductor_cli import cli
from ptc_cli.superagent.dispatch_view import render_dispatch_lines
from ptc_cli.superagent.tui_blocks import DispatchBlock

_SURROGATE = "passed " + chr(0xD800)
_BIDI = "\u202eEGREM"
_CASES = {
    "surrogate": _SURROGATE,
    "bidi": _BIDI,
    "dcs": "\x1bPSECRET_DCS\x1b\\",
    "dcs-open": "\x1bPSECRET_DCS_OPEN",
    "osc": "\x1b]0;SECRET_OSC\x07",
    "osc-open": "\x1b]SECRET_OSC_OPEN",
    "c1": "\x9d0;SECRET_C1\x07",
    "c1-open": "\x90SECRET_C1_OPEN",
}
_PAYLOADS = (
    "SECRET_DCS_OPEN",
    "SECRET_DCS",
    "SECRET_OSC_OPEN",
    "SECRET_OSC",
    "SECRET_C1_OPEN",
    "SECRET_C1",
)


def _summary(reason: str) -> dict:
    return {
        "id": "audit",
        "state": "APPROVAL",
        "lines": [{"id": "line-0"}],
        "gates": {
            "lines": {
                "line-0": {
                    "phase": "awaiting_human",
                    "gates": {"G2": {"result": "pass", "reason": reason}},
                }
            }
        },
    }


class _FakeAPI:
    reason = ""

    def __init__(self, **_kwargs: object) -> None:
        return None

    def get(self, _path: str) -> dict:
        return _summary(self.reason)

    def close(self) -> None:
        return None


def _assert_clean(text: str) -> None:
    encoded = text.encode("utf-8")
    assert encoded.decode("utf-8") == text
    assert "\u202e" not in text
    assert "\ud800" not in text
    for payload in _PAYLOADS:
        assert payload not in text


@pytest.mark.parametrize("reason", _CASES.values(), ids=_CASES.keys())
def test_cli_status_keeps_gate_text_display_safe(monkeypatch: pytest.MonkeyPatch, reason: str) -> None:
    _FakeAPI.reason = reason
    monkeypatch.setattr(cli, "APIClient", _FakeAPI)
    raw = io.BytesIO()
    stdout = io.TextIOWrapper(raw, encoding="utf-8", errors="strict")
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = cli.main(["dispatch", "status", "audit"])
        stdout.flush()
    assert code == 0, stderr.getvalue()
    _assert_clean(raw.getvalue().decode("utf-8"))


@pytest.mark.parametrize("reason", _CASES.values(), ids=_CASES.keys())
def test_tui_dispatch_block_keeps_gate_text_display_safe(reason: str) -> None:
    rendered = "\n".join(DispatchBlock(lines=render_dispatch_lines(_summary(reason))).render_plain())
    _assert_clean(rendered)
