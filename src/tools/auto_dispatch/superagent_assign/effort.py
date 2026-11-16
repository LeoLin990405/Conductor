"""Per-CLI effort mapping for Super-agent line assignment (N73 PRD 4.2).

The unified scale is ``default | low | medium | high | max``. This table is
the only place that turns a level into a CLI token. The launcher consumes
the token; it does not translate.

``default`` is today's launch, not a fourth strength. Checked against
``scripts/dispatch_launch.sh`` on 2026-11-12, the only effort flag any
worker receives is grok's hardcoded ``--reasoning-effort high``. Every
other provider's ``default`` adds nothing. Grok's ``default`` is therefore
``high``, so a fallback stays byte-for-byte the current grok command.

Providers whose CLI has no effort parameter (kimi, glm, deepseek, minimax)
are in the table on purpose. Every unified level resolves to ``n/a`` and
an empty argv. The strength has to be expressed by picking a model, not
by inventing a flag.

Claude and codex stay in the table for a future reviewer effort. The work
feasibility check still refuses them as authors.

``argv`` is the whole effort fragment of the resolved command, not an
extra to append: grok's ``default`` returns ``--reasoning-effort high``,
the flag today's command already carries, so a launcher that appended it
to a hardcoded flag would pass it twice. A launcher reads
:attr:`ResolvedEffort.launch_token` (``None`` means "leave today's
command alone") and puts the token in place of any flag it hardcodes.
``cli_effort`` is what the PRD 4.5 record stores; ``n/a`` is never a
launch value.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from src.server.services.autonomy.roles import canonical_provider
from src.tools.auto_dispatch.superagent_assign.types import Effort

#: Closed vocabulary a choice may name, taken from the shared ``Effort``
#: scale so the two cannot drift. Order is the scale, weakest first, with
#: ``default`` (today's flags) ahead of the named strengths.
UNIFIED_EFFORTS: tuple[str, ...] = tuple(member.value for member in Effort)

EffortStyle = Literal["none", "separate", "config", "model_suffix"]

#: CLI token stored when the provider has no effort parameter at all.
NA = "n/a"


def _levels(
    low: str | None,
    medium: str | None,
    high: str | None,
    maximum: str | None,
    default: str | None,
) -> Mapping[str, str | None]:
    by_level = {
        Effort.DEFAULT.value: default,
        Effort.LOW.value: low,
        Effort.MEDIUM.value: medium,
        Effort.HIGH.value: high,
        Effort.MAX.value: maximum,
    }
    if tuple(by_level) != UNIFIED_EFFORTS:  # pragma: no cover - import-time guard
        raise RuntimeError("effort rows must cover exactly the shared Effort scale")
    return MappingProxyType(by_level)


@dataclass(frozen=True)
class EffortRow:
    """One provider's row from PRD 4.2.

    ``levels`` maps each unified level to the CLI token, or ``None`` when
    that level adds no flag. ``None`` on a provider whose ``style`` is
    ``none`` means the whole row is ``n/a``. ``None`` on any other style
    means "today's command", which is how ``default`` stays quiet for
    every provider except grok.

    ``flag`` is the argv prefix before the token (``--effort``, ``-c``).
    Cursor has no prefix: the token is glued onto the model id.
    """

    provider: str
    cli: str
    version: str
    style: EffortStyle
    flag: tuple[str, ...]
    levels: Mapping[str, str | None]
    config_key: str = ""

    def _render(self, effort: str) -> ResolvedEffort:
        """Argv fragment and model-string suffix for a level on this row.

        The token is looked up here, so a caller cannot pass one the table
        does not hold.
        """
        token = self.levels[effort]
        if self.style == "none":
            return ResolvedEffort(
                provider=self.provider,
                effort=effort,
                cli_effort=NA,
                argv=(),
                model_suffix="",
                supported=True,
            )
        argv: tuple[str, ...] = ()
        suffix = ""
        if token is not None and self.style == "separate":
            argv = (*self.flag, token)
        elif token is not None and self.style == "config":
            argv = (*self.flag, f"{self.config_key}={token}")
        elif token is not None and self.style == "model_suffix":
            suffix = f"[effort={token}]"
        return ResolvedEffort(
            provider=self.provider,
            effort=effort,
            cli_effort=token,
            argv=argv,
            model_suffix=suffix,
            supported=True,
        )


@dataclass(frozen=True)
class ResolvedEffort:
    """What the launcher should add for one choice.

    ``argv`` is empty when nothing is added: ``default`` on every provider
    except grok, and every level on an ``n/a`` provider. ``cli_effort`` is
    the token inside that fragment, ``None`` when a provider that *has* a
    parameter is left at today's flags, and ``n/a`` when it has none.
    ``model_suffix`` is set only for cursor, whose effort lives in the
    model string rather than in argv.

    ``argv`` replaces, it does not add: grok's ``default`` carries the
    ``--reasoning-effort high`` today's command already has.
    """

    provider: str
    effort: str
    cli_effort: str | None
    argv: tuple[str, ...]
    model_suffix: str
    supported: bool

    @property
    def adds_flag(self) -> bool:
        """True when the resolved command carries an effort flag or model suffix.

        Grok's ``default`` is true here: its command carries today's flag.
        Use :attr:`changes_launch` to ask whether the command differs from
        today's.
        """
        return bool(self.argv or self.model_suffix)

    @property
    def changes_launch(self) -> bool:
        """True when the launch differs from today's command for this provider."""
        if not self.supported or self.effort == Effort.DEFAULT.value:
            return False
        return self.launch_token is not None or bool(self.model_suffix)

    @property
    def launch_token(self) -> str | None:
        """The CLI token a launcher may receive as an env value, or ``None``.

        ``None`` for ``default`` (today's command, so the launch stays
        byte-for-byte the same), for every ``n/a`` provider, for an
        unsupported resolution, and for cursor, whose effort travels only in
        the model string (:func:`apply_model_id`) so it has one carrier, not
        two. ``n/a`` itself is a record value and never reaches a launcher.
        """
        if not self.supported or self.effort == Effort.DEFAULT.value:
            return None
        if self.model_suffix:
            return None
        if self.cli_effort is None or self.cli_effort == NA:
            return None
        return self.cli_effort


def _separate(
    provider: str,
    cli: str,
    version: str,
    flag: tuple[str, ...],
    low: str,
    medium: str,
    high: str,
    maximum: str,
    default: str | None,
) -> EffortRow:
    return EffortRow(
        provider=provider,
        cli=cli,
        version=version,
        style="separate",
        flag=flag,
        levels=_levels(low, medium, high, maximum, default),
    )


def _unsupported(provider: str, cli: str, version: str) -> EffortRow:
    return EffortRow(
        provider=provider,
        cli=cli,
        version=version,
        style="none",
        flag=(),
        levels=_levels(None, None, None, None, None),
    )


# Versions are the CLIs whose ``--help`` was read on 2026-11-12 (PRD 4.2).
# They document the row; resolution does not compare them to a live binary.
EFFORT_TABLE: Mapping[str, EffortRow] = MappingProxyType(
    {
        "claude": _separate(
            "claude",
            "claude",
            "2.1.311",
            ("--effort",),
            "low",
            "medium",
            "high",
            "max",
            None,
        ),
        "codex": EffortRow(
            provider="codex",
            cli="codex",
            version="0.167.2",
            style="config",
            flag=("-c",),
            config_key="model_reasoning_effort",
            # max lands on ultra, the value this machine's codex config uses.
            # The full CLI vocabulary is still for L2 to probe.
            levels=_levels("low", "medium", "high", "ultra", None),
        ),
        # default is high because that is the flag dispatch_launch.sh passes
        # today. max collapses to high: --help did not list a higher token.
        "grok": _separate(
            "grok",
            "grok",
            "1.0.77",
            ("--reasoning-effort",),
            "low",
            "medium",
            "high",
            "high",
            "high",
        ),
        "cursor": EffortRow(
            provider="cursor",
            cli="cursor-agent",
            version="2026.11.11",
            style="model_suffix",
            flag=(),
            # No separate argv. The token is written as ``<model>[effort=high]``.
            # max collapses to high, matching the CLI's model-string form.
            levels=_levels("low", "medium", "high", "high", None),
        ),
        "stepfun": _separate(
            "stepfun",
            "step",
            "0.1.7",
            ("--thinking",),
            "low",
            "medium",
            "high",
            "max",
            None,
        ),
        "doubao": _separate(
            "doubao",
            "arkcli",
            "1.0.46",
            ("--reasoning-effort",),
            "low",
            "medium",
            "high",
            "high",
            None,
        ),
        "kimi": _unsupported("kimi", "kimi", "2.5.3"),
        "glm": _unsupported("glm", "zcode", "0.17.1"),
        "deepseek": _unsupported("deepseek", "dsh", "0.2.2-rc.1"),
        "minimax": _unsupported("minimax", "mcode", "0.7.6"),
    }
)


def _normalize_effort(effort: object) -> str:
    """Blank means ``default``. The scale is case-insensitive; unknown words stay.

    An ``Effort`` member is read by value, not by ``str()``.
    """
    if isinstance(effort, Effort):
        return effort.value
    text = str(effort or "").strip().lower()
    return text or Effort.DEFAULT.value


def _row(provider: str) -> EffortRow | None:
    return EFFORT_TABLE.get(canonical_provider(provider))


def effort_in_table(provider: str, effort: object) -> bool:
    """Whether this provider has a row and this level is on the unified scale.

    An ``n/a`` provider still contains every unified level: the row exists,
    and the resolution adds no flag. A word the scale does not name
    (``ultra``, ``xhigh``, ``minimal``) is absent even when the CLI accepts
    it. Those words are outputs of the table, not inputs.
    """
    row = _row(provider)
    if row is None:
        return False
    return _normalize_effort(effort) in row.levels


def resolve_effort(provider: str, effort: object = "default") -> ResolvedEffort:
    """Map one unified level to the CLI fragment the launcher may append.

    Unknown providers and levels come back with ``supported`` false and an
    empty fragment. Callers that launch must treat that as a refusal rather
    than dropping the effort on the floor.
    """
    canon = canonical_provider(provider)
    row = EFFORT_TABLE.get(canon)
    level = _normalize_effort(effort)
    if row is None or level not in row.levels:
        return ResolvedEffort(
            provider=canon,
            effort=level,
            cli_effort=None,
            argv=(),
            model_suffix="",
            supported=False,
        )
    return row._render(level)


class EffortNotLaunchable(ValueError):
    """The effort token has nowhere to go on the command the launcher builds.

    Cursor stores effort in the model string (``<model>[effort=high]``).
    A ``default`` model id means today's command, which passes no model
    string, so the suffix would be dropped and the launch would run at
    the CLI's own effort. That is a refusal, not a quiet success.
    """


def concrete_model_id(model_id: object) -> str | None:
    """A model id the launcher can pass, or None for ``default`` / blank."""
    token = str(model_id or "").strip()
    if not token or token.lower() == "default":
        return None
    return token


def apply_model_id(model_id: object, resolved: ResolvedEffort) -> str | None:
    """Model id to pass through, including cursor's ``[effort=…]`` suffix.

    ``default`` (and a blank id) return ``None`` when the effort adds no
    model suffix: today's commands do not grow a model argument. A cursor
    effort with no concrete model id raises :class:`EffortNotLaunchable`
    instead of returning ``None``, because returning ``None`` would launch
    without the effort the choice named. A concrete id is returned
    unchanged for every other provider, and decorated for cursor when the
    resolution carries a suffix.

    On a provider whose effort lives in the model string (cursor), an id
    that already carries a ``[...]`` block is refused at every level: the
    table, not the id, sets the effort, and ``gpt-5[effort=low]`` would
    otherwise launch at a level the table never produced.
    """
    token = concrete_model_id(model_id)
    row = EFFORT_TABLE.get(resolved.provider)
    if (
        token is not None
        and row is not None
        and row.style == "model_suffix"
        and "[" in token
    ):
        raise EffortNotLaunchable(
            f"{resolved.provider} model id {token!r} already carries a bracket "
            "block; effort comes from the table only"
        )
    if resolved.model_suffix and token is None:
        raise EffortNotLaunchable(
            f"{resolved.provider} effort {resolved.effort} is written into the "
            "model string and needs a concrete model id"
        )
    if token is None:
        return None
    if resolved.model_suffix:
        return f"{token}{resolved.model_suffix}"
    return token
