"""N73 Q8: shadow 30 lines -> 50% sample until 40 adopted -> full, with auto-rollback."""

from __future__ import annotations

from types import SimpleNamespace

from src.tools.auto_dispatch.superagent_assign.rollout import (
    ADOPTED_LINES,
    SHADOW_LINES,
    rollout_state,
)
from src.tools.auto_dispatch.types import Outcome

AUTO = {"CONDUCTOR_DISPATCH_ASSIGN_MODE": "auto"}


def _dispatch(index, entries, outcomes=None):
    lines = {}
    for n, (mode, source) in enumerate(entries):
        lines[f"l{n}"] = {
            "assignment": {
                "r": {"version": 1, "run_id": "r", "seq": 1, "mode": mode, "source": source,
                      "choice": {}, "selector_choice": {}, "feasibility": [], "reask": False}
            }
        }
    return SimpleNamespace(id=f"d{index}", gates={"lines": lines}, outcomes=outcomes or {})


def test_explicit_mode_overrides_the_stages() -> None:
    state = rollout_state([], environ={"CONDUCTOR_DISPATCH_ASSIGN_MODE": "off"})
    assert state.stage == "override" and state.mode == "off"


def test_unset_means_shadow_and_never_starts_the_rollout() -> None:
    shadow = [_dispatch(0, [("shadow", "superagent")] * SHADOW_LINES)]
    adopted = shadow + [_dispatch(1, [("enforce", "superagent")] * ADOPTED_LINES)]
    for history in ([], shadow, adopted):
        state = rollout_state(history, environ={})
        assert (state.stage, state.mode) == ("override", "shadow")


def test_stage_a_then_b_then_c() -> None:
    assert rollout_state([], environ=AUTO).mode == "shadow"
    shadow = [_dispatch(0, [("shadow", "superagent")] * SHADOW_LINES)]
    b = rollout_state(shadow, environ=AUTO)
    assert (b.stage, b.mode, b.sample) == ("sampled", "enforce", 0.5)
    adopted = shadow + [_dispatch(1, [("enforce", "superagent")] * ADOPTED_LINES)]
    c = rollout_state(adopted, environ=AUTO)
    assert (c.stage, c.mode, c.sample) == ("full", "enforce", 1.0)


def test_auto_rollback_on_a_pass_rate_drop() -> None:
    n = ADOPTED_LINES
    entries = [("enforce", "superagent")] * n + [("enforce", "selector")] * 20
    outcomes = {}
    for i in range(n):
        outcomes[f"l{i}"] = Outcome(f"l{i}", "kimi", 10, 0, i % 2 == 0)  # 50%
    for i in range(n, n + 20):
        outcomes[f"l{i}"] = Outcome(f"l{i}", "glm", 10, 0, True)  # 100%
    dispatches = [
        _dispatch(0, [("shadow", "superagent")] * SHADOW_LINES),
        _dispatch(1, entries, outcomes),
    ]
    state = rollout_state(dispatches, environ=AUTO)
    assert state.rolled_back and state.mode == "shadow"
    assert "pass_rate_drop" in state.reasons


# --- review fix 2: persisted, forward-only stage; sticky rollback --------------

import pytest  # noqa: E402

from src.tools.auto_dispatch.superagent_assign.rollout import (  # noqa: E402
    InMemoryRolloutStore,
    RolloutState,
    advance,
)


def _full_history():
    return [
        _dispatch(0, [("shadow", "superagent")] * SHADOW_LINES),
        _dispatch(1, [("enforce", "superagent")] * ADOPTED_LINES),
    ]


@pytest.mark.asyncio
async def test_a_sliding_window_never_moves_the_stage_back() -> None:
    store = InMemoryRolloutStore()
    assert (await advance(_full_history(), store, environ=AUTO)).stage == "full"
    # Fails on 3025c108e: the old shadow lines left the window, so the
    # window-only rule fell back to shadow.
    later = await advance([_dispatch(2, [("enforce", "superagent")] * 5)], store, environ=AUTO)
    assert (later.stage, later.mode, later.sample) == ("full", "enforce", 1.0)
    sampled = InMemoryRolloutStore(RolloutState("sampled", "enforce", 0.5))
    assert (await advance([], sampled, environ=AUTO)).stage == "sampled"


@pytest.mark.asyncio
async def test_a_rollback_is_sticky() -> None:
    store = InMemoryRolloutStore(
        RolloutState("shadow", "shadow", 1.0, rolled_back=True, reasons=("pass_rate_drop",))
    )
    # Even a history that would count as full stays rolled back.
    state = await advance(_full_history(), store, environ=AUTO)
    assert state.rolled_back and state.mode == "shadow"
    assert store.state.rolled_back


def test_rollback_is_checked_in_the_sampled_stage() -> None:
    entries = [("enforce", "superagent")] * 20 + [("enforce", "selector")] * 20
    outcomes = {}
    for i in range(20):
        outcomes[f"l{i}"] = Outcome(f"l{i}", "kimi", 10, 0, i % 4 == 0)  # 25%
    for i in range(20, 40):
        outcomes[f"l{i}"] = Outcome(f"l{i}", "glm", 10, 0, True)
    history = [
        _dispatch(0, [("shadow", "superagent")] * SHADOW_LINES),
        _dispatch(1, entries, outcomes),
    ]
    # 20 adopted < 40: the sampled stage, and the drop still rolls it back.
    state = rollout_state(history, environ=AUTO)
    assert state.rolled_back and state.mode == "shadow"
