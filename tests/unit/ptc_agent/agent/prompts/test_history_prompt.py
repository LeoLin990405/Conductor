"""Where past threads reach only the file tools, both roles' prompts say so."""

import pytest

from ptc_agent.agent.prompts import get_loader

_BASE = dict(current_time="2026-10-03 11:00 ET", subagent_summary="", workspace_folder="ACME")
_ONLY_FILE_TOOLS = (
    "本机上只有 Read、Grep、Glob 能访问这些文件",
    "On this computer only Read, Grep and Glob reach them",
)


@pytest.mark.parametrize("role", ["generalist", "chief_of_staff"])
def test_only_a_computer_without_the_mount_sends_history_to_the_file_tools(role):
    loader = get_loader()

    def prompt(**flags):
        return loader.get_system_prompt(**_BASE, role=role, **flags)

    unmounted = prompt(history_files=True)
    assert "../.agents/threads.jsonl" in unmounted and any(t in unmounted for t in _ONLY_FILE_TOOLS)
    mounted = prompt(files_mounted=True)
    assert "../.agents/threads.jsonl" in mounted and not any(t in mounted for t in _ONLY_FILE_TOOLS)
    assert "../.agents/threads.jsonl" not in prompt()
