"""AST scan: every ``_block_line`` reason must be known to ``consistency.py``.

The machine parks lines with ``self._block_line(dispatch, line, reason)`` or
any name bound to that method (``park = self._block_line`` then ``park(...)``,
including a bare ``_block_line``).  Most reasons are module constants
(``REVIEW_UNAVAILABLE``) or string literals; a few are expressions whose value
comes from a caller.  The test resolves the first kind, and has a short,
explicit list for the second kind.  Anything else is "unresolved" and fails: a
new parking call site must either use a constant or extend
``NON_CONSTANT_REASONS`` deliberately, so the consistency table cannot silently
fall behind the machine again.  A renamed alias is the same call.  A reference
that is not a direct call and not a simple name binding is rejected, so it
cannot hide a reason either.

A local import, ``except`` name, or ``match`` capture shadows a module constant
of the same spelling.  Those bindings are not ``ast.Name`` stores.  The scan
must not resolve them through ``machine.py``'s module constants.

``NON_CONSTANT_REASONS`` is keyed by *call site* -- the enclosing qualname plus
the unparsed expression -- not by expression text alone.  A local variable that
happens to be named ``reason`` in some other function therefore cannot inherit
the conflict reasons listed for ``_apply_review_result``.
"""

from __future__ import annotations

import ast
import types
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeAlias

import src.tools.auto_dispatch.machine as machine
import src.tools.auto_dispatch.execution as execution_module
import src.tools.auto_dispatch.operator as operator_module
from src.tools.auto_dispatch import worker_recovery
from src.tools.auto_dispatch.liveness.consistency import (
    BLOCK_REASONS,
    FAILED_REASON_STAGES,
    KNOWN_REASONS,
    REASON_STAGES,
)

MACHINE = Path(machine.__file__).resolve()

#: ``(enclosing qualname, ast.unparse(reason expression))``.
CallSite: TypeAlias = tuple[str, str]

#: Refusal codes ``stage_gate.advance`` can return for a primary or secondary
#: review event.  The review handlers also return ``review_blocked`` as their
#: accepted BLOCK reason, so the fallback expression can produce all of these.
REVIEW_REFUSALS = frozenset(
    {
        # generic checks in ``advance``
        "unknown_event_kind",
        "already_terminal",
        "timestamp_missing",
        "stage_gate_order",
        # handlers shared by both review kinds
        "provider_required",
        "self_review_forbidden",
        "result_required",
        "invalid_result",
        "invalid_verdict",
        "verdict_result_mismatch",
        # primary/secondary-specific refusals
        "secondary_review_required",
        "reviewer_is_primary",
    }
)

#: Non-constant reason expressions at ``_block_line`` call sites, bound to the
#: function that makes the call.  Each value is the complete set the expression
#: can produce at that site.  Keep this list short and justified: it is the one
#: place the scan trusts a human instead of the AST.
NON_CONSTANT_REASONS: dict[CallSite, frozenset[str]] = {
    # N80 L4: an app lease that ran out, or that the app gave back.
    ("DispatchMachine._end_app_claim", "reason"): frozenset(
        {"app_lease_expired", "app_lease_released"}
    ),
    # ``_park_planning(dispatch, code)`` is called with these two constants.
    ("DispatchMachine._park_planning", "code"): frozenset(
        {"planner_failed", "resume_spec_missing"}
    ),
    # ``_g2_failed`` only parks immediately for ``_IMMEDIATE_G2``.
    ("DispatchMachine._g2_failed", "step.reason"): frozenset(
        {"g2_no_commands_risk_bearing", "g2_revision_moved", "preexisting_failure"}
    ),
    # N76 turns a G5 failure from ERROR into a park; ``_park_g5_failure``
    # carries the gate's own reason code.
    ("DispatchMachine._park_g5_failure", "reason"): frozenset(
        {
            "g5_not_configured",
            "g5_full_suite_failed",
            "g5_timeout",
            "g5_worktree_dirty",
        }
    ),
    # ``_PARKED_REASONS``: the drive turns any of these into a park.
    ("DispatchMachine._run_gated", "line.failure_reason or REPLAN_EXHAUSTED"): frozenset(
        {
            "replan_exhausted",
            "replan_parked",
            "replan_uncombined",
            "replan_base_unresolved",
        }
    ),
    # ``_review_once`` turns a rejected Stage-Gate transition, or a review
    # BLOCK, into a park.  The conflict reasons arrive through ``reason`` below;
    # the rest are the review refusals plus the accepted ``review_blocked``.
    (
        "DispatchMachine._apply_review_result",
        "transition.reason or REVIEW_BLOCKED",
    ): REVIEW_REFUSALS | {"review_blocked"},
    # The conflict tag selected just above the call.
    ("DispatchMachine._apply_review_result", "reason"): frozenset(
        {"reviewer_conflict_scope", "reviewer_conflict_security"}
    ),
    # ``worker_recovery.decide`` returns an Action whose ``reason`` is one of
    # these for every ``park`` branch; ``failure.reason_code`` is the fallback.
    ("DispatchMachine._recover_worker_failure", "safe_reason"): frozenset(
        {
            "author_quota_exhausted",
            "worker_auth_failed",
            "worker_confirmation_required",
            "worker_context_exhausted",
            "context_exhausted_retries_exhausted",
            "worker_turn_budget_exhausted",
            "turn_budget_exhausted_retries_exhausted",
            "worker_tool_blocked",
            "worker_rate_limited",
            "rate_limited_retries_exhausted",
            "worker_overloaded",
            "overloaded_retries_exhausted",
            "worker_timeout",
            "timeout_retries_exhausted",
            "worker_network",
            "network_retries_exhausted",
            "worker_crash",
            "worker_unknown",
            "fix_launch_failed",
        }
    ),
}

_MISSING = object()

#: ``from module import *`` binds every name.  It is not a real identifier, so it
#: cannot collide with a reason constant, and it forces every name in that
#: function to stay unresolved.
_IMPORT_STAR = "*"


@dataclass(frozen=True)
class BlockLineScan:
    """What the scan found in one source file."""

    calls: int
    expressions: tuple[str, ...]
    sites: tuple[CallSite, ...]
    reasons: frozenset[str]
    unresolved: tuple[str, ...]

    def missing_reasons(self) -> tuple[str, ...]:
        return tuple(sorted(self.reasons - KNOWN_REASONS))


def _module_string(name: str) -> str | None:
    value = getattr(machine, name, _MISSING)
    return value if isinstance(value, str) else None


def _import_bound_names(node: ast.Import | ast.ImportFrom) -> set[str]:
    """Names an import statement binds.  ``import pkg.mod`` binds ``pkg``."""
    bound: set[str] = set()
    for alias in node.names:
        if alias.name == "*":
            bound.add(_IMPORT_STAR)
            continue
        if alias.asname:
            bound.add(alias.asname)
        elif isinstance(node, ast.Import):
            bound.add(alias.name.split(".", 1)[0])
        else:
            bound.add(alias.name)
    return bound


def _pattern_bound_names(node: ast.AST) -> set[str]:
    """Bindings that never appear as ``ast.Name`` stores."""
    names: set[str] = set()
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        names.update(_import_bound_names(node))
    elif isinstance(node, ast.ExceptHandler) and node.name:
        names.add(node.name)
    elif isinstance(node, ast.MatchAs) and node.name:
        names.add(node.name)
    elif isinstance(node, ast.MatchStar) and node.name:
        names.add(node.name)
    elif isinstance(node, ast.MatchMapping) and node.rest:
        names.add(node.rest)
    return names


def _local_names(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> frozenset[str]:
    """Names bound inside ``node``; a local shadows a module constant of the same name.

    Imports and ``except`` / ``match`` captures bind a local without an
    ``ast.Name`` store.  Leaving them out resolves the name through the module
    constant and hides whatever the binding actually holds.
    """
    names: set[str] = set()
    args = node.args
    for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
        names.add(arg.arg)
    if args.vararg is not None:
        names.add(args.vararg.arg)
    if args.kwarg is not None:
        names.add(args.kwarg.arg)
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
            names.add(child.id)
        elif isinstance(child, ast.arg):
            names.add(child.arg)
        else:
            names.update(_pattern_bound_names(child))
    return frozenset(names)


def _resolve(
    node: ast.AST,
    found: set[str],
    unresolved: list[str],
    local_names: frozenset[str] = frozenset(),
) -> None:
    """Resolve one reason expression without executing it.

    Constants and module-level names are exact, unless a local binding of the
    same name shadows the module constant.  ``or`` and ``if`` are unions of
    their branches, which is conservative in the right direction for a coverage
    test.  Anything else is appended to ``unresolved``.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        found.add(node.value)
        return
    if isinstance(node, ast.Name):
        shadowed = node.id in local_names or _IMPORT_STAR in local_names
        value = None if shadowed else _module_string(node.id)
        if value is not None:
            found.add(value)
            return
        unresolved.append(ast.unparse(node))
        return
    if isinstance(node, ast.BoolOp):
        for item in node.values:
            _resolve(item, found, unresolved, local_names)
        return
    if isinstance(node, ast.IfExp):
        _resolve(node.body, found, unresolved, local_names)
        _resolve(node.orelse, found, unresolved, local_names)
        return
    unresolved.append(ast.unparse(node))


def _reason_node(call: ast.Call) -> ast.AST | None:
    if len(call.args) >= 3 and not isinstance(call.args[2], ast.Starred):
        return call.args[2]
    for keyword in call.keywords:
        if keyword.arg == "reason":
            return keyword.value
    return None


def _iter_scope_nodes(nodes: list[ast.AST]) -> list[ast.AST]:
    """Nodes in this scope, excluding nested function and class bodies.

    Defaults and decorators of a nested scope are evaluated outside it, so they
    stay here.  The nested body is that scope's own walk.
    """
    found: list[ast.AST] = []
    pending: list[ast.AST] = list(reversed(nodes))
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            pending.extend(reversed(node.decorator_list))
            pending.extend(reversed(node.args.defaults))
            pending.extend(reversed([item for item in node.args.kw_defaults if item is not None]))
            continue
        if isinstance(node, ast.Lambda):
            pending.extend(reversed(node.args.defaults))
            pending.extend(reversed([item for item in node.args.kw_defaults if item is not None]))
            continue
        if isinstance(node, ast.ClassDef):
            pending.extend(reversed(node.decorator_list))
            pending.extend(reversed(node.bases))
            pending.extend(reversed([item.value for item in node.keywords]))
            continue
        found.append(node)
        pending.extend(reversed(list(ast.iter_child_nodes(node))))
    return found


def _is_block_line_ref(node: ast.AST, sources: set[str]) -> bool:
    """True when ``node`` is exactly the ``_block_line`` method or an alias of it."""
    if isinstance(node, ast.Attribute):
        return node.attr == "_block_line" and isinstance(node.ctx, ast.Load)
    if isinstance(node, ast.Name):
        return isinstance(node.ctx, ast.Load) and (
            node.id == "_block_line" or node.id in sources
        )
    if isinstance(node, ast.NamedExpr):
        return _is_block_line_ref(node.value, sources)
    if isinstance(node, ast.IfExp):
        return _is_block_line_ref(node.body, sources) and _is_block_line_ref(
            node.orelse, sources
        )
    if isinstance(node, ast.BoolOp):
        return all(_is_block_line_ref(item, sources) for item in node.values)
    return False


def _names_bound_to_ref(
    targets: list[ast.expr], value: ast.AST, sources: set[str]
) -> set[str]:
    """Simple names this assignment gives a ``_block_line`` reference."""
    if _is_block_line_ref(value, sources):
        return {target.id for target in targets if isinstance(target, ast.Name)}
    if (
        len(targets) == 1
        and isinstance(targets[0], (ast.Tuple, ast.List))
        and isinstance(value, (ast.Tuple, ast.List))
        and len(targets[0].elts) == len(value.elts)
        and not any(isinstance(elt, ast.Starred) for elt in (*targets[0].elts, *value.elts))
    ):
        found: set[str] = set()
        for elt, item in zip(targets[0].elts, value.elts, strict=True):
            found |= _names_bound_to_ref([elt], item, sources)
        return found
    return set()


def _alias_fixpoint(nodes: list[ast.AST], inherited: set[str]) -> set[str]:
    """Names in ``nodes`` that hold ``_block_line``, plus aliases they close over.

    One ref assignment is enough.  A later rebinding to something else does not
    drop the name: a call through it might still be the method, and missing
    that call is the failure this scan exists to catch.
    """
    bindings: list[tuple[list[ast.expr], ast.AST]] = []
    for node in _iter_scope_nodes(nodes):
        if isinstance(node, ast.Assign):
            bindings.append((list(node.targets), node.value))
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            bindings.append(([node.target], node.value))
        elif isinstance(node, ast.NamedExpr):
            bindings.append(([node.target], node.value))
    aliases = set(inherited)
    changed = True
    while changed:
        changed = False
        sources = set(aliases)
        sources.add("_block_line")
        for targets, value in bindings:
            for name in _names_bound_to_ref(targets, value, sources):
                if name not in aliases:
                    aliases.add(name)
                    changed = True
    return aliases


def _directly_bound_names(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> set[str]:
    """Names this function binds itself.  Nested scopes do not shadow it."""
    names: set[str] = set()
    args = node.args
    for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
        names.add(arg.arg)
    if args.vararg is not None:
        names.add(args.vararg.arg)
    if args.kwarg is not None:
        names.add(args.kwarg.arg)
    body: list[ast.AST] = [node.body] if isinstance(node, ast.Lambda) else list(node.body)
    for child in _iter_scope_nodes(body):
        if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
            names.add(child.id)
        else:
            names.update(_pattern_bound_names(child))
    return names


def _same_node(nodes: list[ast.AST], target: ast.AST) -> bool:
    return any(node is target for node in nodes)


def _tracked_unpack_element(
    element: ast.AST,
    sequence: ast.Tuple | ast.List,
    parents: dict[int, ast.AST],
    sources: set[str],
) -> bool:
    assign = parents.get(id(sequence))
    if not isinstance(assign, ast.Assign) or assign.value is not sequence:
        return False
    if len(assign.targets) != 1 or not isinstance(assign.targets[0], (ast.Tuple, ast.List)):
        return False
    target = assign.targets[0]
    if len(target.elts) != len(sequence.elts):
        return False
    if any(isinstance(elt, ast.Starred) for elt in (*target.elts, *sequence.elts)):
        return False
    index = next(i for i, elt in enumerate(sequence.elts) if elt is element)
    return isinstance(target.elts[index], ast.Name) and _is_block_line_ref(element, sources)


def _is_tracked_use(
    node: ast.AST,
    parents: dict[int, ast.AST],
    sources: set[str],
    *,
    propagate: bool,
) -> bool:
    """A load is tracked when it is the callee, or a name alias we will follow.

    ``propagate`` is false in a class body: methods do not close over class
    assignments, so storing the method on a class name is rejected instead of
    followed.
    """
    current = node
    while True:
        parent = parents.get(id(current))
        if parent is None:
            return False
        if isinstance(parent, ast.Call) and parent.func is current:
            return _is_block_line_ref(current, sources)
        if isinstance(parent, ast.IfExp) and (
            parent.body is current or parent.orelse is current
        ):
            current = parent
            continue
        if isinstance(parent, ast.BoolOp) and _same_node(list(parent.values), current):
            current = parent
            continue
        if isinstance(parent, ast.NamedExpr) and parent.value is current:
            current = parent
            continue
        if isinstance(parent, (ast.Tuple, ast.List)) and _same_node(list(parent.elts), current):
            return _tracked_unpack_element(current, parent, parents, sources)
        if isinstance(parent, ast.Assign) and parent.value is current:
            if not propagate:
                return False
            return bool(parent.targets) and all(
                isinstance(target, ast.Name) for target in parent.targets
            ) and _is_block_line_ref(current, sources)
        if isinstance(parent, ast.AnnAssign) and parent.value is current:
            return (
                propagate
                and isinstance(parent.target, ast.Name)
                and _is_block_line_ref(current, sources)
            )
        return False


def _untracked_block_line_uses(
    nodes: list[ast.AST],
    aliases: set[str],
    parents: dict[int, ast.AST],
    *,
    propagate: bool,
) -> list[tuple[int, str]]:
    sources = set(aliases)
    sources.add("_block_line")
    found: list[tuple[int, str]] = []
    for node in _iter_scope_nodes(nodes):
        is_method = isinstance(node, ast.Attribute) and node.attr == "_block_line"
        is_alias = isinstance(node, ast.Name) and (
            node.id == "_block_line" or node.id in aliases
        )
        if not is_method and not is_alias:
            continue
        if not isinstance(getattr(node, "ctx", None), ast.Load):
            continue
        if _is_tracked_use(node, parents, sources, propagate=propagate):
            continue
        found.append((node.lineno, ast.unparse(node)))
    return found


def _is_block_line_callee(func: ast.AST, aliases: set[str]) -> bool:
    """True for a direct call of ``_block_line`` or of a name bound to it."""
    sources = set(aliases)
    sources.add("_block_line")
    return _is_block_line_ref(func, sources)


class _BlockLineVisitor(ast.NodeVisitor):
    """Collect ``_block_line`` calls with the qualname and locals of their scope.

    Name aliases are tracked per function.  A class body is not a closure, so a
    method does not inherit aliases assigned on the class.  Loads that are
    neither a call nor a simple name binding are recorded in ``escapes`` and
    fail the scan.
    """

    def __init__(self) -> None:
        self.stack: list[str] = []
        self.locals: list[frozenset[str]] = []
        self.alias_stack: list[set[str]] = [set()]
        self.hits: list[tuple[str, frozenset[str], ast.Call]] = []
        self.escapes: list[str] = []
        self._parents: dict[int, ast.AST] = {}
        self._parents_ready = False

    def visit(self, node: ast.AST) -> None:
        if not self._parents_ready:
            for parent in ast.walk(node):
                for child in ast.iter_child_nodes(parent):
                    self._parents[id(child)] = parent
            self._parents_ready = True
        super().visit(node)

    def _qualname(self) -> str:
        return ".".join(self.stack) or "<module>"

    def _record_escapes(self, nodes: list[ast.AST], *, propagate: bool) -> None:
        qualname = self._qualname()
        for line, text in _untracked_block_line_uses(
            nodes, self.alias_stack[-1], self._parents, propagate=propagate
        ):
            self.escapes.append(
                f"line {line} in {qualname}: untracked _block_line alias {text}"
            )

    def _push_function_aliases(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda
    ) -> None:
        outer = self.alias_stack[-1]
        inherited = {name for name in outer if name not in _directly_bound_names(node)}
        body: list[ast.AST] = [node.body] if isinstance(node, ast.Lambda) else list(node.body)
        self.alias_stack.append(_alias_fixpoint(body, inherited))
        self._record_escapes(body, propagate=True)

    def visit_Module(self, node: ast.Module) -> None:
        found = _alias_fixpoint(list(node.body), set())
        self.alias_stack.append(found)
        self._record_escapes(list(node.body), propagate=True)
        self.generic_visit(node)
        self.alias_stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        # Methods close over the enclosing function, not over class assignments.
        self.stack.append(node.name)
        self._record_escapes(list(node.body), propagate=False)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(node.name)
        self.locals.append(_local_names(node))
        self._push_function_aliases(node)
        self.generic_visit(node)
        self.alias_stack.pop()
        self.locals.pop()
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.locals.append(_local_names(node))
        self._push_function_aliases(node)
        self.generic_visit(node)
        self.alias_stack.pop()
        self.locals.pop()

    def visit_Call(self, node: ast.Call) -> None:
        if _is_block_line_callee(node.func, self.alias_stack[-1]):
            local_names = frozenset().union(*self.locals) if self.locals else frozenset()
            self.hits.append((self._qualname(), local_names, node))
        self.generic_visit(node)


def scan_block_line_reasons(source: Path = MACHINE) -> BlockLineScan:
    """Walk ``source`` and resolve every ``_block_line`` reason argument."""
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    visitor = _BlockLineVisitor()
    visitor.visit(tree)
    expressions: list[str] = []
    sites: list[CallSite] = []
    reasons: set[str] = set()
    unresolved: list[str] = []
    calls = 0

    for qualname, local_names, call in visitor.hits:
        calls += 1
        reason = _reason_node(call)
        if reason is None:
            unresolved.append(
                f"line {call.lineno} in {qualname}: _block_line call has no "
                "third argument or reason= keyword"
            )
            continue
        expression = ast.unparse(reason)
        site = (qualname, expression)
        expressions.append(expression)
        sites.append(site)
        listed = NON_CONSTANT_REASONS.get(site)
        if listed is not None:
            reasons.update(listed)
            continue
        before = len(unresolved)
        _resolve(reason, reasons, unresolved, local_names)
        if len(unresolved) > before:
            # Put the line number on the piece that failed, not on the call.
            unresolved[before:] = [
                f"line {call.lineno} in {qualname}: {item} in reason "
                f"expression {expression!r}"
                for item in unresolved[before:]
            ]

    unresolved.extend(visitor.escapes)

    return BlockLineScan(
        calls=calls,
        expressions=tuple(expressions),
        sites=tuple(sites),
        reasons=frozenset(reasons),
        unresolved=tuple(unresolved),
    )


def _block_line_reasons(source: Path = MACHINE) -> set[str]:
    """Backwards-compatible helper: just the resolved reason values."""
    return set(scan_block_line_reasons(source).reasons)


def test_the_scan_finds_every_block_line_call() -> None:
    scan = scan_block_line_reasons()

    # A parser regression or a changed method name must fail loudly rather than
    # pass on an empty set.
    assert scan.calls >= 20, f"only {scan.calls} _block_line calls found"
    assert len(scan.reasons) >= 15, f"only {len(scan.reasons)} reasons resolved"
    assert {"review_unavailable", "review_unparseable", "no_new_commit"} <= scan.reasons


def test_no_new_unresolved_call_site_expression_appears() -> None:
    scan = scan_block_line_reasons()

    assert not scan.unresolved, (
        "resolve these _block_line reason expressions or add them to "
        "NON_CONSTANT_REASONS:\n" + "\n".join(scan.unresolved)
    )


def test_every_block_line_reason_has_a_consistency_row() -> None:
    scan = scan_block_line_reasons()
    missing = scan.missing_reasons()

    assert not missing, (
        "consistency.py has no row for these _block_line reasons: "
        + ", ".join(missing)
    )


def test_known_non_constant_call_sites_are_still_present() -> None:
    scan = scan_block_line_reasons()
    present = set(scan.sites)
    stale = sorted(set(NON_CONSTANT_REASONS) - present)

    assert not stale, (
        "NON_CONSTANT_REASONS has call sites the machine no longer uses: "
        + ", ".join(f"{qualname}: {expression!r}" for qualname, expression in stale)
    )


def test_every_listed_non_constant_reason_has_a_consistency_row() -> None:
    missing = sorted(
        {
            reason
            for reasons in NON_CONSTANT_REASONS.values()
            for reason in reasons
            if reason not in KNOWN_REASONS
        }
    )
    assert not missing, "NON_CONSTANT_REASONS names unknown reasons: " + ", ".join(missing)


def test_the_review_transition_whitelist_covers_stage_gate_order() -> None:
    reasons = NON_CONSTANT_REASONS[
        ("DispatchMachine._apply_review_result", "transition.reason or REVIEW_BLOCKED")
    ]

    assert "stage_gate_order" in reasons
    assert "review_blocked" in reasons
    assert {"self_review_forbidden", "reviewer_is_primary"} <= reasons
    assert reasons <= KNOWN_REASONS


def test_the_review_transition_whitelist_codes_are_stage_gate_reasons() -> None:
    from src.server.services.autonomy.stage_gate import REASON_CODES

    codes = {code for code, _meaning in REASON_CODES}
    assert REVIEW_REFUSALS <= codes
    assert "review_blocked" in codes


def test_the_conflict_reason_site_is_bound_to_its_function() -> None:
    assert (
        "DispatchMachine._apply_review_result",
        "reason",
    ) in NON_CONSTANT_REASONS


def test_a_local_variable_cannot_inherit_a_listed_expression(tmp_path: Path) -> None:
    """The repro: a new ``reason`` local in another function must stay unresolved."""
    source = tmp_path / "machine.py"
    source.write_text(
        "def f(self, dispatch, line):\n"
        "    reason = str(123)\n"
        "    self._block_line(dispatch, line, reason)\n",
        encoding="utf-8",
    )

    scan = scan_block_line_reasons(source)

    assert scan.calls == 1
    assert scan.reasons == frozenset()
    assert len(scan.unresolved) == 1
    assert "reason" in scan.unresolved[0]
    assert "f" in scan.unresolved[0]


def test_a_listed_expression_in_another_function_is_unresolved(
    tmp_path: Path,
) -> None:
    source = tmp_path / "machine.py"
    source.write_text(
        "def g(self, dispatch, line, transition, REVIEW_BLOCKED):\n"
        "    self._block_line(\n"
        "        dispatch, line, transition.reason or REVIEW_BLOCKED\n"
        "    )\n",
        encoding="utf-8",
    )

    scan = scan_block_line_reasons(source)

    assert scan.calls == 1
    assert scan.reasons == frozenset()
    assert scan.unresolved
    assert all("g" in item for item in scan.unresolved)
    assert any("transition.reason" in item for item in scan.unresolved)
    assert any("REVIEW_BLOCKED" in item for item in scan.unresolved)


def test_a_renamed_alias_of_block_line_is_not_silently_ignored(tmp_path: Path) -> None:
    """The review repro: ``park = self._block_line`` then ``park(...)``."""
    source = tmp_path / "machine.py"
    source.write_text(
        "def f(self, d, l, value):\n"
        "    park = self._block_line\n"
        "    park(d, l, value.reason)\n",
        encoding="utf-8",
    )

    scan = scan_block_line_reasons(source)

    assert scan.calls == 1
    assert scan.reasons == frozenset()
    assert scan.missing_reasons() == ()
    assert len(scan.unresolved) == 1
    assert "value.reason" in scan.unresolved[0]
    assert "f" in scan.unresolved[0]


def test_a_chained_alias_of_block_line_is_scanned(tmp_path: Path) -> None:
    source = tmp_path / "machine.py"
    source.write_text(
        "def f(self, d, l, value):\n"
        "    park = self._block_line\n"
        "    other = park\n"
        "    other(d, l, value.reason)\n",
        encoding="utf-8",
    )

    scan = scan_block_line_reasons(source)

    assert scan.calls == 1
    assert any("value.reason" in item for item in scan.unresolved)


def test_a_nested_function_sees_an_enclosing_block_line_alias(tmp_path: Path) -> None:
    source = tmp_path / "machine.py"
    source.write_text(
        "def f(self, d, l, value):\n"
        "    park = self._block_line\n"
        "    def g():\n"
        "        park(d, l, value.reason)\n",
        encoding="utf-8",
    )

    scan = scan_block_line_reasons(source)

    assert scan.calls == 1
    assert any("value.reason" in item for item in scan.unresolved)
    assert any("f.g" in item for item in scan.unresolved)


def test_a_different_method_stored_under_park_is_not_a_block_line_call(
    tmp_path: Path,
) -> None:
    source = tmp_path / "machine.py"
    source.write_text(
        "def f(self, d, l, value):\n"
        "    park = self._other\n"
        "    park(d, l, value.reason)\n",
        encoding="utf-8",
    )

    scan = scan_block_line_reasons(source)

    assert scan.calls == 0
    assert scan.unresolved == ()
    assert scan.reasons == frozenset()


def test_a_non_name_store_of_block_line_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "machine.py"
    source.write_text(
        "def f(self, d, l, value):\n"
        "    self.park = self._block_line\n"
        "    self.park(d, l, value.reason)\n",
        encoding="utf-8",
    )

    scan = scan_block_line_reasons(source)

    assert scan.calls == 0
    assert scan.unresolved
    assert any("untracked" in item for item in scan.unresolved)


def test_a_local_import_shadows_the_module_reason_constant(tmp_path: Path) -> None:
    """The review repro: a local import must not resolve to the module constant."""
    samples = {
        "from-import": (
            "def f(self, d, l):\n"
            "    from os import name as REVIEW_UNAVAILABLE\n"
            "    self._block_line(d, l, REVIEW_UNAVAILABLE)\n"
        ),
        "import-as": (
            "def f(self, d, l):\n"
            "    import os as REVIEW_UNAVAILABLE\n"
            "    self._block_line(d, l, REVIEW_UNAVAILABLE)\n"
        ),
        "except": (
            "def f(self, d, l):\n"
            "    try:\n"
            "        pass\n"
            "    except Exception as REVIEW_UNAVAILABLE:\n"
            "        self._block_line(d, l, REVIEW_UNAVAILABLE)\n"
        ),
        "match": (
            "def f(self, d, l, value):\n"
            "    match value:\n"
            "        case int() as REVIEW_UNAVAILABLE:\n"
            "            self._block_line(d, l, REVIEW_UNAVAILABLE)\n"
        ),
    }
    source = tmp_path / "machine.py"
    for label, text in samples.items():
        source.write_text(text, encoding="utf-8")
        scan = scan_block_line_reasons(source)
        assert scan.calls == 1, label
        assert "review_unavailable" not in scan.reasons, label
        assert scan.missing_reasons() == (), label
        assert scan.unresolved, label
        assert any("REVIEW_UNAVAILABLE" in item for item in scan.unresolved), label


def test_a_bare_name_block_line_call_is_not_silently_ignored(tmp_path: Path) -> None:
    """The review repro: aliasing the method must not hide ``value.reason``."""
    source = tmp_path / "machine.py"
    source.write_text(
        "class M:\n"
        "    def f(self, dispatch, line, value):\n"
        "        _block_line = self._block_line\n"
        "        _block_line(dispatch, line, value.reason)\n",
        encoding="utf-8",
    )

    scan = scan_block_line_reasons(source)

    assert scan.calls == 1
    assert scan.reasons == frozenset()
    assert scan.missing_reasons() == ()
    assert len(scan.unresolved) == 1
    assert "value.reason" in scan.unresolved[0]
    assert "M.f" in scan.unresolved[0]


def test_a_bare_name_block_line_call_reports_a_missing_literal(tmp_path: Path) -> None:
    """A constant passed through the bare name still has to be in the table."""
    source = tmp_path / "machine.py"
    source.write_text(
        "class M:\n"
        "    def f(self, dispatch, line):\n"
        "        _block_line = self._block_line\n"
        "        _block_line(dispatch, line, 'brand_new_bare_reason')\n",
        encoding="utf-8",
    )

    scan = scan_block_line_reasons(source)

    assert scan.calls == 1
    assert scan.unresolved == ()
    assert scan.reasons == {"brand_new_bare_reason"}
    assert scan.missing_reasons() == ("brand_new_bare_reason",)


def test_the_scan_reports_a_new_unresolved_call_site_expression(tmp_path: Path) -> None:
    source = tmp_path / "machine.py"
    source.write_text(
        "class M:\n"
        "    def f(self, dispatch, line, value):\n"
        "        self._block_line(dispatch, line, value.reason)\n",
        encoding="utf-8",
    )

    scan = scan_block_line_reasons(source)

    assert scan.calls == 1
    assert scan.reasons == frozenset()
    assert len(scan.unresolved) == 1
    assert "value.reason" in scan.unresolved[0]
    assert "M.f" in scan.unresolved[0]


def test_the_scan_reports_a_new_reason_missing_from_the_table(tmp_path: Path) -> None:
    source = tmp_path / "machine.py"
    source.write_text(
        "class M:\n"
        "    def f(self, dispatch, line):\n"
        "        self._block_line(dispatch, line, 'brand_new_reason')\n",
        encoding="utf-8",
    )

    scan = scan_block_line_reasons(source)

    assert scan.reasons == {"brand_new_reason"}
    assert scan.missing_reasons() == ("brand_new_reason",)


def test_the_scan_resolves_or_ifexp_and_keyword_reason(tmp_path: Path) -> None:
    source = tmp_path / "machine.py"
    source.write_text(
        "class M:\n"
        "    def f(self, dispatch, line, flag):\n"
        "        self._block_line(dispatch, line, 'branch_a' if flag else 'branch_b')\n"
        "        self._block_line(dispatch, line, 'or_a' or 'or_b')\n"
        "        self._block_line(dispatch, line, reason='keyword_reason')\n",
        encoding="utf-8",
    )

    scan = scan_block_line_reasons(source)

    assert scan.calls == 3
    assert scan.unresolved == ()
    assert scan.reasons == {
        "branch_a",
        "branch_b",
        "or_a",
        "or_b",
        "keyword_reason",
    }
    assert scan.sites == (
        ("M.f", "'branch_a' if flag else 'branch_b'"),
        ("M.f", "'or_a' or 'or_b'"),
        ("M.f", "'keyword_reason'"),
    )


def test_the_scan_ignores_other_method_calls(tmp_path: Path) -> None:
    source = tmp_path / "machine.py"
    source.write_text(
        "class M:\n"
        "    def f(self, dispatch, line):\n"
        "        self._block(dispatch, line, 'not_a_park')\n",
        encoding="utf-8",
    )

    scan = scan_block_line_reasons(source)

    assert scan.calls == 0
    assert scan.reasons == frozenset()
    assert scan.unresolved == ()


def test_the_scanned_table_is_the_same_one_check_uses() -> None:
    assert KNOWN_REASONS == BLOCK_REASONS | frozenset(FAILED_REASON_STAGES)
    assert set(REASON_STAGES) == set(BLOCK_REASONS)


# ---------------------------------------------------------------------------
# The hand-written NON_CONSTANT_REASONS sets must follow their sources.
#
# Each listed set stands for reasons that reach ``_block_line`` through a
# parameter or a module collection.  The tests below derive the same set from
# its source, so a new ``_park_planning`` code, a new ``_IMMEDIATE_G2`` /
# ``_PARKED_REASONS`` member or a new ``worker_recovery`` park reason fails
# here instead of slipping past the call-site scan.
# ---------------------------------------------------------------------------


def _listed(qualname: str, expression: str) -> frozenset[str]:
    return NON_CONSTANT_REASONS[(qualname, expression)]


def _park_planning_codes(source: Path = MACHINE) -> tuple[set[str], list[str]]:
    """Resolved ``code`` arguments of every ``_park_planning`` call."""
    tree = ast.parse(source.read_text(encoding="utf-8"))
    codes: set[str] = set()
    unresolved: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name != "_park_planning":
            continue
        arg: ast.AST | None = node.args[1] if len(node.args) > 1 else None
        for keyword in node.keywords:
            if keyword.arg == "code":
                arg = keyword.value
        value: str | None = None
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            value = arg.value
        elif isinstance(arg, ast.Name):
            value = _module_string(arg.id)
        if value is None:
            unresolved.append(ast.unparse(arg) if arg is not None else "<missing>")
        else:
            codes.add(value)
    return codes, unresolved


def test_park_planning_codes_match_the_listed_set() -> None:
    codes, unresolved = _park_planning_codes()

    assert not unresolved, f"unresolved _park_planning codes: {unresolved}"
    assert codes, "no _park_planning call found"
    assert codes == _listed("DispatchMachine._park_planning", "code")


def test_park_planning_scan_sees_a_new_code(tmp_path: Path) -> None:
    source = tmp_path / "machine.py"
    source.write_text(
        "class DispatchMachine:\n"
        "    async def run(self, dispatch):\n"
        "        await self._park_planning(dispatch, 'brand_new_reason')\n",
        encoding="utf-8",
    )

    codes, _ = _park_planning_codes(source)

    assert codes == {"brand_new_reason"}
    assert codes - _listed("DispatchMachine._park_planning", "code")


def test_immediate_g2_matches_the_listed_set() -> None:
    assert set(machine._IMMEDIATE_G2) == _listed(
        "DispatchMachine._g2_failed", "step.reason"
    )


def test_parked_reasons_match_the_listed_set() -> None:
    listed = _listed(
        "DispatchMachine._run_gated", "line.failure_reason or REPLAN_EXHAUSTED"
    )
    assert set(machine._PARKED_REASONS) | {machine.REPLAN_EXHAUSTED} == listed


def _worker_recovery_park_reasons() -> set[str]:
    """Every reason ``worker_recovery.decide`` can park with.

    ``decide`` is pure, so walking every failure class, a retry count past the
    cap and both fix-round values reaches every branch.
    """
    reasons: set[str] = set()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    for failure_class in sorted(worker_recovery._FAILURE_CLASSES):
        for is_fix_round in (False, True):
            for count in range(worker_recovery._MAX_RETRIES + 2):
                attempts = {
                    failure_class: count,
                    "crash": min(count, 1),
                    "unknown": 0,
                }
                action = worker_recovery.decide(
                    failure_class, None, attempts, is_fix_round, now
                )
                if action.kind == "park":
                    assert action.reason, f"park without a reason: {action}"
                    reasons.add(action.reason)
    return reasons


def test_worker_recovery_park_reasons_are_listed() -> None:
    reasons = _worker_recovery_park_reasons()
    listed = _listed("DispatchMachine._recover_worker_failure", "safe_reason")

    assert reasons, "worker_recovery.decide never parked"
    assert not reasons - listed, (
        "worker_recovery.decide parks with reasons NON_CONSTANT_REASONS lacks: "
        + ", ".join(sorted(reasons - listed))
    )


#: ``failure_reason`` values written outside ``_block_line`` that are not a
#: park: ``serial_wait`` keeps an ASSIGNED line waiting behind a sibling.
NOT_A_PARK = frozenset({"serial_wait"})


#: Non-constant ``failure_reason`` writes, keyed by module and unparsed
#: expression. ``reason`` / ``step.reason`` carry a value the caller already
#: chose from the reasons the call-site scan covers; ``dispatch_{word}`` is
#: the operator's FAILED cancel/abort marker, consistent through the FAILED
#: status row. A new expression must be added here deliberately.
NON_CONSTANT_WRITES = frozenset(
    {
        ("src.tools.auto_dispatch.machine", "reason"),
        ("src.tools.auto_dispatch.machine", "step.reason"),
        # N76 off mode keeps main's raw G5 reason.
        ("src.tools.auto_dispatch.machine", "raw_reason"),
        ("src.tools.auto_dispatch.operator", "f'dispatch_{word}'"),
    }
)


def _assigned_failure_reasons(module: object) -> tuple[set[str], list[str]]:
    """Constant values assigned to ``*.failure_reason`` in one module.

    The second value lists every write that is not a string constant, a
    resolvable module constant, or ``None``, so a new shape cannot pass
    unseen.
    """
    source = Path(module.__file__).resolve()  # type: ignore[attr-defined]
    tree = ast.parse(source.read_text(encoding="utf-8"))
    values: set[str] = set()
    names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Attribute) and target.attr == "failure_reason"
            for target in node.targets
        ):
            continue
        value = node.value
        if isinstance(value, ast.Constant) and value.value is None:
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            values.add(value.value)
            continue
        if isinstance(value, ast.Name) and value.id.isupper():
            resolved = getattr(module, value.id, None)
            if isinstance(resolved, str):
                values.add(resolved)
                continue
        names.append(ast.unparse(value))
    return values, names


def test_direct_failure_reason_writes_have_a_consistency_row() -> None:
    """A constant written to ``failure_reason`` outside ``_block_line`` needs a row.

    ``operator.adjudicate`` parks ``two_failed_rounds`` directly; without a row
    the sweep would call a legitimate replan wait inconsistent.
    """
    missing: list[str] = []
    for module in (machine, operator_module, execution_module):
        values, others = _assigned_failure_reasons(module)
        unknown = sorted(
            expression
            for expression in others
            if (module.__name__, expression) not in NON_CONSTANT_WRITES
        )
        assert not unknown, f"{module.__name__}: unlisted failure_reason writes {unknown}"
        missing.extend(
            f"{module.__name__}: {value}"
            for value in sorted(values - KNOWN_REASONS - NOT_A_PARK)
        )

    assert not missing, "no consistency row for: " + ", ".join(missing)
    assert "two_failed_rounds" in REASON_STAGES


def test_direct_write_scan_reports_every_non_constant_shape(tmp_path: Path) -> None:
    source = tmp_path / "fake_module.py"
    source.write_text(
        "def park(line, word, step, reason):\n"
        "    line.failure_reason = f'x_{word}'\n"
        "    line.failure_reason = step.reason\n"
        "    line.failure_reason = reason\n"
        "    line.failure_reason = None\n"
        "    line.failure_reason = 'literal'\n",
        encoding="utf-8",
    )
    fake = types.SimpleNamespace(__file__=str(source), __name__="fake_module")

    values, others = _assigned_failure_reasons(fake)

    assert values == {"literal"}
    assert sorted(others) == ["f'x_{word}'", "reason", "step.reason"]

