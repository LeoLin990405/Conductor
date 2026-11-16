from src.server.services.judgment import (
    COMPLETION_EVIDENCE,
    ROUTER_PRESELECT,
    USES,
    WATCHDOG_PROGRESS,
    Fallback,
    get_use,
    in_sample,
    use_mode,
    use_sample,
)

import pytest


def test_registry_has_three_shadow_uses_with_their_fallbacks():
    assert set(USES) == {WATCHDOG_PROGRESS, COMPLETION_EVIDENCE, ROUTER_PRESELECT}
    assert get_use(WATCHDOG_PROGRESS).fallback is Fallback.ESCALATE
    assert get_use(COMPLETION_EVIDENCE).fallback is Fallback.KEEP_PATH
    assert get_use(ROUTER_PRESELECT).fallback is Fallback.KEEP_PATH
    for spec in USES.values():
        assert (spec.lo, spec.hi, spec.margin, spec.version) == (0.2, 0.8, 0.15, "1")
        assert spec.default_mode == "shadow"


def test_unknown_use_raises_key_error():
    with pytest.raises(KeyError):
        get_use("nope")


@pytest.mark.parametrize(
    ("value", "mode"),
    [(None, "shadow"), ("", "shadow"), ("off", "off"), (" ENFORCE ", "enforce"), ("bogus", "shadow")],
)
def test_use_mode_reads_the_per_use_env(value, mode):
    env = {} if value is None else {"CONDUCTOR_JUDGMENT_WATCHDOG_PROGRESS_MODE": value}
    assert use_mode(WATCHDOG_PROGRESS, env) == mode


def test_use_mode_reads_os_environ_by_default(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_JUDGMENT_ROUTER_PRESELECT_MODE", "off")
    assert use_mode(ROUTER_PRESELECT) == "off"


@pytest.mark.parametrize(
    ("value", "rate"),
    [(None, 0.1), ("0.25", 0.25), ("2", 1.0), ("-1", 0.0), ("x", 0.1), ("nan", 0.1)],
)
def test_use_sample_is_clamped(value, rate):
    env = {} if value is None else {"CONDUCTOR_JUDGMENT_ROUTER_PRESELECT_SAMPLE": value}
    assert use_sample(ROUTER_PRESELECT, env) == rate


def test_in_sample_is_deterministic_and_tracks_the_rate():
    env = {"CONDUCTOR_JUDGMENT_ROUTER_PRESELECT_SAMPLE": "0.3"}
    picks = [in_sample(ROUTER_PRESELECT, f"t{i}", env) for i in range(2000)]
    assert picks == [in_sample(ROUTER_PRESELECT, f"t{i}", env) for i in range(2000)]
    assert 0.25 < sum(picks) / len(picks) < 0.35
    one = {"CONDUCTOR_JUDGMENT_ROUTER_PRESELECT_SAMPLE": "1"}
    assert all(in_sample(ROUTER_PRESELECT, f"t{i}", one) for i in range(50))
    default = [in_sample(ROUTER_PRESELECT, f"t{i}", {}) for i in range(2000)]
    assert 0.07 < sum(default) / len(default) < 0.13  # v1 default rate 0.1
    zero = {"CONDUCTOR_JUDGMENT_ROUTER_PRESELECT_SAMPLE": "0"}
    assert not any(in_sample(ROUTER_PRESELECT, f"t{i}", zero) for i in range(50))


@pytest.mark.parametrize(
    ("global_value", "per_use", "mode"),
    [
        ("off", None, "off"),
        (" OFF ", "enforce", "off"),
        ("off", "shadow", "off"),
        ("enforce", None, "shadow"),  # only off is meaningful globally
        ("shadow", "enforce", "enforce"),
        ("bogus", "off", "off"),
        ("", "enforce", "enforce"),
    ],
)
def test_global_mode_off_overrides_every_use(global_value, per_use, mode):
    env = {"CONDUCTOR_JUDGMENT_MODE": global_value}
    if per_use is not None:
        env["CONDUCTOR_JUDGMENT_WATCHDOG_PROGRESS_MODE"] = per_use
    assert use_mode(WATCHDOG_PROGRESS, env) == mode
    if global_value.strip().lower() == "off":
        assert use_mode(ROUTER_PRESELECT, env) == "off"
