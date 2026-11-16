"""An agent file that is not UTF-8 is reported, not fatal.

``status`` and ``diff`` read every file a target loads. One file with bytes
that do not decode (``b"\\xff\\xfe"``) used to raise ``UnicodeDecodeError`` and
abort the whole command, hiding the other targets. It is now a mismatch with an
``error``, and every other file is still reported.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

from src.conductor_cli.cli import main
from src.instructions_hub.render import render
from src.instructions_hub.store import diff_one, status

_BAD = b"\xff\xfe"


def _write(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")


def _setup(home: Path) -> None:
    """Codex's file is undecodable; dsh's shared file is current."""
    (codex,) = render("operator", "codex", home=home).files
    _write(codex.path, _BAD)
    shared, _cli = render("operator", "dsh", home=home).files
    _write(shared.path, shared.body)


def test_status_reports_the_bad_file_and_keeps_the_others(home: Path) -> None:
    _setup(home)
    rows = {row["target"]: row for row in status(home)["targets"]}

    codex = rows["codex"]
    assert codex["matches"] is False
    (bad,) = codex["files"]
    assert bad["exists"] is True
    assert bad["matches"] is False
    assert bad["managed"] is False
    assert "not UTF-8" in bad["error"]

    dsh = rows["dsh"]
    assert dsh["files"][0]["matches"] is True
    assert "error" not in dsh["files"][0]
    assert len(rows) > 2


def test_diff_reports_the_bad_file_without_its_content(home: Path) -> None:
    _setup(home)
    result = diff_one(home, "operator", "codex")
    assert result["same"] is False
    assert result["current"] is None
    (bad,) = result["files"]
    assert bad["exists"] is True
    assert "not UTF-8" in bad["error"]
    assert bad["rendered"]


def test_cli_status_and_preview_do_not_abort(home: Path) -> None:
    _setup(home)
    for argv in (
        ["instructions", "status", "--home", str(home)],
        ["instructions", "diff", "--home", str(home), "--target", "codex"],
        ["instructions", "render", "--home", str(home), "--target", "codex"],
    ):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            code = main(argv)
        assert code == 0, argv
        json.loads(stdout.getvalue())
