"""The shadow decision JSONL: durable, private, append-only and rotated.

T4 counts seven days of shadow judgements, which outlive the backend log's
10MB x 5 WARNING rotation. These tests pin that stream: one JSON object per
decision with ``ts`` and the same fields as the text line, created ``0600``,
daily rotation keeping more than a month of files, one handler per logger
however often the module is imported, and append (never overwrite) across a
restart. Every test points the record at ``tmp_path``, so no test writes
``~/.conductor``.
"""

from __future__ import annotations

import importlib
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.utils import shadow_log


@pytest.fixture(autouse=True)
def shadow_file(tmp_path, monkeypatch):
    path = tmp_path / "logs" / "shadow-decisions.jsonl"
    monkeypatch.setenv(shadow_log.PATH_ENV, str(path))
    shadow_log._reset_handlers()
    yield path
    shadow_log._reset_handlers()


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _one_decision(**overrides) -> str:
    kwargs: dict[str, object] = {
        "would_change": True,
        "subject": "t-1",
        "current": "new",
        "enforced": "reuse",
        "reason": "duplicate_in_flight",
    }
    kwargs.update(overrides)
    return shadow_log.shadow_decision(
        logging.getLogger("tests.shadow"), "dedup", **kwargs
    )


def test_one_json_record_per_decision_with_ts_and_the_text_fields(
    shadow_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        line = _one_decision(existing_ref="issue:59", attempts=2)
    # The text line the backend log keeps is still one WARNING line, and the
    # JSON record is not written through the caller's logger.
    [record] = caplog.records
    assert record.message == line
    assert record.levelno == logging.WARNING
    [json_record] = _records(shadow_file)
    assert json_record == {
        "ts": json_record["ts"],
        "mode": "dedup",
        "would_change": True,
        "subject": "t-1",
        "current": "new",
        "enforced": "reuse",
        "reason": "duplicate_in_flight",
        "existing_ref": "issue:59",
        "attempts": 2,
    }
    # ISO 8601, UTC: the sample T4 lines up across hosts.
    stamp = datetime.fromisoformat(json_record["ts"])
    assert stamp.utcoffset() == timedelta(0)
    # Same key set on both streams, so a sampled text line finds its record.
    text_keys = {part.split("=", 1)[0] for part in line.split()[1:]}
    assert text_keys == set(json_record) - {"ts"}


def test_a_decision_without_a_reason_or_extras_still_writes_one_record(
    shadow_file: Path,
) -> None:
    _one_decision(reason="")
    [record] = _records(shadow_file)
    assert "reason" not in record
    assert record["would_change"] is True


def test_the_record_file_is_0600(shadow_file: Path) -> None:
    _one_decision()
    assert shadow_file.stat().st_mode & 0o777 == 0o600


def test_rotation_is_daily_keeps_over_a_month_and_archives_stay_0600(
    shadow_file: Path,
) -> None:
    _one_decision()
    lg = logging.getLogger(shadow_log.LOGGER_NAME)
    [handler] = lg.handlers
    handler.doRollover()
    _one_decision(subject="t-2")
    # 45 daily files: a 7-day T4 with 4x margin, and a quiet day does not
    # advance the rotation, so the margin is deliberate.
    assert shadow_log.BACKUP_DAYS >= 31
    assert handler.backupCount == shadow_log.BACKUP_DAYS
    assert handler.when == "MIDNIGHT"
    assert handler.utc is True
    archives = sorted(path for path in shadow_file.parent.iterdir() if path != shadow_file)
    assert len(archives) == 1
    assert archives[0].stat().st_mode & 0o777 == 0o600
    assert _records(shadow_file)[0]["subject"] == "t-2"


def test_a_repeated_import_does_not_attach_the_handler_twice(shadow_file: Path) -> None:
    lg = logging.getLogger(shadow_log.LOGGER_NAME)
    _one_decision()
    assert len(lg.handlers) == 1
    reloaded = importlib.reload(shadow_log)
    assert reloaded is shadow_log
    _one_decision(subject="t-2")
    assert len(lg.handlers) == 1
    # One handler must mean one record per decision, not two.
    assert [record["subject"] for record in _records(shadow_file)] == ["t-1", "t-2"]


def test_a_restart_appends_to_the_existing_file(shadow_file: Path) -> None:
    _one_decision(subject="before-restart")
    # A restarted backend has no handler: simulate it by dropping the live one.
    shadow_log._reset_handlers()
    importlib.reload(shadow_log)
    _one_decision(subject="after-restart")
    assert [record["subject"] for record in _records(shadow_file)] == [
        "before-restart",
        "after-restart",
    ]


def test_the_json_record_keeps_a_blocker_to_120_characters(shadow_file: Path) -> None:
    blocker = "the error handling looks fragile " * 20
    _one_decision(blocker=blocker)
    [record] = _records(shadow_file)
    assert len(record["blocker"]) <= shadow_log.MAX_VALUE_CHARS
    assert "scenario:" not in record["blocker"]


def test_a_broken_log_path_never_breaks_the_decision(
    tmp_path: Path, monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("x", encoding="utf-8")
    monkeypatch.setenv(shadow_log.PATH_ENV, str(blocked / "shadow-decisions.jsonl"))
    shadow_log._reset_handlers()
    with caplog.at_level(logging.DEBUG):
        line = _one_decision()
    assert line.startswith("shadow_decision mode=dedup would_change=true")
    # One debug line, never a raised exception: the decision stands.
    debug = [
        record
        for record in caplog.records
        if record.name == shadow_log.__name__ and record.levelno == logging.DEBUG
    ]
    assert len(debug) == 1
