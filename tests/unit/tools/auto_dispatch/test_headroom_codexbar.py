"""The CodexBar headroom adapter tells the selector what is left.

``src/tools/auto_dispatch/headroom_codexbar.py`` reads Conductor's CodexBar
snapshot and answers the ``HeadroomPort`` question: how much of a Worker's
quota is left.

Two properties matter more than the arithmetic. A Worker whose usage nobody
read is **not** a Worker without quota — every unreadable case answers 1.0 —
and the *smallest* remaining window wins, so a spent 5-hour window counts even
when the weekly window is wide.

The snapshot used here is a fixture, not a live file: no CodexBar CLI is
called and nothing reaches the network.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.server.services.autonomy.roles import RolePolicy
from src.tools.auto_dispatch.headroom_codexbar import (
    LOW_HEADROOM,
    PROVIDER_BY_WORKER,
    CodexBarHeadroom,
)
from src.tools.auto_dispatch.selection import QuotaAwareModelSelector
from src.tools.auto_dispatch.types import Line

FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "codexbar_snapshot_v1.json"
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
ACCOUNT_FIELD = "not-a-real-account@example.invalid"
AUDIT_LINE = Line(id="audit", name="audit", task="read")


def _snapshot(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "codexbar-snapshot.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _with_zai_used(fixture_payload: dict, used_percent: float) -> dict:
    """The fixture with GLM's provider row at another used percentage."""
    payload = json.loads(json.dumps(fixture_payload))
    for row in payload["providers"]:
        if row["provider"] == "zai":
            row["windows"] = [{"used_percent": used_percent}]
    return payload


def _with_observation_times(
    fixture_payload: dict, *, file_time: str, row_time: str
) -> dict:
    """The fixture with the file's time and every row's observation time set.

    The two are separate clocks: the file says when it was written, the row
    says when that provider's quota was actually seen.
    """
    payload = json.loads(json.dumps(fixture_payload))
    payload["updatedAt"] = file_time
    for row in payload["providers"]:
        row["updated_at"] = row_time
    return payload


@pytest.fixture
def fixture_payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_the_worker_provider_map_covers_every_worker() -> None:
    for worker in (
        "kimi",
        "glm",
        "grok",
        "cursor",
        "stepfun",
        "deepseek",
        "minimax",
        "doubao",
        "codex",
        "claude",
    ):
        assert worker in PROVIDER_BY_WORKER


def test_glm_is_read_from_the_zai_provider() -> None:
    assert PROVIDER_BY_WORKER["glm"] == "zai"


def test_doubao_is_read_from_the_doubao_provider() -> None:
    assert PROVIDER_BY_WORKER["doubao"] == "doubao"


@pytest.mark.asyncio
async def test_a_full_quota_reads_as_1(tmp_path, fixture_payload) -> None:
    path = _snapshot(tmp_path, fixture_payload)
    headroom = CodexBarHeadroom(path, now=NOW)
    assert await headroom.remaining("kimi") == 1.0


@pytest.mark.asyncio
async def test_a_partly_used_quota_reads_as_the_remainder(tmp_path, fixture_payload) -> None:
    path = _snapshot(tmp_path, fixture_payload)
    headroom = CodexBarHeadroom(path, now=NOW)
    assert await headroom.remaining("glm") == pytest.approx(0.55)


@pytest.mark.asyncio
async def test_the_spent_window_wins_over_the_wide_one(tmp_path, fixture_payload) -> None:
    """A 5-hour window that is spent leaves no headroom, whatever the week says."""
    path = _snapshot(tmp_path, fixture_payload)
    headroom = CodexBarHeadroom(path, now=NOW)
    assert await headroom.remaining("grok") == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_a_provider_without_windows_reads_as_full(tmp_path, fixture_payload) -> None:
    path = _snapshot(tmp_path, fixture_payload)
    headroom = CodexBarHeadroom(path, now=NOW)
    assert await headroom.remaining("cursor") == 1.0


@pytest.mark.asyncio
async def test_a_provider_that_is_not_read_reads_as_full(tmp_path, fixture_payload) -> None:
    path = _snapshot(tmp_path, fixture_payload)
    headroom = CodexBarHeadroom(path, now=NOW)
    assert await headroom.remaining("deepseek") == 1.0


@pytest.mark.asyncio
async def test_a_worker_that_is_absent_from_the_snapshot_reads_as_full(tmp_path, fixture_payload) -> None:
    path = _snapshot(tmp_path, fixture_payload)
    headroom = CodexBarHeadroom(path, now=NOW)
    assert await headroom.remaining("minimax") == 1.0


@pytest.mark.asyncio
async def test_a_missing_doubao_row_reads_as_full(tmp_path, fixture_payload) -> None:
    assert all(row["provider"] != "doubao" for row in fixture_payload["providers"])
    path = _snapshot(tmp_path, fixture_payload)
    headroom = CodexBarHeadroom(path, now=NOW)
    assert await headroom.remaining("doubao") == 1.0


@pytest.mark.asyncio
async def test_an_unknown_model_reads_as_full(tmp_path, fixture_payload) -> None:
    path = _snapshot(tmp_path, fixture_payload)
    headroom = CodexBarHeadroom(path, now=NOW)
    assert await headroom.remaining("not-a-worker") == 1.0


@pytest.mark.asyncio
async def test_a_missing_snapshot_reads_as_full(tmp_path) -> None:
    headroom = CodexBarHeadroom(tmp_path / "absent.json", now=NOW)
    assert await headroom.remaining("kimi") == 1.0


@pytest.mark.asyncio
async def test_an_unreadable_snapshot_reads_as_full(tmp_path) -> None:
    path = tmp_path / "codexbar-snapshot.json"
    path.write_text("this is not json", encoding="utf-8")
    headroom = CodexBarHeadroom(path, now=NOW)
    assert await headroom.remaining("kimi") == 1.0


@pytest.mark.asyncio
async def test_a_stale_snapshot_reads_as_full(tmp_path, fixture_payload) -> None:
    path = _snapshot(tmp_path, fixture_payload)
    later = NOW + timedelta(minutes=31)
    headroom = CodexBarHeadroom(path, now=later)
    assert await headroom.remaining("glm") == 1.0


@pytest.mark.asyncio
async def test_a_fresh_snapshot_is_trusted(tmp_path, fixture_payload) -> None:
    path = _snapshot(tmp_path, fixture_payload)
    later = NOW + timedelta(minutes=29)
    headroom = CodexBarHeadroom(path, now=later)
    assert await headroom.remaining("glm") == pytest.approx(0.55)


@pytest.mark.asyncio
async def test_a_snapshot_dated_in_the_future_reads_as_full(tmp_path, fixture_payload) -> None:
    path = _snapshot(tmp_path, fixture_payload)
    headroom = CodexBarHeadroom(path, now=NOW - timedelta(hours=1))
    assert await headroom.remaining("glm") == 1.0


@pytest.mark.asyncio
async def test_a_row_observed_long_ago_reads_as_full(tmp_path, fixture_payload) -> None:
    """A file written now can still carry a reading that is hours old.

    CodexBar writes the snapshot on its own refresh interval, so the file's
    timestamp and the provider's observation time are two different clocks.
    Only the observation time says whether the numbers are still worth acting
    on, so a row that was seen hours ago is no reading at all.
    """
    payload = _with_observation_times(
        fixture_payload,
        file_time="2026-11-06T12:00:00Z",
        row_time="2026-11-06T10:00:00Z",
    )
    path = _snapshot(tmp_path, payload)
    headroom = CodexBarHeadroom(path, now=NOW)
    assert await headroom.remaining("glm") == 1.0
    assert await headroom.remaining("grok") == 1.0


@pytest.mark.asyncio
async def test_a_row_observed_recently_is_trusted(tmp_path, fixture_payload) -> None:
    """The same file, with rows that were seen five minutes ago, is trusted."""
    payload = _with_observation_times(
        fixture_payload,
        file_time="2026-11-06T12:00:00Z",
        row_time="2026-11-06T11:55:00Z",
    )
    path = _snapshot(tmp_path, payload)
    headroom = CodexBarHeadroom(path, now=NOW)
    assert await headroom.remaining("glm") == pytest.approx(0.55)
    assert await headroom.remaining("grok") == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_a_stale_exhausted_provider_stays_eligible(tmp_path) -> None:
    """The audit's scenario: a fresh file that carries a stale exhausted row.

    The root timestamp says the file was just written; the zai row says GLM's
    quota was last seen two hours ago. That row is not a reading, so GLM has
    no usable window and answers 1.0 -- and stays on the roster, because a
    provider nobody read is not a provider that is out of quota. The fresh
    exhausted peer is skipped, because its reading is current.
    """
    payload = {
        "updatedAt": "2026-11-06T12:00:00Z",
        "providers": [
            {
                "provider": "zai",
                "kind": "quota",
                "status": "ok",
                "updated_at": "2026-11-06T10:00:00Z",
                "windows": [
                    {"used_percent": 100.0, "resets_at_utc": "2026-11-06T11:00:00Z"}
                ],
            },
            {
                "provider": "kimi",
                "kind": "quota",
                "status": "ok",
                "updated_at": "2026-11-06T11:59:00Z",
                "windows": [
                    {"used_percent": 100.0, "resets_at_utc": "2026-11-06T17:00:00Z"}
                ],
            },
        ],
    }
    path = _snapshot(tmp_path, payload)
    headroom = CodexBarHeadroom(path, now=NOW)

    assert await headroom.remaining("glm") == 1.0
    assert await headroom.remaining("kimi") == 0.0

    # The peer comes first in the roster and is skipped, so the only way the
    # selector can answer "glm" is that the stale row left GLM eligible. If the
    # adapter trusted the stale row the pool would be empty, and if it trusted
    # the fresh exhausted peer the answer would be "kimi".
    selector = QuotaAwareModelSelector(
        headroom=headroom, policy=RolePolicy(developer_pool=("kimi", "glm"), work_tail=())
    )
    assert await selector.select(AUDIT_LINE, []) == "glm"


@pytest.mark.asyncio
async def test_a_row_with_a_malformed_observation_reads_as_full(
    tmp_path, fixture_payload
) -> None:
    """A row whose observation time cannot be read is not a reading."""
    payload = _with_observation_times(
        fixture_payload,
        file_time="2026-11-06T12:00:00Z",
        row_time="not a timestamp",
    )
    path = _snapshot(tmp_path, payload)
    headroom = CodexBarHeadroom(path, now=NOW)
    assert await headroom.remaining("glm") == 1.0


@pytest.mark.asyncio
async def test_a_row_dated_in_the_future_reads_as_full(tmp_path, fixture_payload) -> None:
    """A row observed in the future is not a reading either."""
    payload = _with_observation_times(
        fixture_payload,
        file_time="2026-11-06T12:00:00Z",
        row_time="2026-11-06T14:00:00Z",
    )
    path = _snapshot(tmp_path, payload)
    headroom = CodexBarHeadroom(path, now=NOW)
    assert await headroom.remaining("glm") == 1.0


@pytest.mark.asyncio
async def test_a_row_without_an_observation_time_reads_as_full(tmp_path, fixture_payload) -> None:
    """A row that does not say when it was seen is not a reading."""
    payload = json.loads(json.dumps(fixture_payload))
    for row in payload["providers"]:
        row.pop("updated_at", None)
    path = _snapshot(tmp_path, payload)
    headroom = CodexBarHeadroom(path, now=NOW)
    assert await headroom.remaining("glm") == 1.0


def _provider_row(provider: str, observed_at: str, used_percent: float) -> dict:
    """One provider row in the shape ``codexbar_snapshot`` writes."""
    return {
        "provider": provider,
        "kind": "quota",
        "status": "ok",
        "updated_at": observed_at,
        "windows": [
            {"used_percent": used_percent, "resets_at_utc": "2026-11-06T17:00:00Z"}
        ],
    }


def _stamp(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


@pytest.mark.asyncio
@pytest.mark.parametrize("offset_seconds", [1, 30, 60, 61])
async def test_a_row_a_second_in_the_future_reads_as_full(
    tmp_path, offset_seconds
) -> None:
    """A row dated even one second ahead is unknown data, not fresh data.

    The native-agent routing allows a minute of clock skew; this adapter does
    not, so the boundary is at zero: an observation later than now is skipped
    however close it is.
    """
    payload = {
        "updatedAt": _stamp(NOW),
        "providers": [
            _provider_row("zai", _stamp(NOW + timedelta(seconds=offset_seconds)), 100.0),
            _provider_row("kimi", _stamp(NOW), 100.0),
        ],
    }
    path = _snapshot(tmp_path, payload)
    headroom = CodexBarHeadroom(path, now=NOW)
    assert await headroom.remaining("glm") == 1.0

    # The healthy row beside it is unaffected: it reads its own number, and
    # the future-dated row does not bleed into it.
    assert await headroom.remaining("kimi") == 0.0

    # The stale row left GLM eligible, so the selector can still pick it.
    selector = QuotaAwareModelSelector(
        headroom=CodexBarHeadroom(path, now=NOW),
        policy=RolePolicy(developer_pool=("glm",), work_tail=()),
    )
    assert await selector.select(AUDIT_LINE, []) == "glm"


@pytest.mark.asyncio
async def test_a_row_observed_exactly_now_is_still_read(tmp_path) -> None:
    """Exactly now is not the future: the row is current and is read."""
    payload = {
        "updatedAt": _stamp(NOW),
        "providers": [_provider_row("zai", _stamp(NOW), 100.0)],
    }
    path = _snapshot(tmp_path, payload)
    assert await CodexBarHeadroom(path, now=NOW).remaining("glm") == 0.0

    # The row is current and it says the quota is spent, so it is skipped.
    selector = QuotaAwareModelSelector(
        headroom=CodexBarHeadroom(path, now=NOW),
        policy=RolePolicy(developer_pool=("glm",), work_tail=()),
    )
    assert await selector.select(AUDIT_LINE, []) is None


@pytest.mark.asyncio
async def test_an_old_root_does_not_veto_a_fresh_row(tmp_path) -> None:
    """The root timestamp never decides freshness.

    The producer fixes ``updatedAt`` when collection starts and reads the
    providers afterwards, so a root can already be older than the window while
    the row next to it is still fresh. Judging the root would throw that row
    away and report an exhausted provider as unknown.
    """
    payload = {
        "updatedAt": _stamp(NOW - timedelta(minutes=31)),
        "providers": [_provider_row("zai", _stamp(NOW - timedelta(minutes=26)), 100.0)],
    }
    path = _snapshot(tmp_path, payload)
    headroom = CodexBarHeadroom(path, now=NOW)
    assert await headroom.remaining("glm") == 0.0

    # The row is current and it says the quota is spent, so it is skipped.
    selector = QuotaAwareModelSelector(
        headroom=CodexBarHeadroom(path, now=NOW),
        policy=RolePolicy(developer_pool=("glm",), work_tail=()),
    )
    assert await selector.select(AUDIT_LINE, []) is None


@pytest.mark.asyncio
async def test_a_fresh_root_does_not_rescue_an_old_row(tmp_path) -> None:
    """The mirror of the case above: a new root does not make an old row fresh."""
    payload = {
        "updatedAt": _stamp(NOW),
        "providers": [_provider_row("zai", _stamp(NOW - timedelta(minutes=31)), 100.0)],
    }
    path = _snapshot(tmp_path, payload)
    assert await CodexBarHeadroom(path, now=NOW).remaining("glm") == 1.0

    # Unknown data is not exhaustion: the row is not a reading, so the
    # provider stays on the roster and still gets the work.
    selector = QuotaAwareModelSelector(
        headroom=CodexBarHeadroom(path, now=NOW),
        policy=RolePolicy(developer_pool=("glm",), work_tail=()),
    )
    assert await selector.select(AUDIT_LINE, []) == "glm"


@pytest.mark.asyncio
async def test_a_row_without_updated_at_falls_back_to_observed_at_utc(tmp_path) -> None:
    """A row that spells its observation time the other way is still read.

    The native-agent routing reads ``updated_at`` and falls back to
    ``observed_at_utc`` when the row does not carry the first one. The
    adapter reads the same file with the same fallback, so a snapshot in that
    shape reads the same here as it does there.
    """
    payload = {
        "updatedAt": _stamp(NOW),
        "providers": [
            {
                "provider": "zai",
                "kind": "quota",
                "status": "ok",
                "observed_at_utc": _stamp(NOW),
                "windows": [{"used_percent": 75.0, "resets_at_utc": "2026-11-06T17:00:00Z"}],
            }
        ],
    }
    path = _snapshot(tmp_path, payload)
    assert await CodexBarHeadroom(path, now=NOW).remaining("glm") == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_an_unusable_clock_reads_as_full(tmp_path, fixture_payload) -> None:
    """A clock that cannot even be normalised is not something to argue about.

    The adapter answers 1.0 for every provider rather than raising into the
    selector: no reading is no quota, and a caller with a broken clock still
    gets an answer.
    """
    broken = datetime.min.replace(tzinfo=timezone(timedelta(hours=8)))
    path = _snapshot(tmp_path, fixture_payload)
    headroom = CodexBarHeadroom(path, now=broken)
    assert await headroom.remaining("glm") == 1.0

    selector = QuotaAwareModelSelector(
        headroom=CodexBarHeadroom(path, now=broken),
        policy=RolePolicy(developer_pool=("glm",), work_tail=()),
    )
    assert await selector.select(AUDIT_LINE, []) == "glm"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damaged",
    ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"],
)
async def test_a_damaged_timestamp_is_unknown_data_not_a_crash(
    tmp_path, damaged
) -> None:
    """A timestamp at the edge of the calendar cannot be normalised to UTC.

    The arithmetic raises rather than returning an instant. That is a damaged
    timestamp: the row is unknown data, and it must not interrupt the caller.
    """
    payload = {
        "updatedAt": _stamp(NOW),
        "providers": [
            _provider_row("zai", damaged, 100.0),
            _provider_row("kimi", _stamp(NOW), 40.0),
        ],
    }
    path = _snapshot(tmp_path, payload)
    headroom = CodexBarHeadroom(path, now=NOW)

    assert await headroom.remaining("glm") == 1.0
    # The fresh peer is unaffected and reads its own number. The control
    # matters: a peer that used nothing would read 1.0 either way -- read or
    # failed open -- and would prove nothing about the damaged row's blast
    # radius.
    assert await headroom.remaining("kimi") == pytest.approx(0.6)

    # The selector must not raise either. Which of the two it picks is not
    # part of this contract -- only that it answers.
    mixed = QuotaAwareModelSelector(
        headroom=CodexBarHeadroom(path, now=NOW),
        policy=RolePolicy(developer_pool=("glm", "kimi"), work_tail=()),
    )
    assert await mixed.select(AUDIT_LINE, []) in ("glm", "kimi")

    # And with only the healthy peer on the roster, that peer is chosen.
    only_peer = QuotaAwareModelSelector(
        headroom=CodexBarHeadroom(path, now=NOW),
        policy=RolePolicy(developer_pool=("kimi",), work_tail=()),
    )
    assert await only_peer.select(AUDIT_LINE, []) == "kimi"


@pytest.mark.asyncio
async def test_the_snapshot_the_writer_produces_is_read(tmp_path, monkeypatch) -> None:
    """The adapter reads the shape Conductor's own writer emits.

    The fixture above is hand-written; the file that actually reaches the
    adapter is written by ``conductor_cli.codexbar_snapshot``. This drives that
    writer with a synthetic CLI -- no CodexBar CLI, no network -- and reads
    its output back through the adapter.
    """
    from src.conductor_cli import codexbar_snapshot as writer

    monkeypatch.setattr(writer, "DEFAULT_DEEPSEEK_ENV", tmp_path / "absent-env")
    monkeypatch.setattr(writer, "_codexbar_version", lambda codexbar, timeout: None)
    observed = (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )
    # A reset still ahead: a window whose reset has passed no longer limits.
    resets_at = (
        (datetime.now(timezone.utc) + timedelta(hours=5))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    used = {
        "kimi": 0,
        "zai": 45,
        "grok": 100,
        "cursor": 80,
        "stepfun": 12,
        "minimax": 70,
        "codex": 20,
        "claude": 40,
    }

    def fake_run(command, **_kwargs):
        provider = command[3]
        percent = used.get(provider)
        if percent is None:
            stdout = json.dumps([{"provider": provider, "error": {"message": "no data"}}])
        else:
            stdout = json.dumps(
                [
                    {
                        "provider": provider,
                        "usage": {
                            "updatedAt": observed,
                            "primary": {
                                "usedPercent": percent,
                                "resetsAt": resets_at,
                            },
                        },
                    }
                ]
            )
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(writer.subprocess, "run", fake_run)
    payload = writer.collect_snapshot(codexbar="/safe/fake-cli", timeout=5)
    path = tmp_path / "codexbar-snapshot.json"
    writer.write_snapshot(payload, path)

    headroom = CodexBarHeadroom(path)
    # Every Worker, not a sample: the writer emits a row per provider and the
    # adapter has to read back all of them.
    assert await headroom.remaining("kimi") == pytest.approx(1.0)
    assert await headroom.remaining("glm") == pytest.approx(0.55)
    assert await headroom.remaining("grok") == pytest.approx(0.0)
    assert await headroom.remaining("cursor") == pytest.approx(0.20)
    assert await headroom.remaining("stepfun") == pytest.approx(0.88)
    assert await headroom.remaining("deepseek") == pytest.approx(1.0)
    assert await headroom.remaining("minimax") == pytest.approx(0.30)
    assert await headroom.remaining("codex") == pytest.approx(0.80)
    assert await headroom.remaining("claude") == pytest.approx(0.60)


@pytest.mark.asyncio
async def test_nothing_credential_shaped_is_logged(tmp_path, fixture_payload, caplog) -> None:
    """An account e-mail in the snapshot must not reach a log line."""
    path = _snapshot(tmp_path, fixture_payload)
    headroom = CodexBarHeadroom(path, now=NOW)
    with caplog.at_level("DEBUG"):
        for model in ("kimi", "glm", "grok", "cursor", "deepseek", "minimax"):
            await headroom.remaining(model)
    assert ACCOUNT_FIELD not in caplog.text
    assert "example.invalid" not in caplog.text


@pytest.mark.asyncio
async def test_nothing_credential_shaped_is_raised(tmp_path, fixture_payload) -> None:
    """A snapshot the adapter cannot use is an answer, not an exception."""
    path = _snapshot(tmp_path, fixture_payload)
    headroom = CodexBarHeadroom(path, now=NOW)
    try:
        assert await headroom.remaining("glm") == pytest.approx(0.55)
    except Exception as exc:  # pragma: no cover - the assertion below is the point
        assert ACCOUNT_FIELD not in str(exc)
        raise


@pytest.mark.asyncio
async def test_the_reading_is_kept_within_the_refresh_window(tmp_path, fixture_payload) -> None:
    """The selector asks per model; the file is not read once per model."""
    path = _snapshot(tmp_path, fixture_payload)
    moments = iter([0.0, 10.0])
    headroom = CodexBarHeadroom(path, now=NOW, clock=lambda: next(moments))
    assert await headroom.remaining("glm") == pytest.approx(0.55)
    _snapshot(tmp_path, _with_zai_used(fixture_payload, 10.0))
    # The refresh window has not passed, so the reading already taken is used.
    assert await headroom.remaining("glm") == pytest.approx(0.55)


@pytest.mark.asyncio
async def test_a_newer_snapshot_is_picked_up_after_the_refresh_window(tmp_path, fixture_payload) -> None:
    path = _snapshot(tmp_path, fixture_payload)
    moments = iter([0.0, 120.0])
    headroom = CodexBarHeadroom(path, now=NOW, clock=lambda: next(moments))
    assert await headroom.remaining("glm") == pytest.approx(0.55)
    _snapshot(tmp_path, _with_zai_used(fixture_payload, 10.0))
    # The refresh window has passed, so the file is read again.
    assert await headroom.remaining("glm") == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_the_adapter_satisfies_the_headroom_port() -> None:
    """The adapter must offer what ``HeadroomPort`` asks for."""
    from src.tools.auto_dispatch.selection import HeadroomPort

    assert hasattr(HeadroomPort, "remaining")
    headroom = CodexBarHeadroom(Path("/nonexistent.json"))
    assert callable(headroom.remaining)
    assert await headroom.remaining("kimi") == 1.0


@pytest.mark.asyncio
async def test_the_configured_path_does_not_reach_the_logs(tmp_path, caplog) -> None:
    """The expanded path carries the account's home directory; keep it out.

    The debug line names a stable reason code instead, so the log can be
    grepped for a reason and does not repeat a machine-specific path. There
    are two reasons now: the file could not be read, and a provider had no
    fresh window to read.
    """
    missing = tmp_path / "absent.json"
    stale_row = _snapshot(
        tmp_path,
        {
            "updatedAt": _stamp(NOW),
            "providers": [_provider_row("zai", _stamp(NOW - timedelta(hours=2)), 100.0)],
        },
    )
    with caplog.at_level("DEBUG"):
        await CodexBarHeadroom(missing, now=NOW).remaining("glm")
        await CodexBarHeadroom(stale_row, now=NOW).remaining("glm")
    assert "reason=unreadable" in caplog.text
    assert "reason=no-fresh-window" in caplog.text
    assert str(tmp_path) not in caplog.text


@pytest.mark.asyncio
async def test_the_selector_accepts_the_adapter() -> None:
    """The adapter must be usable where ``QuotaAwareModelSelector`` expects a port."""
    from src.tools.auto_dispatch.selection import QuotaAwareModelSelector

    selector = QuotaAwareModelSelector(headroom=CodexBarHeadroom(Path("/nonexistent.json")))
    assert selector is not None


# Review blocker (R4-2 round 2): CodexBar read four cases as 0.0 -- spent --
# that ``native_runtime.usage_headroom`` reads as low or as no reading. In the
# merged default port the more exhausted reading wins, so each one excluded a
# Worker the snapshot reader kept (cursor, codex on credits).


def _row(provider: str, windows: list, **extra) -> dict:
    return {
        "provider": provider,
        "kind": "quota",
        "status": "ok",
        "updated_at": "2026-11-06T11:59:00Z",
        "windows": windows,
        **extra,
    }


@pytest.mark.asyncio
async def test_a_full_plan_with_credits_left_reads_low_not_spent(tmp_path) -> None:
    path = _snapshot(
        tmp_path,
        {
            "providers": [
                _row(
                    "codex",
                    [{"role": "secondary", "used_percent": 100, "resets_at_utc": "2026-11-10T00:00:00Z"}],
                    credits_remaining=7541.0,
                ),
                _row("kimi", [{"role": "primary", "used_percent": 100}], credits_remaining=0),
            ]
        },
    )
    headroom = CodexBarHeadroom(path, now=NOW)

    assert await headroom.remaining("codex") == pytest.approx(LOW_HEADROOM)
    assert await headroom.remaining("kimi") == 0.0
    # The full plan is still reported, so N63 decides whether review may spend.
    assert await headroom.plan_status("codex") == {
        "plan_full": True,
        "credits": True,
        "resets_at": "2026-11-10T00:00:00Z",
    }
    assert (await headroom.plan_status("kimi"))["credits"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["tertiary", "extra"])
async def test_a_full_bucket_window_reads_low_not_spent(tmp_path, role) -> None:
    path = _snapshot(
        tmp_path,
        {
            "providers": [
                _row(
                    "cursor",
                    [
                        {"role": "primary", "used_percent": 5},
                        {"role": role, "used_percent": 100, "resets_at_utc": "2026-11-10T00:00:00Z"},
                    ],
                )
            ]
        },
    )
    headroom = CodexBarHeadroom(path, now=NOW)

    assert await headroom.remaining("cursor") == pytest.approx(LOW_HEADROOM)
    assert (await headroom.plan_status("cursor"))["plan_full"] is False


@pytest.mark.asyncio
async def test_a_window_whose_reset_has_passed_no_longer_limits(tmp_path) -> None:
    path = _snapshot(
        tmp_path,
        {
            "providers": [
                _row(
                    "kimi",
                    [
                        {"role": "primary", "used_percent": 100, "resets_at_utc": "2026-11-06T11:59:30Z"},
                        {"role": "secondary", "used_percent": 30, "resets_at_utc": "2026-11-10T00:00:00Z"},
                    ],
                ),
                _row(
                    "grok",
                    [{"role": "primary", "used_percent": 100, "resets_at_utc": "2026-11-06T11:00:00Z"}],
                ),
            ]
        },
    )
    headroom = CodexBarHeadroom(path, now=NOW)

    assert await headroom.remaining("kimi") == pytest.approx(0.7)
    assert await headroom.remaining("grok") == 1.0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["unknown", "error", "failed"])
async def test_an_error_row_is_no_reading_not_a_spent_quota(tmp_path, status) -> None:
    path = _snapshot(
        tmp_path,
        {
            "providers": [
                _row("cursor", [{"role": "primary", "used_percent": 100}], status=status)
            ]
        },
    )
    headroom = CodexBarHeadroom(path, now=NOW)

    assert await headroom.remaining("cursor") == 1.0
    assert await headroom.plan_status("cursor") is None


@pytest.mark.asyncio
async def test_codexbar_agrees_with_the_snapshot_reader_on_each_case(tmp_path) -> None:
    """Every case above, read by both readers: spent in one is spent in the other."""
    from src.server.services.native_runtime.usage_headroom import read_usage_headroom

    reset = "2026-11-10T00:00:00Z"
    path = _snapshot(
        tmp_path,
        {
            "providers": [
                _row("codex", [{"role": "secondary", "used_percent": 100, "resets_at_utc": reset}], credits_remaining=5),
                _row("cursor", [{"role": "primary", "used_percent": 5}, {"role": "tertiary", "used_percent": 100, "resets_at_utc": reset}]),
                _row("kimi", [{"role": "primary", "used_percent": 100, "resets_at_utc": "2026-11-06T11:00:00Z"}]),
                _row("grok", [{"role": "primary", "used_percent": 100}], status="error"),
                _row("minimax", [{"role": "primary", "used_percent": 100, "resets_at_utc": reset}]),
            ]
        },
    )
    states = read_usage_headroom(str(path), now=NOW)
    headroom = CodexBarHeadroom(path, now=NOW)

    for worker in ("codex", "cursor", "kimi", "grok", "minimax"):
        spent = await headroom.remaining(worker) <= 0.0
        assert spent == (states.get(worker) == "exhausted"), worker


def test_low_headroom_is_the_selectors_low_figure() -> None:
    from src.tools.auto_dispatch.selection import _SNAPSHOT_FRACTION

    assert LOW_HEADROOM == _SNAPSHOT_FRACTION["low"]
