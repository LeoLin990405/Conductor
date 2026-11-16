"""The real-HOME tripwire must catch leakage without failing on live daemons."""

from __future__ import annotations

import pytest

from tests.unit._isolated_home import assert_untouched, snapshot


def _probe_home(tmp_path):
    home = tmp_path / "probe-home"
    home.mkdir()
    return home


def test_unrelated_daemon_state_can_keep_writing(tmp_path) -> None:
    """A background CLI rewrites its own store; that is not test leakage."""
    home = _probe_home(tmp_path)
    state_dir = home / ".grokbot"
    state_dir.mkdir()
    state = state_dir / "daemon.json"
    state.write_text("one")

    before = snapshot(home)
    state.write_text("two")
    (state_dir / "supervisor.json").write_text("{}")

    assert_untouched(home, before)


def test_a_new_home_child_is_still_refused(tmp_path) -> None:
    home = _probe_home(tmp_path)
    before = snapshot(home)

    (home / ".conductor").mkdir()

    with pytest.raises(AssertionError, match=r"\.conductor"):
        assert_untouched(home, before)


def test_content_written_under_a_watched_store_is_refused(tmp_path) -> None:
    home = _probe_home(tmp_path)
    store = home / ".conductor"
    store.mkdir()
    before = snapshot(home)

    (store / "config.json").write_text("{}")

    with pytest.raises(AssertionError, match=r"\.conductor"):
        assert_untouched(home, before)


def test_overwriting_an_existing_nested_config_is_refused(tmp_path) -> None:
    """A nested file write does not move its parent directory's metadata.

    The snapshot must therefore carry every descendant of a watched store; a
    direct-child signature would see the same ``.conductor`` size and mtime and
    let the overwrite pass.
    """
    home = _probe_home(tmp_path)
    nested = home / ".conductor" / "state"
    nested.mkdir(parents=True)
    config = nested / "config.json"
    config.write_text('{"theme": "light"}')
    before = snapshot(home)

    config.write_text('{"theme": "dark"}')

    with pytest.raises(AssertionError, match=r"\.conductor/state/config\.json"):
        assert_untouched(home, before)


def test_a_transient_top_level_lock_from_another_tool_is_not_a_leak(tmp_path):
    fake_home = tmp_path / "operator"
    fake_home.mkdir()
    (fake_home / ".claude.json").write_text("{}")
    before = snapshot(fake_home)
    (fake_home / ".claude.json.lock").write_text("")
    (fake_home / ".claude.json.tmp.123").write_text("")
    assert_untouched(fake_home, before)

    (fake_home / ".conductor").mkdir()
    with pytest.raises(AssertionError, match=r"\.conductor"):
        assert_untouched(fake_home, before)
