"""The endpoint device's own authority boundary for a BYOM local daemon.

A compromised cloud worker can ask a daemon for anything the daemon's API
surface allows. The device therefore keeps its own policy file: the
operations it recognises, which of them each provider may run, which of them
only read, and a version a lease is bound to. The cloud never relaxes this
file; it only learns the version, so a lease granted under a policy the
device has since replaced can be refused before the native CLI runs.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import shutil
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_POLICY_PATH = Path.home() / ".config" / "conductor" / "daemon_policy.yaml"
POLICY_FORMAT = 1


class PolicyError(ValueError):
    """The policy file is missing, unreadable, or not a valid policy."""


@dataclass(frozen=True)
class OperationRule:
    """One operation and the request patterns that imply it."""

    operation: str
    patterns: tuple[re.Pattern[str], ...]

    def matches(self, request: str) -> bool:
        return any(pattern.search(request) for pattern in self.patterns)


@dataclass(frozen=True)
class ProviderPolicy:
    """What one provider may do on this device."""

    provider: str
    read_only: bool = False
    allow_unclassified: bool = False
    allow: frozenset[str] = frozenset()
    deny: frozenset[str] = frozenset()


@dataclass(frozen=True)
class PolicyDecision:
    """The verdict for one Ticket request."""

    allowed: bool
    reason: str = ""
    operations: tuple[str, ...] = ()


@dataclass(frozen=True)
class DaemonPolicy:
    """A loaded policy file and the version a lease is bound to."""

    path: Path
    version: str
    read_only: bool = False
    read_only_operations: frozenset[str] = frozenset()
    operations: tuple[OperationRule, ...] = ()
    providers: Mapping[str, ProviderPolicy] = field(default_factory=dict)

    def provider_policy(self, provider: str) -> ProviderPolicy | None:
        return self.providers.get(str(provider or "").strip().lower())

    def is_read_only(self, provider: str, *, speculative: bool = False) -> bool:
        """Whether this run must not change the device.

        The restriction is the strictest of the three the device can impose:
        a read-only policy file, a read-only provider entry, and a
        speculative Ticket. The daemon has to enforce the same rule the
        verdict was taken under, not only the Ticket's own flag.
        """
        rules = self.provider_policy(provider)
        provider_read_only = bool(rules.read_only) if rules is not None else False
        return bool(self.read_only or provider_read_only or speculative)

    def evaluate_operations(
        self, provider: str, operations: Iterable[str], *, speculative: bool = False
    ) -> PolicyDecision:
        """Whether these operations may run under this policy on this device.

        The verdict for a request that is already classified: the same deny,
        allowlist and read-only rules, without the "names no operation" rule,
        which belongs to a request and not to a set of operations.
        """
        rules = self.provider_policy(provider)
        if rules is None:
            return PolicyDecision(
                False,
                f"provider {provider.strip().lower() or '(unset)'} has no entry in the "
                "local policy",
            )
        detected = {str(operation) for operation in operations}
        blocked = sorted(detected & rules.deny)
        if blocked:
            return PolicyDecision(
                False,
                f"operation {', '.join(blocked)} is denied for provider {rules.provider}",
                tuple(sorted(detected)),
            )
        out_of_scope = sorted(detected - rules.allow)
        if out_of_scope:
            return PolicyDecision(
                False,
                f"operation {', '.join(out_of_scope)} is not in the allowlist for "
                f"provider {rules.provider}",
                tuple(sorted(detected)),
            )
        if self.is_read_only(provider, speculative=speculative):
            mutating = sorted(
                operation
                for operation in detected
                if operation not in self.read_only_operations
            )
            if mutating:
                return PolicyDecision(
                    False,
                    f"operation {', '.join(mutating)} may change the device and this "
                    "run is read-only",
                    tuple(sorted(detected)),
                )
        return PolicyDecision(True, "", tuple(sorted(detected)))

    def allowed_operations(
        self, provider: str, *, speculative: bool = False
    ) -> frozenset[str] | None:
        """The operations a capability may name for this run.

        A capability is a subset of what the device already allows. A
        read-only run, including every speculative Ticket, cannot be granted
        an operation that may change the device.
        """
        rules = self.provider_policy(provider)
        if rules is None:
            return None
        allowed = set(rules.allow - rules.deny)
        if self.read_only or rules.read_only or speculative:
            allowed &= set(self.read_only_operations)
        return frozenset(allowed)

    def evaluate(
        self, provider: str, request: str, *, speculative: bool = False
    ) -> PolicyDecision:
        """Whether this request may run under this policy on this device.

        A request names the operations it implies, and every one of them has
        to be permitted. A request that names no operation at all is refused
        unless the provider allows unclassified work.
        """
        rules = self.provider_policy(provider)
        if rules is None:
            return PolicyDecision(
                False,
                f"provider {provider.strip().lower() or '(unset)'} has no entry in the "
                "local policy",
            )
        detected = classify(request, self.operations)
        decision = self.evaluate_operations(provider, detected, speculative=speculative)
        if not decision.allowed:
            return decision
        if not detected and not rules.allow_unclassified:
            return PolicyDecision(
                False,
                f"the request matches no operation allowed for provider {rules.provider}",
            )
        return decision


def classify(request: str, rules: Iterable[OperationRule]) -> set[str]:
    """The operations a request text implies."""
    text = request or ""
    return {rule.operation for rule in rules if rule.matches(text)}


# Text the CLI executes is not only prose: it may carry shell command lines.
# The policy vocabulary names operations, not commands, so it can only speak
# about a command segment it recognises. A segment that starts like a command
# but names no operation would reach the adapter unclassified, and that is a
# fail-closed case, not an allowed one.
#
# The parser below is deliberately small and conservative: comments are
# dropped outside quotes, a command line is split on shell control operators,
# control-flow/grouping keywords are command positions, env-assignment
# prefixes are skipped to find the command word, quoted data does not
# authorise an operation, and command substitutions inside a command line are
# checked as their own command lists. Unsupported or unclosed syntax (an
# unbalanced quote or substitution) refuses the line. Prose lines (first word
# is not a command) are left alone: a path reference in an instruction is not
# an executable segment.
_SHELL_SEPARATORS = re.compile(r"&&|\|\||>>|>|<|[&|;]")
_NESTED_COMMAND = re.compile(r"\$\((?P<dollar>[^()]*)\)|`(?P<tick>[^`]*)`")
_LINE_MARKERS = re.compile(r"^[\s>*\-\u2022]*(?:\d+[.)]\s*)?")
_WRAPPING = "\"'`()[]{}:,$"
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SHELL_BUILTINS = frozenset(
    {
        "alias", "bg", "break", "builtin", "caller", "cd", "command", "compgen",
        "complete", "continue", "declare", "dirs", "disown", "echo", "enable",
        "eval", "exec", "exit", "export", "false", "fg", "getopts", "hash",
        "help", "history", "jobs", "kill", "let", "local", "logout", "mapfile",
        "popd", "printf", "pushd", "pwd", "read", "readarray", "return",
        "select", "set", "shift", "shopt", "source", "suspend", "test", "times",
        "trap", "true", "type", "ulimit", "umask", "unalias", "unset", "wait",
    }
)
# A command position can start with shell syntax rather than a program. These
# are executable structure: the segment after them is a command of its own.
_COMPOUND_KEYWORDS = frozenset(
    {
        "{", "}", "!", "(", ")", "[[", "]]",
        "if", "then", "elif", "else", "fi",
        "for", "while", "until", "do", "done",
        "case", "esac", "select", "function", "time", "coproc",
    }
)


def _strip_comment(line: str) -> str:
    """Drop a shell comment: ``#`` at line start or after whitespace, unquoted."""
    quote: str | None = None
    escaped = False
    for index, character in enumerate(line):
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if quote is not None:
            if character == quote:
                quote = None
            continue
        if character in {"'", '"'}:
            quote = character
            continue
        if character == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index]
    return line


def _unquote(segment: str) -> str | None:
    """The segment with quoted data removed, or None when quoting is unclosed.

    Quoted data is argument text, not a command name: it may not authorise an
    operation. An unclosed quote is unsupported syntax and refuses the line.
    """
    output: list[str] = []
    quote: str | None = None
    escaped = False
    for character in segment:
        if escaped:
            if quote is None:
                output.append(character)
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if quote is not None:
            if character == quote:
                quote = None
            continue
        if character in {"'", '"'}:
            quote = character
            continue
        output.append(character)
    if quote is not None or escaped:
        return None
    return "".join(output)


def _split_commands(line: str) -> tuple[str, ...]:
    """The command-sized pieces of one line: list markers stripped."""
    pieces: list[str] = []
    for piece in _SHELL_SEPARATORS.split(line):
        cleaned = _LINE_MARKERS.sub("", piece).strip()
        if cleaned:
            pieces.append(cleaned)
    return tuple(pieces)


def _first_word(segment: str) -> str:
    """The first shell word, skipping env assignments (``X=1 cmd``)."""
    tokens = segment.split()
    index = 0
    while index < len(tokens) and _ASSIGNMENT.match(tokens[index]):
        index += 1
    return tokens[index] if index < len(tokens) else ""


def _looks_like_command(segment: str) -> bool:
    """Whether the segment starts at a command position.

    Compound/control keywords start executable structure. Otherwise a shell
    builtin, an executable on PATH, or a path-like first word starts a command.
    Prose ("Do not modify files") does not: its first word is none of these, so
    the daemon does not try to read it as a command.
    """
    raw = _first_word(segment)
    if raw in _COMPOUND_KEYWORDS:
        return True
    token = raw.strip(_WRAPPING)
    if not token or token in {"-", "--"}:
        return False
    if "/" in token or token.startswith((".", "~")):
        return True
    if token in _SHELL_BUILTINS:
        return True
    return shutil.which(token) is not None


def _unclassified(segment: str, rules: Iterable[OperationRule]) -> bool:
    """True when the segment is executable but names no operation of the policy.

    An operation is authorised by the command text and its unquoted words, not
    by quoted argument data: ``rm -rf . 'git status'`` runs no git command.
    """
    visible = _unquote(segment)
    if visible is None:
        return True
    return not classify(visible, rules)


def _nested_commands(line: str) -> tuple[str, ...]:
    """Command lists inside ``$(...)`` and backtick substitutions."""
    nested: list[str] = []
    for match in _NESTED_COMMAND.finditer(line):
        content = match.group("dollar")
        if content is None:
            content = match.group("tick") or ""
        nested.extend(_split_commands(_strip_comment(content)))
    return tuple(nested)


def unclassified_executable_segments(
    request: str, rules: Iterable[OperationRule]
) -> tuple[str, ...]:
    """Executable segments the policy names no operation for.

    The device refuses these instead of handing the CLI text the local policy
    cannot classify. Prose is not executable and is left to the operation
    patterns; a command the policy does name is judged by ``evaluate``.
    Command substitutions inside a command line are checked as their own
    commands; unsupported or unclosed syntax refuses the line.
    """
    found: list[str] = []
    for raw_line in str(request or "").splitlines():
        line = _strip_comment(raw_line)
        if not line.strip():
            continue
        segments = _split_commands(line)
        if not any(
            _looks_like_command(segment) or _looks_like_command(_unquote(segment) or "")
            for segment in segments
        ):
            # A prose line: inline path references and emphasis are not
            # commands, so nothing on it is handed to a shell by construction.
            continue
        if _unquote(line) is None:
            found.append(line.strip())
            continue
        for segment in segments:
            if _looks_like_command(segment) and _unclassified(segment, rules):
                found.append(segment)
        remainder = _NESTED_COMMAND.sub(" ", line)
        if "`" in remainder or "$(" in remainder:
            found.append(line.strip())
            continue
        for segment in _nested_commands(line):
            if _looks_like_command(segment) and _unclassified(segment, rules):
                found.append(segment)
    return tuple(found)


# ---------------------------------------------------------------------------
# Executable content
# ---------------------------------------------------------------------------
# The launch prompt and the Ticket text are prose, but they may also carry
# shell command lines. The policy vocabulary names operations ("git status"),
# not arbitrary commands, so the daemon authorizes each command by its
# *signature*: the normalized command word and its first subcommand word.
# Argument text never authorizes anything. Shell words are decoded the way the
# shell would (quote concatenation, backslash escapes), unsupported syntax
# (redirection) is refused, and the resulting operations are judged by the
# same allow/deny/read-only rules as the request. The parser is deliberately
# conservative: prose lines are skipped, and anything it cannot decode is a
# refusal.
_SHELL_OPERATORS = frozenset({";", "&&", "||", "|", "&", "(", ")"})


def _shell_words(text: str) -> list[str] | None:
    """Decode shell words: quote concatenation, escapes; None when unclosed.

    ``r"m"`` decodes to ``rm`` and ``p""ush`` to ``push``, exactly as the
    shell would run them, so the signature is the real executable.
    """
    words: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    for character in text:
        if escaped:
            current.append(character)
            escaped = False
            continue
        if quote == "'":
            if character == "'":
                quote = None
            else:
                current.append(character)
            continue
        if character == "\\":
            escaped = True
            continue
        if quote == '"':
            if character == '"':
                quote = None
            else:
                current.append(character)
            continue
        if character in {"'", '"'}:
            quote = character
            continue
        if character.isspace():
            if current:
                words.append("".join(current))
                current = []
            continue
        current.append(character)
    if quote is not None or escaped:
        return None
    if current:
        words.append("".join(current))
    return words


def _unquoted_redirection(line: str) -> bool:
    """True when the line carries a shell redirection outside quotes.

    Any unquoted ``>``/``<`` counts, however it is spaced: ``git status>victim``
    is the same write effect as ``git status > victim``. Quoted operators
    (``grep '>'``) and prose placeholders inside a prose line are handled by the
    caller (prose is not shell text; placeholders in prose are left alone).
    """
    quote: str | None = None
    escaped = False
    for character in line:
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if quote is not None:
            if character == quote:
                quote = None
            continue
        if character in {"'", '"'}:
            quote = character
            continue
        if character in {">", "<"}:
            return True
    return False


def _nested_texts(line: str) -> tuple[tuple[str, ...], bool]:
    """Command text inside $(...) and backticks; second item is closed-ness."""
    found: list[str] = []
    index = 0
    closed = True
    while index < len(line):
        if line.startswith("$(", index):
            depth = 1
            end = index + 2
            while end < len(line) and depth:
                if line[end] == "(":
                    depth += 1
                elif line[end] == ")":
                    depth -= 1
                end += 1
            if depth:
                closed = False
                break
            found.append(line[index + 2 : end - 1])
            line = line[:index] + " " + line[end:]
            index = 0
            continue
        if line[index] == "`":
            end = line.find("`", index + 1)
            if end == -1:
                closed = False
                break
            found.append(line[index + 1 : end])
            line = line[:index] + " " + line[end + 1 :]
            index = 0
            continue
        index += 1
    return tuple(found), closed


def _is_command_word(word: str) -> bool:
    if word in _COMPOUND_KEYWORDS:
        return True
    if "/" in word or word.startswith((".", "~")):
        return True
    if word in _SHELL_BUILTINS:
        return True
    return shutil.which(word) is not None


def _signature(words: list[str]) -> tuple[str, ...]:
    """The command word plus its non-argument continuation.

    Argument data never authorizes: a flag (``-rf``), a path (``./victim``), an
    expansion (``$HOME``) or an assignment stops the signature at the command
    and its subcommand words. ``write the file`` stays a signature because
    none of its words is argument-shaped; ``rm -rf ./victim git status`` stops
    after ``rm``.
    """
    index = 0
    while index < len(words) and _ASSIGNMENT.match(words[index]):
        index += 1
    if index >= len(words):
        return ()
    command = words[index]
    if command in _COMPOUND_KEYWORDS:
        return ()
    if not _is_command_word(command):
        return ()
    signature = [command]
    for candidate in words[index + 1 :]:
        if (
            candidate.startswith("-")
            or candidate.startswith("$")
            or "=" in candidate
            or "/" in candidate
            or candidate.startswith((".", "~"))
            or candidate in _SHELL_OPERATORS
            or candidate in _COMPOUND_KEYWORDS
        ):
            break
        signature.append(candidate)
    return tuple(signature)


def _classify_command(
    signature: tuple[str, ...], rules: Iterable[OperationRule]
) -> set[str]:
    """Operations a command signature names at its start.

    A pattern that matches only later argument text does not authorize the
    command: ``echo git status`` does not become ``repo.read``.
    """
    text = " ".join(signature)
    detected: set[str] = set()
    for rule in rules:
        for pattern in rule.patterns:
            match = pattern.search(text)
            if match is not None and match.start() == 0:
                detected.add(rule.operation)
                break
    return detected


_WORD_CANDIDATE = re.compile(r"[^\s;&|()]+")
_WRAP_CANDIDATES = "\"'`()[]{}:,$"



_LIST_PREFIX = re.compile(r"^\s*(?:[-*+\u2022]+\s+|\d+[.)]\s+)")


def _strip_list_prefix(text: str) -> str:
    """Drop a leading list/numbering prefix before any tokenization.

    Markers are removed from the raw text, not from tokens: parentheses in
    ``1)`` are token separators for the word scanner, so a token-level
    matcher would miss that form.
    """
    stripped = text
    while True:
        updated = _LIST_PREFIX.sub("", stripped, count=1)
        if updated == stripped:
            return stripped
        stripped = updated


_LIST_MARKER_TOKENS = frozenset({"-", "*", "+", "\u2022", ">", ">>"})
_NUMBERED_MARKER = re.compile(r"^\d+[.)]$")


def _strip_leading_markers(tokens: list[str]) -> list[str]:
    """Drop list/numbering markers and env assignments before a command word."""
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if (
            token in _LIST_MARKER_TOKENS
            or token in _COMPOUND_KEYWORDS
            or _NUMBERED_MARKER.match(token)
            or _ASSIGNMENT.match(token)
        ):
            index += 1
            continue
        break
    return tokens[index:]


def _normalized_candidate(token: str) -> str:
    """A detection-only spelling: strip quoting and escape backslashes."""
    candidate = token.strip(_WRAP_CANDIDATES)
    candidate = candidate.replace("\\", "").replace("'", "").replace('"', "")
    return candidate.strip(_WRAP_CANDIDATES)


def _lenient_command_position(text: str) -> bool:
    """Whether a command-sized piece starts at a command position.

    Lenient: quotes and escapes are decoded for detection only, leading
    compound/control keywords and assignments are skipped, and a token that
    starts with an expansion (``$``) or substitution counts as a command
    position so the strict pass can refuse it. Prose apostrophes ("the
    Ticket's request") are stripped as word-edge quoting and stay prose.
    """
    for piece in re.split(r"(?<![&|])&&|\|\||[;&|]", text):
        raw_tokens = _strip_leading_markers(
            [
                token
                for token in _WORD_CANDIDATE.findall(_strip_list_prefix(piece))
                if token
            ]
        )
        if not raw_tokens:
            continue
        raw_head = raw_tokens[0]
        # An expansion or substitution at command position is executable
        # structure: the strict pass refuses it. This is checked on the raw
        # token with any leading quoting stripped, so `"${SHELL}"` and
        # `"$(cmd)"` are not mistaken for a quoted argument.
        unquoted_head = raw_head.lstrip("\"'")
        if (
            raw_head.startswith("`")
            or unquoted_head.startswith(("$", "`"))
            or "$(" in unquoted_head
        ):
            return True
        head = _normalized_candidate(raw_head)
        if _is_command_word(head):
            return True
    return False


def _command_rejection(
    policy: DaemonPolicy,
    provider: str,
    command_text: str,
    *,
    speculative: bool,
) -> str:
    words = _shell_words(_strip_list_prefix(command_text))
    if words is None:
        return "unsupported shell quoting"
    if not words:
        return ""
    if any(word in _COMPOUND_KEYWORDS for word in words):
        return "unsupported shell compound command"
    signature = _signature(_strip_leading_markers(words))
    if not signature:
        return (
            "an unresolved command word (the policy needs a plain executable "
            "name it can classify)"
        )
    detected = _classify_command(signature, policy.operations)
    if not detected:
        return (
            "executable content the local policy cannot classify "
            f"({command_text.strip()[:120]})"
        )
    decision = policy.evaluate_operations(provider, detected, speculative=speculative)
    if not decision.allowed:
        return f"executable content is out of scope: {decision.reason}"
    return ""


def executable_request_rejection(
    policy: DaemonPolicy,
    provider: str,
    request: str,
    *,
    speculative: bool = False,
) -> str:
    """Why executable content in this text may not run; empty when it may.

    Prose lines are not commands and are left to the operation patterns. A
    command line is parsed the way a shell would read it and must be a plain,
    fully resolved command list: expansions, substitutions, compound/control
    syntax and redirection are unsupported and refuse the line. Each command
    is authorized by its normalized signature under the same allow, deny and
    read-only rules as the request.
    """
    for raw_line in str(request or "").splitlines():
        line = _strip_comment(raw_line)
        if not line.strip():
            continue
        has_command = _lenient_command_position(line)
        starts_with_redirection = bool(
            re.match(r"^\s*(?:\d+)?[<>]", line)
        )
        if not has_command:
            # An operator can be attached to the executable (`git>victim`), so
            # command detection also looks at the pieces on both sides of the
            # redirection before deciding the line is prose.
            has_command = any(
                _lenient_command_position(part)
                for part in re.split(r"[<>]", line)
                if part.strip()
            )
        # A redirection is a write effect the policy does not name. It is
        # refused before the prose exemption for a command line or when the
        # line starts with the redirection itself: an otherwise plain line can
        # truncate a file without naming an executable.
        if _unquoted_redirection(line) and (has_command or starts_with_redirection):
            return (
                "unsupported shell redirection (a policy-backed run does not "
                "carry unwritten redirection effects)"
            )
        if not has_command:
            # A prose line: inline path references, emphasis, placeholders and
            # apostrophes are not commands, so nothing there is handed to a
            # shell.
            continue
        if "`" in line or "\\" in line or "$" in line:
            return (
                "unsupported shell expansion or substitution (the policy "
                "authorizes plain command words only)"
            )
        if _shell_words(line) is None:
            return "unsupported shell quoting"
        pieces = re.split(r"(?<![&|])&&|\|\||[;&|]", line)
        for piece in pieces:
            if not piece.strip():
                continue
            rejection = _command_rejection(
                policy, provider, piece, speculative=speculative
            )
            if rejection:
                return rejection
    return ""


def request_operations(
    rules: Iterable[OperationRule], request: str
) -> set[str]:
    """Every operation a text names, including normalized command signatures.

    The raw classifier catches operation names in prose and argument text (the
    Ticket's own request is prose). Command lines add the operations their
    normalized shell signature names, so ``git "status"`` is ``repo.read`` for
    capability and result checks even though the raw regex does not match it.
    Unsupported shell syntax contributes only the raw classification: callers
    that authorize executable content refuse it separately.
    """
    operations = set(classify(request, rules))
    for raw_line in str(request or "").splitlines():
        line = _strip_comment(raw_line)
        if not line.strip() or not _lenient_command_position(line):
            continue
        if (
            _unquoted_redirection(line)
            or "`" in line
            or "\\" in line
            or "$" in line
        ):
            continue
        for piece in re.split(r"(?<![&|])&&|\|\||[;&|]", line):
            if not piece.strip():
                continue
            words = _shell_words(_strip_list_prefix(piece))
            if words is None:
                continue
            words = _strip_leading_markers(
                [word for word in words if word not in _COMPOUND_KEYWORDS]
            )
            signature = _signature(words)
            if signature:
                operations.update(_classify_command(signature, rules))
    return operations


def executable_free_operations(
    rules: Iterable[OperationRule], request: str
) -> set[str] | None:
    """Operations a text names, or None when it carries unresolved shell syntax.

    Like :func:`request_operations`, but a command line the daemon cannot fully
    resolve (substitution, expansion, redirection, unclosed quoting, compound
    syntax, unresolved command word) makes the whole text unclassifiable. That
    matters for untrusted output: a filter that would otherwise drop a
    normalized operation must refuse the text instead of accepting an
    incomplete operation set.
    """
    operations = set(classify(request, rules))
    for raw_line in str(request or "").splitlines():
        line = _strip_comment(raw_line)
        if not line.strip():
            continue
        has_command = _lenient_command_position(line)
        if not has_command:
            has_command = any(
                _lenient_command_position(part)
                for part in re.split(r"[<>]", line)
                if part.strip()
            )
        if _unquoted_redirection(line):
            if has_command or re.match(r"^\s*(?:\d+)?[<>]", line):
                return None
            continue
        if not has_command:
            continue
        if "`" in line or "\\" in line or "$" in line:
            return None
        for piece in re.split(r"(?<![&|])&&|\|\||[;&|]", line):
            if not piece.strip():
                continue
            words = _shell_words(_strip_list_prefix(piece))
            if words is None:
                return None
            if any(word in _COMPOUND_KEYWORDS for word in words):
                return None
            words = _strip_leading_markers(
                [word for word in words if word not in _COMPOUND_KEYWORDS]
            )
            signature = _signature(words)
            if not signature:
                return None
            detected = _classify_command(signature, rules)
            if not detected:
                # An executable segment the policy names no operation for is
                # unclassifiable: callers must refuse the text, not accept the
                # incomplete operation set.
                return None
            operations.update(detected)
    return operations


# ---------------------------------------------------------------------------
# Result text (N71)
# ---------------------------------------------------------------------------
# A native CLI's result is the model's report, mostly prose. The request-side
# parser above reads any line whose first word resolves on PATH as a command
# line, which is right for text the CLI executes but wrong for a report:
# "Make sure the tests pass" or "Find the config and update it" resolve on a
# case-insensitive file system and would make a prose report unclassifiable.
#
# A result line is executable content when it is command-shaped: inside a
# shell code fence, behind a "$ " prompt, an inline code span that reads as a
# command, or a line (also inside a plain or text fence) whose first word is
# a command as typed (exact case) and which does not read as a sentence. A
# fence in a programming language (python, js, ...) is executable content
# too: a line that runs a process or deletes files there is unclassifiable.
# Everything else is prose and contributes only the operations the patterns
# name in it; a real sentence never refuses a result.
_FENCE = re.compile(r"^\s*(```+|~~~+)\s*([A-Za-z0-9_+#-]*)")
_SHELL_FENCES = frozenset(
    {
        "sh", "bash", "zsh", "shell", "ksh", "fish", "dash", "csh", "tcsh",
        "powershell", "pwsh", "ps1", "bat", "batch", "cmd",
    }
)
# Untyped and text fences hold whatever the model pasted: prompts and bare
# command lines in them are checked like prose lines, strictly.
_TEXT_FENCES = frozenset(
    {
        "", "text", "txt", "plaintext", "plain", "console", "output",
        "terminal", "shell-session", "log",
    }
)
_CODE_FENCES = frozenset(
    {
        "python", "py", "python3", "py3", "ipython", "pycon",
        "javascript", "js", "node", "mjs", "cjs", "typescript", "ts", "jsx",
        "tsx", "ruby", "rb", "perl", "pl", "php", "go", "golang", "rust",
        "rs", "java", "kotlin", "kt", "c", "cpp", "c++", "csharp", "cs", "c#",
        "lua", "r", "swift", "scala", "groovy", "julia", "elixir", "exs",
        "erlang", "tcl", "awk", "applescript", "osascript",
    }
)
# Code that runs a process, evaluates a string or removes files. Matching is
# deliberately broad: a code fence is not prose, so a false positive costs a
# refused result in enforce, never a forwarded command.
_CODE_EXECUTION = re.compile(
    r"""
    \b(?:os|posix|nt)\s*\.\s*(?:system|popen\w*|exec\w*|spawn\w*|posix_spawn\w*
        |fork\w*|kill\w*|remove|unlink|rmdir|removedirs|chmod|chown|lchown
        |truncate)\s*\(
    | \bsubprocess\b | \bpty\s*\.\s*spawn | \bcommands\s*\.\s*getoutput
    | \bshutil\s*\.\s*(?:rmtree|move|chown)\s*\(
    | \.\s*(?:unlink|rmdir|rmtree)\s*\(
    | (?<![\w.])(?:eval|exec|execfile|__import__|spawn|system|popen|shell_exec
        |passthru|proc_open|pcntl_exec|execSync|execFileSync|spawnSync
        |execFile)\s*\(
    | \bchild_process\b | \bexec\s*\.\s*Command\w*\s*\(
    | \bCommand\s*::\s*new\s*\( | \bRuntime\s*\.\s*getRuntime\b
    | \bProcessBuilder\b | %x[\[{(] | \bIO\s*\.\s*popen | \bOpen3\b
    | \bKernel\s*\.\s*(?:system|exec|spawn)
    | \bfs\s*\.\s*(?:rm|rmdir|unlink)\w*\s*\(
    | \bdo\s+shell\s+script\b
    | \bimport\s+(?:subprocess|pty)\b | \bfrom\s+(?:subprocess|pty)\s+import\b
    | \bfrom\s+os\s+import\b[^\n#]*\b(?:system|popen|exec\w*|spawn\w*|remove
        |unlink|rmdir)\b
    | \bfrom\s+shutil\s+import\b[^\n#]*\brmtree\b
    """,
    re.VERBOSE,
)
_PROMPT = re.compile(r"^\s*[$%#]\s+")
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
# Tokens only a command line carries: flags, quoting, expansion, assignment,
# absolute/relative/home paths. Any one of them makes the line a command.
_STRONG_ARGUMENT = re.compile(
    r"""^-{1,2}[A-Za-z0-9]|^["'$]|[=$`]|^(?:/|\./|\.\./|~)"""
)
# Tokens that read as an argument (a file name, host, number, mode, path)
# but also occur in prose: they count only when the line is not a sentence.
_WEAK_ARGUMENT = re.compile(r"\w\.\w|^\d+$|\w/\w|^\+|^@")
_CHAIN = re.compile(r"&&|\|\||[|;]")
_SENTENCE_END = re.compile(r"[.!?:,]['\")\]]*$")
_PROSE_WORDS = frozenset(
    """
    a an the this that these those it its is are was were be been being am
    has have had do does did will would shall should can could may might must
    and or but nor so yet not no nor of to in on at by for from with without
    into onto over under about after before again also still now then than
    as if when while because since until unless though although which who
    whom whose what where why how all any each every some most more less
    i we you he she they me us him her them my our your their mine ours
    there here very just only even ever never always already too well
    pass passes passed fail fails failed work works worked green clean
    fine ok okay done unchanged changed fixed updated added removed ready
    """.split()
)
_PROSE_SUFFIX = re.compile(r"^[a-z]{3,}(?:ed|ing|ly)$")
# Commands checked by name, so the answer does not depend on the host PATH.
_KNOWN_COMMANDS = frozenset(
    {
        "rm", "rmdir", "mv", "cp", "dd", "ln", "chmod", "chown", "chgrp",
        "chflags", "sudo", "su", "doas", "curl", "wget", "nc", "ncat",
        "netcat", "ssh", "scp", "sftp", "rsync", "ftp", "telnet", "python",
        "python3", "python2", "perl", "ruby", "node", "deno", "bun", "php",
        "bash", "sh", "zsh", "ksh", "fish", "dash", "kill", "killall",
        "pkill", "reboot", "shutdown", "halt", "poweroff", "mkfs", "shred",
        "truncate", "git", "make", "pip", "pip3", "npm", "npx", "yarn",
        "pnpm", "uv", "uvx", "docker", "kubectl", "crontab", "launchctl",
        "systemctl", "osascript", "tee", "xargs", "env", "nohup", "sed",
        "awk", "tar", "unzip", "zip", "base64", "openssl", "security",
        "defaults", "diskutil", "pytest", "ls", "cat", "touch", "mkdir",
    }
)


@dataclass(frozen=True)
class ResultClassification:
    """What a result text names: operations, and unclassifiable commands.

    ``unclassified`` holds the executable fragments the policy names no
    operation for (or cannot resolve). Callers decide whether that refuses
    the result; they must not echo the fragments into a report.
    """

    operations: frozenset[str]
    unclassified: tuple[str, ...] = ()


def _directory_entries(directory: str) -> frozenset[str]:
    try:
        stamp = os.stat(directory).st_mtime_ns
    except OSError:
        return frozenset()
    return _cached_entries(directory, stamp)


@functools.lru_cache(maxsize=256)
def _cached_entries(directory: str, _stamp: int) -> frozenset[str]:
    try:
        return frozenset(os.listdir(directory))
    except OSError:
        return frozenset()


def _exact_command_word(word: str) -> bool:
    """A command word as typed: exact case, so "Make" in prose is not ``make``.

    ``shutil.which`` answers case-insensitively on a case-insensitive file
    system, so the executable's directory must list the word as spelled.
    """
    if not word:
        return False
    if word in _SHELL_BUILTINS or word in _COMPOUND_KEYWORDS:
        return True
    if word in _KNOWN_COMMANDS:
        return True
    if word.startswith(("/", "./", "../", "~/")):
        return True
    if "/" in word:
        return False
    found = shutil.which(word)
    if found is None:
        return False
    return word in _directory_entries(os.path.dirname(found))


def _prose_word(token: str) -> bool:
    bare = token.strip(".,;:!?\"'()[]{}*_").lower()
    if not bare:
        return False
    return bare in _PROSE_WORDS or bool(_PROSE_SUFFIX.match(bare))


def _command_piece(tokens: list[str], *, sentence_end: bool, span: bool) -> str:
    """'strong', 'weak' or '' for one chain piece whose head is a command.

    Strong: a flag, quoting, expansion, assignment or path argument. Weak: the
    piece does not read as a sentence (no sentence punctuation at the end, no
    ordinary prose words, a label colon nowhere) and is short or carries
    argument-like tokens. A bare command word in a code span is a name.
    """
    arguments = tokens[1:]
    if any(_STRONG_ARGUMENT.search(token) for token in arguments):
        return "strong"
    if sentence_end:
        return ""
    if span and not arguments:
        return ""
    if any(_prose_word(token) or token.endswith(":") for token in arguments):
        return ""
    if len(arguments) <= 2 or any(
        _WEAK_ARGUMENT.search(token) for token in arguments
    ):
        return "weak"
    return ""


def _shell_shaped(line: str, *, span: bool = False) -> bool:
    """Whether a result line outside shell code reads as a command, not prose."""
    text = _strip_list_prefix(_strip_comment(line)).strip()
    if not text:
        return False
    pieces = [piece for piece in _CHAIN.split(text) if piece.strip()]
    command_pieces = 0
    for index, piece in enumerate(pieces):
        tokens = _strip_leading_markers(piece.split())
        if not tokens:
            continue
        head = tokens[0].strip("\"'")
        if not _exact_command_word(head) or head in _COMPOUND_KEYWORDS:
            # Prose like "time to ship; done" starts with keywords only.
            continue
        last = index == len(pieces) - 1
        shape = _command_piece(
            tokens,
            sentence_end=bool(last and _SENTENCE_END.search(piece.strip())),
            span=span,
        )
        if shape:
            return True
        if not any(_prose_word(token) for token in tokens[1:]):
            command_pieces += 1
    if command_pieces and _unquoted_redirection(text):
        return True
    # A chain of two or more command pieces (``git status && ls``) is a
    # command list, not a sentence that happens to use a semicolon.
    return command_pieces >= 2 and len(pieces) >= 2


def classify_result(
    rules: Iterable[OperationRule], text: str
) -> ResultClassification:
    """The operations a CLI result names, and its unclassifiable commands.

    Prose never makes a result unclassifiable. The pattern classifier runs on
    the whole text, so an operation named in prose still counts against the
    capability; executable content is parsed strictly (as the request side
    does) and either adds its normalized operations or is listed as
    unclassified.
    """
    rule_list = tuple(rules)
    operations = set(classify(text, rule_list))
    unclassified: list[str] = []

    def strict(command: str) -> None:
        detected = executable_free_operations(rule_list, command)
        if detected is None:
            unclassified.append(command.strip())
        else:
            operations.update(detected)

    fence: str | None = None
    for raw_line in str(text or "").splitlines():
        marker = _FENCE.match(raw_line)
        if marker is not None:
            fence = None if fence is not None else marker.group(2).lower()
            continue
        if not raw_line.strip():
            continue
        if fence is not None:
            if fence in _SHELL_FENCES:
                strict(_PROMPT.sub("", raw_line, count=1))
            elif fence in _TEXT_FENCES:
                if _PROMPT.match(raw_line):
                    strict(_PROMPT.sub("", raw_line, count=1))
                elif _shell_shaped(raw_line):
                    strict(raw_line)
            elif fence in _CODE_FENCES:
                if _CODE_EXECUTION.search(raw_line):
                    # The policy names operations for shell commands, not
                    # for code that spawns them: unclassifiable.
                    unclassified.append(raw_line.strip())
            # Any other fence is data (JSON, YAML, a diff): the pattern
            # classifier above already saw its text.
            continue
        if _PROMPT.match(raw_line) and raw_line.lstrip()[:1] == "$":
            strict(_PROMPT.sub("", raw_line, count=1))
            continue
        spans = _INLINE_CODE.findall(raw_line)
        for span in spans:
            if _shell_shaped(span, span=True):
                strict(span)
        prose = _INLINE_CODE.sub(" ", raw_line)
        if "`" not in prose and _shell_shaped(prose):
            strict(prose)
    return ResultClassification(frozenset(operations), tuple(unclassified))


_TOP_LEVEL_KEYS = frozenset(
    {"format", "read_only", "read_only_operations", "operations", "providers"}
)
_OPERATION_KEYS = frozenset({"operation", "patterns"})
_PROVIDER_KEYS = frozenset(
    {"read_only", "allow_unclassified", "allow", "deny"}
)


def _reject_unknown_keys(
    mapping: Mapping[Any, Any], allowed: frozenset[str], path: Path | str, label: str
) -> None:
    """Refuse keys the policy model does not know.

    A misspelled restriction (``denyy:``) would otherwise be silently dropped,
    and a policy that still loads is a policy that looks stricter than it is.
    """
    unknown = sorted(str(key) for key in mapping if str(key) not in allowed)
    if unknown:
        raise PolicyError(
            f"{path}: unknown {label} field(s): {', '.join(unknown)}"
        )


def parse_policy(raw: Any, *, path: Path | str = "<policy>") -> DaemonPolicy:
    """Validate a parsed policy document and compute its version."""
    if not isinstance(raw, Mapping):
        raise PolicyError(f"{path}: the policy must be a mapping")
    _reject_unknown_keys(raw, _TOP_LEVEL_KEYS, path, "policy")
    fmt = raw.get("format", POLICY_FORMAT)
    if fmt != POLICY_FORMAT:
        raise PolicyError(f"{path}: unsupported policy format {fmt!r}")
    read_only = _boolean(raw.get("read_only"), path, "read_only")
    operations = _operations(raw.get("operations"), path)
    known = {rule.operation for rule in operations}
    read_only_operations = _names(raw.get("read_only_operations"), path)
    unknown = sorted(read_only_operations - known)
    if unknown:
        raise PolicyError(
            f"{path}: read_only_operations names the unknown operation "
            f"{', '.join(unknown)}"
        )
    providers_raw = raw.get("providers")
    if not isinstance(providers_raw, Mapping) or not providers_raw:
        raise PolicyError(f"{path}: the policy needs a non-empty 'providers' mapping")
    providers: dict[str, ProviderPolicy] = {}
    for name, body in providers_raw.items():
        provider = str(name or "").strip().lower()
        if not provider:
            raise PolicyError(f"{path}: a provider name is empty")
        if provider in providers:
            raise PolicyError(f"{path}: provider {provider} is declared twice")
        providers[provider] = _provider_policy(provider, body, path, known)
    normalized = {
        "format": POLICY_FORMAT,
        "read_only": read_only,
        "read_only_operations": sorted(read_only_operations),
        "operations": {
            rule.operation: sorted(pattern.pattern for pattern in rule.patterns)
            for rule in operations
        },
        "providers": {
            name: {
                "read_only": policy.read_only,
                "allow_unclassified": policy.allow_unclassified,
                "allow": sorted(policy.allow),
                "deny": sorted(policy.deny),
            }
            for name, policy in sorted(providers.items())
        },
    }
    payload = json.dumps(normalized, sort_keys=True, ensure_ascii=True)
    version = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return DaemonPolicy(
        path=Path(path),
        version=version,
        read_only=read_only,
        read_only_operations=frozenset(read_only_operations),
        operations=operations,
        providers=providers,
    )


def _read_policy_snapshot(target: Path) -> tuple[str, tuple[int, int, int]]:
    """Read the policy and stamp it from the same opened file.

    The text and the stamp must describe the same inode: reading the bytes,
    then stat-ing the path separately can pair old rules with a replacement
    file's stamp. Ownership and mode are checked from the descriptor that was
    read, so one file's metadata cannot authorize another file's bytes. If the
    path stops naming the opened file, retry.
    """
    last_error: OSError | None = None
    for _attempt in range(3):
        descriptor: int | None = None
        try:
            descriptor = os.open(
                target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            )
            info = os.fstat(descriptor)
            if info.st_mode & 0o077:
                raise PolicyError(
                    f"the daemon policy at {target} must have mode 0600 or stricter"
                )
            if info.st_uid != os.getuid():
                raise PolicyError(
                    f"the daemon policy at {target} must be owned by the daemon user"
                )
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                descriptor = None
                text = stream.read()
            after = os.stat(target)
            stamp = (info.st_mtime_ns, info.st_size, info.st_ino)
            if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino):
                continue
            return text, stamp
        except PolicyError:
            raise
        except OSError as exc:
            last_error = exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
    raise PolicyError(
        "cannot read the daemon policy at "
        f"{target}: {type(last_error).__name__ if last_error else 'OSError'}"
    )


def _policy_snapshot(target: Path) -> tuple[DaemonPolicy, tuple[int, int, int]]:
    """One policy and its matching stamp, both from the same file snapshot."""
    raw_target = Path(target).expanduser()
    if raw_target.is_symlink():
        raise PolicyError(f"the daemon policy at {raw_target} cannot be a symlink")
    # Resolve the configured path once (operators may use a symlinked parent
    # such as /var on macOS); the policy file itself may not be a symlink.
    target = raw_target.resolve(strict=False)
    if any(parent.is_symlink() for parent in target.parents):
        raise PolicyError(f"the daemon policy at {target} cannot be a symlink")
    text, stamp = _read_policy_snapshot(target)
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PolicyError(
            f"the daemon policy at {target} is not valid YAML: {type(exc).__name__}"
        ) from exc
    return parse_policy(raw, path=target), stamp


def load_policy(path: Path | str) -> DaemonPolicy:
    """Read and validate the policy file on this device.

    The policy is the local authority boundary, so it is read like one: the
    path may not be a symlink, the file must be owned by the daemon user and
    nobody else may write it. The text, its metadata and its version stamp all
    come from the same opened file; a replacement racing the read is retried.
    """
    return _policy_snapshot(Path(path).expanduser())[0]


class PolicyStore:
    """The device's policy file, re-read when it changes on disk.

    A lease is bound to the version it was granted under, so the daemon has to
    notice a policy that was replaced while a Ticket was in flight.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        # The location is resolved and pinned once: reading, change detection
        # and location authorization all use this same path, so a symlinked
        # configuration path cannot be re-pointed between those operations.
        self.resolved_path = self.path.resolve(strict=False)
        self._policy: DaemonPolicy | None = None
        self._stamp: tuple[int, int, int] | None = None

    def load(self) -> DaemonPolicy:
        """Read the policy and its stamp from one file snapshot."""
        policy, stamp = _policy_snapshot(self.resolved_path)
        self._policy = policy
        self._stamp = stamp
        return policy

    def current(self) -> DaemonPolicy:
        """The policy on disk, re-read only when the file changed."""
        stamp = self._stat()
        if self._policy is None or stamp != self._stamp:
            return self.load()
        return self._policy

    def _stat(self) -> tuple[int, int, int] | None:
        try:
            info = self.resolved_path.stat()
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size, info.st_ino)


def _operations(body: Any, path: Path | str) -> tuple[OperationRule, ...]:
    if not isinstance(body, list) or not body:
        raise PolicyError(f"{path}: the policy needs a non-empty 'operations' list")
    rules: list[OperationRule] = []
    seen: set[str] = set()
    for entry in body:
        if not isinstance(entry, Mapping):
            raise PolicyError(f"{path}: every operations entry must be a mapping")
        _reject_unknown_keys(entry, _OPERATION_KEYS, path, "operations entry")
        operation = str(entry.get("operation") or "").strip()
        if not operation:
            raise PolicyError(f"{path}: an operations entry has no operation")
        if operation in seen:
            raise PolicyError(f"{path}: operation {operation} is declared twice")
        seen.add(operation)
        raw_patterns = entry.get("patterns")
        if not isinstance(raw_patterns, list) or not raw_patterns:
            raise PolicyError(
                f"{path}: operation {operation} needs a non-empty 'patterns' list"
            )
        patterns: list[re.Pattern[str]] = []
        for pattern in raw_patterns:
            source = str(pattern or "")
            if not source.strip():
                raise PolicyError(f"{path}: operation {operation} has an empty pattern")
            try:
                patterns.append(re.compile(source, re.IGNORECASE))
            except re.error as exc:
                raise PolicyError(
                    f"{path}: operation {operation} has an invalid pattern: {exc.msg}"
                ) from exc
        rules.append(OperationRule(operation=operation, patterns=tuple(patterns)))
    return tuple(rules)


def _provider_policy(
    provider: str, body: Any, path: Path | str, known: set[str]
) -> ProviderPolicy:
    if not isinstance(body, Mapping):
        raise PolicyError(f"{path}: the rules for provider {provider} must be a mapping")
    _reject_unknown_keys(body, _PROVIDER_KEYS, path, f"provider {provider}")
    read_only = _boolean(body.get("read_only"), path, f"providers.{provider}.read_only")
    allow_unclassified = _boolean(
        body.get("allow_unclassified"),
        path,
        f"providers.{provider}.allow_unclassified",
    )
    allow = _names(body.get("allow"), path, f"providers.{provider}.allow")
    deny = _names(body.get("deny"), path, f"providers.{provider}.deny")
    both = sorted(allow & deny)
    if both:
        raise PolicyError(
            f"{path}: operation {', '.join(both)} is both allowed and denied for "
            f"provider {provider}"
        )
    unknown = sorted((allow | deny) - known)
    if unknown:
        raise PolicyError(
            f"{path}: provider {provider} names the unknown operation "
            f"{', '.join(unknown)}"
        )
    return ProviderPolicy(
        provider=provider,
        read_only=read_only,
        allow_unclassified=allow_unclassified,
        allow=frozenset(allow),
        deny=frozenset(deny),
    )


def _boolean(value: Any, path: Path | str, label: str) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise PolicyError(f"{path}: {label} must be true or false")
    return value


def _names(value: Any, path: Path | str, label: str = "read_only_operations") -> set[str]:
    if value is None:
        return set()
    if not isinstance(value, list):
        raise PolicyError(f"{path}: {label} must be a list")
    names = {str(item or "").strip() for item in value if str(item or "").strip()}
    if any(not str(item or "").strip() for item in value):
        raise PolicyError(f"{path}: {label} holds an empty name")
    return names
