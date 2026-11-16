"""Experiment files are accepted whole or rejected with the field name."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.tools.auto_dispatch.harness_eval.spec import (
    SpecError,
    ensure_within_budget,
    load_experiment,
    selected_inputs_fingerprint,
)
from src.tools.auto_dispatch.harness_eval.types import HARD_CAP_MAX_RUNS, Sample

from harness_eval_fakes import experiment_document, write_tree


def _valid(tmp_path: Path, **kwargs: Any) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    output = tmp_path / "out"
    document = experiment_document(repo=repo, output_dir=output, lines=["zzhist9k:line-0"], **kwargs)
    return write_tree(tmp_path, document, [])


def _mutate(path: Path, mutate) -> Path:
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _rejected(path: Path, field: str) -> SpecError:
    with pytest.raises(SpecError) as caught:
        load_experiment(path)
    assert caught.value.field == field
    assert field in str(caught.value)
    return caught.value


def test_valid_file_parses_and_marks_skills_unknown(tmp_path: Path) -> None:
    experiment = load_experiment(_valid(tmp_path))
    assert experiment.schema == "conductor.harness_eval/v1"
    assert experiment.skills_unknown is True
    assert experiment.canonical_sha256
    assert experiment.variant("baseline").agents_md.mode == "keep"
    assert experiment.variant("baseline").skills.names is None


def test_explicit_empty_skill_list_is_not_unknown(tmp_path: Path) -> None:
    path = _valid(
        tmp_path,
        variants=[
            {
                "id": "baseline",
                "agents_md": {"mode": "keep"},
                "skills": {"mode": "keep", "names": []},
            }
        ],
    )
    experiment = load_experiment(path)
    assert experiment.skills_unknown is False
    assert experiment.variant("baseline").skills.names == ()


def test_keep_variant_inherits_the_baseline_skills(tmp_path: Path) -> None:
    path = _valid(
        tmp_path,
        variants=[
            {
                "id": "baseline",
                "agents_md": {"mode": "keep"},
                "skills": {"mode": "keep", "names": ["alpha", "beta"]},
            },
            {
                "id": "short",
                "agents_md": {"mode": "replace", "file": "variants/short/AGENTS.md"},
                "skills": {"mode": "keep"},
            },
        ],
    )
    experiment = load_experiment(path)
    # Only AGENTS.md is under test: the keep variant must launch with the
    # same skills as the baseline, not with an empty list.
    assert experiment.variant("short").skills.names == ("alpha", "beta")
    assert experiment.skills_unknown is False


def test_keep_variant_with_a_different_list_is_rejected(tmp_path: Path) -> None:
    path = _valid(
        tmp_path,
        variants=[
            {
                "id": "baseline",
                "agents_md": {"mode": "keep"},
                "skills": {"mode": "keep", "names": ["alpha"]},
            },
            {
                "id": "short",
                "agents_md": {"mode": "replace", "file": "variants/short/AGENTS.md"},
                "skills": {"mode": "keep", "names": ["beta"]},
            },
        ],
    )
    _rejected(path, "variants[1].skills.names")


def test_variant_file_bytes_change_the_fingerprint(tmp_path: Path) -> None:
    path = _valid(tmp_path)
    first = load_experiment(path).canonical_sha256
    agents = path.parent / "variants" / "short" / "AGENTS.md"
    agents.write_text(agents.read_text(encoding="utf-8") + "Again.\n", encoding="utf-8")
    second = load_experiment(path).canonical_sha256
    assert first != second


@pytest.mark.parametrize(
    ("field", "mutate"),
    [
        ("budget.max_runs", lambda doc: doc["budget"].__setitem__("max_runs", HARD_CAP_MAX_RUNS + 1)),
        ("budget.max_models", lambda doc: doc["budget"].__setitem__("max_models", 3)),
        ("samples.max", lambda doc: doc["samples"].__setitem__("max", 7)),
        ("variants", lambda doc: doc.__setitem__("variants", _five_variants())),
        ("repeats", lambda doc: doc.__setitem__("repeats", 3)),
        ("baseline_repeats", lambda doc: doc.__setitem__("baseline_repeats", 4)),
        (
            "budget.worker_timeout_seconds",
            lambda doc: doc["budget"].__setitem__("worker_timeout_seconds", 3601),
        ),
        (
            "budget.max_wall_minutes",
            lambda doc: doc["budget"].__setitem__("max_wall_minutes", 481),
        ),
        ("models", lambda doc: _two_models_one_slot(doc)),
        (
            "reviewer.models",
            lambda doc: doc.__setitem__("models", ["codex"]) or doc["reviewer"].__setitem__("models", ["codex"]),
        ),
        ("reviewer.models", lambda doc: doc["reviewer"].__setitem__("models", ["kimi"])),
        ("reviewer.models", lambda doc: doc["reviewer"].__setitem__("models", ["glm"])),
        ("note", lambda doc: doc.__setitem__("note", "extra")),
        ("samples.source", lambda doc: doc["samples"].__setitem__("source", "../outside.json")),
        (
            "baseline",
            lambda doc: doc["variants"][0]["agents_md"].__setitem__("mode", "remove"),
        ),
        (
            "baseline",
            lambda doc: doc["variants"][0]["skills"].__setitem__("mode", "none"),
        ),
        ("budget.max_runs", lambda doc: doc["budget"].__setitem__("max_runs", True)),
    ],
)
def test_rejects_with_the_field_name(tmp_path: Path, field: str, mutate) -> None:
    path = _mutate(_valid(tmp_path), mutate)
    _rejected(path, field)


def test_planned_runs_over_max_runs_are_rejected(tmp_path: Path) -> None:
    path = _valid(tmp_path, repeats=1, baseline_repeats=3, max_runs=3)
    experiment = load_experiment(path)
    with pytest.raises(SpecError) as caught:
        ensure_within_budget(experiment, 1)
    assert caught.value.field == "budget.max_runs"
    assert "4" in str(caught.value)


def test_output_dir_inside_repo_is_rejected(tmp_path: Path) -> None:
    path = _valid(tmp_path)
    repo = tmp_path / "repo"
    nested = repo / "nested-out"
    _mutate(path, lambda doc: doc.__setitem__("output_dir", str(nested)))
    _rejected(path, "output_dir")


@pytest.mark.parametrize("name", ["repos", "worktrees", "g2-worktrees", "runs", "launch-logs"])
def test_repo_overlapping_a_generated_subtree_is_rejected(tmp_path: Path, name: str) -> None:
    path = _valid(tmp_path)
    root = tmp_path / "eval"
    repo = root / name
    repo.mkdir(parents=True)
    _retarget(path, repo, root)
    error = _rejected(path, "output_dir")
    assert name in str(error)


def test_output_overlapping_live_dispatch_dirs_is_rejected(tmp_path: Path, monkeypatch) -> None:
    runs = tmp_path / "state" / "runs"
    monkeypatch.setenv("CONDUCTOR_DISPATCH_RUNS_DIR", str(runs))
    nested = _valid(tmp_path / "nested")
    _retarget(nested, tmp_path / "nested" / "repo", runs / "child")
    _rejected(nested, "output_dir")

    sibling = _valid(tmp_path / "sibling")
    _retarget(sibling, tmp_path / "sibling" / "repo", runs.parent / "dispatch-worktrees")
    _rejected(sibling, "output_dir")

    parent = _valid(tmp_path / "parent")
    _retarget(parent, tmp_path / "parent" / "repo", runs.parent)
    _rejected(parent, "output_dir")


def test_default_live_runs_directory_is_rejected(tmp_path: Path) -> None:
    path = _valid(tmp_path)
    live = Path.home() / ".conductor" / "dispatch" / "runs" / "harness-eval-overlap"
    _retarget(path, tmp_path / "repo", live)
    _rejected(path, "output_dir")


def test_selected_inputs_fingerprint_covers_task_acceptance_and_base() -> None:
    original = _sample()
    digest = selected_inputs_fingerprint((original,))
    assert digest == selected_inputs_fingerprint((_sample(),))
    assert digest != selected_inputs_fingerprint((_sample(task="Rewrite the note."),))
    assert digest != selected_inputs_fingerprint((_sample(acceptance=("cmd: false",)),))
    assert digest != selected_inputs_fingerprint((_sample(base_commit="b" * 40),))


def test_variant_marker_and_oversize_file_are_rejected(tmp_path: Path) -> None:
    path = _valid(tmp_path)
    agents = path.parent / "variants" / "short" / "AGENTS.md"
    agents.write_text("see reviewer_verdict\n", encoding="utf-8")
    _rejected(path, "variants[1].agents_md.file")

    agents.write_bytes(b"\xff")
    _rejected(path, "variants[1].agents_md.file")

    agents.write_text("a" * (32 * 1024 + 1), encoding="utf-8")
    _rejected(path, "variants[1].agents_md.file")


def test_skill_name_with_a_marker_is_rejected(tmp_path: Path) -> None:
    path = _valid(
        tmp_path,
        variants=[
            {"id": "baseline", "agents_md": {"mode": "keep"}, "skills": {"mode": "keep"}},
            {
                "id": "short",
                "agents_md": {"mode": "keep"},
                "skills": {"mode": "set", "names": ["myg2"]},
            },
        ],
    )
    _rejected(path, "variants[1].skills.names")


def test_policy_roster_filters_reviewers(tmp_path: Path) -> None:
    class Policy:
        primary_reviewer = ("codex",)
        secondary_reviewer_pool = ("kimi", "claude")

    allowed = load_experiment(_valid(tmp_path), policy=Policy())
    assert allowed.reviewer.models == ("codex",)

    denied = _mutate(
        _valid(tmp_path / "other"),
        lambda doc: doc["reviewer"].__setitem__("models", ["doubao"]),
    )
    with pytest.raises(SpecError) as caught:
        load_experiment(denied, policy=Policy())
    assert caught.value.field == "reviewer.models"


def test_without_a_policy_only_the_n2_denylist_applies(tmp_path: Path) -> None:
    path = _mutate(_valid(tmp_path), lambda doc: doc["reviewer"].__setitem__("models", ["doubao"]))
    assert load_experiment(path).reviewer.models == ("doubao",)


def _retarget(path: Path, repo: Path, output: Path) -> Path:
    return _mutate(
        path,
        lambda doc, repo=str(repo), output=str(output): doc.update(
            {"repo": repo, "output_dir": output}
        ),
    )


def _sample(**overrides: Any) -> Sample:
    raw: dict[str, Any] = {
        "key": "d:line-0",
        "index": 0,
        "dispatch_id": "d",
        "line_id": "line-0",
        "goal": "Ship the note.",
        "task": "Add note.txt",
        "acceptance": ("cmd: true",),
        "write_scope": ("note.txt",),
        "base_commit": "a" * 40,
    }
    raw.update(overrides)
    return Sample(**raw)


def _five_variants() -> list[dict[str, Any]]:
    return [
        {"id": f"v{index}", "agents_md": {"mode": "keep"}, "skills": {"mode": "keep"}}
        for index in range(5)
    ]


def _two_models_one_slot(document: dict[str, Any]) -> None:
    document["models"] = ["stepfun", "glm"]
    document["budget"]["max_models"] = 1
    document["reviewer"]["models"] = ["codex"]
