"""N61 re-review: judgments never reach the platform key; one shared recorder.

1. ``LLMService.complete(require_user_credentials=True)`` refuses before any
   client is built when the user has no BYOK/OAuth credential; the judgment
   backend always asks for that and the port records ``no_user_credentials``.
   Other callers keep the platform fallback.
2. ``default_recorder()`` is one per path for the process; its writer thread
   is a daemon and ``drain`` is bounded, so a stuck disk cannot hang exit.
"""

from __future__ import annotations

import asyncio
import importlib.util
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ptc_agent.config.agent import CredentialSource
from src.server.services.judgment import (
    COMPLETION_EVIDENCE,
    BoolQuestion,
    JudgmentPort,
    JudgmentRequest,
    JudgmentStatus,
    LLMJudgmentBackend,
    MemoryRecorder,
)
from src.server.services.judgment import port as port_mod
from src.server.services.judgment import record as record_mod
from src.server.services.judgment.record import JsonlRecorder, default_recorder
from src.server.services.llm import service as service_mod
from src.server.services.llm.service import LLMService, UserCredentialsRequired

from .fakes import FULL_SAMPLE


def _wire_service(monkeypatch, source):
    calls: dict[str, int] = {"create_llm": 0, "api": 0}

    async def fake_resolve(**kwargs):
        return SimpleNamespace(
            llm_client=object() if source == CredentialSource.PLATFORM else None,
            llm=SimpleNamespace(),
            credential_source=source,
        )

    def fake_create_llm(*args, **kwargs):
        calls["create_llm"] += 1
        return object()

    async def fake_api(*args, **kwargs):
        calls["api"] += 1
        return "ok"

    monkeypatch.setattr(service_mod, "resolve_llm_config", fake_resolve)
    monkeypatch.setattr(service_mod, "create_llm", fake_create_llm)
    monkeypatch.setattr(service_mod, "make_api_call", fake_api)
    monkeypatch.setattr(service_mod, "slot_model", lambda llm, slot: "m")
    return LLMService(agent_config=SimpleNamespace(llm=SimpleNamespace())), calls


@pytest.mark.parametrize("source", [CredentialSource.PLATFORM, "none"])
def test_complete_refuses_platform_fallback_when_required(monkeypatch, source):
    service, calls = _wire_service(monkeypatch, source)
    with pytest.raises(UserCredentialsRequired):
        asyncio.run(
            service.complete(user_id="u1", user_prompt="x", require_user_credentials=True)
        )
    assert calls == {"create_llm": 0, "api": 0}


def test_complete_requires_a_user_when_required(monkeypatch):
    service, calls = _wire_service(monkeypatch, CredentialSource.BYOK)
    with pytest.raises(UserCredentialsRequired):
        asyncio.run(
            service.complete(user_id=None, user_prompt="x", require_user_credentials=True)
        )
    assert calls == {"create_llm": 0, "api": 0}


@pytest.mark.parametrize("source", [CredentialSource.BYOK, CredentialSource.OAUTH])
def test_complete_runs_with_user_credentials_when_required(monkeypatch, source):
    service, calls = _wire_service(monkeypatch, source)
    out = asyncio.run(
        service.complete(user_id="u1", user_prompt="x", require_user_credentials=True)
    )
    assert out == "ok" and calls["api"] == 1


def test_other_callers_keep_the_platform_fallback(monkeypatch):
    service, calls = _wire_service(monkeypatch, CredentialSource.PLATFORM)
    assert asyncio.run(service.complete(user_id="u1", user_prompt="x")) == "ok"
    assert calls["api"] == 1


class _RefusingLLM:
    def __init__(self):
        self.calls = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        raise UserCredentialsRequired("credential_source=platform")


def _request():
    return JudgmentRequest(
        use=COMPLETION_EVIDENCE,
        version="1",
        state={"g2_output": "ok"},
        questions={"report_supported": BoolQuestion("Supported?")},
        user_id="u1",
    )


def test_backend_asks_for_user_credentials_and_port_records_skip():
    llm = _RefusingLLM()
    recorder = MemoryRecorder()
    port = JudgmentPort(LLMJudgmentBackend(llm), recorder=recorder, retries=2, environ=FULL_SAMPLE)
    result = asyncio.run(port.judge(_request(), action="a", sample_key="k"))
    assert len(llm.calls) == 1  # a skip is not retried
    assert llm.calls[0]["require_user_credentials"] is True
    assert result.status == JudgmentStatus.INCONCLUSIVE
    assert result.reason == "no_user_credentials"
    assert result.probabilities == {}
    (rec,) = recorder.records
    assert rec.reason == "no_user_credentials" and rec.status == "inconclusive"


def test_eval_counts_no_user_credentials_as_skipped():
    path = Path(__file__).resolve().parents[5] / "scripts" / "judgment_eval.py"
    spec = importlib.util.spec_from_file_location("judgment_eval_n61", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "no_user_credentials" in mod.SKIP_REASONS


def test_default_recorder_is_shared_per_path(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTOR_JUDGMENT_RECORDS", str(tmp_path / "shared.jsonl"))
    first = default_recorder()
    assert default_recorder() is first
    monkeypatch.setattr(port_mod, "default_recorder", default_recorder)
    assert port_mod.default_port().recorder is port_mod.default_port().recorder is first


def test_writer_thread_is_a_daemon_and_drain_is_bounded(tmp_path, monkeypatch):
    recorder = JsonlRecorder(tmp_path / "r.jsonl")
    release = threading.Event()
    monkeypatch.setattr(recorder, "_append", lambda line: release.wait(10))
    record = record_mod.JudgmentRecord(
        use="u", version="1", mode="shadow", question_ids=[], status="pass",
        question_status={}, answers={}, probabilities={}, latency_ms=0,
        backend="fake", reason="", action="", state_digest={},
    )

    async def run():
        recorder.write(record)
        started = time.monotonic()
        await recorder.drain(0.1)
        return time.monotonic() - started

    try:
        elapsed = asyncio.run(run())
        assert elapsed < 2.0
        workers = [t for t in threading.enumerate() if t.name == "judgment-record"]
        assert workers and all(t.daemon for t in workers)
        started = time.monotonic()
        recorder.flush(0.1)
        assert time.monotonic() - started < 2.0
    finally:
        release.set()


def test_drain_judgments_drains_the_shared_recorders(monkeypatch):
    from src.server.app import setup

    seen: list[float] = []

    async def fake_drain_shared(timeout=3.0):
        seen.append(timeout)

    monkeypatch.setattr(record_mod, "drain_shared", fake_drain_shared)
    asyncio.run(setup.drain_judgments(0.25))
    assert seen == [0.25]
