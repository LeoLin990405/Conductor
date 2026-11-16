"""Isolate HOME for every collaboration protocol test.

N80's common acceptance rule is that the suite runs with HOME in a temporary
directory and proves the real HOME was not touched. These tests only import
the protocol package, but the rule applies to the directory as a whole so a
future test cannot quietly read a developer's ``~/.conductor``.
"""

from __future__ import annotations

import pytest

from tests.unit._isolated_home import assert_untouched, isolate


@pytest.fixture(autouse=True)
def isolated_collab_home(tmp_path, monkeypatch):
    home, original, before = isolate(tmp_path, monkeypatch)
    yield home
    assert_untouched(original, before)
