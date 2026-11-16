"""Tests for src.server.utils.sanitize.

The fleet dashboard renders log tails written by third-party CLIs, so the
redaction rules are pinned here rather than trusted: a credential-shaped span
must never reach a client, and an ordinary log line must survive recognizable.
"""

from __future__ import annotations

import time
from typing import Callable

import pytest

from src.server.utils.sanitize import sanitize_log_text, sanitize_log_tail

_ALPHABET = "abcdefghijklmnopqrstuvwxyz"


def _fastest_of(
    call: Callable[[str], str], payload: str, runs: int = 3
) -> float:
    """Fastest of ``runs`` timings of ``call(payload)``.

    A shared runner is noisy: taking the fastest run keeps a loaded machine
    from failing a bound that the code itself meets, and the fastest run is
    still the one that shows the cost of the work itself.
    """
    best = float("inf")
    for _ in range(runs):
        start = time.perf_counter()
        call(payload)
        best = min(best, time.perf_counter() - start)
    return best


class TestCredentialRedaction:
    @pytest.mark.parametrize(
        "line",
        [
            "Authorization: Bearer abcdef1234567890",
            "bearer=abcdef1234567890",
            "OPENAI_API_KEY=sk-" "ant-api03-ABCDEFGHIJKLMNOPQRSTUV",
            "aws key AKIA" "IOSFODNN7EXAMPLE here",
            "github token ghp_" "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
            "ghp_" "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
            "slack xoxb-" "1234567890-ABCDEFGHIJ",
            "glpat-" "ABCDEFGHIJKLMNOPQRST",
            "google AIza" "SyABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
            "npm_" "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
            '{"access_token": "abcdef1234567890"}',
            "X-Api-Key: 0123456789abcdef",
            "client_secret = 's3cr3t-value-here'",
            "session_id: 3f2504e0-4f89-11d3-9a0c-0305e82c3301",
            "token 3f2504e0-4f89-11d3-9a0c-0305e82c3301",
            "mail me at operator@example.com please",
            "-----BEGIN " "RSA PRIVATE KEY-----\nMIIEabc\n-----END RSA PRIVATE KEY-----",
            "jwt eyJ" "hbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w",
        ],
    )
    def test_redacts_credential_shapes(self, line):
        redacted = sanitize_log_text(line)
        assert "[REDACTED]" in redacted
        for fragment in (
            "abcdef1234567890",
            "sk-" "ant-api03",
            "AKIA" "IOSFODNN7EXAMPLE",
            "ghp_" "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
            "xoxb-" "1234567890",
            "glpat-",
            "AIzaSy",
            "npm_" "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
            "0123456789abcdef",
            "s3cr3t-value-here",
            "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
            "operator@example.com",
            "MIIEabc",
            "eyJhbGciOiJIUzI1NiJ9",
        ):
            if fragment in line:
                assert fragment not in redacted, line

    def test_keeps_ordinary_log_lines(self):
        line = "2026-11-02 12:00:01 INFO  agent started worktree=agent-fleet branch=feat/agent-fleet"
        assert sanitize_log_text(line) == line

    def test_keeps_token_counts_which_are_not_credentials(self):
        line = "tokens: 12345  prompt_tokens: 456  completion_tokens: 789"
        assert sanitize_log_text(line) == line

    def test_keeps_pytest_failure_lines(self):
        line = "FAILED tests/unit/server/test_agent_scanner.py::test_scan - assert 1 == 2"
        assert sanitize_log_text(line) == line

    def test_redacts_every_occurrence(self):
        redacted = sanitize_log_text("ghp_" "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 and ghp_" "ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ")
        assert redacted == "[REDACTED] and [REDACTED]"

    def test_redacts_a_compound_secret_name(self):
        # The keyword is qualified on both sides, so a name that has to *start*
        # with `secret` skips it entirely and the value sails through.
        line = "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        assert sanitize_log_text(line) == "[REDACTED]"
        assert "wJalrXUtnFEMI" not in sanitize_log_text(
            "aws_secret_access_key: 'wJalrXUtnFEMI/K7MDENG/bPxRfiCY'"
        )

    def test_redacts_every_cookie_in_a_header(self):
        # The value stops at the first `;` under a single-value rule, so every
        # cookie after the first one would be handed back verbatim.
        line = "Cookie: sessionid=abcdef1234567890; other=ghijklmnopqrst; third=stuvwxyz012345"
        redacted = sanitize_log_text(line)
        assert redacted == "[REDACTED]"
        for fragment in ("abcdef1234567890", "ghijklmnopqrst", "stuvwxyz012345"):
            assert fragment not in redacted
        assert sanitize_log_text("set-cookie: sid=abcdef1234567890; Path=/; HttpOnly") == "[REDACTED]"


class TestPartialPrivateKeyBlocks:
    """A tail is the last N bytes of a file, so a block can be cut in half."""

    def test_redacts_a_body_whose_begin_marker_is_outside_the_window(self):
        # Only the END marker is left, with the base64 that leads into it: a
        # rule that needs both markers to bound the block never fires and the
        # body comes back verbatim.
        line = "MIIEabcPrivateKeyBody1234567890\n-----END RSA PRIVATE KEY-----"
        assert sanitize_log_text(line) == "[REDACTED]"

    def test_redacts_a_bare_short_alphabetic_body(self):
        text = "abcdefghij\n-----END PRIVATE KEY-----"
        result = sanitize_log_text(text)
        assert "abcdefghij" not in result
        assert result == "[REDACTED]"

    def test_redacts_a_body_whose_end_marker_is_outside_the_window(self):
        # A key still being written has no END marker at all, so the body runs
        # to the end of the text and is redacted with it.
        line = "-----BEGIN " "RSA PRIVATE KEY-----\nMIIEabcPrivateKeyBody1234567890"
        redacted = sanitize_log_text(line)
        assert "MIIEabcPrivateKeyBody" not in redacted
        assert "-----BEGIN " "RSA PRIVATE KEY-----" not in redacted

    def test_keeps_the_log_text_around_an_orphan_end_marker(self):
        # Only the body and the marker go: the lines before and after survive.
        text = (
            "starting up\n"
            "MIIEabcPrivateKeyBody1234567890\n"
            "-----END RSA PRIVATE KEY-----\n"
            "done\n"
        )
        assert sanitize_log_text(text) == "starting up\n[REDACTED]\ndone"

    def test_redacts_a_multiline_body_before_an_orphan_end_marker(self):
        # The walk back covers every body line in the window, not just the one
        # touching the marker.
        text = (
            "MIIEabcPrivateKeyBody1234567890\n"
            "QWYhemVzdW5hbWUxMjM0NTY3ODkw\n"
            "-----END RSA PRIVATE KEY-----\n"
        )
        redacted = sanitize_log_text(text)
        assert "MIIEabcPrivateKeyBody" not in redacted
        assert "QWYhemVzdW5hbWU" not in redacted

    def test_redacts_a_block_that_shares_a_line_with_log_text(self):
        text = "dumping key: -----BEGIN " "RSA PRIVATE KEY----- then -----END RSA PRIVATE KEY----- ok"
        redacted = sanitize_log_text(text)
        assert "PRIVATE KEY" not in redacted
        assert redacted.startswith("dumping key: [REDACTED]")

    def test_redacts_a_body_line_a_cli_put_a_log_prefix_in_front_of(self):
        # A CLI writes a key body behind its own log prefix, so the line
        # holding the material is not base64 and nothing else. A walk that only
        # accepts a whole base64 line stops there and that line — timestamp,
        # level and body — comes back verbatim.
        text = (
            "2026-11-02T12:00:00.000Z INFO  dumping environment\n"
            "2026-11-02T12:00:01.000Z INFO  MIIEabcPrivateKeyBody1234567890\n"
            "-----END RSA PRIVATE KEY-----\n"
            "2026-11-02T12:00:02.000Z INFO  done\n"
        )
        redacted = sanitize_log_text(text)
        assert "MIIEabcPrivateKeyBody1234567890" not in redacted
        assert "-----END RSA PRIVATE KEY-----" not in redacted
        assert redacted == (
            "2026-11-02T12:00:00.000Z INFO  dumping environment\n"
            "[REDACTED]\n"
            "2026-11-02T12:00:02.000Z INFO  done"
        )

    @pytest.mark.parametrize(
        "prefix",
        [
            "2026-11-02 12:00:01 INFO",
            "Nov 02 12:00:01 INFO",
            "[INFO]",
            "INFO:",
            "info",
            "[2026-11-02 12:00:01] INFO",
            "2026-11-02T12:00:01.000Z",
            "12:00:01 INFO",
            "2026-11-02T12:00:01+08:00 INFO",
            "12345 INFO",
        ],
    )
    @pytest.mark.parametrize(
        "secret",
        ["abcdefgh", "abcdefghijklmnop", "a" * 19, "Abcdefghij"],
    )
    def test_redacts_short_alphabetic_body_behind_common_prefixes(self, prefix, secret):
        result = sanitize_log_text(f"{prefix} {secret}\n-----END RSA PRIVATE KEY-----")
        assert secret not in result
        assert "[REDACTED]" in result

    @pytest.mark.parametrize("marker", ["FAILED", "ERROR"])
    def test_preserves_bare_failure_markers_before_an_orphan_end(self, marker):
        result = sanitize_log_text(f"{marker}\n-----END RSA PRIVATE KEY-----")
        assert result == f"{marker}\n[REDACTED]"


    def test_walks_back_over_several_prefixed_body_lines(self):
        # Every body line in the window is absorbed, prefixed or not: the walk
        # stops at the first line that carries no body run, and the redaction
        # runs from the topmost one down to the marker.
        text = (
            "2026-11-02T12:00:01.000Z INFO  MIIEabcPrivateKeyBody1234567890\n"
            "2026-11-02T12:00:01.001Z DEBUG QWYhemVzdW5hbWUxMjM0NTY3ODkw\n"
            "-----END RSA PRIVATE KEY-----\n"
        )
        redacted = sanitize_log_text(text)
        assert "MIIEabcPrivateKeyBody" not in redacted
        assert "QWYhemVzdW5hbWU" not in redacted
        assert redacted == "[REDACTED]"

    def test_keeps_an_ordinary_line_above_an_orphan_end_marker(self):
        # The walk stops at a line whose tail is not a base64 run, so an
        # ordinary log line above a stray marker keeps its own text.
        text = (
            "2026-11-02T12:00:01.000Z INFO  agent started worktree=agent-fleet\n"
            "-----END RSA PRIVATE KEY-----\n"
        )
        assert sanitize_log_text(text) == (
            "2026-11-02T12:00:01.000Z INFO  agent started worktree=agent-fleet\n[REDACTED]"
        )

    def test_redacts_a_body_the_window_cut_short(self):
        # The last line of a body the window cut is too small to carry a run of
        # its own, so a walk that stops there hands back every line above it —
        # the whole key, not the fragment that ended it.
        text = (
            "2026-11-02T12:00:01.000Z INFO  MIIEabcPrivateKeyBody1234567890\n"
            "2026-11-02T12:00:01.001Z INFO  QWYhemVzdW5hbWUxMjM0NTY3ODkw\n"
            "2026-11-02T12:00:01.002Z INFO  abcd\n"
            "-----END RSA PRIVATE KEY-----\n"
        )
        redacted = sanitize_log_text(text)
        assert "MIIEabcPrivateKeyBody" not in redacted
        assert "QWYhemVzdW5hbWU" not in redacted
        assert "abcd" not in redacted

    def test_redacts_a_body_separated_from_the_marker_by_a_blank_line(self):
        # A blank line between the body and the marker ends the walk for a rule
        # that only accepts body lines, and the body comes back whole.
        text = (
            "2026-11-02T12:00:01.000Z INFO  MIIEabcPrivateKeyBody1234567890\n"
            "\n"
            "-----END RSA PRIVATE KEY-----\n"
        )
        assert sanitize_log_text(text) == "[REDACTED]"

    def test_redacts_material_that_does_not_end_the_line(self):
        # A CLI may trail a word behind the material, so a rule that only reads
        # a run at the end of the line misses the body and hands it back.
        text = (
            "2026-11-02T12:00:01.000Z INFO  MIIEabcPrivateKeyBody1234567890 (dumped)\n"
            "-----END RSA PRIVATE KEY-----\n"
        )
        redacted = sanitize_log_text(text)
        assert "MIIEabcPrivateKeyBody" not in redacted
        assert "-----END RSA PRIVATE KEY-----" not in redacted

    def test_redacts_a_whole_prefixed_body_the_window_cut_short(self):
        # The shape the finding was reported in: the BEGIN marker is outside
        # the window, so only the END marker and the body in front of it are
        # left, and every body line carries the CLI's own timestamp.
        body = [
            f"2026-11-02T12:00:{i:02d}.000Z INFO  MIIEabcPrivateKeyBody{i:04d}"
            for i in range(26)
        ]
        text = "\n".join(body) + "\n-----END RSA PRIVATE KEY-----\n"
        redacted = sanitize_log_text(text)
        for line in body:
            assert line not in redacted
        assert redacted == "[REDACTED]"

    def test_redacts_a_body_the_marker_is_separated_from_by_log_noise(self):
        # The walk back from the orphan marker stops at the first line that
        # carries no material, so a body a CLI interleaved its own log lines
        # with is handed back whole: the lines above the noise are key
        # material and the dashboard is not supposed to show them.
        text = (
            "2026-11-02T12:00:01.000Z INFO  MIIEabcPrivateKeyBody1234567890\n"
            "2026-11-02T12:00:02.000Z INFO  dumping environment\n"
            "2026-11-02T12:00:03.000Z INFO  rotating keys\n"
            "-----END RSA PRIVATE KEY-----\n"
        )
        redacted = sanitize_log_text(text)
        assert "MIIEabcPrivateKeyBody" not in redacted
        assert "-----END RSA PRIVATE KEY-----" not in redacted

    def test_redacts_a_body_the_marker_is_separated_from_by_a_fragment_and_noise(self):
        # A window cut leaves a fragment too small to carry material of its
        # own, and a CLI logs a line of its own behind it. Both are noise
        # between the body and the marker, and both have to be crossed.
        text = (
            "2026-11-02T12:00:01.000Z INFO  MIIEabcPrivateKeyBody1234567890\n"
            "2026-11-02T12:00:02.000Z INFO  QWYhemVzdW5hbWUxMjM0NTY3\n"
            "2026-11-02T12:00:03.000Z INFO  rotating keys\n"
            "-----END RSA PRIVATE KEY-----\n"
        )
        redacted = sanitize_log_text(text)
        assert "MIIEabcPrivateKeyBody" not in redacted
        assert "QWYhemVzdW5hbWU" not in redacted
        assert "-----END RSA PRIVATE KEY-----" not in redacted

    def test_redacts_a_body_the_marker_is_separated_from_by_a_blank_line_and_noise(self):
        # The blank line a log puts between two records is noise as well: the
        # walk has to cross it and the fragment behind it.
        text = (
            "2026-11-02T12:00:01.000Z INFO  MIIEabcPrivateKeyBody1234567890\n"
            "\n"
            "2026-11-02T12:00:03.000Z INFO  rotating keys\n"
            "-----END RSA PRIVATE KEY-----\n"
        )
        redacted = sanitize_log_text(text)
        assert "MIIEabcPrivateKeyBody" not in redacted
        assert "-----END RSA PRIVATE KEY-----" not in redacted

    def test_keeps_the_log_above_a_body_the_marker_is_separated_from_by_noise(self):
        # The noise below the body goes with the body, but the log above it
        # is not part of the block and stays readable.
        text = (
            "2026-11-02T12:00:00.000Z INFO  starting up\n"
            "2026-11-02T12:00:01.000Z INFO  MIIEabcPrivateKeyBody1234567890\n"
            "2026-11-02T12:00:02.000Z INFO  rotating keys\n"
            "-----END RSA PRIVATE KEY-----\n"
            "2026-11-02T12:00:04.000Z INFO  done\n"
        )
        redacted = sanitize_log_text(text)
        assert "MIIEabcPrivateKeyBody" not in redacted
        assert redacted.startswith("2026-11-02T12:00:00.000Z INFO  starting up\n")
        assert redacted.endswith("2026-11-02T12:00:04.000Z INFO  done")

    def test_redacts_a_prefixed_body_whose_material_is_shorter_than_twenty_characters(self):
        # The rule that reads a body line used to want a run of twenty or more
        # base64 characters before it called the line material. A body whose
        # lines carry less than that — the body of a short key, a block a CLI
        # wrote in small chunks — was not material at all: the walk back from
        # the orphan marker found nothing, the marker stood alone and every
        # body line came back whole. The BEGIN marker is outside the window
        # here, so the body is all the window holds.
        body = [
            f"2026-11-02T12:00:{i:02d}.000Z INFO  MIIEab{i:04d}"
            for i in range(26)
        ]
        text = "\n".join(body) + "\n-----END RSA PRIVATE KEY-----\n"
        redacted = sanitize_log_text(text)
        for line in body:
            assert line not in redacted
        assert redacted == "[REDACTED]"

    def test_redacts_a_prefixed_body_whose_material_carries_only_digits(self):
        # A body line that carries digits but no upper-case letter is still
        # key material: a word is written in letters, and the rule reads a
        # run that carries a digit as data.
        body = [
            f"2026-11-02T12:00:{i:02d}.000Z INFO  abcd{i:04d}"
            for i in range(26)
        ]
        text = "\n".join(body) + "\n-----END RSA PRIVATE KEY-----\n"
        redacted = sanitize_log_text(text)
        for line in body:
            assert line not in redacted
        assert redacted == "[REDACTED]"

    def test_redacts_a_prefixed_body_whose_material_is_short_and_word_shaped(self):
        # The residual of the "data or not" rule: a body line whose run is
        # shorter than twenty characters and carries no digit and no `+` is
        # read as a word, so the walk back from the orphan marker finds no
        # material at all and the whole body comes back. Base64 of a short
        # key, or a block a CLI wrote in small chunks, is that shape.
        body = [
            f"2026-11-02T12:00:{index:02d}.000Z INFO  "
            + "".join(_ALPHABET[(index + step) % 26] for step in range(10))
            for index in range(26)
        ]
        text = "\n".join(body) + "\n-----END RSA PRIVATE KEY-----\n"
        redacted = sanitize_log_text(text)
        for line in body:
            assert line not in redacted
        assert redacted == "[REDACTED]"

    def test_a_body_line_behind_a_prefix_is_one_token_where_prose_is_not(self):
        # What tells the two apart, and the price of it: the payload of a body
        # line is one opaque token, while an ordinary log line says something
        # and carries a second long token. A line shaped like a body — a
        # prefix and one token — is read as a body, so a log line whose whole
        # payload is a single long word goes with the key it sits above.
        prose = "2026-11-02T12:00:01.000Z INFO  rotating keys"
        token = "2026-11-02T12:00:02.000Z INFO  abcdefghij"
        redacted = sanitize_log_text(
            f"{prose}\n{token}\n-----END RSA PRIVATE KEY-----\n"
        )
        assert redacted == f"{prose}\n[REDACTED]"

    def test_keeps_a_word_above_an_orphan_end_marker(self):
        # The wider rule must not swallow the log with the key: a line whose
        # only long run is a lower-case word is a log line, and it stays
        # readable when a stray marker sits under it.
        text = (
            "2026-11-02T12:00:01.000Z INFO  starting up\n"
            "-----END RSA PRIVATE KEY-----\n"
        )
        assert sanitize_log_text(text) == (
            "2026-11-02T12:00:01.000Z INFO  starting up\n[REDACTED]"
        )

    @pytest.mark.parametrize(
        "prefix",
        [
            "2026-11-02 12:00:01 INFO  ",  # a date and a clock are two tokens
            "Nov 02 12:00:01 INFO  ",  # the syslog stamp
            "[INFO] ",  # a bracketed level
            "info ",  # a lower-case level
            "[2026-11-02 12:00:01] ",  # a bracketed timestamp
            "2026-11-02T12:00:01.000Z INFO  ",  # the one-token form
            "12345 INFO ",  # a pid and a level
            "2026-11-02 12:00:01,123 INFO  ",  # Python logging's clock
        ],
    )
    @pytest.mark.parametrize("body", ["abcdefghij", "a" * 16, "a" * 19])
    def test_redacts_a_body_behind_any_log_prefix(self, prefix, body):
        # A body of eight to nineteen letters carries no digit and no `+`, so
        # the prefix rule is the only thing that reads it as material — and a
        # prefix rule that accepts one timestamp token and an upper-case level
        # lets every other prefix a CLI writes hand the body back whole. The
        # BEGIN marker is outside the window here, so the body is all it holds.
        text = f"{prefix}{body}\n-----END RSA PRIVATE KEY-----\n"
        redacted = sanitize_log_text(text)
        assert body not in redacted
        assert redacted == "[REDACTED]"

    @pytest.mark.parametrize(
        "line",
        [
            "2026-11-02 12:00:01 INFO  agent started worktree=agent-fleet",
            "Nov 02 12:00:01 INFO  agent started worktree=agent-fleet",
            "[INFO] agent started worktree=agent-fleet",
            "info agent started worktree=agent-fleet",
            "[2026-11-02 12:00:01] agent started worktree=agent-fleet",
            "worker finished",
        ],
    )
    def test_keeps_an_ordinary_line_behind_any_log_prefix(self, line):
        # The wider prefix rule must not swallow the log with the key: an
        # ordinary line says something and carries a second long token, and a
        # lone word behind an ordinary word is prose rather than a body.
        text = f"{line}\n-----END RSA PRIVATE KEY-----\n"
        assert sanitize_log_text(text) == f"{line}\n[REDACTED]"

    @pytest.mark.parametrize("word", ["FAILED", "ERROR", "WARNING", "Traceback", "done"])
    def test_keeps_a_lone_word_above_an_orphan_end_marker(self, word):
        # A line that is one alphanumeric token is not key material: a word is
        # written in letters. Reading one as a body replaced it with
        # `[REDACTED]` whenever a stray marker sat under it, and the dashboard
        # reads the redacted tail for the failure markers that decide an
        # agent's status — so the agent that logged the failure read running.
        text = f"{word}\n-----END RSA PRIVATE KEY-----\n"
        assert sanitize_log_text(text) == f"{word}\n[REDACTED]"

    def test_keeps_a_long_run_or_a_data_character_as_material_on_a_bare_line(self):
        # The boundary of the rule above, on a line with no prefix at all:
        # twenty characters, or a character a word never carries, is data
        # whatever it spells and whatever line it sits on.
        for body in ("a" * 20, "MIIEabc123", "abcdefgh/ijk", "abcdefgh="):
            text = f"{body}\n-----END RSA PRIVATE KEY-----\n"
            assert sanitize_log_text(text) == "[REDACTED]", body

    @pytest.mark.parametrize(
        "line",
        [
            "2026-11-02T12:00:01.000Z INFO  Connection established",
            "2026-11-02T12:00:01.000Z WARN  TimeoutError while loading config",
            "2026-11-02T12:00:01.000Z INFO  MCPConfig loaded",
            "2026-11-02T12:00:01.000Z INFO  OAuthToken refreshed",
            "2026-11-02T12:00:01.000Z INFO  Request completed successfully",
        ],
    )
    def test_keeps_a_capitalized_line_above_an_orphan_end_marker(self, line):
        # A capital letter does not tell a body from a log line: English words
        # are capitalized, and so are the identifiers a log line carries. A
        # rule that reads a capital as key material swallows the log around a
        # key — the walk back from a stray marker takes every line above the
        # body that names anything, and the fleet view loses them.
        text = f"{line}\n-----END RSA PRIVATE KEY-----\n"
        assert sanitize_log_text(text) == f"{line}\n[REDACTED]"

    def test_keeps_the_log_above_a_body_that_carries_a_capital(self):
        # The same rule, one body below: the body goes with the marker and the
        # log line above it keeps its own text.
        text = (
            "2026-11-02T12:00:00.000Z INFO  Connection established\n"
            "2026-11-02T12:00:01.000Z INFO  MIIEabcPrivateKeyBody1234567890\n"
            "-----END RSA PRIVATE KEY-----\n"
        )
        redacted = sanitize_log_text(text)
        assert "MIIEabcPrivateKeyBody" not in redacted
        assert redacted == (
            "2026-11-02T12:00:00.000Z INFO  Connection established\n[REDACTED]"
        )


class TestRedactionCost:
    def test_a_long_dashed_name_is_not_quadratic(self):
        # The generic rule and the email rule scan a name's character class for
        # the credential word (or the `@`) behind it. Anchored on `\b`, which
        # also sits between a name character and the `-` inside one, every
        # character of a dashed run retried that scan: 8 KiB of `a-` took over
        # two seconds and blocked the request handler sanitizing the tail. A
        # linear pass scales with the input, so the bound below is generous
        # enough for a shared runner and still fails a quadratic regression.
        for payload in ("a-" * 4096, "a-" * 16384):
            start = time.perf_counter()
            sanitize_log_text(payload)
            elapsed = time.perf_counter() - start
            assert elapsed < 1.0, f"too slow: {elapsed:.2f}s for {len(payload)} chars"

    def test_a_keyword_heavy_name_is_not_quadratic(self):
        # The suffix is possessive because the separator can only match at the
        # end of the name; giving those characters back is what made a name
        # full of keywords expensive. What was left was the name's leading `*`
        # backtracking a character at a time and, at every position where the
        # keyword then matched, the possessive suffix re-walking the rest of the
        # name: `secret-` × 9,362 — the 64 KiB `log_tail_bytes` ceiling — took
        # 1.27 s in the request handler that sanitizes a tail. One lookahead per
        # name run makes the pass linear, so the bound below is generous enough
        # for a shared runner and still fails a quadratic regression.
        for repeats in (1000, 9362, 40000):
            start = time.perf_counter()
            sanitize_log_text("secret-" * repeats)
            elapsed = time.perf_counter() - start
            assert elapsed < 1.0, f"too slow: {elapsed:.2f}s for {repeats} repeats"

    def test_the_cost_of_a_keyword_heavy_name_grows_with_the_input_not_with_its_square(self):
        # The finding was reported as a measurement: `secret-` repeated took
        # 0.083 s / 0.343 s / 1.323 s at 16 / 32 / 64 KiB, so the cost grew
        # with the square of the input and the 64 KiB tail blocked the request
        # event loop. Four times the input has to stay near four times the
        # cost, and the 64 KiB ceiling — the largest tail the scanner reads —
        # has to stay far inside a request budget.
        costs = {}
        for kib in (16, 32, 64):
            payload = "secret-" * ((kib * 1024) // 7)
            costs[kib] = _fastest_of(sanitize_log_text, payload, runs=5)
        # The ceiling: the pre-fix pass needed 1.3 s for the 64 KiB tail.
        assert costs[64] < 0.5, f"too slow: {costs[64]:.3f}s for 64 KiB"
        # The shape: a quadratic pass costs sixteen times as much for four
        # times the input, where a linear one costs four times. The bound sits
        # between the two and carries no slack a slow runner could hide in,
        # because the timings above are the fastest of five runs.
        assert costs[64] <= 6 * costs[16] + 0.001, (
            f"cost grew with the square of the input: {costs}"
        )

    def test_the_cost_of_a_keyword_heavy_tail_grows_with_the_input_not_with_its_square(self):
        # The same shape is pinned through the entry point the request handler
        # uses: the scanner reads a tail of bytes and sanitizes it, and the
        # reported finding was that this pass blocked the request event loop.
        # `sanitize_log_tail` decodes the bytes and hands the text to the same
        # pass, so the cost has to grow with the input rather than with its
        # square.
        costs = {}
        for kib in (16, 32, 64):
            payload = ("secret-" * ((kib * 1024) // 7)).encode()
            costs[kib] = _fastest_of(sanitize_log_tail, payload, runs=5)
        assert costs[64] < 0.5, f"too slow: {costs[64]:.3f}s for 64 KiB"
        assert costs[64] <= 6 * costs[16] + 0.001, (
            f"cost grew with the square of the input: {costs}"
        )

    def test_a_marker_dense_tail_is_not_quadratic(self):
        # A marker that walked the lines above it on its own rescanned them
        # again for every marker in the text, and finding the marker's own line
        # with a backwards search from the start of the text rescanned the text
        # once per marker too. On a tail that alternates body lines and markers
        # the material lines lead all the way to the top, so each walk crossed
        # the whole tail: 16 / 32 / 64 KiB cost 0.019 / 0.073 / 0.298 s — four
        # times the input for sixteen times the work — in the request handler
        # that sanitizes a tail. Both are one lookup per marker now, so four
        # times the input has to cost four times the work and no more.
        unit = (
            "2026-11-02T12:00:01.000Z INFO  MIIEabcPrivateKeyBody1234567890\n"
            "-----END RSA PRIVATE KEY-----\n"
        )
        costs = {}
        for kib in (16, 32, 64):
            payload = unit * (kib * 1024 // len(unit))
            costs[kib] = _fastest_of(sanitize_log_text, payload, runs=5)
        # The ceiling: the quadratic pass needed 0.3 s for the 64 KiB tail.
        assert costs[64] < 0.2, f"too slow: {costs[64]:.3f}s for 64 KiB"
        # The shape: a quadratic pass costs sixteen times as much for four
        # times the input, where a linear one costs four times. The bound sits
        # between the two and carries no slack a slow runner could hide in,
        # because the timings above are the fastest of five runs.
        assert costs[64] <= 6 * costs[16] + 0.001, (
            f"cost grew with the square of the input: {costs}"
        )

    def test_a_marker_dense_tail_still_redacts_every_block(self):
        # The bound is a bound on the work, not on the redaction: a fast pass
        # that stopped linking bodies to markers would hand the key bodies
        # back. Every block of a marker-dense tail has to go.
        unit = (
            "2026-11-02T12:00:01.000Z INFO  MIIEabcPrivateKeyBody1234567890\n"
            "-----END RSA PRIVATE KEY-----\n"
        )
        repeats = 64 * 1024 // len(unit)
        payload = unit * repeats
        redacted = sanitize_log_text(payload)
        assert "MIIEabcPrivateKeyBody" not in redacted
        assert "-----END RSA PRIVATE KEY-----" not in redacted
        # Adjacent blocks share one span, so what survives is the log text
        # between them and nothing that carries key material.
        assert len(redacted) < len(payload) // 2
        assert "[REDACTED]" in redacted



class TestBounding:
    def test_truncates_long_text(self):
        redacted = sanitize_log_text("x" * 500, max_length=100)
        assert len(redacted) <= 100
        assert redacted.endswith("…")

    def test_keeps_text_under_the_limit(self):
        assert sanitize_log_text("short", max_length=100) == "short"

    def test_drops_control_characters(self):
        assert sanitize_log_text("a\x1b[31mb\x00c") == "abc"

    def test_rejects_non_strings(self):
        assert sanitize_log_text(None) == ""
        assert sanitize_log_text(123) == ""

    def test_tail_decodes_partial_utf8(self):
        tail = sanitize_log_tail("héllo wörld".encode()[:-1])
        assert "llo w" in tail
        assert "\udcff" not in tail

    def test_tail_of_empty_bytes(self):
        assert sanitize_log_tail(b"") == ""
