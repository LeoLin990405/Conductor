
from src.server.services.native_runtime.memory_token import issue_memory_token, verify_memory_token


def test_token_roundtrip_and_scope():
    token = issue_memory_token("user-1", "thread-1")
    payload = verify_memory_token(token)
    assert payload == {"u": "user-1", "t": "thread-1", "e": payload["e"]}


def test_tampered_and_expired_tokens_fail():
    token = issue_memory_token("u", "t", ttl_seconds=1, now=100)
    assert verify_memory_token(token + "x", now=100) is None
    assert verify_memory_token(token, now=102) is None

def test_token_can_carry_workspace_scope():
    token = issue_memory_token("user-1", "thread-1", workspace_id="ws-1")
    payload = verify_memory_token(token)
    assert payload["w"] == "ws-1"

