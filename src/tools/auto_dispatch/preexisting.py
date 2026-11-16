"""G2 tells a failure the base commit already had from one the line caused (N27).

The planner once wrote an acceptance check that main itself failed, so no
author could pass G2 and every fix round was wasted. The ruff lint baseline
(#143) handles ``ruff check``; this generalises it to any focused command:
a G2 command that failed is re-run, unchanged, in a detached checkout of the
line's ``base_commit``. When it fails there the same way, the failure is
``preexisting_failure``, not the line's fault.

"The same way" is strict, so a real regression is never excused:

* the base run exits with the same non-zero code as the head run;
* when the head output names failed tests (pytest's ``FAILED``/``ERROR``
  summary lines), the base output names them too and every head failure is
  among them: one new failing test makes it the line's failure;
* when neither output names tests (not pytest), the matching exit code is the
  evidence, and the record says so (``basis: exit_code``).

A test file the line added does not exist at the base, so pytest exits with a
different code there (4 or 5, not 1) and the failure stays the line's.

``CONDUCTOR_DISPATCH_G2_PREEXISTING_MODE`` (off|shadow|enforce, default
``shadow``): ``off`` re-runs nothing; ``shadow`` records the classification on
the command record and keeps today's outcome (``focused_tests_failed``);
``enforce`` fails G2 with ``preexisting_failure`` instead, which the
scheduler can block on rather than start a fix round no author can win. Only
``enforce`` changes what the author is told: the fix-round note names a
preexisting failure only when the record is enforced.

The re-run costs a second G2 run, so it is bounded:

* ``CONDUCTOR_DISPATCH_PREEXISTING_RERUN`` (off|pytest|all, default
  ``pytest``) says which failed commands are re-run. ``pytest`` re-runs only a
  recognised pytest command (``pytest``/``py.test`` or ``python -m pytest`` as
  the program, no wrapper); ``all`` re-runs any command, but only in
  ``enforce``: in ``shadow`` a non-pytest command is never re-run.
* ``CONDUCTOR_DISPATCH_PREEXISTING_RERUN_TIMEOUT_SECONDS`` (default 300,
  clamped to 1..3600 and never above the G2 timeout) is the re-run's own
  timeout.
* The base checkout has no virtualenv, and its files must not pick the
  interpreter. A leading ``python``/``pytest`` (bare, or a relative path such
  as ``.venv/bin/python``) is replaced by the line worktree's
  ``.venv/bin/python`` (``pytest`` becomes ``<that> -m pytest``). Without a
  line venv a bare name is kept, so ``PATH`` resolves it exactly as for the
  head run; a relative interpreter path is not re-run at all. An absolute
  program path is kept: the head run used it too.
"""

from __future__ import annotations

import logging
import math
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from .check_modes import ENFORCE, OFF, check_mode

logger = logging.getLogger(__name__)

PREEXISTING_MODE_ENV = "CONDUCTOR_DISPATCH_G2_PREEXISTING_MODE"
#: Which failed commands are re-run at the base: off|pytest|all.
RERUN_ENV = "CONDUCTOR_DISPATCH_PREEXISTING_RERUN"
RERUN_OFF = "off"
RERUN_PYTEST = "pytest"
RERUN_ALL = "all"
RERUN_DEFAULT = RERUN_PYTEST
_RERUN_SCOPES = frozenset({RERUN_OFF, RERUN_PYTEST, RERUN_ALL})
#: The re-run's own timeout, in seconds, and its bounds.
RERUN_TIMEOUT_ENV = "CONDUCTOR_DISPATCH_PREEXISTING_RERUN_TIMEOUT_SECONDS"
RERUN_TIMEOUT_DEFAULT = 300.0
RERUN_TIMEOUT_MIN = 1.0
RERUN_TIMEOUT_MAX = 3600.0
#: The G2 reason (enforce) and the record's classification.
PREEXISTING_FAILURE = "preexisting_failure"
LINE_FAILURE = "line_failure"
UNKNOWN = "unknown"

#: How much of each log is read to find the summary lines (they are at the end).
_LOG_WINDOW_BYTES = 256 * 1024
#: At most this many test ids are kept on the record.
_MAX_IDS = 20

#: A summary line's id: a node id, with its parameter brackets (which may hold
#: spaces or `` - ``), before pytest's `` - message``.
_SUMMARY = re.compile(r"^(?:FAILED|ERROR) (?P<id>[^\s\[]+(?:\[[^\]]*\])?)")


class _Runner(Protocol):
    async def run(self, argv, *, cwd, timeout, log_path): ...


class _Worktrees(Protocol):
    async def add_detached(self, repo_root, path, rev): ...

    async def remove(self, repo_root, path): ...


def preexisting_mode(environ: Mapping[str, str] | None = None) -> str:
    """The rollout mode for the base re-run; ``shadow`` when unset."""
    return check_mode(PREEXISTING_MODE_ENV, environ)


def rerun_scope(environ: Mapping[str, str] | None = None) -> str:
    """Which failed commands are re-run; ``pytest`` when unset or not a scope."""
    env = os.environ if environ is None else environ
    value = str(env.get(RERUN_ENV, "") or "").strip().lower()
    return value if value in _RERUN_SCOPES else RERUN_DEFAULT


def rerun_timeout(
    g2_timeout: float | None = None, environ: Mapping[str, str] | None = None
) -> float:
    """The base re-run's timeout: the env value (default 300), bounded.

    Clamped to ``RERUN_TIMEOUT_MIN..RERUN_TIMEOUT_MAX`` and never longer than
    the G2 timeout itself. A value that is not a finite number is the default.
    """
    env = os.environ if environ is None else environ
    raw = str(env.get(RERUN_TIMEOUT_ENV, "") or "").strip()
    try:
        value = float(raw) if raw else RERUN_TIMEOUT_DEFAULT
    except ValueError:
        value = RERUN_TIMEOUT_DEFAULT
    if not math.isfinite(value):
        value = RERUN_TIMEOUT_DEFAULT
    value = min(max(value, RERUN_TIMEOUT_MIN), RERUN_TIMEOUT_MAX)
    if g2_timeout is not None and g2_timeout > 0:
        value = min(value, float(g2_timeout))
    return value


_PYTEST_NAMES = frozenset({"pytest", "py.test"})
_PYTHON_NAME = re.compile(r"^python(?:\d+(?:\.\d+)*)?$")


def _name(item: str) -> str:
    return item.replace("\\", "/").rsplit("/", 1)[-1]


def _python_runs_pytest(argv: Sequence[str]) -> bool:
    """``python [options] -m pytest ...``: the module run is pytest."""
    position = 1
    while position < len(argv) and argv[position].startswith("-"):
        option = argv[position]
        if option == "-m":
            return position + 1 < len(argv) and argv[position + 1] in _PYTEST_NAMES
        if option == "-c":
            return False
        position += 2 if option in ("-W", "-X") else 1
    return False


def is_pytest_command(argv: Sequence[str]) -> bool:
    """Whether the program itself is pytest: ``pytest ...`` or ``python -m pytest``.

    Only the first argument counts. A wrapper (``uv run``, ``env``, ``VAR=x``)
    is not recognised: ``uv run`` would build a fresh venv in the base checkout.
    """
    if not argv:
        return False
    name = _name(argv[0])
    if name in _PYTEST_NAMES:
        return True
    return bool(_PYTHON_NAME.match(name)) and _python_runs_pytest(argv)


def line_interpreter(line_worktree: Path | None) -> Path | None:
    """The line worktree's ``.venv/bin/python`` when it is there and executable."""
    if line_worktree is None:
        return None
    candidate = Path(line_worktree) / ".venv" / "bin" / "python"
    try:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    except OSError:
        return None
    return None


def rerun_argv(
    argv: Sequence[str], line_worktree: Path | None
) -> tuple[list[str] | None, str]:
    """The argv the base re-run uses, and how the interpreter was chosen.

    ``(None, reason)`` when the command must not be re-run. The base checkout
    never picks the interpreter: a relative program path would resolve inside
    it, so a relative ``python``/``pytest`` is replaced by the line worktree's
    interpreter or not run at all.
    """
    items = list(argv)
    if not items:
        return None, "empty_command"
    program, rest = items[0], items[1:]
    name = _name(program)
    is_python = bool(_PYTHON_NAME.match(name))
    is_pytest = name in _PYTEST_NAMES
    if not (is_python or is_pytest):
        return items, "unchanged"
    if Path(program).is_absolute():
        # Chosen by the plan, and it cannot point into a base checkout made
        # after the plan was written: the head run used it, so does the base.
        return items, "absolute"
    interpreter = line_interpreter(line_worktree)
    relative = "/" in program.replace("\\", "/")
    if interpreter is None:
        if relative:
            return None, "no_line_interpreter"
        return items, "path"
    if is_pytest:
        return [str(interpreter), "-m", "pytest", *rest], "line_venv"
    return [str(interpreter), *rest], "line_venv"


def failed_test_ids(text: str) -> frozenset[str] | None:
    """Test ids pytest's summary names as FAILED or ERROR; ``None`` when it names none."""
    found = set()
    for line in text.splitlines():
        match = _SUMMARY.match(line.strip())
        if match:
            found.add(match.group("id").strip())
    return frozenset(found) if found else None


def classify(
    head_exit: int, head_text: str, base_exit: int | None, base_text: str
) -> dict[str, Any]:
    """Whether a head failure is already there at the base. Pure.

    ``base_exit`` ``None`` means the base run did not finish (timed out or
    could not start): nothing is known, so the classification is ``unknown``.
    """
    if base_exit is None:
        return {"classification": UNKNOWN, "reason": "base_run_unfinished"}
    if base_exit == 0:
        return {"classification": LINE_FAILURE, "reason": "base_passed"}
    if base_exit != head_exit:
        return {"classification": LINE_FAILURE, "reason": "exit_code_differs"}
    head_ids = failed_test_ids(head_text)
    base_ids = failed_test_ids(base_text)
    if head_ids is None and base_ids is None:
        return {
            "classification": PREEXISTING_FAILURE,
            "basis": "exit_code",
            "reason": "same_exit_code",
        }
    if head_ids is None or base_ids is None:
        return {"classification": LINE_FAILURE, "reason": "failed_tests_differ"}
    new = sorted(head_ids - base_ids)
    record: dict[str, Any] = {
        "basis": "failed_tests",
        "head_failed": len(head_ids),
        "base_failed": len(base_ids),
        "new_failures": len(new),
    }
    if new:
        record.update(
            classification=LINE_FAILURE,
            reason="new_failed_tests",
            new_failed_ids=new[:_MAX_IDS],
        )
    else:
        record.update(classification=PREEXISTING_FAILURE, reason="same_failed_tests")
    return record


def _read_tail(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            length = stream.tell()
            stream.seek(max(length - _LOG_WINDOW_BYTES, 0))
            return stream.read(_LOG_WINDOW_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return ""


async def check_preexisting(
    *,
    argv: Sequence[str],
    head_exit: int,
    head_log: Path,
    base_commit: str,
    repo_root: Path,
    base_worktree: Path,
    base_log: Path,
    base_log_ref: str,
    timeout: float,
    commands: _Runner,
    worktrees: _Worktrees,
    mode: str | None = None,
    line_worktree: Path | None = None,
    scope: str | None = None,
) -> dict[str, Any] | None:
    """Re-run a failed G2 command at ``base_commit`` and classify the failure.

    ``None`` when the mode is ``off``: nothing ran, the plain verdict stands.
    Otherwise a record with ``mode``, ``classification`` and the evidence; it
    never raises. ``enforce`` is true on it only when the mode is ``enforce``
    and the failure is preexisting, which is when G2 reports
    ``preexisting_failure`` instead of the plain failure.

    ``scope`` (default from ``CONDUCTOR_DISPATCH_PREEXISTING_RERUN``) limits
    which commands are re-run; ``off`` re-runs nothing and returns ``None``.
    In ``shadow`` only a recognised pytest command is ever re-run. The re-run
    uses ``timeout`` and the interpreter :func:`rerun_argv` picks from
    ``line_worktree``.
    """
    mode = mode or preexisting_mode()
    if mode == OFF:
        return None
    scope = scope or rerun_scope()
    if scope == RERUN_OFF:
        return None
    record: dict[str, Any] = {"mode": mode, "classification": UNKNOWN}
    pytest_run = is_pytest_command(argv)
    # Only enforce may re-run a non-pytest command; shadow never does.
    if not pytest_run and not (scope == RERUN_ALL and mode == ENFORCE):
        record["reason"] = "rerun_not_pytest"
        return _finish(record, mode)
    base = (base_commit or "").strip()
    if not base:
        record["reason"] = "no_base_commit"
        return _finish(record, mode)
    command, interpreter = rerun_argv(argv, line_worktree)
    if command is None:
        record["reason"] = interpreter
        return _finish(record, mode)
    record["interpreter"] = interpreter
    record["rerun_timeout_seconds"] = timeout
    base_exit: int | None = None
    added = False
    try:
        await worktrees.add_detached(repo_root, base_worktree, base)
        added = True
        result = await commands.run(
            command, cwd=base_worktree, timeout=timeout, log_path=base_log
        )
        record["base_log_ref"] = base_log_ref
        record["base_exit_code"] = result.exit_code
        if not result.timed_out:
            base_exit = result.exit_code
    except Exception as exc:  # noqa: BLE001 - a classification must not end G2
        logger.warning("G2 preexisting re-run failed: %s", type(exc).__name__)
        record["reason"] = "base_run_failed" if added else "base_checkout_failed"
        return _finish(record, mode)
    finally:
        try:
            await worktrees.remove(repo_root, base_worktree)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "G2 preexisting worktree remove failed: %s", type(exc).__name__
            )
    head_text = _read_tail(head_log)
    base_text = _read_tail(base_log) if base_exit is not None else ""
    record.update(classify(head_exit, head_text, base_exit, base_text))
    return _finish(record, mode)


def _finish(record: dict[str, Any], mode: str) -> dict[str, Any]:
    record["enforced"] = (
        mode == ENFORCE and record.get("classification") == PREEXISTING_FAILURE
    )
    return record


def preexisting_note(record: object) -> str:
    """One line for the fix-round note, or ``""`` when there is nothing to say.

    Only an enforced record speaks: in ``shadow`` the classification is
    recorded and the author is told exactly what they were told before.
    """
    if not isinstance(record, dict):
        return ""
    if record.get("classification") != PREEXISTING_FAILURE:
        return ""
    if record.get("enforced") is not True or record.get("mode") != ENFORCE:
        return ""
    return (
        "preexisting failure: the same command fails the same way on the line's"
        " base commit, so this failure is not the line's; do not try to fix code"
        " outside the line's write scope for it"
    )


__all__ = [
    "LINE_FAILURE",
    "PREEXISTING_FAILURE",
    "PREEXISTING_MODE_ENV",
    "RERUN_ALL",
    "RERUN_DEFAULT",
    "RERUN_ENV",
    "RERUN_OFF",
    "RERUN_PYTEST",
    "RERUN_TIMEOUT_DEFAULT",
    "RERUN_TIMEOUT_ENV",
    "RERUN_TIMEOUT_MAX",
    "RERUN_TIMEOUT_MIN",
    "UNKNOWN",
    "check_preexisting",
    "classify",
    "failed_test_ids",
    "is_pytest_command",
    "line_interpreter",
    "preexisting_mode",
    "preexisting_note",
    "rerun_argv",
    "rerun_scope",
    "rerun_timeout",
]
