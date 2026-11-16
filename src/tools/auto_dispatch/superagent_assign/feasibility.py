"""Pure feasibility check for one Super-agent line choice (N73 PRD 4.4).

The dispatcher does not ask the Super-agent again inside this function and
does not read the clock, the environment, the process table or the quota
ports. Everything those would answer is already on the injected
:class:`Snapshot`. The check returns ``ok`` or the first failing reason, in
the order PRD 4.4 names the conditions:

1. Roster eligibility for work, which always excludes claude and codex
   (Q1: they stay reserved, and ``dispatch_launch.sh`` refuses them), and
   ``can_take_line`` for this line.
2. Model id is ``default`` or a member of the provider's known list. A
   provider with no list accepts only ``default``. A known id still has
   to be one the launcher can pass. GLM passes ``--model`` only on the
   clean-room grammar. That grammar is the explicit ``clean-room``
   runtime, or a ``default`` / ``native_cli`` launch whose injected
   effective binary is ``zcode-cli`` (``NATIVE_CLI_ZCODE_BIN``). With no
   injection the launcher runs official ``zcode``, which has no
   ``--model``. Deepseek's model is a profile patch, not an argv flag.
3. Effort is in the PRD 4.2 table. Cursor's non-default effort is a
   suffix on the model string, so it also needs a concrete model id.
4. Runtime is in the launcher-supported set.
5. The CLI for this runtime was found, the runtime is not disabled
   (compared by runtime identity, so disabling the cancel-only ``zhipu``
   adapter does not disable the ``zcode`` worker),
   not skipped until a reset that is still in the future, and its
   breaker is closed. GLM clean-room is ``zcode-cli``. Official
   ``zcode`` does not prove that binary. ``default`` and ``native_cli``
   use the injected effective binary when the snapshot has one.
6. Headroom is above ``MIN_HEADROOM``, or the plan is full and paid
   credits may be spent.
7. The model is not already this line's reviewer.

``can_take_line`` is a fact on the snapshot (the names for which it
returned true), not a call. The same is true of ``eligible_models("work")``.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from src.server.services.autonomy.roles import canonical_provider, runtime_identity
from src.tools.auto_dispatch.pool import MIN_HEADROOM

from .effort import EffortNotLaunchable, apply_model_id, resolve_effort
from .types import FeasibilityResult

# Reason codes. Quota codes match the selector's ``no_headroom`` and
# ``plan_exhausted`` so a re-ask speaks the same vocabulary as review.
RESERVED_ROLE = "reserved_role"
NOT_ELIGIBLE = "not_eligible"
CANNOT_TAKE_LINE = "cannot_take_line"
UNKNOWN_MODEL_ID = "unknown_model_id"
UNKNOWN_EFFORT = "unknown_effort"
EFFORT_NEEDS_MODEL_ID = "effort_needs_model_id"
MODEL_NOT_LAUNCHABLE = "model_not_launchable"
UNSUPPORTED_RUNTIME = "unsupported_runtime"
CLI_NOT_FOUND = "cli_not_found"
DISABLED = "disabled"
PROVIDER_SKIPPED = "provider_skipped"
BREAKER_OPEN = "breaker_open"
NO_HEADROOM = "no_headroom"
PLAN_EXHAUSTED = "plan_exhausted"
IS_LINE_REVIEWER = "is_line_reviewer"

#: Authors the v1 work check refuses even when a policy roster lists them.
#: Q1: claude and codex stay planner and reviewer until a third reviewer exists.
RESERVED_AUTHORS = frozenset({"claude", "codex"})

#: Today's launcher, and the explicit "use the launcher as it is" value.
RUNTIME_DEFAULT = "default"
NATIVE_CLI = "native_cli"
BASE_RUNTIMES = frozenset({RUNTIME_DEFAULT, NATIVE_CLI})

#: Grammar overrides ``dispatch_launch.sh`` already distinguishes for glm,
#: and the two MiniMax auth modes the launcher is allowed to select.
GLM_RUNTIMES = frozenset({"clean-room", "official"})
MINIMAX_RUNTIMES = frozenset({"api-key", "login"})

_PROVIDER_RUNTIMES: Mapping[str, frozenset[str]] = {
    "glm": GLM_RUNTIMES,
    "minimax": MINIMAX_RUNTIMES,
}


def model_id_launchable(
    provider: str,
    model_id: str,
    runtime: str,
    *,
    grammar: str | None = None,
) -> bool:
    """Whether ``dispatch_launch.sh`` can pass this model id on this runtime.

    A ``default`` id adds nothing, which every worker already does. A
    concrete id has to match a flag the invocation actually has. GLM's
    ``--model`` exists only on the clean-room grammar: the explicit
    ``clean-room`` runtime, or a ``default`` / ``native_cli`` launch whose
    injected ``grammar`` is ``clean-room``. That grammar is the basename
    rule in ``glm_grammar`` (``zcode-cli``). With no injection the launcher
    runs official zcode, which has no ``--model``, and an explicit
    ``official`` runtime never grows one. dsh takes the deepseek model
    from a profile patch, not from argv, so a concrete deepseek id cannot
    be selected at launch.
    """
    if model_id == "default":
        return True
    canon = canonical_provider(provider)
    if canon == "deepseek":
        return False
    if canon == "glm":
        if runtime == "clean-room":
            return True
        if runtime == "official":
            return False
        return grammar == "clean-room"
    return True


def launcher_runtimes(provider: str) -> frozenset[str]:
    """Runtimes PRD 4.1 lets this provider name.

    ``default`` and ``native_cli`` are the launch script. glm may also name
    ``clean-room`` or ``official``; minimax may name ``api-key`` or
    ``login``. Docker and cloud are not in the set.
    """
    canon = canonical_provider(provider)
    return BASE_RUNTIMES | _PROVIDER_RUNTIMES.get(canon, frozenset())


@dataclass(frozen=True)
class Choice:
    """The fields of a line choice the check reads.

    A later ``LineChoice`` with the same attributes is accepted too. Blank
    ``model_id``, ``effort`` and ``runtime`` mean ``default``.
    """

    model: str
    model_id: str = "default"
    effort: str = "default"
    runtime: str = "default"


@dataclass(frozen=True)
class Snapshot:
    """Facts the dispatcher gathered before asking this function.

    Collections are roster spellings. Aliases (``zcode``, ``arkcli``,
    ``dsh``) fold onto the same provider as the choice. Two spellings of
    one CLI are one fact, and a disagreement fails closed: the latest
    skip reset still blocks, any breaker that is not closed is open, and
    headroom counts only when every spelling is above the floor. Paid
    credits cover a full plan only when every spelling says the plan is
    full and still has credits. ``provider_skips`` values are either a
    reset timestamp or ``{"until": timestamp}``, the shape
    ``gates["provider_skips"]`` already stores. A skip whose ``until`` is
    at or before ``now`` has reset and does not block. ``breakers``
    values are ``closed``, ``open`` or ``half_open``; a provider with no
    entry is closed. ``known_model_ids`` omits a provider, or maps it to
    an empty collection, when the adapter published no list.
    ``runtime_cli_found`` is the per-runtime probe, keyed
    ``provider:runtime`` (``glm:clean-room``, ``glm:default``). Values
    are the effective binary for that runtime, as the launcher will
    invoke it. GLM's official ``zcode`` and clean-room ``zcode-cli`` are
    different files. A name in ``cli_found`` proves only the grammar it
    is. ``glm:default`` and ``glm:native_cli`` are the binary
    ``NATIVE_CLI_ZCODE_BIN`` selects; a basename of ``zcode-cli`` is the
    clean-room grammar, and any other name is official. Absent that
    probe, ``default`` and ``native_cli`` are official ``zcode``.
    """

    eligible_models: Collection[str]
    can_take_line: Collection[str]
    cli_found: Collection[str]
    runtime_cli_found: Mapping[str, Collection[str]] = field(default_factory=dict)
    known_model_ids: Mapping[str, Collection[str]] = field(default_factory=dict)
    disabled_providers: Collection[str] = ()
    provider_skips: Mapping[str, Any] = field(default_factory=dict)
    now: float = 0.0
    breakers: Mapping[str, str] = field(default_factory=dict)
    headroom: Mapping[str, float] = field(default_factory=dict)
    plan_full: Mapping[str, bool] = field(default_factory=dict)
    paid_credits: Mapping[str, Any] = field(default_factory=dict)
    paid_credits_allowed: bool = False
    line_reviewers: Collection[str] = ()


def _ok() -> FeasibilityResult:
    return FeasibilityResult.ok()


def _infeasible(reason_code: str) -> FeasibilityResult:
    return FeasibilityResult.infeasible(reason_code)


def _canon(name: object) -> str:
    return canonical_provider(str(name or ""))


def _contains(names: Collection[str], canon: str) -> bool:
    return any(_canon(name) == canon for name in names if str(name or "").strip())


def _basenames(names: Collection[object]) -> set[str]:
    """Lowercased file names, so a path and a bare command compare once."""
    found: set[str] = set()
    for name in names:
        token = str(name or "").strip().lower()
        if token:
            found.add(token.rsplit("/", 1)[-1])
    return found


def _runtime_bins(snapshot: Snapshot, canon: str, runtime: str) -> set[str]:
    """Binaries recorded for this provider and runtime.

    Keys are ``provider:runtime``. The provider half folds aliases
    (``zcode:clean-room`` is ``glm``). The runtime half does not: a
    clean-room probe is not an official probe.
    """
    found: set[str] = set()
    for key, names in snapshot.runtime_cli_found.items():
        provider, sep, rt = str(key).partition(":")
        if not sep or not provider or not rt:
            continue
        if _canon(provider) != canon or rt.strip().lower() != runtime:
            continue
        found |= _basenames(names)
    return found


def _launcher_bins(snapshot: Snapshot, canon: str) -> set[str]:
    """Effective binary for ``default`` and ``native_cli``.

    Those two runtimes are today's launcher, so a probe recorded under
    either key is the binary ``NATIVE_CLI_ZCODE_BIN`` (or the zhipu
    alias) named. They do not describe the explicit ``official`` or
    ``clean-room`` override.
    """
    return _runtime_bins(snapshot, canon, RUNTIME_DEFAULT) | _runtime_bins(
        snapshot, canon, NATIVE_CLI
    )


def _glm_grammar(snapshot: Snapshot, canon: str, runtime: str) -> str:
    """Grammar ``glm_grammar`` would speak for this runtime.

    The check does not resolve symlinks. The snapshot's binary name is
    the invocation name, or the resolved target, that the dispatcher
    already classified. Every recorded launcher binary has to be
    ``zcode-cli`` before ``default`` / ``native_cli`` count as
    clean-room; one official name keeps the official grammar.
    """
    if runtime == "clean-room":
        return "clean-room"
    if runtime == "official":
        return "official"
    probed = _launcher_bins(snapshot, canon)
    if probed and probed <= {"zcode-cli"}:
        return "clean-room"
    return "official"


def _cli_available(snapshot: Snapshot, canon: str, runtime: str) -> bool:
    """Whether the binary this runtime launches was found.

    One provider usually has one executable, so ``cli_found`` is enough.
    GLM does not. ``dispatch_launch.sh`` runs official ``zcode`` unless
    ``NATIVE_CLI_ZCODE_BIN`` names another file, and only a basename of
    ``zcode-cli`` speaks the clean-room grammar. Canonicalizing both
    names to ``glm`` would treat an official install as permission to
    select clean-room. The roster spelling ``glm`` records the unset
    default probe; it does not record ``zcode-cli``. A probe under
    ``glm:default`` or ``glm:native_cli`` is that override and is the
    binary the launcher will run.
    """
    if canon != "glm":
        probed = _runtime_bins(snapshot, canon, runtime)
        return _contains(snapshot.cli_found, canon) or bool(probed)

    names = _basenames(snapshot.cli_found)
    if runtime == "clean-room":
        # The clean-room file, wherever the probe recorded it. An official
        # name under the default key does not qualify; a zcode-cli name does,
        # because that probe found the binary clean-room launches.
        found = set(names)
        found |= _runtime_bins(snapshot, canon, "clean-room")
        found |= _launcher_bins(snapshot, canon)
        return "zcode-cli" in found
    if runtime == "official":
        # The roster or official name on PATH, or any binary the official
        # probe recorded that is not zcode-cli. An executable override or a
        # wrapper (``zcode-wrapper``) speaks the official grammar, the same
        # rule ``glm_grammar`` applies, so its name need not be ``zcode``.
        if "zcode" in names or "glm" in names:
            return True
        return bool(_runtime_bins(snapshot, canon, "official") - {"zcode-cli"})
    # default / native_cli: the injected effective binary, else official zcode.
    if _launcher_bins(snapshot, canon):
        return True
    return "zcode" in names or "glm" in names


def _disabled(snapshot: Snapshot, canon: str) -> bool:
    """Whether the runtime this choice launches is disabled.

    ``disabled_providers`` binds a runtime, not a quota plan, so it is
    compared by :func:`runtime_identity`, as
    ``registry.disabled_provider_reason`` does. Disabling the cancel-only
    ``zhipu`` adapter leaves the ``zcode`` worker (``glm``) launchable;
    disabling ``glm``, ``zcode`` or ``zcode-cli`` disables it.
    """
    target = runtime_identity(canon)
    return any(
        runtime_identity(str(name)) == target
        for name in snapshot.disabled_providers
        if str(name or "").strip()
    )


def _spellings(table: Mapping[str, Any], canon: str) -> tuple[Any, ...]:
    """Every value stored under a spelling of ``canon``, in map order."""
    return tuple(value for key, value in table.items() if _canon(key) == canon)


def _latest_skip(snapshot: Snapshot, canon: str) -> float | None:
    """Latest reset among this CLI's spellings, ignoring entries with no time.

    One spelling whose reset is still ahead keeps the provider skipped
    even when another spelling has already reset. A bare timestamp and
    ``{"until": timestamp}`` are the same fact.
    """
    latest: float | None = None
    for entry in _spellings(snapshot.provider_skips, canon):
        until = _until(entry)
        # NaN compares false with every number, so it must not become the
        # latest reset and hide a spelling that is still skipped.
        if until is None or until != until:
            continue
        if latest is None or until > latest:
            latest = until
    return latest


def _breaker_blocks(snapshot: Snapshot, canon: str) -> bool:
    """True when any spelling of this CLI is present and not closed."""
    for state in _spellings(snapshot.breakers, canon):
        if str(state).strip().lower() != "closed":
            return True
    return False


def _headroom_ok(snapshot: Snapshot, canon: str) -> bool:
    """True only when every spelling shows headroom above the floor.

    A missing provider has no reading. One exhausted spelling is the
    plan, even when another spelling still looks open.
    """
    readings = _spellings(snapshot.headroom, canon)
    return bool(readings) and all(_above_min(reading) for reading in readings)


def _blank_default(value: object) -> str:
    text = str(value or "").strip().lower()
    return text or "default"


def _model_id(value: object) -> str:
    text = str(value or "").strip()
    if not text or text.lower() == "default":
        return "default"
    return text


def _choice_field(choice: Any, name: str, default: str) -> object:
    if isinstance(choice, Mapping):
        return choice.get(name, default)
    return getattr(choice, name, default)


def _known_ids(snapshot: Snapshot, canon: str) -> frozenset[str] | None:
    """Concrete model ids for this provider, or None when no list was published."""
    found = False
    ids: set[str] = set()
    for key, values in snapshot.known_model_ids.items():
        if _canon(key) != canon:
            continue
        found = True
        for item in values:
            text = str(item or "").strip()
            if text and text.lower() != "default":
                ids.add(text)
    if not found or not ids:
        return None
    return frozenset(ids)


def _until(entry: object) -> float | None:
    if isinstance(entry, Mapping):
        entry = entry.get("until")
    if isinstance(entry, bool) or not isinstance(entry, (int, float)):
        return None
    return float(entry)


def _now(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _above_min(value: object) -> bool:
    # ``bool`` is an ``int`` subclass. ``True`` must not read as full quota.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return float(value) > MIN_HEADROOM


def _has_credits(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value) > 0
    return False


def _quota(snapshot: Snapshot, canon: str) -> str | None:
    """Headroom above the floor, or a full plan that may spend paid credits.

    Paid credits are usable only when every spelling agrees the plan is
    full and still has credits. One spelling that says the plan is full
    is enough to report ``plan_exhausted`` once that agreement is missing.
    """
    if _headroom_ok(snapshot, canon):
        return None
    full_flags = _spellings(snapshot.plan_full, canon)
    credit_flags = _spellings(snapshot.paid_credits, canon)
    every_full = bool(full_flags) and all(bool(flag) for flag in full_flags)
    every_credit = bool(credit_flags) and all(_has_credits(flag) for flag in credit_flags)
    if every_full and every_credit and bool(snapshot.paid_credits_allowed):
        return None
    if any(bool(flag) for flag in full_flags):
        return PLAN_EXHAUSTED
    return NO_HEADROOM


def check_feasibility(choice: Any, snapshot: Snapshot) -> FeasibilityResult:
    """Return ok, or the first PRD 4.4 reason this choice cannot be launched.

    ``choice`` is a :class:`Choice`, a mapping, or any object with
    ``model``, ``model_id``, ``effort`` and ``runtime``. ``snapshot`` is
    the injected facts for this line; the function does not replace a
    missing fact with a live lookup.
    """
    if not isinstance(snapshot, Snapshot):
        raise TypeError("snapshot must be a Snapshot of injected facts")

    canon = _canon(_choice_field(choice, "model", ""))
    if canon in RESERVED_AUTHORS:
        return _infeasible(RESERVED_ROLE)
    if not canon or not _contains(snapshot.eligible_models, canon):
        return _infeasible(NOT_ELIGIBLE)
    if not _contains(snapshot.can_take_line, canon):
        return _infeasible(CANNOT_TAKE_LINE)

    model_id = _model_id(_choice_field(choice, "model_id", "default"))
    known = _known_ids(snapshot, canon)
    if model_id != "default" and (known is None or model_id not in known):
        return _infeasible(UNKNOWN_MODEL_ID)

    effort = _blank_default(_choice_field(choice, "effort", "default"))
    resolved = resolve_effort(canon, effort)
    if not resolved.supported:
        return _infeasible(UNKNOWN_EFFORT)
    # Cursor writes the effort into ``<model>[effort=…]``. Default means
    # today's command, which has no model string, so the suffix would be
    # discarded and the worker would not run at the chosen effort.
    if resolved.model_suffix and model_id == "default":
        return _infeasible(EFFORT_NEEDS_MODEL_ID)
    # The launcher builds the model argument with effort.apply_model_id, which
    # refuses what it cannot pass (a cursor id that already carries a
    # ``[...]`` block). The check must not call that choice feasible.
    try:
        apply_model_id(model_id, resolved)
    except EffortNotLaunchable:
        return _infeasible(MODEL_NOT_LAUNCHABLE)

    runtime = _blank_default(_choice_field(choice, "runtime", "default"))
    if runtime not in launcher_runtimes(canon):
        return _infeasible(UNSUPPORTED_RUNTIME)
    grammar = _glm_grammar(snapshot, canon, runtime) if canon == "glm" else None
    if not model_id_launchable(canon, model_id, runtime, grammar=grammar):
        return _infeasible(MODEL_NOT_LAUNCHABLE)

    if not _cli_available(snapshot, canon, runtime):
        return _infeasible(CLI_NOT_FOUND)
    if _disabled(snapshot, canon):
        return _infeasible(DISABLED)

    until = _latest_skip(snapshot, canon)
    if until is not None and until > _now(snapshot.now):
        return _infeasible(PROVIDER_SKIPPED)

    if _breaker_blocks(snapshot, canon):
        return _infeasible(BREAKER_OPEN)

    quota = _quota(snapshot, canon)
    if quota is not None:
        return _infeasible(quota)

    if _contains(snapshot.line_reviewers, canon):
        return _infeasible(IS_LINE_REVIEWER)
    return _ok()


# --- N80 §5.2 step 4: claim check for an interactive app -------------------
#
# An interactive app (Claude Code, Codex app, ...) claims a line itself. It
# has no launcher, CLI probe, runtime choice or quota port behind it, so the
# checks that read those facts do not apply and are recorded as skipped.


@dataclass(frozen=True, slots=True)
class AppClaimant:
    """The app asking to take a line, as the claim check reads it."""

    app_id: str
    can_write: bool
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class AppClaimCheck:
    """Result of :func:`check_app_claim` plus the checks it did not run."""

    result: FeasibilityResult
    skipped: tuple[str, ...]

    def audit(self) -> dict[str, object]:
        return {
            "feasibility": self.result.to_dict(),
            "skipped": {code: "interactive_app" for code in self.skipped},
        }


#: Launcher and quota checks that have no meaning for an interactive app.
APP_SKIPPED_CHECKS: tuple[str, ...] = (
    CLI_NOT_FOUND,
    MODEL_NOT_LAUNCHABLE,
    UNSUPPORTED_RUNTIME,
    NO_HEADROOM,
)

_APP_PREFIX = "app:"


def app_identity(name: object) -> str:
    """Canonical ``app:<id>`` of a registered app (bare id or ``app:`` name).

    The shared normaliser (``stage_gate.canonical_app``), the same one the
    Stage-Gate self-review bar uses.
    """
    from src.server.services.autonomy.stage_gate import canonical_app

    return canonical_app(name)


def check_app_claim(
    app: AppClaimant,
    *,
    write_scope: Sequence[str],
    line_reviewers: Iterable[str] = (),
    reserved_role: bool = False,
) -> AppClaimCheck:
    """Whether an interactive app may claim a line (N80 §5.2 step 4).

    Pure: no IO, no environment, no clock. First failure wins, in order:
    ``cannot_take_line`` (read-only app, non-blank write scope),
    ``disabled``, ``is_line_reviewer`` (identity compared in canonical
    ``app:<id>`` form on both sides), then ``reserved_role``.

    Planner ruling P5: the reserved-author rule (claude, codex) does not
    apply to interactive apps by default, so ``reserved_role`` is off and
    the code is reported as skipped. Q3 (whether it should apply) is still
    open for the owner; a caller that wants it passes ``reserved_role=True``.
    """
    skipped = APP_SKIPPED_CHECKS if reserved_role else (*APP_SKIPPED_CHECKS, RESERVED_ROLE)

    def done(result: FeasibilityResult) -> AppClaimCheck:
        return AppClaimCheck(result=result, skipped=skipped)

    if not app.can_write and any(str(path or "").strip() for path in write_scope):
        return done(_infeasible(CANNOT_TAKE_LINE))
    if not app.enabled:
        return done(_infeasible(DISABLED))
    ident = app_identity(app.app_id)
    from src.server.services.autonomy.stage_gate import reviewer_identity

    # A reviewer without the ``app:`` prefix is a native provider, never this app.
    if ident and any(reviewer_identity(name) == ident for name in line_reviewers):
        return done(_infeasible(IS_LINE_REVIEWER))
    if reserved_role and ident[len(_APP_PREFIX) :] in RESERVED_AUTHORS:
        return done(_infeasible(RESERVED_ROLE))
    return done(_ok())
