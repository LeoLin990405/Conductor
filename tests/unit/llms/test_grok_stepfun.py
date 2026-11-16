import json


def test_grok_result_and_stream_parser():
    from src.llms.native_cli.grok import parse_grok_result
    from src.server.services.native_runtime.grok_stream import parse_grok_stream_line

    text, session, usage, _ = parse_grok_result(json.dumps({
        "text": "grok-ok",
        "sessionId": "grok-session",
        "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
    }))
    assert text == "grok-ok"
    assert session == "grok-session"
    assert usage.total_tokens == 5

    assert parse_grok_stream_line('{"type":"text","data":"hi"}')[0].text == "hi"
    started = parse_grok_stream_line(
        '{"type":"tool_call","toolCallId":"c1","toolName":"write","rawInput":{"a":1}}'
    )[0]
    assert started.kind == "tool_started"
    completed = parse_grok_stream_line(
        '{"type":"tool_call_update","toolCallId":"c1","status":"completed","content":"ok"}'
    )[0]
    assert completed.kind == "tool_result"
    end = parse_grok_stream_line(
        '{"type":"end","sessionId":"s","usage":{"input_tokens":1,"output_tokens":1}}'
    )
    assert [event.kind for event in end] == ["usage", "turn_completed"]


def test_step_result_and_argv():
    from src.llms.native_cli.stepfun import StepFunAdapter, parse_step_result

    text, session, usage, _ = parse_step_result(json.dumps([
        {"type": "session", "id": "step-session"},
        {"type": "result", "result": "step-ok", "usage": {"input_tokens": 2, "output_tokens": 1}},
    ]))
    assert text == "step-ok"
    assert session == "step-session"
    assert usage.input_tokens == 2

    invocation = StepFunAdapter(binary="/opt/step", read_only=True).build_invocation(
        "hello", cwd="/tmp"
    )
    assert invocation.argv[:3] == ("/opt/step", "--mode", "json")
    assert "--session-id" not in invocation.argv
    assert invocation.cwd == "/tmp"


def test_grok_and_stepfun_approval_capabilities_are_opt_in(monkeypatch):
    from src.server.services.native_runtime import registry

    monkeypatch.delenv("NATIVE_CLI_ACP", raising=False)
    monkeypatch.delenv("NATIVE_CLI_STEPFUN_APPROVALS", raising=False)
    assert registry.status_for("grok").capabilities.approvals is False
    assert registry.status_for("stepfun").capabilities.approvals is False

    monkeypatch.setenv("NATIVE_CLI_ACP", "grok")
    assert registry.status_for("grok").capabilities.approvals is True

    monkeypatch.setenv("NATIVE_CLI_STEPFUN_APPROVALS", "1")
    assert registry.status_for("stepfun").capabilities.approvals is True


def test_grok_resumes_only_the_id_it_reported_and_never_a_sentinel():
    from src.llms.native_cli.grok import GrokCliAdapter, grok_stream_session_id

    adapter = GrokCliAdapter(binary="/opt/grok")
    assert adapter.session_modes == frozenset({"resume"})
    assert adapter.supports_self_report is False
    fresh = adapter.build_invocation("hello").argv
    assert "--resume" not in fresh and "--session-id" not in fresh
    legacy = adapter.build_invocation("again", session_id="continue").argv
    assert "--resume" not in legacy and "--continue" not in legacy
    resumed = adapter.build_invocation("again", session_id="grok-made-1").argv
    assert resumed[resumed.index("--resume") + 1] == "grok-made-1"
    assert "--session-id" not in resumed

    stream = "\n".join(
        [
            '{"type":"text","data":"hi"}',
            "not json",
            '{"type":"end","sessionId":"grok-made-1"}',
        ]
    )
    assert grok_stream_session_id(stream) == "grok-made-1"
    assert grok_stream_session_id('{"type":"text","data":"hi"}') is None
