"""The TUI display module re-exports the core scanner. It does not keep a copy."""

from __future__ import annotations

import conductor_cli.terminal_text as core
import ptc_cli.superagent.text as client
import pytest

_NAMES = (
    "_drop_strings",
    "_string_end",
    "_csi_end",
    "_construct_end",
    "_line_breaks",
    "_HIDDEN_RANGES",
    "_HIDDEN",
    "_OSC_ENDS",
    "_C1_OSC_ENDS",
    "_STRING_ENDS",
    "_END_OSC",
    "_END_C1_OSC",
    "_END_STRING",
    "_INTRODUCER",
    "_ESC_STRING_ENDS",
    "_C1_STRING_ENDS",
    "_CSI_ANY",
    "_CSI_ANY_PREFIX",
    "_CSI_HOLD_MAX",
    "_STRING_HOLD_MAX",
    "_C0_DROPPED",
    "_OSC",
    "_C1_OSC",
    "_C1_STRING_CONTROL",
    "_ESC_STRING_CONTROL",
    "_CSI",
    "_C1_CSI",
    "_CONTROLS",
    "_OSC_OPEN",
    "_ESC_STRING_OPEN",
    "_C1_OSC_OPEN",
    "_C1_STRING_OPEN",
    "_CSI_OPEN",
    "_C1_CSI_OPEN",
    "_scan",
    "plain",
    "strip_controls",
    "TokenSanitizer",
)


@pytest.mark.parametrize("name", _NAMES)
def test_tui_name_is_the_core_object(name: str) -> None:
    assert getattr(client, name) is getattr(core, name)
