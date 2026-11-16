"""One id per worker, however the files spell it.

``src/llms/native_cli/identity.py`` is the answer to "which worker is
this?". The table it holds is spelled out again here, because a table that
is only tested against itself proves nothing: these tests pin the table to
the one the task states, pin the table to the registry (a provider the
registry adds and the table does not know would split one worker in two),
and pin the merge of two old records to the rule the task states -- the
counters add up, the quota marker of the newer generation wins.

The scenario the task names is the reason the module exists: ``step``
succeeds five times, the quota runs out, and then ``stepfun`` succeeds
once. The worker is working again, but the status line folded the two
spellings with ``max()`` and still showed it as exhausted.
"""

from __future__ import annotations

import copy

import pytest

from src.llms.native_cli.identity import (
    WORKER_ALIASES as WORKER_ALIASES_OF_THE_MODULE,
)
from src.llms.native_cli.identity import (
    canonical_worker_id,
    known_worker_ids,
    merge_worker_records,
    worker_ids_for,
)
from src.llms.native_cli.registry import list_adapters

# The canonical table, spelled out as the task states it. Every spelling on
# the left answers with the id on the right.
ALIAS_TABLE = {
    "arkcli": "doubao",
    "doubao": "doubao",
    "claude": "claude",
    "claude-code": "claude",
    "claudedev": "claude",
    "codex": "codex",
    "codexrev": "codex",
    "openai": "codex",
    "openai-codex": "codex",
    "cursor": "cursor",
    "deepseek": "deepseek",
    "deepseek-dsh": "deepseek",
    "dsh": "deepseek",
    "glm": "glm",
    "zai": "glm",
    "zcode": "glm",
    "zhipu": "glm",
    "grok": "grok",
    "kimi": "kimi",
    "moonshot": "kimi",
    "manus": "manus",
    "mimo": "mimo",
    "opencode": "opencode",
    "minimax": "minimax",
    "minimax-mmx": "minimax",
    "mmx": "minimax",
    "step": "stepfun",
    "stepfun": "stepfun",
}

# The canonical ids the table above yields.
CANONICAL_IDS = frozenset(ALIAS_TABLE.values())


def _exhausted(record: dict) -> bool:
    """Whether the status line would show this worker as out of quota.

    This is the rule ``src/conductor_cli/workers.py`` applies: the worker is
    blocked while the success counter has not passed the generation the quota
    failure was recorded at. It is written out here so the test states the
    rule it checks, instead of importing the very code under repair.
    """
    failed_at = record.get("quota_failed_at_seq")
    if failed_at is None:
        return False
    return int(record.get("success_seq") or 0) <= int(failed_at)


def _legacy_records(events: list[tuple[str, str]]) -> dict[str, dict]:
    """The per-spelling records the old state format leaves behind.

    ``events`` is a list of ``(spelling, kind)`` pairs, where the kind is
    ``"success"`` (a turn that got through) or ``"quota"`` (the quota ran
    out). The old format kept one counter per literal spelling, and a quota
    failure recorded the generation it happened in -- the success count the
    worker had reached.
    """
    records: dict[str, dict] = {}
    for spelling, kind in events:
        record = records.setdefault(
            spelling, {"success_seq": 0, "quota_failed_at_seq": None}
        )
        if kind == "success":
            record["success_seq"] += 1
        else:
            record["quota_failed_at_seq"] = record["success_seq"]
    return records


def _max_folded(records: dict[str, dict]) -> dict:
    """What the status line's old fold made of the same records.

    The line took the **largest** success counter and the **largest** quota
    marker of the aliases, which drops the successes of one spelling and can
    report a worker that is working again as out of quota. It is kept here
    only to show what the merge changes.
    """
    success = 0
    failed_at: int | None = None
    for record in records.values():
        success = max(success, int(record.get("success_seq") or 0))
        marker = record.get("quota_failed_at_seq")
        if marker is not None:
            failed_at = int(marker) if failed_at is None else max(failed_at, int(marker))
    return {"success_seq": success, "quota_failed_at_seq": failed_at}


# ----------------------------------------------------------------------
# canonical_worker_id


@pytest.mark.parametrize(
    ("spelling", "canonical"),
    sorted(ALIAS_TABLE.items()),
    ids=[f"{spelling}->{canonical}" for spelling, canonical in sorted(ALIAS_TABLE.items())],
)
def test_a_known_spelling_answers_with_its_canonical_id(spelling, canonical) -> None:
    assert canonical_worker_id(spelling) == canonical


def test_the_table_is_the_one_the_task_states() -> None:
    """The module's table is the table the task spells out."""
    assert dict(WORKER_ALIASES_OF_THE_MODULE) == ALIAS_TABLE
    assert known_worker_ids() == CANONICAL_IDS


def test_every_canonical_id_is_a_spelling_the_table_knows() -> None:
    """A canonical id is always a name the table knows.

    Otherwise a canonical id could not be resolved again, and a caller that
    canonicalised twice would get a different answer than one that did it
    once.
    """
    for canonical in CANONICAL_IDS:
        assert canonical_worker_id(canonical) == canonical


@pytest.mark.parametrize(
    ("spelling", "canonical"),
    [
        ("STEP", "stepfun"),
        ("  StepFun  ", "stepfun"),
        ("ArkCLI", "doubao"),
        ("\tZCODE\n", "glm"),
        ("OpenAI-Codex", "codex"),
    ],
)
def test_the_spelling_does_not_decide_the_worker(spelling, canonical) -> None:
    """The name is not careful about case or surrounding whitespace."""
    assert canonical_worker_id(spelling) == canonical


@pytest.mark.parametrize("name", [None, "", "   ", "\t\n"])
def test_no_name_names_no_worker(name) -> None:
    assert canonical_worker_id(name) == ""


@pytest.mark.parametrize(
    ("name", "canonical"),
    [
        ("weird-worker", "weird-worker"),
        ("SomeOther CLI", "someother cli"),
        ("not-a-known-one", "not-a-known-one"),
    ],
)
def test_an_unknown_name_is_its_own_id(name, canonical) -> None:
    """An unknown name is not refused: it names a worker of its own.

    This is what the status line already did -- it showed an unknown name
    lowercased rather than dropping the row.
    """
    assert canonical_worker_id(name) == canonical


# ----------------------------------------------------------------------
# the registry


def test_every_registry_provider_has_a_known_canonical_id() -> None:
    """A provider the registry knows is a spelling the table knows.

    A spelling the table does not know would be an id of its own, so one
    worker would be counted under two ids and shown as two workers. The
    registry is read here, so a provider added to it fails this test until
    the table learns the name.
    """
    for entry in list_adapters():
        provider = entry["provider"]
        assert provider in ALIAS_TABLE, provider
        assert canonical_worker_id(provider) in CANONICAL_IDS, provider


def test_the_registry_names_do_not_split_a_worker() -> None:
    """Two registered names of one adapter are one canonical id.

    The registry registers ``step`` and ``stepfun`` (and ``arkcli`` and
    ``doubao``, and ``glm``, ``zcode`` and ``zhipu``) as one worker each.
    """
    by_class: dict[str, set[str]] = {}
    for entry in list_adapters():
        by_class.setdefault(entry["class"], set()).add(entry["provider"])
    for adapter_class, providers in by_class.items():
        ids = {canonical_worker_id(provider) for provider in providers}
        assert len(ids) == 1, (adapter_class, providers)


# ----------------------------------------------------------------------
# merge_worker_records


def test_the_counters_of_two_aliases_add_up() -> None:
    """Equal counters are doubled: the two records are one worker's."""
    merged = merge_worker_records(
        {
            "step": {"success_seq": 3, "quota_failed_at_seq": 3},
            "stepfun": {"success_seq": 3, "quota_failed_at_seq": 3},
        }
    )
    assert set(merged) == {"stepfun"}
    assert merged["stepfun"]["success_seq"] == 6
    assert merged["stepfun"]["quota_failed_at_seq"] == 3


def test_unequal_counters_are_summed() -> None:
    merged = merge_worker_records(
        {
            "step": {"success_seq": 5, "quota_failed_at_seq": 5},
            "stepfun": {"success_seq": 1, "quota_failed_at_seq": None},
        }
    )
    assert merged["stepfun"]["success_seq"] == 6
    assert merged["stepfun"]["quota_failed_at_seq"] == 5


def test_the_newest_quota_marker_wins() -> None:
    """The marker of the newer generation is the one that counts.

    The marker is the generation the quota failure was recorded at, so the
    larger marker is the newer one.
    """
    merged = merge_worker_records(
        {
            "step": {"success_seq": 2, "quota_failed_at_seq": 2},
            "stepfun": {"success_seq": 2, "quota_failed_at_seq": 7},
        }
    )
    assert merged["stepfun"]["quota_failed_at_seq"] == 7


def test_a_marker_survives_an_alias_without_one() -> None:
    """An alias without a failure does not clear the failure of its alias."""
    merged = merge_worker_records(
        {
            "step": {"success_seq": 4, "quota_failed_at_seq": 4},
            "stepfun": {"success_seq": 1, "quota_failed_at_seq": None},
        }
    )
    assert merged["stepfun"]["success_seq"] == 5
    assert merged["stepfun"]["quota_failed_at_seq"] == 4


def test_no_marker_anywhere_is_no_failure() -> None:
    merged = merge_worker_records(
        {
            "step": {"success_seq": 2, "quota_failed_at_seq": None},
            "stepfun": {"success_seq": 1, "quota_failed_at_seq": None},
        }
    )
    assert merged["stepfun"]["success_seq"] == 3
    assert merged["stepfun"]["quota_failed_at_seq"] is None


def test_a_record_without_a_marker_key_has_no_failure() -> None:
    merged = merge_worker_records({"step": {"success_seq": 2}})
    assert merged["stepfun"]["success_seq"] == 2
    assert merged["stepfun"]["quota_failed_at_seq"] is None


def test_an_unknown_spelling_is_a_worker_of_its_own() -> None:
    """An unknown name is not merged with anything -- not even with itself."""
    merged = merge_worker_records(
        {
            "weird-worker": {"success_seq": 2, "quota_failed_at_seq": 2},
            "step": {"success_seq": 5, "quota_failed_at_seq": 5},
        }
    )
    assert set(merged) == {"weird-worker", "stepfun"}
    assert merged["weird-worker"]["success_seq"] == 2
    assert merged["stepfun"]["success_seq"] == 5


def test_a_spelling_with_no_record_contributes_nothing() -> None:
    merged = merge_worker_records(
        {
            "step": {"success_seq": 5, "quota_failed_at_seq": 5},
            "stepfun": {},
        }
    )
    assert merged["stepfun"]["success_seq"] == 5
    assert merged["stepfun"]["quota_failed_at_seq"] == 5


def test_the_extra_keys_of_a_record_are_ignored() -> None:
    merged = merge_worker_records(
        {
            "step": {
                "success_seq": 1,
                "quota_failed_at_seq": None,
                "rotations": 3,
                "provider": "step",
            }
        }
    )
    assert set(merged["stepfun"]) == {"success_seq", "quota_failed_at_seq"}


def test_the_records_are_not_mutated() -> None:
    """The merge reads the caller's records; it does not change them."""
    records = {
        "step": {"success_seq": 5, "quota_failed_at_seq": 5},
        "stepfun": {"success_seq": 1, "quota_failed_at_seq": None},
    }
    before = copy.deepcopy(records)
    merge_worker_records(records)
    assert records == before


def test_the_merge_is_the_same_whatever_the_order_of_the_records() -> None:
    """The merge does not depend on the order of the input mapping."""
    first = {
        "step": {"success_seq": 5, "quota_failed_at_seq": 5},
        "stepfun": {"success_seq": 1, "quota_failed_at_seq": None},
    }
    second = {
        "stepfun": {"success_seq": 1, "quota_failed_at_seq": None},
        "step": {"success_seq": 5, "quota_failed_at_seq": 5},
    }
    assert merge_worker_records(first) == merge_worker_records(second)


# ----------------------------------------------------------------------
# the scenario the task names


def test_a_worker_that_succeeded_under_its_other_name_is_not_exhausted() -> None:
    """The case the audit found: five turns, a quota failure, one more turn.

    The worker is working again -- the turn that got through under the other
    spelling says so -- so the status line must not show it as exhausted.
    """
    records = _legacy_records(
        [("step", "success")] * 5 + [("step", "quota")] + [("stepfun", "success")]
    )
    merged = merge_worker_records(records)["stepfun"]
    assert merged["success_seq"] == 6
    assert merged["quota_failed_at_seq"] == 5
    assert not _exhausted(merged)


def test_the_old_fold_is_what_got_this_wrong() -> None:
    """The fold the status line used is what said "exhausted" here.

    It took the largest success counter of the two spellings (5, not 6), so
    the worker still looked out of quota. This test says what the repair
    changes; it is not a test of the status line.
    """
    records = _legacy_records(
        [("step", "success")] * 5 + [("step", "quota")] + [("stepfun", "success")]
    )
    assert _exhausted(_max_folded(records))
    assert not _exhausted(merge_worker_records(records)["stepfun"])


@pytest.mark.parametrize("swap", [False, True], ids=["step-first", "stepfun-first"])
def test_the_two_aliases_may_appear_in_either_order(swap) -> None:
    """Whichever spelling the turn came in under, the worker is one worker."""
    events = [("step", "success")] * 5 + [("step", "quota")] + [("stepfun", "success")]
    if swap:
        events = [
            (("stepfun" if spelling == "step" else "step"), kind)
            for spelling, kind in events
        ]
    records = _legacy_records(events)
    merged = merge_worker_records(records)
    assert set(merged) == {"stepfun"}
    assert not _exhausted(merged["stepfun"])


def test_the_counters_of_the_two_spellings_are_not_unequal_after_the_merge() -> None:
    """The merge counts the worker once, whichever spelling carried a turn."""
    records = _legacy_records(
        [("step", "success")] * 5 + [("step", "quota")] + [("stepfun", "success")]
    )
    merged = merge_worker_records(records)["stepfun"]
    assert merged["success_seq"] == 6
    # The old fold kept only the larger of the two counters.
    assert merged["success_seq"] > _max_folded(records)["success_seq"]


# ----------------------------------------------------------------------
# worker_ids_for


def test_the_spellings_of_step_are_both_known() -> None:
    """A write path finds an old record of the worker under either name."""
    assert worker_ids_for("step") == ("step", "stepfun")
    assert worker_ids_for("stepfun") == ("step", "stepfun")
    assert worker_ids_for("STEP") == ("step", "stepfun")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("arkcli", ("arkcli", "doubao")),
        ("doubao", ("arkcli", "doubao")),
        ("glm", ("glm", "zai", "zcode", "zhipu")),
        ("zcode", ("glm", "zai", "zcode", "zhipu")),
        ("openai", ("codex", "codexrev", "openai", "openai-codex")),
    ],
)
def test_the_spellings_of_a_worker_are_the_ones_of_its_canonical_id(name, expected) -> None:
    assert worker_ids_for(name) == expected


def test_an_unknown_name_is_a_worker_of_its_own() -> None:
    assert worker_ids_for("weird-worker") == ("weird-worker",)


def test_no_name_names_no_worker_for_the_spellings() -> None:
    assert worker_ids_for(None) == ("",)
    assert worker_ids_for("   ") == ("",)


def test_every_spelling_of_the_table_is_reachable_from_its_canonical_id() -> None:
    """The canonical id of a worker names every spelling of that worker."""
    for spelling, canonical in ALIAS_TABLE.items():
        spellings = worker_ids_for(canonical)
        assert spelling in spellings
        assert canonical_worker_id(spelling) == canonical
