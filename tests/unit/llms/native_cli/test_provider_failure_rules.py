"""Each provider teaches the classifier its own wording, and the loader finds it.

Section 3 of the W8 PRD puts every provider's characteristic rules in that
provider's own adapter, and ``failure.py`` collects them. A rule set only
reaches a caller once the adapter has been imported, and the entry point runs
in a fresh interpreter that imports nothing but the classifier -- so the
lazy loader in ``failure.py`` is what makes a provider's own wording
reachable at all.

These tests therefore pin three things:

* every provider in the registry has rules of its own, and the generic rules
  are still there beside them;
* a fresh interpreter finds them, and the generic rules alone would not have
  produced the same answer;
* the classifier still keeps its promises -- the fixed priority decides the
  class, a provider rule only names the reason, and the diagnostic never
  travels with the answer.
"""

from __future__ import annotations

import importlib
import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from src.llms.native_cli import failure as failure_module
from src.llms.native_cli.failure import (
    FailureClass,
    classify_failure,
    failure_rules,
)
from src.llms.native_cli.registry import _ADAPTERS
from src.tools.auto_dispatch.pool import DEV_MODEL_POOL

REPO_ROOT = Path(__file__).resolve().parents[4]

# The token the PRD's acceptance asks for: it stands in for whatever the
# provider leaked into its error text.
SENTINEL = "sentinel-secret-token-abc123"

# A provider no adapter registers rules for. The generic rules are the same
# for every provider, so this name says what the generic rules alone would
# make of a diagnostic.
NO_RULES = "no-such-provider"

# One phrase per provider that only that provider's own rule knows, with the
# class and the reason code the provider's rule has to produce. Every phrase
# is one the provider's own CLI prints, and none of them is matched by the
# generic rules -- which is what makes the fresh-interpreter test prove
# something.
OWN_WORDING: list[tuple[str, str, FailureClass, str]] = [
    (
        "kimi",
        "input token count exceeds maximum number of tokens",
        FailureClass.CONTEXT_EXHAUSTED,
        "kimi_context_overflow",
    ),
    (
        "codex",
        "usage limit reached",
        FailureClass.QUOTA_EXHAUSTED,
        "codex_usage_limit_reached",
    ),
    (
        "claude-code",
        "usage limit reached",
        FailureClass.QUOTA_EXHAUSTED,
        "claude_usage_limit_reached",
    ),
    (
        "cursor",
        "usage_limit_exceeded",
        FailureClass.QUOTA_EXHAUSTED,
        "cursor_usage_limit_exceeded",
    ),
    (
        "deepseek-dsh",
        "context_window_exceeded",
        FailureClass.CONTEXT_EXHAUSTED,
        "deepseek_context_window_exceeded",
    ),
    (
        "doubao",
        "insufficient_balance_error",
        FailureClass.QUOTA_EXHAUSTED,
        "arkcli_quota_exhausted",
    ),
    (
        "grok",
        "exceed_context_size_error",
        FailureClass.CONTEXT_EXHAUSTED,
        "grok_context_size_exceeded",
    ),
    (
        "mimo",
        "context_window_exceeded",
        FailureClass.CONTEXT_EXHAUSTED,
        "mimo_context_overflow",
    ),
    (
        "minimax",
        "input is too long for requested model",
        FailureClass.CONTEXT_EXHAUSTED,
        "minimax_context_window_exceeded",
    ),
    (
        "minimax-mmx",
        "api key rejected",
        FailureClass.AUTH_FAILED,
        "mmx_api_key_rejected",
    ),
    (
        "stepfun",
        "account quota unavailable. Retry when online.",
        FailureClass.QUOTA_EXHAUSTED,
        "stepfun_quota_exhausted",
    ),
    (
        "zcode",
        "credit_balance_exhausted",
        FailureClass.QUOTA_EXHAUSTED,
        "zcode_credit_exhausted",
    ),
    (
        "zhipu",
        "[1302][您的账户已达到速率限制，请您控制请求频率]",
        FailureClass.RATE_LIMITED,
        "zhipu_account_rate_limited",
    ),
]

# The spellings the dispatch roster and the registry use for one provider.
# The classifier is called with the roster's spelling, so a rule registered
# under the canonical name has to be found under every alias.
ALIASES: list[tuple[str, str, str, str]] = [
    ("claude", "claude-code", "usage limit reached", "claude_usage_limit_reached"),
    (
        "deepseek",
        "deepseek-dsh",
        "context_window_exceeded",
        "deepseek_context_window_exceeded",
    ),
    ("arkcli", "doubao", "insufficient_balance_error", "arkcli_quota_exhausted"),
    ("mmx", "minimax-mmx", "api key rejected", "mmx_api_key_rejected"),
    (
        "step",
        "stepfun",
        "account quota unavailable. Retry when online.",
        "stepfun_quota_exhausted",
    ),
    (
        "glm",
        "zcode",
        "credit_balance_exhausted",
        "zcode_credit_exhausted",
    ),
]

CANONICAL_PROVIDERS = [row[0] for row in OWN_WORDING]

# Cursor's RESOURCE_EXHAUSTED names two failures, and the word beside it
# tells them apart: on its own the provider is throttling the Worker, and
# with a word about money the account has nothing left to spend. The two
# need opposite recoveries -- a back-off answers the first, a refill only
# the second -- so the class depends on the companion word.
CURSOR_RESOURCE_EXHAUSTED: list[tuple[str, FailureClass, str]] = [
    ("RESOURCE_EXHAUSTED", FailureClass.RATE_LIMITED, "cursor_resource_exhausted"),
    (
        "RESOURCE_EXHAUSTED: quota reached",
        FailureClass.QUOTA_EXHAUSTED,
        "cursor_quota_exhausted",
    ),
    (
        "RESOURCE_EXHAUSTED: billing limit reached",
        FailureClass.QUOTA_EXHAUSTED,
        "cursor_quota_exhausted",
    ),
    (
        "RESOURCE_EXHAUSTED: credit balance exhausted",
        FailureClass.QUOTA_EXHAUSTED,
        "cursor_quota_exhausted",
    ),
    (
        "RESOURCE_EXHAUSTED: account balance depleted",
        FailureClass.QUOTA_EXHAUSTED,
        "cursor_quota_exhausted",
    ),
    (
        "RESOURCE_EXHAUSTED: insufficient allowance",
        FailureClass.QUOTA_EXHAUSTED,
        "cursor_quota_exhausted",
    ),
]

# Kimi's "the request is too large" is two failures as well: with a word
# about the context beside it, the context ran out and the session has to
# rotate, and without one the request was simply too big and the turn
# crashed. The companion word is what tells them apart.
KIMI_REQUEST_TOO_LARGE: list[tuple[str, FailureClass, str]] = [
    (
        "request too large: token budget exceeded",
        FailureClass.CONTEXT_EXHAUSTED,
        "kimi_request_too_large_context",
    ),
    (
        "request too large: context limit reached",
        FailureClass.CONTEXT_EXHAUSTED,
        "kimi_request_too_large_context",
    ),
    (
        "request too large: length of the body exceeded",
        FailureClass.CONTEXT_EXHAUSTED,
        "kimi_request_too_large_context",
    ),
    ("request too large", FailureClass.CRASH, "kimi_request_too_large"),
]


def _run_entry_point(*args: str) -> subprocess.CompletedProcess:
    """Run the module the way the exit-code stream does."""
    return subprocess.run(
        [sys.executable, "-m", "src.llms.native_cli.failure_cli", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.parametrize("provider", sorted(_ADAPTERS))
def test_every_provider_has_rules_of_its_own(provider: str) -> None:
    """Each adapter registers its own wording, so the generic rules are not all."""
    rules = failure_rules(provider)
    own = [rule for rule in rules if not rule.generic]
    assert own, f"{provider} has no provider-specific rule"
    # A provider rule names its own reason, so the answer says which
    # provider's wording was recognised. A confirmation refusal is the one
    # exception: N39 fixes its reason (worker_confirmation_required) for every
    # provider, so the dispatcher has a single code to branch on; a safety-gate
    # block (worker_tool_blocked) is fixed the same way.
    assert all(
        rule.reason_code != f"worker_{rule.failure_class.value}"
        or rule.failure_class
        in (FailureClass.CONFIRMATION_REQUIRED, FailureClass.TOOL_BLOCKED)
        for rule in own
    )


@pytest.mark.parametrize("provider", sorted(_ADAPTERS))
def test_the_generic_rules_are_still_there(provider: str) -> None:
    """A provider's own rules are added to the shared ones, never instead."""
    rules = failure_rules(provider)
    generic = {rule.reason_code for rule in failure_rules(NO_RULES) if rule.generic}
    assert generic <= {rule.reason_code for rule in rules}
    assert FailureClass.AUTH_FAILED in {rule.failure_class for rule in rules}
    assert FailureClass.NETWORK in {rule.failure_class for rule in rules}


@pytest.mark.parametrize(("provider", "phrase", "expected", "reason"), OWN_WORDING)
def test_a_fresh_interpreter_finds_the_providers_own_words(
    provider: str, phrase: str, expected: FailureClass, reason: str
) -> None:
    """The bug: a fresh interpreter imported no adapter, so no rule was registered."""
    result = _run_entry_point("--provider", provider, "--text", phrase)
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["failure_class"] == expected.value
    assert payload["reason_code"] == reason


@pytest.mark.parametrize(("provider", "phrase", "expected", "reason"), OWN_WORDING)
def test_the_generic_rules_alone_would_not_have_produced_it(
    provider: str, phrase: str, expected: FailureClass, reason: str
) -> None:
    """Without this the fresh-interpreter test would prove nothing."""
    generic = classify_failure(NO_RULES, phrase)
    assert generic.reason_code != reason
    assert generic.failure_class is not expected


def test_the_same_phrase_is_answered_by_the_provider_that_owns_it() -> None:
    """A provider's rules are its own: two providers may word it the same."""
    # Codex and Claude Code both say "usage limit reached", and each names it
    # in its own reason code -- the rule belongs to the provider, not to the
    # phrase.
    assert (
        classify_failure("codex", "usage limit reached").reason_code
        == "codex_usage_limit_reached"
    )
    assert (
        classify_failure("claude-code", "usage limit reached").reason_code
        == "claude_usage_limit_reached"
    )


@pytest.mark.parametrize("provider", sorted(_ADAPTERS))
def test_a_provider_still_classifies_a_diagnostic_that_matches_nothing(
    provider: str,
) -> None:
    """A provider's own rules never make an unrecognisable diagnostic known."""
    classification = classify_failure(provider, "the worker stopped")
    assert classification.failure_class is FailureClass.UNKNOWN
    assert classification.reason_code == "worker_unknown"


def test_a_provider_nobody_registered_still_classifies() -> None:
    """No entry in the map is not an error: the generic rules answer."""
    classification = classify_failure(NO_RULES, "the worker stopped")
    assert classification.failure_class is FailureClass.UNKNOWN
    assert classification.reason_code == "worker_unknown"


def test_the_loader_degrades_quietly_when_an_adapter_cannot_be_imported(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A missing adapter must not turn one Worker's failure into an exception."""
    monkeypatch.setattr(failure_module, "_LOADED_PROVIDERS", set())
    monkeypatch.setitem(
        failure_module._PROVIDER_MODULES,
        "broken-provider",
        "src.llms.native_cli.does_not_exist",
    )
    with caplog.at_level(logging.DEBUG):
        classification = classify_failure("broken-provider", "the worker stopped")
    assert classification.failure_class is FailureClass.UNKNOWN
    assert classification.reason_code == "worker_unknown"
    # The failure to import is a debug note, never an error the caller sees.
    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []


def test_the_loader_imports_only_the_module_that_owns_the_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One provider's rules are one module's business."""
    imported: list[str] = []
    real_import_module = importlib.import_module

    class _Spy:
        @staticmethod
        def import_module(name: str):
            imported.append(name)
            return real_import_module(name)

    monkeypatch.setattr(failure_module, "importlib", _Spy)
    monkeypatch.setattr(failure_module, "_LOADED_PROVIDERS", set())
    failure_module._load_provider_rules("kimi")
    assert imported == ["src.llms.native_cli.kimi"]


def test_collecting_the_rules_loads_the_providers_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rules are collected only after the adapter has been imported."""
    imported: list[str] = []
    real_import_module = importlib.import_module

    class _Spy:
        @staticmethod
        def import_module(name: str):
            imported.append(name)
            return real_import_module(name)

    monkeypatch.setattr(failure_module, "importlib", _Spy)
    monkeypatch.setattr(failure_module, "_LOADED_PROVIDERS", set())
    failure_module.failure_rules("kimi")
    assert imported == ["src.llms.native_cli.kimi"]


def test_the_loader_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Asking twice imports once: the second call does nothing."""
    imported: list[str] = []
    real_import_module = importlib.import_module

    class _Spy:
        @staticmethod
        def import_module(name: str):
            imported.append(name)
            return real_import_module(name)

    monkeypatch.setattr(failure_module, "importlib", _Spy)
    monkeypatch.setattr(failure_module, "_LOADED_PROVIDERS", set())
    failure_module._load_provider_rules("kimi")
    failure_module._load_provider_rules("kimi")
    assert imported == ["src.llms.native_cli.kimi"]


def test_a_provider_rule_changes_the_reason_code_but_not_the_class() -> None:
    """The PRD's fixed priority decides the class; a provider only names it."""
    # Kimi's own quota wording and the generic quota rule are the same class,
    # so the provider's rule wins the reason code and nothing else.
    classification = classify_failure("kimi", "insufficient_quota")
    assert classification.failure_class is FailureClass.QUOTA_EXHAUSTED
    assert classification.reason_code == "kimi_insufficient_quota"

    # Kimi's overload wording is a lower class than a rate limit, so the
    # fixed priority -- not the provider's rule -- decides.
    overloaded = classify_failure("kimi", "overload")
    assert overloaded.failure_class is FailureClass.OVERLOADED
    both = classify_failure("kimi", "overload; HTTP 429 Too Many Requests")
    assert both.failure_class is FailureClass.RATE_LIMITED
    assert both.reason_code == "worker_rate_limited"


@pytest.mark.parametrize(("alias", "canonical", "phrase", "reason"), ALIASES)
def test_an_alias_reaches_the_providers_own_rule(
    alias: str, canonical: str, phrase: str, reason: str
) -> None:
    """The exit-code stream passes the roster's spelling, not the registry's."""
    classification = classify_failure(alias, phrase)
    assert classification.reason_code == reason
    assert failure_rules(alias) == failure_rules(canonical)


@pytest.mark.parametrize("provider", DEV_MODEL_POOL)
def test_every_name_on_the_dispatch_roster_finds_its_rules(provider: str) -> None:
    """The roster is what reaches the classifier at runtime."""
    rules = failure_rules(provider)
    assert [rule for rule in rules if not rule.generic]


def test_the_diagnostic_appears_nowhere_in_the_result(caplog) -> None:
    """The sentinel test: a leak into the result is a credential leak."""
    diagnostic = f"input token count exceeds maximum number of tokens ({SENTINEL})"
    with caplog.at_level(logging.DEBUG):
        classification = classify_failure("kimi", diagnostic)
    assert SENTINEL not in repr(classification)
    assert SENTINEL not in str(classification)
    assert SENTINEL not in repr(vars(classification))
    assert SENTINEL not in caplog.text
    assert classification.failure_class is FailureClass.CONTEXT_EXHAUSTED
    assert classification.reason_code == "kimi_context_overflow"


def test_the_diagnostic_appears_nowhere_in_the_entry_point() -> None:
    """The same promise has to hold in the fresh interpreter."""
    result = _run_entry_point(
        "--provider",
        "kimi",
        "--text",
        f"input token count exceeds maximum number of tokens ({SENTINEL})",
    )
    assert result.returncode == 0
    assert SENTINEL not in result.stdout
    assert SENTINEL not in result.stderr
    payload = json.loads(result.stdout)
    assert payload["failure_class"] == "context_exhausted"
    assert payload["reason_code"] == "kimi_context_overflow"


@pytest.mark.parametrize(("phrase", "expected", "reason"), CURSOR_RESOURCE_EXHAUSTED)
def test_cursors_resource_exhausted_is_read_with_the_word_beside_it(
    phrase: str, expected: FailureClass, reason: str
) -> None:
    """One phrase, two failures: the companion word is what tells them apart."""
    classification = classify_failure("cursor", phrase)
    assert classification.failure_class is expected
    assert classification.reason_code == reason
    # The provider's own rule named it, not the shared one.
    assert classification.reason_code != "worker_quota_exhausted"
    assert classification.reason_code != "worker_rate_limited"
    # Without the provider's rule the generic ones would not have said this.
    generic = classify_failure(NO_RULES, phrase)
    assert generic.failure_class is not expected


@pytest.mark.parametrize(("phrase", "expected", "reason"), KIMI_REQUEST_TOO_LARGE)
def test_kimis_request_too_large_is_read_with_the_word_beside_it(
    phrase: str, expected: FailureClass, reason: str
) -> None:
    """The same phrase: a full context rotates the session, the rest crashes."""
    classification = classify_failure("kimi", phrase)
    assert classification.failure_class is expected
    assert classification.reason_code == reason
    # The provider's own rule named it, not the shared one.
    assert classification.reason_code != "worker_context_exhausted"
    assert classification.reason_code != "worker_crash"
    # Without the provider's rule the generic ones would not have said this.
    generic = classify_failure(NO_RULES, phrase)
    assert generic.failure_class is not expected


def test_the_companion_word_is_matched_whatever_its_case() -> None:
    """Cursor prints its own code in upper case; the wording still counts."""
    assert (
        classify_failure("cursor", "RESOURCE_EXHAUSTED").failure_class
        is FailureClass.RATE_LIMITED
    )
    assert (
        classify_failure("cursor", "resource_exhausted").failure_class
        is FailureClass.RATE_LIMITED
    )
    assert (
        classify_failure("cursor", "Resource_Exhausted: QUOTA reached").failure_class
        is FailureClass.QUOTA_EXHAUSTED
    )
    assert (
        classify_failure("kimi", "Request Too Large").failure_class is FailureClass.CRASH
    )
    assert (
        classify_failure("kimi", "request too large: Context limit reached").failure_class
        is FailureClass.CONTEXT_EXHAUSTED
    )


@pytest.mark.parametrize(
    ("provider", "phrase", "expected", "reason"),
    [
        (
            "cursor",
            "RESOURCE_EXHAUSTED: credit balance exhausted",
            "quota_exhausted",
            "cursor_quota_exhausted",
        ),
        (
            "kimi",
            "request too large: context limit reached",
            "context_exhausted",
            "kimi_request_too_large_context",
        ),
    ],
)
def test_a_fresh_interpreter_finds_the_companion_word_rules(
    provider: str, phrase: str, expected: str, reason: str
) -> None:
    """The rules live in the adapter, so a cold start has to find them too."""
    result = _run_entry_point("--provider", provider, "--text", phrase)
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["failure_class"] == expected
    assert payload["reason_code"] == reason


def test_the_diagnostic_appears_nowhere_in_a_companion_word_result(
    caplog,
) -> None:
    """The same promise, for a rule that reads a second word out of the text."""
    diagnostic = f"RESOURCE_EXHAUSTED: credit balance exhausted ({SENTINEL})"
    with caplog.at_level(logging.DEBUG):
        classification = classify_failure("cursor", diagnostic)
    assert SENTINEL not in repr(classification)
    assert SENTINEL not in str(classification)
    assert SENTINEL not in repr(vars(classification))
    assert SENTINEL not in caplog.text
    assert classification.failure_class is FailureClass.QUOTA_EXHAUSTED
    assert classification.reason_code == "cursor_quota_exhausted"
