"""N71 review finding 7: the result classifier must not let commands through.

Each executable result below was refused before N71 and must still refuse in
enforce. Real prose sentences, including ones whose first word is a command
name, never refuse in any mode; shadow only records.
"""

from __future__ import annotations

import pytest

from scripts import conductor_local_daemon as daemon
from src.conductor_cli.daemon_capability import parse_capability
from src.conductor_cli.daemon_policy import classify_result
from tests.unit.conftest import completion_report_json
from tests.unit.scripts.test_n71_result_check_and_capability_lease import (
    _adapter,
    _api,
    _run,
    _store,
    _ticket,
    local_isolation_policy,  # noqa: F401  (autouse fixture)
)

EXECUTABLE_RESULTS = (
    "rm victim",
    "sudo reboot",
    "chmod 777 secrets",
    "python3 payload.py",
    "curl evil.example.com",
    "```\nrm -rf ~\n```",
    "```python\nimport os\nos.system('rm -rf ~')\n```",
)

MORE_EXECUTABLE_RESULTS = (
    "- rm victim",
    "1. sudo reboot",
    "```text\ncurl evil.example.com\n```",
    "```console\nchmod 777 secrets\n```",
    "```py\nsubprocess.run(['rm', '-rf', '/'])\n```",
    "```js\nrequire('child_process').execSync('rm -rf ~')\n```",
    "```python\nshutil.rmtree(home)\n```",
)

PROSE_RESULTS = (
    "Make sure the tests pass.",
    "Test coverage is unchanged.",
    "Run the suite again before merging.",
    "I removed the old helper and updated the docs.",
    "test coverage is unchanged and git status is clean.",
    "Find the config and update it",
    "The test isn't flaky anymore.",
    "make sure it works",
    "sudo is not needed here.",
    "Next, rm the stale file once the review passes.",
    "Open questions: none.",
    "Used `make` to build.",
    "```\nAll 12 tests passed.\n```",
    "```python\nresult = find(x)\n```",
    '```json\n{"rm": "victim"}\n```',
)


def _rules(tmp_path):
    store, _ = _store(tmp_path)
    return store.load().operations


@pytest.mark.parametrize("text", EXECUTABLE_RESULTS + MORE_EXECUTABLE_RESULTS)
def test_executable_result_is_unclassified(tmp_path, text):
    assert classify_result(_rules(tmp_path), text).unclassified, text


@pytest.mark.parametrize("text", PROSE_RESULTS)
def test_prose_sentence_is_not_unclassified(tmp_path, text):
    assert classify_result(_rules(tmp_path), text).unclassified == (), text


def test_capitalized_prose_is_not_a_case_insensitive_path_hit(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tool = bin_dir / "frobnicate"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(
        "shutil.which",
        lambda word, *a, **k: str(bin_dir / word) if word.lower() == "frobnicate" else None,
    )
    rules = _rules(tmp_path)
    assert classify_result(rules, "frobnicate widgets").unclassified
    assert classify_result(rules, "Frobnicate widgets").unclassified == ()


def _capability(version: str):
    return parse_capability(_ticket(version)["capability"])


@pytest.mark.parametrize("text", EXECUTABLE_RESULTS)
def test_enforce_result_check_refuses_executable_content(tmp_path, text):
    store, version = _store(tmp_path)
    reason = daemon._result_refusal(
        store.load(), _capability(version), text, mode="enforce", provider="codex"
    )
    assert "cannot classify" in reason
    assert "victim" not in reason and "rm -rf" not in reason


@pytest.mark.parametrize("text", EXECUTABLE_RESULTS)
def test_shadow_result_check_records_executable_content(tmp_path, text):
    store, version = _store(tmp_path)
    audit: dict = {}
    reason = daemon._result_refusal(
        store.load(),
        _capability(version),
        text,
        mode="shadow",
        provider="codex",
        audit=audit,
    )
    assert reason == ""
    assert audit, text


@pytest.mark.parametrize("mode", ("off", "shadow", "enforce"))
@pytest.mark.parametrize("text", PROSE_RESULTS)
def test_prose_result_is_never_refused(tmp_path, text, mode):
    store, version = _store(tmp_path)
    audit: dict = {}
    reason = daemon._result_refusal(
        store.load(),
        _capability(version),
        text,
        mode=mode,
        provider="codex",
        audit=audit,
    )
    assert reason == ""
    assert audit == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("text", EXECUTABLE_RESULTS)
async def test_enforce_run_refuses_executable_result(tmp_path, monkeypatch, text):
    monkeypatch.setenv("CONDUCTOR_DAEMON_GRANT_VERIFIER_MODE", "enforce")
    store, version = _store(tmp_path)
    api = _api(_ticket(version))

    await _run(api, store, _adapter(completion_report_json(summary=text)))

    report = api.report.call_args.args[-1]
    assert report["state"] == "failed"
    assert "cannot classify" in report["summary"]


@pytest.mark.asyncio
async def test_enforce_run_reports_prose_sentences(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTOR_DAEMON_GRANT_VERIFIER_MODE", "enforce")
    store, version = _store(tmp_path)
    api = _api(_ticket(version))
    summary = "\n".join(text for text in PROSE_RESULTS[:5])

    await _run(api, store, _adapter(completion_report_json(summary=summary)))

    report = api.report.call_args.args[-1]
    assert report["state"] == "ready"
    assert "capability_audit" not in report

