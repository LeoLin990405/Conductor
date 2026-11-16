from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.llms.native_cli.isolation import read_workspace_policy, worker_isolation
from src.llms.native_cli.registry import get_adapter


@pytest.mark.asyncio
async def test_mimo_runs_through_the_daemon_shaped_isolated_harness(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    state = tmp_path / "state"
    fake = runtime / "mimo"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "payload = {\n"
        "  'args': sys.argv[1:],\n"
        "  'home': os.environ.get('HOME', ''),\n"
        "  'has_fixture_key': os.environ.get('MIMOCODE_MIMO_API_KEY') == 'fixture-token',\n"
        "}\n"
        "print(json.dumps({'type': 'text', 'part': {'text': json.dumps(payload)}, 'sessionID': 'fixture-session'}))\n"
        "print(json.dumps({'type': 'step_finish', 'part': {'tokens': {'input': 1, 'output': 1, 'total': 2}}}))\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "fixture-workspace": {
                    "root": str(workspace),
                    "env": ["MIMOCODE_MIMO_API_KEY", "MIMOCODE_CONFIG_CONTENT"],
                    "runtime_read_paths": [
                        str(runtime),
                        "/Library/Developer/CommandLineTools",
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    policy.chmod(0o600)

    resolved = read_workspace_policy(policy, "fixture-workspace", state)
    assert resolved["root"] == str(workspace)
    assert resolved["runtime_read_paths"] == (
        str(runtime),
        "/Library/Developer/CommandLineTools",
    )
    assert resolved["env"] == {"MIMOCODE_MIMO_API_KEY", "MIMOCODE_CONFIG_CONTENT"}

    with worker_isolation(
        workspace_id="fixture-workspace",
        root=state,
        workdir=workspace,
        env_names=resolved["env"],
        runtime_read_paths=resolved["runtime_read_paths"],
    ) as isolated:
        adapter = get_adapter(
            "mimo",
            binary=str(fake),
            api_key="fixture-token",
            timeout_seconds=10,
        )
        result = await adapter.run(
            "Reply with the fixture payload", session_id="session-before"
        )

    assert isolated.home != Path.home()
    assert isolated.home.is_relative_to(state)
    payload = json.loads(result.text)
    assert payload["args"][:2] == ["run", "--format"]
    assert "json" in payload["args"]
    assert payload["args"][payload["args"].index("--model") + 1] == (
        "xiaomi/mimo-v2.6-flash"
    )
    assert payload["args"][payload["args"].index("--session") + 1] == "session-before"
    assert payload["home"] == str(isolated.home)
    assert payload["has_fixture_key"] is True
    assert result.provider == "mimo"
    assert result.session_id == "fixture-session"
    assert result.usage.total_tokens == 2
