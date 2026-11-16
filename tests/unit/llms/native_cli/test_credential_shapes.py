"""The credential shapes a numeric value takes, from the text to the file.

A credential whose value happens to be made of digits only is still a
credential: the exemption that lets a count through belongs to the seven
key names that name a count and to nothing else. These tests walk the
whole way a diagnostic travels -- the text is redacted, the reduced text is
hashed, and the note the next Worker reads is written to the disk -- and
they look for the digits at every step.

The file at the end is the one that matters: the handover note is the one
file a human reads, and a number that no rule recognises is a number that
reaches it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.llms.native_cli.failure import FailureClass, redact_credentials
from src.llms.native_cli.session_manager import WorkerSessionManager
from src.llms.native_cli.sessions import NativeCliSessionStore

# The four shapes in which a credential's value is made of digits only, and
# the digit string each one carries.
NUMERIC_CREDENTIALS = (
    ("password", "password='9876543210'", "9876543210"),
    ("api key", "api_key=9876543211", "9876543211"),
    ("authorization bearer", "Authorization: Bearer 9876543212", "9876543212"),
    ("bearer", "Bearer 9876543213", "9876543213"),
)

# The one shape that is not a credential: the number of tokens a turn used,
# under a key that names a count.
TOKEN_COUNT = ("token count", "max_tokens=131072", "131072")

# The shape that used to be one and is not any more: a bare "token" cannot
# be told apart from a credential, so it goes with the rest of them. It
# carries a number of its own, so a note that holds both says which one
# survived.
BARE_TOKEN = ("bare token", "token=262144", "262144")


def _git(repo: Path, *args: str) -> str:
    """One git answer from the throwaway repository."""
    completed = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return completed.stdout.strip()


def _fake_repo(root: Path, subjects: tuple[str, ...]) -> tuple[Path, str]:
    """A throwaway repository: a baseline commit and one commit per subject.

    The subjects are what the Worker wrote, so they are what the handover
    note reports on. The baseline is the commit the line started from, so
    every subject above it is a commit the line finished.
    """
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-b", "feat/worker-health")
    _git(root, "config", "user.email", "worker@example.invalid")
    _git(root, "config", "user.name", "worker")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "commit", "--no-gpg-sign", "--allow-empty", "-m", "chore: baseline")
    baseline = _git(root, "rev-parse", "HEAD")
    # Every subject commit also appends a line, so the diff the handoff
    # reports carries a real count: the whitelist handoff drops subject
    # text, and the count it keeps is the stat's own.
    note = root / "note.txt"
    for index, subject in enumerate(subjects):
        note.open("a", encoding="utf-8").write(f"{subject}\n")
        _git(root, "add", "note.txt")
        _git(
            root,
            "commit",
            "--no-gpg-sign",
            "-m",
            f"chore: {subject}",
        )
    return root, baseline


def _rotate(root: Path, baseline: str, tmp_path: Path) -> Path:
    """Rotate a line and return the handover note it wrote."""
    store = NativeCliSessionStore(tmp_path / "sessions.json")
    key = "thread:numeric-credential:kimi"
    store.set(key, "session-old")
    manager = WorkerSessionManager(
        path=tmp_path / "worker-sessions.json",
        store=store,
        worktree=root,
        baseline=baseline,
        task_file="task.md",
        report="report.md",
        handoff_dir=tmp_path / "handoffs",
    )
    result = manager.rotate(key, reason=FailureClass.CONTEXT_EXHAUSTED.value)

    assert result.handoff_path is not None
    return result.handoff_path


def test_the_handoff_note_carries_no_numeric_credential(tmp_path: Path) -> None:
    """The file the next Worker reads carries no numeric credential.

    Every shape is in one note, so the note is looked at as a whole: a
    digit string that survives in one line of it is a digit string that
    survived.
    """
    subjects = (
        tuple(text for _, text, _ in NUMERIC_CREDENTIALS)
        + (TOKEN_COUNT[1], BARE_TOKEN[1])
    )
    root, baseline = _fake_repo(tmp_path / "repo", subjects)

    handoff = _rotate(root, baseline, tmp_path)
    content = handoff.read_text(encoding="utf-8")

    for case, _, sentinel in NUMERIC_CREDENTIALS:
        assert sentinel not in content, case
    # A bare "token" cannot be told apart from a credential, so its number
    # goes the way every other credential value goes.
    assert BARE_TOKEN[2] not in content
    # Under the whitelist handoff, the commit subjects -- where every one of
    # these shapes was planted -- never reach the note at all: the note
    # carries the commit hashes and the diff-stat counts instead.
    for _, text, _ in NUMERIC_CREDENTIALS:
        assert text not in content
    assert TOKEN_COUNT[1] not in content
    assert BARE_TOKEN[1] not in content
    # v1: the note carries the change as numbers only. The file paths are
    # left to the next session, which runs `git diff --stat` itself, so the
    # count is in the note and the name of the file is not.
    assert "1 file changed, 6 insertions(+)" in content
    assert "note.txt" not in content


def test_the_count_reaches_the_handoff_note(tmp_path: Path) -> None:
    """The count the git output carries is in the note the Worker reads.

    The count is what the next Worker needs to know: how much of the
    context the turn used. The redaction takes the credential values out
    of the note and leaves the counts alone.
    """
    root, baseline = _fake_repo(tmp_path / "repo", (TOKEN_COUNT[1],))

    handoff = _rotate(root, baseline, tmp_path)
    content = handoff.read_text(encoding="utf-8")

    # The subject that carried the count is the worker's own text and never
    # reaches the whitelist note; the count the note keeps is the stat's.
    assert TOKEN_COUNT[2] not in content
    assert "max_tokens" not in content
    assert "1 file changed, 1 insertion(+)" in content
    assert "note.txt" not in content


def test_a_bare_token_does_not_reach_the_handoff_note(tmp_path: Path) -> None:
    """A bare "token" is a credential key, so its number does not reach it.

    The Planner's ruling: a number under a bare "token" cannot be told
    apart from a credential, so it is redacted like one.
    """
    root, baseline = _fake_repo(tmp_path / "repo", (BARE_TOKEN[1],))

    handoff = _rotate(root, baseline, tmp_path)
    content = handoff.read_text(encoding="utf-8")

    assert BARE_TOKEN[2] not in content
    assert "token=" not in content
    assert "1 file changed, 1 insertion(+)" in content
    assert "note.txt" not in content


@pytest.mark.parametrize(
    ("case", "text", "sentinel"),
    NUMERIC_CREDENTIALS,
    ids=[case for case, _, _ in NUMERIC_CREDENTIALS],
)
def test_one_numeric_credential_is_redacted_in_the_handoff_note(
    case: str, text: str, sentinel: str, tmp_path: Path
) -> None:
    """One shape on its own, so a leak names the shape that leaked."""
    root, baseline = _fake_repo(tmp_path / "repo", (text,))

    handoff = _rotate(root, baseline, tmp_path)
    content = handoff.read_text(encoding="utf-8")

    assert sentinel not in content, case
    assert text not in content, case
    # The subject never reaches the note: the commit is listed as a hash,
    # and the note's own count is the stat's.
    import re as _re

    assert _re.search(r"^[0-9a-f]{40}$", content, _re.MULTILINE), case
    assert "1 file changed, 1 insertion(+)" in content
    assert "note.txt" not in content


@pytest.mark.parametrize(
    ("case", "text", "sentinel"),
    NUMERIC_CREDENTIALS,
    ids=[case for case, _, _ in NUMERIC_CREDENTIALS],
)
def test_a_numeric_credential_is_redacted_to_the_marker(
    case: str, text: str, sentinel: str
) -> None:
    """The redaction itself leaves one marker and drops the digits."""
    redacted = redact_credentials(text)

    assert "[redacted]" in redacted, case
    assert sentinel not in redacted, case


def test_a_token_count_is_left_as_it_is() -> None:
    """A count is not a credential, so the redaction leaves it alone."""
    assert redact_credentials(TOKEN_COUNT[1]) == TOKEN_COUNT[1]
    assert redact_credentials({"max_tokens": 131072}) == {"max_tokens": 131072}


def test_a_bare_token_is_redacted_as_a_credential() -> None:
    """A bare "token" is a credential key, so its number goes with it.

    The exemption belongs to the keys that name a count, and a bare "token"
    is not one of them.
    """
    assert redact_credentials(BARE_TOKEN[1]) == "token=[redacted]"
    assert BARE_TOKEN[2] not in redact_credentials(BARE_TOKEN[1])
    assert redact_credentials({"token": 262144}) == {"token": "[redacted]"}


# ----------------------------------------------------------------------
# the fourth fix round: the same shapes, in a JSON body
#
# The key of a credential names it wherever it appears: quoted or not, in
# front of a string, a bare word or a number. These shapes go into the
# note the next Worker reads, and the note is the one file a human reads.
# ----------------------------------------------------------------------

# The shapes a credential takes in a JSON body, and the digit string each
# one carries. The digit string is what the note is searched for: a
# number under a credential key is a value and goes, and a count under a
# count key stays.
JSON_CREDENTIALS = (
    ("quoted key, bare number", '{"api_key": 9876543214}', "9876543214"),
    ("prefixed quoted key", '{"my_api_key": "9876543215"}', "9876543215"),
    ("authorization code", '{"authorization_code": "9876543217"}', "9876543217"),
)

# The name of the branch is part of the note as well, and it may carry a
# credential too.
BRANCH_CREDENTIAL = "9876543218"


def test_the_handoff_note_carries_no_credential_in_a_json_body(tmp_path: Path) -> None:
    """The file the next Worker reads carries no credential.

    Every shape is in one note -- a commit subject and the name of the
    branch -- so the note is looked at as a whole: a digit string that
    survives in one line of it is a digit string that survived.
    """
    subjects = tuple(text for _, text, _ in JSON_CREDENTIALS)
    root, baseline = _fake_repo(tmp_path / "repo", subjects)
    _git(root, "branch", "-m", f"feature/api_key={BRANCH_CREDENTIAL}")

    handoff = _rotate(root, baseline, tmp_path)
    content = handoff.read_text(encoding="utf-8")

    for case, _, sentinel in JSON_CREDENTIALS:
        assert sentinel not in content, case
    assert BRANCH_CREDENTIAL not in content
    # The tainted branch segment is replaced whole by the marker; the note
    # still says which branch family it is about.
    assert "feature/<redacted>" in content


def test_a_json_credential_is_redacted_by_its_key() -> None:
    """The redaction itself leaves one marker and drops the digits.

    The key names the credential, and the key may be quoted: the value
    goes, and the JSON around it comes out as it went in.
    """
    for case, text, sentinel in JSON_CREDENTIALS:
        redacted = redact_credentials(text)

        assert sentinel not in redacted, case
        assert "[redacted]" in redacted, case
        assert redacted.endswith("}"), case


# ----------------------------------------------------------------------
# v1: the note carries numbers and hashes. A path is a path, whatever
# says it, and a name can carry a credential.
# ----------------------------------------------------------------------

# An AWS access key id: a fixed prefix and sixteen upper case characters,
# with no separator in it. No key names it, so the key/value rule does not
# see it, and the shape rule needs a separator it does not have.
AWS_KEY = "AKIA" "IOSFODNN7EXAMPLE"


def test_a_file_named_changed_does_not_put_its_path_in_the_note(tmp_path: Path) -> None:
    """A file whose *name* carries the summary word does not leak its path.

    The full ``git diff --stat`` prints one line per file, and the summary
    line is found by looking for the word "changed" in it. A file called
    ``changed.md`` has that word in its own line, so a search by that word
    can take a path line for the summary -- and a path is a name the worker
    chose. The note is written from ``--shortstat``, which prints numbers
    only, so no path can reach it whatever the files are called.
    """
    root, baseline = _fake_repo(tmp_path / "repo", ("chore: something",))
    (root / "changed.md").write_text("x\n", encoding="utf-8")
    _git(root, "add", "changed.md")
    _git(root, "commit", "--no-gpg-sign", "-m", "chore: add a file called changed")

    handoff = _rotate(root, baseline, tmp_path)
    content = handoff.read_text(encoding="utf-8")

    assert "changed.md" not in content
    assert "2 files changed, 2 insertions(+)" in content


@pytest.mark.parametrize("field", ["task_file", "report"])
def test_a_credential_in_a_path_does_not_reach_the_note(field: str, tmp_path: Path) -> None:
    """A path the Planner gave is a path, and a path can carry a key.

    The task file and the report are named by the Planner, so they are
    trusted. A name is still a name: an AWS access key id has a fixed form
    that no key names, so the shared rule takes it out of the note -- and
    the rest of the path stays, so the next session still knows where the
    task file is.
    """
    root, baseline = _fake_repo(tmp_path / "repo", ("chore: something",))
    store = NativeCliSessionStore(tmp_path / "sessions.json")
    key = "thread:numeric-credential:kimi"
    store.set(key, "session-old")
    tainted = tmp_path / f"{AWS_KEY}-task.md"
    manager = WorkerSessionManager(
        path=tmp_path / "worker-sessions.json",
        store=store,
        worktree=root,
        baseline=baseline,
        task_file=str(tainted) if field == "task_file" else "task.md",
        report=str(tainted) if field == "report" else "report.md",
        handoff_dir=tmp_path / "handoffs",
    )
    result = manager.rotate(key, reason=FailureClass.CONTEXT_EXHAUSTED.value)

    content = result.handoff_path.read_text(encoding="utf-8")

    assert AWS_KEY not in content
    assert "[redacted]" in content


def test_a_commit_hash_is_not_a_credential(tmp_path: Path) -> None:
    """The hash the note carries is a fact about the work, not a secret.

    The unnamed-secret rule that takes every long run of characters would
    take the hash with it -- and the temporary directories of a path. The
    rule that is in place has a fixed form, so it leaves both alone.
    """
    root, baseline = _fake_repo(tmp_path / "repo", ("chore: something",))

    handoff = _rotate(root, baseline, tmp_path)
    content = handoff.read_text(encoding="utf-8")

    assert _git(root, "rev-parse", "HEAD") in content


def test_a_reason_that_only_looks_like_a_code_is_not_written(tmp_path: Path) -> None:
    """The reason is written as a code, and only a code this program knows.

    A caller may pass anything. A shape rule -- lower case letters, digits
    and underscores -- would let ``quota_ghp_abc123def456`` through, because
    it looks exactly like a code, and a credential that looks like a code is
    still a credential. The rule is a whitelist of the codes the program
    defines, so anything else reads as ``unknown``.
    """
    root, baseline = _fake_repo(tmp_path / "repo", ("chore: something",))
    store = NativeCliSessionStore(tmp_path / "sessions.json")
    key = "thread:numeric-credential:kimi"
    store.set(key, "session-old")
    manager = WorkerSessionManager(
        path=tmp_path / "worker-sessions.json",
        store=store,
        worktree=root,
        baseline=baseline,
        task_file="task.md",
        report="report.md",
        handoff_dir=tmp_path / "handoffs",
    )
    result = manager.rotate(key, reason="quota_ghp_abc123def456")

    content = result.handoff_path.read_text(encoding="utf-8")

    assert "ghp_" "abc123def456" not in content
    assert "- 轮换原因: unknown" in content


def test_a_known_reason_code_is_written_as_it_is(tmp_path: Path) -> None:
    """The codes the program defines are written as they are."""
    root, baseline = _fake_repo(tmp_path / "repo", ("chore: something",))
    store = NativeCliSessionStore(tmp_path / "sessions.json")
    key = "thread:numeric-credential:kimi"
    store.set(key, "session-old")
    manager = WorkerSessionManager(
        path=tmp_path / "worker-sessions.json",
        store=store,
        worktree=root,
        baseline=baseline,
        task_file="task.md",
        report="report.md",
        handoff_dir=tmp_path / "handoffs",
    )
    result = manager.rotate(key, reason=FailureClass.CONTEXT_EXHAUSTED.value)

    content = result.handoff_path.read_text(encoding="utf-8")

    assert "- 轮换原因: context_exhausted" in content


# ----------------------------------------------------------------------
# the edge of a credential is not a word boundary
#
# An underscore is a word character in a regular expression, so a word
# boundary next to one does not exist. A rule written with ``\b`` sees no
# edge there, and the key walks straight into the note -- in a task file
# name, in a report path, in a branch name, all of which the sanitizer
# lets the underscore through.
# ----------------------------------------------------------------------

AWS_KEY = "AKIA" "IOSFODNN7EXAMPLE"

#: The three places the note takes a name from outside the program, and
#: the clean text each of them must still carry after the key is taken out.
KEY_EDGES = {
    "task_file": ("prefix_{key}_suffix", ("prefix_", "_suffix")),
    "report": ("prefix_{key}_suffix", ("prefix_", "_suffix")),
    "branch": ("feature/x_{key}_y", ("feature/x_", "_y")),
}


@pytest.mark.parametrize("field", sorted(KEY_EDGES))
def test_a_key_next_to_an_underscore_does_not_reach_the_note(
    field: str, tmp_path: Path
) -> None:
    """A key that sits next to an underscore is still a key.

    The three places the note takes a name from outside the program are
    covered: the task file, the report and the branch. Each of them is
    written into the note, and each of them keeps its underscores, so a
    rule that uses a word boundary sees no edge next to the key and lets it
    through.
    """
    template, clean_parts = KEY_EDGES[field]
    tainted = template.format(key=AWS_KEY)
    root, baseline = _fake_repo(tmp_path / "repo", ("chore: something",))
    if field == "branch":
        _git(root, "branch", "-m", tainted)
    store = NativeCliSessionStore(tmp_path / "sessions.json")
    key = "thread:numeric-credential:kimi"
    store.set(key, "session-old")
    manager = WorkerSessionManager(
        path=tmp_path / "worker-sessions.json",
        store=store,
        worktree=root,
        baseline=baseline,
        task_file=tainted if field == "task_file" else "task.md",
        report=tainted if field == "report" else "report.md",
        handoff_dir=tmp_path / "handoffs",
    )
    result = manager.rotate(key, reason=FailureClass.CONTEXT_EXHAUSTED.value)

    content = result.handoff_path.read_text(encoding="utf-8")

    assert AWS_KEY not in content, field
    assert "[redacted]" in content, field
    # The rule takes the key out, not the name: what is left of the name
    # is still there, so the next session still knows where the file is.
    for part in clean_parts:
        assert part in content, field
