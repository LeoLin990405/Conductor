"""N27: a G2 fix round carries the failed focused-test output."""

from __future__ import annotations

import pytest

from src.tools.auto_dispatch.gates import (
    G2_FIX_OUTPUT_BUDGET_BYTES,
    MAX_FIX_TASK_BYTES,
    GateRunner,
    GateSettings,
    g2_failure_note,
    read_log_tail,
    redacted_output_tail,
    with_g2_failure,
)
from src.tools.auto_dispatch.types import Dispatch, Line

SECRET = "sk-" "ant-api03-abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGH"



def _failed_cell(log_ref: str, *, exit_code: int = 1) -> dict:
    return {
        "result": "fail",
        "reason": "focused_tests_failed",
        "commands": [
            {
                "index": 0,
                "safe_command": "pytest -q tests/unit/test_ok.py",
                "exit_code": 0,
                "log_ref": "gates/d/line-0/g2-0-0.log",
            },
            {
                "index": 1,
                "safe_command": "pytest -q tests/unit/test_x.py",
                "exit_code": exit_code,
                "log_ref": log_ref,
            },
        ],
    }


def _write_log(runs, ref: str, text: str) -> None:
    path = runs / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_g2_note_carries_command_exit_code_and_the_end_of_the_output(tmp_path):
    ref = "gates/d/line-0/g2-0-1.log"
    head = "collected 400 items\n" + ("." * 50 + "\n") * 400
    tail = (
        f"E   AssertionError: api_key={SECRET}\n"
        "FAILED tests/unit/test_x.py::test_one - AssertionError\n"
        "1 failed, 399 passed in 2.00s\n"
    )
    _write_log(tmp_path, ref, head + tail)
    note = g2_failure_note(_failed_cell(ref), tmp_path)
    assert "Focused tests (G2) failed: focused_tests_failed" in note
    assert "command: pytest -q tests/unit/test_x.py" in note
    assert "exit code: 1" in note
    assert "1 failed, 399 passed in 2.00s" in note
    assert "FAILED tests/unit/test_x.py::test_one" in note
    assert SECRET not in note
    assert "collected 400 items" not in note
    output = note.split("credentials redacted):\n", 1)[1]
    assert len(output.encode("utf-8")) <= G2_FIX_OUTPUT_BUDGET_BYTES + 200


def test_redacted_tail_is_bounded_after_redaction():
    raw = (("x" * 70 + "\n") * 200 + f"token={SECRET}\nend\n").encode()
    tail = redacted_output_tail(raw, 512)
    assert len(tail.encode("utf-8")) <= 512
    assert tail.endswith("end")
    assert SECRET not in tail


def test_g2_note_is_empty_unless_g2_failed(tmp_path):
    assert g2_failure_note({"result": "pass", "commands": []}, tmp_path) == ""
    assert g2_failure_note(None, tmp_path) == ""


def test_g2_note_does_not_read_a_log_outside_the_runs_dir(tmp_path):
    outside = tmp_path / "secret.log"
    outside.write_text("do-not-read", encoding="utf-8")
    runs = tmp_path / "runs"
    runs.mkdir()
    note = g2_failure_note(_failed_cell("../secret.log"), runs)
    assert "do-not-read" not in note
    assert "output: unavailable" in note


def test_with_g2_failure_keeps_the_note_whole_inside_the_task_limit():
    note = "Focused tests (G2) failed\n" + "n" * 3000 + "\n"
    task = "t" * 7000
    combined = with_g2_failure(task, note)
    assert len(combined.encode("utf-8")) <= MAX_FIX_TASK_BYTES
    assert combined.endswith(note)
    assert "truncated" in combined
    assert with_g2_failure("short", "") == "short"
    assert with_g2_failure("short", note) == "short\n\n" + note


def test_gate_runner_g2_fix_task_reads_the_stored_g2_cell(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    runs = tmp_path / "runs"
    gate = GateRunner(GateSettings(runs_dir=runs, repo_root=repo))
    line = Line(id="line-0", name="x", task="fix x", branch="feature/x")
    dispatch = Dispatch(id="d", goal="goal", user_id="u", created_at="now", lines=[line])
    snapshot = gate.line_snapshot(dispatch, line)
    ref = "gates/d/line-0/g2-0-1.log"
    _write_log(runs, ref, "FAILED tests/unit/test_x.py::test_one\n")
    snapshot["gates"]["G2"] = _failed_cell(ref)
    task = gate.g2_fix_task(dispatch, line, "fix x\n\nBlockers:\n- none\n")
    assert "FAILED tests/unit/test_x.py::test_one" in task
    snapshot["gates"]["G2"] = {"result": "pass", "commands": []}
    assert gate.g2_fix_task(dispatch, line, "fix x") == "fix x"


def test_a_secret_cut_by_the_window_edge_does_not_leak():
    """Review repro: a value whose key fell before the window must not show."""
    raw = (("export API_KEY=" + "k" * 5000 + "\n") * 2 + "FAILED x\n").encode()
    tail = redacted_output_tail(raw)
    assert "kkkk" not in tail
    assert tail.endswith("FAILED x")


def test_bearer_tokens_straddling_the_window_do_not_leak():
    import base64

    jwt = ".".join(
        base64.urlsafe_b64encode(bytes([i * 7 + 1]) * 650).decode().rstrip("=")
        for i in range(3)
    )
    assert 2400 < len(jwt) < 3000
    raw = ("".join(f"Authorization: Bearer {jwt}\n" for _ in range(4)) + "FAILED x\n").encode()
    tail = redacted_output_tail(raw)
    for part in jwt.split("."):
        assert part[:40] not in tail
    assert tail.endswith("FAILED x")


def test_no_text_from_before_the_last_budget_bytes_is_kept():
    # Window: the end of the A line (a fragment, dropped), then a whole c
    # line that starts before the last 1000 bytes (not kept), then d and b.
    raw = ("A" * 3000 + "\n" + "c" * 500 + "\n" + "d" * 600 + "\n" + "b" * 100 + "\n").encode()
    tail = redacted_output_tail(raw, 1000)
    assert "A" not in tail and "c" not in tail
    assert tail == "d" * 600 + "\n" + "b" * 100


def test_the_note_reads_only_the_end_of_a_large_log(tmp_path):
    path = tmp_path / "big.log"
    path.write_bytes(b"x" * 100_000 + b"\nprev\nlast line\n")
    raw, cut = read_log_tail(path, 64)
    assert cut and len(raw) == 64
    # "prev" follows the dropped fragment, so it is never shown.
    assert redacted_output_tail(raw, 32, cut=cut) == "last line"


@pytest.mark.parametrize(
    "key_line",
    ['  "api_key":', "  api_key:", "  password =", "  Authorization:", '  "client_secret":'],
)
def test_a_value_on_the_line_after_its_key_does_not_leak(key_line):
    """Re-review repro: the cut falls between a key line and its value line."""
    val = '    "SECRETVAL' + "z" * 30 + '"\n'
    pad = ("w" * 100 + "\n") * 38 + "FAILED\n"
    tail = val + "w" * (4096 - len(val + pad) - 1) + "\n" + pad
    raw = ("y" * 100 + "\n") * 80 + key_line + "\n" + tail
    # The value line starts just inside the last 4096 bytes, the key line
    # just before them.
    out = redacted_output_tail(raw.encode(), 4096)
    assert "SECRETVAL" not in out
    assert "zzzzzzzzzz" not in out
    assert out.endswith("FAILED")
    assert len(out.encode("utf-8")) <= 4096


def test_a_key_at_the_end_of_an_overlong_fragment_does_not_leak():
    """Re-review: the dropped fragment alone is longer than the budget."""
    raw = ("w" * 4995 + " api_key:\n" + "  SECRETVAL" + "z" * 30 + "\nFAILED x\n").encode()
    for budget in (4096, 4000):
        out = redacted_output_tail(raw, budget, cut=True)
        assert "SECRETVAL" not in out
        assert "zzzzzzzzzz" not in out
        assert out == "FAILED x"
