"""Selection is deterministic and every rejection has a reason code."""

from __future__ import annotations

import json

import pytest

from src.tools.auto_dispatch.dev import InMemoryDispatchStore
from src.tools.auto_dispatch.harness_eval.report import write_plan
from src.tools.auto_dispatch.harness_eval.runner import plan_runs
from src.tools.auto_dispatch.harness_eval.sampling import select_samples
from src.tools.auto_dispatch.harness_eval.spec import load_experiment
from src.tools.auto_dispatch.types import Dispatch, Line

from harness_eval_fakes import (
    DISPATCH_ID,
    LINE_ID,
    VERDICT_TOKEN,
    build_world,
    dispatch_document,
    line_dict,
)


def _reasons(skips) -> dict[str, str]:
    return {item.key: item.reason for item in skips}


@pytest.mark.asyncio
async def test_explicit_and_seeded_are_stable(tmp_path) -> None:
    world = build_world(tmp_path)
    experiment = load_experiment(world.experiment)
    first, first_skips = await select_samples(experiment)
    second, second_skips = await select_samples(experiment)
    assert [item.key for item in first] == [item.key for item in second]
    assert first_skips == second_skips
    assert first[0].index == 0
    assert first[0].base_commit == world.base
    assert VERDICT_TOKEN in first[0].forbidden

    seeded_doc = json.loads(world.experiment.read_text(encoding="utf-8"))
    seeded_doc["samples"]["select"] = "seeded"
    seeded_doc["samples"].pop("lines")
    world.experiment.write_text(json.dumps(seeded_doc), encoding="utf-8")
    seeded = load_experiment(world.experiment)
    left, _ = await select_samples(seeded)
    right, _ = await select_samples(seeded)
    assert [item.key for item in left] == [item.key for item in right]


@pytest.mark.asyncio
async def test_skip_reasons_keep_the_specific_code(tmp_path) -> None:
    missing = "deadbeef" * 5
    rows = [
        dispatch_document(
            "",
            lines=[
                line_dict(line_id="empty", task="  ", base_commit="abc"),
                line_dict(line_id="nobase", base_commit=""),
                line_dict(line_id="missing", base_commit=missing),
                line_dict(line_id="nocmd", acceptance=("looks done",), base_commit="placeholder"),
                line_dict(
                    line_id="badcmd",
                    acceptance=('cmd: "unterminated',),
                    base_commit="placeholder",
                ),
                line_dict(
                    line_id="risk",
                    write_scope=("migrations/001.py",),
                    base_commit="placeholder",
                ),
                line_dict(line_id="ok", base_commit="placeholder"),
                line_dict(line_id="ok2", base_commit="placeholder"),
            ],
        )
    ]
    # The placeholder is replaced after the repo exists.
    world = build_world(tmp_path, dispatches=rows, lines=["unused:nope"], samples_max=1)
    text = world.experiment.parent.joinpath("samples/dispatches.json").read_text(encoding="utf-8")
    text = text.replace("placeholder", world.base)
    world.experiment.parent.joinpath("samples/dispatches.json").write_text(text, encoding="utf-8")
    listed = [
        f"{DISPATCH_ID}:empty",
        f"{DISPATCH_ID}:nobase",
        f"{DISPATCH_ID}:missing",
        f"{DISPATCH_ID}:nocmd",
        f"{DISPATCH_ID}:badcmd",
        f"{DISPATCH_ID}:risk",
        f"{DISPATCH_ID}:ok",
        f"{DISPATCH_ID}:ok",
        f"{DISPATCH_ID}:ok2",
        f"{DISPATCH_ID}:absent",
    ]
    document = json.loads(world.experiment.read_text(encoding="utf-8"))
    document["samples"]["lines"] = listed
    document["samples"]["max"] = 1
    world.experiment.write_text(json.dumps(document), encoding="utf-8")
    experiment = load_experiment(world.experiment)
    chosen, skips = await select_samples(experiment)
    reasons = _reasons(skips)
    assert reasons[f"{DISPATCH_ID}:empty"] == "empty_task"
    assert reasons[f"{DISPATCH_ID}:nobase"] == "no_base_commit"
    assert reasons[f"{DISPATCH_ID}:missing"] == "base_missing"
    assert reasons[f"{DISPATCH_ID}:nocmd"] == "no_cmd_acceptance"
    assert reasons[f"{DISPATCH_ID}:badcmd"] == "acceptance_unparseable"
    assert reasons[f"{DISPATCH_ID}:risk"] == "risk_bearing"
    assert reasons[f"{DISPATCH_ID}:ok"] == "duplicate"
    assert reasons[f"{DISPATCH_ID}:ok2"] == "over_max"
    assert reasons[f"{DISPATCH_ID}:absent"] == "not_found"
    assert "not_found" not in {item.reason for item in skips if item.key.endswith(":empty")}
    assert [item.line_id for item in chosen] == ["ok"]


@pytest.mark.asyncio
async def test_gates_base_commit_and_store(tmp_path) -> None:
    world = build_world(
        tmp_path,
        dispatches=[
            dispatch_document(
                "",
                lines=[line_dict(base_commit="")],
                gates_base=None,
            )
        ],
    )
    # Rewrite the export so the line has no commit and the dispatch gates do.
    payload = [
        dispatch_document("", lines=[line_dict(base_commit="")], gates_base=world.base)
    ]
    source = world.experiment.parent / "samples" / "dispatches.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    experiment = load_experiment(world.experiment)
    chosen, skips = await select_samples(experiment)
    assert skips == ()
    assert chosen[0].base_commit == world.base

    store = InMemoryDispatchStore()
    await store.create(
        Dispatch(
            id=DISPATCH_ID,
            goal="Ship the note.",
            user_id="u",
            created_at="1970-01-01T00:00:00+00:00",
            lines=[
                Line(
                    id=LINE_ID,
                    name=LINE_ID,
                    task="Add note.txt with one line of text.",
                    acceptance=("cmd: true",),
                    write_scope=("note.txt",),
                    base_commit=world.base,
                )
            ],
        )
    )
    via_store, store_skips = await select_samples(experiment, store=store)
    assert store_skips == ()
    assert via_store[0].key == chosen[0].key


@pytest.mark.asyncio
async def test_plan_omits_forbidden_outcome_text(tmp_path) -> None:
    world = build_world(tmp_path)
    experiment = load_experiment(world.experiment)
    samples, skips = await select_samples(experiment)
    write_plan(world.output, experiment, samples, skips, plan_runs(experiment, samples))
    text = (world.output / "plan.json").read_text(encoding="utf-8")
    payload = json.loads(text)
    assert VERDICT_TOKEN not in text
    assert "forbidden" not in payload
    assert all("forbidden" not in sample for sample in payload["samples"])
    assert samples[0].forbidden
