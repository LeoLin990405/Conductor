"""Build a fresh eval repository that contains only one base and its ancestors.

The worker and the reviewer see a blinded line. The variant name, the model
name, and the original dispatch id stay out of the branch and the ticket id.
``ScriptLauncher`` still requires its own branch spelling, so the blinded
branch is ``feature/auto-dispatch-eval-<run8>/line-s<index>``.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import urllib.parse
from pathlib import Path

from src.tools.auto_dispatch.delivery import build_line_delivery_task
from src.tools.auto_dispatch.types import Dispatch, Line

from .spec import find_marker
from .types import (
    HARNESS_AUTHOR_EMAIL,
    HARNESS_AUTHOR_NAME,
    AgentsMdSpec,
    Experiment,
    HarnessCommit,
    Sample,
    SkillsSpec,
    VariantSpec,
)

_SHA = re.compile(r"[0-9a-fA-F]{7,64}\Z")
_WORKER_REF = re.compile(
    r"refs/heads/feature/auto-dispatch-eval-[0-9a-f]{8}/line-s[0-9]+\Z"
)


class LeakError(RuntimeError):
    """The eval repository or a prompt failed the closed-world check."""

    def __init__(self, message: str = "leak_check_failed") -> None:
        super().__init__(message)


def git_env(*extra: tuple[str, str]) -> dict[str, str]:
    """Git environment with no inherited config and no protocol but file."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_COUNT"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ALLOW_PROTOCOL"] = "file"
    for key, value in extra:
        env[key] = value
    return env


def git(
    repo: Path,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        env=env or git_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise LeakError(f"git {' '.join(args)} failed: {detail}")
    return result


def resolve_commit(repo: Path, sha: str) -> str | None:
    """Full commit id, or None when ``repo`` does not contain that commit."""
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        return None
    result = git(repo, ["rev-parse", "--verify", f"{sha}^{{commit}}"], check=False)
    if result.returncode != 0:
        return None
    text = result.stdout.strip()
    return text or None


def file_url(path: Path) -> str:
    posix = path.resolve().as_posix()
    return "file://" + urllib.parse.quote(posix)


def prepare_eval_repo(output_dir: Path, source: Path, base_sha: str) -> Path:
    """Create ``<output_dir>/repos/<sha12>`` with only ``base_sha`` and ancestors.

    ``git fetch`` uses the ``file://`` protocol and adds no remote. A later
    commit that exists only in ``source`` is not in the new object database.
    """
    full = resolve_commit(source, base_sha)
    if full is None:
        raise LeakError("leak_check_failed")
    dest = output_dir / "repos" / full[:12]
    _refuse_foreign_git_dir(dest)
    if (dest / ".git").is_dir():
        _assert_own_git_dir(dest)
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    init = subprocess.run(
        ["git", "init", "-b", "main", str(dest)],
        env=git_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    if init.returncode != 0:
        raise LeakError(f"git init failed: {init.stderr.strip()}")
    git(dest, ["config", "core.autocrlf", "false"])
    git(dest, ["config", "commit.gpgsign", "false"])
    # Materialize a tracked AGENTS.md symlink so apply can reject it. A false
    # setting would check the link text out as a regular file and hide the escape.
    git(dest, ["config", "core.symlinks", "true"])
    fetched = git(
        dest,
        ["fetch", "--no-tags", file_url(source), full],
        check=False,
    )
    if fetched.returncode != 0:
        raise LeakError(f"fetch failed: {fetched.stderr.strip()}")
    _assert_own_git_dir(dest)
    git(dest, ["checkout", "--detach", "FETCH_HEAD"])
    git(dest, ["branch", "-D", "main"], check=False)
    if git(dest, ["remote"], check=False).stdout.strip():
        raise LeakError("leak_check_failed")
    # HEAD is a ref, not a sha, so it cannot go through resolve_commit.
    checked = git(dest, ["rev-parse", "--verify", "HEAD^{commit}"], check=False)
    if checked.returncode != 0 or checked.stdout.strip() != full:
        raise LeakError("leak_check_failed")
    return dest


def prepare_run_repo(template: Path, dest: Path, harness: HarnessCommit, base_sha: str) -> set[str]:
    """Create ``dest`` holding only one run's harness commit and its ancestors.

    Workers and reviewers never use the shared per-base template, which
    carries every variant's harness ref. Each run gets its own repository,
    fetched over ``file://`` from the template by the harness commit alone,
    with that single ref and no remote. Another run's worker branch or
    another variant's harness commit is not in its object database.

    The root checkout is left at ``base_sha`` so a reviewer working in the
    repository root does not read the variant's AGENTS.md; the worker gets its
    own worktree from the harness commit. Returns the pre-run closure.
    """
    if dest.exists() or dest.is_symlink():
        raise LeakError("leak_check_failed")
    dest.parent.mkdir(parents=True, exist_ok=True)
    init = subprocess.run(
        ["git", "init", "-b", "main", str(dest)],
        env=git_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    if init.returncode != 0:
        raise LeakError(f"git init failed: {init.stderr.strip()}")
    _assert_own_git_dir(dest)
    git(dest, ["config", "core.autocrlf", "false"])
    git(dest, ["config", "commit.gpgsign", "false"])
    git(dest, ["config", "core.symlinks", "true"])
    fetched = git(dest, ["fetch", "--no-tags", file_url(template), harness.commit], check=False)
    if fetched.returncode != 0:
        raise LeakError(f"fetch failed: {fetched.stderr.strip()}")
    git(dest, ["update-ref", harness.ref, harness.commit])
    git(dest, ["checkout", "--detach", "--force", base_sha])
    git(dest, ["branch", "-D", "main"], check=False)
    if git(dest, ["remote"], check=False).stdout.strip():
        raise LeakError("leak_check_failed")
    assert_no_leak(dest, {harness.ref})
    return closure_of(dest, {harness.ref})


def assert_run_closed(
    repo: Path,
    *,
    harness: HarnessCommit,
    worker_branch: str,
    closure: set[str],
) -> None:
    """After one run, its repository may hold only its harness ref and its branch.

    Every reachable commit is either in the pre-run closure or descends from
    the harness commit. A fetched later commit (an answer from the source
    repository), any other ref, or a remote is ``leak_check_failed``.
    """
    allowed = {harness.ref, f"refs/heads/{worker_branch}"}
    if not _refs(repo) <= allowed:
        raise LeakError("leak_check_failed")
    if git(repo, ["remote"], check=False).stdout.strip():
        raise LeakError("leak_check_failed")
    for commit in _rev_list(repo, "--all"):
        if commit in closure:
            continue
        if _is_ancestor(repo, harness.commit, commit):
            continue
        raise LeakError("leak_check_failed")


def variant_material_sha256(experiment: Experiment, variant: VariantSpec) -> str:
    """Hash of the bytes that define the AGENTS.md change. ``keep`` hashes empty."""
    if variant.agents_md.mode in {"replace", "append"} and variant.agents_md.file:
        data = (experiment.experiment_dir / variant.agents_md.file).read_bytes()
    else:
        data = b""
    return hashlib.sha256(data).hexdigest()


def skills_value(skills: SkillsSpec) -> tuple[str, bool]:
    """``(skills_hub, skills_unknown)`` for ``ScriptLauncher``.

    Unset ``keep`` names are ``skills_unknown`` and launch with an empty list.
    ``set`` joins the operator's names. ``none`` is an empty list and is known.
    """
    if skills.mode == "keep":
        if skills.names is None:
            return "", True
        return ",".join(skills.names), False
    if skills.mode == "set":
        return ",".join(skills.names or ()), False
    return "", False


def harness_token(base_sha: str, sample_key: str, variant: VariantSpec, file_sha: str) -> str:
    names = "" if variant.skills.names is None else ",".join(variant.skills.names)
    material = "\n".join(
        (base_sha, sample_key, variant.id, variant.agents_md.mode, file_sha, variant.skills.mode, names)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def make_harness_commit(
    repo: Path,
    *,
    base_sha: str,
    sample: Sample,
    variant: VariantSpec,
    experiment: Experiment,
) -> HarnessCommit:
    """One deterministic commit on ``base_sha``. Re-running returns the same id.

    ``keep`` still gets an empty commit so every variant has the same shape.
    The message is only the opaque token. Author, committer, and date are fixed
    to the harness identity and the base commit's author date.
    """
    file_sha = variant_material_sha256(experiment, variant)
    token = harness_token(base_sha, sample.key, variant, file_sha)
    ref = f"refs/heads/h/{token[:32]}"
    existing = git(repo, ["rev-parse", "--verify", ref], check=False)
    skills_hub, unknown = skills_value(variant.skills)
    if existing.returncode == 0 and existing.stdout.strip():
        return HarnessCommit(
            sample_key=sample.key,
            variant_id=variant.id,
            commit=existing.stdout.strip(),
            variant_files_sha256=file_sha,
            skills_hub=skills_hub,
            skills_unknown=unknown,
            ref=ref,
        )
    _scan_variant(experiment, variant, sample)
    git(repo, ["checkout", "--detach", "--force", base_sha])
    git(repo, ["reset", "--hard", base_sha])
    git(repo, ["clean", "-fd"])
    _apply_agents(repo, variant.agents_md, experiment)
    env = _commit_env(repo, base_sha)
    git(repo, ["add", "--", "AGENTS.md"], env=env, check=False)
    committed = git(
        repo,
        [
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--allow-empty",
            "-m",
            token,
        ],
        env=env,
        check=False,
    )
    if committed.returncode != 0:
        raise LeakError(f"harness commit failed: {committed.stderr.strip()}")
    sha = git(repo, ["rev-parse", "HEAD"]).stdout.strip()
    git(repo, ["update-ref", ref, sha])
    # Leave the root at the base, not at the last variant's AGENTS.md: any
    # process that works in this directory must not read a variant.
    git(repo, ["checkout", "--detach", "--force", base_sha])
    return HarnessCommit(
        sample_key=sample.key,
        variant_id=variant.id,
        commit=sha,
        variant_files_sha256=file_sha,
        skills_hub=skills_hub,
        skills_unknown=unknown,
        ref=ref,
    )


def assert_no_leak(repo: Path, expected_refs: set[str]) -> None:
    """Fail when a ref or a commit is outside the harness closure.

    Expected refs are the harness refs this eval created. A ref added later,
    or a commit that is not an ancestor of those refs, is ``leak_check_failed``.
    Worker branches are not part of this check; call it before they exist.
    """
    actual = _refs(repo)
    if actual != expected_refs:
        raise LeakError("leak_check_failed")
    allowed: set[str] = set()
    for ref in expected_refs:
        allowed.update(_rev_list(repo, ref))
    seen = _rev_list(repo, "--all")
    if not seen or not seen <= allowed:
        raise LeakError("leak_check_failed")


def assert_descendants_only(
    repo: Path,
    *,
    harness_refs: set[str],
    harness_shas: set[str],
    closure: set[str],
) -> None:
    """After workers run, extra refs may only be this eval's blinded branches.

    A commit is allowed when it was in the pre-worker closure or it descends
    from a harness commit. A sibling of the base, such as a later answer
    commit, is rejected.
    """
    for ref in _refs(repo):
        if ref in harness_refs or ref.startswith("refs/heads/h/"):
            continue
        if _WORKER_REF.fullmatch(ref):
            continue
        raise LeakError("leak_check_failed")
    for commit in _rev_list(repo, "--all"):
        if commit in closure:
            continue
        if any(_is_ancestor(repo, harness, commit) for harness in harness_shas):
            continue
        raise LeakError("leak_check_failed")


def blinded_identity(run8: str, sample_index: int) -> tuple[str, str, str]:
    """``(dispatch_id, line_id, branch)`` with no variant, model, or experiment name."""
    line_id = f"s{sample_index}"
    dispatch_id = f"eval-{run8}"
    branch = f"feature/auto-dispatch-{dispatch_id}/line-{line_id}"
    return dispatch_id, line_id, branch


def blinded_line(sample: Sample, run8: str, harness_commit: str) -> Line:
    _dispatch_id, line_id, branch = blinded_identity(run8, sample.index)
    return Line(
        id=line_id,
        name=line_id,
        task=sample.task,
        branch=branch,
        acceptance=sample.acceptance,
        write_scope=sample.write_scope,
        base_commit=harness_commit,
        risk_bearing=sample.risk_bearing,
        current_run=run8,
    )


def blinded_dispatch(sample: Sample, run8: str, line: Line) -> Dispatch:
    dispatch_id, _line_id, _branch = blinded_identity(run8, sample.index)
    return Dispatch(
        id=dispatch_id,
        goal=sample.goal,
        user_id="harness-eval",
        created_at="1970-01-01T00:00:00+00:00",
        lines=[line],
    )


def worker_prompt(line: Line, dispatch_id: str) -> str:
    """The task argument ``ScriptLauncher`` will put on the worker argv."""
    return build_line_delivery_task(
        dispatch_id=dispatch_id,
        line_id=line.id,
        branch=line.branch,
        task=line.task,
    )


def assert_prompt_closed(
    prompt: str,
    *,
    sample: Sample,
    variant_id: str,
    model: str,
    experiment_id: str,
) -> None:
    """Reject a worker prompt that carries the original result or an eval name.

    Identifiers match as whole tokens only (not inside a longer word, path,
    or id), so ``short`` does not match ``short-lived``.

    - The whole prompt, task included, must not carry the original
      dispatch id, the experiment id, or the original outcome texts.
    - The part the eval writes around the task (blinded ids, branch,
      delivery instructions) must not carry the variant id or the model.
      The task itself is the historical text and may mention a model or a
      common word such as ``baseline``; the eval never puts names there.
    """
    for item in (sample.dispatch_id, experiment_id):
        if _has_token(prompt, item):
            raise LeakError("leak_check_failed")
    for item in sample.forbidden:
        if item and item in prompt:
            raise LeakError("leak_check_failed")
    wrapper = prompt.replace(sample.task, "", 1) if sample.task else prompt
    for item in (variant_id, model):
        if _has_token(wrapper, item):
            raise LeakError("leak_check_failed")


def _has_token(text: str, token: str) -> bool:
    if not token:
        return False
    pattern = rf"(?<![A-Za-z0-9_.-]){re.escape(token)}(?![A-Za-z0-9_-])"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def closure_of(repo: Path, refs: set[str]) -> set[str]:
    allowed: set[str] = set()
    for ref in refs:
        allowed.update(_rev_list(repo, ref))
    return allowed


def _scan_variant(experiment: Experiment, variant: VariantSpec, sample: Sample) -> None:
    texts: list[str] = []
    if variant.agents_md.file:
        texts.append((experiment.experiment_dir / variant.agents_md.file).read_text(encoding="utf-8"))
    if variant.skills.names:
        texts.extend(variant.skills.names)
    extra = (sample.dispatch_id,)
    for text in texts:
        if find_marker(text, *extra) is not None:
            raise LeakError("leak_check_failed")


def _apply_agents(repo: Path, spec: AgentsMdSpec, experiment: Experiment) -> None:
    path = repo / "AGENTS.md"
    if spec.mode == "keep":
        return
    if spec.mode == "remove":
        tracked = git(repo, ["ls-files", "--", "AGENTS.md"], check=False)
        if tracked.stdout.strip():
            git(repo, ["rm", "-f", "--", "AGENTS.md"])
        elif path.exists():
            path.unlink()
        return
    data = (experiment.experiment_dir / (spec.file or "")).read_bytes()
    if path.is_symlink():
        # write_bytes follows the link. A base commit whose AGENTS.md points
        # outside this eval repo would overwrite that target (source files or
        # live configuration). Refuse the escape before any read or write.
        # An in-repo link is removed and replaced by a regular file.
        if _agents_link_escapes(repo, path):
            raise LeakError("leak_check_failed")
        if spec.mode == "append":
            data = path.read_bytes() + data
        path.unlink()
    elif spec.mode == "append" and path.is_file():
        data = path.read_bytes() + data
    path.write_bytes(data)


def _commit_env(repo: Path, base_sha: str) -> dict[str, str]:
    dated = git(repo, ["log", "-1", "--format=%ad", "--date=raw", base_sha])
    when = dated.stdout.strip()
    return git_env(
        ("GIT_AUTHOR_NAME", HARNESS_AUTHOR_NAME),
        ("GIT_AUTHOR_EMAIL", HARNESS_AUTHOR_EMAIL),
        ("GIT_COMMITTER_NAME", HARNESS_AUTHOR_NAME),
        ("GIT_COMMITTER_EMAIL", HARNESS_AUTHOR_EMAIL),
        ("GIT_AUTHOR_DATE", when),
        ("GIT_COMMITTER_DATE", when),
    )


def _refs(repo: Path) -> set[str]:
    result = git(repo, ["for-each-ref", "--format=%(refname)"])
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _rev_list(repo: Path, rev: str) -> set[str]:
    result = git(repo, ["rev-list", rev], check=False)
    if result.returncode != 0:
        raise LeakError("leak_check_failed")
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _agents_link_escapes(repo: Path, path: Path) -> bool:
    """True when ``path`` resolves outside the eval repository.

    ``resolve`` follows a chain, so a link inside the repo that then leaves
    it is still an escape. A loop or an unreadable target is an escape too:
    the safe action is to stop before opening the target.
    """
    root = repo.resolve()
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return True
    if resolved == root:
        return True
    try:
        resolved.relative_to(root)
    except ValueError:
        return True
    return False


def _refuse_foreign_git_dir(dest: Path) -> None:
    """Reject a planted ``dest`` or ``dest/.git`` that could point elsewhere.

    A symlinked directory or ``.git``, or a ``gitdir:`` file, would let the
    harness commits and ``git init`` land in another repository, such as
    the source checkout.
    """
    if dest.is_symlink():
        raise LeakError("leak_check_failed")
    dot_git = dest / ".git"
    if dot_git.is_symlink():
        raise LeakError("leak_check_failed")
    if dot_git.exists() and not dot_git.is_dir():
        raise LeakError("leak_check_failed")


def _assert_own_git_dir(dest: Path) -> None:
    _refuse_foreign_git_dir(dest)
    result = git(dest, ["rev-parse", "--absolute-git-dir"], check=False)
    if result.returncode != 0:
        raise LeakError("leak_check_failed")
    try:
        actual = Path(result.stdout.strip()).resolve()
    except OSError as exc:
        raise LeakError("leak_check_failed") from exc
    if actual != (dest / ".git").resolve() or dest.resolve() not in actual.parents:
        raise LeakError("leak_check_failed")


def _is_ancestor(repo: Path, ancestor: str, commit: str) -> bool:
    result = git(repo, ["merge-base", "--is-ancestor", ancestor, commit], check=False)
    return result.returncode == 0
