"""The shared live-output continuity guard (server + daemon)."""

from __future__ import annotations

import time

import pytest

from src.server.utils import output_guard as guard
from src.server.utils.output_guard import GuardState, scan


def run(chunks: list[str]) -> tuple[list[bool], GuardState, str]:
    """Scan chunks in order; returns per-chunk ok, final state, stored text."""
    state = GuardState()
    oks: list[bool] = []
    stored = ""
    for chunk in chunks:
        result = scan(state, chunk)
        state = result.state
        oks.append(result.ok)
        if result.ok:
            stored += result.text
    return oks, state, stored


def test_benign_prose_is_accepted_verbatim():
    oks, state, stored = run(
        ["Reading the config now.\n", "Found `max_retries = 3` and 3.14 in notes.\n"]
    )
    assert oks == [True, True] and not state.tainted
    assert stored.endswith("notes.\n")


def test_events_must_end_in_whitespace_so_no_run_is_ever_half_stored():
    ok, state, _ = run(["a complete line\n", "half a wor"])[0:3]
    assert ok == [True, False]
    assert state.tainted and state.reason == guard.REASON_UNALIGNED


def test_crlf_and_lone_cr_are_normalized_to_newlines():
    result = scan(GuardState(), "one\r\ntwo\rthree\r")
    assert result.ok and result.text == "one\ntwo\nthree\n"


@pytest.mark.parametrize(
    "text",
    [
        "a \x1b[31mred\x1b[0m line\n",
        "x\x9dtitle\x07 y\n",
        "bell\x07 \n",
        "nul\x00 \n",
        "del\x7f \n",
        "\x1b",
        # Every string-control form: OSC, DCS, SOS, PM, APC (7-bit and C1).
        "\x1b]0;title\x07 \n",
        "\x1bPq data\x1b\\ \n",
        "\x1bXsos\x1b\\ \n",
        "\x1b^pm\x1b\\ \n",
        "\x1b_apc\x1b\\ \n",
        "\x90dcs\x9c \n",
        "\x98sos\x9c \n",
        "\x9e pm\x9c \n",
        "\x9f apc\x9c \n",
        "\x9b31m red \n",
    ],
)
def test_any_terminal_control_taints(text):
    result = scan(GuardState(), text)
    assert not result.ok and result.state.tainted
    assert result.reason == guard.REASON_CONTROL


def test_a_control_sequence_split_over_events_never_reaches_storage():
    # ESC in one event, its payload in the next: the first is refused, so the
    # second is judged against a tainted stream and nothing is stored.
    oks, state, stored = run(["safe prefix \n", "\x1b", "]0;SECRETPAYLOAD\x07 \n"])
    assert oks == [True, False, False]
    assert stored == "safe prefix \n" and state.tainted


def test_credential_shapes_taint_before_the_sanitizers_full_length_rules():
    for secret in (
        "eyJhbGciOiJIUzI1NiJ9",
        "sk-" + "A" * 12,
        "ghp_" + "b" * 12,
        "github_pat_" + "c" * 12,
        "AKIA" + "D" * 12,
        "xoxb-" + "1" * 12,
    ):
        result = scan(GuardState(), f"the key is {secret} ok\n")
        assert not result.ok, secret


def test_long_jwt_split_across_many_events_never_completes():
    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" + "." + "A" * 700 + "." + "B" * 700
    # A single unbroken run longer than the cap taints at once...
    assert not scan(GuardState(), jwt + "\n").ok
    # ...and the same token chopped into aligned pieces taints at the first
    # piece that carries enough of the prefix.
    pieces = [jwt[i : i + 40] + " " for i in range(0, len(jwt), 40)]
    oks, state, stored = run(pieces)
    assert oks[0] is False and state.tainted
    assert stored == ""


def test_pending_key_with_more_than_16_kib_of_blanks_cannot_bypass():
    chunks = ["password\n"] + [" " * 4000] * 5 + ["=reviewsecret\n"]
    oks, state, stored = run(chunks)
    assert oks == [True] * 6 + [False]
    assert state.tainted and "reviewsecret" not in stored


def test_many_oversized_blank_gaps_before_a_value_are_compressed_not_forgotten():
    # A key the narrow rule does not name still reaches the sanitizer with the
    # gap collapsed: the value is withheld however long the gap was.
    chunks = ["session_token\n"] + [" " * 4000] * 5 + ["= abcdefghijk\n"]
    oks, state, stored = run(chunks)
    assert oks[-1] is False and "abcdefghijk" not in stored


@pytest.mark.parametrize("gap", [": ", " = ", ":=", " :", "= \n"])
def test_a_keyword_is_not_forgotten_behind_thousands_of_separator_characters(gap):
    """The id rule joins `session` to a UUID with any run of blanks, `:` and `=`;
    the key must survive a run far longer than the window."""
    chunk = gap * (guard.EVENT_TEXT_MAX // len(gap) - 1)
    oks, state, stored = run(
        ["session \n", chunk, chunk, "123e4567-e89b-12d3-a456-426614174000\n"]
    )
    assert oks[-1] is False and state.tainted
    assert "123e4567" not in stored


def test_gap_compression_keeps_one_character_and_the_separator_kind():
    assert guard._compress("key  :  : =  value") == "key  :  : =  value"  # short: exact
    assert guard._compress("a\n\n  b") == "a\n\n  b"
    assert guard._compress("x=y") == "x=y"
    assert guard._compress("a" + " " * 50 + "b") == "a b"  # blank-only: one blank
    assert guard._compress("a" + "\n" + " " * 50 + "b") == "a\nb"
    long_mixed = "k" + ": " * 500 + "v"
    squeezed = guard._compress(long_mixed)
    assert len(squeezed) == 1 + 20 + 8 + 1 and squeezed.startswith("k: : ")
    # A separator among thousands of blanks is not dropped...
    kept = guard._compress("session" + " " * 3000 + ":" + " " * 3000 + "v")
    assert kept == "session : v"
    # ...and a long contiguous run of separators is capped at eight, which is
    # all any threshold in the sanitizer counts.
    assert guard._compress("a" + "=" * 100 + "b") == "a" + "=" * 8 + "b"


def test_separator_glued_context_does_not_lose_the_key_when_the_window_is_trimmed():
    """`abcd : ` x520 compresses to one 2.6 KB run with no blanks in it; cutting
    the window at the first blank dropped the trailing `password`."""
    oks, state, stored = run(
        [("abcd : " * 520) + "password\n", " " * 4000, "=synthetic_value\n"]
    )
    assert oks == [True, True, False] and state.tainted
    assert "synthetic_value" not in stored


def test_window_cuts_land_on_a_gap_not_inside_a_glued_run():
    state = GuardState()
    for _ in range(3):
        result = scan(state, ("ab=" * 700) + "key\n")
        assert result.ok or True
        state = result.state if result.ok else GuardState()
    result = scan(GuardState(), ("w : " * 900) + "password\n")
    assert result.ok
    assert "password" in result.state.window
    assert len(result.state.window) <= guard.WINDOW_MAX


def _joined(*messages: str):
    """Scan model messages the way the daemon sends them: a message that ends
    mid-word gets a synthetic newline and is marked `join`."""
    state = GuardState()
    results = []
    for message in messages:
        join = not message[-1].isspace()
        result = scan(state, message + ("\n" if join else ""), join=join)
        state = result.state
        results.append(result)
    return results, state


@pytest.mark.parametrize(
    "messages",
    [
        # Reviewer repros: the head is short enough to look harmless on its own.
        ["eyJ", "hbGciOiJIUzI1NiJ9", " tail"],
        ["Bearer", " abcdef0123456789", " tail"],
        ["-----BEG", "IN PRIVATE KEY-----"],
        ["sk-", "A" * 20],
        ["ghp_ab", "c" * 20],
        ["password", ": hunter2"],
        ["pass", "word=value"],
        ["AKIA", "IOSFODNN7EXAMPLE"],
    ],
)
def test_a_credential_split_across_two_messages_is_still_one_token(messages):
    """The newline the daemon adds after a message that ends mid-word is
    display only; a continuation that starts the next message must be judged
    against the open word, not against a fresh line."""
    results, state = _joined(*messages)
    assert state.tainted
    assert results[0].ok  # the short head alone is harmless...
    assert not all(r.ok for r in results)  # ...the pair is refused


@pytest.mark.parametrize(
    "messages",
    [
        # Reviewer repro: displayed as `word / Bearer /  abcdef...`, the model
        # wrote `wordBearer abcdef...`; the scheme word is only a word in one.
        ["word", "Bearer", " abcdef0123456789", " tail"],
        ["word", "Basic", " dGVzdDp1c2VyOnBhc3N3b3Jk"],
        ["x", "password", ": hunter2value"],
        ["a1", "eyJ", "hbGciOiJIUzI1NiJ9"],
    ],
)
def test_both_the_displayed_and_the_written_text_are_judged(messages):
    results, state = _joined(*messages)
    assert state.tainted
    assert not all(r.ok for r in results)


def test_the_joined_windows_exist_only_while_they_differ_from_the_displayed_ones():
    state = scan(GuardState(), "plain line \n").state
    assert state.joined is None
    state = scan(state, "no end\n", join=True).state
    assert state.joined is not None and state.joined[0].endswith("no end")
    assert state.window.endswith("no end\n")
    # Enough clean text later, the newline has left the window and they agree.
    for _ in range(80):
        state = scan(state, ("filler words go here " * 8) + "\n").state
    assert state.joined is None


def test_joined_windows_round_trip_and_malformed_joined_state_fails_closed():
    state = scan(GuardState(), "see Bear\n", join=True).state
    assert state.joined is not None
    good = state.to_json()
    restored = GuardState.from_columns(state.window, False, good)
    assert restored == state
    for bad in (
        {k: v for k, v in good.items() if k != "joined"},
        {**good, "joined": "x"},
        {**good, "joined": {"window": "a"}},
        {**good, "joined": {**good["joined"], "text_only": "y" * (guard.WINDOW_MAX + 1)}},
    ):
        assert GuardState.from_columns(state.window, False, bad).tainted


def test_ordinary_adjacent_messages_still_flow():
    results, state = _joined("Reading the config.", "Found three files.", "Done.")
    assert all(r.ok for r in results) and not state.tainted
    assert results[-1].text == "Done.\n"


def test_a_joined_event_carries_its_open_word_in_the_window():
    state = scan(GuardState(), "see eyJ\n", join=True).state
    assert state.joined is not None and state.joined[0].endswith("eyJ")
    assert state.window.endswith("eyJ\n")  # displayed form keeps the line break
    assert not scan(state, "hbGciOiJIUzI1NiJ9 \n").ok


def test_a_join_event_must_end_with_the_synthetic_newline():
    assert not scan(GuardState(), "no newline", join=True).ok
    assert not scan(GuardState(), "\n", join=True).ok
    assert scan(GuardState(), "word\n", join=True).ok


def test_unjoined_unaligned_events_are_still_refused():
    assert scan(GuardState(), "half a wor").reason == guard.REASON_UNALIGNED


def test_a_chain_of_joined_events_cannot_grow_an_unbounded_word():
    state = GuardState()
    refused = False
    for _ in range(10):
        result = scan(state, "x" * 100 + "\n", join=True)
        state = result.state
        if not result.ok:
            refused = result.reason == guard.REASON_LONG_RUN
            break
    assert refused


def test_bearer_prefix_token_body_and_tail_across_events():
    oks, state, stored = run(["prefix Bearer \n", "abcdef0123456789xyz\n", " tail\n"])
    assert oks == [True, False, False]
    assert "abcdef0123456789xyz" not in stored and "tail" not in stored
    assert state.tainted


def test_taint_is_permanent_and_drops_the_window():
    first = scan(GuardState(), "token: x\nAuthorization: y\n")
    assert not first.ok
    later = scan(first.state, "perfectly harmless\n")
    assert not later.ok and later.state.tainted and later.text == ""
    assert first.state.window == ""


@pytest.mark.parametrize(
    "text",
    [
        "-----BEGIN " "PRIVATE KEY-----\n",
        "-----BEGIN " "RSA PRIVATE KEY-----\n",
        "see -----END PRIVATE KEY-----\n",
    ],
)
def test_pem_markers_taint(text):
    assert scan(GuardState(), text).reason == guard.REASON_PEM


def test_pem_marker_split_across_events_still_taints():
    oks, _, stored = run(["-----BEGIN\n", " PRIVATE KEY-----\n", "MIIEvQ\n"])
    assert oks[0] is False  # the marker prefix alone is already a marker
    assert "MIIEvQ" not in stored


@pytest.mark.parametrize(
    "key",
    [
        "Authorization:",
        "cookie=",
        "Set-Cookie :",
        '"password":',
        "api_key =",
        "client_secret:",
        "x-api-key:",
        "access_token=",
        "private-key:",
    ],
)
def test_credential_keys_taint_even_without_a_value(key):
    assert scan(GuardState(), f"{key}\n").reason == guard.REASON_KEY


def test_key_split_from_its_separator_by_events_and_blanks():
    oks, _, _ = run(["the api_key\n", "   \n", "  \n", "= v\n"])
    assert oks == [True, True, True, False]


def test_email_and_uuid_after_a_key_word_are_redaction_candidates():
    assert scan(GuardState(), "mail alice@example.com now\n").reason == (
        guard.REASON_REDACTION
    )
    assert not scan(
        GuardState(), "key: 123e4567-e89b-12d3-a456-426614174000 \n"
    ).ok


def test_run_cap_is_enforced_but_urls_below_it_are_fine():
    ok_url = "https://example.com/" + "a" * 400 + "\n"
    assert scan(GuardState(), ok_url).ok
    assert scan(GuardState(), "x" * (guard.RUN_MAX + 1) + "\n").reason == (
        guard.REASON_LONG_RUN
    )
    assert scan(GuardState(), "x" * guard.RUN_MAX + "\n").ok


def test_event_size_cap():
    assert scan(GuardState(), ("word " * 1000) + "\n").reason == guard.REASON_OVERSIZE


def test_window_stays_bounded_and_keeps_whole_runs():
    state = GuardState()
    for index in range(400):
        result = scan(state, f"line {index} " + "w" * 100 + "\n")
        assert result.ok
        state = result.state
    assert len(state.window) <= guard.WINDOW_MAX
    assert state.window[:1].isspace()  # a cut never lands inside a run


def test_persisted_state_round_trip_and_malformed_state_fails_closed():
    result = scan(GuardState(), "hello there \n")
    columns = (result.state.window, False, result.state.to_json())
    assert GuardState.from_columns(*columns).window == result.state.window
    good = result.state.to_json()
    for bad in (
        (None, False, good),
        ("x" * (guard.WINDOW_MAX + 1), False, good),
        ("", False, {}),
        ("", False, {**good, "v": 99}),
        ("", False, {**good, "v": 1}),  # the single-window shape of version 1
        ("", False, "not-a-dict"),
    ):
        restored = GuardState.from_columns(*bad)
        assert restored.tainted and restored.reason == guard.REASON_STATE
    tainted = GuardState.from_columns("anything", True, {**good, "reason": "batch_gap"})
    assert tainted.tainted and tainted.window == "" and tainted.reason == "batch_gap"


@pytest.mark.parametrize("field", ["no_reasoning", "no_tool", "text_only"])
def test_a_missing_projection_window_taints_instead_of_defaulting_to_empty(field):
    """Reviewer repro: accept text, reasoning and a tool name, delete one window
    from the persisted state, restore, append the value. The lost context must
    not read as an empty one."""
    state = GuardState()
    for text, channel in (
        ("password\n", "text"),
        ("thinking\n", "reasoning"),
        ("exec_command\n", "tool"),
    ):
        state = scan(state, text, channel).state
    good = state.to_json()
    degraded = {key: value for key, value in good.items() if key != field}
    restored = GuardState.from_columns(state.window, False, degraded)
    assert restored.tainted and restored.reason == guard.REASON_STATE
    assert not scan(restored, "=ordinary-value\n").ok
    assert GuardState.from_columns(state.window, False, {**good, field: None}).tainted is not False
    intact = GuardState.from_columns(state.window, False, good)
    assert not intact.tainted
    assert not scan(intact, "=ordinary-value\n").ok  # and the real state refuses it


def test_state_reason_never_contains_stream_text():
    result = scan(GuardState(), "password: hunter2hunter2\n")
    assert "hunter2" not in repr(result.state) and "hunter2" not in result.reason


@pytest.mark.parametrize(
    "text",
    [
        '"' * 400 + "://" * 50 + "\n",
        ("a://" * 120 + " ") * 8 + "\n",
        "secret-" * 70 + "\n",
        ("api_key" + "=" * 3 + " ") * 150 + "\n",
        ("@" + "a." * 60 + " ") * 30 + "\n",
        "'" * 3000 + " \n",
        ("token" + " " * 30) * 100 + "\n",
    ],
)
def test_adversarial_inputs_scan_in_bounded_time(text):
    started = time.perf_counter()
    state = GuardState()
    for _ in range(5):
        result = scan(state, text[: guard.EVENT_TEXT_MAX])
        state = result.state
    assert time.perf_counter() - started < 1.0


@pytest.mark.parametrize(
    "text",
    [
        "pass\u200bword: hunter2\n",  # zero-width space inside the key
        "pa\u00adssword = x\n",  # soft hyphen
        "api\u2060_key: x\n",  # word joiner
        "password\uff1dhunter2\n",  # full-width equals
        "\uff50\uff41\uff53\uff53\uff57\uff4f\uff52\uff44: x\n",  # full-width key
        "Bear\u200ber abcdef0123456789\n",
        "s\u200bk-" + "A" * 24 + "\n",
    ],
)
def test_invisible_and_compatibility_look_alikes_do_not_hide_a_key(text):
    assert not scan(GuardState(), text).ok


@pytest.mark.parametrize(
    "text",
    ["a \u202eevil\u202c line\n", "tag \U000e0041\U000e0042 hidden\n", "iso \u2066x\u2069 \n"],
)
def test_bidi_controls_and_tag_characters_taint(text):
    assert scan(GuardState(), text).reason == guard.REASON_CONTROL


def test_zero_width_characters_alone_never_make_a_boundary():
    # The visible text must still end in whitespace, and a zero-width tail does
    # not count as one.
    assert not scan(GuardState(), "word\u200b").ok
    assert not scan(GuardState(), "\u200b").ok
    assert scan(GuardState(), "word\u200b \n").ok


def test_the_original_text_is_stored_and_only_the_decision_is_folded():
    result = scan(GuardState(), "caf\u00e9 na\u00efve \u2603 snow\n")
    assert result.ok and result.text == "caf\u00e9 na\u00efve \u2603 snow\n"
    zwj = scan(GuardState(), "family \U0001f468\u200d\U0001f469 ok\n")
    assert zwj.ok and "\u200d" in zwj.text


# -- soundness: what the scanner accepts is stable under the sanitizer as a whole --

_KEYWORDS = [
    "session", "token", "secret", "password", "passwd", "key", "auth",
    "authorization", "cookie", "api_key", "credential", "signature", "nonce",
    "client_secret", "access-key", "bearer", "Bearer", "basic", "Basic", "Cookie",
    "set-cookie", "x-api-key", "otp", "refresh_token", "private_key",
]
_SEPS = [":", "=", ": ", " = ", '":', "': ", ": :", ":::", "= =", ":=:=", ' ":" ']
_GAPS = [
    "", " ", "  ", "\n", "\n\n", " " * 40, " " * 500, ": " * 300, "=" * 200, "\t",
    # Long enough to push a keyword out of any fixed-size window.
    ": " * 1500, " = " * 1000, ":" * 400 + " " + ":" * 400 + " " + ":" * 400, " " * 3000,
    # A separator in the middle of thousands of blanks, and sub-runs of '='.
    " " * 800 + ":" + " " * 800, " " * 500 + "=" + " " * 500 + "==", "= = ====", ": " * 5 + "=" * 7,
]
_VALUES = [
    "====ab",
    "========",
    "123e4567-e89b-12d3-a456-426614174000",
    "abcdefghij",
    "123456",
    "x" * 7,
    "eyJhbGciOiJIUzI1NiJ9",
    "sk-" + "A" * 20,
    "user@example.com",
    "plain",
    "42",
    "[REDACTED]",
    "AKIA" "IOSFODNN7EXAMPLE",
    "ghp_" + "b" * 20,
    "dGVzdDp1c2VyOnBhc3N3b3Jk",
    "a=b;c=d",
]
_GLUE = ["abcd : ", "a=", "k: ", "z :: ", "w = ", "q:"]
_FILLER = ["the", "build", "passed", "in", "3.2s", "see", "notes", "ok.", "`x`", "(a)"]


def _document(rng) -> list[str]:
    """A stream of whitespace-aligned events cut from adversarial text.

    The text is built first and cut afterwards at random whitespace positions,
    so a key, its gap and its value land in different events in every way the
    transport could split them.
    """
    parts: list[str] = []
    for _ in range(rng.randint(2, 14)):
        roll = rng.random()
        if roll < 0.45:
            piece = rng.choice(_KEYWORDS) + rng.choice(_GAPS) + rng.choice(_SEPS)
            piece += rng.choice(_GAPS) + rng.choice(_VALUES)
        elif roll < 0.7:
            piece = rng.choice(_KEYWORDS) + rng.choice(_GAPS)
        elif roll < 0.85:
            piece = rng.choice(_SEPS) + rng.choice(_GAPS) + rng.choice(_VALUES)
        elif roll < 0.92:
            # Many short words glued by separators: compressing the gaps makes
            # one long run with no whitespace in it.
            piece = (
                rng.choice(_GLUE) * rng.randint(100, 1000)
                + rng.choice(_KEYWORDS)
                + rng.choice(["\n", " \n", "  "])
                + rng.choice(_GAPS)
                + rng.choice(_SEPS)
                + rng.choice(_VALUES)
            )
        else:
            piece = " ".join(rng.choice(_FILLER) for _ in range(rng.randint(1, 6)))
        parts.append(piece + rng.choice(["\n", " ", "  \n"]))
    document = "".join(parts)
    events: list[str] = []
    start = 0
    last_ws = -1
    for index, char in enumerate(document):
        if char.isspace():
            last_ws = index
            if rng.random() < 0.08:
                events.append(document[start : index + 1])
                start = index + 1
                last_ws = -1
                continue
        if index + 1 - start >= guard.EVENT_TEXT_MAX and last_ws >= start:
            events.append(document[start : last_ws + 1])
            start = last_ws + 1
            last_ws = -1
    if start < len(document):
        events.append(document[start:])
    return [event for event in events if event]


_FRAGMENTS = [
    "====ab", "=", "==", "Basic", " ====", "=" * 7,
    "word", "x", "a1", "Bear", "er", "Bearer", "Basic", "eyJ", "hbGciOiJIUzI1NiJ9",
    "sk-", "AAAAAAAAAAAAAAAAAAAA", "pass", "word", "password", ": ", "=", " abcdef0123456789",
    " dGVzdDp1c2VyOnBhc3N3b3Jk", "-----BEG", "IN PRIVATE KEY-----", "token", " hunter2value",
]


def _message_document(rng) -> list[tuple[str, bool]]:
    """Short model messages that mostly end mid-word, as the daemon sends them."""
    out: list[tuple[str, bool]] = []
    for _ in range(rng.randint(2, 7)):
        message = rng.choice(_FRAGMENTS)
        if rng.random() < 0.2:
            out.append((message + " \n", False))
        else:
            out.append((message + "\n", True))
    return out


def _cut(document: str, rng) -> list[str]:
    events: list[str] = []
    start = 0
    last_ws = -1
    for index, char in enumerate(document):
        if char.isspace():
            last_ws = index
            if rng.random() < 0.08:
                events.append(document[start : index + 1])
                start, last_ws = index + 1, -1
                continue
        if index + 1 - start >= guard.EVENT_TEXT_MAX and last_ws >= start:
            events.append(document[start : last_ws + 1])
            start, last_ws = last_ws + 1, -1
    if start < len(document):
        events.append(document[start:])
    return [event for event in events if event]


def _targeted_document(rng) -> list[str]:
    """An attack shape: one event that is mostly a separator-glued stretch (its
    gap-compressed form outgrows the window) and ends with a key, then
    blank-only events, then the separator and a value."""
    unit = rng.choice(["abcd : ", "k: ", "ab :", "abc= ", "a : b= "])
    keyword = rng.choice(_KEYWORDS)
    ending = keyword + rng.choice(["\n", " ", "  \n"])
    reps = (guard.EVENT_TEXT_MAX - len(ending) - 1) // len(unit) - rng.randint(0, 40)
    events = [unit * reps + ending]
    for _ in range(rng.randint(0, 3)):
        events.append(rng.choice([" " * 4000, "\n" * 100, ": " * 1500, " "]))
    events.append(rng.choice(_SEPS) + rng.choice(_GAPS) + rng.choice(_VALUES) + "\n")
    return events


_PROJECTIONS = {
    "all": {"text", "reasoning", "tool"},
    "no_reasoning": {"text", "tool"},
    "no_tool": {"text", "reasoning"},
    "text_only": {"text"},
}


def test_every_accepted_stream_is_unchanged_by_the_sanitizer_as_a_whole():
    """The invariant the design rests on, checked on random adversarial streams
    and on every way a watcher may render them (all channels, without
    reasoning, without tool names, text alone).

    If any sanitizer rule could match across a stretch a scanner window
    forgot, some accepted projection would be rewritten when sanitized whole.
    """
    import random

    from src.server.utils.sanitize import sanitize_log_text

    rng = random.Random(20261105)
    accepted_streams = 0
    for case in range(400):
        events = _targeted_document(rng) if case % 3 == 0 else _document(rng)
        channels = [
            rng.choice(["text", "text", "text", "reasoning", "tool"]) for _ in events
        ]
        state = GuardState()
        stored: list[tuple[str, str, bool]] = []
        for event, channel in zip(events, channels):
            join = channel != "tool" and rng.random() < 0.2
            if join:
                # A message that ends mid-word: drop its trailing blanks and
                # give it the daemon's synthetic newline.
                event = event.rstrip() + "\n"
                if event == "\n":
                    join = False
            result = scan(state, event, channel, join=join)
            state = result.state
            if not result.ok:
                break
            stored.append((channel, result.text, join))
        for name, shown in _PROJECTIONS.items():
            # Both what is displayed and what the model wrote (the synthetic
            # newline of a joined event removed) must be sanitizer-stable.
            displayed = "".join(t for c, t, _ in stored if c in shown)
            adjacent = "".join(
                t[:-1] if j else t for c, t, j in stored if c in shown
            )
            for text in (displayed, adjacent):
                whole = sanitize_log_text(text, max_length=0, strip=False)
                assert whole == text, (case, name, events)
        accepted_streams += bool(stored)
    assert accepted_streams > 300  # the generator is not just producing refusals


def test_short_joined_messages_are_sound_in_every_view_and_projection():
    """Messages that end mid-word, as the daemon sends them: the displayed text
    (synthetic newlines in) and the model's own text (newlines out) must both
    survive the sanitizer, for every projection."""
    import random

    from src.server.utils.sanitize import sanitize_log_text

    rng = random.Random(7919)
    refused_some = accepted_some = 0
    for case in range(800):
        messages = _message_document(rng)
        channels = [rng.choice(["text", "text", "reasoning", "tool"]) for _ in messages]
        state = GuardState()
        stored: list[tuple[str, str, bool]] = []
        for (text, join), channel in zip(messages, channels):
            if channel == "tool":
                join = False
                text = text.rstrip() + "\n"
            result = scan(state, text, channel, join=join)
            state = result.state
            if not result.ok:
                refused_some += 1
                break
            stored.append((channel, result.text, join))
        accepted_some += bool(stored)
        for name, shown in _PROJECTIONS.items():
            displayed = "".join(t for c, t, _ in stored if c in shown)
            written = "".join(t[:-1] if j else t for c, t, j in stored if c in shown)
            for text in (displayed, written):
                assert sanitize_log_text(text, max_length=0, strip=False) == text, (
                    case, name, messages, channels,
                )
    assert refused_some > 50 and accepted_some > 500


def test_hiding_reasoning_cannot_join_a_key_to_its_value():
    oks, state, stored = run_channels(
        [("text", "password\n"), ("reasoning", "the worker pauses here\n"), ("text", "=ordinary-value\n")]
    )
    assert oks == [True, True, False] and state.tainted
    assert "ordinary-value" not in stored


def test_hiding_tool_names_cannot_join_a_key_to_its_value():
    oks, state, _ = run_channels(
        [("text", "password\n"), ("tool", "exec_command\n"), ("text", "=ordinary-value\n")]
    )
    assert oks == [True, True, False] and state.tainted


def test_hiding_both_cannot_either():
    oks, _, _ = run_channels(
        [
            ("text", "password\n"),
            ("reasoning", "thinking\n"),
            ("tool", "exec\n"),
            ("text", "=ordinary-value\n"),
        ]
    )
    assert oks == [True, True, True, False]


def test_a_reasoning_event_does_not_enter_the_windows_that_hide_it():
    state = scan(GuardState(), "visible words\n").state
    after = scan(state, "private thoughts\n", "reasoning").state
    assert "private" in after.window and "private" in after.no_tool
    assert "private" not in after.no_reasoning and "private" not in after.text_only


def test_projection_windows_round_trip_and_malformed_extras_fail_closed():
    state = scan(scan(GuardState(), "alpha beta\n").state, "gamma\n", "reasoning").state
    restored = GuardState.from_columns(state.window, False, state.to_json())
    assert restored == state
    for bad in (
        {"no_reasoning": 5},
        {"no_tool": "x" * (guard.WINDOW_MAX + 1)},
        {"text_only": None},
    ):
        columns = state.to_json() | bad
        assert GuardState.from_columns(state.window, False, columns).tainted


def run_channels(events: list[tuple[str, str]]):
    state = GuardState()
    oks: list[bool] = []
    stored = ""
    for channel, text in events:
        result = scan(state, text, channel)
        state = result.state
        oks.append(result.ok)
        if result.ok:
            stored += result.text
    return oks, state, stored


def test_unknown_channels_are_refused():
    assert not scan(GuardState(), "hello\n", "thinking").ok


def test_sanitize_output_text_is_linear_on_repeated_unterminated_introducers():
    import time

    from src.server.utils.sanitize import sanitize_output_text

    for introducer in ("\x1bP", "\x1b]", "\x1bX", "\x1b^", "\x1b_", "\x9d", "\x90"):
        started = time.perf_counter()
        cleaned = sanitize_output_text(introducer * 32_000, limit=0)
        assert time.perf_counter() - started < 1.0, introducer
        assert cleaned == ""
    # Short text keeps its exact behaviour: terminated strings go, text stays.
    assert sanitize_output_text("a\x1b]0;t\x07b\x1bPq\x1b\\c") == "abc"
    assert sanitize_output_text("keep \x1b[31mred\x1b[0m") == "keep red"


def test_has_control_is_cheap_and_matches_the_scanner():
    assert guard.has_control("a\x1bPq") and guard.has_control("x‮y")
    assert not guard.has_control("plain text\nwith\ttabs")
    import time

    started = time.perf_counter()
    assert guard.has_control("\x1bP" * 100_000)
    assert time.perf_counter() - started < 0.5


# -- default-ignorable and format characters ---------------------------------


def _invisible_code_points() -> list[int]:
    """Every format character (Cf) and every Default_Ignorable_Code_Point range."""
    import unicodedata

    found = {
        cp
        for cp in range(0x110000)
        if unicodedata.category(chr(cp)) == "Cf" and not 0xD800 <= cp <= 0xDFFF
    }
    for low, high in (
        (0x034F, 0x034F), (0x115F, 0x1160), (0x17B4, 0x17B5), (0x180B, 0x180F),
        (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8),
        (0x1BCA0, 0x1BCA3), (0xE0100, 0xE01EF),
    ):
        found.update(range(low, high + 1))
    return sorted(found)


def test_every_format_and_default_ignorable_character_is_folded_or_refused():
    pattern = guard._FOLD_OUT_RE
    taint = guard._HIDDEN_TAINT_RE
    missing = [
        hex(cp)
        for cp in _invisible_code_points()
        if not (pattern.match(chr(cp)) or taint.match(chr(cp)))
    ]
    assert missing == []


_SHAPES = [
    "sk-" "ABCDEFGHIJKLMNOPQRSTUV",
    "ghp_" "ABCDEFGHIJKLMNOPQRSTUV",
    "eyJhbGciOiJIUzI1NiJ9.abcdefghij.klmnopqrst",
    "AKIA" "IOSFODNN7EXAMPLE",
    "Bearer abcdef0123456789",
    "password: hunter2value",
    "-----BEGIN " "PRIVATE KEY-----",
]


def test_an_invisible_character_at_any_offset_of_a_credential_shape_does_not_hide_it():
    """Reviewer sweep: 29 of 30 default-ignorable code points inserted into a
    key were accepted. Insert every one at every offset of the key shape (and a
    spread of offsets for the others)."""
    points = _invisible_code_points()
    sk = _SHAPES[0]
    leaked = [
        (hex(cp), at)
        for cp in points
        for at in range(1, len(sk))
        if scan(GuardState(), "see " + sk[:at] + chr(cp) + sk[at:] + " now\n").ok
    ]
    assert leaked == []
    others = [
        (hex(cp), shape[:12], at)
        for cp in points
        for shape in _SHAPES[1:]
        for at in (2, len(shape) // 2, len(shape) - 3)
        if scan(GuardState(), "see " + shape[:at] + chr(cp) + shape[at:] + "\n").ok
    ]
    assert others == []


def test_lone_surrogates_taint_instead_of_failing_later():
    result = scan(GuardState(), "bad \ud800 text\n")
    assert not result.ok and result.reason == guard.REASON_CONTROL


def test_folded_expansion_is_bounded():
    # U+FDFA folds to eighteen characters: an unbroken run of them is refused
    # at once, not after a megabyte of folded text is scanned.
    import time

    started = time.perf_counter()
    result = scan(GuardState(), "ﷺ" * 1000 + "\n")
    assert not result.ok
    assert time.perf_counter() - started < 0.5


def test_a_warm_window_scan_stays_cheap():
    """A full batch of plain text against warm windows must not cost seconds
    (the decision runs while the session and lease rows are locked)."""
    import time

    state = GuardState()
    filler = ("lorem ipsum dolor sit amet " * 40)[:3990] + "\n"
    for _ in range(4):  # warm: fill every window
        state = scan(state, filler).state
    started = time.perf_counter()
    for _ in range(64):
        result = scan(state, filler)
        assert result.ok
        state = result.state
    assert time.perf_counter() - started < 3.0


def test_unbroken_runs_are_checked_in_linear_time():
    import time

    started = time.perf_counter()
    assert scan(GuardState(), "x" * 3990 + "\n").reason == guard.REASON_LONG_RUN
    assert scan(GuardState(), ("y" * 511 + " ") * 7 + "\n").ok
    assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize(
    "messages",
    [
        ["Basic", " YTpi", " tail"],  # `a:b`
        ["Basic", " dGVzdDpwYXNz"],
        ["Basic YTpi tail"],
        ["Basic", " dGVz", "dDpw", "YXNz"],  # fragmented, joined
        ["Basic\n", "abc123xyz"],
    ],
)
def test_a_basic_credential_is_refused_even_when_it_is_short_or_fragmented(messages):
    results, state = _joined(*messages)
    assert state.tainted
    assert not results[-1].ok or not all(r.ok for r in results)
    stored = "".join(r.text for r in results if r.ok)
    assert "YTpi" not in stored and "dGVz" not in stored.replace("dGVz\n", "x")


@pytest.mark.parametrize(
    "prose",
    [
        "basic usage of the tool\n",
        "Basic Usage of caching\n",
        "the basic HTTP auth flow\n",
        "a basic idea\n",
        "BASIC code\n",
    ],
)
def test_the_word_basic_in_prose_is_not_a_credential(prose):
    assert scan(GuardState(), prose).ok


@pytest.mark.parametrize(
    "token",
    ["MDA6", "YTpi", "dGVzdDpwYXNz", "QUJDRA==", "ab+/", "Zm9v", "a1b2", "AbCd"],
)
@pytest.mark.parametrize("shape", ["whole", "split"])
def test_short_basic_tokens_are_withheld_whole_or_fragmented(token, shape):
    if shape == "whole":
        results, state = _joined(f"Basic {token} tail")
    else:
        results, state = _joined("Basic", " " + token, " tail")
    assert state.tainted
    stored = "".join(r.text for r in results if r.ok)
    assert token not in stored and "tail" not in stored


_UUID = "123e4567-e89b-12d3-a456-426614174000"


@pytest.mark.parametrize("lead", ["token ", "session: ", "key=", 'secret "', "auth\n", "OTP  :  "])
def test_a_uuid_after_a_key_word_is_withheld_once_its_first_groups_are_out(lead):
    """The sanitizer's ``session <uuid>`` rule needs the complete id; a message
    that stopped one character short stored 35 of its 36 characters."""
    for cut in range(1, len(_UUID)):
        results, state = _joined(f"see {lead}{_UUID[:cut]}", _UUID[cut:] + " done \n")
        stored = "".join(r.text for r in results)
        assert not all(r.ok for r in results), (lead, cut)
        assert _UUID[:14] not in stored, (lead, cut)  # at most the first 13 characters
        assert state.tainted


def test_a_uuid_without_a_key_word_beside_it_still_flows():
    results, state = _joined(f"thread {_UUID} is open")
    assert [r.ok for r in results] == [True] and not state.tainted
    results, _ = _joined("see token 123e4567", " and more")
    assert all(r.ok for r in results)
