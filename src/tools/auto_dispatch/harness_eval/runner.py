"""Plan a matrix, or replay it one run at a time.

Dry-run is the default: it checks the experiment, selects lines, and writes
``plan.json``. It does not create an eval repository, a worktree, or a review.
Live mode reserves budget before each worker and each review, and stops when
the ledger refuses. Reports are proposals; this module never writes the
outcome store.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import os
import shlex
import shutil
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from src.tools.auto_dispatch.execution import (
    CrossReviewer,
    ScriptLauncher,
    WorkerLaunchFailure,
    branch_evidence,
    verdict_unparseable,
)
from src.tools.auto_dispatch.gates import (
    G2_TIMEOUT,
    GateRunner,
    GateSettings,
    SubprocessCommandRunner,
    path_matches,
)
from src.tools.auto_dispatch.protocols import (
    DispatchError,
    LaunchError,
    NoNewCommitError,
    UncommittedChangesError,
)
from src.tools.auto_dispatch.types import Dispatch, Line

from . import safe_io
from .budget import Ledger
from .report import (
    append_record,
    format_dry_run,
    load_records,
    read_plan,
    write_plan,
    write_reports,
)
from .sampling import select_samples
from .scoring import build_report, score_run
from .spec import (
    SpecError,
    assert_output_contained,
    assert_purge_target,
    ensure_within_budget,
    load_experiment,
    selected_inputs_fingerprint,
)
from .types import (
    EXIT_BUDGET,
    EXIT_LEAK,
    EXIT_OK,
    EXIT_SETUP,
    EXIT_VALIDATION,
    CommandOutcome,
    Experiment,
    G2Status,
    HarnessCommit,
    PlannedRun,
    ReviewStatus,
    RunRecord,
    Sample,
    WorkerStatus,
)
from .variants import (
    LeakError,
    assert_no_leak,
    assert_prompt_closed,
    assert_run_closed,
    blinded_dispatch,
    blinded_identity,
    blinded_line,
    closure_of,
    git,
    make_harness_commit,
    prepare_eval_repo,
    prepare_run_repo,
)

ReviewFactory = Callable[[Path], Any]
Clock = Callable[[], float]


async def execute(
    path: Path,
    *,
    live: bool = False,
    confirm_runs: int | None = None,
    purge: bool = False,
    store: Any | None = None,
    policy: Any | None = None,
    runner: Any | None = None,
    review_backend_factory: ReviewFactory | None = None,
    command_runner: Any | None = None,
    clock: Clock | None = None,
    script: Path | None = None,
    serial_lock_dir: Path | None = None,
) -> int:
    """Run ``plan`` or ``live`` for one experiment file. Returns the process status.

    ``serial_lock_dir`` is the per-provider serial lock directory the live
    dispatch launcher uses. It must match the backend's launcher
    environment; without it the default is derived from this process's
    environment (``live_serial_lock_dir``).
    """
    try:
        experiment = load_experiment(path, policy=policy)
        samples, skips = await select_samples(experiment, store=store)
        run_count = ensure_within_budget(experiment, len(samples))
        runs = plan_runs(experiment, samples)
    except SpecError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_VALIDATION
    if len(runs) != run_count:
        print("planned run matrix does not match the budget formula", file=sys.stderr)
        return EXIT_VALIDATION
    try:
        _reject_sha_mismatch(experiment, samples)
    except SpecError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_VALIDATION
    if not live:
        # plan.json is shared with a live run on the same output_dir, so the
        # dry run takes the same lock before it rewrites the file.
        stack = contextlib.ExitStack()
        try:
            held = stack.enter_context(_exclusive_output(experiment.output_dir))
        except OSError as exc:
            print(f"output_dir: {exc}", file=sys.stderr)
            return EXIT_VALIDATION
        with stack:
            if not held:
                print(
                    "a live run holds this output_dir; wait for it or choose another",
                    file=sys.stderr,
                )
                return EXIT_VALIDATION
            write_plan(experiment.output_dir, experiment, samples, skips, runs)
        sys.stdout.write(format_dry_run(experiment, samples, skips, run_count))
        return EXIT_OK
    if confirm_runs is None:
        print("live mode requires --confirm-runs N", file=sys.stderr)
        return EXIT_VALIDATION
    if confirm_runs != run_count:
        print(
            f"confirm-runs must equal planned runs ({run_count})",
            file=sys.stderr,
        )
        return EXIT_VALIDATION
    if review_backend_factory is None:
        print("live mode requires a review backend", file=sys.stderr)
        return EXIT_VALIDATION
    stack = contextlib.ExitStack()
    try:
        held = stack.enter_context(_exclusive_output(experiment.output_dir))
    except OSError as exc:
        print(f"output_dir: {exc}", file=sys.stderr)
        return EXIT_VALIDATION
    with stack:
        if not held:
            print(
                "another live run holds this output_dir; wait for it or choose another",
                file=sys.stderr,
            )
            return EXIT_VALIDATION
        write_plan(experiment.output_dir, experiment, samples, skips, runs)
        _print_budget(experiment, run_count)
        code = await _live(
            experiment,
            samples,
            runs,
            runner=runner,
            review_backend_factory=review_backend_factory,
            command_runner=command_runner,
            clock=clock or time.time,
            script=script or _default_script(),
            serial_lock_dir=Path(serial_lock_dir) if serial_lock_dir else live_serial_lock_dir(),
        )
        if purge:
            try:
                _purge(experiment)
            except SpecError as exc:
                print(str(exc), file=sys.stderr)
                return EXIT_VALIDATION
        return code


@contextlib.contextmanager
def _exclusive_output(output_dir: Path) -> Iterator[bool]:
    """Hold ``<output_dir>/live.lock`` for one live run, without waiting.

    Two processes on the same ``output_dir`` would otherwise both skip the
    same completed runs and launch the same pending run twice. The ledger
    has its own lock for each reserve; this one serialises whole runs.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    fd = safe_io.open_lock(output_dir, output_dir / "live.lock")
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


async def regenerate(output_dir: Path) -> int:
    """Rebuild the reports from ``runs.jsonl`` without launching anything."""
    output_dir = Path(output_dir).resolve()
    try:
        plan = read_plan(output_dir)
    except (OSError, ValueError, FileNotFoundError) as exc:
        print(f"plan.json: {exc}", file=sys.stderr)
        return EXIT_VALIDATION
    experiment = plan.get("experiment") if isinstance(plan.get("experiment"), dict) else {}
    baseline = str(experiment.get("baseline") or "")
    variants = tuple(
        str(item.get("id"))
        for item in experiment.get("variants") or ()
        if isinstance(item, dict) and item.get("id")
    )
    records = load_records(output_dir)
    planned = {str(item.get("run8")) for item in plan.get("runs") or () if isinstance(item, dict)}
    incomplete = _incomplete(records, planned)
    report = build_report(
        records,
        baseline=baseline,
        variant_ids=variants,
        incomplete=incomplete,
        skills_unknown=bool(plan.get("skills_unknown")),
    )
    write_reports(output_dir, report)
    return _exit_code(records, planned, incomplete)


def _exit_code(records: list[RunRecord], planned: set[str], incomplete: bool) -> int:
    """Leak, then all-setup-failed, then incomplete, then success.

    An experiment whose every planned run failed setup measured nothing, so
    it must not exit 0 even though its report is complete.
    """
    if any(item.worker_status == WorkerStatus.LEAK_CHECK_FAILED.value for item in records):
        return EXIT_LEAK
    mine = [item for item in records if item.run8 in planned]
    if mine and planned <= {item.run8 for item in mine} and all(
        item.worker_status == WorkerStatus.SETUP_FAILED.value for item in mine
    ):
        return EXIT_SETUP
    if incomplete:
        return EXIT_BUDGET
    return EXIT_OK


def plan_runs(experiment: Experiment, samples: tuple[Sample, ...]) -> tuple[PlannedRun, ...]:
    """Sample, then model, then a seed-shuffled variant, then repeat."""
    planned: list[PlannedRun] = []
    seen: set[str] = set()
    for sample in samples:
        for model in experiment.models:
            for variant in _ordered_variants(experiment, sample.key, model):
                repeats = (
                    experiment.baseline_repeats
                    if variant.id == experiment.baseline
                    else experiment.repeats
                )
                for repeat in range(repeats):
                    run8 = run_id(
                        experiment.id, sample.key, variant.id, model, repeat
                    )
                    if run8 in seen:
                        raise SpecError("id", f"run id collision on {run8}")
                    seen.add(run8)
                    planned.append(
                        PlannedRun(
                            run8=run8,
                            sample_key=sample.key,
                            sample_index=sample.index,
                            variant_id=variant.id,
                            model=model,
                            repeat=repeat,
                            reviewer_model=_reviewer_model(
                                experiment, sample.key, model, variant.id, repeat
                            ),
                            is_baseline=variant.id == experiment.baseline,
                        )
                    )
    return tuple(planned)


def run_id(
    experiment_id: str, sample_key: str, variant_id: str, model: str, repeat: int
) -> str:
    material = f"{experiment_id}\n{sample_key}\n{variant_id}\n{model}\n{repeat}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:8]


async def _live(
    experiment: Experiment,
    samples: tuple[Sample, ...],
    runs: tuple[PlannedRun, ...],
    *,
    runner: Any | None,
    review_backend_factory: ReviewFactory,
    command_runner: Any | None,
    clock: Clock,
    script: Path,
    serial_lock_dir: Path,
) -> int:
    by_sample = {item.key: item for item in samples}
    try:
        assert_output_contained(experiment.repo, experiment.output_dir)
    except SpecError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_VALIDATION
    try:
        for name in ("repos", "runs", "worktrees", "g2-worktrees"):
            safe_io.ensure_real_dir(experiment.output_dir, experiment.output_dir / name)
    except OSError as exc:
        print(f"output_dir: {exc}", file=sys.stderr)
        return EXIT_VALIDATION
    try:
        harness, leaked, broken = _prepare_harness(experiment, samples)
    except LeakError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_LEAK
    base_g2 = await _base_g2(experiment, samples, harness, command_runner)
    template_refs: dict[str, set[str]] = defaultdict(set)
    for (sample_key, _variant), made in harness.items():
        template_refs[by_sample[sample_key].base_commit].add(made.ref)
    ledger = Ledger(experiment.output_dir / "ledger.jsonl", experiment.budget, clock=clock)
    prior = {
        item.run8: item
        for item in load_records(experiment.output_dir)
        if item.completed and item.worker_status not in _RETRIED_ON_RESUME
    }
    fresh: list[RunRecord] = []
    stop = False
    for planned in runs:
        if planned.run8 in prior:
            continue
        sample = by_sample[planned.sample_key]
        if sample.base_commit in broken:
            fresh.append(_setup_failed_record(planned, base_g2, sample))
            continue
        if stop or sample.base_commit in leaked:
            fresh.append(_skipped_record(planned, sample, harness, base_g2, leaked))
            if sample.base_commit in leaked:
                continue
            stop = True
            continue
        record, reserved = await _one_run(
            experiment,
            planned,
            sample,
            harness[(sample.key, planned.variant_id)],
            base_g2.get((sample.key, planned.variant_id), G2Status.NOT_RUN.value),
            ledger,
            template_refs=template_refs[sample.base_commit],
            runner=runner,
            review_backend_factory=review_backend_factory,
            command_runner=command_runner,
            script=script,
            serial_lock_dir=serial_lock_dir,
        )
        fresh.append(record)
        append_record(experiment.output_dir, record)
        if not reserved:
            stop = True
    records = load_records(experiment.output_dir)
    # Runs recorded only in memory (leak/budget before append) must be on disk.
    stored = {item.run8: item for item in records}
    for record in fresh:
        previous = stored.get(record.run8)
        if previous is None:
            append_record(experiment.output_dir, record)
            records.append(record)
        elif previous.worker_status != record.worker_status and (
            previous.worker_status in _RETRIED_ON_RESUME
        ):
            # A retried setup failure or budget skip that ended differently.
            append_record(experiment.output_dir, record)
            records[records.index(previous)] = record
    planned_ids = {item.run8 for item in runs}
    incomplete = _incomplete(records, planned_ids)
    report = build_report(
        records,
        baseline=experiment.baseline,
        variant_ids=tuple(item.id for item in experiment.variants),
        incomplete=incomplete,
        skills_unknown=experiment.skills_unknown,
    )
    write_reports(experiment.output_dir, report)
    return _exit_code(records, planned_ids, incomplete)


# A budget skip and a setup failure (a full disk, a planted symlink, a
# directory AGENTS.md) spend no budget, so a resume tries them again.
_RETRIED_ON_RESUME = frozenset(
    {WorkerStatus.BUDGET_SKIPPED.value, WorkerStatus.SETUP_FAILED.value}
)


def _prepare_harness(
    experiment: Experiment, samples: tuple[Sample, ...]
) -> tuple[dict[tuple[str, str], HarnessCommit], set[str], set[str]]:
    """Build the per-base templates.

    Returns the harness commits, the bases that failed the closed-world
    check, and the bases whose setup failed for a local reason (for example
    a tracked ``AGENTS.md`` that is a directory). Neither stops the other
    bases, and the report is still written.
    """
    grouped: dict[str, list[Sample]] = defaultdict(list)
    for sample in samples:
        grouped[sample.base_commit].append(sample)
    harness: dict[tuple[str, str], HarnessCommit] = {}
    leaked: set[str] = set()
    broken: set[str] = set()
    for base, group in grouped.items():
        try:
            safe_io.ensure_real_dir(experiment.output_dir, experiment.output_dir / "repos")
            repo = prepare_eval_repo(experiment.output_dir, experiment.repo, base)
            made: list[HarnessCommit] = []
            for sample in group:
                for variant in experiment.variants:
                    made.append(
                        make_harness_commit(
                            repo,
                            base_sha=base,
                            sample=sample,
                            variant=variant,
                            experiment=experiment,
                        )
                    )
            refs = {item.ref for item in made}
            _check_repo(experiment.output_dir, repo, base, refs, {item.commit for item in made})
        except LeakError:
            leaked.add(base)
            continue
        except OSError as exc:
            print(f"setup failed for base {base[:12]}: {type(exc).__name__}", file=sys.stderr)
            broken.add(base)
            continue
        for item in made:
            harness[(item.sample_key, item.variant_id)] = item
    return harness, leaked, broken


def _check_repo(
    output_dir: Path,
    repo: Path,
    base: str,
    refs: set[str],
    harness_shas: set[str],
) -> None:
    """The per-base template holds exactly the harness refs and their closure.

    Workers never run in the template (each run has its own repository), so
    on every start, resumed or not, any extra ref or commit is a leak.
    """
    del harness_shas
    assert_no_leak(repo, refs)
    path = _closure_path(output_dir, base)
    safe_io.write_text(output_dir, path, "\n".join(sorted(closure_of(repo, refs))) + "\n")


async def _base_g2(
    experiment: Experiment,
    samples: tuple[Sample, ...],
    harness: dict[tuple[str, str], HarnessCommit],
    command_runner: Any | None,
) -> dict[tuple[str, str], str]:
    found: dict[tuple[str, str], str] = {}
    for sample in samples:
        for variant in experiment.variants:
            commit = harness.get((sample.key, variant.id))
            if commit is None:
                continue
            status, _commands = await _focused(
                experiment,
                sample,
                commit,
                command_runner,
                branch=commit.commit,
                run_label=f"base-{commit.commit[:8]}",
            )
            found[(sample.key, variant.id)] = status
    return found


async def _one_run(
    experiment: Experiment,
    planned: PlannedRun,
    sample: Sample,
    commit: HarnessCommit,
    base_status: str,
    ledger: Ledger,
    *,
    template_refs: set[str],
    runner: Any | None,
    review_backend_factory: ReviewFactory,
    command_runner: Any | None,
    script: Path,
    serial_lock_dir: Path,
) -> tuple[RunRecord, bool]:
    dispatch_id, _line_id, _branch = blinded_identity(planned.run8, sample.index)
    line = blinded_line(sample, planned.run8, commit.commit)
    dispatch = blinded_dispatch(sample, planned.run8, line)
    template = experiment.output_dir / "repos" / sample.base_commit[:12]
    repo = _run_repo_path(experiment.output_dir, planned.run8)
    launcher = ScriptLauncher(
        experiment.output_dir / "runs",
        cwd=repo,
        worktrees_dir=experiment.output_dir / "worktrees",
        skills_hub=commit.skills_hub,
        dispatch_id=dispatch_id,
        script=_launch_script(
            experiment.output_dir, script, runner=runner, serial_lock_dir=serial_lock_dir
        ),
        runner=runner,
    )
    try:
        argv = launcher.argv(line, planned.model, [])
        visible = "\n".join(argv[2:])
        assert_prompt_closed(
            visible,
            sample=sample,
            variant_id=planned.variant_id,
            model=planned.model,
            experiment_id=experiment.id,
        )
        _ensure_run_dirs(experiment.output_dir, repo)
        closure = prepare_run_repo(template, repo, commit, sample.base_commit)
    except LeakError:
        return _record(
            planned,
            commit,
            base_status,
            worker_status=WorkerStatus.LEAK_CHECK_FAILED.value,
            completed=True,
        ), True
    except OSError as exc:
        print(f"setup failed for run {planned.run8}: {exc}", file=sys.stderr)
        return _record(
            planned,
            commit,
            base_status,
            worker_status=WorkerStatus.SETUP_FAILED.value,
            completed=True,
        ), True

    async def finish(record: RunRecord, keep_going: bool) -> tuple[RunRecord, bool]:
        # Close the run: keep the diff for review by hand, then re-run the
        # closed-world check on this run's repository and on the template
        # before the next run starts.
        _save_worker_diff(experiment.output_dir, repo, commit.commit, line.branch, planned.run8)
        leaked = False
        try:
            assert_run_closed(repo, harness=commit, worker_branch=line.branch, closure=closure)
            assert_no_leak(template, template_refs)
        except LeakError:
            leaked = True
        await _cleanup(launcher, dispatch)
        if leaked:
            return _record(
                planned,
                commit,
                base_status,
                worker_status=WorkerStatus.LEAK_CHECK_FAILED.value,
                worker_exit_code=record.worker_exit_code,
                tokens=record.tokens,
                duration_secs=record.duration_secs,
                completed=True,
            ), keep_going
        return record, keep_going

    reserved = ledger.reserve("run", planned.run8, planned.model)
    if not reserved.ok:
        return await finish(
            _record(
                planned,
                commit,
                base_status,
                worker_status=WorkerStatus.BUDGET_SKIPPED.value,
                completed=False,
            ),
            False,
        )
    started = time.monotonic()
    worker_status, exit_code, tokens = await _launch(
        launcher, line, planned.model, experiment.budget.worker_timeout_seconds
    )
    duration = time.monotonic() - started
    ledger.settle(
        "run",
        planned.run8,
        result=worker_status,
        tokens=tokens,
        duration_secs=duration,
    )
    g2_status = G2Status.NOT_RUN.value
    commands: tuple[CommandOutcome, ...] = ()
    files = insertions = deletions = out_of_scope = 0
    preexisting = base_status == G2Status.FAIL.value
    if worker_status == WorkerStatus.COMMITTED.value:
        g2_status, commands = await _focused(
            experiment,
            sample,
            commit,
            command_runner,
            branch=line.branch,
            run_label=planned.run8,
            dispatch=dispatch,
            line=line,
            repo=repo,
        )
        files, insertions, deletions, paths = _diff_stat(repo, commit.commit, line.branch)
        out_of_scope = _out_of_scope(paths, line.write_scope)
    review_status = ReviewStatus.NOT_RUN.value
    review_passed: bool | None = None
    blocker_count = 0
    blocker_sha = ""
    needs_review = _needs_review(worker_status, g2_status, preexisting, base_status)
    if needs_review:
        # CrossReviewer retries an unparseable reply, which is a second
        # backend call. Reserve immediately before each call, including that
        # retry, or one logical review spends two reviews past max_reviews.
        # A refused retry is budget exhaustion, not a finished unparseable
        # review: the run stays incomplete so the report exits 3.
        meter = _ReviewMeter(ledger, planned.run8)
        try:
            review_status, review_passed, blocker_count, blocker_sha = await _review(
                review_backend_factory,
                dispatch,
                repo,
                line,
                planned.reviewer_model,
                meter,
            )
        except _ReviewBudgetRefused:
            record = _record(
                planned,
                commit,
                base_status,
                worker_status=worker_status,
                worker_exit_code=exit_code,
                g2_status=g2_status,
                g2_commands=commands,
                tokens=tokens,
                duration_secs=duration,
                files_changed=files,
                insertions=insertions,
                deletions=deletions,
                out_of_scope_files=out_of_scope,
                preexisting=preexisting,
                unscored_reason="budget_skipped",
                completed=False,
            )
            return await finish(record, False)
        except Exception:  # noqa: BLE001 -- a review that cannot start is unscored
            review_status = ReviewStatus.UNPARSEABLE.value
    score, reason = score_run(
        worker_status=worker_status,
        g2_status=g2_status,
        review_status=review_status,
        preexisting_failure=preexisting,
        base_g2_status=base_status,
    )
    record = _record(
        planned,
        commit,
        base_status,
        worker_status=worker_status,
        worker_exit_code=exit_code,
        g2_status=g2_status,
        g2_commands=commands,
        review_status=review_status,
        review_passed=review_passed,
        blocker_count=blocker_count,
        blocker_sha256=blocker_sha,
        tokens=tokens,
        duration_secs=duration,
        files_changed=files,
        insertions=insertions,
        deletions=deletions,
        out_of_scope_files=out_of_scope,
        preexisting=preexisting,
        score=score,
        unscored_reason=reason,
        completed=True,
    )
    return await finish(record, True)


def _needs_review(
    worker_status: str, g2_status: str, preexisting: bool, base_status: str
) -> bool:
    if worker_status != WorkerStatus.COMMITTED.value:
        return False
    if base_status == G2Status.ENV_ERROR.value:
        return False
    if g2_status == G2Status.TIMEOUT.value:
        return False
    if g2_status == G2Status.FAIL.value and not preexisting:
        return False
    if g2_status == G2Status.ENV_ERROR.value and not preexisting:
        return False
    return True


async def _launch(
    launcher: ScriptLauncher, line: Line, model: str, timeout: float
) -> tuple[str, int | None, int]:
    try:
        result = await asyncio.wait_for(
            launcher.launch_worker(
                line,
                model,
                [],
                session_id=None,
                session_mode=None,
                handoff_path=None,
            ),
            timeout=timeout,
        )
    except TimeoutError:
        return WorkerStatus.TIMEOUT.value, None, 0
    except NoNewCommitError:
        return WorkerStatus.NO_NEW_COMMIT.value, 0, 0
    except UncommittedChangesError:
        return WorkerStatus.UNCOMMITTED_CHANGES.value, 0, 0
    except WorkerLaunchFailure as exc:
        kind = getattr(exc.failure_class, "value", "")
        if not isinstance(kind, str):
            kind = ""
        return _failure_status(exc.exit_code, kind), exc.exit_code, 0
    except LaunchError as exc:
        code = getattr(exc, "exit_code", None)
        if not isinstance(code, int):
            # No exit code: the worker never started (missing launch script,
            # a refused worktree). That is the harness, not the variant.
            return WorkerStatus.ENV_ERROR.value, None, 0
        return _failure_status(code), code, 0
    except DispatchError:
        # git_evidence_unavailable: the launcher could not prepare or read the
        # line worktree. Unscored, and the experiment still writes its report.
        return WorkerStatus.ENV_ERROR.value, None, 0
    return WorkerStatus.COMMITTED.value, 0, int(result.tokens)


def _failure_status(code: int | None, failure_class: str = "") -> str:
    """Classify a launcher exit.

    Infrastructure stays unscored: the launcher timeout (124), a missing CLI
    (127), and the provider classes rate_limited (75), quota_exhausted (76),
    auth_failed (77), confirmation_required, network, and overloaded (the classifier's verdict on
    the generic exit 79). Only a real worker failure, such as a crash or
    context exhaustion, scores 0.
    """
    if code == 124:
        return WorkerStatus.TIMEOUT.value
    if code == 127:
        return WorkerStatus.ENV_ERROR.value
    by_class = _INFRA_FAILURE_CLASSES.get(failure_class)
    if by_class is not None:
        return by_class
    by_code = _INFRA_EXIT_CODES.get(code) if code is not None else None
    if by_code is not None:
        return by_code
    return WorkerStatus.WORKER_FAILED.value


_INFRA_FAILURE_CLASSES = {
    "rate_limited": WorkerStatus.RATE_LIMITED.value,
    "quota_exhausted": WorkerStatus.QUOTA_EXHAUSTED.value,
    "auth_failed": WorkerStatus.AUTH_FAILED.value,
    # The CLI's own guard refused a command (N39): not the model's work.
    "confirmation_required": WorkerStatus.CONFIRMATION_REQUIRED.value,
    # The CLI's safety gate blocked a tool call: not the model's work either.
    "tool_blocked": WorkerStatus.CONFIRMATION_REQUIRED.value,
    "network": WorkerStatus.NETWORK.value,
    "overloaded": WorkerStatus.OVERLOADED.value,
}
_INFRA_EXIT_CODES = {
    75: WorkerStatus.RATE_LIMITED.value,
    76: WorkerStatus.QUOTA_EXHAUSTED.value,
    77: WorkerStatus.AUTH_FAILED.value,
}


async def _focused(
    experiment: Experiment,
    sample: Sample,
    commit: HarnessCommit,
    command_runner: Any | None,
    *,
    branch: str,
    run_label: str,
    dispatch: Dispatch | None = None,
    line: Line | None = None,
    repo: Path | None = None,
) -> tuple[str, tuple[CommandOutcome, ...]]:
    """Reuse GateRunner. It does not need a backend or the dispatch machine.

    Its logs live under ``runs/gates/``. A copy is also written to
    ``runs/<run>/g2-<i>.log`` so a person can open one directory per run.
    """
    if repo is None:
        repo = experiment.output_dir / "repos" / sample.base_commit[:12]
    runs_dir = experiment.output_dir / "runs"
    settings = GateSettings(
        runs_dir=runs_dir,
        repo_root=repo,
        worktrees_dir=experiment.output_dir / "g2-worktrees",
    )
    gate = GateRunner(settings, commands=command_runner or SubprocessCommandRunner())
    if line is None:
        line = Line(
            id=f"s{sample.index}",
            name=f"s{sample.index}",
            task=sample.task,
            branch=branch,
            acceptance=sample.acceptance,
            write_scope=sample.write_scope,
            base_commit=commit.commit,
        )
    if dispatch is None:
        dispatch = Dispatch(
            id=f"base-{run_label}",
            goal=sample.goal,
            user_id="harness-eval",
            created_at="1970-01-01T00:00:00+00:00",
            lines=[line],
        )
    step = await gate.run_g2(dispatch, line)
    commands = _command_outcomes(dispatch, line.id)
    for item in commands:
        source = _find_log(runs_dir, dispatch.id, line.id, item.index)
        if source is None:
            continue
        dest = runs_dir / run_label / f"g2-{item.index}.log"
        try:
            safe_io.write_bytes(experiment.output_dir, dest, safe_io.read_bytes(source))
        except OSError as exc:
            # The copy is a convenience; the original stays under runs/gates.
            print(f"g2 log copy skipped for {run_label}: {exc}", file=sys.stderr)
    return _classify_g2(step.result, commands, reason=step.reason), commands


def _classify_g2(
    result: str, commands: tuple[CommandOutcome, ...], *, reason: str = ""
) -> str:
    if result == "skipped":
        return G2Status.SKIPPED.value
    if result == "pass":
        return G2Status.PASS.value
    # SubprocessCommandRunner records a timeout as exit 124 and reason
    # g2_timeout. That is an infrastructure miss, not a failing acceptance.
    if reason == G2_TIMEOUT or any(item.exit_code == 124 for item in commands):
        return G2Status.TIMEOUT.value
    if any(item.exit_code == 127 for item in commands):
        return G2Status.ENV_ERROR.value
    return G2Status.FAIL.value


def _command_outcomes(dispatch: Dispatch, line_id: str) -> tuple[CommandOutcome, ...]:
    gates = dispatch.gates if isinstance(dispatch.gates, dict) else {}
    lines = gates.get("lines") if isinstance(gates.get("lines"), dict) else {}
    snap = lines.get(line_id) if isinstance(lines, dict) else None
    gate_map = snap.get("gates") if isinstance(snap, dict) else None
    g2 = gate_map.get("G2") if isinstance(gate_map, dict) else None
    raw = g2.get("commands") if isinstance(g2, dict) else None
    if not isinstance(raw, list):
        return ()
    outcomes: list[CommandOutcome] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        outcomes.append(
            CommandOutcome(
                index=int(item.get("index") or 0),
                exit_code=int(item.get("exit_code") or 0),
                duration_ms=int(item.get("duration_ms") or 0),
            )
        )
    return tuple(outcomes)


def _find_log(runs_dir: Path, dispatch_id: str, line_id: str, index: int) -> Path | None:
    direct = runs_dir / "gates" / dispatch_id / line_id / f"g2-0-{index}.log"
    if direct.is_file():
        return direct
    matches = list(runs_dir.glob(f"gates/**/g2-0-{index}.log"))
    return matches[0] if matches else None


class _ReviewBudgetRefused(Exception):
    """The ledger refused a review before the backend was called."""


class _ReviewMeter:
    """Reserves one review for every backend call, including a retry."""

    def __init__(self, ledger: Ledger, run8: str) -> None:
        self.ledger = ledger
        self.run8 = run8
        self.calls = 0

    def wrap(self, backend: Any) -> Any:
        meter = self

        class _Backend:
            async def review(self, model: str, prompt: str) -> str:
                reservation = meter.ledger.reserve("review", meter.run8, model)
                if not reservation.ok:
                    raise _ReviewBudgetRefused()
                started = time.monotonic()
                result = "called"
                try:
                    return await backend.review(model, prompt)
                except Exception as exc:
                    result = type(exc).__name__
                    raise
                finally:
                    meter.calls += 1
                    meter.ledger.settle(
                        "review",
                        meter.run8,
                        result=result,
                        tokens=0,
                        duration_secs=time.monotonic() - started,
                    )

        return _Backend()


async def _review(
    factory: ReviewFactory,
    dispatch: Dispatch,
    repo: Path,
    line: Line,
    model: str,
    meter: _ReviewMeter,
) -> tuple[str, bool | None, int, str]:
    backend = meter.wrap(factory(repo))
    reviewer = CrossReviewer(backend, branch_evidence(dispatch, repo))
    verdict = await reviewer.review_line(line.id, model)
    digest = hashlib.sha256("\n".join(verdict.blockers).encode("utf-8")).hexdigest()
    if verdict_unparseable(verdict):
        return ReviewStatus.UNPARSEABLE.value, None, len(verdict.blockers), digest
    if verdict.passed:
        return ReviewStatus.PASSED.value, True, 0, ""
    return ReviewStatus.BLOCKED.value, False, len(verdict.blockers), digest


async def _cleanup(launcher: ScriptLauncher, dispatch: Dispatch) -> None:
    try:
        await launcher.cleanup_worktrees(dispatch)
    except Exception:
        # The branch stays for inspection. A cleanup failure is not a score.
        return


def _ensure_run_dirs(output_dir: Path, repo: Path) -> None:
    """Every harness-owned directory a run writes under must be real.

    A symlinked ``repos/runs`` (or ``runs``, ``worktrees``) parent would put
    the run repository, worktree, or logs wherever the link points.
    """
    safe_io.ensure_real_dir(output_dir, repo.parent)
    for name in ("runs", "worktrees", "g2-worktrees"):
        safe_io.ensure_real_dir(output_dir, output_dir / name)


def _run_repo_path(output_dir: Path, run8: str) -> Path:
    """A fresh repository per attempt. A crashed attempt's repo stays for review."""
    root = output_dir / "repos" / "runs"
    candidate = root / run8
    attempt = 0
    while candidate.exists() or candidate.is_symlink():
        attempt += 1
        candidate = root / f"{run8}.{attempt}"
    return candidate


def _save_worker_diff(output_dir: Path, repo: Path, base: str, branch: str, run8: str) -> None:
    exists = git(repo, ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], check=False)
    if exists.returncode != 0:
        return
    result = git(
        repo,
        ["diff", "--binary", "--no-ext-diff", "--no-textconv", f"{base}...{branch}", "--"],
        check=False,
    )
    if result.returncode != 0:
        return
    dest = output_dir / "runs" / run8 / "worker.diff"
    try:
        safe_io.write_text(output_dir, dest, result.stdout)
    except OSError as exc:
        # The diff is for review by hand; losing it must not stop the loop.
        print(f"worker.diff not saved for {run8}: {exc}", file=sys.stderr)


def _diff_stat(repo: Path, base: str, branch: str) -> tuple[int, int, int, tuple[str, ...]]:
    result = git(
        repo,
        [
            "diff",
            "--numstat",
            "--no-ext-diff",
            "--no-textconv",
            f"{base}...{branch}",
            "--",
        ],
        check=False,
    )
    files = 0
    insertions = 0
    deletions = 0
    paths: list[str] = []
    if result.returncode != 0:
        return 0, 0, 0, ()
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        files += 1
        paths.append(parts[-1])
        if parts[0].isdigit():
            insertions += int(parts[0])
        if parts[1].isdigit():
            deletions += int(parts[1])
    return files, insertions, deletions, tuple(paths)


def _out_of_scope(paths: tuple[str, ...], write_scope: tuple[str, ...]) -> int:
    if not write_scope:
        return 0
    return sum(
        1
        for path in paths
        if not any(path_matches(pattern, path) for pattern in write_scope)
    )


def _record(
    planned: PlannedRun,
    commit: HarnessCommit,
    base_status: str,
    *,
    worker_status: str,
    completed: bool,
    worker_exit_code: int | None = None,
    g2_status: str = G2Status.NOT_RUN.value,
    g2_commands: tuple[CommandOutcome, ...] = (),
    review_status: str = ReviewStatus.NOT_RUN.value,
    review_passed: bool | None = None,
    blocker_count: int = 0,
    blocker_sha256: str = "",
    tokens: int = 0,
    duration_secs: float = 0.0,
    files_changed: int = 0,
    insertions: int = 0,
    deletions: int = 0,
    out_of_scope_files: int = 0,
    preexisting: bool = False,
    score: float | None = None,
    unscored_reason: str | None = None,
) -> RunRecord:
    if score is None and unscored_reason is None:
        score, unscored_reason = score_run(
            worker_status=worker_status,
            g2_status=g2_status,
            review_status=review_status,
            preexisting_failure=preexisting,
            base_g2_status=base_status,
        )
    return RunRecord(
        run8=planned.run8,
        sample_key=planned.sample_key,
        sample_index=planned.sample_index,
        variant_id=planned.variant_id,
        model=planned.model,
        repeat=planned.repeat,
        reviewer_model=planned.reviewer_model,
        worker_status=worker_status,
        worker_exit_code=worker_exit_code,
        g2_status=g2_status,
        g2_commands=g2_commands,
        review_status=review_status,
        review_passed=review_passed,
        blocker_count=blocker_count,
        blocker_sha256=blocker_sha256,
        tokens=tokens,
        duration_secs=duration_secs,
        files_changed=files_changed,
        insertions=insertions,
        deletions=deletions,
        out_of_scope_files=out_of_scope_files,
        harness_commit=commit.commit,
        variant_files_sha256=commit.variant_files_sha256,
        skills_unknown=commit.skills_unknown,
        score=score,
        unscored_reason=unscored_reason,
        preexisting_failure=preexisting,
        base_g2_status=base_status,
        completed=completed,
    )


def _skipped_record(
    planned: PlannedRun,
    sample: Sample,
    harness: dict[tuple[str, str], HarnessCommit],
    base_g2: dict[tuple[str, str], str],
    leaked: set[str],
) -> RunRecord:
    commit = harness.get((sample.key, planned.variant_id))
    if commit is None:
        commit = HarnessCommit(
            sample_key=sample.key,
            variant_id=planned.variant_id,
            commit="",
            variant_files_sha256="",
            skills_hub="",
            skills_unknown=False,
            ref="",
        )
    status = (
        WorkerStatus.LEAK_CHECK_FAILED.value
        if sample.base_commit in leaked
        else WorkerStatus.BUDGET_SKIPPED.value
    )
    base_status = base_g2.get((sample.key, planned.variant_id), G2Status.NOT_RUN.value)
    return _record(
        planned,
        commit,
        base_status,
        worker_status=status,
        completed=status == WorkerStatus.LEAK_CHECK_FAILED.value,
    )


def _setup_failed_record(
    planned: PlannedRun, base_g2: dict[tuple[str, str], str], sample: Sample
) -> RunRecord:
    commit = HarnessCommit(
        sample_key=sample.key,
        variant_id=planned.variant_id,
        commit="",
        variant_files_sha256="",
        skills_hub="",
        skills_unknown=False,
        ref="",
    )
    base_status = base_g2.get((sample.key, planned.variant_id), G2Status.NOT_RUN.value)
    return _record(
        planned,
        commit,
        base_status,
        worker_status=WorkerStatus.SETUP_FAILED.value,
        completed=True,
    )


def _incomplete(records: list[RunRecord], planned: set[str]) -> bool:
    if any(item.worker_status == WorkerStatus.BUDGET_SKIPPED.value for item in records):
        return True
    finished = {
        item.run8
        for item in records
        if item.completed or item.worker_status == WorkerStatus.BUDGET_SKIPPED.value
    }
    return not planned <= finished


def _ordered_variants(experiment: Experiment, sample_key: str, model: str) -> tuple[Any, ...]:
    seed = experiment.samples.seed

    def key(variant: Any) -> str:
        material = f"{seed}:{sample_key}:{model}:{variant.id}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    return tuple(sorted(experiment.variants, key=key))


def _reviewer_model(
    experiment: Experiment,
    sample_key: str,
    author: str,
    variant_id: str,
    repeat: int,
) -> str:
    pool = [item for item in experiment.reviewer.models if item.lower() != author.lower()]
    if not pool:
        raise SpecError("reviewer.models", "no reviewer distinct from the author")
    if experiment.reviewer.fixed_per_sample:
        material = f"{experiment.samples.seed}:{sample_key}"
    else:
        material = f"{experiment.samples.seed}:{sample_key}:{variant_id}:{repeat}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return pool[int(digest[:8], 16) % len(pool)]


def _reject_sha_mismatch(experiment: Experiment, samples: tuple[Sample, ...]) -> None:
    path = experiment.output_dir / "plan.json"
    if not path.is_file():
        return
    try:
        previous = read_plan(experiment.output_dir)
    except (OSError, ValueError) as exc:
        raise SpecError("output_dir", f"existing plan.json cannot be read ({exc})") from exc
    if previous.get("experiment_sha256") != experiment.canonical_sha256:
        raise SpecError(
            "output_dir",
            "experiment changed since plan.json; choose a different output_dir",
        )
    current = selected_inputs_fingerprint(samples)
    stored = previous.get("inputs_sha256")
    if not isinstance(stored, str) or stored != current:
        raise SpecError(
            "samples",
            "selected inputs or resolved bases changed since plan.json; "
            "choose a different output_dir",
        )


def _closure_path(output_dir: Path, base: str) -> Path:
    return output_dir / "repos" / f"{base[:12]}.closure"


def _print_budget(experiment: Experiment, run_count: int) -> None:
    budget = experiment.budget
    print(
        "budget "
        f"max_runs={budget.max_runs} max_reviews={budget.max_reviews} "
        f"max_models={budget.max_models} max_wall_minutes={budget.max_wall_minutes} "
        f"planned_runs={run_count}"
    )


def _purge(experiment: Experiment) -> None:
    """Delete generated trees only after containment still holds.

    A symlink planted at ``output_dir/repos`` that points at the source
    repository is refused here, before ``rmtree``.
    """
    assert_output_contained(experiment.repo, experiment.output_dir)
    for name in ("repos", "worktrees", "g2-worktrees"):
        target = experiment.output_dir / name
        if not target.exists() and not target.is_symlink():
            continue
        assert_purge_target(experiment.repo, experiment.output_dir, target)
        if target.is_symlink():
            target.unlink()
            continue
        shutil.rmtree(target)


def _launch_script(
    output_dir: Path,
    script: Path,
    *,
    runner: Any | None,
    serial_lock_dir: Path | None = None,
) -> Path:
    """Point the real launcher's logs and FIFOs at ``output_dir``.

    ``ScriptLauncher`` copies the process environment into the child and does
    not set ``CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR``. ``dispatch_launch.sh`` then
    writes prompts and the provider lock under the live state directory. A
    wrapper execs the real script only after exporting a directory inside
    ``output_dir``. The provider serial lock stays the live one
    (``live_serial_lock_dir``), so eval never runs beside a live worker on a
    serialised provider. Injected runners never execute the script, so they keep
    the caller's path.
    """
    if runner is not None:
        return script
    return _contained_launch_script(
        output_dir, script, serial_lock_dir or live_serial_lock_dir()
    )


def _contained_launch_script(output_dir: Path, script: Path, serial_lock_dir: Path) -> Path:
    log_dir = (output_dir / "launch-logs").resolve()
    root = output_dir.resolve()
    if log_dir != root and not _is_relative_to(log_dir, root):
        raise SpecError("output_dir", "launch log directory escapes output_dir")
    log_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(log_dir, 0o700)
    wrapper = log_dir / "contained-launch.sh"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"export CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR={shlex.quote(str(log_dir))}\n"
        f"export CONDUCTOR_DISPATCH_SERIAL_LOCK_DIR={shlex.quote(str(serial_lock_dir))}\n"
        f"exec {shlex.quote(str(script))} \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o700)
    return wrapper


def live_serial_lock_dir(environ: dict[str, str] | None = None) -> Path:
    """The per-provider serial lock directory a live dispatch launch uses.

    Eval logs stay in ``output_dir``, but the lock is shared with live
    dispatch so an eval worker queues behind a live worker on the same
    provider instead of running beside it. This mirrors
    ``dispatch_launch.sh``: an explicit ``CONDUCTOR_DISPATCH_SERIAL_LOCK_DIR``,
    else ``<log dir>/locks`` where the log dir is
    ``CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR`` or
    ``${XDG_STATE_HOME:-$HOME/.local/state}/conductor/dispatch-launch``.
    Only the launcher creates anything there, and only a lock directory.
    This reads the current process's environment; when the backend runs
    with a different one, pass ``--serial-lock-dir`` (``serial_lock_dir``).
    """
    env = os.environ if environ is None else environ
    explicit = env.get("CONDUCTOR_DISPATCH_SERIAL_LOCK_DIR")
    if explicit:
        return Path(explicit)
    log_dir = env.get("CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR")
    if not log_dir:
        state = env.get("XDG_STATE_HOME") or str(Path(env.get("HOME") or Path.home()) / ".local" / "state")
        log_dir = str(Path(state) / "conductor" / "dispatch-launch")
    return Path(log_dir) / "locks"


def _is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _default_script() -> Path:
    return Path(__file__).resolve().parents[4] / "scripts" / "dispatch_launch.sh"
