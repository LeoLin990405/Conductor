"""R6 L4: role, session and step on dispatch events."""

from __future__ import annotations

from src.tools.auto_dispatch import event_tags


def test_tags_only_when_enabled_or_forced() -> None:
    event: dict = {"kind": "x"}
    assert event_tags.tag(event, role="worker", session="r1", env={}) == {"kind": "x"}
    snap: dict = {}
    tagged = event_tags.tag(
        {"kind": "x"}, role="worker", session="r1", snapshot=snap, env={event_tags.ENV: "1"}
    )
    assert tagged == {"kind": "x", "role": "worker", "session": "r1", "step": 1}
    again = event_tags.tag({}, role="reviewer", session="r2", snapshot=snap, force=True)
    assert again["step"] == 2 and again["role"] == "reviewer"


def test_unknown_role_is_system_and_existing_keys_win() -> None:
    tagged = event_tags.tag({"role": "planner"}, role="bogus", session=None, force=True)
    assert tagged["role"] == "planner" and tagged["session"] == ""
    assert event_tags.tags("bogus", None, 0)["role"] == "system"


def test_remember_event_tags_stage_gate_events(monkeypatch) -> None:
    from src.tools.auto_dispatch.machine import _remember_event

    class _Event:
        def to_dict(self):
            return {"kind": "review_blocked"}

    class _Transition:
        event = _Event()

    monkeypatch.setenv(event_tags.ENV, "on")
    snap = {"machine": {"stage": "primary_review", "run_id": "run-7"}}
    _remember_event(snap, _Transition())
    assert snap["events"][-1] == {
        "kind": "review_blocked",
        "role": "reviewer",
        "session": "run-7",
        "step": 1,
    }
    monkeypatch.delenv(event_tags.ENV)
    _remember_event(snap, _Transition())
    assert snap["events"][-1] == {"kind": "review_blocked"}
