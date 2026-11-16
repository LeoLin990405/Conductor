"""Differential-harness skeleton: schema, redaction, comparison and CLI.

The harness is a skeleton until live capture lands (Phase 3/4), so the parts
worth pinning now are the policies a later capture must obey: fixtures load and
validate, a committed fixture cannot carry a credential, and the comparison
tolerates exactly the documented volatility while failing on protocol drift.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.refactor_diff import cli
from scripts.refactor_diff.compare import (
    DiffReport,
    compare_fixtures,
)
from scripts.refactor_diff.normalize import (
    RedactionError,
    assert_redacted,
    normalize_fixture,
    normalize_text,
)
from scripts.refactor_diff.schema import (
    DbSnapshot,
    Exchange,
    Fixture,
    FixtureError,
    SseEvent,
    SseStream,
    load_fixture,
)

REPO = Path(__file__).resolve().parents[4]
FIXTURES = REPO / "scripts" / "refactor_diff" / "fixtures"


def _load(flow: str, name: str) -> Fixture:
    return load_fixture(FIXTURES / flow / name)


def _base_fixture(**overrides) -> Fixture:
    values = {
        "flow": "F2",
        "revision": "test",
        "http": (
            Exchange(
                method="POST",
                path="/api/v1/threads/x/messages",
                status=200,
                request={"body": {"message": "hello"}},
                response={"accepted": True},
            ),
        ),
        "sse": (
            SseStream(
                label="main",
                events=(
                    SseEvent(type="message_chunk", payload={"seq": 1, "content": "hi"}),
                ),
            ),
        ),
        "db": (
            DbSnapshot(
                table="conversation_responses",
                rows=({"conversation_response_id": "a", "status": "completed"},),
                keys=("conversation_response_id",),
            ),
        ),
    }
    values.update(overrides)
    return Fixture(**values)


class TestSchema:
    def test_committed_fixtures_load_and_validate(self):
        paths = sorted(FIXTURES.glob("*/*.json"))
        assert len(paths) == 7  # 6 synthetic baselines + 1 live F1 capture
        for path in paths:
            fixture = load_fixture(path)
            assert fixture.flow in {"F1", "F2", "F3"}
            if "live-" in path.name:
                # One real capture is committed; it must say so explicitly.
                assert fixture.synthetic is False
            else:
                assert fixture.synthetic is True

    @pytest.mark.parametrize(
        "fixture, message",
        [
            (_base_fixture(flow="F9"), "unknown flow"),
            (_base_fixture(revision=" "), "revision"),
            (
                _base_fixture(
                    http=(Exchange(method="post", path="/x", status=200),)
                ),
                "uppercase",
            ),
            (
                _base_fixture(
                    http=(Exchange(method="GET", path="relative", status=200),)
                ),
                "absolute",
            ),
            (
                _base_fixture(
                    http=(Exchange(method="GET", path="/x", status=42),)
                ),
                "status",
            ),
            (
                _base_fixture(
                    sse=(SseStream(label="main", events=(SseEvent(type=" "),)),)
                ),
                "event needs a type",
            ),
            (
                _base_fixture(
                    db=(DbSnapshot(table="t", rows=({"a": 1},), keys=("id",)),)
                ),
                "identity keys",
            ),
        ],
    )
    def test_validation_errors(self, fixture, message):
        with pytest.raises(FixtureError, match=message):
            fixture.validate()

    def test_round_trip_save_and_load(self, tmp_path):
        fixture = _base_fixture()
        path = tmp_path / "fixture.json"
        fixture.save(path)
        assert load_fixture(path) == fixture
        assert json.loads(path.read_text())["flow"] == "F2"


class TestNormalization:
    def test_uuid_and_timestamp_are_replaced(self):
        text = "thread 4f6a0e4c-3f9c-4d43-8a0b-9e0f1a2b3c4d at 2026-10-25T01:23:45Z"
        normalized = normalize_text(text)
        assert "4f6a0e4c" not in normalized
        assert "2026-10-25" not in normalized
        assert "<uuid>" in normalized and "<ts>" in normalized

    def test_normalization_is_idempotent(self):
        once = normalize_text("id 4f6a0e4c-3f9c-4d43-8a0b-9e0f1a2b3c4d")
        assert normalize_text(once) == once

    def test_fixture_normalization_clears_capture_metadata(self):
        fixture = _base_fixture(captured_at="2026-10-25T00:00:00Z")
        normalized = normalize_fixture(fixture)
        assert normalized.captured_at == ""
        assert normalized.revision == fixture.revision

    def test_different_uuids_normalize_together(self):
        old = _load("F1", "login-baseline.json")
        new = _load("F1", "login-candidate.json")
        old_norm = normalize_fixture(old)
        new_norm = normalize_fixture(new)
        assert old_norm.http[0].path == new_norm.http[0].path


class TestRedaction:
    def test_committed_fixtures_are_clean(self):
        for path in FIXTURES.glob("*/*.json"):
            assert_redacted(load_fixture(path))

    def test_sk_token_is_rejected(self):
        fixture = _base_fixture(
            http=(
                Exchange(
                    method="GET",
                    path="/x",
                    status=200,
                    response={"key": "sk-" "live-abcdefghijklmnop"},
                ),
            )
        )
        with pytest.raises(RedactionError, match="key"):
            assert_redacted(fixture)

    def test_secret_field_must_be_redacted_or_empty(self):
        fixture = _base_fixture(
            http=(
                Exchange(
                    method="GET",
                    path="/x",
                    status=200,
                    request={"headers": {"authorization": "Bearer real-token-value"}},
                ),
            )
        )
        with pytest.raises(RedactionError, match="secret field|Bearer"):
            assert_redacted(fixture)

    def test_redacted_marker_is_accepted(self):
        fixture = _base_fixture(
            http=(
                Exchange(
                    method="GET",
                    path="/x",
                    status=200,
                    request={"headers": {"authorization": "[REDACTED]"},
                             "api_key": "[REDACTED]"},
                ),
            )
        )
        assert_redacted(fixture)


class TestCompare:
    def test_f1_identical_after_normalization(self):
        report = compare_fixtures(
            normalize_fixture(_load("F1", "login-baseline.json")),
            normalize_fixture(_load("F1", "login-candidate.json")),
        )
        assert report.ok
        assert report.differences == []

    def test_f2_tolerates_volatility_and_flags_text(self):
        report = compare_fixtures(
            normalize_fixture(_load("F2", "turn-baseline.json")),
            normalize_fixture(_load("F2", "turn-candidate.json")),
        )
        assert report.ok, report.failures
        kinds = {diff.kind for diff in report.tolerated}
        assert "text" in kinds and "tolerated" in kinds
        assert len(report.tolerated) >= 4

    def test_f3_subagent_shape_is_stable(self):
        report = compare_fixtures(
            normalize_fixture(_load("F3", "subagent-baseline.json")),
            normalize_fixture(_load("F3", "subagent-candidate.json")),
        )
        assert report.ok
        assert report.differences == []

    def test_protocol_status_change_fails(self):
        old = normalize_fixture(_base_fixture())
        new_exchange = replace(old.http[0], status=500)
        new = replace(old, http=(new_exchange,))
        report = compare_fixtures(old, new)
        assert not report.ok
        assert any(diff.path.endswith(".status") for diff in report.failures)

    def test_numeric_beyond_tolerance_fails(self):
        old = normalize_fixture(_base_fixture())
        old_stream = old.sse[0]
        old_event = replace(old_stream.events[0], payload={"seq": 1, "token_count": 1000})
        old = replace(
            old,
            sse=(replace(old_stream, events=(old_event,)),),
        )
        new_event = replace(old_event, payload={"seq": 1, "token_count": 1400})
        new = replace(old, sse=(replace(old_stream, events=(new_event,)),))
        report = compare_fixtures(old, new)
        assert not report.ok
        assert any("token_count" in diff.path for diff in report.failures)

    def test_free_text_is_flagged_not_failed(self):
        old = normalize_fixture(_base_fixture())
        new_event = replace(old.sse[0].events[0], payload={"seq": 1, "content": "hello"})
        new = replace(old, sse=(replace(old.sse[0], events=(new_event,)),))
        report = compare_fixtures(old, new)
        assert report.ok
        assert report.tolerated

    def test_db_row_order_does_not_matter(self):
        old = _base_fixture(
            db=(
                DbSnapshot(
                    table="t",
                    rows=({"id": "a"}, {"id": "b"}),
                    keys=("id",),
                ),
            )
        )
        new = replace(old, db=(replace(old.db[0], rows=({"id": "b"}, {"id": "a"})),))
        report = compare_fixtures(normalize_fixture(old), normalize_fixture(new))
        assert report.ok

    def test_extra_http_exchange_fails(self):
        old = normalize_fixture(_base_fixture())
        new = replace(old, http=old.http + (old.http[0],))
        report = compare_fixtures(old, new)
        assert not report.ok
        assert any(diff.path == "http.length" for diff in report.failures)

    def test_sse_event_type_change_fails(self):
        old = normalize_fixture(_base_fixture())
        changed = replace(old.sse[0].events[0], type="error")
        new = replace(old, sse=(replace(old.sse[0], events=(changed,)),))
        report = compare_fixtures(old, new)
        assert not report.ok

    def test_report_renders_markdown_and_json(self):
        report = compare_fixtures(
            normalize_fixture(_load("F2", "turn-baseline.json")),
            normalize_fixture(_load("F2", "turn-candidate.json")),
        )
        markdown = report.render_markdown()
        assert "### Refactor diff F2: PASS" in markdown
        assert "| kind | path | old | new |" in markdown
        data = report.to_dict()
        assert data["ok"] is True
        assert data["flow"] == "F2"
        assert len(data["differences"]) == len(report.differences)


class TestCli:
    def test_validate_committed_fixtures(self, capsys):
        paths = [str(path) for path in sorted(FIXTURES.glob("*/*.json"))]
        code = cli.main(["validate", *paths])
        captured = capsys.readouterr()
        assert code == 0
        assert "OK" in captured.out
        assert "FAIL" not in captured.err

    def test_validate_reports_bad_fixture(self, tmp_path, capsys):
        path = tmp_path / "bad.json"
        fixture = _base_fixture()
        fixture.save(path)
        data = json.loads(path.read_text())
        data["flow"] = "F9"
        path.write_text(json.dumps(data))
        assert cli.main(["validate", str(path)]) == 1
        assert "FAIL" in capsys.readouterr().err

    def test_compare_f2_passes(self, capsys):
        code = cli.main(
            [
                "compare",
                "--old",
                str(FIXTURES / "F2" / "turn-baseline.json"),
                "--new",
                str(FIXTURES / "F2" / "turn-candidate.json"),
            ]
        )
        assert code == 0
        assert "PASS" in capsys.readouterr().out

    def test_compare_writes_reports(self, tmp_path):
        markdown = tmp_path / "report.md"
        report_json = tmp_path / "report.json"
        code = cli.main(
            [
                "compare",
                "--old",
                str(FIXTURES / "F1" / "login-baseline.json"),
                "--new",
                str(FIXTURES / "F1" / "login-candidate.json"),
                "--markdown",
                str(markdown),
                "--json",
                str(report_json),
            ]
        )
        assert code == 0
        assert "PASS" in markdown.read_text()
        assert json.loads(report_json.read_text())["ok"] is True

    def test_compare_fails_on_redaction(self, tmp_path, capsys):
        bad = tmp_path / "bad.json"
        fixture = _base_fixture(
            http=(
                Exchange(
                    method="GET",
                    path="/x",
                    status=200,
                    response={"key": "sk-" "live-abcdefghijklmnop"},
                ),
            )
        )
        fixture.save(bad)
        code = cli.main(["compare", "--old", str(bad), "--new", str(bad)])
        assert code == 2
        assert "FAIL" in capsys.readouterr().err


def test_diff_report_defaults_to_pass():
    report = DiffReport(flow="F1", old_revision="a", new_revision="b")
    assert report.ok
    assert report.render_markdown().startswith("### Refactor diff F1: PASS")
