"""Which path a granted MCP tool takes to the model: sandbox wrapper, JSON tool, or both.

Precedence, highest first: the user's per-tool override, the row's ``ptc_only``
preset, and finally ``ptc``. A non-relayable row (stdio/sse) can only take the
sandbox path, because the relay has no address to dial.

The core product has no per-vendor capability groups: every relayable
tool may take any of the three paths unless the row says otherwise.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal, get_args

from src.server.services.egress import fold_tool_name

__all__ = [
    "ALL_BINDINGS",
    "BINDINGS",
    "Binding",
    "BindingInputs",
    "BindingPlan",
    "BindingSource",
    "PRESETS",
    "PRESET_PTC_ONLY",
    "Resolved",
    "allowed_bindings",
    "inputs_from_row",
    "merge_overrides",
    "resolve_plan",
    "resolve_tool",
    "strip_disallowed_overrides",
    "validate_overrides",
]

Binding = Literal["ptc", "direct", "both"]
ALL_BINDINGS: frozenset[Binding] = frozenset(get_args(Binding))
BINDINGS: frozenset[str] = ALL_BINDINGS

PRESET_PTC_ONLY = "ptc_only"
PRESETS: frozenset[str] = frozenset({PRESET_PTC_ONLY})

BindingSource = Literal["policy", "override", "preset", "default"]


@dataclass(frozen=True)
class Resolved:
    binding: Binding
    source: BindingSource
    allowed: frozenset[Binding] = ALL_BINDINGS


@dataclass(frozen=True)
class BindingPlan:
    direct: frozenset[str] = frozenset()
    sandbox_excluded: frozenset[str] = frozenset()
    by_tool: Mapping[str, Resolved] = field(default_factory=dict)


@dataclass(frozen=True)
class BindingInputs:
    overrides: Mapping[str, str] = field(default_factory=dict)
    preset: str | None = None
    relayable: bool = True
    folded_overrides: Mapping[str, str] = field(
        init=False, repr=False, compare=False, default_factory=dict
    )

    def __post_init__(self) -> None:
        view = {fold_tool_name(k): self.overrides[k] for k in sorted(self.overrides)}
        object.__setattr__(self, "folded_overrides", view)


def inputs_from_row(row: Mapping[str, object] | None) -> BindingInputs:
    if not row:
        return BindingInputs()
    overrides = row.get("tool_binding") or {}
    return BindingInputs(
        overrides=dict(overrides) if isinstance(overrides, Mapping) else {},
        preset=row.get("binding_preset") or None,  # type: ignore[arg-type]
        relayable=row.get("transport") == "http",
    )


def allowed_bindings(
    vendor: str | None, tool: str, *, relayable: bool = True
) -> frozenset[Binding]:
    del vendor, tool
    return ALL_BINDINGS if relayable else frozenset({"ptc"})


def _ladder(tool: str, inputs: BindingInputs) -> tuple[Binding, BindingSource]:
    override = inputs.folded_overrides.get(fold_tool_name(tool))
    if override in BINDINGS:
        return override, "override"  # type: ignore[return-value]
    if inputs.preset == PRESET_PTC_ONLY:
        return "ptc", "preset"
    return "ptc", "default"


def resolve_tool(vendor: str | None, tool: str, inputs: BindingInputs) -> Resolved:
    allowed = allowed_bindings(vendor, tool, relayable=inputs.relayable)
    binding, source = _ladder(tool, inputs)
    if binding not in allowed:
        binding, source = next(iter(allowed)), "policy"
    return Resolved(binding, source, allowed)


def resolve_plan(
    vendor: str | None,
    granted: Iterable[str],
    inputs: BindingInputs,
    *,
    candidates: Iterable[str] = (),
) -> BindingPlan:
    names: set[str] = set(candidates)
    names.update(granted)
    names.update(inputs.overrides)
    by_tool = {tool: resolve_tool(vendor, tool, inputs) for tool in sorted(names)}
    direct = frozenset(t for t, r in by_tool.items() if r.binding in ("direct", "both"))
    excluded = frozenset(t for t, r in by_tool.items() if r.binding == "direct")
    return BindingPlan(direct=direct, sandbox_excluded=excluded, by_tool=by_tool)


_PATH_WORDS: dict[str, str] = {
    "ptc": "a sandbox wrapper",
    "direct": "a direct tool call",
    "both": "both at once",
}


def _refusal(
    vendor: str | None, tool: str, binding: str, relayable: bool, *, where: str
) -> str | None:
    allowed = allowed_bindings(vendor, tool, relayable=relayable)
    if binding in allowed:
        return None
    paths = " or ".join(_PATH_WORDS[b] for b in sorted(allowed))
    return f"{where} can only be bound as {paths}"


def merge_overrides(
    stored: Mapping[str, str],
    *,
    set_: Mapping[str, str] | None = None,
    unset: Iterable[str] | None = None,
) -> dict[str, str]:
    touched = {fold_tool_name(n) for n in (set_ or {})}
    touched |= {fold_tool_name(n) for n in (unset or ())}
    merged = {k: v for k, v in stored.items() if fold_tool_name(k) not in touched}
    merged.update(set_ or {})
    return merged


def validate_overrides(
    vendor: str | None,
    overrides: Mapping[str, str],
    *,
    stored: Mapping[str, str] | None = None,
    relayable: bool = True,
) -> str | None:
    stored = stored or {}
    # A tool is named by its place in the request, not its name: the key is the
    # caller's text, and a server's tool list is not what it is checked against.
    for position, (tool, binding) in enumerate(overrides.items(), start=1):
        where = f"tool_binding_set entry {position}"
        if binding not in BINDINGS:
            return f"{where}: binding must be one of {sorted(BINDINGS)}"
        if stored.get(tool) == binding:
            continue
        refusal = _refusal(vendor, tool, binding, relayable, where=where)
        if refusal:
            return refusal
    spellings: dict[str, list[str]] = {}
    for tool in overrides:
        spellings.setdefault(fold_tool_name(tool), []).append(tool)
    for names in spellings.values():
        if len(names) < 2:
            continue
        if all(stored.get(tool) == overrides[tool] for tool in names):
            continue
        return "two entries of tool_binding_set name the same tool, so only one of them can carry a binding"
    return None


def strip_disallowed_overrides(
    vendor: str | None, overrides: Mapping[str, str], relayable: bool = True
) -> dict[str, str]:
    return {
        tool: binding
        for tool, binding in overrides.items()
        if binding in allowed_bindings(vendor, tool, relayable=relayable)
    }
