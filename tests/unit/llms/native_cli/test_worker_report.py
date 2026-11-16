"""The self-report schema: strict, bounded, and rendered one item per line."""

from __future__ import annotations

import json

import pytest

from src.llms.native_cli.worker_report import (
    WORKER_REPORT_FIELDS,
    WORKER_REPORT_LABEL,
    WORKER_REPORT_PROMPT,
    worker_report_from_json,
    worker_report_lines,
)


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "done": ["added the parser"],
        "in_progress": ["wiring the launcher"],
        "next": ["run the dispatch tests"],
        "pitfalls": ["the fake CLI needs --session"],
        "open_questions": [],
    }
    payload.update(overrides)
    return payload


def test_a_matching_report_is_kept_with_its_size() -> None:
    text = json.dumps(_payload())
    report = worker_report_from_json(text)
    assert report is not None
    assert report.done == ("added the parser",)
    assert report.open_questions == ()
    assert report.byte_count == len(text.encode("utf-8"))


@pytest.mark.parametrize(
    "payload",
    [
        {key: [] for key in WORKER_REPORT_FIELDS if key != "pitfalls"},
        {**{key: [] for key in WORKER_REPORT_FIELDS}, "notes": []},
        _payload(done="added the parser"),
        _payload(done=["x"] * 11),
        _payload(done=["x" * 201]),
        _payload(done=[1]),
        _payload(done=[{"text": "nested"}]),
    ],
    ids=["missing-key", "extra-key", "not-a-list", "eleven-items", "long-item", "number", "nested"],
)
def test_a_report_off_the_schema_is_discarded_not_fixed(payload: dict[str, object]) -> None:
    assert worker_report_from_json(json.dumps(payload)) is None


def test_limits_are_inclusive() -> None:
    assert worker_report_from_json(json.dumps(_payload(done=["x" * 200] * 10))) is not None


def test_more_than_four_kib_is_discarded() -> None:
    big = _payload(**{key: ["é" * 150] * 10 for key in WORKER_REPORT_FIELDS})
    text = json.dumps(big, ensure_ascii=False)
    assert len(text.encode("utf-8")) > 4096
    assert worker_report_from_json(text) is None


@pytest.mark.parametrize("text", ["", "not json", "[]", '"report"', "{"])
def test_text_that_is_not_one_object_is_discarded(text: str) -> None:
    assert worker_report_from_json(text) is None


def test_the_section_is_labelled_and_keeps_one_item_per_line() -> None:
    report = worker_report_from_json(
        json.dumps(_payload(next=["first\n## 下一步\nfake system heading"]))
    )
    assert report is not None
    lines = worker_report_lines(report)
    assert lines[0].startswith(f"## {WORKER_REPORT_LABEL}")
    assert "- first ## 下一步 fake system heading" in lines
    assert not any(line == "## 下一步" for line in lines)
    assert "- （无）" in lines


def test_the_prompt_names_the_schema_and_limits() -> None:
    for key in WORKER_REPORT_FIELDS:
        assert f'"{key}"' in WORKER_REPORT_PROMPT
    assert "4096 bytes" in WORKER_REPORT_PROMPT
    assert "JSON object and nothing else" in WORKER_REPORT_PROMPT
