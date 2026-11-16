import asyncio
import json

import pytest

from src.server.services.judgment import (
    BoolQuestion,
    ChoiceQuestion,
    JudgmentPort,
    JudgmentRequest,
    JudgmentResult,
    JudgmentStatus,
    MemoryRecorder,
    ScoreQuestion,
    WATCHDOG_PROGRESS,
    default_port,
)

from .fakes import FULL_SAMPLE, FakeBackend

P, F, I, E = (
    JudgmentStatus.PASS,
    JudgmentStatus.FAIL,
    JudgmentStatus.INCONCLUSIVE,
    JudgmentStatus.ERROR,
)
SECRET = "the material must never reach a record 12345"
OPTIONS = ("progressing", "stalled", "off_track", "needs_verification")


def _req(questions, use=WATCHDOG_PROGRESS, user_id="u1"):
    return JudgmentRequest(
        use=use, version="1", state={"report": SECRET}, questions=questions,
        user_id=user_id,
    )


def _judge(backend, questions, *, env=None, recorder=None, **kw):
    recorder = recorder if recorder is not None else MemoryRecorder()
    port = JudgmentPort(
        backend, recorder=recorder, environ={**FULL_SAMPLE, **(env or {})},
        budget_s=kw.pop("budget_s", 2.0), retries=kw.pop("retries", 1),
    )
    request = _req(questions, user_id=kw.pop("user_id", "u1"))
    result = asyncio.run(port.judge(request, **kw))
    return result, recorder


CHOICE = {"progress": ChoiceQuestion("How is it going?", OPTIONS)}
SCORE = {"quality": ScoreQuestion("How good?", ("low", "mid", "high"))}
BOOL = {"ok": BoolQuestion("Is it ok?")}


@pytest.mark.parametrize(
    ("dist", "status", "answer"),
    [
        ({"progressing": 0.8, "stalled": 0.2}, P, "progressing"),  # top == hi, margin 0.6
        ({"progressing": 0.79, "stalled": 0.21}, I, None),  # just under hi
        ({"progressing": 1.0}, P, "progressing"),  # absent candidates are 0
        ({"progressing": 0.5, "stalled": 0.5}, I, None),  # tie
        ({"stalled": 0.9, "off_track": 0.1}, P, "stalled"),
        ({"progressing": 0.25, "stalled": 0.25, "off_track": 0.25, "needs_verification": 0.25}, I, None),
    ],
)
def test_choice_mapping(dist, status, answer):
    result, _ = _judge(FakeBackend({"progress": dist}), CHOICE)
    assert result.question_status["progress"] is status
    assert result.status is status
    assert result.answers["progress"] == answer
    assert result.probabilities["progress"]["progressing"] == dist.get("progressing", 0.0)


def test_choice_margin_boundary_with_a_custom_use(monkeypatch):
    from src.server.services.judgment import port as port_mod
    from src.server.services.judgment.uses import Fallback, UseSpec

    spec = UseSpec("wide", "1", 0.2, 0.5, 0.15, Fallback.KEEP_PATH)
    monkeypatch.setattr(port_mod, "get_use", lambda name: spec)
    # margin exactly 0.15 -> PASS; 0.14 -> INCONCLUSIVE even though top >= hi
    at, _ = _judge(FakeBackend({"progress": {"progressing": 0.575, "stalled": 0.425}}), CHOICE)
    under, _ = _judge(FakeBackend({"progress": {"progressing": 0.57, "stalled": 0.43}}), CHOICE)
    assert at.status is P and at.answers["progress"] == "progressing"
    assert under.status is I and under.answers["progress"] is None


@pytest.mark.parametrize(
    ("dist", "status", "answer"),
    [
        ({"high": 0.85, "mid": 0.15}, P, "high"),
        ({"low": 0.8, "mid": 0.1, "high": 0.1}, P, "low"),
        ({"high": 0.7, "mid": 0.3}, I, None),
        ({"high": 0.45, "mid": 0.45, "low": 0.1}, I, None),
    ],
)
def test_score_maps_like_choice(dist, status, answer):
    result, _ = _judge(FakeBackend({"quality": dist}), SCORE)
    assert (result.status, result.answers["quality"]) == (status, answer)


@pytest.mark.parametrize(
    ("p", "status", "answer"),
    [
        (0.8, P, True),
        (0.95, P, True),
        (0.7999, I, None),
        (0.5, I, None),
        (0.2001, I, None),
        (0.2, F, False),
        (0.0, F, False),
    ],
)
def test_bool_mapping(p, status, answer):
    result, _ = _judge(FakeBackend({"ok": {"true": p}}), BOOL)
    assert (result.status, result.answers["ok"]) == (status, answer)
    assert result.probabilities["ok"] == {"true": p}


def test_overall_status_precedence():
    qs = {"a": BoolQuestion("a"), "b": BoolQuestion("b"), "c": BoolQuestion("c")}
    r, _ = _judge(FakeBackend({"a": {"true": 0.9}, "b": {"true": 0.5}, "c": {"true": 0.9}}), qs)
    assert r.status is I
    r, _ = _judge(FakeBackend({"a": {"true": 0.9}, "b": {"true": 0.5}, "c": {"true": 0.1}}), qs)
    assert r.status is F
    r, _ = _judge(FakeBackend({"a": {"true": 0.9}, "b": {"true": 0.1}, "c": "junk"}), qs)
    assert r.status is E
    assert r.question_status == {"a": P, "b": F, "c": E}
    assert "c" not in r.probabilities
    r, _ = _judge(FakeBackend({k: {"true": 0.9} for k in qs}), qs)
    assert r.status is P


@pytest.mark.parametrize(
    "garbage",
    [
        None,
        "pass",
        [],
        {},
        {"progress": {}},
        {"progress": {"progressing": 1.2}},
        {"progress": {"progressing": -0.1}},
        {"progress": {"progressing": float("nan")}},
        {"progress": {"progressing": True}},
        {"progress": {"progressing": "0.9"}},
        {"progress": {"done": 0.9}},
        {"progress": {"progressing": 0.7, "stalled": 0.7}},
        {"other": {"progressing": 0.9}},
    ],
)
def test_malformed_output_is_error_not_zero(garbage):
    result, rec = _judge(FakeBackend(garbage), CHOICE)
    assert result.status is E
    assert result.answers["progress"] is None
    assert result.probabilities == {}
    assert result.reason.startswith("invalid_output")
    assert len(rec.records) == 1 and rec.records[0].status == "error"


def test_bool_garbage_is_error():
    for garbage in ({"ok": {"false": 0.9}}, {"ok": {"true": 0.9, "false": 0.1}}, {"ok": 0.9}):
        result, _ = _judge(FakeBackend(garbage), BOOL)
        assert result.status is E and result.probabilities == {}


def test_exception_is_retried_then_error():
    backend = FakeBackend(RuntimeError("boom"), RuntimeError("boom"))
    result, rec = _judge(backend, CHOICE, retries=1)
    assert backend.calls == 2
    assert result.status is E
    assert result.reason == "backend_error:RuntimeError"
    assert result.probabilities == {}
    assert rec.records[0].reason == "backend_error:RuntimeError"


def test_retry_recovers():
    backend = FakeBackend(ValueError("x"), {"ok": {"true": 0.9}})
    result, _ = _judge(backend, BOOL, retries=1)
    assert backend.calls == 2 and result.status is P


def test_timeout_is_error_and_budget_covers_retries():
    backend = FakeBackend({"ok": {"true": 0.9}}, delay=0.3)
    result, rec = _judge(backend, BOOL, budget_s=0.05, retries=3)
    assert result.status is E and result.reason == "timeout"
    assert backend.calls == 1
    assert rec.records[0].reason == "timeout"


def test_not_implemented_backend_is_error():
    from src.server.services.judgment import LocalJudgmentBackend, unavailable_reason

    result, _ = _judge(LocalJudgmentBackend(), BOOL)
    assert result.status is E and result.backend == "local"
    assert result.reason == "backend_error:NotImplementedError"
    assert "not implemented" in unavailable_reason()
    assert LocalJudgmentBackend().unavailable_reason() == unavailable_reason()


def test_backend_none_is_recorded_error():
    result, rec = _judge(None, BOOL)
    assert result.status is E and result.reason == "backend_unavailable"
    assert result.backend == "none"
    assert len(rec.records) == 1


def test_off_never_calls_backend_and_records_nothing():
    backend = FakeBackend({"ok": {"true": 0.9}})
    env = {"CONDUCTOR_JUDGMENT_WATCHDOG_PROGRESS_MODE": "off"}
    result, rec = _judge(backend, BOOL, env=env)
    assert backend.calls == 0 and rec.records == []
    assert result.status is I and result.reason == "mode_off" and result.backend == "none"


def test_out_of_sample_never_calls_backend():
    backend = FakeBackend({"ok": {"true": 0.9}})
    env = {"CONDUCTOR_JUDGMENT_WATCHDOG_PROGRESS_SAMPLE": "0"}
    result, rec = _judge(backend, BOOL, env=env, sample_key="t1")
    assert backend.calls == 0 and rec.records == []
    assert result.status is I and result.reason == "not_sampled"


def test_unknown_use_is_error_and_port_never_raises():
    port = JudgmentPort(
        FakeBackend({"ok": {"true": 0.9}}), recorder=MemoryRecorder(), environ=FULL_SAMPLE
    )
    result = asyncio.run(port.judge(_req(BOOL, use="unknown")))
    assert result.status is E and result.reason == "unknown_use"

    class Broken:
        use = WATCHDOG_PROGRESS
        version = "1"
        state = {}
        user_id = "u1"
        questions = None  # not a mapping

    result = asyncio.run(port.judge(Broken()))  # type: ignore[arg-type]
    assert result.status is E


def test_recorder_failure_does_not_raise():
    class Exploding:
        def write(self, record):
            raise OSError("disk full")

    result, _ = _judge(FakeBackend({"ok": {"true": 0.9}}), BOOL, recorder=Exploding())
    assert result.status is P


def test_every_judgment_writes_one_record_without_material():
    rec = MemoryRecorder()
    for output in ({"ok": {"true": 0.9}}, RuntimeError("x"), "junk"):
        _judge(FakeBackend(output, output), BOOL, recorder=rec, action="review", subject="T-1")
    assert len(rec.records) == 3
    for record in rec.records:
        blob = json.dumps(record.to_dict())
        assert SECRET not in blob
        assert record.state_digest["report"]["len"] == len(SECRET)
        assert len(record.state_digest["report"]["sha256"]) == 64
        assert record.action == "review" and record.subject == "T-1"
        assert record.mode == "shadow" and record.use == WATCHDOG_PROGRESS
        assert record.question_ids == ["ok"]
    first = rec.records[0].to_dict()
    assert first["status"] == "pass" and first["answers"] == {"ok": True}
    assert first["probabilities"] == {"ok": {"true": 0.9}} and first["backend"] == "fake"


def test_error_helper():
    r = JudgmentResult.error("x", "why", 5, ["a", "b"])
    assert r.status is E and r.answers == {"a": None, "b": None}
    assert r.question_status == {"a": E, "b": E} and r.probabilities == {}


def test_mode_for_uses_the_port_environ():
    port = JudgmentPort(None, environ={"CONDUCTOR_JUDGMENT_WATCHDOG_PROGRESS_MODE": "enforce"})
    assert port.mode_for(WATCHDOG_PROGRESS) == "enforce"


def test_default_port_without_llm_service(monkeypatch, tmp_path):
    from src.server.app import setup

    monkeypatch.setattr(setup, "llm_service", None, raising=False)
    for name, value in FULL_SAMPLE.items():
        monkeypatch.setenv(name, value)
    port = default_port()
    assert port.backend is None
    assert port.recorder.path == tmp_path / "records.jsonl"

    async def run():
        result = await port.judge(_req(BOOL))
        await port.drain()
        return result

    result = asyncio.run(run())
    assert result.reason == "backend_unavailable"
    assert (tmp_path / "records.jsonl").exists()


def test_default_port_with_llm_service(monkeypatch):
    from src.server.app import setup
    from src.server.services.judgment import LLMJudgmentBackend, MemoryRecorder

    monkeypatch.setattr(setup, "llm_service", object(), raising=False)
    rec = MemoryRecorder()
    port = default_port(recorder=rec)
    assert isinstance(port.backend, LLMJudgmentBackend) and port.recorder is rec


@pytest.mark.parametrize("user_id", [None, "", "   "])
def test_no_user_never_calls_backend_and_is_recorded(user_id):
    backend = FakeBackend({"ok": {"true": 0.9}})
    result, rec = _judge(backend, BOOL, user_id=user_id)
    assert backend.calls == 0
    assert result.status is I and result.reason == "no_user" and result.backend == "none"
    assert result.answers == {"ok": None} and result.probabilities == {}
    assert len(rec.records) == 1
    assert rec.records[0].reason == "no_user" and rec.records[0].status == "inconclusive"


def test_global_kill_switch_turns_every_use_off():
    backend = FakeBackend({"ok": {"true": 0.9}})
    env = {"CONDUCTOR_JUDGMENT_MODE": "off", "CONDUCTOR_JUDGMENT_WATCHDOG_PROGRESS_MODE": "enforce"}
    result, rec = _judge(backend, BOOL, env=env)
    assert backend.calls == 0 and rec.records == []
    assert result.reason == "mode_off"


def test_default_sample_rate_skips_most_keys():
    backend = FakeBackend({"ok": {"true": 0.9}})
    port = JudgmentPort(backend, recorder=MemoryRecorder(), environ={})
    reasons = [
        asyncio.run(port.judge(_req(BOOL), sample_key=f"t{i}")).reason for i in range(400)
    ]
    judged = sum(1 for r in reasons if r != "not_sampled")
    assert 0.05 < judged / len(reasons) < 0.15
    assert backend.calls == judged


def test_port_drain_waits_for_the_jsonl_recorder(tmp_path):
    from src.server.services.judgment import JsonlRecorder

    path = tmp_path / "drained.jsonl"
    port = JudgmentPort(
        FakeBackend({"ok": {"true": 0.9}}), recorder=JsonlRecorder(path), environ=FULL_SAMPLE
    )

    async def run():
        for _ in range(20):
            await port.judge(_req(BOOL))
        await port.drain()

    asyncio.run(run())
    assert len(path.read_text().splitlines()) == 20
    asyncio.run(JudgmentPort(None).drain())  # a recorder without drain is fine
