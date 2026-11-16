"""The two CLI test files must not read the operator's sessions.

``src/llms/native_cli/sessions.py`` works out the default path of the legacy
session store when its module is **imported**. A test file that is
collected next to another one therefore gets the home of whoever started
the run -- the operator's home -- not the home of the test. A legacy
session of the same provider that happens to be there is then counted as a
session of the worker under test: the test fails on a machine that has
used Conductor and passes on one that has not, which is the worst kind of
failure, because it is green exactly where nobody looks.

This is the regression for that. It starts a **separate** pytest run whose
home holds a synthetic session of the same provider, collects the two
files that build a real session manager, and asserts that the run passes
-- that is, that the operator's session is not counted.

The run is a subprocess on purpose. The default path is fixed when the
module is imported, so the pollution has to be in place **before** the
collection, and only a fresh interpreter can do that.

The file is not collected by the run it starts (it lives in this same
directory, and the run collects two other files), so the run cannot
recurse into itself.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

#: The root of the repository: this file is ``<root>/tests/unit/conductor_cli/``.
REPO_ROOT = Path(__file__).resolve().parents[3]

#: The files that build a real session manager, and therefore the ones whose
#: collection has to survive a polluted home.
COLLECTED = (
    "tests/unit/conductor_cli/test_worker_state.py",
    "tests/unit/conductor_cli/test_workers.py",
)

#: The legacy session the operator's home holds: the same provider as the
#: worker the tests are about, so it would be counted as a second session.
POLLUTION = {"thread:other:kimi": "synthetic-session"}


def _polluted_home(tmp_path: Path) -> Path:
    """A home that holds one legacy session of the same provider.

    The path is **resolved**: the CLI refuses a private data directory that
    is not inside the home it works out, and on macOS ``/tmp`` is a symlink,
    so an unresolved path would be refused for a reason that has nothing to
    do with this test.
    """
    home = (tmp_path / "operator-home").resolve()
    native = home / ".cache" / "conductor" / "native-cli"
    native.mkdir(parents=True)
    (native / "sessions.json").write_text(json.dumps(POLLUTION), encoding="utf-8")
    return home


def test_the_operators_session_is_not_counted_when_the_two_files_are_collected(
    tmp_path: Path,
) -> None:
    """Collecting the two files with a polluted home must still pass.

    Before the isolation was complete, the real-manager test read the
    operator's session: two sessions instead of one, and the collected run
    failed. The two lines of the failure are the whole story -- a test that
    only breaks on the machines that use the thing.
    """
    home = _polluted_home(tmp_path)

    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("PYTHONPATH", None)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            *COLLECTED,
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )

    assert result.returncode == 0, (
        "collecting "
        + " ".join(COLLECTED)
        + f" with a home that holds {POLLUTION} must pass, but the run failed:\n"
        + result.stdout[-4000:]
        + result.stderr[-2000:]
    )
    # The operator's session was never counted, so it is nowhere in the
    # output of a run that passed.
    assert POLLUTION["thread:other:kimi"] not in result.stdout
