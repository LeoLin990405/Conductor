"""Regression tests for the ps/pty environment probes and their skip hook.

These pin the DeepSeek-sandbox failure modes (``ps`` exec blocked with EPERM,
no pty devices left) so marked tests skip with a clear reason there, and run
unchanged where the capability exists.
"""

from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import tests.env_probe as env_probe


class _FakeItem:
    """Minimum of pytest's item API the collection hook touches."""

    def __init__(self, *marks: str):
        self.keywords = {m: object() for m in marks}
        self.marks: list = []

    def add_marker(self, marker, append: bool = True) -> None:
        self.marks.append(marker)


def test_ps_probe_is_false_when_exec_is_blocked(monkeypatch):
    def _blocked(*args, **kwargs):
        raise PermissionError(1, "Operation not permitted", "ps")

    monkeypatch.setattr(env_probe.subprocess, "run", _blocked)
    assert env_probe.ps_available() is False


def test_ps_probe_is_false_when_ps_is_missing(monkeypatch):
    def _missing(*args, **kwargs):
        raise FileNotFoundError("ps")

    monkeypatch.setattr(env_probe.subprocess, "run", _missing)
    assert env_probe.ps_available() is False


def test_ps_probe_is_false_when_ps_times_out(monkeypatch):
    def _hang(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="ps", timeout=10)

    monkeypatch.setattr(env_probe.subprocess, "run", _hang)
    assert env_probe.ps_available() is False


def test_ps_probe_is_true_when_ps_answers(monkeypatch):
    monkeypatch.setattr(
        env_probe.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )
    assert env_probe.ps_available() is True


def test_ps_probe_is_false_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(
        env_probe.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1),
    )
    assert env_probe.ps_available() is False


def test_pty_probe_is_false_when_out_of_devices(monkeypatch):
    def _exhausted():
        raise OSError("out of pty devices")

    monkeypatch.setattr(env_probe.pty, "openpty", _exhausted)
    assert env_probe.pty_available() is False


def test_pty_probe_is_true_when_a_pair_opens(monkeypatch):
    fds = os.open("/dev/null", os.O_RDONLY), os.open("/dev/null", os.O_RDONLY)
    monkeypatch.setattr(env_probe.pty, "openpty", lambda: fds)
    assert env_probe.pty_available() is True


def test_hook_skips_marked_tests_only_when_capability_is_missing(monkeypatch):
    ps_item = _FakeItem("requires_ps")
    pty_item = _FakeItem("requires_pty")
    plain_item = _FakeItem()
    monkeypatch.setattr(env_probe, "ps_available", lambda: False)
    monkeypatch.setattr(env_probe, "pty_available", lambda: True)

    env_probe.pytest_collection_modifyitems([ps_item, pty_item, plain_item])

    assert len(ps_item.marks) == 1
    assert ps_item.marks[0].name == "skip"
    assert "ps" in ps_item.marks[0].kwargs["reason"]
    assert pty_item.marks == []
    assert plain_item.marks == []


def test_hook_reports_the_missing_pty_with_a_clear_reason(monkeypatch):
    pty_item = _FakeItem("requires_pty")
    monkeypatch.setattr(env_probe, "ps_available", lambda: True)
    monkeypatch.setattr(env_probe, "pty_available", lambda: False)

    env_probe.pytest_collection_modifyitems([pty_item])

    assert len(pty_item.marks) == 1
    assert "pty" in pty_item.marks[0].kwargs["reason"]


def test_hook_adds_no_marks_when_capabilities_are_present(monkeypatch):
    items = [_FakeItem("requires_ps"), _FakeItem("requires_pty")]
    monkeypatch.setattr(env_probe, "ps_available", lambda: True)
    monkeypatch.setattr(env_probe, "pty_available", lambda: True)

    env_probe.pytest_collection_modifyitems(items)

    assert all(item.marks == [] for item in items)
