"""The live Swarm verifier's SSE reduction, without a server."""

from scripts.verify_swarm_runtime import scan_sse


def test_two_task_events_are_proof():
    lines = [
        "event: artifact",
        'data: {"artifact_id": "task:abc"}',
        "",
        "event: artifact",
        'data: {"artifact_id": "task:def"}',
        "",
        "event: turn_ended",
        "data: {}",
        "",
    ]
    result = scan_sse(lines)
    assert result["proof"] is True
    assert result["task_ids"] == ["abc", "def"]
    assert result["terminal"] is True


def test_one_task_is_not_enough_and_error_fails_closed():
    one = ["event: artifact", 'data: {"task_id": "abc"}', ""]
    assert scan_sse(one)["proof"] is False
    error = [
        "event: artifact",
        'data: {"task_id": "abc"}',
        "",
        "event: artifact",
        'data: {"task_id": "def"}',
        "",
        "event: error",
        'data: {"message": "boom"}',
        "",
    ]
    assert scan_sse(error)["proof"] is False
