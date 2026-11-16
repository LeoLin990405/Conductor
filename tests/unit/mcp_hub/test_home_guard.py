"""The ``isolated_home`` guard itself refuses writes under the real HOME."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from .conftest import GUARD_TOUCHED, real_home


def test_guard_sets_home_and_refuses_real_home_writes(isolated_home: Path) -> None:
    assert Path(os.environ["HOME"]) == isolated_home
    assert Path.home() == isolated_home
    target = real_home() / ".conductor-hub-guard-probe-never-created"
    with pytest.raises(AssertionError):
        target.write_text("x")
    with pytest.raises(AssertionError):
        os.open(target, os.O_WRONLY | os.O_CREAT, 0o600)
    with pytest.raises(AssertionError):
        os.replace(isolated_home / "missing", target)
    assert not target.exists()
    assert len(GUARD_TOUCHED) == 3
    GUARD_TOUCHED.clear()
    (isolated_home / "ok.txt").write_text("fine")
