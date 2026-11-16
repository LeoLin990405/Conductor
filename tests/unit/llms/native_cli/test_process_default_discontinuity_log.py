"""A caller that passes no ``on_discontinuity`` must not get silence.

The six server-side native stream consumers call ``stream_process`` without a
callback. When bytes of the CLI's stdout are skipped or replaced the run is not
failed (that would turn a huge tool result into a failed turn), but the gap must
leave a trace: one content-free warning per reason per run.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from src.llms.native_cli import process as process_mod
from src.llms.native_cli.base import CliInvocation
from src.llms.native_cli.process import stream_process, track_processes

CAP = 64
SECRET_SHAPED = b"sk-" b"fake-NOT-A-REAL-KEY-"
BIG = SECRET_SHAPED * 5  # 115 bytes: over the shrunken cap, with a recognisable body


class _Reader:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def read(self, n: int = -1) -> bytes:
        await asyncio.sleep(0)
        return self._chunks.pop(0) if self._chunks else b""


class _Stdin:
    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


class _Process:
    pid = 424244

    def __init__(self, chunks: list[bytes]) -> None:
        self.stdout = _Reader(chunks)
        self.stderr = _Reader([])
        self.stdin = _Stdin()
        self.returncode = 0

    async def wait(self) -> int:
        return 0


@pytest.fixture
def run(monkeypatch, caplog):
    """Run ``stream_process`` over scripted stdout chunks; return the lines."""
    monkeypatch.setattr(process_mod, "_MAX_STDOUT", CAP)
    monkeypatch.setattr(
        process_mod, "prepare_process", lambda argv, cwd, env: (argv, None, cwd)
    )
    # The module's redaction is unrelated here and would add its own reason.
    monkeypatch.setattr(process_mod, "redact_diagnostic", lambda text, env=None: text)
    caplog.set_level(logging.WARNING, logger=process_mod.logger.name)

    async def _run(
        chunks: list[bytes],
        *,
        argv: tuple[str, ...] = ("fake-cli",),
        **kwargs,
    ) -> list[str]:
        async def fake_exec(*_argv, **_kwargs):
            return _Process(chunks)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        lines: list[str] = []
        with track_processes(None):
            invocation = CliInvocation(argv=argv, stdin="", timeout_seconds=30.0)
            async for line in stream_process(invocation, **kwargs):
                lines.append(line)
        return lines

    return _run


def _discontinuity_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "discontinuity" in r.getMessage()]


@pytest.mark.asyncio
async def test_an_oversized_record_is_logged_once_without_its_bytes(run, caplog):
    lines = await run([b"head\n" + BIG + b"\ntail\n"])

    assert lines == ["head", "tail"]  # delivery is unchanged
    records = _discontinuity_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    message = records[0].getMessage()
    assert "reason=oversized_record" in message
    assert "cli=fake-cli" in message
    # Not the skipped body, not any fragment of it, in the message or its args.
    assert SECRET_SHAPED.decode() not in caplog.text
    assert "sk-fake" not in repr(records[0].args)


@pytest.mark.asyncio
async def test_each_distinct_reason_is_logged_once(run, caplog):
    # An unterminated record outgrows the cap, then stdout ends inside it:
    # two different reasons, each reported once.
    await run([b"head\n", BIG, BIG])

    reasons = sorted(r.args[0] for r in _discontinuity_records(caplog))
    assert reasons == ["oversized_buffer", "oversized_record_at_eof"]


@pytest.mark.asyncio
async def test_invalid_utf8_is_logged_and_the_record_is_still_delivered(run, caplog):
    lines = await run([b"pass\xffword\n", b"after\n"])

    assert lines == ["pass�word", "after"]
    records = _discontinuity_records(caplog)
    assert [r.args[0] for r in records] == ["invalid_utf8"]


@pytest.mark.asyncio
async def test_a_repeated_reason_does_not_repeat_the_log_line(run, caplog):
    chunks = [(BIG + b"\n") * 50 + b"ok\n"]
    lines = await run(chunks)

    assert lines == ["ok"]
    assert len(_discontinuity_records(caplog)) == 1


@pytest.mark.asyncio
async def test_the_log_names_the_cli_by_basename_only(run, caplog):
    await run([BIG + b"\n"], argv=("/opt/vendor/bin/fake-cli",))

    (record,) = _discontinuity_records(caplog)
    assert record.args[1] == "fake-cli"
    assert "/opt/vendor" not in caplog.text


@pytest.mark.asyncio
async def test_a_clean_run_logs_nothing(run, caplog):
    lines = await run([b"a\nb\n", b"c"])

    assert lines == ["a", "b", "c"]
    assert _discontinuity_records(caplog) == []


@pytest.mark.asyncio
async def test_a_caller_with_a_callback_owns_the_signal_and_no_default_line_is_logged(
    run, caplog
):
    reasons: list[str] = []

    lines = await run([b"head\n" + BIG + b"\ntail\n"], on_discontinuity=reasons.append)

    assert lines == ["head", "tail"]
    assert reasons == ["oversized_record"]
    assert _discontinuity_records(caplog) == []
