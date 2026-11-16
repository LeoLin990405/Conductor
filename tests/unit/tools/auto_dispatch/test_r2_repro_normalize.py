"""R2: normalize before matching, exempt untagged security, ``out-of-scope:``, tighter repro.

Four fixes that precede flipping ``CONDUCTOR_DISPATCH_REVIEW_REPRO_MODE`` to
``enforce`` (wave/ops dbd76ca69):

1. Fold the blocker before the security/scope tag regexes run: NFKD, the
   invisible characters, the nonspacing marks on a Latin letter, then NFKC and
   the look-alike letters (``text_normalize.fold``).
2. An untagged finding that names a security flaw is exempt from the N51
   downgrade -- it must not be dropped as a format problem -- but it is not a
   conflict tag: N41 routing stays tag-only (R2 review item 1, the high one).
3. ``out-of-scope:`` is a scope tag; its negated forms (``not out-of-scope``,
   ``non-out-of-scope``) are not. The router and the exemption read one regex.
4. A repro is tighter: a fenced code block is not a command by itself (a
   powershell block used to pass), a capitalized word is not a command
   (``Git reset --hard``, ``Rm -rf`` used to pass), and a bare lower-case word
   is not a repro (``repro: `a``` used to pass).

Every bypass is exercised in the form a reviewer could actually write it:
full-width characters, a zero-width space inside a word, a combining mark, a
dotless i, and a Cyrillic or Greek look-alike. The modes stay at their default
``shadow`` here: enforce is opt-in per test.
"""

from __future__ import annotations

import pytest

from src.tools.auto_dispatch import text_normalize
from src.tools.auto_dispatch.machine import _conflict_of, conflict_tag
from src.tools.auto_dispatch.review_triage import (
    REPRO_MODE_ENV,
    blocker_missing,
    is_security_tagged,
    triage_verdict,
)
from src.tools.auto_dispatch.types import Verdict

_ENV = {REPRO_MODE_ENV: "enforce"}
_VAGUE = "the error handling looks fragile"

# The three ways a word can look right and match nothing: full-width forms,
# a zero-width space, and a look-alike letter from another script.
INJECTION = {
    "ascii": "injection",
    "fullwidth": "\uff49\uff4e\uff4a\uff45\uff43\uff54\uff49\uff4f\uff4e",  # ｉｎｊｅｃｔｉｏｎ
    "zero_width": "in\u200bjection",
    "cyrillic": "\u0456njection",  # іnjection, Cyrillic і (U+0456)
}


# --- 1. fold before matching: every bypass spelling reads as the ASCII word ---


@pytest.mark.parametrize("spelling", INJECTION.values(), ids=INJECTION.keys())
def test_fold_reads_every_bypass_spelling_as_the_ascii_word(spelling: str) -> None:
    assert text_normalize.fold(spelling) == "injection"
    assert text_normalize.mentions_security(spelling) is True


def test_fold_strips_the_invisible_characters_a_viewer_never_sees() -> None:
    for char in ("\u200b", "\u200c", "\u200d", "\ufeff", "\u2060", "\u00ad"):
        assert text_normalize.fold(f"se{char}cret") == "secret"


def test_fold_normalizes_a_fullwidth_tag_to_the_ascii_one() -> None:
    # ｓｅｃｕｒｉｔｙ: token logged
    assert (
        text_normalize.fold(
            "\uff53\uff45\uff43\uff55\uff52\uff49\uff54\uff59: token"
        )
        == "security: token"
    )


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [
        ("secur\u0131ty", "security"),  # dotless i (U+0131)
        ("\u017fcope", "scope"),  # long s (U+017F)
        ("\u0585ut-\u0585f-\u0455cope", "out-of-scope"),  # Armenian օ + Cyrillic ѕ
        ("i\u043fjection", "injection"),  # Cyrillic п for n
    ],
)
def test_fold_equates_the_letters_re_ignorecase_matched_but_kept(
    spelling: str, expected: str
) -> None:
    # R2 review item 2: re.I matched 'ı' as 'i', but the captured text kept the
    # dotless letter, so conflict_tag() returned "securıty" and the tag was
    # dropped. Folding the letter makes the captured text the ASCII tag.
    assert text_normalize.fold(spelling) == expected


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("secur\u0131ty: token logged", "security"),
        ("\u017fcope: edits src/billing.py", "scope"),
        ("i\u043fjection: x", ""),  # a look-alike word is not a tag
    ],
)
def test_a_dotless_or_long_s_tag_is_the_tag(tag: str, expected: str) -> None:
    assert text_normalize.conflict_tag(tag) == expected
    assert is_security_tagged(tag) is (expected == "security")


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [
        ("se\u0301curity", "security"),  # s + combining acute
        ("secur\u0301ity", "security"),  # i + combining acute
        ("s\u0335ecurity", "security"),  # s + combining short stroke overlay
        ("\u015fecurity", "security"),  # precomposed s-cedilla decomposes too
        ("secur\u0131\u0301ty", "security"),  # dotless i + accent
    ],
)
def test_fold_strips_a_combining_mark_on_a_latin_letter(
    spelling: str, expected: str
) -> None:
    # R2 review item 3: NFKC leaves "sécurity" composed, so neither the tag nor
    # the keyword matched. NFKD first, then drop the mark.
    assert text_normalize.fold(spelling) == expected


@pytest.mark.parametrize(
    "tag",
    [
        "se\u0301curity: token logged",
        "s\u0335ecurity: token logged",
        "\u015fecurity: token logged",
    ],
)
def test_a_combining_mark_does_not_hide_a_security_tag(tag: str) -> None:
    assert text_normalize.conflict_tag(tag) == "security"
    triage = triage_verdict(Verdict.blocked([tag]), environ=_ENV)
    assert triage.verdict.passed is False


def test_a_space_inside_a_chinese_phrase_is_folded_away() -> None:
    # R2 review item 6: "注 入" reads as 注入 but is a different string.
    finding = "SQL \u6ce8 \u5165 in the login form"
    assert text_normalize.mentions_security(finding) is True
    triage = triage_verdict(Verdict.blocked([finding]), environ=_ENV)
    assert triage.verdict.passed is False


@pytest.mark.parametrize(
    "tag",
    [
        "security: token logged",
        "\uff53\uff45\uff43\uff55\uff52\uff49\uff54\uff59: token logged",
        "secur\u200bity: token logged",
        "\u0455ecurity: token logged",  # Cyrillic ѕ (U+0455)
    ],
)
def test_a_security_tag_is_recognized_however_it_is_spelled(tag: str) -> None:
    assert text_normalize.conflict_tag(tag) == "security"
    assert is_security_tagged(tag) is True


def test_machine_conflict_tag_reads_the_folded_text() -> None:
    assert conflict_tag("scope: edits src/billing.py") == "scope"
    assert conflict_tag("\uff53\uff43\uff4f\uff50\uff45: edits src/billing.py") == "scope"
    assert conflict_tag("sco\u200bpe: edits src/billing.py") == "scope"
    assert conflict_tag("\u0455cope: edits src/billing.py") == "scope"
    assert conflict_tag("the fixture is module-scoped") == ""


def test_a_scope_tag_survives_a_look_alike_before_it() -> None:
    # Cyrillic с (U+0441) in "score" is folded to c, so this is not a tag.
    assert conflict_tag("\u0455core: 3 tests are failing") == ""
    assert conflict_tag("scope: edits src/billing.py") == "scope"


def test_enforce_never_downgrades_a_folded_security_tag() -> None:
    blocker = "\uff53\uff45\uff43\uff55\uff52\uff49\uff54\uff59: token logged in the log"
    triage = triage_verdict(Verdict.blocked([blocker]), environ=_ENV)
    assert triage.verdict.passed is False
    assert triage.verdict.blockers == [blocker]  # the blocker text is not rewritten
    assert "downgraded" not in triage.record


def test_enforce_never_downgrades_a_folded_scope_tag() -> None:
    blocker = "\u0455cope: edits src/billing.py outside the write scope"
    triage = triage_verdict(Verdict.blocked([blocker]), environ=_ENV)
    assert triage.verdict.passed is False
    assert triage.verdict.blockers == [blocker]


# --- 2. an untagged security finding is exempt by keyword --------------------


@pytest.mark.parametrize(
    "finding",
    [
        "SQL injection in the login form",
        "\uff33\uff51\uff4c \uff49\uff4e\uff4a\uff45\uff43\uff54\uff49\uff4f\uff4e",
        "sql in\u200bjection in the login form",
        "\u0391uth bypass on /admin",  # Greek Α (U+0391) reads as A
        "an auth bypass on /admin",
        "authentication bypass on /admin",
        "a secret is printed in the trace",
        "the credential cache is world-readable",
        "a session token is logged",
        "a path traversal in the upload handler",
        "an XSS in the comment box",
        "a CSRF token is not checked",
        "an RCE through the pickle loader",
        "an SSRF in the fetch proxy",
        "SQLi in the login query",
        "unsafe deserialization of the cached payload",
        "privilege escalation through the sudo shim",
    ],
)
def test_an_untagged_security_finding_fails_closed(finding: str) -> None:
    triage = triage_verdict(Verdict.blocked([finding]), environ=_ENV)
    assert triage.verdict.passed is False
    assert triage.verdict.blockers == [finding]
    assert "downgraded" not in triage.record


@pytest.mark.parametrize(
    "finding",
    [
        "SQL \u6ce8\u5165 in the login form",  # 注入
        "\u8ba4\u8bc1\u7ed5\u8fc7 on /admin",  # 认证绕过
        "\u9274\u6743\u7ed5\u8fc7 on /admin",  # 鉴权绕过
        "\u5bc6\u94a5\u6cc4\u6f0f in the trace",  # 密钥泄漏
        "\u6cc4\u9732\u5bc6\u94a5 in the trace",  # 泄露密钥
        "\u51ed\u8bc1\u88ab\u8bb0\u5f55",  # 凭证
        "\u8def\u5f84\u7a7f\u8d8a in the upload handler",  # 路径穿越
        "\u8de8\u7ad9\u811a\u672c in the comment box",  # 跨站脚本
        "\u8fdc\u7a0b\u4ee3\u7801\u6267\u884c through the loader",  # 远程代码执行
    ],
)
def test_an_untagged_security_finding_is_exempt_in_chinese_too(finding: str) -> None:
    assert text_normalize.mentions_security(finding) is True
    triage = triage_verdict(Verdict.blocked([finding]), environ=_ENV)
    assert triage.verdict.passed is False
    assert triage.verdict.blockers == [finding]


@pytest.mark.parametrize(
    "bypass",
    [
        "an \u0456njection in the login form",  # Cyrillic і
        "an in\u200bjection in the login form",  # zero-width space
        "\uff41\uff55\uff54\uff48 bypass on /admin",  # full-width auth
        "an i\u043fjection in the header",  # Cyrillic п for n
    ],
)
def test_an_untagged_security_finding_is_exempt_however_it_is_spelled(
    bypass: str,
) -> None:
    triage = triage_verdict(Verdict.blocked([bypass]), environ=_ENV)
    assert triage.verdict.passed is False


def test_a_plain_format_blocker_is_still_downgraded() -> None:
    triage = triage_verdict(Verdict.blocked([_VAGUE]), environ=_ENV)
    assert triage.verdict.passed is True
    assert triage.verdict.should_fix == [_VAGUE]


# --- 2b. routing stays tag-only: the keyword exemption parks nothing ----------

# R2 review item 1 (high): the keyword list used to classify the N41 conflict,
# so any blocker that mentioned "token", "secret" or "credentials" sent the
# line to a human as reviewer_conflict_security in every mode. The keyword's
# only job is the N51 exemption above; the router reads the tag.


@pytest.mark.parametrize(
    "blocker",
    [
        "src/login.py:12 - SQL injection in the form",
        "src/app.py:3 - the token budget is exceeded",
        "keep the secret sauce in README",
        "README mentions credentials setup",
    ],
)
def test_an_untagged_security_finding_is_not_a_conflict(blocker: str) -> None:
    assert conflict_tag(blocker) == ""
    assert _conflict_of(Verdict.blocked([blocker])) == ""


def test_a_tagged_security_finding_still_routes_as_a_conflict() -> None:
    verdict = Verdict.blocked(["src/login.py:12 - security: SQL injection in the form"])
    assert conflict_tag(verdict.blockers[0]) == "security"
    assert _conflict_of(verdict) == "security"


def test_a_scope_tag_routes_scope_even_when_the_text_names_a_token() -> None:
    # The keyword still exempts the blocker from the N51 downgrade (section 2);
    # it does not upgrade the conflict the reviewer tagged.
    verdict = Verdict.blocked(
        ["src/session.py:9 - scope: touches the session token handling"]
    )
    assert conflict_tag(verdict.blockers[0]) == "scope"
    assert _conflict_of(verdict) == "scope"


def test_a_scope_tag_without_a_security_flaw_stays_a_scope_conflict() -> None:
    verdict = Verdict.blocked(["a.py:3 - scope: edits src/billing.py"])
    assert _conflict_of(verdict) == "scope"


def test_an_untagged_scope_only_blocker_is_not_a_conflict() -> None:
    assert _conflict_of(Verdict.blocked([_VAGUE])) == ""


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
def test_routing_does_not_depend_on_the_repro_mode(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    # The fix is a revert, not a new mode: a keyword-only blocker routes in no
    # mode, and shadow stays the default.
    monkeypatch.setenv(REPRO_MODE_ENV, mode)
    verdict = Verdict.blocked(["src/app.py:3 - the token budget is exceeded"])
    assert _conflict_of(verdict) == ""


# --- 3. ``out-of-scope:`` is a scope tag; its negations are not --------------


@pytest.mark.parametrize(
    "blocker",
    [
        "out-of-scope: edits src/billing.py outside the write scope",
        "Out-Of-Scope: edits src/billing.py",
        "\uff4f\uff55\uff54\uff0d\uff4f\uff46\uff0d\uff53\uff43\uff4f\uff50\uff45: x",  # ｏｕｔ－ｏｆ－ｓｃｏｐｅ
        "src/billing.py:12 - out-of-scope: edits outside the write scope",
        "[out-of-scope] edits src/billing.py",
    ],
)
def test_out_of_scope_is_a_scope_tag(blocker: str) -> None:
    # R2 review item 4: router and exemption read one regex, so an
    # out-of-scope blocker is now adjudicated as a scope conflict too.
    from src.tools.auto_dispatch.review_triage import _SCOPE_TAG

    assert _SCOPE_TAG.search(text_normalize.fold(blocker)) is not None
    assert text_normalize.conflict_tag(blocker) == "scope"
    assert _conflict_of(Verdict.blocked([blocker])) == "scope"


@pytest.mark.parametrize(
    "blocker",
    [
        "not out-of-scope: edits src/billing.py",
        "non-out-of-scope: edits src/billing.py",
        "not-out-of-scope: edits src/billing.py",
        "NOT OUT-OF-SCOPE: edits src/billing.py",
        "not out of scope: edits src/billing.py",
        "non out of scope: edits src/billing.py",
    ],
)
def test_a_negated_out_of_scope_is_not_a_scope_tag(blocker: str) -> None:
    # R2 review item 4: "not out of scope:" used to be a scope tag for the
    # router and not for the exemption; both agree now.
    from src.tools.auto_dispatch.review_triage import _SCOPE_TAG

    assert _SCOPE_TAG.search(text_normalize.fold(blocker)) is None
    assert text_normalize.conflict_tag(blocker) == ""
    assert _conflict_of(Verdict.blocked([blocker])) == ""
    # …so the N51 exemption does not apply, and enforce may downgrade it.
    triage = triage_verdict(Verdict.blocked([blocker]), environ=_ENV)
    assert triage.record["downgraded"][0]["blocker"] == blocker


def test_a_plain_out_of_scope_blocker_is_never_downgraded() -> None:
    blocker = "out-of-scope: edits src/billing.py\n- scenario: the patch writes elsewhere"
    triage = triage_verdict(Verdict.blocked([blocker]), environ=_ENV)
    assert triage.verdict.passed is False
    assert triage.verdict.blockers == [blocker]


def test_a_tag_after_the_first_dash_is_prose_not_a_tag() -> None:
    assert conflict_tag("Bug in parser - scope: mentioned after the dash") == ""
    assert conflict_tag("src/a.py:3 - the bug - security: mentioned later") == ""


# --- 4. a tighter repro check: fences, capitalized words, a bare word ---------

_NO_REPRO = "src/app.py:42 - scenario: an empty list -> IndexError - repro:"


@pytest.mark.parametrize(
    "tail",
    [
        " ```powershell\nRemove-Item . -Recurse\n```",
        " ```ps1\nGet-ChildItem .\n```",
        " ```powershell\nWrite-Host hello\n```",
        " ```\nsome prose about the failure\n```",
        " ```bash\nno command here\n```",
    ],
)
def test_a_fenced_block_is_not_a_repro_by_itself(tail: str) -> None:
    # N71 low: the two backticks of a fence used to read as an inline span.
    assert blocker_missing(_NO_REPRO + tail) == ["repro"]
    triage = triage_verdict(Verdict.blocked([_NO_REPRO + tail]), environ=_ENV)
    assert triage.verdict.passed is True
    assert triage.record["downgraded"][0]["missing"] == ["repro"]


def test_a_command_inside_a_fence_is_still_a_repro() -> None:
    blocker = _NO_REPRO + " ```bash\npytest tests/test_app.py::test_empty -q\n```"
    assert blocker_missing(blocker) == []


@pytest.mark.parametrize(
    "tail",
    [
        " Git reset --hard",
        " `Git reset --hard`",
        " `Rm -rf build`",
        " Rm -rf build",
        " Pytest -q",
        " DOCKER compose down",
    ],
)
def test_a_capitalized_command_word_is_not_a_repro(tail: str) -> None:
    # N71 low: the command matchers were case-insensitive, so a capitalized
    # word (prose, or a quoted command) passed.
    assert blocker_missing(_NO_REPRO + tail) == ["repro"]


@pytest.mark.parametrize(
    "tail",
    [
        " `a`",
        " `ok`",
        " `see the log`",
        " `the failing case`",
    ],
)
def test_a_bare_lower_case_word_is_not_a_repro(tail: str) -> None:
    # R2 review item 7: any backticked span that started with a lower-case
    # letter counted, so "repro: `a`" and "repro: `see the log`" passed. A
    # repro is a known command or a path now.
    assert blocker_missing(_NO_REPRO + tail) == ["repro"]
    triage = triage_verdict(Verdict.blocked([_NO_REPRO + tail]), environ=_ENV)
    assert triage.verdict.passed is True
    assert triage.record["downgraded"][0]["missing"] == ["repro"]


@pytest.mark.parametrize(
    "tail",
    [
        " `pytest tests/test_app.py -q`",
        " `app.parse([])`",
        " `tests/test_app.py`",
        " `./run.sh`",
        " pytest tests/test_app.py::test_empty -q",
        " uv run python -c 'import app; app.parse([])'",
        " $ make test",
        " git status",
    ],
)
def test_a_lower_case_command_is_still_a_repro(tail: str) -> None:
    assert blocker_missing(_NO_REPRO + tail) == []


def test_a_lower_case_command_in_prose_is_still_a_repro() -> None:
    blocker = "src/app.py:42 fails when the list is empty; run `python -c 'import app'`"
    assert blocker_missing(blocker) == []


# --- the modes: default stays shadow and changes nothing ---------------------


@pytest.mark.parametrize("spelling", INJECTION.values(), ids=INJECTION.keys())
def test_the_default_mode_is_shadow_and_still_changes_nothing(spelling: str) -> None:
    security = f"{spelling} in the login form"
    verdict = Verdict.blocked([security, _VAGUE])
    triage = triage_verdict(verdict, environ={})
    assert triage.record["repro_mode"] == "shadow"
    assert triage.verdict is verdict
    assert triage.verdict.blockers == [security, _VAGUE]
    assert triage.record["downgraded_count"] == 1
    assert triage.record["downgraded"][0]["blocker"] == _VAGUE


@pytest.mark.parametrize(
    "blocker",
    [
        "SCOPE - edits outside the task",
        "out-of-scope - edits outside the task",
    ],
)
def test_the_scope_dash_shorthand_is_a_scope_tag(blocker: str) -> None:
    # The N51 exemption already accepted this shorthand; the router reads the
    # same regex now, so both agree it is a scope tag.
    assert text_normalize.conflict_tag(blocker) == "scope"
    triage = triage_verdict(Verdict.blocked([blocker]), environ=_ENV)
    assert triage.verdict.passed is False
