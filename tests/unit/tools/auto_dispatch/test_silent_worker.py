"""Silent-worker detector: modes, threshold, CLI output shapes, records."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from src.tools.auto_dispatch import silent_worker as sw

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)


def _j(*events: object) -> str:
    return "\n".join(json.dumps(event) for event in events) + "\n"


# ------------------------------------------------------------------ modes


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "shadow"),
        ("", "shadow"),
        ("off", "off"),
        ("shadow", "shadow"),
        ("enforce", "enforce"),
        (" ENFORCE ", "shadow"),
        ("Off", "shadow"),
        ("bogus", "shadow"),
    ],
)
def test_silent_mode(value, expected):
    env = {} if value is None else {sw.MODE_ENV: value}
    assert sw.silent_mode(env) == expected


def test_mode_env_name():
    assert sw.MODE_ENV == "CONDUCTOR_DISPATCH_SILENT_WORKER_MODE"
    assert sw.MINUTES_ENV == "CONDUCTOR_DISPATCH_SILENT_WORKER_MINUTES"


def test_silent_mode_reads_process_env(monkeypatch):
    monkeypatch.setenv(sw.MODE_ENV, "off")
    assert sw.silent_mode() == "off"
    monkeypatch.delenv(sw.MODE_ENV)
    assert sw.silent_mode() == "shadow"


@pytest.mark.parametrize(
    ("mode", "record", "act"),
    [
        ("off", False, False),
        ("shadow", True, False),
        ("enforce", True, True),
        ("x", True, False),
    ],
)
def test_mode_helpers(mode, record, act):
    assert sw.should_record(mode) is record
    assert sw.should_act(mode) is act


def test_mode_helpers_from_environ():
    assert sw.should_act(environ={sw.MODE_ENV: "enforce"}) is True
    assert sw.should_record(environ={sw.MODE_ENV: "off"}) is False
    assert sw.should_record(environ={}) is True
    assert sw.should_act(environ={}) is False


# -------------------------------------------------------------- threshold


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        (None, 900.0),
        ("", 900.0),
        ("abc", 900.0),
        ("0", 900.0),
        ("-3", 900.0),
        ("nan", 900.0),
        ("inf", 900.0),
        ("-inf", 900.0),
        ("5", 300.0),
        (" 2.5 ", 150.0),
        ("0.5", 30.0),
    ],
)
def test_silent_after_seconds(value, seconds):
    env = {} if value is None else {sw.MINUTES_ENV: value}
    assert sw.silent_after_seconds(env) == seconds


def test_silent_after_seconds_process_env(monkeypatch):
    monkeypatch.setenv(sw.MINUTES_ENV, "1")
    assert sw.silent_after_seconds() == 60.0
    assert sw.DEFAULT_MINUTES == 15


# ------------------------------------------------------- output detection

LAUNCHER_ONLY = (
    "[launcher] deepseek: restored HEAD in the admin dir\n"
    "dispatch_launch: objects_not_migrated worker_status=0 quarantine=/tmp/q x\n"
    "[watchdog] no log growth for 900s, stopping the worker\n"
    "/x/scripts/dispatch_launch.sh: line 12: 4242 Terminated  run_worker\n"
    "Terminated\n"
    "\n   \n"
)


def test_empty_and_blank():
    assert sw.has_assistant_output("") is False
    assert sw.has_assistant_output("\n \n\t\n") is False
    assert sw.has_assistant_output(None) is False  # type: ignore[arg-type]


def test_launcher_lines_are_not_output():
    assert sw.has_assistant_output(LAUNCHER_ONLY) is False


def test_ansi_is_stripped():
    assert (
        sw.has_assistant_output("\x1b[2K\x1b[0m\x1b]0;title\x07\n\x1b[31m\x1b[0m\n")
        is False
    )
    assert sw.has_assistant_output("\x1b[32m[launcher] x\x1b[0m\n") is False
    assert sw.has_assistant_output("\x1b[32mhello\x1b[0m\n") is True


def test_plain_text_counts_as_output():
    assert sw.has_assistant_output(LAUNCHER_ONLY + "I fixed the bug.\n") is True


def test_carriage_return_progress():
    assert sw.has_assistant_output("[launcher] a\r[launcher] b\r") is False


def test_echoed_prompt_is_not_output():
    prompt = "Fix the parser.\n\nRun the tests.\n"
    log = "Fix the parser.\nRun the tests.\n" + LAUNCHER_ONLY
    assert sw.has_assistant_output(log, prompt=prompt) is False
    assert sw.has_assistant_output(log) is True  # unknown echo: conservative
    assert sw.has_assistant_output(log + "Done.\n", prompt=prompt) is True


def test_unparseable_json_counts():
    assert sw.has_assistant_output('{"type": "assistant", "trunc\n') is True
    assert sw.has_assistant_output("[INFO] thinking about it\n") is True


def test_unknown_typed_event_counts():
    assert sw.has_assistant_output(_j({"type": "mystery.event"})) is True


def test_untyped_object():
    assert sw.has_assistant_output(_j({"session_id": "s", "usage": {"x": 1}})) is False
    assert sw.has_assistant_output(_j({"foo": "bar"})) is True


def test_json_scalars_and_lists():
    assert sw.has_assistant_output("0\n") is True  # plain text line
    assert sw.has_assistant_output("[1, 2]\n") is False
    assert sw.has_assistant_output('["hi"]\n') is True
    assert sw.has_assistant_output('[{"type": "system"}]\n') is False
    assert sw.has_assistant_output("null\n") is True  # plain text, not "{["
    assert sw.has_assistant_output("[null, true]\n") is False


# Claude / GLM zcode / cursor stream-json
CLAUDE_INIT = {"type": "system", "subtype": "init", "session_id": "s1", "tools": []}
CLAUDE_USER = {
    "type": "user",
    "message": {"role": "user", "content": [{"type": "text", "text": "Fix the bug"}]},
}
CLAUDE_EMPTY_RESULT = {
    "type": "result",
    "subtype": "success",
    "result": "",
    "usage": {},
}


def test_claude_silent():
    assert (
        sw.has_assistant_output(_j(CLAUDE_INIT, CLAUDE_USER, CLAUDE_EMPTY_RESULT))
        is False
    )


def test_claude_assistant_text():
    msg = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "Done"}]},
    }
    assert sw.has_assistant_output(_j(CLAUDE_INIT, msg)) is True


def test_claude_assistant_empty_and_tool_use():
    empty = {"type": "assistant", "message": {"role": "assistant", "content": []}}
    assert sw.has_assistant_output(_j(empty)) is False
    blank = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "  "}]},
    }
    assert sw.has_assistant_output(_j(blank)) is False
    tool = {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]},
    }
    assert sw.has_assistant_output(_j(tool)) is True


def test_claude_result_text():
    assert (
        sw.has_assistant_output(_j({"type": "result", "result": "All green"})) is True
    )


def test_claude_tool_result_user_event():
    tr = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "content": "ok"}],
        },
    }
    assert sw.has_assistant_output(_j(tr)) is False


def test_claude_stream_event_partials():
    start = {
        "type": "stream_event",
        "event": {"type": "message_start", "message": {"usage": {}}},
    }
    delta = {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "Hi"},
        },
    }
    empty_delta = {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": ""},
        },
    }
    assert sw.has_assistant_output(_j(start, empty_delta)) is False
    assert sw.has_assistant_output(_j(start, delta)) is True


def test_rate_limit_and_error_events():
    assert (
        sw.has_assistant_output(_j({"type": "rate_limit_event", "info": {}})) is False
    )
    assert sw.has_assistant_output(_j({"type": "error", "message": "boom"})) is False


def test_glm_turn_events():
    events = (
        CLAUDE_INIT,
        CLAUDE_USER,
        {"type": "turn.completed", "usage": {"input_tokens": 3}},
        {"type": "turn.failed", "error": {"message": "x"}},
    )
    assert sw.has_assistant_output(_j(*events)) is False


# Codex --json
def test_codex_silent():
    events = (
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "turn.started"},
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 0}},
    )
    assert sw.has_assistant_output(_j(*events)) is False


def test_codex_agent_message():
    msg = {
        "type": "item.completed",
        "item": {"id": "i", "type": "agent_message", "text": "Fixed"},
    }
    assert (
        sw.has_assistant_output(_j({"type": "thread.started", "thread_id": "t"}, msg))
        is True
    )
    empty = {
        "type": "item.completed",
        "item": {"id": "i", "type": "agent_message", "text": ""},
    }
    assert sw.has_assistant_output(_j(empty)) is False


def test_codex_reasoning_and_command():
    reasoning = {
        "type": "item.completed",
        "item": {"type": "reasoning", "text": "Look first"},
    }
    command = {
        "type": "item.started",
        "item": {"type": "command_execution", "command": "ls"},
    }
    unknown = {"type": "item.completed", "item": {"type": "brand_new_kind"}}
    user = {
        "type": "item.completed",
        "item": {"type": "user_message", "text": "prompt"},
    }
    assert sw.has_assistant_output(_j(reasoning)) is True
    assert sw.has_assistant_output(_j(command)) is True
    assert sw.has_assistant_output(_j(unknown)) is True
    assert sw.has_assistant_output(_j(user)) is False


# Kimi stream-json
def test_kimi_shapes():
    meta = {"role": "meta", "type": "session.resume_hint", "session_id": "k1"}
    user = {"role": "user", "content": "Fix it"}
    tool = {"role": "tool", "content": "output of ls"}
    assert sw.has_assistant_output(_j(meta, user, tool)) is False
    ans = {"role": "assistant", "content": [{"type": "text", "text": "Done"}]}
    assert sw.has_assistant_output(_j(meta, ans)) is True
    empty = {"role": "assistant", "content": [{"type": "text", "text": ""}]}
    assert sw.has_assistant_output(_j(meta, empty)) is False
    err = {"error": {"code": "429", "message": "rate limited"}}
    assert sw.has_assistant_output(_j(meta, err)) is False


# Grok streaming-json
def test_grok_shapes():
    usage = {"type": "usage", "input_tokens": 3}
    end = {"type": "end", "sessionId": "g1"}
    assert sw.has_assistant_output(_j(usage, end)) is False
    assert sw.has_assistant_output(_j({"type": "text", "text": "Hi"}, end)) is True
    assert sw.has_assistant_output(_j({"type": "thought", "text": "hmm"})) is True
    assert sw.has_assistant_output(_j({"type": "tool_call", "name": "bash"})) is True
    assert sw.has_assistant_output(_j({"type": "text", "text": ""})) is False


# DeepSeek / DSH headless NDJSON
def test_dsh_shapes():
    session = {"type": "session", "sessionId": "d1"}
    status = {"type": "status", "phase": "step_end", "usage": {"input_tokens": 1}}
    assert sw.has_assistant_output(_j(session, status)) is False
    assert (
        sw.has_assistant_output(_j(session, {"type": "final", "text": "Answer"}))
        is True
    )
    assert sw.has_assistant_output(_j({"type": "thinking", "text": "plan"})) is True
    assert sw.has_assistant_output(_j(session, {"type": "final", "text": ""})) is False


# StepFun --mode json
def test_stepfun_shapes():
    session = {"type": "session", "id": "st1"}
    tool_result = {"type": "tool_result", "output": "file list"}
    assert sw.has_assistant_output(_j(session, tool_result)) is False
    end = {
        "type": "message_end",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
    }
    assert sw.has_assistant_output(_j(session, end)) is True
    user_end = {"type": "message_end", "message": {"role": "user", "content": "prompt"}}
    assert sw.has_assistant_output(_j(user_end)) is False
    empty_turn = {"type": "turn_end", "message": {"content": []}}
    assert sw.has_assistant_output(_j(empty_turn)) is False
    done = {"type": "done", "result": "final answer"}
    assert sw.has_assistant_output(_j(done)) is True


# MiniMax mcode exec / doubao arkcli (single, possibly pretty-printed, JSON)
def test_minimax_exec_result():
    ok = {
        "type": "exec.result",
        "status": "succeeded",
        "output": "Done",
        "sessionId": "m",
    }
    empty = {
        "type": "exec.result",
        "status": "succeeded",
        "output": "",
        "sessionId": "m",
    }
    assert sw.has_assistant_output(json.dumps(ok)) is True
    assert sw.has_assistant_output(json.dumps(empty)) is False
    assert sw.has_assistant_output(json.dumps(empty, indent=2)) is False
    assert sw.has_assistant_output(json.dumps(ok, indent=2)) is True


def test_doubao_envelope():
    ok = {
        "id": "r1",
        "content": "Hello",
        "usage": {"total_tokens": 3},
        "status": "completed",
    }
    empty = {
        "id": "r1",
        "content": "",
        "usage": {"total_tokens": 3},
        "status": "completed",
    }
    assert sw.has_assistant_output(json.dumps(ok, indent=2)) is True
    assert sw.has_assistant_output(json.dumps(empty, indent=2)) is False
    assert sw.has_assistant_output(LAUNCHER_ONLY + json.dumps(empty, indent=2)) is False


def test_mixed_log_with_launcher_and_events():
    log = LAUNCHER_ONLY + _j(CLAUDE_INIT, CLAUDE_USER) + "\x1b[0m\n"
    assert sw.has_assistant_output(log) is False


def test_deep_nesting_is_conservative():
    deep: dict = {"text": "x"}
    for _ in range(20):
        deep = {"wrap": deep}
    deep["type"] = "assistant"
    assert sw.has_assistant_output(_j(deep)) is True


# ------------------------------------------------------------ classify_run


def test_classify_empty_final():
    f = sw.classify_run(
        LAUNCHER_ONLY, complete=True, exit_code=0, elapsed_seconds=12.0, environ={}
    )
    assert f == sw.SilentFinding(
        kind=sw.KIND_EMPTY_FINAL, elapsed_seconds=12.0, threshold_seconds=900.0
    )
    assert f.reason == sw.WORKER_NO_OUTPUT == "worker_no_output"


def test_classify_complete_exit_none():
    f = sw.classify_run(
        "", complete=True, exit_code=None, elapsed_seconds=None, environ={}
    )
    assert f is not None and f.kind == sw.KIND_EMPTY_FINAL and f.elapsed_seconds is None


def test_classify_nonzero_exit_is_not_ours():
    assert (
        sw.classify_run(
            "", complete=True, exit_code=1, elapsed_seconds=5000, environ={}
        )
        is None
    )
    assert (
        sw.classify_run(
            "", complete=False, exit_code=79, elapsed_seconds=5000, environ={}
        )
        is None
    )


def test_classify_output_present():
    assert (
        sw.classify_run(
            "Done.\n", complete=True, exit_code=0, elapsed_seconds=1, environ={}
        )
        is None
    )
    assert (
        sw.classify_run(
            "Done.\n", complete=False, exit_code=None, elapsed_seconds=9e9, environ={}
        )
        is None
    )


def test_classify_timeout():
    env = {sw.MINUTES_ENV: "10"}
    assert (
        sw.classify_run(
            "", complete=False, exit_code=None, elapsed_seconds=599.9, environ=env
        )
        is None
    )
    f = sw.classify_run(
        "", complete=False, exit_code=None, elapsed_seconds=600, environ=env
    )
    assert f is not None and f.kind == sw.KIND_NO_OUTPUT_TIMEOUT
    assert f.threshold_seconds == 600.0 and f.elapsed_seconds == 600.0


def test_classify_timeout_needs_elapsed():
    assert (
        sw.classify_run(
            "", complete=False, exit_code=None, elapsed_seconds=None, environ={}
        )
        is None
    )
    nan = float("nan")
    assert (
        sw.classify_run(
            "", complete=False, exit_code=None, elapsed_seconds=nan, environ={}
        )
        is None
    )


def test_classify_uses_prompt():
    prompt = "Do the thing"
    log = "Do the thing\n"
    assert (
        sw.classify_run(log, complete=True, exit_code=0, elapsed_seconds=1, environ={})
        is None
    )
    f = sw.classify_run(
        log, complete=True, exit_code=0, elapsed_seconds=1, environ={}, prompt=prompt
    )
    assert f is not None


def test_finding_to_record_is_json_safe():
    f = sw.SilentFinding(
        kind=sw.KIND_NO_OUTPUT_TIMEOUT,
        elapsed_seconds=float("inf"),
        threshold_seconds=900,
    )
    rec = f.to_record()
    assert rec == {
        "kind": sw.KIND_NO_OUTPUT_TIMEOUT,
        "reason": sw.WORKER_NO_OUTPUT,
        "elapsed_seconds": None,
        "threshold_seconds": 900.0,
    }
    json.dumps(rec, allow_nan=False)


# --------------------------------------------------------------- records

FINDING = sw.SilentFinding(
    kind=sw.KIND_EMPTY_FINAL, elapsed_seconds=3.0, threshold_seconds=900.0
)


def test_record_finding_creates_cell():
    snap: dict = {}
    rec = sw.record_finding(
        snap, FINDING, run_id="r1", attempt=2, author="glm", mode="shadow", now=NOW
    )
    cell = snap[sw.RECORD_KEY]
    assert cell["version"] == 1 and cell["last"] == "r1"
    assert cell["runs"]["r1"] is rec
    assert rec["kind"] == sw.KIND_EMPTY_FINAL and rec["reason"] == sw.WORKER_NO_OUTPUT
    assert rec["author"] == "glm" and rec["attempt"] == 2 and rec["mode"] == "shadow"
    assert rec["first_seen"] == rec["last_seen"] == NOW.isoformat()
    assert rec["count"] == 1 and rec["alerted"] is False
    json.dumps(snap, allow_nan=False)


def test_record_finding_idempotent():
    snap: dict = {}
    sw.record_finding(
        snap, FINDING, run_id="r1", attempt=1, author="kimi", mode="shadow", now=NOW
    )
    later = NOW + timedelta(minutes=5)
    timeout = sw.SilentFinding(kind=sw.KIND_NO_OUTPUT_TIMEOUT)
    rec = sw.record_finding(
        snap, timeout, run_id="r1", attempt=1, author="kimi", mode="enforce", now=later
    )
    assert rec["count"] == 2
    assert rec["first_seen"] == NOW.isoformat()
    assert rec["last_seen"] == later.isoformat()
    assert rec["kind"] == sw.KIND_NO_OUTPUT_TIMEOUT and rec["mode"] == "enforce"
    assert len(snap[sw.RECORD_KEY]["runs"]) == 1


def test_record_keeps_alerted_flag():
    snap: dict = {}
    sw.record_finding(
        snap, FINDING, run_id="r1", attempt=1, author="a", mode="shadow", now=NOW
    )
    assert sw.mark_alerted(snap, "r1") is True
    rec = sw.record_finding(
        snap, FINDING, run_id="r1", attempt=1, author="a", mode="shadow", now=NOW
    )
    assert rec["alerted"] is True


def test_record_bounds_runs():
    snap: dict = {}
    for i in range(20):
        sw.record_finding(
            snap,
            FINDING,
            run_id=f"r{i:02d}",
            attempt=i,
            author="a",
            mode="shadow",
            now=NOW + timedelta(seconds=i),
        )
    runs = snap[sw.RECORD_KEY]["runs"]
    assert len(runs) == sw.MAX_RUNS == 16
    assert set(runs) == {f"r{i:02d}" for i in range(4, 20)}
    assert snap[sw.RECORD_KEY]["last"] == "r19"


def test_record_bound_keeps_current_even_if_older_clock():
    snap: dict = {}
    for i in range(16):
        sw.record_finding(
            snap,
            FINDING,
            run_id=f"r{i}",
            attempt=0,
            author="a",
            mode="shadow",
            now=NOW + timedelta(hours=1, seconds=i),
        )
    sw.record_finding(
        snap, FINDING, run_id="old", attempt=0, author="a", mode="shadow", now=NOW
    )
    runs = snap[sw.RECORD_KEY]["runs"]
    assert "old" in runs and "r0" not in runs and len(runs) == 16


def test_record_resets_malformed_cell_and_normalizes():
    snap: dict = {sw.RECORD_KEY: {"version": 0, "runs": []}}
    naive = datetime(2026, 11, 14, 12, 0)
    rec = sw.record_finding(
        snap, FINDING, run_id=None, attempt=0, author="", mode="ENFORCE", now=naive
    )
    assert snap[sw.RECORD_KEY]["last"] == "unknown"
    assert rec["mode"] == "shadow"
    assert rec["first_seen"] == "2026-11-14T12:00:00+00:00"
    snap2: dict = {sw.RECORD_KEY: "junk"}
    sw.record_finding(
        snap2, FINDING, run_id="r", attempt=0, author="a", mode="off", now=NOW
    )
    assert snap2[sw.RECORD_KEY]["runs"]["r"]["mode"] == "off"


def test_record_tolerates_bad_count():
    snap: dict = {
        sw.RECORD_KEY: {"version": 1, "runs": {"r": {"count": "x"}}, "last": "r"}
    }
    rec = sw.record_finding(
        snap, FINDING, run_id="r", attempt=0, author="a", mode="shadow", now=NOW
    )
    assert rec["count"] == 1


# ---------------------------------------------------------------- alerts


def test_should_alert_and_mark():
    snap: dict = {}
    assert sw.should_alert(snap, "r1") is False
    assert sw.mark_alerted(snap, "r1") is False
    sw.record_finding(
        snap, FINDING, run_id="r1", attempt=0, author="a", mode="shadow", now=NOW
    )
    assert sw.should_alert(snap, "r1") is True
    assert sw.should_alert(snap, "r2") is False
    assert sw.mark_alerted(snap, "r1") is True
    assert sw.should_alert(snap, "r1") is False
    assert sw.should_alert({sw.RECORD_KEY: "junk"}, "r1") is False
    assert sw.mark_alerted({sw.RECORD_KEY: {"runs": []}}, "r1") is False


def test_alert_text():
    empty = sw.alert_text("L-7", "glm", FINDING)
    assert (
        empty
        == "Dispatch line L-7: worker glm finished with no model output (worker_no_output)."
    )
    timeout = sw.SilentFinding(kind=sw.KIND_NO_OUTPUT_TIMEOUT, elapsed_seconds=1800.0)
    assert "no model output for 30 min" in sw.alert_text(3, "kimi", timeout)
    bare = sw.SilentFinding(kind=sw.KIND_NO_OUTPUT_TIMEOUT)
    assert "before the timeout" in sw.alert_text(3, "kimi", bare)
    other = sw.SilentFinding(kind="weird")
    assert "weird" in sw.alert_text(None, None, other)


def test_alert_text_is_short_and_sanitized():
    text = sw.alert_text("x\n" * 200, "a\x1bb\r\nc", FINDING)
    assert "\n" not in text and "\x1b" not in text and "\r" not in text
    assert len(text) < 250
