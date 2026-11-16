"""Lane selection: same order as messaging.py, flag gates the P lane."""

import pytest

from src.server.services.superagent_runtime import selection
from src.server.services.superagent_runtime.selection import (
    SuperagentLane,
    superagent_lane_for,
    superagent_role_for,
)


@pytest.mark.parametrize(
    ("platform", "route_role", "is_dispatch", "expected"),
    [
        ("superagent_watchdog", "generalist", True, "watchdog"),
        ("superagent_watchdog", "generalist", False, None),
        ("superagent_router", "supervisor", True, "supervisor"),
        ("superagent_worker", "worker", True, None),
        ("superagent_worker", "generalist", True, None),
        (None, "generalist", False, None),
        ("web", "generalist", False, None),
        # Watchdog wins first, as in messaging.py's host branch.
        ("superagent_watchdog", "supervisor", True, "watchdog"),
    ],
)
def test_role_mapping(platform, route_role, is_dispatch, expected):
    assert (
        superagent_role_for(
            platform=platform, route_role=route_role, is_dispatch=is_dispatch
        )
        == expected
    )


@pytest.fixture
def flags(monkeypatch):
    values = {"supervisor": "ptc", "watchdog": "ptc"}
    calls = []

    def fake(role):
        calls.append(role)
        return values.get(role, "ptc")

    monkeypatch.setattr("src.config.settings.get_superagent_runtime", fake)
    return values, calls


def _lane(platform, route_role, is_dispatch=True, tools=("t1", "t2")):
    return superagent_lane_for(
        platform=platform,
        route_role=route_role,
        is_dispatch=is_dispatch,
        host="host",
        tools=list(tools) if tools is not None else None,
    )


def test_flags_off_no_lane(flags):
    assert _lane("superagent_watchdog", "generalist") is None
    assert _lane("superagent_router", "supervisor") is None


def test_watchdog_on(flags):
    values, _ = flags
    values["watchdog"] = "p"
    lane = _lane("superagent_watchdog", "generalist")
    assert lane == SuperagentLane(role="watchdog", host="host", tools=("t1", "t2"))
    assert isinstance(lane.tools, tuple)
    assert _lane("superagent_router", "supervisor") is None


def test_supervisor_on(flags):
    values, _ = flags
    values["supervisor"] = "p"
    lane = _lane("superagent_router", "supervisor")
    assert lane == SuperagentLane(role="supervisor", host="host", tools=("t1", "t2"))
    assert _lane("superagent_watchdog", "generalist") is None


def test_workers_and_user_turns_untouched(flags):
    values, calls = flags
    values.update(supervisor="p", watchdog="p")
    assert _lane("superagent_worker", "worker") is None
    assert _lane(None, "generalist", is_dispatch=False) is None
    assert _lane("superagent_watchdog", "generalist", is_dispatch=False) is None
    # No role means no flag read at all.
    assert calls == []


def test_none_tools_becomes_empty_tuple(flags):
    values, _ = flags
    values["watchdog"] = "p"
    assert _lane("superagent_watchdog", "generalist", tools=None).tools == ()


def test_lane_is_frozen():
    lane = SuperagentLane(role="watchdog", host=None, tools=())
    with pytest.raises(AttributeError):
        lane.role = "supervisor"  # type: ignore[misc]


def test_module_import_is_light():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(selection))
    top_imports = [
        node
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    names = [
        getattr(node, "module", None) or node.names[0].name for node in top_imports
    ]
    assert not any(name and name.startswith("src.") for name in names)
