"""Isolate HOME for every collaboration persistence test.

The database helpers are pure SQL shapes in these unit tests, but the same
N80 acceptance rule applies: HOME lives in ``tmp_path`` and the fixture asserts
the operator's real HOME is unchanged when the test finishes.
"""

from __future__ import annotations

import pytest

from tests.unit._isolated_home import assert_untouched, isolate


@pytest.fixture(autouse=True)
def isolated_collab_db_home(tmp_path, monkeypatch):
    home, original, before = isolate(tmp_path, monkeypatch)
    yield home
    assert_untouched(original, before)
