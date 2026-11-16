"""The terminal client strips every character the live-output guard folds or refuses.

The guard (server and daemon) looks *through* default-ignorable and format
characters when it scans and taints on the ones that reorder or smuggle text.
The CLI/TUI is a separate package that cannot import the server, so its strip
table is a copy; this pins the copy to the guard's tables, read from the
client's source so the check needs no TUI dependency.
"""

from __future__ import annotations

import ast
from pathlib import Path

from src.server.utils import output_guard as guard

CLIENT_TEXT = (
    Path(__file__).resolve().parents[4] / "src/conductor_cli/terminal_text.py"
)


def _client_ranges() -> list[tuple[int, int]]:
    tree = ast.parse(CLIENT_TEXT.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_HIDDEN_RANGES"
            and node.value is not None
        ):
            return list(ast.literal_eval(node.value))
    raise AssertionError("_HIDDEN_RANGES not found in the client's text module")


def _covered(low: int, high: int, ranges: list[tuple[int, int]]) -> bool:
    position = low
    for start, end in sorted(ranges):
        if start <= position <= end:
            position = end + 1
        if position > high:
            return True
    return position > high


def test_the_client_strips_everything_the_guard_folds_or_refuses():
    client = _client_ranges()
    missing = [
        (hex(low), hex(high))
        for low, high in (*guard._INVISIBLE_RANGES, *guard._TAINT_RANGES)
        if not _covered(low, high, client)
    ]
    assert missing == []
