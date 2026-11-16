"""The CLI's copy of the worker facts, held in step with the original.

``conductor_cli.worker_registry`` keeps the names of a worker, the size of
its context window and the way two spellings of one worker are merged. The
originals live in ``src.llms`` -- a package the wheel that ships the CLI
does not contain -- so the CLI cannot ask for them at runtime. These tests
are what makes the copy safe: they import the originals, which is possible
here because a test runs in a checkout.
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from conductor_cli import worker_registry
from src.llms.native_cli import identity, registry

#: A few names that are not spellings of a known worker, so the answers
#: that are "this name is its own worker" are covered too.
UNKNOWN_NAMES = ("", "   ", "not-a-worker", "Kimi ", "\tstepfun\n")


def test_the_cli_knows_exactly_the_aliases_the_native_cli_knows() -> None:
    assert dict(worker_registry.WORKER_ALIASES) == dict(identity.WORKER_ALIASES)


@pytest.mark.parametrize("alias", sorted(identity.WORKER_ALIASES))
def test_every_alias_resolves_to_the_same_worker_in_both_tables(alias: str) -> None:
    assert worker_registry.canonical_worker_id(alias) == identity.canonical_worker_id(
        alias
    )


@pytest.mark.parametrize("name", UNKNOWN_NAMES)
def test_a_name_nobody_knows_answers_the_same_in_both_tables(name: str) -> None:
    assert worker_registry.canonical_worker_id(name) == identity.canonical_worker_id(
        name
    )


@pytest.mark.parametrize("value", [None, 42, object()])
def test_a_name_that_is_not_text_answers_the_same_in_both_tables(value: Any) -> None:
    assert worker_registry.canonical_worker_id(value) == identity.canonical_worker_id(
        value
    )


def _merge_inputs() -> list[dict[str, dict[str, Any]]]:
    """The shapes of worker records the state file can hold."""
    return [
        {},
        {"step": {"success_seq": 3}},
        {"step": {"success_seq": 3}, "stepfun": {"success_seq": 4}},
        {
            "step": {"success_seq": 3, "quota_failed_at_seq": 7},
            "stepfun": {"success_seq": 4, "quota_failed_at_seq": 2},
        },
        {"zai": {"success_seq": 1}, "glm": {"success_seq": 2, "quota_failed_at_seq": 5}},
        {"kimi": {"success_seq": 0, "quota_failed_at_seq": None}},
        {"not-a-worker": {"success_seq": 9}},
    ]


@pytest.mark.parametrize("records", _merge_inputs())
def test_two_spellings_merge_the_same_way_in_both_tables(
    records: dict[str, dict[str, Any]],
) -> None:
    assert worker_registry.merge_worker_records(records) == identity.merge_worker_records(
        records
    )


def _random_merge_inputs(count: int) -> list[dict[str, dict[str, Any]]]:
    """Random records, drawn the same way every time.

    The seven shapes above are the ones the state file really holds. These
    are the ones nobody thought of: the merge adds counters and takes the
    larger marker, and a rule that is wrong in some corner of that shows up
    here.
    """
    names = sorted(identity.WORKER_ALIASES) + ["not-a-worker", ""]
    generator = random.Random(20261107)
    inputs: list[dict[str, dict[str, Any]]] = []
    for _ in range(count):
        records: dict[str, dict[str, Any]] = {}
        for _ in range(generator.randint(0, 5)):
            name = generator.choice(names)
            record: dict[str, Any] = {"success_seq": generator.randint(0, 9)}
            if generator.random() < 0.4:
                record["quota_failed_at_seq"] = generator.randint(0, 9)
            records[name] = record
        inputs.append(records)
    return inputs


@pytest.mark.parametrize("records", _random_merge_inputs(60))
def test_random_records_merge_the_same_way_in_both_tables(
    records: dict[str, dict[str, Any]],
) -> None:
    assert worker_registry.merge_worker_records(records) == identity.merge_worker_records(
        records
    )


def _provider_names() -> list[str]:
    """Every name a caller may ask the window of.

    The registry's own adapter tables are read directly rather than through
    ``list_adapters()``: that one also loads the compat configuration from
    the real home directory, and a test has no business doing that.
    """
    names = set(identity.WORKER_ALIASES)
    names.update(registry._ADAPTERS)
    names.update(registry._POOL_NAMES)
    names.update(UNKNOWN_NAMES)
    return sorted(names)


@pytest.mark.parametrize("provider", _provider_names())
def test_every_provider_has_the_same_context_window_in_both_tables(
    provider: str,
) -> None:
    assert worker_registry.context_window_tokens(
        provider
    ) == registry.context_window_tokens(provider)


def test_a_provider_without_a_window_is_unknown_and_not_zero() -> None:
    # "cursor" is a known name with no configured window: the answer is
    # "unknown", never a zero that would read as "no context used".
    assert worker_registry.context_window_tokens("cursor") is None
    assert worker_registry.context_window_tokens("not-a-worker") is None
