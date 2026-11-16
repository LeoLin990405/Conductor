"""N61 round 3: a torn earlier append never merges with the next record."""

from __future__ import annotations

import json

from src.server.services.judgment.record import JsonlRecorder, JudgmentRecord


def _record(action: str) -> JudgmentRecord:
    return JudgmentRecord(
        use="u", version="1", mode="shadow", question_ids=[], status="pass",
        question_status={}, answers={}, probabilities={}, latency_ms=0,
        backend="fake", reason="", action=action, state_digest={},
    )


def test_append_after_a_torn_line_starts_on_its_own_line(tmp_path):
    path = tmp_path / "records.jsonl"
    path.write_bytes(b'{"use": "u", "act')  # a half-written record, no newline
    JsonlRecorder(path).write(_record("next"))
    lines = path.read_bytes().split(b"\n")
    assert lines[0] == b'{"use": "u", "act'
    assert json.loads(lines[1])["action"] == "next"
    assert lines[2] == b""


def test_every_record_is_one_complete_line(tmp_path):
    path = tmp_path / "records.jsonl"
    recorder = JsonlRecorder(path)
    for i in range(3):
        recorder.write(_record(str(i)))
    data = path.read_bytes()
    assert data.endswith(b"\n") and not data.startswith(b"\n")
    assert [json.loads(x)["action"] for x in data.splitlines()] == ["0", "1", "2"]
