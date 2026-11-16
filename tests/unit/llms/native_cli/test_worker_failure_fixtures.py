"""Every redacted fixture classifies to the class its own name claims.

PRD section 8: "每个类别至少有 2 个 provider 的脱敏样例，并有对应的分类测试。"

The fixtures under ``tests/fixtures/worker_failures/`` are the evidence for the
provider-specific rules. This test walks them and asserts that the classifier
reaches the same verdict the file name states -- so a rule that stops matching
its own sample fails here rather than in production.

The provider is passed **as the fixture's directory name** (``claude``,
``deepseek``, ...), not as the registry's spelling. The roster uses the former
and the registry the latter, so this also pins that the two are reconciled.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.llms.native_cli.failure import FailureClass, classify_failure


FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "worker_failures"

#: The classes, and the exit code each fixture is classified with.
#:
#: ``crash`` is the process dying without saying why, so it needs a non-zero
#: exit code; every other class is decided by the text alone, and an
#: ``unknown`` fixture is deliberately unclassifiable text.
_EXIT_CODE_FOR = {
    FailureClass.CRASH: 1,
    FailureClass.UNKNOWN: None,
}


def _fixtures() -> list[tuple[str, FailureClass, Path]]:
    found: list[tuple[str, FailureClass, Path]] = []
    for path in sorted(FIXTURES.rglob("*.txt")):
        provider = path.parent.name
        stem = path.stem.rsplit("-", 1)[0]
        found.append((provider, FailureClass(stem), path))
    return found


_FIXTURES = _fixtures()


def test_the_fixture_tree_is_present():
    """There is evidence to classify, and every file names a known class."""
    assert _FIXTURES, f"no fixtures under {FIXTURES}"
    for provider, failure_class, path in _FIXTURES:
        assert provider, path
        assert isinstance(failure_class, FailureClass), path


#: Classes only one CLI is known to produce, with why. A class listed here
#: still needs its one provider's sample; it is not exempt from evidence.
_SINGLE_PROVIDER_CLASSES = {
    # Only Step's safety gate is known to end a headless run on a tool call
    # it will not run unattended; the other CLIs' blocks are tool results
    # their run goes on from (see the fixtures README, tool_blocked).
    FailureClass.TOOL_BLOCKED: "stepfun",
}


def test_every_class_has_at_least_two_providers():
    """PRD section 8: at least two providers' samples for every class."""
    providers: dict[FailureClass, set[str]] = {}
    for provider, failure_class, _ in _FIXTURES:
        providers.setdefault(failure_class, set()).add(provider)
    for failure_class, provider in _SINGLE_PROVIDER_CLASSES.items():
        assert provider in providers.get(failure_class, set()), failure_class
    missing = {
        failure_class.value: sorted(providers.get(failure_class, set()))
        for failure_class in FailureClass
        if failure_class not in _SINGLE_PROVIDER_CLASSES
        and len(providers.get(failure_class, set())) < 2
    }
    assert not missing, f"classes with fewer than two providers: {missing}"


@pytest.mark.parametrize(
    ("provider", "failure_class", "path"),
    _FIXTURES,
    ids=[f"{provider}-{path.stem}" for provider, _, path in _FIXTURES],
)
def test_a_fixture_classifies_to_the_class_it_is_named_after(
    provider: str, failure_class: FailureClass, path: Path
):
    """The sample reaches the class its name says."""
    text = path.read_text(encoding="utf-8")
    exit_code = _EXIT_CODE_FOR.get(failure_class)
    result = classify_failure(provider, text, exit_code=exit_code)
    assert result.failure_class is failure_class, (
        f"{path} was classified {result.failure_class.value}, "
        f"expected {failure_class.value}"
    )


@pytest.mark.parametrize(
    ("provider", "failure_class", "path"),
    _FIXTURES,
    ids=[f"{provider}-{path.stem}" for provider, _, path in _FIXTURES],
)
def test_a_fixture_never_reaches_the_result_as_text(
    provider: str, failure_class: FailureClass, path: Path
):
    """The sample's text does not travel with the verdict.

    The class is a fixed vocabulary word, so a fixture whose whole text *is*
    that word (``overloaded``) legitimately shares it with the result. What
    must never happen is the **reason code** carrying the diagnostic: it is
    the field that ends up in an event log, so it has to stay a stable token.
    A token cannot hold a sentence, so a multi-word line is what would prove
    a leak; a single word can only ever be a vocabulary item.
    """
    text = path.read_text(encoding="utf-8")
    exit_code = _EXIT_CODE_FOR.get(failure_class)
    result = classify_failure(provider, text, exit_code=exit_code)

    # The reason code is a stable token, never a sentence and never the text.
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}", result.reason_code), (
        f"{path} produced a reason code that is not a stable token"
    )
    for line in text.splitlines():
        stripped = line.strip()
        if " " in stripped:
            assert stripped not in result.reason_code, path


def test_the_fixtures_carry_no_credentials():
    """A fixture is evidence, so it holds no token, email or account id."""
    import re

    email = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    absolute_path = re.compile(r"(?:/Users/|/home/|/root/|/private/)")
    for provider, failure_class, path in _FIXTURES:
        text = path.read_text(encoding="utf-8")
        assert not email.search(text), path
        assert not absolute_path.search(text), path
        assert "leo" not in text.lower(), path
