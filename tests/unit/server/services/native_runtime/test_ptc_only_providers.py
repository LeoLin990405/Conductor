import pytest

from src.llms.native_cli.registry import get_adapter, list_adapters
from src.server.services.native_runtime.registry import get_runtime, list_statuses, status_for


def test_ptc_only_providers_are_not_native():
    native = {row["provider"] for row in list_adapters()}
    status = {row.provider for row in list_statuses()}
    for provider in {"gemini", "qwen", "manus", "agy"}:
        assert provider not in native
        assert provider not in status
    assert {"codex", "claude-code", "cursor", "kimi", "grok", "stepfun", "zcode"} <= native
    assert {"claude-code", "cursor"} <= status


def test_manus_is_not_registered_as_a_native_coding_cli():
    from src.llms.native_cli import NativeCliError

    with pytest.raises(NativeCliError, match="unknown native CLI provider"):
        get_adapter("manus")


def test_zhipu_stays_cancel_only_and_glm_uses_the_zcode_adapter(monkeypatch):
    from src.llms.native_cli.zcode import ZCodeCliAdapter
    from src.llms.native_cli.zhipu import ZhipuCliAdapter

    assert isinstance(get_adapter("zhipu"), ZhipuCliAdapter)
    assert isinstance(get_adapter("glm"), ZCodeCliAdapter)
    assert get_runtime("glm").provider == "zcode"

    monkeypatch.setattr(
        ZhipuCliAdapter,
        "binary",
        property(lambda self: "/test/zcode"),
    )
    result = status_for("zhipu")
    assert result.installed is True
    assert result.live_verified is True
    assert result.capabilities.as_dict() == {
        "streaming": False,
        "tool_events": False,
        "session_resume": False,
        "cache_usage": False,
        "approvals": False,
        "mcp": False,
        "memory": False,
        "skills": False,
        "workspace_files": False,
        "cancel": True,
    }


def test_zcode_remains_a_distinct_native_runtime():
    from src.llms.native_cli.zcode import ZCodeCliAdapter

    status = {row.provider: row for row in list_statuses()}
    assert isinstance(get_adapter("zcode"), ZCodeCliAdapter)
    assert status["zcode"].capabilities.session_resume is True
    assert status["zcode"].capabilities.mcp is True


@pytest.mark.asyncio
async def test_zhipu_runtime_uses_one_shot_adapter_protocol(monkeypatch, tmp_path):
    from src.llms.native_cli.process import ProcessOutput
    from src.llms.native_cli.zhipu import ZhipuCliAdapter

    monkeypatch.setattr("src.llms.native_cli.zhipu._LOCK_PATH", tmp_path / "runtime.lock")
    invocations = []

    async def run_process(invocation, **_kwargs):
        invocations.append(invocation)
        return ProcessOutput(stdout="answer", stderr="", returncode=0)

    monkeypatch.setattr("src.llms.native_cli.zhipu.run_process", run_process)
    runtime = get_runtime(
        "zhipu", adapter=ZhipuCliAdapter(binary="/opt/zcode")
    )
    events = [
        event
        async for event in runtime.stream_turn("hello", cwd="/workspace/task")
    ]

    assert [event.kind for event in events] == [
        "text_delta", "usage", "turn_completed"
    ]
    assert events[0].text == "answer"
    assert invocations[0].cwd == "/tmp"
    assert invocations[0].argv == (
        "/opt/zcode", "-p", "hello", "--mode", "plan"
    )


def test_clean_room_zcode_does_not_advertise_desktop_capabilities(monkeypatch):
    from src.server.services.native_runtime import registry

    monkeypatch.setenv("NATIVE_CLI_ZCODE_BIN", "/opt/zcode-cli")
    monkeypatch.setenv("NATIVE_CLI_ZCODE_APPROVALS", "1")
    caps = registry.status_for("zcode").capabilities
    assert not (caps.mcp or caps.memory or caps.skills or caps.approvals)
    assert caps.session_resume

    runtime = registry.get_runtime("zcode", binary="/opt/zcode-cli")
    assert not runtime.capabilities().mcp

    monkeypatch.setenv("NATIVE_CLI_ZCODE_BIN", "/opt/zcode")
    assert registry.status_for("zcode").capabilities.approvals
    assert registry.get_runtime("zcode", binary="/opt/zcode").capabilities().mcp
