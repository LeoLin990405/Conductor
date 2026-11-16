import json

from src.server.services.native_runtime.stepfun_stream import parse_stepfun_line


def test_stepfun_stream_text_and_completion():
    assert parse_stepfun_line(json.dumps({
        "type": "message_update",
        "assistantMessageEvent": {"type": "text_delta", "delta": "hello"},
    }))[0].text == "hello"
    assert parse_stepfun_line(json.dumps({
        "type": "message_update",
        "assistantMessageEvent": {"type": "thinking_delta", "delta": "think"},
    }))[0].kind == "thinking_delta"
    events = parse_stepfun_line(json.dumps({
        "type": "turn_end",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "done"}],
            "usage": {"input": 5, "output": 2, "totalTokens": 7},
        },
    }))
    assert events[0].kind == "usage"
    assert events[0].data["input_tokens"] == 5
    end = parse_stepfun_line(json.dumps({"type": "agent_end", "messages": []}))
    assert end[0].kind == "turn_completed"


def test_stepfun_tool_events():
    started = parse_stepfun_line(json.dumps({
        "type": "tool_execution_start",
        "toolCallId": "c1",
        "toolName": "write",
        "args": {"path": "/tmp/x"},
    }))[0]
    assert started.kind == "tool_started"
    done = parse_stepfun_line(json.dumps({
        "type": "tool_execution_end",
        "toolCallId": "c1",
        "toolName": "write",
        "result": "ok",
    }))[0]
    assert done.kind == "tool_result"
