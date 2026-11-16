import json

from src.server.services.judgment import (
    JsonlRecorder,
    JudgmentRecord,
    default_recorder,
    digest_state,
)
from src.server.services.judgment.record import default_records_path


def _record(**kw):
    base = dict(
        use="u", version="1", mode="shadow", question_ids=["q"], status="pass",
        question_status={"q": "pass"}, answers={"q": True},
        probabilities={"q": {"true": 0.9}}, latency_ms=3, backend="fake",
        reason="", action="kept", state_digest=digest_state({"a": "xyz"}),
    )
    base.update(kw)
    return JudgmentRecord(**base)


def test_digest_state_is_hash_and_length_only():
    d = digest_state({"a": "hello"})
    assert d == {
        "a": {
            "sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
            "len": 5,
        }
    }


def test_jsonl_recorder_creates_directory_lazily(tmp_path):
    path = tmp_path / "deep" / "dir" / "records.jsonl"
    recorder = JsonlRecorder(path)
    assert not path.parent.exists()
    recorder.write(_record())
    recorder.write(_record(status="error"))
    lines = path.read_text().splitlines()
    assert [json.loads(line)["status"] for line in lines] == ["pass", "error"]
    assert json.loads(lines[0])["ts"]


def test_jsonl_recorder_swallows_write_failures(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    JsonlRecorder(blocker / "records.jsonl").write(_record())  # parent is a file


def test_default_path_honours_env_and_home(tmp_path):
    assert default_records_path({"CONDUCTOR_JUDGMENT_RECORDS": str(tmp_path / "r.jsonl")}) == tmp_path / "r.jsonl"
    home_default = default_records_path({})
    assert home_default.parts[-3:] == (".conductor", "judgment", "records.jsonl")
    assert str(home_default).startswith(str(tmp_path))  # HOME is the tmp home in tests
    assert default_recorder().path == tmp_path / "records.jsonl"


def test_jsonl_recorder_creates_private_dir_and_file(tmp_path):
    import os
    import stat

    parent = tmp_path / "existing"
    parent.mkdir(mode=0o755)
    os.chmod(parent, 0o755)
    path = parent / "new" / "deeper" / "records.jsonl"
    old_umask = os.umask(0)  # mode must not depend on the umask
    try:
        JsonlRecorder(path).write(_record())
    finally:
        os.umask(old_umask)
    mode = lambda p: stat.S_IMODE(p.stat().st_mode)  # noqa: E731
    assert mode(path) == 0o600
    assert mode(path.parent) == 0o700 and mode(path.parent.parent) == 0o700
    assert mode(parent) == 0o755  # an existing user dir is left alone


def test_jsonl_recorder_writes_off_the_loop_thread(tmp_path, monkeypatch):
    import asyncio
    import threading

    recorder = JsonlRecorder(tmp_path / "r.jsonl")
    threads = []
    original = JsonlRecorder._append

    def spy(self, line):
        threads.append(threading.get_ident())
        original(self, line)

    monkeypatch.setattr(JsonlRecorder, "_append", spy)

    async def run():
        loop_thread = threading.get_ident()
        for i in range(30):
            recorder.write(_record(reason=str(i)))
        await recorder.drain()
        return loop_thread

    loop_thread = asyncio.run(run())
    assert len(threads) == 30 and loop_thread not in threads
    reasons = [json.loads(x)["reason"] for x in (tmp_path / "r.jsonl").read_text().splitlines()]
    assert reasons == [str(i) for i in range(30)]  # in order

    threads.clear()
    recorder.write(_record())  # no running loop: synchronous, this thread
    assert threads == [threading.get_ident()]


def test_jsonl_recorder_flush_and_bounded_queue(tmp_path, monkeypatch):
    import asyncio
    import threading

    gate = threading.Event()
    original = JsonlRecorder._append

    def slow(self, line):
        gate.wait(5)
        original(self, line)

    monkeypatch.setattr(JsonlRecorder, "_append", slow)
    recorder = JsonlRecorder(tmp_path / "r.jsonl", max_pending=3)

    async def run():
        for _ in range(10):
            recorder.write(_record())  # never blocks, never raises

    asyncio.run(run())
    gate.set()
    recorder.flush()
    assert len((tmp_path / "r.jsonl").read_text().splitlines()) == 3
