"""Classify a native Worker failure into the fixed vocabulary of W8a.

The dispatcher has to tell a rate limit from a spent quota from a full
context without reading the Worker's output itself, and it has to do that
without carrying the output anywhere: a diagnostic carries API keys, account
ids and e-mail addresses. So this module recognises a failure, names it with
a stable reason code, and keeps the text nowhere.

Three properties make that safe to build on:

* the class is one of a fixed set of names, so a caller may branch on them
  and a new one can never appear by accident;
* the priority between those classes is fixed, so a diagnostic that says two
  things at once still gets the same answer on every run;
* the result object holds no diagnostic, so the event log, the dispatch
  machine and the ``conductor workers`` view can all print it.

Providers teach the classifier their own wording through
:func:`register_failure_rule`; the generic rules below are the HTTP shape
every provider shares.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import importlib
import json
import logging
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any

from src.llms.native_cli.base import (
    NativeCliResult,
    NativeCliUnavailable,
    raise_if_auth_failure,
)

logger = logging.getLogger(__name__)


class FailureClass(str, Enum):
    """The failure classes the dispatcher may branch on.

    A ``str`` enum so the value crosses into JSON and into a shell exit table
    without a conversion step at every call site, and so a comparison against
    the plain string still holds. ``timeout`` is a local process or read
    deadline. A path that never reached the provider stays ``network``.
    ``confirmation_required`` is the CLI's own safety guard refusing a tool
    call it would have asked a human to confirm (N39): not a credential, not
    a quota and not the model's fault, and worth one retry with
    :data:`CONFIRMATION_REQUIRED_PROMPT_NOTE`.
    ``turn_budget_exhausted`` is the CLI stopping because the run used every
    turn ``--max-turns`` allowed: every request before it was answered, so it
    is neither a credential nor a quota failure. The same session is worth
    continuing, told so by :data:`TURN_BUDGET_EXHAUSTED_PROMPT_NOTE`.
    ``tool_blocked`` is the CLI's own safety gate ending a headless run on a
    tool call it would not run unattended (a shell command it could not fully
    analyse, or a write its approval policy refuses): not a credential, not a
    quota, not the model's fault. The gate is this CLI's, so the line is
    handed to another Worker rather than retried on the same one.
    """

    RATE_LIMITED = "rate_limited"
    OVERLOADED = "overloaded"
    NETWORK = "network"
    TIMEOUT = "timeout"
    QUOTA_EXHAUSTED = "quota_exhausted"
    AUTH_FAILED = "auth_failed"
    CONTEXT_EXHAUSTED = "context_exhausted"
    TURN_BUDGET_EXHAUSTED = "turn_budget_exhausted"
    CONFIRMATION_REQUIRED = "confirmation_required"
    TOOL_BLOCKED = "tool_blocked"
    CRASH = "crash"
    UNKNOWN = "unknown"


# The order the PRD fixes, highest first. It is not the enum's order: the
# enum is the vocabulary, this is the arbitration, and a diagnostic that
# matches two classes must always land on the same one.
_PRIORITY: tuple[FailureClass, ...] = (
    FailureClass.AUTH_FAILED,
    FailureClass.QUOTA_EXHAUSTED,
    # A confirmation refusal (N39) is the CLI's own guard refusing a tool call
    # nobody can confirm in a headless run. The log it is read from also holds
    # the model's text and tool output, so a real credential or quota failure
    # in the same log must still win: those need a human or a reset, and a
    # retry would only repeat them. Above everything else, so the launcher's
    # exit-77 fallback no longer turns a refusal into auth_failed.
    FailureClass.CONFIRMATION_REQUIRED,
    # A safety-gate block that is not the confirmation line above: the same
    # reasoning places it below a real credential or quota failure, and just
    # below a confirmation refusal so N39's own line keeps its own class. Its
    # end-of-run shape is a ``terminal`` rule (see :class:`FailureRule`).
    FailureClass.TOOL_BLOCKED,
    FailureClass.CONTEXT_EXHAUSTED,
    # A spent turn budget used to be read as a full context. It is its own
    # class now, just below the context: a phrase such as "max turns" may be
    # the model's own text, and a real context overflow in the same log must
    # still rotate the session. The CLI's own end-of-run marker is a
    # ``terminal`` rule and outranks every class (see :class:`FailureRule`).
    FailureClass.TURN_BUDGET_EXHAUSTED,
    FailureClass.RATE_LIMITED,
    FailureClass.OVERLOADED,
    # A local deadline shares the words "timed out" and "timeout" with a
    # transport error. It has to outrank network, or the broad network
    # patterns would keep claiming the process and read deadlines.
    FailureClass.TIMEOUT,
    FailureClass.NETWORK,
    FailureClass.CRASH,
    FailureClass.UNKNOWN,
)

# A Retry-After longer than ten minutes is not a delay the dispatcher can
# wait out. The cap keeps the number a bounded wait instead of an
# open-ended promise the recovery loop would have to special-case.
_MAX_RETRY_AFTER_SECONDS = 600

# How deep a payload is walked for its error parts. A provider envelope nests
# a couple of levels; past this the walk stops rather than chasing a shape
# nobody can predict.
_MAX_MAPPING_DEPTH = 4

# How many non-empty lines at the end of a log a terminal rule reads: the
# CLI's end event and its error line, plus the few lines a launcher appends.
_TERMINAL_TAIL_LINES = 8

# How many strings one payload may contribute. The classifier only needs the
# first error line; a giant event stream must not become the diagnostic.
_MAX_TEXT_PARTS = 64


@dataclass(frozen=True)
class FailureRule:
    """One recognition rule: the wording that means one failure class.

    ``provider`` is ``None`` for the generic rules every provider shares and
    the provider key for a rule an adapter registered. ``generic`` marks the
    built-ins, so an adapter's own wording can win the reason code without
    ever changing the class. ``requires`` names the companion words: a rule
    that lists them fires only when the text says the phrase *and* one of
    those words, which is how one phrase can mean two different classes.
    """

    failure_class: FailureClass
    reason_code: str
    patterns: tuple[str, ...] = ()
    status_codes: tuple[int, ...] = ()
    provider: str | None = None
    generic: bool = False
    requires: tuple[str, ...] = ()
    # Regular expressions matched against each line of the lowered text
    # (``re.MULTILINE``). They let a rule recognise a CLI's own line shape --
    # anchored at the start of a line -- rather than a phrase anywhere in a
    # log that also carries the model's text and tool output.
    line_patterns: tuple[str, ...] = ()
    # The CLI's own end-of-run marker: a line shape the CLI prints when it
    # stopped by itself, matched by ``line_patterns`` only and only in the
    # last :data:`_TERMINAL_TAIL_LINES` non-empty lines, where the CLI writes
    # it -- an object nested in tool output earlier in the log (a
    # sub-agent's own stop) is not this run's stop. Such a stop says
    # every request before it was answered, so any credential, quota or
    # rate-limit wording in the same log is the model's text or a tool's
    # output, and a terminal rule outranks every class. Only a spent turn
    # budget and a safety-gate block may be terminal (see
    # :func:`register_failure_rule`): a gate can only block a tool call the
    # model made, so the provider answered every request before it.
    terminal: bool = False


@dataclass(frozen=True)
class FailureClassification:
    """What the dispatcher may act on: a class, a stable code, and a delay.

    There is deliberately no field for the diagnostic. Everything downstream
    prints this object, and the diagnostic is exactly the text that must not
    be printed.
    """

    failure_class: FailureClass
    reason_code: str
    retry_after_seconds: int | None = None


# The generic rules: the HTTP shape every provider shares, before any
# provider's own wording is considered.
_GENERIC_RULES: tuple[FailureRule, ...] = (
    # The DSH CLI's own launch-time line when the provider route carries no
    # key (N26/N30): ``MISSING_CREDENTIAL: no API key``. It is our
    # environment's problem, not the model's, and only a human can fix it, so
    # it is a credential failure with its own stable code. The marker is
    # matched at the start of a line (after colour codes, a ``[tag]`` and an
    # optional ``error:`` prefix),
    # the shape the CLI prints, so the model quoting the word mid-sentence
    # does not count.
    FailureRule(
        failure_class=FailureClass.AUTH_FAILED,
        reason_code="missing_credential",
        line_patterns=(
            r"^(?:\x1b\[[0-9;]*m|[\W_])*(?:\[[a-z0-9_.-]+\]\s*)?"
            r"(?:error\s*:?\s*)?missing_credential\b",
        ),
        generic=True,
    ),
    # A credential failure outranks everything else because it is the one
    # class the recovery loop must not retry: the Worker has to be
    # re-authenticated by a human first.
    FailureRule(
        failure_class=FailureClass.AUTH_FAILED,
        reason_code="worker_auth_failed",
        status_codes=(401, 403),
        generic=True,
    ),
    # A spent quota looks like a 429 to the HTTP layer, but the two need
    # opposite recoveries: a quota waits for the reset, a rate limit backs
    # off. The wording is what separates them, which is why the quota rule
    # outranks the rate-limit rule.
    FailureRule(
        failure_class=FailureClass.QUOTA_EXHAUSTED,
        reason_code="worker_quota_exhausted",
        patterns=(
            "insufficient_quota",
            "insufficient balance",
            "insufficient credit",
            "insufficient funds",
            "quota exceeded",
            "quota exhausted",
            "exceeded your current quota",
            "exceeded your quota",
            "out of credit",
            "credit balance is too low",
            "billing hard limit",
            "balance is insufficient",
            "额度不足",
            "额度耗尽",
            "额度用完",
            "余额不足",
            "余额耗尽",
            "配额不足",
            "配额耗尽",
            "超出额度",
        ),
        status_codes=(402,),
        generic=True,
    ),
    # A full context is recoverable by rotating the session, so it has to
    # beat the rate limit: a Worker whose context ran out will keep
    # reporting the same failure however long the dispatcher backs off.
    FailureRule(
        failure_class=FailureClass.CONTEXT_EXHAUSTED,
        reason_code="worker_context_exhausted",
        patterns=(
            "context length",
            "context_length_exceeded",
            "context window",
            "maximum context",
            "max context",
            "maximum tokens",
            "too many tokens",
            "prompt is too long",
            "input length exceeds",
            "conversation is too long",
            "上下文超长",
            "上下文长度",
            "上下文窗口",
            "上下文超出",
        ),
        generic=True,
    ),
    # A spent turn budget is not a full context: the session is fine and is
    # worth continuing, so a rotation would throw its work away. The loose
    # phrases rank where the context rule's turn phrases used to.
    FailureRule(
        failure_class=FailureClass.TURN_BUDGET_EXHAUSTED,
        reason_code="worker_turn_budget_exhausted",
        patterns=(
            "maximum number of turns",
            "max turns",
            "turn limit",
            "turn budget",
            "对话轮数",
            "轮数上限",
            "超出最大轮数",
        ),
        generic=True,
    ),
    # The structured end-of-run marker a CLI writes when its turn budget ran
    # out: ``"stopReason": "max_turns"`` / ``"max_turn_requests"`` and a
    # result type of ``error_max_turns`` (zcode's transcript, grok's
    # streaming ``end`` event, claude's ``subtype``). The key must be a real
    # JSON key, not one escaped inside a string: model text and tool output
    # reach the log JSON-encoded, so their quotes carry a backslash.
    FailureRule(
        failure_class=FailureClass.TURN_BUDGET_EXHAUSTED,
        reason_code="worker_turn_budget_exhausted",
        line_patterns=(
            r'(?<!\\)"stopreason"\s*:\s*"(?:max_turns|max_turn_requests)"',
            r'(?<!\\)"(?:resulttype|subtype)"\s*:\s*"error_max_turns"',
        ),
        generic=True,
        terminal=True,
    ),
    FailureRule(
        failure_class=FailureClass.RATE_LIMITED,
        reason_code="worker_rate_limited",
        patterns=(
            "rate limit",
            "rate_limit",
            "too many requests",
            "throttl",
            "请求过于频繁",
            "触发限流",
            "限流",
        ),
        status_codes=(429,),
        generic=True,
    ),
    # 5xx is the server's problem, not the Worker's: the same Worker with
    # the same prompt is worth retrying.
    FailureRule(
        failure_class=FailureClass.OVERLOADED,
        reason_code="worker_overloaded",
        patterns=(
            "overloaded",
            "server overload",
            "capacity exceeded",
            "capacity has been reached",
            "service unavailable",
            "bad gateway",
            "internal server error",
            "upstream error",
            "服务不可用",
            "服务过载",
            "系统繁忙",
            "服务繁忙",
        ),
        # Every 5xx is the server's problem. The old range stopped at 511
        # and jumped to 529, so the codes in between -- 522 among them,
        # which is what a busy edge returns -- fell through to "crash".
        # The recovery policy treats a crash differently from an
        # overloaded server, so a gap in the range was a wrong verdict.
        status_codes=tuple(range(500, 600)),
        generic=True,
    ),
    # The runner's own deadline: the process was killed, or a read of its
    # output did not return, before the provider answered. These phrases
    # also contain "timed out" / "timeout", which the network rule still
    # uses for a path that failed in transit. This rule is the narrower
    # one and outranks network, so the local deadline is not reported as
    # a network error. "connection timed out", "etimedout" and a provider
    # line such as "Request timed out" do not match it.
    FailureRule(
        failure_class=FailureClass.TIMEOUT,
        reason_code="worker_timeout",
        patterns=(
            "cli timed out",
            "read timed out",
            "read timeout",
            "readtimeout",
            "read operation timed out",
        ),
        generic=True,
    ),
    # The connection never carried the request, so the Worker itself is
    # fine and the same turn is worth repeating. "timed out" and "timeout"
    # stay here for a transport timeout. A local process or read deadline
    # is the rule above, and it wins when both match.
    FailureRule(
        failure_class=FailureClass.NETWORK,
        reason_code="worker_network",
        patterns=(
            "connection refused",
            "connection reset",
            "connection closed",
            "connection aborted",
            "connection timed out",
            "connection error",
            "econnrefused",
            "econnreset",
            "etimedout",
            "epipe",
            "network is unreachable",
            "no route to host",
            "host is unreachable",
            "name resolution",
            "could not resolve host",
            "temporary failure in name resolution",
            "dns",
            "nxdomain",
            "ssl error",
            "ssl handshake",
            "tls handshake",
            "certificate",
            "timed out",
            "timeout",
            "网络连接",
            "连接被拒绝",
            "连接重置",
            "连接超时",
            "域名解析失败",
            "证书",
        ),
        generic=True,
    ),
)

# The reason code of a confirmation refusal, and the note a retry carries.
# The note is data: the recovery decision hands it to the caller, which adds
# it to the retried Worker's prompt.
CONFIRMATION_REQUIRED_REASON = "worker_confirmation_required"
CONFIRMATION_REQUIRED_PROMPT_NOTE = (
    "Note from the dispatcher: your previous run was stopped because the CLI"
    " refused a command that needs interactive confirmation (for example"
    " `rm -rf`, a force push or another destructive shell command). Nobody can"
    " confirm a command in this run. Do not run commands that need"
    " confirmation: delete files with `git rm` or by naming each file, and"
    " leave any destructive clean-up out of this task."
)

# The reason code of a safety-gate block (worker_tool_blocked). It carries no
# prompt note: the recovery hands the line to another Worker.
TOOL_BLOCKED_REASON = "worker_tool_blocked"

# The note a run that spent its turn budget carries into its continuation.
TURN_BUDGET_EXHAUSTED_REASON = "worker_turn_budget_exhausted"
TURN_BUDGET_EXHAUSTED_PROMPT_NOTE = (
    # Worded without the class's own phrases ("max turns", "turn budget"):
    # the continued model may repeat the note, and its echo must not decide
    # the class of the next run's failure.
    "Note from the dispatcher: your previous run stopped because the CLI's"
    " cap on turns ran out before it finished. Your earlier changes are still in"
    " the worktree. Continue from where you stopped: check `git status` and"
    " `git log` first, do not redo finished steps, and commit each finished"
    " step as you go so the next stop loses nothing."
)


def retry_prompt_note(failure_class: FailureClass | str) -> str | None:
    """The prompt note a retry of this class carries, or ``None``.

    A confirmation refusal has one: the retried Worker must be told not to
    try the refused kind of command again. A spent turn budget has one: the
    continued Worker must be told to pick up where it stopped.
    """
    value = failure_class.value if isinstance(failure_class, FailureClass) else str(failure_class)
    if value == FailureClass.CONFIRMATION_REQUIRED.value:
        return CONFIRMATION_REQUIRED_PROMPT_NOTE
    if value == FailureClass.TURN_BUDGET_EXHAUSTED.value:
        return TURN_BUDGET_EXHAUSTED_PROMPT_NOTE
    return None


# The rule that stands in for the shared credential check, which recognises
# its own markers and status shapes rather than substrings of its own.
_AUTH_TEXT_RULE = FailureRule(
    failure_class=FailureClass.AUTH_FAILED,
    reason_code="worker_auth_failed",
    generic=True,
)

# A status code only counts when something names it. A bare "500" in the
# middle of a message is far more likely to be a token count than a status.
_STATUS_LABEL_RE = re.compile(
    r"\b(?:http(?:/\d(?:\.\d)?)?(?:\s+status)?|status(?:\s+code)?|error\s+code"
    r"|error|code|resp(?:onse)?)\s*[:=]?\s*(\d{3})\b",
    re.IGNORECASE,
)
_STATUS_TRAILING_RE = re.compile(
    r"\b(\d{3})\s+(?:too many requests|rate limit(?:ed)?|bad gateway"
    r"|service unavailable|internal server error|gateway timeout|overloaded"
    r"|not found|forbidden|unauthorized)\b",
    re.IGNORECASE,
)
_RETRY_AFTER_RE = re.compile(r"retry[-_ ]?after\s*[:=]?\s*(\d+)", re.IGNORECASE)

# A payload key is matched on its bare name, so ``Retry-After``, ``retryAfter``
# and ``retry_after`` all reach the same rule.
_RETRY_AFTER_KEYS = frozenset({"retryafter", "retryafterseconds"})
_STATUS_KEYS = frozenset(
    {
        "status",
        "statuscode",
        "httpstatus",
        "httpstatuscode",
        "statusnumber",
        "errorcode",
        "code",
    }
)

# A reason code reaches the event log and the dispatch timeline, so it has to
# stay a token: a sentence there would carry whatever the provider wrote.
_REASON_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")

# The spellings one provider answers to. The classifier is called with the
# dispatch roster's spelling ("claude", "deepseek"), the registry keys the
# adapter under another ("claude-code", "deepseek-dsh"), and both must reach
# the same rule set. The canonical name is the registry's name for the
# adapter, because that is the name the adapter registers its rules under.
_PROVIDER_ALIASES: dict[str, str] = {
    "arkcli": "doubao",
    "claude": "claude-code",
    "deepseek": "deepseek-dsh",
    # The dispatch roster runs the GLM Worker under "glm", and the launch
    # script canonicalises "zcode" and "zcode-cli" to "glm" as well, so
    # "glm" is the zcode adapter's spelling and nothing else. Pointing it
    # at "zhipu" meant the zcode adapter's own wording was never found:
    # a spent credit was a crash for this Worker and an exhausted quota
    # for every other name of it. "zhipu" stays reachable under its own
    # name, and the two adapters stay apart.
    "glm": "zcode",
    "mmx": "minimax-mmx",
    "step": "stepfun",
}

# The adapter module that owns each provider's own wording. The modules are
# named, not imported: an adapter imports this module to register its rules,
# so importing one here at module level would be a cycle.
_PROVIDER_MODULES: dict[str, str] = {
    "claude-code": "src.llms.native_cli.claude",
    "codex": "src.llms.native_cli.codex",
    "cursor": "src.llms.native_cli.cursor",
    "deepseek-dsh": "src.llms.native_cli.deepseek",
    "doubao": "src.llms.native_cli.doubao",
    "grok": "src.llms.native_cli.grok",
    "kimi": "src.llms.native_cli.kimi",
    "mimo": "src.llms.native_cli.mimo",
    "opencode": "src.llms.native_cli.opencode",
    "minimax": "src.llms.native_cli.minimax",
    "minimax-mmx": "src.llms.native_cli.minimax_mmx",
    "stepfun": "src.llms.native_cli.stepfun",
    "zcode": "src.llms.native_cli.zcode",
    "zhipu": "src.llms.native_cli.zhipu",
}


def _shared_provider_rules() -> dict[str, list[FailureRule]]:
    """The one provider-rule registry, however many copies of this module run.

    Running this module as ``__main__`` would execute it a second time, while
    the package import already loaded it as a submodule. Two copies of this
    module would keep two registries, and the executable copy -- the one that
    actually classifies -- would find none of the rules the adapters
    registered. The registry therefore lives on the package, which is
    imported once.

    The command-line entry point is a module of its own
    (``src.llms.native_cli.failure_cli``) so that it is never run as
    ``__main__``; see that file for why.
    """
    package = sys.modules.get(__package__ or "")
    if package is None:
        return {}
    registry = getattr(package, "_failure_provider_rules", None)
    if registry is None:
        registry = {}
        package._failure_provider_rules = registry  # type: ignore[attr-defined]
    return registry


# The rules an adapter registered, keyed by provider.
_PROVIDER_RULES: dict[str, list[FailureRule]] = _shared_provider_rules()

# The providers whose adapter has already been imported, so a second call
# does nothing.
_LOADED_PROVIDERS: set[str] = set()


@dataclass(frozen=True)
class _Diagnostic:
    """What the classifier may look at, and nothing more.

    The text is held only for the length of one call: it is matched, counted
    and dropped. It is never stored on a result and never logged.
    """

    text: str
    status_codes: frozenset[int]
    retry_after_seconds: int | None


def _provider_key(provider: str) -> str:
    """The registry key for a provider name."""
    return (provider or "").strip().lower()


def _canonical_key(provider: str) -> str:
    """The canonical provider name an alias resolves to.

    A rule is registered under the canonical name, so every spelling of the
    provider has to resolve to it before the rules are looked up.
    """
    key = _provider_key(provider)
    return _PROVIDER_ALIASES.get(key, key)


def _load_provider_rules(provider: str) -> None:
    """Import the adapter that owns ``provider`` so its own rules are registered.

    An adapter registers its rules when it is imported, so a provider's
    wording is only present once that adapter has been imported. A fresh
    interpreter that imported nothing but this module knows no provider's
    wording until this loader has run, which is why the loader exists.

    A provider this map does not know, and an adapter that cannot be
    imported, both degrade quietly to the generic rules: one Worker's
    failure must not become an exception, and a missing rule set must not
    stop the next one from being classified.
    """
    key = _canonical_key(provider)
    if key in _LOADED_PROVIDERS:
        return
    module_name = _PROVIDER_MODULES.get(key)
    if module_name is None:
        return
    _LOADED_PROVIDERS.add(key)
    try:
        importlib.import_module(module_name)
    except Exception:
        logger.debug("no provider failure rules for %s", key, exc_info=True)


def _normalized_key(key: Any) -> str:
    """Fold a payload key to its bare name: ``Retry-After`` -> ``retryafter``."""
    return re.sub(r"[^a-z0-9]+", "", str(key).lower())


def _as_status_code(value: Any) -> int | None:
    """Read a value as an HTTP status code, or nothing."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        code = value
    elif isinstance(value, str) and value.strip().isdigit():
        code = int(value.strip())
    else:
        return None
    return code if 100 <= code <= 599 else None


def _retry_seconds(value: Any) -> int | None:
    """Read a Retry-After as whole seconds, capped; nothing else parses."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        value = int(value)
    if isinstance(value, int):
        seconds = value
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped.isdigit():
            return None
        seconds = int(stripped)
    else:
        return None
    if seconds < 0:
        return None
    return min(seconds, _MAX_RETRY_AFTER_SECONDS)


def _status_codes_from_text(text: str) -> set[int]:
    """The HTTP status codes the prose names, and only those."""
    codes: set[int] = set()
    for match in _STATUS_LABEL_RE.finditer(text):
        codes.add(int(match.group(1)))
    for match in _STATUS_TRAILING_RE.finditer(text):
        codes.add(int(match.group(1)))
    stripped = text.strip()
    if len(stripped) == 3 and stripped.isdigit():
        codes.add(int(stripped))
    return {code for code in codes if 100 <= code <= 599}


def _retry_after_from_text(text: str) -> int | None:
    """The Retry-After a header line carries, if it carries one."""
    match = _RETRY_AFTER_RE.search(text)
    if match is None:
        return None
    return _retry_seconds(match.group(1))


def _collect_value(
    value: Any,
    texts: list[str],
    statuses: set[int],
    retries: list[Any],
    depth: int,
) -> None:
    """Walk one payload value and collect the parts a rule may match."""
    if isinstance(value, Mapping):
        if depth > _MAX_MAPPING_DEPTH:
            return
        for key, item in value.items():
            _collect_item(key, item, texts, statuses, retries, depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _collect_value(item, texts, statuses, retries, depth + 1)
        return
    if isinstance(value, str):
        if value.strip() and len(texts) < _MAX_TEXT_PARTS:
            texts.append(value)


def _collect_item(
    key: Any,
    value: Any,
    texts: list[str],
    statuses: set[int],
    retries: list[Any],
    depth: int,
) -> None:
    """Read one payload entry: a delay, a status, or prose."""
    name = _normalized_key(key)
    if name in _RETRY_AFTER_KEYS:
        retries.append(value)
        return
    if name in _STATUS_KEYS:
        code = _as_status_code(value)
        if code is not None:
            statuses.add(code)
            return
    _collect_value(value, texts, statuses, retries, depth)


def _assemble(
    texts: list[str],
    statuses: set[int],
    retries: list[Any],
) -> _Diagnostic:
    """Turn the collected parts into the view the rules match against."""
    text = "\n".join(texts)
    statuses |= _status_codes_from_text(text)
    retry_after: int | None = None
    for candidate in retries:
        retry_after = _retry_seconds(candidate)
        if retry_after is not None:
            break
    if retry_after is None:
        retry_after = _retry_after_from_text(text)
    return _Diagnostic(text, frozenset(statuses), retry_after)


def _text_diagnostic(text: str) -> _Diagnostic:
    """The view of a plain diagnostic."""
    return _Diagnostic(
        text=text,
        status_codes=frozenset(_status_codes_from_text(text)),
        retry_after_seconds=_retry_after_from_text(text),
    )


def _payload_diagnostic(payload: Mapping[str, Any]) -> _Diagnostic:
    """The view of a structured payload."""
    texts: list[str] = []
    statuses: set[int] = set()
    retries: list[Any] = []
    _collect_value(payload, texts, statuses, retries, 0)
    return _assemble(texts, statuses, retries)


def _result_diagnostic(result: NativeCliResult) -> _Diagnostic:
    """The view of a finished turn.

    The turn's text is the diagnostic when it says anything. A failed turn
    sometimes explains itself only in its event stream, so the stream is
    read when the text is empty -- and only then, so a successful turn's
    events cannot invent a failure.
    """
    text = result.text or ""
    if text.strip():
        return _text_diagnostic(text)
    texts: list[str] = []
    statuses: set[int] = set()
    retries: list[Any] = []
    for event in result.raw:
        _collect_value(event, texts, statuses, retries, 0)
    return _assemble(texts, statuses, retries)


def _diagnostic(source: Any) -> _Diagnostic:
    """The view of whatever the caller handed over."""
    if source is None:
        return _Diagnostic("", frozenset(), None)
    if isinstance(source, NativeCliResult):
        return _result_diagnostic(source)
    if isinstance(source, Mapping):
        return _payload_diagnostic(source)
    if isinstance(source, str):
        return _text_diagnostic(source)
    if isinstance(source, (bytes, bytearray)):
        return _text_diagnostic(bytes(source).decode("utf-8", errors="replace"))
    try:
        return _text_diagnostic(str(source))
    except Exception:  # pragma: no cover - a hostile __str__ is not a failure
        return _Diagnostic("", frozenset(), None)


def _is_auth_failure(provider: str, text: str) -> bool:
    """Ask the shared credential check, so one marker list decides auth.

    ``raise_if_auth_failure`` already owns the curated list of sign-in
    markers and the 401/403 shapes. A second list here would drift from it
    the first time a provider's wording changed.
    """
    if not text.strip():
        return False
    try:
        raise_if_auth_failure(provider, text)
    except NativeCliUnavailable:
        return True
    return False


def _rule_matches(rule: FailureRule, lowered: str, statuses: frozenset[int]) -> bool:
    """Whether a rule recognises this diagnostic.

    A rule that names the words it requires fires only when the text says
    the phrase *and* one of those words: one phrase can mean two classes,
    and the companion word is what tells them apart. The status codes are
    not consulted for such a rule, because a rule that needs a companion
    must not fire on a bare status code.
    """
    if rule.terminal:
        tail = "\n".join(
            [line for line in lowered.splitlines() if line.strip()][-_TERMINAL_TAIL_LINES:]
        )
        return any(_line_regex(pattern).search(tail) for pattern in rule.line_patterns)
    if rule.requires:
        if not any(required in lowered for required in rule.requires):
            return False
        return any(pattern in lowered for pattern in rule.patterns)
    if any(pattern in lowered for pattern in rule.patterns):
        return True
    if any(_line_regex(pattern).search(lowered) for pattern in rule.line_patterns):
        return True
    return bool(statuses.intersection(rule.status_codes))


@functools.lru_cache(maxsize=128)
def _line_regex(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.MULTILINE)


def register_failure_rule(
    provider: str,
    failure_class: FailureClass,
    *,
    reason_code: str | None = None,
    patterns: Sequence[str] = (),
    status_codes: Sequence[int] = (),
    requires: Sequence[str] = (),
    line_patterns: Sequence[str] = (),
    terminal: bool = False,
) -> None:
    """Teach the classifier one provider's own wording.

    An adapter calls this for the phrases its CLI actually prints, so the
    generic rules do not have to guess at every vendor's phrasing. The class
    stays inside the fixed vocabulary and the reason code stays a stable
    token, because both end up in a log the dispatch machine reads.

    ``requires`` names the companion words of the rule: it fires only when
    the text says the phrase *and* one of those words. That is how one
    phrase -- "RESOURCE_EXHAUSTED" -- can mean a rate limit on one side and
    a spent quota on the other.

    ``terminal`` marks the CLI's own end-of-run line (see
    :class:`FailureRule`): it outranks every class, so it is allowed only for
    a spent turn budget or a safety-gate block (:data:`_TERMINAL_CLASSES`)
    and only as ``line_patterns``. A loose phrase could come from the model's
    own text and must never outrank a credential.

    The rules are stored under the provider's canonical name, so one
    registration covers every spelling that provider answers to.
    """
    key = _canonical_key(provider)
    if not key:
        raise ValueError("provider must name a provider")
    # A class outside the vocabulary is refused here rather than silently
    # becoming a class the dispatcher has no branch for.
    resolved = FailureClass(failure_class)
    code = reason_code or f"worker_{resolved.value}"
    if terminal and (
        resolved not in _TERMINAL_CLASSES
        or patterns
        or status_codes
        or requires
        or not line_patterns
    ):
        raise ValueError(
            "a terminal rule is a turn_budget_exhausted or tool_blocked rule"
            " with line_patterns only"
        )
    if not _REASON_CODE_RE.match(code):
        raise ValueError(
            "reason_code must be a stable token: letters, digits, '_', '.', ':' or '-'"
        )
    rule = FailureRule(
        failure_class=resolved,
        reason_code=code,
        patterns=tuple(
            str(pattern).strip().lower() for pattern in patterns if str(pattern).strip()
        ),
        status_codes=tuple(int(code) for code in status_codes),
        provider=key,
        requires=tuple(
            str(word).strip().lower() for word in requires if str(word).strip()
        ),
        # Compiled here once, so a bad expression fails at registration and
        # not in the middle of classifying a failure.
        line_patterns=tuple(
            _line_regex(str(pattern)).pattern for pattern in line_patterns if str(pattern)
        ),
        terminal=bool(terminal),
    )
    rules = _PROVIDER_RULES.setdefault(key, [])
    # The same rule twice changes no verdict; skipping it keeps the list
    # stable when a module that registers rules is executed a second time.
    if rule not in rules:
        rules.append(rule)


# The classes whose CLI end-of-run line may be a terminal rule.
_TERMINAL_CLASSES = frozenset(
    {FailureClass.TURN_BUDGET_EXHAUSTED, FailureClass.TOOL_BLOCKED}
)


# Step's safety gate (2026-11-13, R11 line-3): in a headless run with no
# approval UI it ends the run on a tool call it will not run unattended, and
# says why with one of these fixed sentences (read out of the Step CLI
# binary, ``formatUnattendedBlockReason``):
#
# * a shell command it could not fully analyse (a heredoc, a compound
#   command): "... No interactive approval is available. Use a supported
#   literal command or review it in an interactive session."
# * a call this run's policy denies: "(no interactive approval is available,
#   and this run's policy denies unattended approvals). Permit them with
#   --non-interactive-approval allow ..."
# * a write with no permission preset: "(no interactive approval is available
#   and no permission preset is configured). Permit unattended writes ..."
# * any other block: the reason, then "(no interactive approval is
#   available)." at the end of the line.
#
# The hazardous branch ("Dangerous command requires confirmation (<rule>)")
# stays N39's confirmation_required rule in ``stepfun.py``: its suffix is not
# matched here. The "continue" mode's text ("... so the call was not
# executed") is a tool result the run went on from, and is not matched
# either. Each sentence is a terminal rule in the log's last lines -- a gate
# can only block a call the model made -- and an ordinary line rule
# elsewhere, where a real credential or quota failure still outranks it.
_STEP_GATE_LINES = (
    r"no interactive approval is available\. use a supported literal command"
    r" or review it in an interactive session",
    r"\(no interactive approval is available, and this run's policy denies"
    r" unattended approvals\)",
    r"\(no interactive approval is available and no permission preset is"
    r" configured\)",
    r"\(no interactive approval is available\)\.[ \t]*$",
)
for _terminal in (True, False):
    register_failure_rule(
        "stepfun",
        FailureClass.TOOL_BLOCKED,
        reason_code=TOOL_BLOCKED_REASON,
        line_patterns=_STEP_GATE_LINES,
        terminal=_terminal,
    )
del _terminal


def failure_rules(provider: str) -> tuple[FailureRule, ...]:
    """Every rule that applies to ``provider``: the generic ones and its own.

    The generic rules come first so a reader sees the shared contract before
    the provider's additions. The classifier sorts them by priority anyway,
    so the order here is for humans, not for matching.

    The provider's adapter is imported first, because that is when its own
    rules are registered; without it a caller would only ever see the
    generic rules.
    """
    key = _canonical_key(provider)
    _load_provider_rules(key)
    return _GENERIC_RULES + tuple(_PROVIDER_RULES.get(key, ()))


def classify_failure(
    provider: str,
    result_or_diagnostic: str | Mapping[str, Any] | NativeCliResult | None = None,
    *,
    exit_code: int | None = None,
    ruleset: RuleSet | None = None,
) -> FailureClassification:
    """Name the failure a Worker hit, without carrying the diagnostic away.

    The class comes from the fixed vocabulary and the reason code is stable:
    ``worker_<class>`` unless the provider's own rule names one. The
    diagnostic is matched, counted and dropped -- it reaches neither the
    result nor the log.

    ``ruleset`` holds the rules a human approved. A built-in rule always
    wins: a learned rule is consulted only when nothing built-in knew the
    failure, so an approved rule can fill a gap and can never overturn the
    shared vocabulary. A candidate rule is shadowed: it records what it
    would have said and changes nothing.
    """
    diagnostic = _diagnostic(result_or_diagnostic)
    lowered = diagnostic.text.lower()

    # A terminal rule -- the CLI's own end-of-run line -- sorts ahead of
    # every priority: the run stopped by itself, so the credential or quota
    # wording elsewhere in its log came from the model or a tool.
    candidates: list[tuple[int, int, int, int, FailureRule]] = []
    for order, rule in enumerate(failure_rules(provider)):
        if _rule_matches(rule, lowered, diagnostic.status_codes):
            candidates.append(
                (
                    0 if rule.terminal else 1,
                    _PRIORITY.index(rule.failure_class),
                    1 if rule.generic else 0,
                    order,
                    rule,
                )
            )
    if _is_auth_failure(provider, diagnostic.text) and not any(
        candidate[4].failure_class is FailureClass.AUTH_FAILED for candidate in candidates
    ):
        candidates.append(
            (1, _PRIORITY.index(FailureClass.AUTH_FAILED), 1, -1, _AUTH_TEXT_RULE)
        )

    signature: Signature | None = None
    learned: LearnedRule | None = None
    shadow: tuple[LearnedRule, ...] = ()

    if candidates:
        candidates.sort(key=lambda candidate: candidate[:4])
        rule = candidates[0][4]
        failure_class = rule.failure_class
        reason_code = rule.reason_code
    else:
        # Nothing built-in knew this failure. Only here may a learned rule
        # speak, and only an approved one.
        if ruleset is not None:
            # The signature is taken from the diagnostic itself and not from
            # the text it was flattened into: a mapping loses the key of its
            # values on the way, and the redaction needs that key.
            signature = failure_signature(provider, result_or_diagnostic)
            learned = ruleset.approved_rule(
                provider, signature.signature_id, signature.normalized_text
            )
            shadow = ruleset.shadow_rules(
                provider, signature.signature_id, signature.normalized_text
            )
        if learned is not None:
            failure_class = learned.failure_class
            reason_code = f"learned_{learned.failure_class.value}"
        elif exit_code:
            # The process died without saying why. That is a crash, and it is
            # worth one more turn with a fresh session.
            failure_class = FailureClass.CRASH
            reason_code = "worker_crash"
        else:
            failure_class = FailureClass.UNKNOWN
            reason_code = "worker_unknown"

    # The log line carries the provider, the class and the code -- and
    # nothing that came out of the Worker's output.
    logger.info(
        "worker failure classified: provider=%s class=%s reason_code=%s",
        _provider_key(provider),
        failure_class.value,
        reason_code,
    )
    # A shadowed candidate says what it would have said. It says nothing that
    # changes the classification, and it says it about the signature rather
    # than about the diagnostic.
    for rule in shadow:
        # A rule that was downgraded says why it only shadows: the code
        # names the approval metadata it did not carry.
        downgraded = (
            f" reason_code=rule_missing_approval"
            f" missing={','.join(rule.missing_approval)}"
            if rule.missing_approval
            else ""
        )
        logger.info(
            "worker failure shadow prediction: provider=%s signature_id=%s"
            " predicted_class=%s status=%s%s",
            _provider_key(provider),
            signature.signature_id if signature is not None else "",
            rule.failure_class.value,
            rule.effective_status,
            downgraded,
        )
    return FailureClassification(
        failure_class=failure_class,
        reason_code=reason_code,
        retry_after_seconds=diagnostic.retry_after_seconds,
    )


# ---------------------------------------------------------------------------
# The redaction of a credential
#
# One function does it, and everything that has to take a credential out of
# a value calls it: the signature of a failure, and the handover note the
# session manager writes. A second implementation would drift from the first
# one, and what drifts first is the rule that decides what a credential is.
#
# The value goes and the key stays, so the text still says what the value
# was; the normalisation turns the marker into the placeholder that names it.
#
# A number is not a secret by itself: a credential whose value happens to be
# made of digits is still a credential, and it goes like any other one. The
# only numbers that stay are the counts of tokens, and only under the seven
# key names that name a count -- a bare "token" is not one of them, because
# a number under a bare "token" cannot be told apart from a credential.
#
# Three shapes have no key the pair rule can see, or the key and the value
# are not on one line. A PEM private-key block is the key, markers and
# body together. A URL's userinfo (``scheme://user:password@host``) is a
# credential whether or not a key names it. A unified diff puts the value
# on the next added line, and the ``+`` in front of that line is not the
# value -- reading it as one leaves the secret on the rest of the line.
# ---------------------------------------------------------------------------

# The marker a redacted value leaves behind. One string, so a caller may
# look for it.
_REDACTED = "[redacted]"

# The key names that name a credential.
#
# The name is judged as a whole, so "api_key", "apiKey" and "api-key" all
# reach the same rule, and so does "my_api_key": the name holds the word,
# whatever else it holds. The compound names carry a ``[ _-]?`` separator,
# so their space-separated legacy spellings -- "api key", "access key",
# "auth code", "session key", "account id", "private key",
# "client secret" -- are the same names: a mapping under "api key" is a
# mapping under "api_key". The charset around the compound stays tight --
# letters, digits, an underscore, a dot and a hyphen -- so a whole
# sentence cannot borrow its way into the rule through the prefix or the
# suffix. A quote is not part of a name, so a quoted key is read as the
# name it holds.
#
# The count keys are not decided here: the number under "max_tokens" is a
# count and not a secret, and the names that say so are the seven of
# _COUNT_KEY_NAMES below.
#
# "credential" is in the list on purpose, and it stays. The pattern this
# rule was given does not name it, and a key called "credential" is one of
# the oldest names a secret goes by -- it was redacted before the rule was
# written down, and a rule that is written down must not quietly stop
# redacting it.
_CRED_KEY = re.compile(
    r"(?i)^[a-z0-9_.-]*?"
    r"(api[ _-]?key|token|secret|password|passwd|authorization|auth[ _-]?code"
    r"|bearer|cookie|session[ _-]?key|account[ _-]?id|access[ _-]?key"
    r"|private[ _-]?key|client[ _-]?secret|credential)"
    r"[a-z0-9_.-]*$"
)

# The keys that name an account. They say whose account this is, so they
# are taken out as well.
_ACCOUNT_KEYS = (
    r"acct|account|account[-_ ]?id|user[-_ ]?id|uid|customer[-_ ]?id"
    r"|sub[-_ ]?id|org[-_ ]?id|project[-_ ]?id"
)

# The shape a credential takes when no key names it: the prefixes the
# providers hand out.
#
# The edges are **lookarounds**, not ``\b``: an underscore is a word
# character in a regular expression, so a word boundary next to one does not
# exist -- ``task_ghp_abc123def456`` would slip through a ``\b`` and be
# written to the file. A lookaround on the letters and the digits alone
# treats the underscore as the edge it is.
_CREDENTIAL_SHAPE_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?:sk|pk|rk|xox[baprs]|gh[pousr]|AKIA)[-_][A-Za-z0-9_\-]{8,}"
    r"(?![A-Za-z0-9])"
)

# The one shape that needs a rule of its own: an AWS access key id.
#
# It has no separator in it -- the prefix and the sixteen characters that
# follow it are one token -- so the shape rule above, which needs the
# separator, does not see it. And nothing names it either: a key id that
# sits in a path, in a file name or in a message has no key in front of it,
# so the key/value rule does not see it either. Two rules miss it, so it
# gets a rule of its own: the form is fixed, so the rule can be fixed too.
#
# Only the two prefixes AWS hands out are here, the sixteen characters are
# upper case, and a git hash is lower case -- so a commit hash and a file
# path are not touched. The edges are lookarounds and not ``\b``, for the
# reason the shape rule above gives: an underscore next to the key is an
# edge, and a word boundary does not see it.
_AWS_KEY_RE = re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Za-z0-9])")

# A key and the separator that follows it.
#
# The key is a candidate: the characters a key name is made of, and
# nothing else. A candidate stops at anything else -- the "/" of a branch
# name is not part of a key, so the key in "feature/api_key=..." is read
# as "api_key" and the branch keeps its shape. What the candidate means
# is decided afterwards, by _CRED_KEY.
#
# The value is not part of the match: it is read after the separator, so
# that a pair which names no credential cannot hide the credential that
# follows it.
#
# A unified diff can put the separator on the next added line
# (``+"api_key"`` / ``+: "secret"``). The ``+`` of that line sits between
# the key and the ``:`` or ``=``. It is part of the separator, and only
# when a newline sits in front of it, so a same-line ``key=+secret`` still
# reads the ``+`` as the start of the value.
#
# The key starts at the start of a run of key characters, and the
# lookbehind says so. Without it every position inside a long run is a
# fresh start that walks the rest of the run before failing at the
# separator, which is quadratic: a 40 kB word took half a minute. A key
# always ends where its run ends -- nothing that may follow a key is a key
# character -- so a start inside a run can never match where the run's own
# start failed, and the anchor changes no result.
_CREDENTIAL_PAIR_RE = re.compile(
    r"(?<![A-Za-z0-9_.\-])(?P<key>[A-Za-z0-9_.\-]+)(?P<quote>[\"']?)"
    r"(?P<sep>\s*(?:(?<=[\n\r])\+[ \t]*)?[:=]\s*)"
)

# The value of a credential: a quoted string -- an escaped quote inside it
# belongs to the value -- or a bare word that runs up to the next
# delimiter. The delimiter set stops the value before the closing brace of
# a JSON object, so the text around the value comes out as it went in:
# '{"api_key": 123}' becomes '{"api_key": [redacted]}'.
_CREDENTIAL_VALUE_RE = re.compile(
    r"\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|[^\s,;&\"'})\]]+"
)

# A value that already moved onto the next line. A comma, a semicolon or
# an ampersand ends a same-line pair, and on this line it is part of the
# password: the line holds the value and nothing after it. Stopping at one
# of them leaves the rest of the secret on the line.
_CREDENTIAL_DIFF_VALUE_RE = re.compile(
    r"\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|[^\r\n]+"
)

# The Authorization header owns its own rule, so the pair rule leaves it
# alone. "Owns" means the key **ends** with the word: "Authorization" and
# "X-Authorization" are the header, while "authorization_token" and
# "authorization_code" are credentials of their own and the pair rule has
# to take them. The two rules have to agree on this, or one of them leaves
# a secret behind.
_AUTHORIZATION_KEY_RE = re.compile(r"authorization$", re.IGNORECASE)

# The Authorization header: the scheme may be any of them, and the value is
# what follows it -- everything up to the end of the line, or up to the next
# quote. The marker counts as a scheme too, so the rule still fires when the
# pair rule above already took the scheme word.
#
# The key is anything that ends with the word, the same way
# _AUTHORIZATION_KEY_RE above decides it -- so the header rule really fires
# on every key the pair rule leaves to it. The key may be quoted, and so may
# the value: a header that arrives as JSON is a header too. The value stops
# before the closing brace of that JSON, so the object around it comes out
# as it went in.
#
# The optional group is the ``+`` of the next added diff line. It may sit
# in front of the separator (``+"authorization"`` / ``+: Bearer secret``)
# or in front of the value. It is only that marker when a newline sits in
# front of it, so a same-line value that starts with "+"
# ("authorization=+secret") stays the value. Without the group the marker
# is read as the separator or the value, and the secret stays.
_ADDED_DIFF_LINE = r"(?:(?<=[\n\r])\+[ \t]*)?"
_AUTHORIZATION_RE = re.compile(
    r"[\"']?authorization[\"']?\s*"
    + _ADDED_DIFF_LINE
    + r"[:=]\s*"
    + _ADDED_DIFF_LINE
    + r"(?:(?P<scheme>[A-Za-z][A-Za-z0-9._~+-]*|\[redacted\])\s+)?"
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^\r\n\"'}\])]+)",
    re.IGNORECASE,
)

# A bearer token on its own. The character set is the one a bearer token
# really has: it may hold a "+" and a "/". The edge in front of the word is
# a lookaround and not ``\b``: an underscore is a word character, so a word
# boundary next to one does not exist, and ``x_Bearer …`` would slip through.
_BEARER_RE = re.compile(
    r"(?<![A-Za-z0-9])Bearer\s+(?P<value>[A-Za-z0-9._~+/=-]+)", re.IGNORECASE
)

# The key form of an account id ("uid=...") and the bare form
# ("acct_998877"). The keyword needs a separator, so a class name that
# merely starts with "Account" is left alone.
_ACCOUNT_KEY_RE = re.compile(
    r"[\"']?(?<![A-Za-z0-9])(?P<key>" + _ACCOUNT_KEYS + r")(?![A-Za-z0-9])"
    r"[\"']?\s*"
    + _ADDED_DIFF_LINE
    + r"[:=]\s*"
    + _ADDED_DIFF_LINE
    + r"(?P<value>\"[^\"]*\"|'[^']*'|[^\s\"]+)",
    re.IGNORECASE,
)
_ACCOUNT_SHAPE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:acct|account|uid)[-_][A-Za-z0-9]{4,}(?![A-Za-z0-9])",
    re.IGNORECASE,
)

# The space-separated legacy spellings of the credential keys, in a text.
#
# The pair rule cannot carry them: its key candidate is the characters a
# key name is made of, and a candidate that could cross a space would
# swallow the words in front of the key -- "error 500 api_key=x" would
# reach the rule as one candidate "error 500 api_key", name no credential
# after its prefix charset failed on the space, and leave the key behind.
# So the spaced names match by their own rule, the way the account rule
# above matches its own spaced spellings: the compound is named in full,
# with the space mandatory -- the underscore and hyphen forms are the pair
# rule's, and a rule that reached them here would match again under a
# value this rule has already taken out. A boundary keeps "api keyboard"
# out. The value is not part of the match: on a diff line it is the rest
# of that line, spaces included, and on the same line it is one word.
# Reading only the first word leaves the rest of an unquoted diff value
# in the clear (``+api key:`` / ``+  two words secret``).
# _CRED_KEY judges the same names for a mapping, where the key arrives
# whole; this rule is the text half of that.
_CRED_SPACED_KEY_RE = re.compile(
    r"[\"']?(?<![A-Za-z0-9])"
    r"(?P<key>api key|access key|auth code|session key|account id"
    r"|private key|client secret)"
    r"(?![A-Za-z0-9])[\"']?"
    r"(?P<sep>\s*"
    + _ADDED_DIFF_LINE
    + r"[:=]\s*)",
    re.IGNORECASE,
)
# Same-line spaced value: a quoted string, or one word. A comma ends a
# same-line pair for the pair rule; here the older word class stays, so
# ``api key=+plus-secret`` still takes the leading ``+`` as the value.
_SPACED_VALUE_RE = re.compile(r"\"[^\"]*\"|'[^']*'|[^\s\"]+")

# A PEM private-key block. The label is the RFC 7468 form: "PRIVATE KEY",
# with an optional type word in front ("RSA", "OPENSSH", "ENCRYPTED").
# A certificate and a public key do not carry that label. The body between
# the labels is the key, whether or not a diff prefix sits on those lines.
_PEM_BEGIN_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_PEM_END_RE = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY-----")

# ``scheme://user:password@host``. The scheme is a letter plus at most 31
# scheme characters: an open-ended run scans every start of a long
# scheme-looking word to its end before failing at "://", which is
# quadratic on text a caller chooses. The authority is what follows, up to
# ``/ ? #`` or whitespace -- the same end a URL parser uses -- and also up
# to a double quote, a square or curly bracket or an angle bracket. Those
# are not userinfo. A comma, a parenthesis and an apostrophe are, in the
# user name and in the password (``db,user:secret``, ``pa,ss``). Ending
# the authority on one of them before the ``@`` leaves the password in
# place. They still end the host, so a later email ``@`` after the host
# is not read as userinfo. A minified diagnostic is cut by the quote
# before that email (``{"url":"https://example.com","email":"a@b.com"}``).
# A password may itself hold an ``@`` (``user:pa@ss@host``); the userinfo
# then runs to the last ``@`` in that authority. A colon has to sit in
# front of that ``@``, so ``scheme://user@host`` is left alone. Whether
# the colon is a port is decided only after that ``@`` is found: a dotted
# name and a numeric password prefix (``john.doe:123,secret@host``) are
# still a password. The scan is one forward pass. A cap in front of the
# colon would hide a long user name's password, and a rejected scheme
# advances past the span already walked so the pass stays linear.
_URL_SCHEME_MAX = 32
_URL_SCHEME_START = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
_URL_SCHEME_CHARS = _URL_SCHEME_START | frozenset("0123456789+.-")
_URL_AUTHORITY_END = frozenset("/?# \t\r\n\"[]{}<>")
_URL_USERINFO_MARKS = frozenset(",()'")
_URL_USERINFO_FORBIDDEN = frozenset("\"[]{}<>")

# An unencoded ``#``, quote, square bracket or blank inside a password ends
# the authority a URL parser reads, before the ``@``, and leaves the
# password in the clear (``postgres://u:p#ss@db``, ``postgres://u:p w@db``).
# When the authority stops on one of these with a ``user:`` in front of it
# and no ``@`` yet, the password is read on to the last ``@`` that a host
# follows -- on that line, and before the next ``://``. A run of digits
# after the colon is a port (``https://host:8080 ...``), so a URL without a
# password keeps its shape. The region ends at the next newline or
# ``://``, so two URLs never scan the same text and the pass stays linear.
_URL_PASSWORD_SPILL = frozenset("#\"'[] \t")
_URL_HOST_START = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789["
)
_URL_SPILL_END_RE = re.compile(r"[\r\n]|://")


def _is_a_number(value: Any) -> bool:
    """Whether a value is only a number.

    A count is a number, and a credential that happens to be made of digits
    is a number too: what tells the two apart is the key the value sits
    under, and not the value itself.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return value.is_integer()
    if isinstance(value, str):
        return value.strip().isdigit()
    return False


def _looks_like_a_count(value: str) -> bool:
    """Whether a text is only a number.

    The trailing punctuation of the sentence is not part of the count, so it
    is read apart before the number is judged.
    """
    stripped = value.strip().strip("\"'").rstrip(".,;:!?)]}'\"")
    return _is_a_number(stripped)


#: The key names whose number is a count and not a secret. Only these
#: seven names: a bare "token" is not one of them, because a number under a
#: bare "token" cannot be told apart from a credential.
_COUNT_KEY_NAMES = frozenset(
    {
        "tokens",
        "token_count",
        "max_tokens",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "context_window_tokens",
    }
)


def _is_count_key(key: str) -> bool:
    """Whether the key names a count of tokens.

    The name is judged whole and without regard to case: "max_tokens" and
    "MAX_TOKENS" are the same key, and "my_max_tokens" is another one -- a
    key that is not one of the seven names is a credential key.
    """
    return key.strip().strip("\"'").lower() in _COUNT_KEY_NAMES


def _is_credential_key(key: str) -> bool:
    """Whether the key names a credential.

    The name is judged whole, by _CRED_KEY: "api_key" and "my_api_key" are
    both credential keys, and "status" is neither. A name that holds no
    credential word is not a credential key, whatever its value is.

    The seven count names are judged by their value and not here: the number
    under "max_tokens" is a count and not a secret, and any other value under
    it is a value and goes -- the same as under any other key.
    """
    candidate = key.strip().strip("\"'")
    if not candidate:
        return False
    return _CRED_KEY.fullmatch(candidate) is not None


def _redact_value(match: re.Match[str]) -> str:
    """Swap the value a match found for the redaction marker.

    The key stays: the text still says what the value was, and the
    normalisation turns the marker into the placeholder that names it.

    A number under one of the count keys is the one value that stays. The
    very same number under any other key is the value of that key, and a
    value goes -- a purely numeric API key is still an API key.
    """
    value = match.group("value")
    if not value:
        return match.group(0)
    # Judge the text at the value, not only the captured group. The header
    # rule stops the value at "]" and "}", so a value that is already the
    # marker is captured without its closing bracket. Replacing that
    # fragment leaves the bracket behind ("[redacted]]"). The text still
    # starts with the marker, and a marker is left alone: redacting it
    # again would swallow whatever came after it.
    # ``startswith`` at an offset: slicing the rest of the text for every
    # match copies it each time, which is quadratic on a run of matches.
    if match.string.startswith(_REDACTED, match.start("value")):
        return match.group(0)
    key = (match.groupdict().get("key") or "").strip("\"' ")
    if _is_count_key(key) and _looks_like_a_count(value):
        # A number under a count key is a count, not a secret.
        return match.group(0)
    prefix = match.group(0)[: match.start("value") - match.start()]
    return f"{prefix}{_REDACTED}"


def _redact_account_value(match: re.Match[str]) -> str:
    """Swap the value of an account id for the redaction marker.

    An account id is an identifier and not a count, so it goes whatever it
    looks like. A value that is already the marker is left alone, for the
    same reason as in :func:`_redact_value`.
    """
    value = match.group("value")
    if value.startswith(_REDACTED):
        return match.group(0)
    prefix = match.group(0)[: match.start("value") - match.start()]
    return f"{prefix}{_REDACTED}"


def _diff_value_start(text: str, separator: str, value_start: int) -> int:
    """Where the value starts when a diff put it on the next added line.

    The separator has already taken the newline, so the next character is
    the ``+`` git writes in column one, plus the indent after it. That
    marker is not the value: a reader that stops on it leaves the secret
    on the rest of the line. A ``+`` that was not across a newline belongs
    to the value (``key=+secret``), so this leaves it where it is.
    """
    if "\n" not in separator and "\r" not in separator:
        return value_start
    if value_start >= len(text) or text[value_start] != "+":
        return value_start
    value_start += 1
    while value_start < len(text) and text[value_start] in " \t":
        value_start += 1
    return value_start


def _redact_pem_blocks(text: str) -> str:
    """Replace each PEM private-key block with the redaction marker.

    The block is one secret: the label and the base64 body go together, and
    a window that kept the body would keep the key. An added diff puts
    ``+`` on the body lines and on the END line; those prefixes sit between
    the two labels, so they go with the block. A BEGIN with no END redacts
    the rest of the text, which is the only place the body can still be. A
    certificate or a public key does not carry this label, and stays.
    """
    pieces: list[str] = []
    cursor = 0
    for begin in _PEM_BEGIN_RE.finditer(text):
        if begin.start() < cursor:
            continue
        end = _PEM_END_RE.search(text, begin.end())
        pieces.append(text[cursor : begin.start()])
        pieces.append(_REDACTED)
        if end is None:
            cursor = len(text)
            break
        cursor = end.end()
    pieces.append(text[cursor:])
    return "".join(pieces)


def _scheme_start(text: str, colon: int) -> int:
    """Where the scheme of the ``://`` at ``colon`` starts, or -1.

    The scheme is a letter and then at most 31 scheme characters. A longer
    run is not a scheme: taking it whole is what made a chosen text
    quadratic. Digits, ``+``, ``.`` and ``-`` may follow the letter, so a
    window that starts on one of those is read from the letter.
    """
    begin = colon - 1
    limit = max(0, colon - _URL_SCHEME_MAX)
    while begin >= limit and text[begin] in _URL_SCHEME_CHARS:
        begin -= 1
    start = begin + 1
    while start < colon and text[start] not in _URL_SCHEME_START:
        start += 1
    if start >= colon:
        return -1
    return start


def _find_authority_end(text: str, auth_start: int) -> int:
    """Return the index where the authority ends.

    A comma, parenthesis or apostrophe belongs to the user name or the
    password, so it does not end the authority before the ``@``. Stopping
    there hides ``db,user:secret`` and ``john.doe:123,secret``. The same
    mark ends the host once the ``@`` has been seen. The ``@`` is what
    resolves userinfo; a dotted name and a numeric prefix are not read as
    a host and a port before that ``@`` is known.
    """
    length = len(text)
    end = auth_start
    at_sign = -1
    while end < length:
        char = text[end]
        if char in _URL_AUTHORITY_END:
            return end
        if char in _URL_USERINFO_MARKS and at_sign >= 0:
            return end
        if char == "@":
            at_sign = end
        end += 1
    return end


def _url_userinfo_at(authority: str) -> int | None:
    """Index of the ``@`` that ends a password userinfo, or None.

    The password may itself hold an ``@``, so the end is the last one.
    The colon has to sit in front of it: a colon after it is a port, and
    ``scheme://user@host`` names a user, not a password. An empty password
    is not a password. A double quote, bracket or angle bracket is not
    userinfo: a raw one means this authority is not a credential URL.
    A comma, parenthesis or apostrophe is userinfo, and stays.
    """
    at = authority.rfind("@")
    if at < 0:
        return None
    userinfo = authority[:at]
    colon = userinfo.find(":")
    if colon < 0 or colon + 1 == at:
        return None
    for char in userinfo:
        if char in _URL_USERINFO_FORBIDDEN:
            return None
    return at


def _spilled_password_at(text: str, auth_start: int, auth_end: int) -> int | None:
    """Index of the ``@`` that ends a password holding a raw delimiter.

    ``auth_end`` is where the plain authority stopped. The password only
    spills past it when that stop is a ``#``, a quote, a square bracket or
    a blank, a colon sits in front of it after a user name with no ``@``,
    and what follows the colon is not a port. The ``@`` is the last one on
    the line, before the next ``://``, that a host name follows. Returns an
    absolute index into ``text``, or None.
    """
    if auth_end >= len(text) or text[auth_end] not in _URL_PASSWORD_SPILL:
        return None
    head = text[auth_start:auth_end]
    colon = head.find(":")
    if colon < 0 or "@" in head:
        return None
    after_colon = head[colon + 1 :]
    if after_colon.isdigit():
        return None
    if not after_colon and text[auth_end] in " \t":
        return None
    stop = _URL_SPILL_END_RE.search(text, auth_end)
    region_end = stop.start() if stop is not None else len(text)
    at = text.rfind("@", auth_end, region_end)
    while at >= 0:
        if at + 1 < len(text) and text[at + 1] in _URL_HOST_START:
            return at
        at = text.rfind("@", auth_end, at)
    return None


def _resume_after_rejected_authority(
    text: str,
    sep: int,
    auth_start: int,
    auth_end: int,
) -> int:
    """Where to look next after an authority that holds no password.

    The search moves past this ``://``. It does not move past a later
    scheme whose slash is what ended the authority:
    ``https://example.comhttps://user:pass@host`` stops the first
    authority on that slash, and the second scheme starts before it.
    """
    if (
        auth_end > 0
        and auth_end + 1 < len(text)
        and text[auth_end] == "/"
        and text[auth_end - 1] == ":"
        and text[auth_end + 1] == "/"
    ):
        nested = _scheme_start(text, auth_end - 1)
        if nested >= auth_start:
            return nested
    return max(auth_end, sep + 3)


def _redact_url_userinfo(text: str) -> str:
    """Replace a URL's userinfo and keep the scheme and the host.

    The user name and the password go together: a user name in a URL can
    itself be the token, and the password beside it is a password. The
    userinfo runs to the last ``@`` of the authority, so a password that
    holds an ``@`` does not leave its suffix in front of the host. Whatever
    follows that ``@`` stays, so the text still says which server failed.

    A rejected scheme advances past the authority it already walked. Leaving
    the search where it was makes every later ``://`` walk that same run
    again.
    """
    pieces: list[str] = []
    cursor = 0
    search = 0
    length = len(text)
    while search < length:
        sep = text.find("://", search)
        if sep < 0:
            break
        scheme = _scheme_start(text, sep)
        if scheme < 0 or scheme < cursor:
            search = sep + 3
            continue
        auth_start = sep + 3
        auth_end = _find_authority_end(text, auth_start)
        at = _url_userinfo_at(text[auth_start:auth_end])
        if at is None:
            spilled = _spilled_password_at(text, auth_start, auth_end)
            if spilled is not None:
                at = spilled - auth_start
        if at is None:
            search = _resume_after_rejected_authority(
                text, sep, auth_start, auth_end
            )
            continue
        pieces.append(text[cursor:scheme])
        pieces.append(text[scheme:auth_start])
        pieces.append(_REDACTED)
        pieces.append("@")
        cursor = auth_start + at + 1
        search = cursor
    pieces.append(text[cursor:])
    return "".join(pieces)


def _value_already_marked(text: str, value_start: int, separator: str) -> bool:
    """Whether this value is already only the marker.

    A same-line value that is ``[redacted]`` stops at the ``]``, so taking
    it again leaves the bracket behind. A value that crossed onto the next
    line is the whole of that line. A marker glued to a leftover secret
    (``[redacted]/tail-secret``, after a provider prefix was taken out on
    its own) is still a secret, and the line is taken whole.
    """
    if "\n" in separator or "\r" in separator:
        return False
    return text.startswith(_REDACTED, value_start)


def _redact_credential_pairs(text: str) -> str:
    """Take the credential values out of the key/value pairs a text holds.

    A pair is a key and the separator after it; the value is read after the
    separator, so that a pair which names no credential cannot hide the
    credential that follows it.

    The key is judged by its own name -- see :func:`_is_credential_key` --
    before the value is read, and a pair that names no credential leaves
    the text exactly as it was, the value with it. Reading that value
    first walks a whitespace-free tail to its end, and every later key in
    the run walks the same tail: a long ``x://`` is one such run. A value
    that a unified diff carried onto the next added line is read after
    that line's ``+``, not at the marker itself.
    """
    pieces: list[str] = []
    pos = 0
    for match in _CREDENTIAL_PAIR_RE.finditer(text):
        if match.start() < pos:
            # A pair that lies inside a value which was already taken out.
            continue
        key = match.group("key")
        if not _is_credential_key(key):
            # The pair names no credential: the text around it is left
            # exactly as it was. The value stays unread on purpose.
            continue
        if _AUTHORIZATION_KEY_RE.search(key):
            # The Authorization header owns its own rule: the scheme in
            # front of the value is kept, and only the value goes.
            continue
        separator = match.group("sep")
        value_start = _diff_value_start(text, separator, match.end())
        if _value_already_marked(text, value_start, separator):
            # The same-line value is already the marker. Taking it again
            # would swallow whatever came after it -- the marker's own
            # text runs up to the next space or quote, and a closing
            # brace is neither.
            continue
        # The value crossed onto the next line, so the punctuation that
        # ends a same-line pair is part of the secret on this one.
        value_re = (
            _CREDENTIAL_DIFF_VALUE_RE
            if "\n" in separator or "\r" in separator
            else _CREDENTIAL_VALUE_RE
        )
        value_match = value_re.match(text, value_start)
        if value_match is None:
            # There is no value to take out: the pair names a key and
            # nothing else.
            continue
        value = value_match.group(0)
        if _is_count_key(key) and _looks_like_a_count(value):
            # A number under a count key is a count, not a secret.
            continue
        pieces.append(text[pos : match.start()])
        pieces.append(text[match.start() : value_start])
        pieces.append(_REDACTED)
        pos = value_match.end()
    pieces.append(text[pos:])
    return "".join(pieces)


def _redact_spaced_keys(text: str) -> str:
    """Take the value out of a space-separated credential key.

    The pair rule cannot see these names: its key stops at the space.
    On the same line the value is one word, as it always was. Across a
    newline the value is the rest of the added line. An unquoted diff
    value may hold spaces, and stopping at the first word leaves the
    rest of the secret on that line.
    """
    pieces: list[str] = []
    pos = 0
    for match in _CRED_SPACED_KEY_RE.finditer(text):
        if match.start() < pos:
            continue
        separator = match.group("sep")
        value_start = _diff_value_start(text, separator, match.end())
        if _value_already_marked(text, value_start, separator):
            continue
        on_next_line = "\n" in separator or "\r" in separator
        value_re = _CREDENTIAL_DIFF_VALUE_RE if on_next_line else _SPACED_VALUE_RE
        value_match = value_re.match(text, value_start)
        if value_match is None:
            continue
        value = value_match.group(0)
        if _is_count_key(match.group("key")) and _looks_like_a_count(value):
            continue
        pieces.append(text[pos : match.start()])
        pieces.append(text[match.start() : value_start])
        pieces.append(_REDACTED)
        pos = value_match.end()
    pieces.append(text[pos:])
    return "".join(pieces)


def _redact_text(text: str) -> str:
    """Take the credential values out of a text."""
    # The block and the userinfo go first. A later rule that read the body
    # or the password as its own value would keep the host or the label and
    # still have to be sure the secret was gone; taking the whole of each
    # out once leaves nothing for those rules to split.
    redacted = _redact_pem_blocks(text)
    redacted = _redact_url_userinfo(redacted)
    redacted = _AWS_KEY_RE.sub(_REDACTED, redacted)
    redacted = _CREDENTIAL_SHAPE_RE.sub(_REDACTED, redacted)
    redacted = _redact_credential_pairs(redacted)
    redacted = _redact_spaced_keys(redacted)
    redacted = _ACCOUNT_KEY_RE.sub(_redact_account_value, redacted)
    redacted = _ACCOUNT_SHAPE_RE.sub(_REDACTED, redacted)
    redacted = _AUTHORIZATION_RE.sub(_redact_value, redacted)
    redacted = _BEARER_RE.sub(_redact_value, redacted)
    return redacted


def redact_credentials(value: Any) -> Any:
    """Take the credential values out of a value.

    A mapping is walked key by key: a key that names a credential has its
    whole value replaced, whatever the value is -- a string, a number, a
    list, a mapping or nothing at all. The one exception is a number under
    one of the count keys, which is a count of tokens and not a secret. A
    list or a tuple is walked item by item. A text has its credential
    values replaced: the key/value pair -- quoted or not, in front of a
    string, a bare word or a number, including a value a unified diff
    carried onto the next ``+`` line -- the Authorization header and a
    bearer token on its own -- an AWS access key id, whose form is fixed
    and which no key names -- a PEM private-key block -- and the userinfo
    of a URL (``scheme://user:password@host``). Bytes are decoded, redacted
    and encoded again, so a byte diagnostic is not a way around the
    redaction. Anything else is returned as it is.

    The value is replaced by one marker, ``[redacted]``, and the same input
    always gives the same output, so two callers may compare their results.
    """
    if isinstance(value, Mapping):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            name = str(key)
            if _is_credential_key(name) and not (
                _is_count_key(name) and _is_a_number(item)
            ):
                redacted[key] = _REDACTED
            else:
                redacted[key] = redact_credentials(item)
        return redacted
    if isinstance(value, (list, tuple)):
        items = [redact_credentials(item) for item in value]
        return tuple(items) if isinstance(value, tuple) else items
    if isinstance(value, (bytes, bytearray)):
        # The bytes are decoded the way the diagnostic path decodes them, so
        # a credential in a byte diagnostic is seen before it becomes text.
        # Without this the redaction would be skipped entirely: the bytes
        # are not a string, and the text they carry only appears later.
        redacted = _redact_text(bytes(value).decode("utf-8", errors="replace"))
        encoded = redacted.encode("utf-8")
        return encoded if isinstance(value, bytes) else bytearray(encoded)
    if isinstance(value, str):
        return _redact_text(value)
    return value


# ---------------------------------------------------------------------------
# 9.1 -- the signature, and the rules a human approved
#
# A signature is what lets the same failure be counted twice without keeping
# the diagnostic. The text is reduced -- and the reduction ends in a
# whitelist: only the words of a fixed vocabulary, a status code a keyword
# named, and the placeholders and punctuation the reduction itself left
# behind can appear in what is stored. Nothing identifying can be left in
# it, because nothing else can appear in it at all. Only that text is ever
# stored: the original is read, reduced and dropped inside a single call.
# ---------------------------------------------------------------------------

# The reduced text is bounded, so a signature is a fixed-size thing to count
# and to store, however long the diagnostic was.
_MAX_SIGNATURE_BYTES = 512

# What the reduction leaves behind. These are words, not values: a reduced
# text can be read, compared and logged without carrying anything out.
_REDACTED_EMAIL = "<email>"
_REDACTED_TOKEN = "<token>"
_REDACTED_ACCOUNT = "<account>"
_REDACTED_UUID = "<uuid>"
_REDACTED_TIMESTAMP = "<timestamp>"
_REDACTED_PATH = "<path>"
_REDACTED_HEX = "<hex>"
_REDACTED_NUMBER = "<num>"

# The words that say what went wrong, as opposed to the lines that say where
# the program was when it went wrong.
_ERROR_MARKERS = (
    "error",
    "exception",
    "failed",
    "failure",
    "fatal",
    "panic",
    "traceback",
    "timeout",
    "timed out",
    "refused",
    "denied",
    "exceeded",
    "exhausted",
    "invalid",
    "unauthorized",
    "forbidden",
    "not found",
    "unable",
    "cannot",
    "quota",
    "rate limit",
    "overload",
    "unavailable",
)

# A class name in the text ("TypeError", "ServiceException") is a strong
# signal that this is the line that carries the failure.
_SIGNATURE_ERROR_CLASS_RE = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Fault|Panic)\b"
)

# A stack frame says where the program was, not what went wrong, so it loses
# to a line that names the failure.
_SIGNATURE_FRAME_RES = (
    re.compile(r"^\s*at\s+\S"),
    re.compile(r"^\s*File\s+\"", re.IGNORECASE),
    re.compile(r"\.(?:py|js|ts|tsx|rb|go|java|rs|cc|cpp|h):\d+"),
)

# The line after the error line that belongs to the same error.
_SIGNATURE_CONTINUATION_RE = re.compile(
    r"^(?:cause|caused by|reason|detail|details)\b", re.IGNORECASE
)

_SIGNATURE_URL_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9+.\-]*)://([^\s/?#]+)([^\s]*)")
_SIGNATURE_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")

# The signature keeps only the numbers that name an HTTP status, and only
# where a keyword says so. The classifier has its own, more generous rules
# -- a status may also be named by the phrase that follows it, as in "429
# Too Many Requests" -- and those must not change, because that is how the
# classifier recognises the failure. The signature therefore has a pattern
# of its own here.
#
# A keyword is one of the words that name a status ("status", "http",
# "code", "error") or a compound of them ("status_code", "status-code",
# "http status", "errcode", "error code", "resp code"). The number follows
# the keyword directly or through whitespace, a colon or an equals sign.
_SIGNATURE_STATUS_KEYWORD = (
    r"(?:http|status|code|error|err|resp)"
    r"(?:[-_ ]?(?:status|code|error|err|http|resp))*"
)

# Exactly three digits in the range 100 to 599 -- the codes that are
# statuses. The trailing guard keeps "1000" from being read as "100".
_SIGNATURE_STATUS_NUMBER = r"[1-5]\d{2}(?!\d)"

# The "HTTP/1.1 503" form: the keyword carries the protocol version, and
# the code follows the token.
_SIGNATURE_STATUS_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    + _SIGNATURE_STATUS_KEYWORD
    + r"(?![A-Za-z0-9])"
    + r"(?:/\d(?:\.\d)?)?"
    + r"\s*[:=]?\s*"
    + r"(?P<code>" + _SIGNATURE_STATUS_NUMBER + r")",
    re.IGNORECASE,
)

# The marker the redaction leaves behind, in the two shapes it takes in a
# text: bare, and quoted.
_REDACTED_MARKER = r"[\"']?\[redacted\][\"']?"

# The normalisation's half of the redaction. The value is already gone --
# the redaction left the marker behind -- so what is left only says what
# used to be there: a credential is a <token> and an account is an
# <account>. The key is a candidate, and _CRED_KEY decides whether it
# names a credential -- the same rule the redaction itself used, so the
# two can never disagree about what a credential is.
_SIGNATURE_CREDENTIAL_RE = re.compile(
    r"[\"']?(?P<key>[A-Za-z0-9_.\-]+)[\"']?\s*[:=]\s*"
    + _REDACTED_MARKER,
    re.IGNORECASE,
)

# The shapes an account id takes. The keyword needs a separator, so a class
# name that merely starts with "Account" is left alone.
_SIGNATURE_ACCOUNT_RES = (
    re.compile(
        r"[\"']?(?<![A-Za-z0-9])(?:acct|account|account[-_ ]?id|user[-_ ]?id|uid"
        r"|customer[-_ ]?id|sub[-_ ]?id|org[-_ ]?id|project[-_ ]?id)"
        r"(?![A-Za-z0-9])[\"']?\s*[:=]\s*"
        + _REDACTED_MARKER,
        re.IGNORECASE,
    ),
)

_SIGNATURE_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_SIGNATURE_TIMESTAMP_RES = (
    re.compile(
        r"\d{4}-\d{2}-\d{2}"
        r"(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?(?!\d)"
    ),
    re.compile(r"\b\d{2}:\d{2}:\d{2}(?:\.\d+)?\b"),
)
_SIGNATURE_PATH_RES = (
    re.compile(r"(?<![\w.])/(?:[\w.+@-]+/)+[\w.+@-]+"),
    re.compile(r"\b[A-Za-z]:\\[^\s'\"]+"),
)
_SIGNATURE_HEX_RE = re.compile(r"\b[0-9a-f]{32,}\b", re.IGNORECASE)
_SIGNATURE_NUMBER_RE = re.compile(r"\d+")

# The whitelist. The rules above take out the shapes a diagnostic is made
# of -- ids, addresses, keys -- but a blacklist can only name what it
# thought of, and a review found two diagnostics whose credentials walked
# around it (an escaped quote inside an Authorization value, and a value
# that starts with the redaction marker itself). So the reduction does not
# end in a blacklist: it ends in this vocabulary. A token that is not one
# of these words cannot appear in the stored text, whatever slipped past
# the rules in front of it -- and a secret that cannot appear cannot leak.
#
# The words are the ones the generic rules and the provider rules already
# match on, plus the words of the placeholders the reduction leaves behind
# (``<num>``, ``<token>``, ...) and the seven count keys whose numbers are
# counts. They are lowercase and made of word characters only, so the
# final pass can match them against whole tokens.
_SIGNATURE_VOCAB: frozenset[str] = frozenset(
    (
        # The words the reduction has always kept: what went wrong.
        "error", "failed", "failure", "fatal", "panic", "traceback",
        "exception", "exceeded", "exceeds", "exhausted", "invalid",
        "unauthorized", "forbidden", "denied", "unable", "cannot",
        "rejected", "required", "found", "out", "timed",
        # Rate limits, and the words a 429 is written with.
        "rate", "rate_limit", "rate_limited", "rate_limit_error",
        "limit", "limited", "too", "many", "requests", "request",
        "throttle", "throttled", "throttling", "retry", "congestion",
        "too_many_requests", "resource_exhausted", "usage_limit_exceeded",
        # Quota and billing.
        "quota", "quota_exceeded", "quota_exhausted", "account_quota",
        "insufficient", "insufficient_quota", "insufficient_balance",
        "balance", "balance_exhausted", "credit", "credits",
        "credits_exhausted", "exhaust", "fund", "funds", "low", "spend",
        "spent", "depleted", "budget", "usage", "reached", "bill",
        "billing", "payment", "expire", "expired", "account", "credential",
        # Context length.
        "context", "context_length", "context_length_exceeded",
        "context_window", "context_window_exceeded", "context_exceeded",
        "length", "window", "large", "long", "size", "maximum", "max",
        "count", "number", "tokens", "token", "prompt", "input",
        "output", "total", "turns", "conversation", "entity", "payload",
        # Overloaded servers, and the words a 5xx is written with.
        "overload", "overloaded", "overloaded_error", "server",
        "server_error", "server_overloaded", "service", "unavailable",
        "service_unavailable", "bad", "gateway", "bad_gateway",
        "gateway_timeout", "internal", "internal_error", "upstream",
        "capacity", "temporarily", "maintenance", "suspended",
        "deactivated", "locked", "http", "https",
        "status", "code", "resp", "response", "model", "models",
        # Network failures.
        "connection", "connect", "refused", "reset", "aborted", "abort",
        "closed", "interrupted", "cancel", "shutdown", "timeout",
        "network", "network_error", "unreachable", "route", "host",
        "resolve", "resolution", "dns", "nxdomain", "ssl", "tls",
        "handshake", "certificate", "socket", "fetch", "temporary",
        "econnrefused", "econnreset", "etimedout", "epipe",
        "ehostunreach", "enetunreach", "eai_again", "enotfound",
        # Authentication, sessions and permissions.
        "auth", "authentication", "authorization", "bearer", "login",
        "logged", "oauth", "session", "key", "permission",
        "permission_error", "provider_error", "billing_error",
        # The seven count keys, whose number is a count and not a secret.
        "token_count", "max_tokens", "input_tokens", "output_tokens",
        "total_tokens", "context_window_tokens",
        # The words of the placeholders the reduction leaves behind. A
        # placeholder is a name, not a value, so its word stays.
        "num", "email", "uuid", "timestamp", "path", "hex", "redacted",
    )
)

# The punctuation the whitelist keeps: the characters the reduction chain
# itself leaves behind -- the separators of a message, the assignment sign
# of a kept pair, the brackets of the redaction marker and of the
# ``<placeholder>`` words. Punctuation carries no secret by itself; the
# words around it are what the vocabulary decides.
_SIGNATURE_PUNCT: frozenset[str] = frozenset(':,().-/[]{}<>="\'')

# The tokens of a reduced text: a run of word characters, a run of the rest
# that is not whitespace, or the whitespace itself.
_STATUS_CODE_TOKEN_RE = re.compile(r"[1-5][0-9]{2}")


_SIGNATURE_TOKEN_RE = re.compile(r"\w+|[^\w\s]+|\s+")

# What a token the whitelist does not name is replaced by.
_SIGNATURE_UNKNOWN_TOKEN = "<x>"

# A signature id is a sha256, and nothing else is accepted as one.
_SIGNATURE_ID_RE = re.compile(r"^[0-9a-f]{64}$")

# The states a learned rule can be in. "approved" is the only one that acts;
# "candidate" predicts and is shadowed; "rejected" is remembered so the
# learning loop stops asking about it.
_LEARNED_RULE_STATUSES = frozenset({"approved", "candidate", "rejected"})


@dataclass(frozen=True)
class Signature:
    """The reduced shape of one failure, and the id that names it.

    ``normalized_text`` is the reduced text and nothing else: the diagnostic
    it came from is not kept, not logged and not returned. The reduction
    ends in a whitelist, so the text holds only vocabulary words, protected
    status codes and the placeholders and punctuation the reduction itself
    leaves behind -- no token outside that set can appear in it, whatever
    the diagnostic carried. The id is the sha256 of that text, so two
    occurrences of the same failure share it however different their
    identifiers and timestamps were.
    """

    provider: str
    signature_id: str
    normalized_text: str


@dataclass(frozen=True)
class LearnedRule:
    """One rule a human approved, read back from the rules file.

    ``status`` is what the file said. ``missing_approval`` names the
    approval metadata the file did not carry: a rule that acts has to say
    when it was approved, what should be done and how often the failure
    was seen. A rule that is missing any of the three has nothing behind
    it, so it only shadows -- ``effective_status`` is the status it acts
    with, which is then "candidate".
    """

    provider: str
    signature_id: str
    pattern: str
    failure_class: FailureClass
    action: str
    status: str
    approved_at: str | None
    evidence_count: int
    missing_approval: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Read the approval metadata the rule does not carry.

        The fields are the only source, so a rule that is built by hand is
        judged by the same rule as one that was read from the file: a rule
        that says it is approved but carries no approval behind it has
        nothing to act on, and it only shadows.
        """
        if self.status != "approved":
            # A candidate or a rejected rule was never going to act anyway.
            object.__setattr__(self, "missing_approval", ())
            return
        missing: list[str] = []
        if not isinstance(self.approved_at, str) or not self.approved_at.strip():
            missing.append("approved_at")
        if not isinstance(self.action, str) or not self.action.strip():
            missing.append("action")
        if _evidence_count(self.evidence_count) <= 0:
            missing.append("evidence_count")
        object.__setattr__(self, "missing_approval", tuple(missing))

    @property
    def effective_status(self) -> str:
        """The status the rule acts with, once the approval is judged."""
        if self.missing_approval:
            return "candidate"
        return self.status


def default_rules_path() -> Path:
    """Where the approved rules live."""
    return Path.home() / ".config" / "conductor" / "worker-failure-rules.json"


def _marker(index: int) -> str:
    """A placeholder that carries no digit, so the number sweep leaves it."""
    first = ord("a") + index % 26
    second = ord("a") + (index // 26) % 26
    return f"\x02{chr(first)}{chr(second)}\x02"


def _error_score(line: str) -> int:
    """How much a line says about what went wrong.

    The score picks the error line out of a diagnostic: a traceback carries
    the message somewhere in the middle, and the frames above it say nothing
    about the failure. A frame therefore loses to a line that names the
    failure, and an indented line loses to both.
    """
    stripped = line.strip()
    lowered = stripped.lower()
    score = 0
    if _SIGNATURE_ERROR_CLASS_RE.search(stripped):
        score += 2
    if _STATUS_LABEL_RE.search(stripped):
        score += 2
    markers = sum(1 for marker in _ERROR_MARKERS if marker in lowered)
    score += min(markers, 2)
    for pattern in _SIGNATURE_FRAME_RES:
        if pattern.search(stripped):
            score -= 3
            break
    if line[:1].isspace():
        score -= 1
    return score


def _is_continuation(line: str) -> bool:
    """Whether a line belongs to the error line above it."""
    if line[:1].isspace():
        return True
    return bool(_SIGNATURE_CONTINUATION_RE.match(line.strip()))


def _error_segment(text: str) -> str:
    """The part of the diagnostic that says what went wrong.

    Only this part is reduced. A diagnostic that carries a whole traceback
    would otherwise reduce to its first frame, and the frame changes every
    time the program is run a different way.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    best_index = 0
    best_score: int | None = None
    for index, line in enumerate(lines):
        score = _error_score(line)
        if best_score is None or score > best_score:
            best_index = index
            best_score = score
    segment = [lines[best_index].strip()]
    for line in lines[best_index + 1 :]:
        if not _is_continuation(line):
            break
        segment.append(line.strip())
    return "\n".join(segment)


def _protect_status_codes(text: str) -> tuple[str, dict[str, str]]:
    """Take the HTTP status codes out of reach of the number sweep.

    A status code is the one number that carries the class: "429" and "503"
    are different failures and both would otherwise reduce to "<num>". The
    code is put behind a placeholder and put back afterwards, so the reduced
    text keeps the one number that matters and loses every other one.

    Only a code that a keyword names is kept. A naked "429" on a line of
    its own is as likely to be a count as a status, and a number that no
    keyword names says nothing about the class.
    """
    protected: dict[str, str] = {}
    counter = [0]

    def stash(match: re.Match[str]) -> str:
        code = match.group("code")
        marker = _marker(counter[0])
        counter[0] += 1
        protected[marker] = code
        head = match.group(0)[: match.start("code") - match.start()]
        return f"{head}{marker}"

    text = _SIGNATURE_STATUS_RE.sub(stash, text)
    return text, protected


def _url_replacement(match: re.Match[str]) -> str:
    """Keep the scheme and the host, drop the path and the query.

    The path and the query are where a key usually hides; the host is the
    provider's own address and says nothing about the user. The whole URL is
    taken in one go, so that the path cannot be left behind to be read as a
    path.
    """
    scheme = match.group(1).lower()
    authority = match.group(2)
    host = authority.rsplit("@", 1)[-1]
    return f"{scheme}://{host}"


def _truncate_bytes(text: str, limit: int) -> str:
    """Cut a text to a number of bytes, not a number of characters."""
    encoded = text.encode("utf-8", errors="ignore")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore").strip()


def _redact_signature_credential(match: re.Match[str]) -> str:
    """Name a credential in the reduced text, and nothing else.

    The marker is what is left of a value, so the key in front of it says
    what the value was: a credential is a <token>. A key that names no
    credential is left as it is -- the reduction keeps the words of the
    error, and "error=[redacted]" is not a credential.
    """
    key = match.group("key")
    if _is_count_key(key) or not _is_credential_key(key):
        return match.group(0)
    return _REDACTED_TOKEN


def _whitelist_pass(text: str) -> str:
    """Keep the tokens the vocabulary names, and placeholder every other one.

    This is the last pass of the reduction, and it is a whitelist, not a
    blacklist: a word token survives only when its lowercase form is in
    :data:`_SIGNATURE_VOCAB`; a digit token survives only when it is a
    three-digit code in the status range; a punctuation run survives only
    when every character of it is one of :data:`_SIGNATURE_PUNCT`. Every
    other token is replaced by ``<x>``, and a run of neighbouring
    ``<x>`` tokens collapses into one.

    The invariant this gives the reduction: no token outside the whitelist
    can survive into the text that is stored and hashed -- so no secret can
    appear there, whatever the redaction rules in front of this pass
    missed.

    The digit rule needs no bookkeeping of its own. The number sweep in
    front of it has already replaced every number with ``<num>``, and the
    one thing it put back is the status codes the status pass had stashed
    behind digit-free markers -- codes that are three digits in the range
    100 to 599 by their own pattern. So a three-digit token in the status
    range at this point is exactly a restored status code, and nothing
    else.
    """
    kept: list[str] = []
    for token in _SIGNATURE_TOKEN_RE.findall(text):
        if token[0].isspace():
            # Whitespace is the fold's own output, and it separates the
            # tokens; it is kept as it is.
            kept.append(token)
        elif all(char in _SIGNATURE_PUNCT for char in token):
            kept.append(token)
        elif token.isascii() and _STATUS_CODE_TOKEN_RE.fullmatch(token):
            # A restored status code: the one number that carries the class.
            # The match is ASCII and exact (`[1-5][0-9]{2}`): `isdigit()`
            # alone is true for superscript and circled digits, whose int()
            # conversion raises.
            kept.append(token)
        elif token.lower() in _SIGNATURE_VOCAB:
            kept.append(token)
        else:
            kept.append(_SIGNATURE_UNKNOWN_TOKEN)
    collapsed: list[str] = []
    for piece in kept:
        if (
            piece == _SIGNATURE_UNKNOWN_TOKEN
            and collapsed
            and collapsed[-1] == _SIGNATURE_UNKNOWN_TOKEN
        ):
            continue
        collapsed.append(piece)
    return "".join(collapsed)


def _normalize_signature_text(text: str) -> str:
    """Reduce a diagnostic to the part that stays the same between failures.

    Everything that differs between two occurrences of the same error -- the
    request id, the timestamp, the token count, the account, the key, the
    path -- is taken out, and the reduction then ends in a whitelist: only
    the words of :data:`_SIGNATURE_VOCAB`, a status code the status pass
    protected, and the punctuation and placeholders the reduction itself
    leaves behind can appear in what comes out. What is left is
    lower-cased, has its whitespace folded and is bounded, so the sha256 of
    it is the same for every occurrence of that error.

    The invariant: a token outside the whitelist cannot survive into the
    reduced text, so no secret -- whatever the redaction missed -- can
    appear in it.
    """
    segment = _error_segment(text)
    if not segment:
        return ""
    segment, statuses = _protect_status_codes(segment)
    segment = _SIGNATURE_URL_RE.sub(_url_replacement, segment)
    segment = _SIGNATURE_EMAIL_RE.sub(_REDACTED_EMAIL, segment)
    # The account comes first: an account id is an account, and the
    # credential rule would otherwise name it a token.
    for pattern in _SIGNATURE_ACCOUNT_RES:
        segment = pattern.sub(_REDACTED_ACCOUNT, segment)
    segment = _SIGNATURE_CREDENTIAL_RE.sub(_redact_signature_credential, segment)
    segment = _SIGNATURE_UUID_RE.sub(_REDACTED_UUID, segment)
    for pattern in _SIGNATURE_TIMESTAMP_RES:
        segment = pattern.sub(_REDACTED_TIMESTAMP, segment)
    for pattern in _SIGNATURE_PATH_RES:
        segment = pattern.sub(_REDACTED_PATH, segment)
    segment = _SIGNATURE_HEX_RE.sub(_REDACTED_HEX, segment)
    segment = _SIGNATURE_NUMBER_RE.sub(_REDACTED_NUMBER, segment)
    for marker, code in statuses.items():
        segment = segment.replace(marker, code)
    segment = segment.lower()
    segment = re.sub(r"\s+", " ", segment).strip()
    # The final pass: a token the whitelist does not name cannot survive
    # into the text that is stored, whatever slipped past the rules above.
    segment = _whitelist_pass(segment)
    return _truncate_bytes(segment, _MAX_SIGNATURE_BYTES)


def _redacted_source(source: Any) -> Any:
    """The diagnostic with its credential values taken out.

    A finished turn carries its events apart from its text, and an event may
    carry a credential too, so both parts are redacted.
    """
    if isinstance(source, NativeCliResult):
        return replace(
            source,
            text=redact_credentials(source.text),
            raw=tuple(redact_credentials(event) for event in source.raw),
        )
    return redact_credentials(source)


def failure_signature(
    provider: str,
    result_or_diagnostic: str | Mapping[str, Any] | NativeCliResult | None = None,
) -> Signature:
    """Name a failure by its shape, without keeping its text.

    The diagnostic is redacted, reduced, hashed and dropped. The result holds
    the reduced text and the id; the text it was made of is not in it, is not
    in the log and is not returned. The reduction ends in a whitelist, so
    the reduced text holds nothing outside the vocabulary, the protected
    status codes and the placeholders -- no secret the redaction missed can
    reach it.

    The redaction happens before the diagnostic is flattened into text: a
    mapping loses the key of its values on the way, so a credential has to be
    taken out while the key is still there.
    """
    diagnostic = _diagnostic(_redacted_source(result_or_diagnostic))
    normalized = _normalize_signature_text(diagnostic.text)
    signature_id = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return Signature(
        provider=_provider_key(provider),
        signature_id=signature_id,
        normalized_text=normalized,
    )


def _evidence_count(value: Any) -> int:
    """Read the evidence count, or nothing when it is not one."""
    if isinstance(value, bool):
        return 0
    try:
        count = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, count)


def _parse_learned_rule(entry: Any) -> LearnedRule | None:
    """Read one rule out of the file, or nothing when it cannot be used.

    A rule that names a class outside the vocabulary, or an id that is not a
    sha256, is not a rule: it is skipped, so a damaged file costs the rules
    it holds and not the classifier.
    """
    if not isinstance(entry, Mapping):
        return None
    raw_class = entry.get("class")
    if raw_class is None:
        return None
    try:
        failure_class = FailureClass(str(raw_class))
    except ValueError:
        return None
    signature_id = str(entry.get("signature_id") or "").strip().lower()
    if not _SIGNATURE_ID_RE.match(signature_id):
        return None
    provider = _canonical_key(str(entry.get("provider") or ""))
    if not provider:
        return None
    status = str(entry.get("status") or "approved").strip().lower()
    if status not in _LEARNED_RULE_STATUSES:
        return None
    evidence_count = _evidence_count(entry.get("evidence_count"))
    approved_at = entry.get("approved_at")
    # The approval is judged by the rule itself, so a rule that was built by
    # hand and a rule that was read from the file are held to one rule.
    rule = LearnedRule(
        provider=provider,
        signature_id=signature_id,
        pattern=str(entry.get("pattern") or ""),
        failure_class=failure_class,
        action=str(entry.get("action") or "").strip(),
        status=status,
        approved_at=(
            approved_at
            if isinstance(approved_at, str) and approved_at.strip()
            else None
        ),
        evidence_count=evidence_count,
    )
    if rule.missing_approval:
        # Only a rule that says it is approved can lose its approval: a
        # candidate or a rejected rule was never going to act anyway. The log
        # names what is missing and nothing the Worker wrote.
        logger.warning(
            "learned failure rule downgraded to a candidate: provider=%s"
            " signature_id=%s missing=%s reason_code=rule_missing_approval",
            provider,
            signature_id,
            ",".join(rule.missing_approval),
        )
    return rule


def _parse_learned_rules(payload: Any) -> tuple[LearnedRule, ...]:
    """Read the rules out of the file's payload."""
    if isinstance(payload, Mapping):
        entries = payload.get("rules")
    elif isinstance(payload, list):
        entries = payload
    else:
        entries = None
    if not isinstance(entries, list):
        return ()
    rules = []
    for entry in entries:
        rule = _parse_learned_rule(entry)
        if rule is not None:
            rules.append(rule)
    return tuple(rules)


def _rule_covers(
    rule: LearnedRule,
    provider_key: str,
    signature_id: str,
    normalized_text: str,
) -> bool:
    """Whether a learned rule is about this failure.

    The id is the match. The pattern is a second way in, for a rule that was
    written against a shape rather than against one id.
    """
    if rule.provider != provider_key:
        return False
    if rule.signature_id and rule.signature_id == signature_id:
        return True
    if rule.pattern:
        try:
            return re.search(rule.pattern, normalized_text) is not None
        except re.error:
            return False
    return False


class RuleSet:
    """The rules a human approved, read back from one private file.

    The file is the only place a learned rule lives, so it is checked before
    it is read: a file another user can read is a file another user can
    write, and a rule file like that is not read at all.
    """

    def __init__(self, rules: Sequence[LearnedRule] = ()) -> None:
        self._rules: tuple[LearnedRule, ...] = tuple(rules)

    def __len__(self) -> int:
        return len(self._rules)

    @property
    def rules(self) -> tuple[LearnedRule, ...]:
        """Every rule the file holds."""
        return self._rules

    @classmethod
    def empty(cls) -> RuleSet:
        """A rule set with nothing in it."""
        return cls(())

    @classmethod
    def load(cls, path: Path | str | None = None) -> RuleSet:
        """Read the rules file, or come back with nothing.

        A file that is not there is not a failure: most Workers never fail.
        A file that cannot be read, or that is not private, is refused for
        the same reason the classifier refuses to carry a diagnostic: it is
        better to lose the rules than to act on rules nobody signed off on.
        """
        target = Path(path) if path is not None else default_rules_path()
        try:
            mode = target.stat().st_mode
        except OSError:
            return cls(())
        if mode & 0o077:
            logger.warning(
                "refusing learned failure rules: %s is not private", target
            )
            return cls(())
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("learned failure rules unreadable: %s", target)
            return cls(())
        return cls(_parse_learned_rules(payload))

    def approved_rule(
        self,
        provider: str,
        signature_id: str,
        normalized_text: str = "",
    ) -> LearnedRule | None:
        """The approved rule that covers this failure, if one does.

        A rule acts only when it carries its approval metadata, so a rule
        that is missing any of it is a candidate and is not returned here.

        A learned rule may not name ``auth_failed``. That verdict is the one
        a human has to make -- a Worker is not re-authenticated by a rule
        that a rule learned from a mistake -- so a rule that asks for it is
        skipped rather than obeyed.
        """
        key = _canonical_key(provider)
        for rule in self._rules:
            if rule.effective_status != "approved":
                continue
            if rule.failure_class is FailureClass.AUTH_FAILED:
                continue
            if _rule_covers(rule, key, signature_id, normalized_text):
                return rule
        return None

    def shadow_rules(
        self,
        provider: str,
        signature_id: str,
        normalized_text: str = "",
    ) -> tuple[LearnedRule, ...]:
        """The candidate rules that would cover this failure.

        A candidate is not approved yet, so it may not change anything. It
        may say what it would have said, so the learning loop can see
        whether it was right. A rule that says it is approved but carries
        no approval metadata is a candidate too, and is shadowed here.
        """
        key = _canonical_key(provider)
        return tuple(
            rule
            for rule in self._rules
            if rule.effective_status == "candidate"
            and _rule_covers(rule, key, signature_id, normalized_text)
        )


def _read_stdin() -> str:
    """Read the diagnostic from stdin, tolerating any byte sequence."""
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is not None:
        try:
            return buffer.read().decode("utf-8", errors="replace")
        except (OSError, ValueError):
            return ""
    try:
        return sys.stdin.read()
    except (OSError, UnicodeDecodeError):
        return ""


def main(argv: Sequence[str] | None = None) -> int:
    """Print one JSON line: the class, the reason code and the delay.

    The diagnostic is read and dropped; only the three fields are printed.
    """
    parser = argparse.ArgumentParser(
        prog="python -m src.llms.native_cli.failure_cli",
        description=(
            "Classify a Worker diagnostic into the fixed failure vocabulary. "
            "The diagnostic is read and discarded; only the class, the "
            "reason code and the Retry-After delay reach stdout."
        ),
    )
    parser.add_argument("--provider", required=True, help="provider key, e.g. kimi")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--diagnostic-file",
        help="file holding the diagnostic; '-' reads stdin",
    )
    source.add_argument("--text", help="the diagnostic itself")
    parser.add_argument("--exit-code", type=int, default=None, help="the Worker's exit code")
    parser.add_argument(
        "--ruleset",
        default=None,
        help=(
            "the file the approved learned rules live in; "
            f"the default is {default_rules_path()}"
        ),
    )
    parser.add_argument(
        "--no-learned-rules",
        action="store_true",
        help="ignore the learned rules and use the built-in ones only",
    )
    args = parser.parse_args(argv)

    # The command's contract is one line on stdout and nothing on stderr, so
    # the module's own logging is silenced here. A library caller that wants
    # the log line still gets it.
    logger.addHandler(logging.NullHandler())

    if args.text is not None:
        text = args.text
    elif args.diagnostic_file == "-":
        text = _read_stdin()
    else:
        try:
            text = Path(args.diagnostic_file).read_text(encoding="utf-8", errors="replace")
        except OSError:
            # The path is the caller's, but the file may hold the diagnostic,
            # so nothing about it is echoed.
            print("failure: cannot read diagnostic file", file=sys.stderr)
            return 2

    # The approved rules are read on every call, so a rule that was approved
    # since the last failure takes effect without a restart.
    ruleset = None if args.no_learned_rules else RuleSet.load(args.ruleset)

    classification = classify_failure(
        args.provider, text, exit_code=args.exit_code, ruleset=ruleset
    )
    print(
        json.dumps(
            {
                "failure_class": classification.failure_class.value,
                "reason_code": classification.reason_code,
                "retry_after_seconds": classification.retry_after_seconds,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
