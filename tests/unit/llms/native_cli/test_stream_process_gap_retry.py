"""stream_process: a gap report that failed is retried before what follows it.

A live-output consumer joins the records it is given into one text, so it must
be told about a hole before it is handed anything after it. These tests drive
the real generator against fake process objects (cap shrunk to 64 bytes) with a
callback that fails on demand.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from src.llms.native_cli import process as process_mod
from src.llms.native_cli.process import stream_process, track_processes
from tests.unit.llms.native_cli.test_stream_process_cleanup import (
    FakeProcess,
    FakeReader,
    _invocation,
)

BIG = b"X" * 100


@pytest.fixture(autouse=True)
def _small_cap(monkeypatch):
    monkeypatch.setattr(process_mod, "_MAX_STDOUT", 64)


@pytest.fixture
def run(monkeypatch):
    """Run to completion; returns the interleaved ('line'|'gap', value) events."""

    async def _run(chunks: list[bytes], *, fails) -> list[tuple[str, str]]:
        proc = FakeProcess(stdout=FakeReader(chunks), returncode=0)

        async def fake_exec(*argv, **kwargs):
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(
            process_mod, "prepare_process", lambda argv, cwd, env: (argv, None, cwd)
        )
        events: list[tuple[str, str]] = []
        calls: dict[str, int] = {}

        def callback(reason: str) -> None:
            events.append(("gap", reason))
            calls[reason] = calls.get(reason, 0) + 1
            if fails(reason, calls[reason]):
                raise RuntimeError("sink glitch")

        with track_processes(None):
            async for line in stream_process(_invocation(), on_discontinuity=callback):
                events.append(("line", line))
        return events

    return _run


@pytest.mark.asyncio
async def test_failed_report_is_offered_again_before_the_next_record(run):
    events = await run(
        [b"a\n" + BIG + b"\nb\nc\n"], fails=lambda reason, call: call == 1
    )
    assert events == [
        ("line", "a"),
        ("gap", "oversized_record"),
        ("gap", "oversized_record"),  # retried before "b" is handed over
        ("line", "b"),
        ("line", "c"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("failures", [1, 2])
async def test_a_gap_at_the_end_of_stdout_is_retried_after_a_failed_report(
    run, failures
):
    events = await run(
        [b"a\n", BIG, BIG],
        fails=lambda reason, call: (
            reason == "oversized_record_at_eof" and call <= failures
        ),
    )
    assert events[0] == ("line", "a")
    assert events.count(("gap", "oversized_record_at_eof")) == failures + 1
    assert events[-1] == ("gap", "oversized_record_at_eof")


@pytest.mark.asyncio
async def test_invalid_utf8_in_the_final_record_is_retried_before_the_record(run):
    events = await run([b"a\n", b"tail\xc3"], fails=lambda reason, call: call == 1)
    assert events == [
        ("line", "a"),
        ("gap", "invalid_utf8"),
        ("gap", "invalid_utf8"),
        ("line", "tail�"),
    ]


@pytest.mark.asyncio
async def test_a_redaction_in_the_final_record_is_retried_before_the_record(
    run, monkeypatch
):
    monkeypatch.setattr(
        process_mod,
        "redact_diagnostic",
        lambda text, env=None: text.replace("password", "[redacted]"),
    )
    events = await run([b"a\n", b"tail password"], fails=lambda reason, call: call == 1)
    assert events == [
        ("line", "a"),
        ("gap", "redacted"),
        ("gap", "redacted"),
        ("line", "tail [redacted]"),
    ]


@pytest.mark.asyncio
async def test_a_callback_that_keeps_failing_is_bounded_and_logged_as_an_error(
    run, caplog
):
    lines = b"".join(b"line%d\n" % index for index in range(300))
    with caplog.at_level(logging.WARNING, logger=process_mod.logger.name):
        events = await run([b"a\n" + BIG + b"\n" + lines], fails=lambda r, c: True)

    assert events.count(("gap", "oversized_record")) == 3
    assert len([e for e in events if e[0] == "line"]) == 301  # the run still ends well
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "never told" in errors[0].getMessage()


@pytest.mark.asyncio
async def test_giving_up_on_one_gap_does_not_silence_a_later_one(run):
    events = await run(
        [b"a\n" + BIG + b"\nb\nc\nd\n" + BIG + b"\ne\n"],
        fails=lambda reason, call: call <= 5,
    )
    # The first gap is dropped after calls 1-3. The second fails on 4 and 5 and
    # is delivered by the last chance at the end of stdout (call 6).
    assert events.count(("gap", "oversized_record")) == 6
    assert events[-2:] == [("line", "e"), ("gap", "oversized_record")]


@pytest.mark.asyncio
async def test_a_new_occurrence_of_a_pending_reason_gets_a_fresh_budget(run, caplog):
    # The first oversized record's report fails once and is still pending when
    # the second one is skipped. The second is a gap of its own, so it gets the
    # full three tries: 1 (first) + 3 (second) = 4 calls, not 3.
    with caplog.at_level(logging.WARNING, logger=process_mod.logger.name):
        events = await run(
            [BIG + b"\n" + BIG + b"\nb\nc\nd\n"], fails=lambda reason, call: True
        )

    assert events.count(("gap", "oversized_record")) == 4
    assert [e for e in events if e[0] == "line"] == [
        ("line", "b"),
        ("line", "c"),
        ("line", "d"),
    ]
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
