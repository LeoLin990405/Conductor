"""The eval repo contains only the base ancestry, and prompts stay blinded."""

from __future__ import annotations

import json

import pytest

from src.tools.auto_dispatch.harness_eval.sampling import select_samples
from src.tools.auto_dispatch.harness_eval.spec import SpecError, load_experiment
from src.tools.auto_dispatch.harness_eval.variants import (
    LeakError,
    assert_descendants_only,
    assert_no_leak,
    assert_prompt_closed,
    blinded_identity,
    blinded_line,
    closure_of,
    git,
    make_harness_commit,
    prepare_eval_repo,
    resolve_commit,
    worker_prompt,
)

from harness_eval_fakes import (
    DISPATCH_ID,
    VARIANT_ID,
    build_world,
    commit_file,
    dispatch_document,
    experiment_document,
    git as local_git,
    init_repo,
    source_fingerprint,
    write_tree,
)


def test_answer_commit_is_absent_and_there_is_no_remote(tmp_path) -> None:
    world = build_world(tmp_path)
    dest = prepare_eval_repo(world.output, world.repo, world.base)
    assert resolve_commit(dest, world.answer) is None
    listed = set(git(dest, ["rev-list", "--all"]).stdout.split())
    assert world.base in listed
    assert world.answer not in listed
    assert git(dest, ["remote"], check=False).stdout.strip() == ""
    assert git(dest, ["rev-parse", "HEAD"]).stdout.strip() == world.base


@pytest.mark.asyncio
async def test_harness_commit_is_deterministic_and_an_extra_ref_fails(tmp_path) -> None:
    world = build_world(tmp_path)
    experiment = load_experiment(world.experiment)
    samples, _ = await select_samples(experiment)
    sample = samples[0]
    dest = prepare_eval_repo(world.output, world.repo, world.base)
    other = prepare_eval_repo(tmp_path / "out-b", world.repo, world.base)
    variant = experiment.variant("baseline")
    first = make_harness_commit(
        dest, base_sha=world.base, sample=sample, variant=variant, experiment=experiment
    )
    second = make_harness_commit(
        dest, base_sha=world.base, sample=sample, variant=variant, experiment=experiment
    )
    mirrored = make_harness_commit(
        other, base_sha=world.base, sample=sample, variant=variant, experiment=experiment
    )
    assert first.commit == second.commit == mirrored.commit
    assert first.skills_unknown is True
    assert first.skills_hub == ""
    assert git(dest, ["log", "-1", "--format=%an", first.commit]).stdout.strip() == (
        "conductor-harness-eval"
    )
    message = git(dest, ["log", "-1", "--format=%B", first.commit]).stdout.strip()
    assert VARIANT_ID not in message
    assert DISPATCH_ID not in message
    assert len(message) == 64
    refs = {first.ref}
    assert_no_leak(dest, refs)
    git(dest, ["update-ref", "refs/heads/extra", world.base])
    with pytest.raises(LeakError, match="leak_check_failed"):
        assert_no_leak(dest, refs)


def test_variant_file_with_reviewer_verdict_is_rejected(tmp_path) -> None:
    world = build_world(tmp_path)
    agents = world.experiment.parent / "variants" / "short" / "AGENTS.md"
    agents.write_text("prefix reviewer_verdict suffix\n", encoding="utf-8")
    with pytest.raises(SpecError) as caught:
        load_experiment(world.experiment)
    assert "variants[1].agents_md.file" == caught.value.field


@pytest.mark.asyncio
async def test_worker_prompt_hides_the_original_result(tmp_path) -> None:
    world = build_world(tmp_path)
    experiment = load_experiment(world.experiment)
    samples, _ = await select_samples(experiment)
    sample = samples[0]
    dispatch_id, line_id, branch = blinded_identity("abcd1234", sample.index)
    line = blinded_line(sample, "abcd1234", "a" * 40)
    prompt = worker_prompt(line, dispatch_id)
    assert_prompt_closed(
        prompt,
        sample=sample,
        variant_id=VARIANT_ID,
        model="stepfun",
        experiment_id=experiment.id,
    )
    assert dispatch_id == "eval-abcd1234"
    assert line_id == "s0"
    assert branch == "feature/auto-dispatch-eval-abcd1234/line-s0"
    assert sample.dispatch_id not in prompt
    assert VARIANT_ID not in prompt
    assert "stepfun" not in prompt
    assert experiment.id not in prompt
    for forbidden in sample.forbidden:
        assert forbidden not in prompt
    with pytest.raises(LeakError):
        assert_prompt_closed(
            prompt + sample.dispatch_id,
            sample=sample,
            variant_id=VARIANT_ID,
            model="stepfun",
            experiment_id=experiment.id,
        )


@pytest.mark.asyncio
async def test_descendant_check_allows_only_worker_commits(tmp_path) -> None:
    world = build_world(tmp_path)
    experiment = load_experiment(world.experiment)
    samples, _ = await select_samples(experiment)
    dest = prepare_eval_repo(world.output, world.repo, world.base)
    made = make_harness_commit(
        dest,
        base_sha=world.base,
        sample=samples[0],
        variant=experiment.variant("baseline"),
        experiment=experiment,
    )
    closure = closure_of(dest, {made.ref})
    worker_ref = "refs/heads/feature/auto-dispatch-eval-abcd1234/line-s0"
    git(dest, ["branch", worker_ref.removeprefix("refs/heads/"), made.commit])
    assert_descendants_only(
        dest, harness_refs={made.ref}, harness_shas={made.commit}, closure=closure
    )
    git(dest, ["update-ref", "refs/heads/sneaky", world.base])
    with pytest.raises(LeakError, match="leak_check_failed"):
        assert_descendants_only(
            dest, harness_refs={made.ref}, harness_shas={made.commit}, closure=closure
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["replace", "append"])
async def test_external_agents_symlink_is_not_followed(tmp_path, mode: str) -> None:
    sentinel = tmp_path / "live" / "sentinel.txt"
    sentinel.parent.mkdir()
    sentinel.write_text("LIVE-SENTINEL\n", encoding="utf-8")
    source = init_repo(tmp_path / "source")
    commit_file(source, "README", "base\n", "base")
    (source / "AGENTS.md").symlink_to(sentinel)
    local_git(source, "add", "--", "AGENTS.md")
    local_git(source, "commit", "-q", "-m", "agents symlink")
    base = local_git(source, "rev-parse", "HEAD")
    assert local_git(source, "ls-files", "-s", "--", "AGENTS.md").split()[0] == "120000"
    before = source_fingerprint(source)
    original = sentinel.read_bytes()
    output = tmp_path / "out"
    document = experiment_document(
        repo=source,
        output_dir=output,
        lines=[f"{DISPATCH_ID}:line-0"],
        variants=[
            {"id": "baseline", "agents_md": {"mode": "keep"}, "skills": {"mode": "keep"}},
            {
                "id": VARIANT_ID,
                "agents_md": {"mode": mode, "file": "variants/short/AGENTS.md"},
                "skills": {"mode": "keep"},
            },
        ],
        baseline_repeats=1,
        max_runs=2,
        max_reviews=2,
    )
    path = write_tree(tmp_path, document, [dispatch_document(base)])
    experiment = load_experiment(path)
    samples, _skipped = await select_samples(experiment)
    assert len(samples) == 1
    dest = prepare_eval_repo(output, source, base)
    with pytest.raises(LeakError, match="leak_check_failed"):
        make_harness_commit(
            dest,
            base_sha=base,
            sample=samples[0],
            variant=experiment.variant(VARIANT_ID),
            experiment=experiment,
        )
    assert sentinel.read_bytes() == original
    assert source_fingerprint(source) == before
    assert (source / "README").read_text(encoding="utf-8") == "base\n"
    assert (source / "AGENTS.md").is_symlink()
    assert (source / "AGENTS.md").resolve() == sentinel.resolve()
    assert (dest / "AGENTS.md").is_symlink()
    assert (dest / "AGENTS.md").resolve() == sentinel.resolve()


@pytest.mark.parametrize("kind", ["symlink", "gitfile", "dir-symlink"])
def test_a_planted_git_dir_pointing_at_the_source_is_refused(tmp_path, kind: str) -> None:
    world = build_world(tmp_path)
    before = source_fingerprint(world.repo)
    dest = world.output / "repos" / world.base[:12]
    if kind == "dir-symlink":
        dest.parent.mkdir(parents=True)
        dest.symlink_to(world.repo, target_is_directory=True)
    else:
        dest.mkdir(parents=True)
        if kind == "symlink":
            (dest / ".git").symlink_to(world.repo / ".git", target_is_directory=True)
        else:
            (dest / ".git").write_text(f"gitdir: {world.repo / '.git'}\n", encoding="utf-8")
    with pytest.raises(LeakError, match="leak_check_failed"):
        prepare_eval_repo(world.output, world.repo, world.base)
    assert source_fingerprint(world.repo) == before


@pytest.mark.asyncio
async def test_a_task_that_mentions_a_model_or_a_common_word_is_allowed(tmp_path) -> None:
    task = "Compare against the baseline and keep the short glm and stepfun notes."
    world = build_world(tmp_path)
    source = world.experiment.parent / "samples" / "dispatches.json"
    rows = json.loads(source.read_text(encoding="utf-8"))
    rows[0]["lines"][0]["task"] = task
    source.write_text(json.dumps(rows) + "\n", encoding="utf-8")
    path = world.experiment
    experiment = load_experiment(path)
    samples, _ = await select_samples(experiment)
    sample = samples[0]
    dispatch_id, _line_id, _branch = blinded_identity("abcd1234", sample.index)
    prompt = worker_prompt(blinded_line(sample, "abcd1234", "a" * 40), dispatch_id)
    assert task in prompt
    assert_prompt_closed(
        prompt, sample=sample, variant_id=VARIANT_ID, model="stepfun", experiment_id=experiment.id
    )
    assert_prompt_closed(
        prompt, sample=sample, variant_id="baseline", model="glm", experiment_id=experiment.id
    )
    # An id inside a longer token is not a match; a whole-token id is.
    assert_prompt_closed(
        prompt + f" {experiment.id}-extra",
        sample=sample,
        variant_id=VARIANT_ID,
        model="stepfun",
        experiment_id=experiment.id,
    )
    with pytest.raises(LeakError):
        assert_prompt_closed(
            prompt + f" see {experiment.id}.",
            sample=sample,
            variant_id=VARIANT_ID,
            model="stepfun",
            experiment_id=experiment.id,
        )
    with pytest.raises(LeakError):
        assert_prompt_closed(
            prompt.replace(task, "") + " stepfun",
            sample=sample,
            variant_id=VARIANT_ID,
            model="stepfun",
            experiment_id=experiment.id,
        )
