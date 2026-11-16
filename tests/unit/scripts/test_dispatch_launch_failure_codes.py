"""The launch script says *why* a worker failed, with a distinguishable code.

``scripts/dispatch_launch.sh`` does not read a failed worker's output to
decide what happened: it asks the failure classifier
(``src.llms.native_cli/failure_cli.py``) and maps the class the classifier names
onto an exit code (W8a). A rate limit, an exhausted quota, a dead login and
an exhausted context each get their own code, so the dispatcher can act on
them without reading the worker's output — and the raw output never reaches
the exit-code path.

These tests drive the script with a fake CLI and a fake classifier: no real
model is started, no quota is spent and no real classifier is needed. The
fake classifier is reached through ``WORKER_FAILURE_CLASSIFIER_CMD``, the
script's test-only seam.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "dispatch_launch.sh"

# The exit codes the launch script reports.
EXIT_USAGE = 64
EXIT_RESERVED = 65
EXIT_RATE_LIMITED = 75
EXIT_QUOTA = 76
EXIT_AUTH = 77
EXIT_CONTEXT = 78
EXIT_WORKER_FAILED = 79
EXIT_TIMEOUT = 124
EXIT_NO_BINARY = 127

# The class the classifier names, and the code it is reported as.
CLASS_CODES = [
    ("rate_limited", EXIT_RATE_LIMITED),
    ("quota_exhausted", EXIT_QUOTA),
    ("auth_failed", EXIT_AUTH),
    ("context_exhausted", EXIT_CONTEXT),
]

# The classes that carry no dedicated code: they are all "the worker
# failed".
FALLBACK_CLASSES = ["overloaded", "network", "crash", "unknown"]

# A secret that must never reach the launcher's own output. It stands in for
# a token in a worker's error message.
SENTINEL = "sentinel-secret-token-abc123"

# The fake CLI: the worker. It says what the test tells it to say and exits
# with the code the test asks for. It records nothing — these tests are about
# the exit code the launcher reports, not about the argv.
FAKE_CLI = """#!/bin/bash
if [ -n "${FAKE_STDOUT:-}" ]; then
    printf '%s\\n' "$FAKE_STDOUT"
fi
if [ -n "${FAKE_STDERR:-}" ]; then
    printf '%s\\n' "$FAKE_STDERR" >&2
fi
sleep "${FAKE_SLEEP:-0}"
exit "${FAKE_EXIT:-0}"
"""

# The fake classifier: it records how it was called, and answers with the
# class the test asked for. It can also leak the diagnostic it was handed
# (a classifier that misbehaves), hang, fail, or answer with garbage.
FAKE_CLASSIFIER = """#!/bin/bash
if [ -n "${FAKE_CLASSIFIER_RECORD:-}" ]; then
    printf '%s\\n' "$*" > "$FAKE_CLASSIFIER_RECORD"
    pwd > "${FAKE_CLASSIFIER_RECORD}.cwd"
fi
if [ "${FAKE_CLASSIFIER_ECHO:-0}" = "1" ]; then
    diagnostic=""
    while [ "$#" -gt 0 ]; do
        if [ "$1" = "--diagnostic-file" ]; then
            diagnostic="$2"
            break
        fi
        shift
    done
    if [ -n "$diagnostic" ] && [ -f "$diagnostic" ]; then
        cat "$diagnostic" >&2
    fi
fi
if [ -n "${FAKE_CLASSIFIER_SLEEP:-}" ]; then
    # exec: the sleep takes over this process, so stopping the classifier
    # stops the sleep with it and leaves nothing behind.
    exec sleep "$FAKE_CLASSIFIER_SLEEP"
fi
answer="${FAKE_CLASSIFIER_OUTPUT:-}"
if [ -z "$answer" ]; then
    answer='{"failure_class": "unknown"}'
fi
printf '%s\\n' "$answer"
exit "${FAKE_CLASSIFIER_EXIT:-0}"
"""


def _write_executable(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def fake_bin(tmp_path: Path) -> Path:
    """A directory of fake CLIs, first on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "kimi", FAKE_CLI)
    return bin_dir


@pytest.fixture
def classifier(tmp_path: Path) -> Path:
    """The fake classifier the script is pointed at."""
    return _write_executable(tmp_path / "fake_classifier.sh", FAKE_CLASSIFIER)


def _run(
    tmp_path: Path,
    fake_bin: Path,
    classifier: Path,
    *args: str,
    env: dict[str, str] | None = None,
    timeout: int = 60,
    use_fake_classifier: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run the launch script with the fake CLI and the fake classifier."""
    environment = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR": str(tmp_path / "logs"),
        "FAKE_CLASSIFIER_RECORD": str(tmp_path / "classifier-args.txt"),
    }
    if use_fake_classifier:
        environment["WORKER_FAILURE_CLASSIFIER_CMD"] = str(classifier)
    if env:
        environment.update(env)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=str(tmp_path),
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _classifier_answer(failure_class: str) -> str:
    """The one line of JSON the classifier prints."""
    return json.dumps(
        {
            "failure_class": failure_class,
            "reason_code": f"worker_{failure_class}",
            "retry_after_seconds": 1,
        }
    )


# ------------------------------------------------------- the classified codes


@pytest.mark.parametrize(("failure_class", "code"), CLASS_CODES)
def test_a_classified_failure_is_reported_with_its_own_code(
    tmp_path, fake_bin, classifier, failure_class, code
) -> None:
    """Each class the classifier names has a code of its own."""
    result = _run(
        tmp_path,
        fake_bin,
        classifier,
        "kimi",
        "the task",
        env={
            "FAKE_EXIT": "1",
            "FAKE_STDOUT": "the worker hit an API error",
            "FAKE_CLASSIFIER_OUTPUT": _classifier_answer(failure_class),
        },
    )
    assert result.returncode == code, result.stderr


@pytest.mark.parametrize("failure_class", FALLBACK_CLASSES)
def test_a_class_without_a_dedicated_code_is_a_plain_worker_failure(
    tmp_path, fake_bin, classifier, failure_class
) -> None:
    """The classes that name no code are all "the worker failed"."""
    result = _run(
        tmp_path,
        fake_bin,
        classifier,
        "kimi",
        "the task",
        env={
            "FAKE_EXIT": "1",
            "FAKE_STDOUT": "the worker hit an API error",
            "FAKE_CLASSIFIER_OUTPUT": _classifier_answer(failure_class),
        },
    )
    assert result.returncode == EXIT_WORKER_FAILED, result.stderr


def test_the_classification_outranks_the_workers_own_exit_code(
    tmp_path, fake_bin, classifier
) -> None:
    """The class decides the code; the worker's own code does not."""
    result = _run(
        tmp_path,
        fake_bin,
        classifier,
        "kimi",
        "the task",
        env={
            "FAKE_EXIT": "3",
            "FAKE_STDOUT": "the worker hit an API error",
            "FAKE_CLASSIFIER_OUTPUT": _classifier_answer("quota_exhausted"),
        },
    )
    assert result.returncode == EXIT_QUOTA, result.stderr


# ------------------------------------------------------------ code collisions


@pytest.mark.parametrize("raw_code", [75, 76, 77, 78])
def test_a_worker_code_that_collides_with_a_class_code_is_not_a_class(
    tmp_path, fake_bin, classifier, raw_code
) -> None:
    """A worker that exits 75 did not classify anything as rate limited."""
    result = _run(
        tmp_path,
        fake_bin,
        classifier,
        "kimi",
        "the task",
        env={
            "FAKE_EXIT": str(raw_code),
            "FAKE_STDOUT": "the worker hit an API error",
            "FAKE_CLASSIFIER_OUTPUT": _classifier_answer("unknown"),
        },
    )
    assert result.returncode == EXIT_WORKER_FAILED, result.stderr


def test_a_colliding_worker_code_does_not_survive_a_classification(
    tmp_path, fake_bin, classifier
) -> None:
    """A colliding worker code is replaced by the class, not reported as one."""
    result = _run(
        tmp_path,
        fake_bin,
        classifier,
        "kimi",
        "the task",
        env={
            "FAKE_EXIT": "75",
            "FAKE_STDOUT": "the worker hit an API error",
            "FAKE_CLASSIFIER_OUTPUT": _classifier_answer("auth_failed"),
        },
    )
    assert result.returncode == EXIT_AUTH, result.stderr


def test_a_colliding_worker_code_without_a_diagnostic_is_a_worker_failure(
    tmp_path, fake_bin, classifier
) -> None:
    """A silent worker that exits 75 is not read as a rate limit either."""
    result = _run(
        tmp_path,
        fake_bin,
        classifier,
        "kimi",
        "the task",
        env={"FAKE_EXIT": "75"},
    )
    assert result.returncode == EXIT_WORKER_FAILED, result.stderr


def test_a_silent_worker_keeps_its_own_exit_code(
    tmp_path, fake_bin, classifier
) -> None:
    """A worker that failed without writing anything keeps its own code.

    There is no diagnostic to classify, so the exit code the worker chose is
    the only information the caller has.
    """
    result = _run(
        tmp_path,
        fake_bin,
        classifier,
        "kimi",
        "the task",
        env={"FAKE_EXIT": "3"},
    )
    assert result.returncode == 3, result.stderr


# --------------------------------------------------- a classifier that fails


def test_a_classifier_that_fails_leaves_a_plain_worker_failure(
    tmp_path, fake_bin, classifier
) -> None:
    """A classifier that cannot answer must not take the launch down."""
    result = _run(
        tmp_path,
        fake_bin,
        classifier,
        "kimi",
        "the task",
        env={
            "FAKE_EXIT": "1",
            "FAKE_STDOUT": "the worker hit an API error",
            "FAKE_CLASSIFIER_EXIT": "2",
        },
    )
    assert result.returncode == EXIT_WORKER_FAILED, result.stderr


def test_a_classifier_that_answers_with_garbage_leaves_a_plain_worker_failure(
    tmp_path, fake_bin, classifier
) -> None:
    """An answer that is not a class is no answer at all."""
    result = _run(
        tmp_path,
        fake_bin,
        classifier,
        "kimi",
        "the task",
        env={
            "FAKE_EXIT": "1",
            "FAKE_STDOUT": "the worker hit an API error",
            "FAKE_CLASSIFIER_OUTPUT": "not json at all",
        },
    )
    assert result.returncode == EXIT_WORKER_FAILED, result.stderr


def test_a_classifier_that_names_an_unknown_class_leaves_a_plain_worker_failure(
    tmp_path, fake_bin, classifier
) -> None:
    """Only the classes of the fixed vocabulary are honoured."""
    result = _run(
        tmp_path,
        fake_bin,
        classifier,
        "kimi",
        "the task",
        env={
            "FAKE_EXIT": "1",
            "FAKE_STDOUT": "the worker hit an API error",
            "FAKE_CLASSIFIER_OUTPUT": json.dumps({"failure_class": "something_else"}),
        },
    )
    assert result.returncode == EXIT_WORKER_FAILED, result.stderr


def test_a_classifier_that_hangs_does_not_hold_the_launch_open(
    tmp_path, fake_bin, classifier
) -> None:
    """A classifier that never answers is stopped, and the launch ends."""
    started = time.monotonic()
    result = _run(
        tmp_path,
        fake_bin,
        classifier,
        "kimi",
        "the task",
        env={
            "FAKE_EXIT": "1",
            "FAKE_STDOUT": "the worker hit an API error",
            "FAKE_CLASSIFIER_SLEEP": "300",
            "WORKER_FAILURE_CLASSIFIER_TIMEOUT": "1",
        },
        timeout=60,
    )
    elapsed = time.monotonic() - started
    assert result.returncode == EXIT_WORKER_FAILED, result.stderr
    assert elapsed < 60, f"the launch waited {elapsed:.1f}s for the classifier"


def test_the_default_classifier_is_used_when_the_seam_is_not_set(
    tmp_path, fake_bin, classifier
) -> None:
    """Without the seam, the real entry point runs and a missing one is safe.

    The real entry point does not exist in every checkout, and when it does
    it answers "unknown" for an output that names no signal. Both leave the
    caller with the same plain failure.
    """
    result = _run(
        tmp_path,
        fake_bin,
        classifier,
        "kimi",
        "the task",
        env={
            "FAKE_EXIT": "1",
            "FAKE_STDOUT": "the worker hit an API error",
        },
        use_fake_classifier=False,
    )
    assert result.returncode == EXIT_WORKER_FAILED, result.stderr


# ------------------------------------------------------ the pre-existing codes


def test_an_unknown_model_is_refused_with_64(tmp_path, fake_bin, classifier) -> None:
    result = _run(tmp_path, fake_bin, classifier, "not-a-worker", "the task")
    assert result.returncode == EXIT_USAGE, result.stderr


def test_a_reserved_role_is_refused_with_65(tmp_path, fake_bin, classifier) -> None:
    result = _run(tmp_path, fake_bin, classifier, "codex", "the task")
    assert result.returncode == EXIT_RESERVED, result.stderr


def test_a_missing_binary_is_refused_with_127(tmp_path, fake_bin, classifier) -> None:
    result = _run(
        tmp_path,
        fake_bin,
        classifier,
        "kimi",
        "the task",
        env={"NATIVE_CLI_KIMI_BIN": "/nonexistent/kimi"},
    )
    assert result.returncode == EXIT_NO_BINARY, result.stderr


def test_a_run_that_overruns_its_timeout_stops_with_124(
    tmp_path, fake_bin, classifier
) -> None:
    """A stopped run is a timeout, not a classified failure."""
    result = _run(
        tmp_path,
        fake_bin,
        classifier,
        "kimi",
        "the task",
        env={
            "CONDUCTOR_DISPATCH_LAUNCH_TIMEOUT": "1",
            "CONDUCTOR_DISPATCH_LAUNCH_STOP_GRACE": "1",
            "FAKE_SLEEP": "20",
        },
        timeout=60,
    )
    assert result.returncode == EXIT_TIMEOUT, result.stderr


# --------------------------------------------------------- the diagnostic path


def test_the_classifier_is_asked_about_the_diagnostic_by_path(
    tmp_path, fake_bin, classifier
) -> None:
    """The diagnostic goes to the classifier as a file, not as an argument.

    A raw diagnostic on the command line is visible to every process on the
    machine, and it is what the launch must not spread around.
    """
    result = _run(
        tmp_path,
        fake_bin,
        classifier,
        "kimi",
        "the task",
        env={
            "FAKE_EXIT": "1",
            "FAKE_STDOUT": "the worker hit an API error",
            "FAKE_CLASSIFIER_OUTPUT": _classifier_answer("rate_limited"),
        },
    )
    assert result.returncode == EXIT_RATE_LIMITED, result.stderr
    recorded = (tmp_path / "classifier-args.txt").read_text(encoding="utf-8")
    assert "--provider kimi" in recorded, recorded
    assert "--exit-code 1" in recorded, recorded
    assert "--diagnostic-file" in recorded, recorded
    path = recorded.split("--diagnostic-file", 1)[1].split()[0]
    assert Path(path).is_file(), path
    assert "the worker hit an API error" in Path(path).read_text(encoding="utf-8")
    assert "the worker hit an API error" not in recorded, recorded


def test_the_classifier_runs_in_the_repository_root(
    tmp_path, fake_bin, classifier
) -> None:
    """The classifier is a module of the repository, so it runs there."""
    result = _run(
        tmp_path,
        fake_bin,
        classifier,
        "kimi",
        "the task",
        env={
            "FAKE_EXIT": "1",
            "FAKE_STDOUT": "the worker hit an API error",
            "FAKE_CLASSIFIER_OUTPUT": _classifier_answer("rate_limited"),
        },
    )
    assert result.returncode == EXIT_RATE_LIMITED, result.stderr
    where = (tmp_path / "classifier-args.txt.cwd").read_text(encoding="utf-8").strip()
    assert Path(where) == ROOT, where


def test_a_worker_that_succeeds_is_not_classified(
    tmp_path, fake_bin, classifier
) -> None:
    """A run that worked needs no classification."""
    result = _run(
        tmp_path,
        fake_bin,
        classifier,
        "kimi",
        "the task",
        env={"FAKE_EXIT": "0", "FAKE_STDOUT": "all good"},
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "classifier-args.txt").exists(), (
        "the classifier was asked about a worker that succeeded"
    )


# ------------------------------------------------------------------ the secret


def test_the_raw_diagnostic_never_reaches_the_launchers_own_output(
    tmp_path, fake_bin, classifier
) -> None:
    """A secret in the worker's output stays in the worker's output.

    The worker's own output is forwarded to the caller by the launcher, as
    it always has been. What must not happen is that the launcher adds a
    copy of it, or names it in a message of its own: the classification
    path reads the class and nothing else. The classifier here leaks the
    diagnostic on its own stderr, and that leak must not be forwarded
    either.
    """
    result = _run(
        tmp_path,
        fake_bin,
        classifier,
        "kimi",
        "the task",
        env={
            "FAKE_EXIT": "1",
            "FAKE_STDOUT": f"the worker failed: {SENTINEL}",
            "FAKE_CLASSIFIER_ECHO": "1",
            "FAKE_CLASSIFIER_OUTPUT": _classifier_answer("rate_limited"),
        },
    )
    assert result.returncode == EXIT_RATE_LIMITED, result.stderr
    # The launcher's own messages never carry the diagnostic.
    assert SENTINEL not in result.stderr, result.stderr
    # The worker's output is forwarded once, and once only: the
    # classification path adds no second copy of it.
    assert result.stdout.count(SENTINEL) == 1, result.stdout
    # The exit code is a status, never a message.
    assert isinstance(result.returncode, int)
    assert SENTINEL not in str(result.returncode)
    # The run log holds the worker's own output, once, and nothing else
    # carries the diagnostic: what the classifier wrote is removed.
    for path in (tmp_path / "logs").iterdir():
        if path.suffix == ".log":
            assert path.read_text(encoding="utf-8").count(SENTINEL) == 1, path
        else:
            assert SENTINEL not in path.read_text(encoding="utf-8"), path


def test_the_exit_codes_do_not_reuse_the_launchers_own_codes() -> None:
    """The classification codes live in a range of their own."""
    text = SCRIPT.read_text(encoding="utf-8")
    assigned = {}
    for line in text.splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip()
        if name.startswith("EXIT_") and value.isdigit():
            assigned[name] = int(value)
    own = {
        assigned["EXIT_USAGE"],
        assigned["EXIT_RESERVED"],
        assigned["EXIT_TIMEOUT"],
        assigned["EXIT_NO_BINARY"],
    }
    classification = {
        assigned["EXIT_RATE_LIMITED"],
        assigned["EXIT_QUOTA"],
        assigned["EXIT_AUTH"],
        assigned["EXIT_CONTEXT"],
        assigned["EXIT_WORKER_FAILED"],
    }
    assert not own & classification, assigned
    assert assigned["EXIT_RATE_LIMITED"] == EXIT_RATE_LIMITED
    assert assigned["EXIT_QUOTA"] == EXIT_QUOTA
    assert assigned["EXIT_AUTH"] == EXIT_AUTH
    assert assigned["EXIT_CONTEXT"] == EXIT_CONTEXT
    assert assigned["EXIT_WORKER_FAILED"] == EXIT_WORKER_FAILED
    assert os.access(SCRIPT, os.X_OK)
