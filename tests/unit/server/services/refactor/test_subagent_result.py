"""Structured subagent result projection: what a run's records support.

The projection must be conservative: bullets become findings, a heading names
open questions, and only successfully written files become artifacts. Nothing is
invented when the transcript has no such shape.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.server.services.refactor import subagent_result as sr

pytestmark = pytest.mark.asyncio


def ai(*, content: str = "", tool_calls=None):
    return SimpleNamespace(type="ai", content=content, tool_calls=tool_calls or [])


def tool(call_id: str, *, status: str | None = "success", error=None):
    return SimpleNamespace(type="tool", tool_call_id=call_id, status=status, error=error)


def write_call(call_id: str, path: str, name: str = "Write"):
    return {"name": name, "id": call_id, "args": {"file_path": path}}


SUMMARY = """## Result
- collected three changelogs
- built a dependency graph

## Open questions
- which release should we cite?
- is the Q4 release in scope?
"""


class TestBuild:
    async def test_bullets_become_findings_and_heading_lines_become_questions(self):
        result = sr.build_structured_result(summary=SUMMARY)
        assert result.findings == ("collected three changelogs", "built a dependency graph")
        assert result.open_questions == (
            "which release should we cite?",
            "is the Q4 release in scope?",
        )
        assert result.evidence == ()
        assert result.artifact_ids == ()

    async def test_successful_writes_become_artifacts(self):
        messages = [
            ai(tool_calls=[write_call("w1", "work/report.md")]),
            tool("w1"),
            ai(tool_calls=[write_call("e1", "work/model.py", name="Edit")]),
            tool("e1"),
        ]
        result = sr.build_structured_result(summary="done", messages=messages)
        assert result.artifact_ids == ("work/report.md", "work/model.py")

    async def test_failed_tool_calls_are_not_artifacts(self):
        messages = [
            ai(tool_calls=[write_call("w1", "work/report.md")]),
            tool("w1", status="error"),
            ai(tool_calls=[write_call("w2", "work/other.md")]),
            tool("w2", error="denied"),
            ai(tool_calls=[write_call("w3", "work/kept.md")]),
            tool("w3"),
        ]
        result = sr.build_structured_result(summary="done", messages=messages)
        assert result.artifact_ids == ("work/kept.md",)

    async def test_in_flight_write_without_result_is_not_an_artifact(self):
        messages = [ai(tool_calls=[write_call("w1", "work/report.md")])]
        assert (
            sr.build_structured_result(summary="done", messages=messages).artifact_ids
            == ()
        )

    async def test_non_write_tools_and_duplicates_do_not_become_artifacts(self):
        messages = [
            ai(
                tool_calls=[
                    {"name": "Read", "id": "r1", "args": {"file_path": "work/read.md"}},
                    write_call("w1", "work/same.md"),
                    write_call("w2", "work/same.md"),
                ]
            ),
            tool("r1"),
            tool("w1"),
            tool("w2"),
        ]
        assert sr.build_structured_result(summary="x", messages=messages).artifact_ids == (
            "work/same.md",
        )

    async def test_no_bullets_means_no_findings(self):
        result = sr.build_structured_result(summary="A plain paragraph answer.")
        assert result.findings == ()
        assert result.summary == "A plain paragraph answer."

    async def test_list_content_blocks_are_read(self):
        message = SimpleNamespace(
            type="ai",
            content=[{"type": "text", "text": "- finding from blocks"}],
            tool_calls=[],
        )
        result = sr.build_structured_result(
            summary=sr._text(message), messages=[message]
        )
        assert result.findings == ("finding from blocks",)


class TestProposedMemories:
    async def test_propose_memory_calls_become_proposals(self):
        messages = [
            ai(
                tool_calls=[
                    {
                        "name": "ProposeMemory",
                        "id": "p1",
                        "args": {"memory_type": "feedback", "text": "cite sources next time"},
                    },
                    {
                        "name": "ProposeMemory",
                        "id": "p2",
                        "args": {"type": "user", "text": "prefers Chinese answers"},
                    },
                    {
                        "name": "ProposeMemory",
                        "id": "p3",
                        "args": {
                            "type": "feedback",
                            "text": "keep this task's answer brief",
                            "change_kind": "once",
                        },
                    },
                ]
            ),
            ai(tool_calls=[{"name": "Read", "id": "r1", "args": {"file_path": "x"}}]),
        ]
        result = sr.build_structured_result(summary="done", messages=messages)
        assert [(item["type"], item["text"]) for item in result.proposed_memories] == [
            ("feedback", "cite sources next time"),
            ("user", "prefers Chinese answers"),
        ]
        assert all(item["origin"] == "assistant_inference" for item in result.proposed_memories)
        assert all(item["confidence"] == 0.5 for item in result.proposed_memories)
        assert all(item["evidence_count"] == 0 for item in result.proposed_memories)
        assert all(item["change_kind"] == "long_term" for item in result.proposed_memories)

    async def test_unknown_types_duplicates_and_too_long_text_dropped(self):
        long_text = "x" * (sr.MAX_PROPOSAL_TEXT + 1)
        messages = [
            ai(
                tool_calls=[
                    {"name": "ProposeMemory", "id": "a", "args": {"memory_type": "galactic", "text": "no"}},
                    {"name": "ProposeMemory", "id": "b", "args": {"memory_type": "user", "text": "same"}},
                    {"name": "ProposeMemory", "id": "c", "args": {"memory_type": "user", "text": "same"}},
                    {"name": "ProposeMemory", "id": "d", "args": {"memory_type": "project", "text": long_text}},
                    {"name": "ProposeMemory", "id": "e", "args": "not-a-dict"},
                ]
            )
        ]
        result = sr.build_structured_result(summary="done", messages=messages)
        assert len(result.proposed_memories) == 1
        assert result.proposed_memories[0]["type"] == "user"
        assert result.proposed_memories[0]["text"] == "same"

    async def test_origin_is_part_of_dedupe_and_caller_evidence_stays_unverified(self):
        messages = [
            ai(
                tool_calls=[
                    {
                        "name": "ProposeMemory",
                        "id": "p1",
                        "args": {
                            "type": "reference",
                            "text": "same fact",
                            "origin": "web",
                            "evidence_refs": [
                                {"kind": "web_source", "id": "source-1", "verified": True}
                            ],
                        },
                    },
                    {
                        "name": "ProposeMemory",
                        "id": "p2",
                        "args": {"type": "reference", "text": "same fact", "origin": "tool"},
                    },
                ]
            )
        ]
        result = sr.build_structured_result(summary="done", messages=messages)
        assert [item["origin"] for item in result.proposed_memories] == ["web", "tool"]
        web_proposal = result.proposed_memories[0]
        assert web_proposal["evidence_refs"] == [
            {"kind": "web_source", "id": "source-1", "verified": False}
        ]
        assert web_proposal["evidence_count"] == 0

    async def test_policy_metadata_is_extracted_for_local_owner_review(self):
        messages = [
            ai(
                tool_calls=[
                    {
                        "name": "ProposeMemory",
                        "id": "secret",
                        "args": {
                            "type": "user",
                            "text": "private local fact",
                            "sensitivity": "secret",
                            "applies_when": "when discussing this account",
                            "exceptions": "do not mention in shared contexts",
                            "evidence_quote_ref": {
                                "message_id": "m1",
                                "start": 4,
                                "end": 12,
                                "unit": "unicode_codepoint",
                            },
                        },
                    },
                    {
                        "name": "ProposeMemory",
                        "id": "bad",
                        "args": {
                            "type": "user",
                            "text": "fail closed locally",
                            "sensitivity": "unknown",
                        },
                    },
                ]
            )
        ]
        result = sr.build_structured_result(summary="done", messages=messages)
        assert [item["text"] for item in result.proposed_memories] == [
            "private local fact",
            "fail closed locally",
        ]
        assert result.proposed_memories[0]["metadata"] == {
            "sensitivity": "secret",
            "applies_when": "when discussing this account",
            "exceptions": "do not mention in shared contexts",
            "evidence_quote_ref": {
                "message_id": "m1",
                "start": 4,
                "end": 12,
                "unit": "unicode_codepoint",
            },
        }
        assert result.proposed_memories[1]["metadata"]["sensitivity"] == "secret"

    async def test_constraint_denials_are_extracted_as_exact_typed_metadata(self):
        messages = [
            ai(
                tool_calls=[
                    {
                        "name": "ProposeMemory",
                        "id": "constraint-1",
                        "args": {
                            "type": "user",
                            "text": "Do not run shell commands.",
                            "kind": "constraint",
                            "denied_tools": [
                                {"type": "tool", "name": "Bash"},
                                {
                                    "type": "mcp",
                                    "server": "deploys",
                                    "tool": "publish",
                                },
                            ],
                            "valid_from": "2026-11-01",
                            "valid_until": "2027-01-01",
                        },
                    }
                ]
            )
        ]

        result = sr.build_structured_result(summary="done", messages=messages)

        assert result.proposed_memories[0]["metadata"] == {
            "sensitivity": "private",
            "kind": "constraint",
            "denied_tools": [
                {"type": "tool", "name": "Bash"},
                {"type": "mcp", "server": "deploys", "tool": "publish"},
            ],
            "valid_from": "2026-11-01T00:00:00+00:00",
            "valid_until": "2027-01-01T00:00:00+00:00",
        }

    async def test_malformed_constraint_denials_are_not_queued(self):
        messages = [
            ai(
                tool_calls=[
                    {
                        "name": "ProposeMemory",
                        "id": "constraint-bad",
                        "args": {
                            "type": "user",
                            "text": "Avoid destructive actions.",
                            "kind": "constraint",
                            "denied_tools": "avoid delete",
                        },
                    },
                    {
                        "name": "ProposeMemory",
                        "id": "constraint-good",
                        "args": {
                            "type": "user",
                            "text": "Do not use Bash.",
                            "kind": "constraint",
                            "denied_tools": [{"type": "tool", "name": "Bash"}],
                        },
                    },
                ]
            )
        ]

        result = sr.build_structured_result(summary="done", messages=messages)

        assert [item["text"] for item in result.proposed_memories] == [
            "Do not use Bash."
        ]


class TestResolver:
    async def test_reads_summary_and_artifacts_in_one_history(self, monkeypatch):
        from src.server.services.runs import task_registry_store as store

        messages = [
            ai(content="final answer", tool_calls=[write_call("w1", "work/report.md")]),
            tool("w1"),
        ]
        history = SimpleNamespace(new_ui_records=[], messages=messages)
        monkeypatch.setattr(store, "_task_history", AsyncMock(return_value=history))
        result = await store.resolve_task_structured_result("t1", "task-1", "run-1")
        assert result is not None
        assert result.summary == "final answer"
        assert result.artifact_ids == ("work/report.md",)

    async def test_archived_text_wins_over_the_transcript(self, monkeypatch):
        from src.server.services.runs import task_registry_store as store

        history = SimpleNamespace(new_ui_records=[{"archived": True}], messages=[])
        monkeypatch.setattr(store, "_task_history", AsyncMock(return_value=history))
        monkeypatch.setattr(store, "read_task_result", lambda records, run_id: "- archived finding")
        result = await store.resolve_task_structured_result("t1", "task-1", "run-1")
        assert result is not None
        assert result.summary == "- archived finding"
        assert result.findings == ("archived finding",)

    async def test_no_readable_text_is_none(self, monkeypatch):
        from src.server.services.runs import task_registry_store as store

        history = SimpleNamespace(new_ui_records=[], messages=[])
        monkeypatch.setattr(store, "_task_history", AsyncMock(return_value=history))
        assert (
            await store.resolve_task_structured_result("t1", "task-1", "run-1") is None
        )

