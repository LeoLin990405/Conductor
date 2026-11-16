"""Local CLI participant argv and reply parsing. No real CLIs are spawned."""

from __future__ import annotations

import json

import pytest

from src.server.services.deliberation.cli_adapter import (
    LocalCliParticipantAdapter,
    command_argv,
    parse_participant_output,
)
from src.server.services.deliberation.errors import DeliberationError


def _three_step_plan() -> dict:
    return {
        "summary": "s",
        "steps": [
            {
                "step_key": "spec",
                "title": "Write the spec",
                "prompt": "keep {brace",
                "assigned_agent": "codex",
                "depends_on": [],
            },
            {
                "step_key": "impl",
                "title": "Implement",
                "prompt": "p",
                "assigned_agent": "kimi",
                "depends_on": [],
            },
            {
                "step_key": "review",
                "title": "Review",
                "prompt": "p",
                "assigned_agent": "grok",
                "depends_on": ["spec", "impl"],
            },
        ],
    }


def test_codex_argv_reads_prompt_on_stdin():
    prompt = "do not put this token in argv"
    binary = "/usr/local/bin/codex"
    argv, stdin = command_argv("codex", binary, prompt)
    assert argv == [
        binary,
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "-",
    ]
    assert argv[:2] == [binary, "exec"]
    assert argv[-1] == "-"
    assert stdin == prompt
    assert prompt not in argv
    assert "--sandbox" in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"


def test_kimi_argv_passes_prompt_with_print_flag():
    prompt = "hello from kimi"
    argv, stdin = command_argv("kimi", "/bin/kimi", prompt)
    assert argv == ["/bin/kimi", "-p", prompt]
    assert stdin is None


def test_grok_argv_passes_prompt_with_print_flag():
    prompt = "hello from grok"
    argv, stdin = command_argv("grok", "/bin/grok", prompt)
    assert argv == ["/bin/grok", "-p", prompt]
    assert stdin is None


def test_unknown_provider_raises():
    with pytest.raises(DeliberationError) as exc_info:
        command_argv("claude", "claude", "hi")
    assert exc_info.value.code == "unknown_provider"


def test_parse_plan_json_ballot_and_nested_braces():
    plan = _three_step_plan()
    text = (
        "BODY: Ship it.\n"
        "CRITIQUE: Tight scope.\n"
        f"PLAN_JSON: {json.dumps(plan)}\n"
        "trailing }\n"
        "BALLOT: agree\n"
        "COMMENT: ready\n"
    )
    parsed = parse_participant_output(text, agent_key="codex")
    assert parsed["body"] == "Ship it."
    assert parsed["critique"] == "Tight scope."
    assert parsed["comment"] == "ready"
    assert parsed["ballot"] == "agree"
    assert len(parsed["plan"]["steps"]) == 3
    assert [step["step_key"] for step in parsed["plan"]["steps"]] == [
        "spec",
        "impl",
        "review",
    ]
    assert parsed["plan"]["steps"][0]["prompt"] == "keep {brace"
    assert parsed["plan"]["steps"][2]["depends_on"] == ["spec", "impl"]


def test_parse_unwraps_grok_json_wrapper():
    plan = {
        "summary": "s",
        "steps": [
            {
                "step_key": "work",
                "title": "t",
                "prompt": "p",
                "assigned_agent": "grok",
                "depends_on": [],
            }
        ],
    }
    inner = "BODY: hi\nPLAN_JSON: " + json.dumps(plan)
    parsed = parse_participant_output(json.dumps({"text": inner}), agent_key="grok")
    assert parsed["body"] == "hi"
    assert parsed["plan"]["steps"][0]["step_key"] == "work"
    assert parsed["plan"]["summary"] == "s"

    parsed = parse_participant_output(json.dumps({"result": inner}), agent_key="grok")
    assert parsed["body"] == "hi"
    assert len(parsed["plan"]["steps"]) == 1


def test_missing_plan_falls_back_to_one_agent_step():
    text = "C" * 2500
    parsed = parse_participant_output(text, agent_key="kimi")
    assert parsed["body"] == "C" * 2000
    assert parsed["plan"]["summary"] == "C" * 200
    step = parsed["plan"]["steps"][0]
    assert step["step_key"] == "kimi"
    assert step["title"] == "kimi step"
    assert step["prompt"] == "C" * 500
    assert step["assigned_agent"] == "kimi"
    assert step["depends_on"] == []

    invalid = 'BODY: hi\nPLAN_JSON: {"summary":"s","steps":"nope"}\n'
    parsed = parse_participant_output(invalid, agent_key="codex")
    assert parsed["body"] == "hi"
    assert parsed["plan"]["steps"][0]["assigned_agent"] == "codex"
    assert parsed["plan"]["steps"][0]["step_key"] == "codex"


def test_missing_ballot_stays_none():
    parsed = parse_participant_output(
        "BODY: hello\nCOMMENT: noted\n", agent_key="codex"
    )
    assert parsed["ballot"] is None
    assert parsed["comment"] == "noted"
    assert parsed["critique"] == ""


def test_binary_resolution(monkeypatch):
    explicit = LocalCliParticipantAdapter(agent_key="codex", binary="/opt/codex")
    assert explicit.binary() == "/opt/codex"

    monkeypatch.setenv("NATIVE_CLI_CODEX_BIN", "/from/env/codex")
    from_env = LocalCliParticipantAdapter(agent_key="codex")
    assert from_env.binary() == "/from/env/codex"

    monkeypatch.delenv("NATIVE_CLI_GROK_BIN", raising=False)
    monkeypatch.setattr(
        "src.server.services.deliberation.cli_adapter.shutil.which",
        lambda name: "/usr/bin/grok" if name == "grok" else None,
    )
    assert LocalCliParticipantAdapter(agent_key="grok").binary() == "/usr/bin/grok"

    monkeypatch.delenv("NATIVE_CLI_KIMI_BIN", raising=False)
    monkeypatch.setattr(
        "src.server.services.deliberation.cli_adapter.shutil.which",
        lambda name: None,
    )
    with pytest.raises(DeliberationError) as exc_info:
        LocalCliParticipantAdapter(agent_key="kimi").binary()
    assert exc_info.value.code == "cli_unavailable"


@pytest.mark.asyncio
async def test_propose_with_fake_runner_returns_plan():
    seen: dict = {}
    plan = _three_step_plan()

    async def runner(argv, stdin, cwd, timeout):
        seen.update(argv=argv, stdin=stdin, cwd=cwd, timeout=timeout)
        return "BODY: ready\nCRITIQUE: none\nPLAN_JSON: " + json.dumps(plan) + "\n"

    adapter = LocalCliParticipantAdapter(
        agent_key="codex",
        binary="/usr/local/bin/codex",
        cwd="/tmp/work",
        timeout=30,
        runner=runner,
        daemon_id="daemon-1",
    )
    result = await adapter.propose({"objective": "ship it"})
    assert adapter.daemon_id == "daemon-1"
    assert adapter.provider == "codex"
    assert seen["argv"][1] == "exec"
    assert seen["argv"][0] == "/usr/local/bin/codex"
    assert seen["argv"][-1] == "-"
    assert seen["cwd"] == "/tmp/work"
    assert seen["timeout"] == 30
    assert "ship it" in seen["stdin"]
    assert "Do not modify files" in seen["stdin"]
    assert '"depends_on":["spec","impl"]' in seen["stdin"]
    assert all("ship it" not in arg for arg in seen["argv"])
    assert result["body"] == "ready"
    assert result["critique"] == "none"
    assert [step["step_key"] for step in result["plan"]["steps"]] == [
        "spec",
        "impl",
        "review",
    ]


@pytest.mark.asyncio
async def test_critique_keeps_prompt_under_limit():
    seen: dict = {}

    async def runner(argv, stdin, cwd, timeout):
        seen["argv"] = argv
        seen["stdin"] = stdin
        return "BODY: revised\nCRITIQUE: overlap\n"

    adapter = LocalCliParticipantAdapter(agent_key="grok", binary="grok", runner=runner)
    transcript = [
        {"agent_key": "codex", "kind": "propose", "body": "A" * 20000},
        {"agent_key": "kimi", "kind": "critique", "body": "short"},
    ]
    result = await adapter.critique({"objective": "ship it", "transcript": transcript})
    prompt = seen["argv"][2]
    assert seen["stdin"] is None
    assert seen["argv"][:2] == ["grok", "-p"]
    assert len(prompt) < 12000
    assert "Do not modify files" in prompt
    assert "CRITIQUE:" in prompt
    assert '"depends_on":["spec","impl"]' in prompt
    assert "kimi critique: short" in prompt
    assert "A" * 20000 not in prompt
    assert result["body"] == "revised"
    assert result["critique"] == "overlap"
    assert result["plan"]["steps"][0]["assigned_agent"] == "grok"


@pytest.mark.asyncio
async def test_vote_without_ballot_line_dissents():
    seen: dict = {}

    async def runner(argv, stdin, cwd, timeout):
        seen["argv"] = argv
        seen["stdin"] = stdin
        return "nope"

    adapter = LocalCliParticipantAdapter(
        agent_key="kimi", binary="/bin/kimi", runner=runner
    )
    result = await adapter.vote({"objective": "x"}, {"summary": "s"})
    assert result == {"ballot": "dissent", "comment": "no ballot line"}
    assert seen["argv"][:2] == ["/bin/kimi", "-p"]
    assert seen["stdin"] is None
    assert "BALLOT: agree" in seen["argv"][2]
    assert "Otherwise BALLOT: dissent." in seen["argv"][2]


@pytest.mark.asyncio
async def test_vote_keeps_parsed_agree():
    async def runner(argv, stdin, cwd, timeout):
        return "BALLOT: agree\nCOMMENT: steps line up\n"

    adapter = LocalCliParticipantAdapter(agent_key="grok", binary="grok", runner=runner)
    result = await adapter.vote({}, {"steps": []})
    assert result == {"ballot": "agree", "comment": "steps line up"}


@pytest.mark.asyncio
async def test_synthesize_raises_bad_role():
    adapter = LocalCliParticipantAdapter(agent_key="codex", binary="codex")
    with pytest.raises(DeliberationError) as exc_info:
        await adapter.synthesize({"objective": "x"})
    assert exc_info.value.code == "bad_role"
