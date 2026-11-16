"""The live ``default_headroom()`` port honours N63's paid-credits rule.

Review blocker (R4-2 round 2): after core/main brought N63, the live default
port was ``MostConstrainedHeadroom(CodexBarHeadroom(), SnapshotOrUnlimitedHeadroom())``.
It had no ``plan_status``, so the selector never saw a full plan, and CodexBar
read codex's full window as 0.0 even with credits left. With
``CONDUCTOR_DISPATCH_ALLOW_PAID_CREDITS=1`` (prod) codex never reviewed.

These tests drive the real ``default_headroom()`` and the real selector over a
snapshot written to ``tmp_path``. Both readers are pointed at that file by
monkeypatching their paths/config; nothing reads the home directory and
nothing reaches the network.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.server.services.autonomy.roles import RolePolicy
from src.tools.auto_dispatch.protocols import ModelRole
from src.tools.auto_dispatch.selection import (
    ALLOW_PAID_CREDITS_ENV,
    PLAN_EXHAUSTED,
    MostConstrainedHeadroom,
    QuotaAwareModelSelector,
)
from src.tools.auto_dispatch.types import Line

LINE = Line(id="l1", name="l1", task="t", write_scope=("a.py",))


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _snapshot(tmp_path: Path) -> tuple[Path, str]:
    """The prod-shaped snapshot of the probe: codex plan full on credits."""
    now = datetime.now(timezone.utc)
    observed = _iso(now - timedelta(minutes=1))
    reset = _iso(now + timedelta(days=4))
    payload = {
        "updatedAt": observed,
        "providers": [
            {
                "provider": "codex",
                "kind": "quota",
                "status": "ok",
                "updated_at": observed,
                "windows": [
                    {"role": "secondary", "used_percent": 100, "resets_at_utc": reset}
                ],
                "credits_remaining": 7541.0,
            },
            {
                "provider": "cursor",
                "kind": "quota",
                "status": "ok",
                "updated_at": observed,
                "windows": [
                    {"role": "primary", "used_percent": 5},
                    {"role": "tertiary", "used_percent": 100, "resets_at_utc": reset},
                ],
            },
            {
                "provider": "grok",
                "kind": "quota",
                "status": "ok",
                "updated_at": observed,
                "windows": [{"role": "primary", "used_percent": 10, "resets_at_utc": reset}],
            },
        ],
    }
    path = tmp_path / "codexbar-snapshot.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path, reset


def _wire(monkeypatch, path: Path, *, native_enabled: bool = True) -> None:
    import src.server.services.native_runtime.usage_headroom as usage_headroom
    import src.tools.auto_dispatch.headroom_codexbar as codexbar

    monkeypatch.setattr(codexbar, "DEFAULT_SNAPSHOT_PATH", path)
    config = SimpleNamespace(
        enabled=native_enabled,
        snapshot_path=str(path),
        max_age_seconds=900,
        max_bytes=262144,
        low_balance_thresholds={},
    )
    monkeypatch.setattr(
        usage_headroom, "get_native_runtime_usage_headroom_config", lambda: config
    )


def _review(port) -> tuple[str | None, dict]:
    async def run():
        selector = QuotaAwareModelSelector(port, policy=RolePolicy.default())
        blockers = await selector.review_blockers(LINE, [])
        pick = await selector.select(LINE, ["kimi"], role=ModelRole.REVIEW)
        return pick, blockers

    return asyncio.run(run())


@pytest.mark.parametrize("native_enabled", [True, False])
def test_paid_credits_on_lets_codex_review_through_the_live_default(
    tmp_path, monkeypatch, native_enabled
):
    from src.server.services.dispatch import default_headroom

    path, _ = _snapshot(tmp_path)
    _wire(monkeypatch, path, native_enabled=native_enabled)
    monkeypatch.setenv(ALLOW_PAID_CREDITS_ENV, "1")
    assert "codex" in RolePolicy.default().primary_reviewer

    pick, blockers = _review(default_headroom())

    assert pick == "codex"
    assert "codex" not in blockers


@pytest.mark.parametrize("native_enabled", [True, False])
def test_paid_credits_off_keeps_codex_off_review_as_plan_exhausted(
    tmp_path, monkeypatch, native_enabled
):
    """Flag off: codex is skipped because its plan is full, with the reset time.

    ``native_enabled=False`` leaves CodexBar the only reader: its own
    ``plan_status`` must still report the full plan, or codex (now low, not
    0.0) would review on credits with the flag off.
    """
    from src.server.services.dispatch import default_headroom

    path, reset = _snapshot(tmp_path)
    _wire(monkeypatch, path, native_enabled=native_enabled)
    # N73 S1: paid credits are on by default; "0" is the off switch.
    monkeypatch.setenv(ALLOW_PAID_CREDITS_ENV, "0")

    pick, blockers = _review(default_headroom())

    assert pick not in (None, "codex")
    assert blockers["codex"]["reason"] == PLAN_EXHAUSTED
    assert blockers["codex"]["resets_at"] is not None
    assert datetime.fromisoformat(
        blockers["codex"]["resets_at"].replace("Z", "+00:00")
    ) == datetime.fromisoformat(reset.replace("Z", "+00:00"))


def test_the_live_default_reads_a_full_bucket_and_credits_as_low(tmp_path, monkeypatch):
    from src.server.services.dispatch import default_headroom

    path, _ = _snapshot(tmp_path)
    _wire(monkeypatch, path)
    port = default_headroom()

    assert asyncio.run(port.remaining("codex")) == pytest.approx(0.25)
    assert asyncio.run(port.remaining("cursor")) == pytest.approx(0.25)
    assert asyncio.run(port.remaining("grok")) == pytest.approx(0.9)


class _Plan:
    def __init__(self, status=None, *, raises=False):
        self.status = status
        self.raises = raises

    async def remaining(self, model):
        return 1.0

    async def plan_status(self, model):
        if self.raises:
            raise RuntimeError("boom")
        return self.status


class _NoPlan:
    async def remaining(self, model):
        return 1.0


def test_most_constrained_merges_plan_status_so_a_later_full_plan_counts():
    """A first port with a not-full row must not hide a second port's full plan."""
    port = MostConstrainedHeadroom(
        _NoPlan(),
        _Plan({"plan_full": False, "credits": False, "resets_at": None}),
        _Plan(raises=True),
        _Plan({"plan_full": True, "credits": True, "resets_at": "2026-11-15T19:45:48Z"}),
        _Plan({"plan_full": True, "credits": True, "resets_at": "2026-11-15T19:45:48.5Z"}),
    )

    assert asyncio.run(port.plan_status("codex")) == {
        "plan_full": True,
        "credits": True,
        "resets_at": "2026-11-15T19:45:48.5Z",
    }


def test_most_constrained_credits_need_every_full_reader_to_see_them():
    port = MostConstrainedHeadroom(
        _Plan({"plan_full": True, "credits": True, "resets_at": None}),
        _Plan({"plan_full": True, "credits": False, "resets_at": None}),
    )

    assert asyncio.run(port.plan_status("codex"))["credits"] is False


def test_most_constrained_plan_status_is_none_without_any_reading():
    port = MostConstrainedHeadroom(_NoPlan(), _Plan(None), _Plan(raises=True))

    assert asyncio.run(port.plan_status("codex")) is None


def test_n73_paid_credits_are_on_by_default(monkeypatch):
    from src.tools.auto_dispatch.selection import paid_credits_allowed

    monkeypatch.delenv(ALLOW_PAID_CREDITS_ENV, raising=False)
    assert paid_credits_allowed() is True
    for off in ("0", "false", "no", "OFF"):
        monkeypatch.setenv(ALLOW_PAID_CREDITS_ENV, off)
        assert paid_credits_allowed() is False
    monkeypatch.setenv(ALLOW_PAID_CREDITS_ENV, "1")
    assert paid_credits_allowed() is True
