#!/bin/bash
# Dispatch launch script for Conductor development Workers.
#
# Implements the ScriptLauncher seam (src/tools/auto_dispatch/execution.py):
#   dispatch_launch.sh <model> <task> [--skills=<s>] [--impact-map=<p>]
#
# The model is the DISPATCH ROSTER name, not a CLI name. Claude (planner) and
# Codex (auditor) are reserved roles and are refused here: this script only
# starts WORK workers.
#
# No credential is embedded, read or logged here. Each CLI is started with the
# environment it already has; the per-workspace isolation policy supplies the
# provider's own values.
#
# Portable to bash 3.2 (the macOS system bash): no associative arrays and no
# ${var,,}.
#
# The Worker runs in the caller's own process group, on purpose. The
# dispatcher cancels a launch by killing that group (kill_child_group), and
# the worker left in a group of its own survives that kill: it keeps working
# in the worktree while the line is already reassigned. Keeping the Worker in
# the caller's group puts the Worker, the helpers and the CLI's own children
# under one cancellation boundary. A stop of this script's own (the timeout
# and the watchdog) therefore walks the Worker's process tree instead of
# signalling a group, because the caller's group also holds the dispatcher.

set -euo pipefail

# The exit codes the caller dispatches on. The launcher's own codes are the
# ones it has always used; the classification codes are the ones W8a added,
# and they live in a range of their own, so a caller can tell "the worker
# failed because of this" from "the launch was refused".
#
#    0   the worker finished on its own terms
#    64  the launch was refused: bad arguments        (EXIT_USAGE)
#    65  the launch was refused: a reserved role       (EXIT_RESERVED)
#    66  the launch was refused: an unusable line assignment (EXIT_BAD_ASSIGNMENT)
#    75  the worker was rate limited                   (EXIT_RATE_LIMITED)
#    76  the worker's quota is exhausted               (EXIT_QUOTA)
#    77  the worker's login is not valid               (EXIT_AUTH)
#    78  the worker's context is exhausted             (EXIT_CONTEXT)
#    79  the worker failed for another reason          (EXIT_WORKER_FAILED)
#   124 the run was stopped by the timeout or watchdog (EXIT_TIMEOUT)
#   127 the worker's CLI is not installed              (EXIT_NO_BINARY)
EXIT_USAGE=64
EXIT_RESERVED=65
EXIT_BAD_ASSIGNMENT=66
EXIT_TIMEOUT=124
EXIT_NO_BINARY=127

# The classes the failure classifier can name, and the code each one is
# reported as. 79 is the fallback: it says "the worker failed", without
# claiming a class the classifier did not name.
EXIT_RATE_LIMITED=75
EXIT_QUOTA=76
EXIT_AUTH=77
EXIT_CONTEXT=78
EXIT_WORKER_FAILED=79

PROG="$(basename "$0")"

log() {
    printf '%s\n' "$*" >&2
}

usage_error() {
    # The task text is caller-controlled and can be large; only the reason is
    # reported, never the task itself.
    log "$PROG: $1"
    exit "$EXIT_USAGE"
}

# ---------------------------------------------------------------- arguments

if [ "$#" -lt 2 ]; then
    usage_error "expected <model> and <task>"
fi

model="$1"
task="$2"
shift 2

skills=""
impact_map=""
session_id=""
session_mode=""
handoff=""
context_budget=""
handoff_request=""
history_manifest=""
run_log=""

while [ "$#" -gt 0 ]; do
    case "$1" in
        --skills=*)
            skills="${1#--skills=}"
            ;;
        --impact-map=*)
            impact_map="${1#--impact-map=}"
            ;;
        --session-id=*)
            session_id="${1#--session-id=}"
            ;;
        --session-mode=*)
            session_mode="${1#--session-mode=}"
            ;;
        --handoff=*)
            handoff="${1#--handoff=}"
            ;;
        --context-budget=*)
            context_budget="${1#--context-budget=}"
            ;;
        --handoff-request=*)
            handoff_request="${1#--handoff-request=}"
            ;;
        --history-manifest=*)
            history_manifest="${1#--history-manifest=}"
            ;;
        --run-log=*)
            run_log="${1#--run-log=}"
            ;;
        --*)
            usage_error "unknown option"
            ;;
        *)
            usage_error "unexpected argument"
            ;;
    esac
    shift
done

# Backstop for the caller's own check (resumable_session_id): an id must start
# with a letter or digit and hold only letters, digits and . _ : - (at most
# 128 characters), so it can never read as a flag or split into words. Any
# other id is no session at all: the worker runs fresh.
if [ -n "$session_id" ]; then
    if ! [[ "$session_id" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$ ]]; then
        log "$PROG: ignoring an invalid session id; running fresh"
        session_id=""
        session_mode=""
    fi
fi

if [ -n "$session_id" ] || [ -n "$session_mode" ]; then
    [ -n "$session_id" ] || usage_error "session mode requires a session id"
    case "$session_mode" in
        fresh|resume) ;;
        *) usage_error "unknown session mode" ;;
    esac
fi

# ------------------------------------------------------------- model table

# Roster name -> the CLI that serves it. CLI aliases fold onto the roster.
canonical=""
case "$model" in
    kimi)            canonical="kimi" ;;
    glm)             canonical="glm" ;;
    grok)            canonical="grok" ;;
    cursor)          canonical="cursor" ;;
    stepfun)         canonical="stepfun" ;;
    deepseek)        canonical="deepseek" ;;
    minimax)         canonical="minimax" ;;
    doubao)          canonical="doubao" ;;
    # CLI aliases.
    zcode|zcode-cli) canonical="glm" ;;
    step)            canonical="stepfun" ;;
    dsh)             canonical="deepseek" ;;
    mcode)           canonical="minimax" ;;
    cursor-agent)    canonical="cursor" ;;
    arkcli)          canonical="doubao" ;;
    # Reserved roles: planner and auditor, never WORK workers.
    codex|claude|claude-code|codexrev|claudedev)
        log "$PROG: $model is a reserved role, not a WORK worker"
        exit "$EXIT_RESERVED"
        ;;
    *)
        usage_error "unknown model"
        ;;
esac

# ------------------------------------------------------------ sandbox policy
#
# A dispatch workspace may declare a sandbox policy. It is an execution
# boundary, not a label: this script is the process that actually starts a
# WORK worker, so it is the place that has to enforce the value. A WORK line
# writes the checkout, commits and pushes, so ``read-only`` is contradictory
# here and is refused instead of being silently ignored; an unknown policy is
# refused too, because a typo must not widen the sandbox. ``workspace-write``
# is the one policy a write worker can honour (the checkout is the caller's
# cwd and the worker never cd's out of it); an empty value keeps the historical
# behavior for deployments that never set a policy.
sandbox_policy="${CONDUCTOR_DISPATCH_SANDBOX_POLICY:-}"
case "$sandbox_policy" in
    ""|default|workspace-write)
        ;;
    read-only)
        log "$PROG: sandbox policy read-only forbids a WORK worker (it must write the checkout)"
        exit "$EXIT_BAD_ASSIGNMENT"
        ;;
    *)
        log "$PROG: unsupported sandbox policy: $sandbox_policy"
        exit "$EXIT_BAD_ASSIGNMENT"
        ;;
esac

# ------------------------------------------------- per-line assignment (N73 L2)
#
# The dispatcher may choose a line's model id, CLI effort and runtime and
# pass them in the environment. Unset, empty or "default" means today's argv,
# byte for byte; a flag is only added for a value that was actually set.
#
#   CONDUCTOR_LINE_MODEL_ID    a provider model id (GLM-5.3, step-3.7-flash)
#   CONDUCTOR_LINE_CLI_EFFORT  an ALREADY MAPPED CLI value for the provider;
#                              the launcher never translates an effort scale
#   CONDUCTOR_LINE_RUNTIME     native_cli (this script itself, no extra
#                              argument), plus the provider variant PRD 4.1
#                              names: clean-room/official select the GLM
#                              grammar and the matching binary; API key/login
#                              select MiniMax's credential profile. The
#                              prefixed spellings (native_cli:clean-room,
#                              native_cli:glm-clean-room, ...) are accepted
#                              too. A variant for another provider, or any
#                              other spelling, is refused.
#
# A value outside the provider's supported set is refused with
# EXIT_BAD_ASSIGNMENT (66) before a Worker starts. Dropping it silently would
# launch a different worker than the one the dispatcher recorded.
#
# Effort values probed on this host, 2026-11-12 (PRD 4.2):
#   grok 1.0.77       --reasoning-effort / --effort: none, minimal, low,
#                     medium, high, xhigh, max. The built-in --help lists the
#                     flag but not the values; the binary's own effort parser
#                     and docs name these canonical levels. Today's fixed
#                     value is high, so default == high.
#   codex 0.167.2     -c model_reasoning_effort=<v>: low, medium, high,
#                     xhigh, max, ultra (`codex debug models` on this host).
#                     codex is a reserved role and this script never starts
#                     it; the values are recorded for the reviewer path.
#   claude 2.1.311    --effort low, medium, high, xhigh, max (reserved too).
#   cursor-agent      no effort flag; <model>[effort=<v>], low/medium/high.
#   step 0.1.7        --thinking off, minimal, low, medium, high, xhigh, max.
#   arkcli 1.0.46     +chat --reasoning-effort minimal, low, medium, high.
#   kimi 2.5.3, glm, deepseek 0.2.2-rc.1 and mcode 0.7.6: PRD 4.2 maps no
#                     effort; the launcher refuses one rather than dropping it.

line_model_id="${CONDUCTOR_LINE_MODEL_ID:-}"
line_cli_effort="${CONDUCTOR_LINE_CLI_EFFORT:-}"
line_runtime="${CONDUCTOR_LINE_RUNTIME:-}"

assignment_refused() {
    log "$PROG: $1"
    exit "$EXIT_BAD_ASSIGNMENT"
}

case "$line_model_id" in
    default) line_model_id="" ;;
esac
case "$line_cli_effort" in
    default) line_cli_effort="" ;;
esac
# Normalize the runtime to one provider variant. An empty value and
# "native_cli" both mean today's behavior; the prefixed spellings are the
# same variants under the name the dispatcher records in other contexts.
runtime_variant=""
case "$line_runtime" in
    ""|default|native_cli) runtime_variant="" ;;
    clean-room|native_cli:clean-room|native_cli:glm-clean-room)
        runtime_variant="glm-clean-room"
        ;;
    official|native_cli:official|native_cli:glm-official)
        runtime_variant="glm-official"
        ;;
    api[-]key|native_cli:api[-]key|native_cli:minimax-api[-]key)
        runtime_variant="minimax-key"
        ;;
    login|native_cli:login|native_cli:minimax-login)
        runtime_variant="minimax-login"
        ;;
    *) assignment_refused "unsupported runtime: $line_runtime" ;;
esac

# A variant belongs to one provider. Accepting "login" for glm would name a
# MiniMax credential profile while launching a different worker.
case "$runtime_variant" in
    glm-clean-room|glm-official)
        if [ "$canonical" != "glm" ]; then
            assignment_refused "runtime $line_runtime is only supported for glm"
        fi
        ;;
    minimax-key|minimax-login)
        if [ "$canonical" != "minimax" ]; then
            assignment_refused "runtime $line_runtime is only supported for minimax"
        fi
        ;;
esac

# The model ids each provider's CLI is known to accept. PRD 4.4 collects
# them from the adapter default and the provider's env override; the literal
# lists below are the ids the adapters and this host's model manifest name.
# An id that is well formed but not in the list is refused before launch,
# because a CLI would otherwise fail after the Worker has already started.
# deepseek is handled separately: its model comes from its profile.
model_is_known() {
    local candidate="$1"
    local minimax_base
    case "$canonical" in
        kimi)
            case "$candidate" in
                kimi-k3|kimi-k3.1-pro|kimi-k3.1-pro-highspeed|kimi-for-coding|kimi-k2.5|kimi-k2|k3|k3.1-pro|k3.1-pro-highspeed)
                    return 0
                    ;;
            esac
            if [ -n "${NATIVE_CLI_KIMI_MODEL:-}" ] \
                && [ "$candidate" = "$NATIVE_CLI_KIMI_MODEL" ]; then
                return 0
            fi
            ;;
        glm)
            case "$candidate" in
                GLM-5.3|GLM-5.3-Flash|GLM-5.2|glm-5.3|glm-5.3-flash|glm-5.2)
                    return 0
                    ;;
            esac
            if [ -n "${NATIVE_CLI_ZCODE_MODEL:-}" ] \
                && [ "$candidate" = "$NATIVE_CLI_ZCODE_MODEL" ]; then
                return 0
            fi
            if [ -n "${NATIVE_CLI_ZHIPU_MODEL:-}" ] \
                && [ "$candidate" = "$NATIVE_CLI_ZHIPU_MODEL" ]; then
                return 0
            fi
            if [ -n "${CONDUCTOR_GLM_MODEL:-}" ] \
                && [ "$candidate" = "$CONDUCTOR_GLM_MODEL" ]; then
                return 0
            fi
            ;;
        grok)
            case "$candidate" in
                grok-4.7|grok-4.6|grok-4.5|grok-4.1|grok-4|grok-code-fast-1|grok-3)
                    return 0
                    ;;
            esac
            if [ -n "${NATIVE_CLI_GROK_MODEL:-}" ] \
                && [ "$candidate" = "$NATIVE_CLI_GROK_MODEL" ]; then
                return 0
            fi
            ;;
        cursor)
            # The dispatcher may already have applied [effort=...] to the
            # model id (N73 L1); the catalog holds the base id only.
            candidate="${candidate%%\[*}"
            case "$candidate" in
                auto|gpt-5|gpt-5.1|gpt-5-codex|sonnet-4.5|sonnet-4|opus-4.1|composer-1|cursor-small)
                    return 0
                    ;;
            esac
            if [ -n "${NATIVE_CLI_CURSOR_MODEL:-}" ] \
                && [ "$candidate" = "$NATIVE_CLI_CURSOR_MODEL" ]; then
                return 0
            fi
            ;;
        stepfun)
            case "$candidate" in
                step-5-preview|step-3.7-flash|step-3.5-flash|step-router-v1|step-3|step-2)
                    return 0
                    ;;
            esac
            if [ -n "${NATIVE_CLI_STEPFUN_MODEL:-}" ] \
                && [ "$candidate" = "$NATIVE_CLI_STEPFUN_MODEL" ]; then
                return 0
            fi
            ;;
        minimax)
            # mcode takes provider/model#variant. The catalog lists the base
            # id; a single trailing variant is the same model.
            minimax_base="$candidate"
            case "$candidate" in
                *#*) minimax_base="${candidate%%#*}" ;;
            esac
            case "$minimax_base" in
                MiniMax-M3|MiniMax-M3.1-Flash|MiniMax-M2.7|MiniMax-M2.7-highspeed|MiniMax-M2|minimax-m3|minimax-m2|minimax/minimax-m3|minimax/minimax-m2|minimax/MiniMax-M3|minimax/MiniMax-M3.1-Flash|minimax/MiniMax-M2.7|minimax/MiniMax-M2.7-highspeed|minimax/MiniMax-M2)
                    return 0
                    ;;
            esac
            if [ -n "${NATIVE_CLI_MINIMAX_MODEL:-}" ] \
                && { [ "$candidate" = "$NATIVE_CLI_MINIMAX_MODEL" ] \
                    || [ "$minimax_base" = "$NATIVE_CLI_MINIMAX_MODEL" ]; }; then
                return 0
            fi
            if [ -n "${NATIVE_CLI_MINIMAX_MMX_MODEL:-}" ] \
                && { [ "$candidate" = "$NATIVE_CLI_MINIMAX_MMX_MODEL" ] \
                    || [ "$minimax_base" = "$NATIVE_CLI_MINIMAX_MMX_MODEL" ]; }; then
                return 0
            fi
            ;;
        doubao)
            # An Ark endpoint id (ep-...) is account-specific, so there is no
            # static list to validate it against. It counts as known only when
            # the provider's own NATIVE_CLI_DOUBAO_MODEL declares that exact
            # id (checked below); an undeclared ep-... is refused with 66,
            # not forwarded to arkcli to fail after the Worker started.
            case "$candidate" in
                doubao-seed-2-1-pro|doubao-seed-2-1-turbo|doubao-seed-2-1-lite|doubao-seed-2-0-pro|doubao-seed-2|doubao-seed-1-6-250615)
                    return 0
                    ;;
            esac
            if [ -n "${NATIVE_CLI_DOUBAO_MODEL:-}" ] \
                && [ "$candidate" = "$NATIVE_CLI_DOUBAO_MODEL" ]; then
                return 0
            fi
            ;;
    esac
    return 1
}

cursor_model_arg=""
if [ -n "$line_model_id" ]; then
    # The argv is built from these tokens, so an id that could read as a flag
    # or hold a control character is refused instead of passed through.
    # MiniMax is the exception: mcode's parser takes provider/model#variant,
    # and the hash has to survive this check so a configured id such as
    # minimax/MiniMax-M3#xhigh can match NATIVE_CLI_MINIMAX_MODEL below.
    # The hash stays inside one argv word, so the shell never treats it as
    # a comment. Every other provider still rejects it.
    if [ "$canonical" = "minimax" ]; then
        case "$line_model_id" in
            -*|*[!A-Za-z0-9._:/@+=\[\]#-]*)
                assignment_refused "unsupported model id"
                ;;
            \#*|*#*#*|*#)
                assignment_refused "unsupported model id"
                ;;
        esac
        case "$line_model_id" in
            *#*)
                minimax_variant="${line_model_id#*#}"
                case "$minimax_variant" in
                    ""|-*|*[!A-Za-z0-9._-]*)
                        assignment_refused "unsupported model id"
                        ;;
                esac
                ;;
        esac
    else
        case "$line_model_id" in
            -*|*[!A-Za-z0-9._:/@+=\[\]-]*)
                assignment_refused "unsupported model id"
                ;;
        esac
    fi

    # cursor-agent has no effort flag: the value rides in the model string as
    # <model>[effort=<v>]. The dispatcher may send the id already suffixed
    # (N73 L1), so the suffix is validated here rather than forwarded as-is:
    # model_is_known below ignores it, and an unknown value would make the CLI
    # fail only after the Worker had started. The same set as the separate
    # variable applies. A suffix that disagrees with CONDUCTOR_LINE_CLI_EFFORT
    # is refused too: forwarding either one would silently drop the other.
    #
    # Any bracket at all must form exactly one valid trailing [effort=...]
    # suffix. A malformed id such as "gpt-5[" is refused here, before the
    # binary lookup, rather than handed to an installed CLI to reject after
    # launch.
    if [ "$canonical" = "cursor" ]; then
        case "$line_model_id" in
            *\[*)
                cursor_base="${line_model_id%%\[*}"
                cursor_suffix="${line_model_id#*\[}"
                # The base id may not contain the closing bracket of an
                # earlier suffix, and the rest must be exactly one known one.
                case "$cursor_base" in
                    *\]*)
                        assignment_refused "unsupported cursor model suffix: $line_model_id"
                        ;;
                esac
                case "$cursor_suffix" in
                    effort=low]|effort=medium]|effort=high])
                        cursor_suffix_effort="${cursor_suffix#effort=}"
                        cursor_suffix_effort="${cursor_suffix_effort%]}"
                        ;;
                    *)
                        assignment_refused "unsupported cursor model suffix: $line_model_id"
                        ;;
                esac
                if [ -n "$line_cli_effort" ] \
                    && [ "$line_cli_effort" != "$cursor_suffix_effort" ]; then
                    assignment_refused "conflicting cursor effort: model suffix is $cursor_suffix_effort, CONDUCTOR_LINE_CLI_EFFORT is $line_cli_effort"
                fi
                cursor_model_arg="$line_model_id"
                ;;
            *\]*)
                # A closing bracket without an opening one is malformed the
                # same way, so it is refused rather than passed to the CLI.
                assignment_refused "unsupported cursor model suffix: $line_model_id"
                ;;
            *)
                if [ -n "$line_cli_effort" ]; then
                    cursor_model_arg="${line_model_id}[effort=$line_cli_effort]"
                else
                    cursor_model_arg="$line_model_id"
                fi
                ;;
        esac
    fi

    if [ "$canonical" = "deepseek" ]; then
        assignment_refused "deepseek takes its model from its profile, not CONDUCTOR_LINE_MODEL_ID"
    fi
    if ! model_is_known "$line_model_id"; then
        assignment_refused "unknown $canonical model id: $line_model_id"
    fi
fi

# mcode's --model parser takes provider/model (or provider/model#variant).
# The catalog's MiniMax ids are bare (MiniMax-M3 etc.), while mcode's built-in
# provider id is "minimax", so a bare id is qualified before it reaches argv.
# A value already in provider/model form is forwarded unchanged.
minimax_model_arg=""
if [ "$canonical" = "minimax" ] && [ -n "$line_model_id" ]; then
    case "$line_model_id" in
        */*) minimax_model_arg="$line_model_id" ;;
        *) minimax_model_arg="minimax/$line_model_id" ;;
    esac
fi

case "$canonical" in
    grok)
        case "$line_cli_effort" in
            ""|none|minimal|low|medium|high|xhigh|max) ;;
            *) assignment_refused "unsupported grok effort: $line_cli_effort" ;;
        esac
        ;;
    stepfun)
        case "$line_cli_effort" in
            ""|off|minimal|low|medium|high|xhigh|max) ;;
            *) assignment_refused "unsupported stepfun effort: $line_cli_effort" ;;
        esac
        ;;
    doubao)
        case "$line_cli_effort" in
            ""|minimal|low|medium|high) ;;
            *) assignment_refused "unsupported doubao effort: $line_cli_effort" ;;
        esac
        ;;
    cursor)
        # cursor-agent has no effort flag: the value rides in the model
        # string as <model>[effort=<v>], so a model id is required.
        case "$line_cli_effort" in
            ""|low|medium|high) ;;
            *) assignment_refused "unsupported cursor effort: $line_cli_effort" ;;
        esac
        if [ -n "$line_cli_effort" ] && [ -z "$line_model_id" ]; then
            assignment_refused "cursor needs a model id to carry an effort"
        fi
        ;;
    *)
        # kimi, glm, deepseek and minimax: PRD 4.2 maps no effort for them.
        # Dropping it silently would launch a different worker than the one
        # the dispatcher recorded, so it is refused.
        if [ -n "$line_cli_effort" ]; then
            assignment_refused "unsupported $canonical effort: $line_cli_effort"
        fi
        ;;
esac

# Classify a GLM binary the same way ZCodeCliAdapter._is_clean_room_cli does,
# in its two steps: the invocation name (before resolving symlinks) picks the
# clean-room grammar when it is zcode-cli; otherwise the resolved target's name
# decides. Anything else, including the official app CLI and shell wrappers,
# does not.
glm_grammar() {
    local resolved name
    resolved="$1"
    if command -v "$resolved" >/dev/null 2>&1; then
        resolved="$(command -v "$resolved")"
    fi
    # Fast path: the name the binary was invoked by, before any symlink is
    # resolved (Path(binary).name in the adapter). A link named zcode-cli
    # whose target is something else is still the clean-room CLI.
    name="${resolved##*/}"
    if [ "$name" = "zcode-cli" ]; then
        printf '%s\n' clean-room
        return
    fi
    # Slow path: a link named anything else that resolves to zcode-cli speaks
    # the clean-room grammar too (Path.resolve().name in the adapter).
    if command -v python3 >/dev/null 2>&1; then
        resolved="$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$resolved" 2>/dev/null || printf '%s\n' "$resolved")"
    fi
    name="${resolved##*/}"
    if [ "$name" = "zcode-cli" ]; then
        printf '%s\n' clean-room
    else
        printf '%s\n' official
    fi
}

# Binary per worker. NATIVE_CLI_<X>_BIN overrides, using the same names as
# src/server/services/native_runtime/registry.py. GLM is the "zcode" key, so
# NATIVE_CLI_ZCODE_BIN is its name; the "zhipu" name is accepted as well,
# because the same CLI is registered under both.
binary=""
case "$canonical" in
    kimi)     binary="${NATIVE_CLI_KIMI_BIN:-kimi}" ;;
    glm)
        # The official zcode app CLI is the default. The clean-room zcode-cli
        # runs only when an explicit override names it or the line's runtime
        # asks for it; a host that happens to have both on PATH still launches
        # the official one for an unset/default runtime.
        case "$runtime_variant" in
            glm-clean-room)
                # A matching explicit override wins; otherwise the first
                # explicit override is kept so the grammar check below can
                # refuse it rather than silently substituting another binary.
                # With no override, PATH mirrors the clean-room adapter's own
                # search: ~/.local/bin/zcode-cli before zcode-cli.
                if [ -n "${NATIVE_CLI_ZHIPU_BIN:-}" ] \
                    && [ "$(glm_grammar "$NATIVE_CLI_ZHIPU_BIN")" = "clean-room" ]; then
                    binary="$NATIVE_CLI_ZHIPU_BIN"
                elif [ -n "${NATIVE_CLI_ZCODE_BIN:-}" ] \
                    && [ "$(glm_grammar "$NATIVE_CLI_ZCODE_BIN")" = "clean-room" ]; then
                    binary="$NATIVE_CLI_ZCODE_BIN"
                elif [ -n "${NATIVE_CLI_ZHIPU_BIN:-}" ]; then
                    binary="$NATIVE_CLI_ZHIPU_BIN"
                elif [ -n "${NATIVE_CLI_ZCODE_BIN:-}" ]; then
                    binary="$NATIVE_CLI_ZCODE_BIN"
                elif [ -x "$HOME/.local/bin/zcode-cli" ]; then
                    binary="$HOME/.local/bin/zcode-cli"
                else
                    binary="zcode-cli"
                fi
                ;;
            glm-official)
                if [ -n "${NATIVE_CLI_ZCODE_BIN:-}" ] \
                    && [ "$(glm_grammar "$NATIVE_CLI_ZCODE_BIN")" = "official" ]; then
                    binary="$NATIVE_CLI_ZCODE_BIN"
                elif [ -n "${NATIVE_CLI_ZHIPU_BIN:-}" ] \
                    && [ "$(glm_grammar "$NATIVE_CLI_ZHIPU_BIN")" = "official" ]; then
                    binary="$NATIVE_CLI_ZHIPU_BIN"
                elif [ -n "${NATIVE_CLI_ZCODE_BIN:-}" ]; then
                    binary="$NATIVE_CLI_ZCODE_BIN"
                elif [ -n "${NATIVE_CLI_ZHIPU_BIN:-}" ]; then
                    binary="$NATIVE_CLI_ZHIPU_BIN"
                else
                    binary="zcode"
                fi
                ;;
            *)
                if [ -n "${NATIVE_CLI_ZCODE_BIN:-}" ]; then
                    binary="$NATIVE_CLI_ZCODE_BIN"
                elif [ -n "${NATIVE_CLI_ZHIPU_BIN:-}" ]; then
                    binary="$NATIVE_CLI_ZHIPU_BIN"
                else
                    binary="zcode"
                fi
                ;;
        esac
        ;;
    grok)     binary="${NATIVE_CLI_GROK_BIN:-grok}" ;;
    cursor)   binary="${NATIVE_CLI_CURSOR_BIN:-cursor-agent}" ;;
    stepfun)  binary="${NATIVE_CLI_STEPFUN_BIN:-step}" ;;
    deepseek) binary="${NATIVE_CLI_DSH_BIN:-dsh}" ;;
    minimax)  binary="${NATIVE_CLI_MINIMAX_BIN:-mcode}" ;;
    doubao)   binary="${NATIVE_CLI_DOUBAO_BIN:-arkcli}" ;;
esac

# Resolve the GLM grammar from the binary that was actually selected, and
# refuse a runtime variant whose binary does not speak it. The official CLI
# has no --model flag, so a line model id is refused there before the prompt
# is written. This early check runs only for an assigned line: with no
# assignment the grammar is resolved after the binary lookup, exactly where
# main resolved it, so a missing binary still exits 127 with no other effect.
glm_syntax=""
if [ "$canonical" = "glm" ] \
    && { [ -n "$runtime_variant" ] || [ -n "$line_model_id" ]; }; then
    glm_syntax="$(glm_grammar "$binary")"
    case "$runtime_variant" in
        glm-clean-room)
            if [ "$glm_syntax" != "clean-room" ]; then
                assignment_refused "clean-room runtime selected a non-clean-room GLM binary: $binary"
            fi
            ;;
        glm-official)
            if [ "$glm_syntax" != "official" ]; then
                assignment_refused "official runtime selected a clean-room GLM binary: $binary"
            fi
            ;;
    esac
    if [ "$glm_syntax" = "official" ] && [ -n "$line_model_id" ]; then
        assignment_refused "the official zcode CLI has no model flag"
    fi
fi

if ! command -v "$binary" >/dev/null 2>&1; then
    log "$PROG: $canonical binary not found: $binary"
    exit "$EXIT_NO_BINARY"
fi
if [ "$canonical" = "glm" ] && [ -z "$glm_syntax" ]; then
    glm_syntax="$(glm_grammar "$binary")"
fi

# MiniMax's runtime variants select a credential profile, not an argv
# spelling. The key profile is the adapter's API-key data directory, kept
# apart from mcode's default (login) profile so neither can read or
# overwrite the other. login keeps today's environment unless
# an operator names a separate login data directory. The launcher still never
# reads or sets a credential: the environment it already has supplies that,
# exactly as it always has.
minimax_env=()
case "$runtime_variant" in
    minimax-key)
        # The API-key profile is the one the MiniMax adapter provisions
        # (src/llms/native_cli/minimax.py data_dir and its ready marker), so
        # both paths share one directory and one variable name. An
        # unprovisioned profile is refused here: mcode would otherwise fail
        # on auth after the Worker started, or create a fresh profile. This
        # check needs the profile, not argv, so it runs after the binary
        # lookup: a missing mcode is 127 first. Both precede any Worker.
        minimax_data_dir="${NATIVE_CLI_MINIMAX_DATA_DIR:-$HOME/.cache/conductor/minimax}"
        if [ ! -f "$minimax_data_dir/.alpha-provider-ready" ]; then
            assignment_refused "minimax key-profile runtime is not provisioned: $minimax_data_dir"
        fi
        minimax_env=(MINIMAX_DATA_DIR="$minimax_data_dir")
        ;;
    minimax-login)
        if [ -n "${NATIVE_CLI_MINIMAX_LOGIN_DATA_DIR:-}" ]; then
            minimax_env=(MINIMAX_DATA_DIR="$NATIVE_CLI_MINIMAX_LOGIN_DATA_DIR")
        fi
        ;;
esac

# ------------------------------------------------------------ prompt build

log_dir="${CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/conductor/dispatch-launch}"
umask 077
mkdir -p "$log_dir"
chmod 700 "$log_dir"

run_stamp="$(date +%Y%m%d-%H%M%S)-$$"
prompt_file="$log_dir/$canonical-$run_stamp.prompt"
log_file="$log_dir/$canonical-$run_stamp.log"
fifo="$log_dir/$canonical-$run_stamp.fifo"

{
    printf '%s\n' "$task"
    if [ -n "$skills" ]; then
        printf 'Skills: %s\n' "$skills"
    fi
    if [ -n "$impact_map" ]; then
        printf 'Impact map: %s\n' "$impact_map"
    fi
} > "$prompt_file"
if [ -n "$handoff" ]; then
    printf '\nContinue from the deterministic handoff notes at: %s\n' "$handoff" >> "$prompt_file"
    # The note itself goes into the prompt too, so the new session has it even
    # when it cannot read that path. A regular file only, and a bounded read.
    if [ -f "$handoff" ] && [ ! -L "$handoff" ]; then
        printf '\n' >> "$prompt_file"
        head -c 16384 "$handoff" >> "$prompt_file"
        printf '\n' >> "$prompt_file"
    fi
fi

# N29: the context budget block, right after the handoff note. Only with
# --context-budget; without it the prompt and the Worker's environment are
# what they always were. The same rules as the note: a regular file only, no
# symlink, a bounded read (2 KiB). A missing or refused file inlines nothing
# and the launch goes on. The variables go to the Worker alone (exported in
# its subshell below), not to the helpers this script runs.
context_env=()
if [ -n "$context_budget" ]; then
    if [ -f "$context_budget" ] && [ ! -L "$context_budget" ]; then
        {
            printf '\n'
            head -c 2048 "$context_budget"
            printf '\n'
        } >> "$prompt_file"
    fi
    # The interpreter is the failure classifier's venv python (see
    # classification below), else python3 from PATH, always as an absolute
    # path. Interpreter and script go out as two variables, so a path with a
    # space survives: "$CONDUCTOR_HISTORY_PYTHON" "$CONDUCTOR_HISTORY_SCRIPT".
    history_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd -P || true)"
    history_python="$history_root/.venv/bin/python"
    if [ ! -x "$history_python" ]; then
        history_python="$(command -v python3 2>/dev/null || true)"
        case "$history_python" in
            /*) ;;
            "") history_python="/usr/bin/python3" ;;
            *) history_python="$PWD/$history_python" ;;
        esac
    fi
    history_script="$history_root/scripts/worker_history.py"
    context_env+=(CONDUCTOR_CONTEXT_BUDGET_FILE="$context_budget")
    [ -z "$handoff_request" ] || context_env+=(CONDUCTOR_HANDOFF_REQUEST_FILE="$handoff_request")
    if [ -n "$history_manifest" ]; then
        context_env+=(CONDUCTOR_HISTORY_MANIFEST="$history_manifest")
        # The runs dir the helper is pinned to: manifest -> history ->
        # recovery-sessions -> runs dir (context_budget.runs_dir_for_manifest).
        history_runs_root="$(dirname "$(dirname "$(dirname "$history_manifest")")")"
        context_env+=(CONDUCTOR_HISTORY_RUNS_ROOT="$history_runs_root")
    fi
    [ -z "$run_log" ] || context_env+=(CONDUCTOR_RUN_LOG="$run_log")
    context_env+=(CONDUCTOR_HISTORY_PYTHON="$history_python")
    context_env+=(CONDUCTOR_HISTORY_SCRIPT="$history_script")
    # Kept for compatibility; each word is shell-quoted.
    context_env+=(CONDUCTOR_HISTORY_CMD="$(printf '%q %q' "$history_python" "$history_script")")
fi

# ------------------------------------------------- deepseek git sandbox

# N72 and its follow-up. `dsh --profile headless` runs the Worker under a
# workspace-write sandbox (Seatbelt on macOS) rooted at the worktree, and
# --add-dir can grant whole directories only. A linked worktree keeps its git
# metadata outside the worktree, so a commit needs some of it writable. The
# grants are kept to what `git add` and `git commit` on the line's own branch
# write, and nothing else of the common dir:
#
#   <admin dir>                    index, HEAD, ORIG_HEAD, logs/HEAD,
#                                  COMMIT_EDITMSG and their .lock files
#                                  (git rev-parse --absolute-git-dir)
#   <common>/<dir of the branch>   refs/heads/feature/auto-dispatch-<id>/, for
#                                  line-<n> and line-<n>.lock
#   <common>/logs/<same dir>       the branch reflog
#   <common>/conductor-objects/<admin id>-<run stamp>
#                                  this launch's object quarantine
#
# Not granted: hooks/, config, info/, packed-refs, every other ref directory
# and objects/. The Worker's git writes new objects into the quarantine
# (GIT_OBJECT_DIRECTORY) and reads the real store as an alternate. After the
# Worker and anything left in its process group are gone, on every exit path,
# the host moves the objects into the real store with git itself
# (pack-objects | index-pack --strict, bounded by
# CONDUCTOR_DISPATCH_QUARANTINE_MAX_BYTES), which hashes and checks every
# object and never overwrites an existing one. The host's git never trusts
# the Worker's side: it runs with --git-dir=<common> captured before the run,
# no hooks, no fsmonitor, no system or global config, and checks the tip by
# the branch name captured before the run, not the admin dir's HEAD.
#
# A quarantine that cannot be moved (a symlink in it, a corrupt or oversized
# pack, a missing tip) is kept, reported with a
# "dispatch_launch: objects_not_migrated" line and exit 79, and retried by the
# next deepseek launch in the worktree, up to
# CONDUCTOR_DISPATCH_QUARANTINE_MAX_RETRIES times. Until then the next Worker
# reads it as a read-only alternate, so the line's history stays readable.
# It lives in the common dir, not the admin dir, so a worktree removal does
# not take unmoved commits with it.
#
# Known residue, by construction of a directory grant: the branch directory
# is shared by the other lines of the same dispatch, so their refs are
# writable too. A commit prints "Unable to create '<common>/packed-refs.lock'"
# (git 2.56 tries that lock on every ref update and carries on without it);
# the commit still succeeds, and granting it would mean the whole common dir.
# The admin dir also holds commondir, gitdir and config.worktree, which steer
# the host's own git in this worktree: they are restored after the Worker,
# and checked again after the migration.

deepseek_grants=""
deepseek_admin=""
deepseek_common=""
deepseek_branch_ref=""
deepseek_quarantine=""
deepseek_kept_alternates=""
deepseek_snapshot=""
deepseek_finished=0
deepseek_migration_failed=0
# The Worker's own exit status, for the objects_not_migrated line: the
# caller still needs to know a quota or rate limit behind it.
deepseek_worker_status=""

deepseek_max_bytes="${CONDUCTOR_DISPATCH_QUARANTINE_MAX_BYTES:-2147483648}"
case "$deepseek_max_bytes" in
    ''|*[!0-9]*) deepseek_max_bytes=2147483648 ;;
esac
deepseek_max_retries="${CONDUCTOR_DISPATCH_QUARANTINE_MAX_RETRIES:-3}"
case "$deepseek_max_retries" in
    ''|*[!0-9]*) deepseek_max_retries=3 ;;
esac

# The admin dir files the host's own git trusts. A Worker that rewrote them
# could point the host at another object store or turn hooks on.
deepseek_protected_files="commondir gitdir config.worktree"

physical_dir() {
    (CDPATH='' cd -- "$1" 2>/dev/null && pwd -P)
}

# The git environment the host never inherits, from its caller or the Worker.
git_clean_env=(
    -u GIT_OBJECT_DIRECTORY -u GIT_ALTERNATE_OBJECT_DIRECTORIES
    -u GIT_CONFIG_PARAMETERS -u GIT_CONFIG_COUNT
    -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE -u GIT_COMMON_DIR
    -u GIT_NAMESPACE
)

# The host's own git: no quarantine, no hooks, no fsmonitor, no system or
# global config.
host_git() {
    env "${git_clean_env[@]}" GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
        git -c core.fsmonitor=false -c core.hooksPath=/dev/null "$@"
}

# The host's git on the real store, by the common dir captured before the
# run: nothing in the worktree or the admin dir chooses the repository.
store_git() {
    host_git --git-dir="$deepseek_common" "$@"
}

# The same, reading objects from one quarantine. The second argument is the
# alternates list, or empty for the quarantine alone.
quarantine_git() {
    local quarantine="$1" alternates="$2"
    shift 2
    if [ -n "$alternates" ]; then
        env "${git_clean_env[@]}" GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
            GIT_OBJECT_DIRECTORY="$quarantine" GIT_ALTERNATE_OBJECT_DIRECTORIES="$alternates" \
            git -c core.fsmonitor=false -c core.hooksPath=/dev/null \
            --git-dir="$deepseek_common" "$@"
    else
        env "${git_clean_env[@]}" GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
            GIT_OBJECT_DIRECTORY="$quarantine" \
            git -c core.fsmonitor=false -c core.hooksPath=/dev/null \
            --git-dir="$deepseek_common" "$@"
    fi
}

deepseek_note() {
    log "$PROG: deepseek: $*"
    printf '[launcher] deepseek: %s\n' "$*" >> "$log_file" 2>/dev/null || true
}

# The fsck severities index-pack --strict applies. --strict turns every
# fsck warning into an error, so a Worker that fetched old external history
# (a tree with a zero-padded file mode, say) would wedge the line forever.
# Only the two warnings that are pure legacy format stay warnings:
# zeroPaddedFilemode and largePathname. The path-unsafe ones (hasDot,
# hasDotdot, hasDotgit, fullPathname, emptyName) and nulInCommit, which can
# hide text from git log, stay errors: verify_path covers the .git variants
# only with protectHFS/protectNTFS, and not git archive, older git on other
# hosts or other git libraries; GitHub refuses such trees on push anyway.
# Errors are still refused; the repository's own fsck.<id> settings win.
deepseek_fsck_strict() {
    local spec key value
    spec="zeroPaddedFilemode=warn,largePathname=warn"
    while read -r key value; do
        key="${key#fsck.}"
        case "$key" in
            ''|*[!A-Za-z]*|skiplist) continue ;;
        esac
        case "$value" in
            error|warn|ignore) spec="$spec,$key=$value" ;;
        esac
    done < <(store_git config --get-regexp '^fsck\.[A-Za-z]+$' 2>/dev/null || true)
    printf '%s\n' "$spec"
}

# How many times a kept quarantine failed to move. The count lives next to
# it in conductor-objects/, which no Worker is granted.
quarantine_attempts() {
    local count
    count="$(cat "$1.attempts" 2>/dev/null || true)"
    case "$count" in
        ''|*[!0-9]*) count=0 ;;
    esac
    printf '%s\n' "$count"
}

quarantine_failed() {
    local quarantine="$1" reason="$2" count
    count="$(quarantine_attempts "$quarantine")"
    printf '%s\n' "$((count + 1))" > "$quarantine.attempts" 2>/dev/null || true
    deepseek_note "object quarantine kept at $quarantine: $reason"
    return 1
}

# Move one quarantine's objects into the common store. 0 once every object
# is readable from the store with no quarantine env, and, for this launch's
# own quarantine ("current"), the line's tip too; the quarantine itself is
# left for drop_quarantine. 1 with it kept. A leftover of an earlier launch
# ("leftover") is judged by its own objects only: the line's tip may well
# live in a newer quarantine that has not been moved yet.
migrate_quarantine() {
    local quarantine="$1" mode="${2:-current}" list tip links
    [ -n "$quarantine" ] || return 0
    if [ ! -e "$quarantine" ] && [ ! -L "$quarantine" ]; then
        return 0
    fi
    # Nothing is read, listed or removed through a path the Worker controls:
    # a symlink anywhere in the quarantine could point the host's own reads
    # and rm at another directory.
    if [ -L "$quarantine" ] || [ ! -d "$quarantine" ]; then
        quarantine_failed "$quarantine" "it is not a real directory"
        return 1
    fi
    if ! links="$(find "$quarantine" -type l -print 2>/dev/null)"; then
        quarantine_failed "$quarantine" "it could not be inspected"
        return 1
    fi
    if [ -n "$links" ]; then
        quarantine_failed "$quarantine" "it holds a symlink"
        return 1
    fi
    list="$(mktemp "$log_dir/$canonical-$run_stamp.objects.XXXXXX")" || {
        quarantine_failed "$quarantine" "no list file"
        return 1
    }
    # Only the quarantine's own objects: an alternates file the Worker left
    # there would pull another store into the listing.
    rm -f "${quarantine:?}/info/alternates" "${quarantine:?}/info/http-alternates"
    if ! quarantine_git "$quarantine" "" \
            cat-file --batch-all-objects --batch-check='%(objectname)' \
            > "$list" 2>> "$log_file"; then
        rm -f "$list"
        quarantine_failed "$quarantine" "it could not be listed"
        return 1
    fi
    # Objects the store already has need no move: a quarantine made only of
    # them is done.
    if [ -s "$list" ] && store_git cat-file --batch-check < "$list" > "$list.check" 2>> "$log_file" \
            && ! grep -q ' missing$' "$list.check"; then
        : > "$list"
    fi
    if [ -s "$list" ]; then
        # The objects stay packed in the store, and index-pack checks every
        # one (hash, fsck, links) and refuses a pack over the bound.
        if ! quarantine_git "$quarantine" "$deepseek_common/objects" \
                pack-objects -q --stdout < "$list" 2>> "$log_file" \
                | store_git index-pack --stdin --strict="$(deepseek_fsck_strict)" \
                    --max-input-size="$deepseek_max_bytes" \
                    > /dev/null 2>> "$log_file"; then
            rm -f "$list"
            quarantine_failed "$quarantine" "the objects could not be moved"
            return 1
        fi
        if ! store_git cat-file --batch-check < "$list" > "$list.check" 2>> "$log_file" \
                || grep -q ' missing$' "$list.check"; then
            rm -f "$list" "$list.check"
            quarantine_failed "$quarantine" "objects are still missing from the store"
            return 1
        fi
    fi
    rm -f "$list" "$list.check"
    # The line's tip must be readable from the store alone: G2, the review
    # and the next launch read it from a fresh worktree with no quarantine.
    # The branch is the one captured before the run.
    if [ "$mode" = "current" ] && [ -n "$deepseek_branch_ref" ]; then
        tip="$(store_git rev-parse -q --verify "$deepseek_branch_ref" 2>/dev/null || true)"
        if [ -n "$tip" ] && ! store_git cat-file -e "$tip^{commit}" 2>/dev/null; then
            quarantine_failed "$quarantine" "the tip $tip is not in the store"
            return 1
        fi
    fi
    return 0
}

drop_quarantine() {
    rm -rf "${1:?}"
    rm -f "${1:?}.attempts"
}

# Put the admin dir files the host trusts back as they were before the run.
# Prints the names it had to restore or remove.
restore_admin_files() {
    local name
    [ -n "$deepseek_snapshot" ] || return 0
    for name in $deepseek_protected_files; do
        if [ -f "$deepseek_snapshot/$name" ]; then
            if [ -L "$deepseek_admin/$name" ] \
                    || ! cmp -s "$deepseek_snapshot/$name" "$deepseek_admin/$name"; then
                rm -f "${deepseek_admin:?}/${name:?}"
                cp -p "$deepseek_snapshot/$name" "$deepseek_admin/$name"
                printf '%s\n' "$name"
            fi
        elif [ -e "$deepseek_admin/$name" ] || [ -L "$deepseek_admin/$name" ]; then
            rm -f "${deepseek_admin:?}/${name:?}"
            printf '%s\n' "$name"
        fi
    done
}

# Work out the grants and make the quarantine, before the Worker starts.
# Sets deepseek_grants (one physical directory per line) and the rest of the
# deepseek_* state. A cwd outside a git worktree, a main worktree or a host
# without git gets no grant at all.
deepseek_prepare_git() {
    local git_dir common_dir ref ref_dir format sub granted name leftover attempts
    command -v git >/dev/null 2>&1 || return 0
    git_dir="$(host_git rev-parse --absolute-git-dir 2>/dev/null)" || return 0
    git_dir="$(physical_dir "$git_dir")" || return 0
    # --git-common-dir may be relative to the cwd; physical_dir resolves it.
    common_dir="$(host_git rev-parse --git-common-dir 2>/dev/null)" || return 0
    common_dir="$(physical_dir "$common_dir")" || return 0
    [ -n "$git_dir" ] && [ -n "$common_dir" ] || return 0
    if [ "$git_dir" = "$common_dir" ]; then
        deepseek_note "a main worktree's git dir is the whole common dir; nothing is granted"
        return 0
    fi
    deepseek_admin="$git_dir"
    deepseek_common="$common_dir"
    deepseek_grants="$git_dir"

    deepseek_snapshot="$(mktemp -d "$log_dir/$canonical-$run_stamp.admin.XXXXXX")" || deepseek_snapshot=""
    if [ -n "$deepseek_snapshot" ]; then
        for name in $deepseek_protected_files; do
            if [ -f "$git_dir/$name" ] && [ ! -L "$git_dir/$name" ]; then
                cp -p "$git_dir/$name" "$deepseek_snapshot/$name" 2>/dev/null || true
            fi
        done
    fi

    format="$(host_git rev-parse --show-ref-format 2>/dev/null || true)"
    ref="$(host_git symbolic-ref -q HEAD 2>/dev/null || true)"
    case "$ref" in
        refs/heads/?*) deepseek_branch_ref="$ref" ;;
    esac
    if [ -n "$format" ] && [ "$format" != "files" ]; then
        deepseek_note "ref storage is $format, not files; no ref directory is granted"
    else
        case "$ref" in
            refs/heads/*/*)
                ref_dir="${ref%/*}"
                for sub in "$ref_dir" "logs/$ref_dir"; do
                    # Made on the host: git cannot make it from inside.
                    mkdir -p "$common_dir/$sub" 2>/dev/null || continue
                    granted="$(physical_dir "$common_dir/$sub")" || continue
                    case "$granted" in
                        "$common_dir"/*) deepseek_grants="$deepseek_grants
$granted" ;;
                    esac
                done
                ;;
            refs/heads/*)
                deepseek_note "branch ${ref#refs/heads/} sits directly in refs/heads, which holds every branch; it is not granted"
                ;;
        esac
    fi

    if mkdir -p "$common_dir/conductor-objects" 2>/dev/null; then
        # Oldest first, by modification time (ls -t compares nanoseconds):
        # a newer quarantine's objects can need an older one's, and the
        # names' text order is not time order within one second.
        while IFS= read -r leftover <&3; do
            case "$leftover" in
                ''|*.attempts) continue ;;
            esac
            [ -e "$leftover" ] || [ -L "$leftover" ] || continue
            attempts="$(quarantine_attempts "$leftover")"
            if [ "$attempts" -ge "$deepseek_max_retries" ]; then
                deepseek_note "object quarantine kept at $leftover: gave up after $attempts attempts; it needs a human"
            elif migrate_quarantine "$leftover" leftover; then
                drop_quarantine "$leftover"
                continue
            fi
            # Still kept: the next Worker reads it, never writes it (it is
            # not granted), so the line's history stays readable.
            if [ -d "$leftover" ] && [ ! -L "$leftover" ]; then
                deepseek_kept_alternates="$deepseek_kept_alternates:$leftover"
            fi
        done 3< <(ls -1dtr -- "$common_dir/conductor-objects/${git_dir##*/}-"* 2>/dev/null || true)
        deepseek_quarantine="$common_dir/conductor-objects/${git_dir##*/}-$run_stamp"
        if mkdir "$deepseek_quarantine" 2>/dev/null \
                && deepseek_quarantine="$(physical_dir "$deepseek_quarantine")"; then
            deepseek_grants="$deepseek_grants
$deepseek_quarantine"
        else
            deepseek_quarantine=""
        fi
    fi
}

# Undo what the Worker may have done to the admin dir files the host trusts,
# move its objects, and check the admin files again: a change after the first
# restore means something of the Worker's is still running, so the
# quarantine is kept. Runs once, on every exit path, after the Worker's
# process group is swept. 0, or 1 when the objects were not moved, with the
# objects_not_migrated marker printed.
deepseek_finish_git() {
    local name changed
    [ "$deepseek_finished" -eq 0 ] || return "$deepseek_migration_failed"
    deepseek_finished=1
    [ -n "$deepseek_admin" ] || return 0
    for name in $(restore_admin_files); do
        deepseek_note "restored $name in the admin dir"
    done
    if [ -n "$deepseek_quarantine" ]; then
        if migrate_quarantine "$deepseek_quarantine"; then
            changed="$(restore_admin_files | tr '\n' ' ')"
            if [ -n "$changed" ]; then
                quarantine_failed "$deepseek_quarantine" \
                    "admin files changed again during the migration: $changed" || true
                deepseek_migration_failed=1
            else
                drop_quarantine "$deepseek_quarantine"
            fi
        else
            deepseek_migration_failed=1
        fi
    fi
    if [ -n "$deepseek_snapshot" ]; then
        rm -rf "${deepseek_snapshot:?}"
    fi
    if [ "$deepseek_migration_failed" -ne 0 ]; then
        deepseek_marker
    fi
    return "$deepseek_migration_failed"
}

# One line of its own, for the caller's classifier: the Worker's commits are
# not in the store yet. The Worker's own exit status rides along, so a quota
# or rate limit behind it is not lost.
# An extra word ("sweep=failed") goes before the quarantine path, which is
# last because it may hold spaces.
deepseek_marker() {
    local line
    line="dispatch_launch: objects_not_migrated worker_status=${deepseek_worker_status:-unknown}${1:+ $1} quarantine=$deepseek_quarantine"
    log "$line"
    printf '%s\n' "$line" >> "$log_file" 2>/dev/null || true
}

# The Worker's process group could not be emptied: something of it may still
# write to the quarantine or the admin dir, so nothing is moved or trusted.
# The quarantine is kept for the next launch, which sweeps first.
deepseek_keep_unmigrated() {
    [ "$deepseek_finished" -eq 0 ] || return "$deepseek_migration_failed"
    deepseek_finished=1
    [ -n "$deepseek_admin" ] || return 0
    [ -n "$deepseek_quarantine" ] || return 0
    quarantine_failed "$deepseek_quarantine" "$1" || true
    deepseek_migration_failed=1
    if [ -n "$deepseek_snapshot" ]; then
        restore_admin_files > /dev/null || true
    fi
    deepseek_marker "sweep=failed"
    return 1
}

# Each worker takes the prompt in its own way. The working directory is the
# caller's cwd: this script never cd's anywhere.
# P0 privacy: a WORK Worker runs under the operator's real HOME, so a CLI can
# fold the operator's own memory and global instruction files into a prompt
# sent to another vendor. Only switches the CLI documents, that turn off just
# that loading and leave its login where it is, are set here; the Worker's
# project AGENTS.md still loads. The variables go to the Worker alone.
#   grok      GROK_MEMORY=0 force-disables cross-session memory
#             (~/.grok/memory-v2). GROK_{CLAUDE,CURSOR}_{AGENTS,RULES}_ENABLED
#             stop the compat scan of ~/.claude/CLAUDE.md, ~/.claude/rules,
#             ~/.cursor named files and ~/.cursor/rules. GROK_HOME (the
#             login) is untouched, and a top-level project CLAUDE.md or
#             AGENTS.md is still read.
#   deepseek  DSH_AGENTS_HOME points the shared agents root (~/.agents:
#             AGENTS.md and skills) at an empty directory. DSH_HOME, which
#             holds the profiles and the login, is untouched, so
#             $DSH_HOME/AGENTS.md still loads: dsh has no switch for it alone.
# Every other WORK CLI has no such switch; see the P0 report.
# Known limits (no switch spares the login, so these still load):
#   grok      everything under GROK_HOME (the login lives there, so it is
#             not moved), plus its compat scans of ~/.claude.json MCP
#             servers and ~/.claude/skills.
#   deepseek  $DSH_HOME/AGENTS.md (~/.dsh/AGENTS.md): moving DSH_HOME would
#             move the profiles and the login with it.
#   kimi      ~/.kimi-code/AGENTS.md and ~/.agents (KIMI_CODE_HOME holds the login).
#   cursor    ~/.cursor/rules/*.mdc.
#   stepfun   ~/.stepcode/agent/AGENTS.md and parent-folder AGENTS.md/CLAUDE.md
#             (--no-context-files would also drop the project AGENTS.md).
#   minimax   $MINIMAX_DATA_DIR/AGENTS.md and config.yaml memory settings.
#   glm       ~/.zcode/cli/memories when memoryEnabled is set.
personal_memory_env=()
personal_memory_isolation() {
    case "$canonical" in
        grok)
            personal_memory_env=(GROK_MEMORY=0
                GROK_CLAUDE_AGENTS_ENABLED=false
                GROK_CLAUDE_RULES_ENABLED=false
                GROK_CURSOR_AGENTS_ENABLED=false
                GROK_CURSOR_RULES_ENABLED=false)
            ;;
        deepseek)
            local empty_agents_home="$log_dir/empty-agents-home"
            mkdir -p "$empty_agents_home" && chmod 700 "$empty_agents_home" \
                || assignment_refused "cannot create an empty agents home: $empty_agents_home"
            personal_memory_env=(DSH_AGENTS_HOME="$empty_agents_home")
            ;;
    esac
}
personal_memory_isolation

run_worker() {
    if [ "${#personal_memory_env[@]}" -gt 0 ]; then
        export "${personal_memory_env[@]}"
    fi
    case "$canonical" in
        kimi)
            # Kimi 2.5 resumes only a session it created: --session <id> of
            # an id it never saw answers "Session not found". A fresh run
            # therefore takes no id at all, even when one was passed, and
            # stream-json makes Kimi name the session it created, which the
            # caller records and resumes. Never --continue: it picks the
            # cwd's latest session, which may be another line's. The legacy
            # stored word "continue" names no session and runs fresh too.
            # The assigned model is a new flag, so it goes before the
            # positional prompt; the prompt itself stays where it was.
            kimi_args=()
            if [ -n "$line_model_id" ]; then
                kimi_args+=(-m "$line_model_id")
            fi
            kimi_args+=(-p "$(cat "$prompt_file")" --output-format stream-json)
            if [ -n "$session_id" ] && [ "$session_mode" = "resume" ] \
                && [ "$session_id" != "continue" ]; then
                kimi_args+=(--session "$session_id")
            fi
            "$binary" "${kimi_args[@]}"
            ;;
        glm)
            # The grammar was resolved once before the prompt was written,
            # from the binary that was actually selected. zcode-cli is the
            # clean-room CLI and the only one that accepts --model and
            # --max-turns. The official zcode app CLI, shell wrappers and an
            # override that is neither speaks the official grammar.
            prompt="$(cat "$prompt_file")"
            # With no assigned model id the argv is the one this script
            # always built, byte for byte. An assigned id is a new flag, so
            # it goes before --prompt.
            if [ -n "$session_id" ]; then
                if [ "$glm_syntax" = "clean-room" ]; then
                    if [ -n "$line_model_id" ]; then
                        "$binary" --model "$line_model_id" \
                            --prompt "$prompt" \
                            --output-format stream-json \
                            --mode yolo \
                            --max-turns 200 \
                            --no-color --resume "$session_id"
                    else
                        "$binary" --prompt "$prompt" \
                            --output-format stream-json \
                            --mode yolo \
                            --model "${CONDUCTOR_GLM_MODEL:-GLM-5.3}" \
                            --max-turns 200 \
                            --no-color --resume "$session_id"
                    fi
                else
                    "$binary" --prompt "$prompt" \
                        --output-format stream-json \
                        --mode yolo \
                        --surface terminal \
                        --no-color --resume "$session_id"
                fi
            else
                if [ "$glm_syntax" = "clean-room" ]; then
                    if [ -n "$line_model_id" ]; then
                        "$binary" --model "$line_model_id" \
                            --prompt "$prompt" \
                            --output-format stream-json \
                            --mode yolo \
                            --max-turns 200 \
                            --no-color
                    else
                        "$binary" --prompt "$prompt" \
                            --output-format stream-json \
                            --mode yolo \
                            --model "${CONDUCTOR_GLM_MODEL:-GLM-5.3}" \
                            --max-turns 200 \
                            --no-color
                    fi
                else
                    "$binary" --prompt "$prompt" \
                        --output-format stream-json \
                        --mode yolo \
                        --surface terminal \
                        --no-color
                fi
            fi
            ;;
        grok)
            # Grok resumes only a session it created and saved ("No saved
            # copy of session <id> was found remotely" otherwise). A fresh
            # run takes no id; the end event of streaming-json names the
            # session Grok created, which the caller records and resumes.
            # Today's fixed effort is high; an assigned value replaces it.
            # The model is a new flag, so it goes before --prompt-file.
            grok_args=()
            if [ -n "$line_model_id" ]; then
                grok_args+=(-m "$line_model_id")
            fi
            grok_args+=(--prompt-file "$prompt_file" --always-approve \
                --reasoning-effort "${line_cli_effort:-high}" \
                --max-turns 150 --output-format streaming-json)
            if [ -n "$session_id" ] && [ "$session_mode" = "resume" ] \
                && [ "$session_id" != "continue" ]; then
                grok_args+=(--resume "$session_id")
            fi
            "$binary" "${grok_args[@]}"
            ;;
        cursor)
            # cursor-agent has no effort flag: the value is part of the model
            # string, <model>[effort=<v>]. The assignment check above
            # validated any incoming suffix and refused an effort without a
            # model id; cursor_model_arg is the effective, checked value.
            cursor_args=(-p --force --trust --workspace "$PWD" \
                --output-format stream-json)
            if [ -n "$cursor_model_arg" ]; then
                cursor_args+=(--model "$cursor_model_arg")
            fi
            if [ -n "$session_id" ]; then
                cursor_args+=(--resume "$session_id")
            fi
            cursor_args+=("$(cat "$prompt_file")")
            "$binary" "${cursor_args[@]}"
            ;;
        stepfun)
            step_args=(-p --no-session --approval-mode auto \
                --non-interactive-approval allow)
            if [ -n "$line_model_id" ]; then
                step_args+=(--model "$line_model_id")
            fi
            if [ -n "$line_cli_effort" ]; then
                step_args+=(--thinking "$line_cli_effort")
            fi
            step_args+=(--mode json "$(cat "$prompt_file")")
            "$binary" "${step_args[@]}"
            ;;
        deepseek)
            # The grants and the quarantine are worked out before the
            # Worker starts (deepseek_prepare_git); the grants come before
            # the stdin marker.
            dsh_args=(--profile headless)
            while IFS= read -r git_path; do
                [ -n "$git_path" ] || continue
                dsh_args+=(--add-dir "$git_path")
            done <<< "$deepseek_grants"
            dsh_args+=(-)
            if [ -n "$deepseek_quarantine" ]; then
                # dsh passes these to its shell: its scrub (SENSITIVE_ENV_PATTERN
                # in @deepseek-ai/dsh-subprocess) drops only credential-shaped
                # names and DSH_*. A name with "KEY" in it is credential-shaped,
                # so the config goes in GIT_CONFIG_PARAMETERS and not
                # GIT_CONFIG_COUNT with its key/value pairs. No auto gc: it
                # would write gc.pid in the common dir.
                export GIT_OBJECT_DIRECTORY="$deepseek_quarantine"
                # Kept quarantines of earlier launches come after the store:
                # readable, never granted.
                export GIT_ALTERNATE_OBJECT_DIRECTORIES="$deepseek_common/objects$deepseek_kept_alternates"
                export GIT_CONFIG_PARAMETERS="${GIT_CONFIG_PARAMETERS:+$GIT_CONFIG_PARAMETERS }'gc.auto'='0' 'maintenance.auto'='false'"
            fi
            "$binary" "${dsh_args[@]}" < "$prompt_file"
            ;;
        minimax)
            minimax_args=(exec --cwd "$PWD" --prompt-mode coding --permission full \
                --output-format stream-json)
            if [ -n "$line_model_id" ]; then
                minimax_args+=(--model "$minimax_model_arg")
            fi
            if [ -n "$session_id" ]; then
                minimax_args+=(--session "$session_id")
            fi
            minimax_args+=(--input -)
            if [ "${#minimax_env[@]}" -gt 0 ]; then
                env "${minimax_env[@]}" "$binary" "${minimax_args[@]}" < "$prompt_file"
            else
                "$binary" "${minimax_args[@]}" < "$prompt_file"
            fi
            ;;
        doubao)
            # The per-line model wins over the provider-wide override; with
            # neither, the profile's own default is used.
            doubao_model="${line_model_id:-${NATIVE_CLI_DOUBAO_MODEL:-}}"
            doubao_args=(+chat --format json --no-progress)
            if [ -n "$doubao_model" ]; then
                doubao_args+=(--model "$doubao_model")
            fi
            if [ -n "$line_cli_effort" ]; then
                doubao_args+=(--reasoning-effort "$line_cli_effort")
            fi
            doubao_args+=(-- "$(cat "$prompt_file")")
            "$binary" "${doubao_args[@]}"
            ;;
    esac
}

# ------------------------------------------------------------ serial lock

# Some providers allow only one request in flight per plan (GLM Coding Plan
# answers a second concurrent Worker with error 1302, a request-frequency
# limit, not a quota). Those providers run one Worker at a time on this
# machine: a launch takes a lock directory before the worker starts and
# waits while another launch holds it. mkdir is atomic, so two launches
# cannot both take it. A lock whose holder is gone is taken over. A launch
# that waits longer than the bound gives up as rate limited, so the caller
# backs off instead of hanging. While it waits it prints a heartbeat, so
# the caller's stall check does not count the wait as a silent worker.
serial_providers="${CONDUCTOR_DISPATCH_SERIAL_PROVIDERS-glm}"
# The wait bound stays under the caller's run timeout (600 s by default), so
# a launch that cannot get the lock reports rate limited instead of being
# cancelled as a timeout.
serial_wait_seconds="${CONDUCTOR_DISPATCH_SERIAL_WAIT_SECONDS:-540}"
serial_heartbeat_seconds="${CONDUCTOR_DISPATCH_SERIAL_HEARTBEAT_SECONDS:-30}"
if [ "$serial_heartbeat_seconds" -lt 1 ] 2>/dev/null || ! [ "$serial_heartbeat_seconds" -eq "$serial_heartbeat_seconds" ] 2>/dev/null; then
    serial_heartbeat_seconds=30
fi
serial_lock=""

# shellcheck disable=SC2329  # called through a trap
serial_early_signal() {
    trap - TERM INT HUP
    if [ -n "${worker_pid:-}" ] && kill -0 "$worker_pid" 2>/dev/null && command -v stop_worker >/dev/null 2>&1; then
        stop_worker
    fi
    exit "$1"
}

# shellcheck disable=SC2329  # called through a trap
release_serial_lock() {
    if [ -n "$serial_lock" ] && [ "$(cat "$serial_lock/pid" 2>/dev/null || true)" = "$$" ]; then
        rm -rf "$serial_lock"
    fi
    serial_lock=""
}

# The mtime of a path in epoch seconds: GNU stat first, then BSD stat. A
# failed probe prints nothing on stdout, so the fallback is clean.
path_mtime() {
    stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || date +%s
}

# Taking over a stale lock is itself guarded: two waiters that both saw the
# same dead holder must not both remove the lock, or the second would remove
# the fresh lock the first just took. Inside the guard the holder is read
# again, and the lock is removed only if it is still the stale one.
take_over_stale_lock() {
    stale_holder="$1"
    guard="$lock_root/$canonical.takeover"
    if ! mkdir "$guard" 2>/dev/null; then
        if [ $(( $(date +%s) - $(path_mtime "$guard") )) -gt 10 ]; then
            rmdir "$guard" 2>/dev/null || true
        fi
        return 0
    fi
    current="$(cat "$lock/pid" 2>/dev/null || true)"
    if [ "$current" = "$stale_holder" ]; then
        rm -rf "$lock"
    fi
    rmdir "$guard" 2>/dev/null || true
}

acquire_serial_lock() {
    # CONDUCTOR_DISPATCH_SERIAL_LOCK_DIR lets a caller that keeps its own log
    # directory (the offline harness eval) still queue behind live dispatch
    # workers on the same provider. Unset, the lock stays under the log dir.
    lock_root="${CONDUCTOR_DISPATCH_SERIAL_LOCK_DIR:-$log_dir/locks}"
    mkdir -p "$lock_root"
    chmod 700 "$lock_root"
    lock="$lock_root/$canonical.lock"
    waited=0
    announced=0
    while ! mkdir "$lock" 2>/dev/null; do
        holder="$(cat "$lock/pid" 2>/dev/null || true)"
        if [ -n "$holder" ] && ! kill -0 "$holder" 2>/dev/null; then
            # The holder is gone without releasing: take the lock over.
            take_over_stale_lock "$holder"
            continue
        fi
        if [ -z "$holder" ] && [ -d "$lock" ]; then
            # A lock directory without a pid file older than a few seconds
            # was left by a launch that died between mkdir and the pid write.
            age=$(( $(date +%s) - $(path_mtime "$lock") ))
            if [ "$age" -gt 10 ]; then
                take_over_stale_lock ""
                continue
            fi
        fi
        if [ "$waited" -ge "$serial_wait_seconds" ]; then
            log "$PROG: $canonical serial lock still held after ${serial_wait_seconds}s, giving up"
            exit "$EXIT_RATE_LIMITED"
        fi
        if [ "$announced" -eq 0 ] || [ $((waited % serial_heartbeat_seconds)) -eq 0 ]; then
            printf '[serial] waiting for another %s worker to finish (%ss)\n' "$canonical" "$waited"
            announced=1
        fi
        sleep 1
        waited=$((waited + 1))
    done
    printf '%s\n' "$$" > "$lock/pid"
    serial_lock="$lock"
}

case " $(printf '%s' "$serial_providers" | tr ',' ' ') " in
    *" $canonical "*)
        # Until the worker's own traps are set, a stop signal ends the
        # launch through the EXIT trap, which gives the lock back. A worker
        # already started in that window is stopped first: the lock is never
        # given back while a worker still runs.
        trap 'serial_early_signal 143' TERM
        trap 'serial_early_signal 130' INT
        trap 'serial_early_signal 129' HUP
        trap release_serial_lock EXIT
        acquire_serial_lock
        ;;
esac

if [ "$canonical" = "deepseek" ]; then
    deepseek_prepare_git || true
fi

# The worker's output goes to the run log file AND to stdout, which the
# caller collects. A FIFO keeps the worker's own exit code intact: waiting on
# a pipeline would report the exit code of the last stage instead.
rm -f "$fifo"
mkfifo "$fifo"

tee -a "$log_file" < "$fifo" &
tee_pid=$!

# The worker runs in the caller's process group, with the helpers: one
# cancellation boundary for the Worker, its children, the tee and the
# helpers. The wrapper traps the stop signal and exits with a normal status:
# a job that dies of a signal is reported by the shell as "Terminated", on
# the stderr the caller reads next to the worker's output.
(
    trap 'exit 143' TERM INT HUP
    if [ "${#context_env[@]}" -gt 0 ]; then
        export "${context_env[@]}"
    fi
    run_worker > "$fifo" 2>&1
) &
worker_pid=$!

timeout_seconds="${CONDUCTOR_DISPATCH_LAUNCH_TIMEOUT:-5400}"
stall_seconds="${CONDUCTOR_DISPATCH_LAUNCH_STALL:-900}"

# How long the worker is given to shut down cleanly before it is killed.
stop_grace="${CONDUCTOR_DISPATCH_LAUNCH_STOP_GRACE:-10}"

# Whether this script leads the process group it runs in. When it does, the
# group holds nothing but this script and what it started, and the group can
# be reaped as a whole. A script that shares its caller's group (a direct run
# from a shell) must never signal that group: the group belongs to the
# caller, and the caller may have other work in it.
script_leads_group=0
if [ "$(ps -o pgid= -p $$ 2>/dev/null | tr -d ' ')" = "$$" ]; then
    script_leads_group=1
fi

# Why the run ended, when it did not end on the worker's own terms. A file,
# because the watchdog and the timeout helper are separate processes and a
# variable set in a subshell would not be visible here.
reason_file="$log_dir/$canonical-$run_stamp.reason"

# The pids the worker started, deepest child first.
worker_descendants() {
    local parent="$1" child
    for child in $(_child_pids "$parent"); do
        worker_descendants "$child"
        printf '%s\n' "$child"
    done
    return 0
}

# The direct children of a pid.
_child_pids() {
    if command -v pgrep >/dev/null 2>&1; then
        pgrep -P "$1" 2>/dev/null || true
    else
        ps -A -o pid=,ppid= 2>/dev/null | awk -v p="$1" '$2 == p { print $1 }' || true
    fi
    return 0
}

# The pids in a process group, excluding processes the launcher shell itself
# forked (a `$(...)` pipeline's subshell, ps, awk, a sleep): they are in the
# group for a moment and would otherwise be enumerated as survivors by the
# very enumeration that is running. A bare "is its parent the shell" check
# misses the pipeline's children (they are the substitution subshell's, not
# the shell's), so the parent chain is walked instead: anything descending
# from the launcher shell is dropped, and only processes the worker side
# forked are kept. A member's ancestors are in the group whenever it is,
# because a process inherits its group until it changes it itself, which
# nothing in a launch run does.
_group_members() {
    ps -A -o pid=,pgid=,ppid= 2>/dev/null |
        awk -v g="$1" -v own="$2" '
            $2 == g { rows[NR] = $1 " " $3 }
            END {
                desc[own] = 1
                changed = 1
                while (changed) {
                    changed = 0
                    for (i = 1; i <= NR; i++) {
                        split(rows[i], parts, " ")
                        if (!(parts[1] in desc) && (parts[2] in desc)) {
                            desc[parts[1]] = 1
                            changed = 1
                        }
                    }
                }
                for (i = 1; i <= NR; i++) {
                    split(rows[i], parts, " ")
                    if (parts[1] in desc) continue
                    print parts[1]
                }
            }' || true
    return 0
}

# Reap the group this script leads. A worker can leave a child behind: a
# child it forked and detached is re-parented to init, so it is no longer in
# the worker's tree and the tree walk cannot reach it, but it is still in
# this script's group and still holds the run's pipe open. The group is
# swept once the worker is done, so no worker is left running in the
# worktree. The tee is spared: it still has to flush the output.
#
# The sweep re-enumerates the group every round: a member's TERM handler can
# fork a replacement while the grace is running, and a replacement is in the
# same group with the same fate. Each round TERMs the members it sees, waits
# one second, KILLs whatever exists now, and looks again; an empty group
# returns success. If the group still is not empty after stop_grace rounds,
# the sweep prints the surviving pids and returns non-zero — the launch must
# not report success while a member of the run's group is still alive.
sweep_group() {
    local pgid pid survivors rounds
    [ "$script_leads_group" -eq 1 ] || return 0
    pgid="$(ps -o pgid= -p $$ 2>/dev/null | tr -d ' ')"
    [ -n "$pgid" ] || return 0
    rounds=0
    while :; do
        survivors=""
        for pid in $(_group_members "$pgid" "$$"); do
            [ "$pid" = "$$" ] && continue
            [ "$pid" = "$tee_pid" ] && continue
            survivors="$survivors $pid"
        done
        [ -n "$survivors" ] || return 0
        if [ "$rounds" -ge "$stop_grace" ]; then
            echo "dispatch_launch: process group $pgid still has live members" \
                "after the sweep:$survivors" >&2
            return 1
        fi
        rounds=$((rounds + 1))
        for pid in $survivors; do
            kill -TERM "$pid" 2>/dev/null || true
        done
        sleep 1
        # Not the set from above: the grace may have seen members fork
        # replacements and die themselves.
        survivors=""
        for pid in $(_group_members "$pgid" "$$"); do
            [ "$pid" = "$$" ] && continue
            [ "$pid" = "$tee_pid" ] && continue
            survivors="$survivors $pid"
        done
        for pid in $survivors; do
            kill -KILL "$pid" 2>/dev/null || true
        done
        sleep 1
    done
}

# Signal the worker and the pids given, deepest child first: a child that is
# still running when its parent dies is the one that would be left behind.
_signal_tree() {
    local signal="$1" pids="$2" pid
    for pid in $pids; do
        kill "-$signal" "$pid" 2>/dev/null || true
    done
    kill "-$signal" "$worker_pid" 2>/dev/null || true
    return 0
}

# Stop the worker and everything it started. SIGTERM first, so a worker that
# can exit cleanly does; then SIGKILL after a bounded grace, because a CLI
# that traps or ignores SIGTERM must not be able to hold the run, the
# worktree and the dispatcher open forever.
stop_worker() {
    local pids waited
    pids="$(worker_descendants "$worker_pid")"
    _signal_tree TERM "$pids"
    waited=0
    while [ "$waited" -lt "$stop_grace" ]; do
        if ! kill -0 "$worker_pid" 2>/dev/null; then
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done
    # Whatever is left: the pids we knew, and anything that appeared since.
    pids="$(worker_descendants "$worker_pid") $pids"
    _signal_tree KILL "$pids"
    return 0
}

# The dispatcher cancels a launch by killing the script's process group, so
# the worker goes with it. A stop that arrives as a signal the script can
# catch still stops the worker first, so no worker is left behind.
# shellcheck disable=SC2329  # called through a trap
on_signal() {
    trap - TERM INT HUP
    stop_worker
    deepseek_worker_status="$1"
    exit "$1"
}

# A run that ends for any other reason (a failed tee, a write error) must not
# leave the worker behind either.
# shellcheck disable=SC2329  # called through a trap
on_exit() {
    if [ -n "${worker_pid:-}" ] && kill -0 "$worker_pid" 2>/dev/null; then
        stop_worker
    fi
    # A stop by signal, a timeout or a failed write: the Worker's objects
    # still reach the store before the launch ends, once nothing of the
    # Worker's is left in the group to keep writing.
    if [ -n "$deepseek_admin" ] && [ "$deepseek_finished" -eq 0 ]; then
        if sweep_group; then
            deepseek_finish_git || true
        else
            deepseek_keep_unmigrated "the Worker's process group is not empty" || true
        fi
    fi
    release_serial_lock
}

trap 'on_signal 143' TERM
trap 'on_signal 130' INT
trap 'on_signal 129' HUP
trap on_exit EXIT

# The watchdog watches the log for GROWTH, not for elapsed time: a worker
# that is still producing output is working, however long it takes. It stops
# by itself once the worker is gone, so it is never signalled.
watchdog() {
    last_size=0
    quiet=0
    while kill -0 "$worker_pid" 2>/dev/null; do
        sleep 1
        # The worker may have finished while the watchdog slept. A run that
        # ended is never a stall: the completion wins over the watchdog.
        if ! kill -0 "$worker_pid" 2>/dev/null; then
            return 0
        fi
        if [ ! -f "$log_file" ]; then
            continue
        fi
        size=$(wc -c < "$log_file" 2>/dev/null || printf '0')
        if [ "$size" -gt "$last_size" ]; then
            last_size="$size"
            quiet=0
        else
            quiet=$((quiet + 1))
            if [ "$quiet" -ge "$stall_seconds" ]; then
                printf '[watchdog] no log growth for %ss, stopping the worker\n' "$stall_seconds" >> "$log_file"
                printf 'stall\n' > "$reason_file"
                stop_worker
                return 0
            fi
        fi
    done
    return 0
}

watchdog &
watchdog_pid=$!

# The overall timeout is a wall clock limit on the whole run. It polls in
# short steps and stops by itself once the worker is gone, so the run is not
# held open for the whole timeout after the work has finished.
(
    deadline=$(( $(date +%s) + timeout_seconds ))
    while kill -0 "$worker_pid" 2>/dev/null; do
        if [ "$(date +%s)" -ge "$deadline" ]; then
            printf 'timeout\n' > "$reason_file"
            stop_worker
            break
        fi
        sleep 1
    done
) &
timeout_pid=$!

set +e
wait "$worker_pid"
status=$?
set -e

# The helpers stop by themselves when the worker is gone; waiting for them
# here is what keeps them from outliving this script.
wait "$watchdog_pid" 2>/dev/null || true
wait "$timeout_pid" 2>/dev/null || true

# A worker that left a child behind is reaped with the group, before the
# pipe is waited for: the child is what holds the pipe open. A group that
# resists the whole sweep fails the launch — but only after the run log and
# the fifo are torn down, so the failure report is complete.
sweep_status=0
sweep_group || sweep_status=$?

# The tee ends when the last writer closes the FIFO. A child that was left
# behind keeps it open, so the wait is bounded: the run must not hang on a
# pipe.
tee_waited=0
while [ "$tee_waited" -lt "$stop_grace" ] && kill -0 "$tee_pid" 2>/dev/null; do
    sleep 1
    tee_waited=$((tee_waited + 1))
done
if kill -0 "$tee_pid" 2>/dev/null; then
    kill -TERM "$tee_pid" 2>/dev/null || true
    sleep 1
    kill -KILL "$tee_pid" 2>/dev/null || true
fi
wait "$tee_pid" 2>/dev/null || true

rm -f "$fifo"

# The Worker and its group are gone (swept above): its objects move into the
# store now, before the caller reads the branch. Commits that never reached
# the store outrank every other result: the caller sees 79 with the
# objects_not_migrated line, whatever the Worker did, and the quarantine is
# kept for the next launch.
deepseek_worker_status="$status"
if [ -f "$reason_file" ]; then
    case "$status" in
        143|137) deepseek_worker_status="$EXIT_TIMEOUT" ;;
    esac
fi
if [ "$sweep_status" -ne 0 ]; then
    # Live members of the group: moving the objects now would race them.
    if ! deepseek_keep_unmigrated "the Worker's process group is not empty"; then
        rm -f "$reason_file"
        exit "$EXIT_WORKER_FAILED"
    fi
elif ! deepseek_finish_git; then
    rm -f "$reason_file"
    exit "$EXIT_WORKER_FAILED"
fi

# A worker stopped by the timeout or the watchdog did not choose its own exit
# code: the caller must see 124, not the signal death (128 + SIGTERM). A
# worker that finished on its own keeps its own code, even when the watchdog
# noticed the end a moment later: a completed run is never a stall.
if [ -f "$reason_file" ]; then
    rm -f "$reason_file"
    case "$status" in
        143|137) exit "$EXIT_TIMEOUT" ;;
    esac
fi

# The sweep result outranks a worker's own success code: a launch that left
# live group members behind is not a success, whatever the worker printed.
if [ "$sweep_status" -ne 0 ]; then
    exit "$sweep_status"
fi

# ----------------------------------------------------------- classification

# A worker that failed on its own terms is classified: the class the
# classifier names tells the caller what to do next (retry with a backoff,
# park the line, rotate the session), and the worker's own exit code does
# not. The script never reads the worker's output to decide that: it hands
# the classifier the file the output was collected in, and reads the class
# the classifier names. The raw output of a worker therefore never reaches
# the exit-code path.
#
# The repository root is resolved from this script's own location, so the
# classifier is found no matter which directory the script is called from.
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd || true)"

# The repository's own virtualenv when it has one: the classifier runs with
# the dependencies it was written against. A plain python3 otherwise.
classifier_python="$repo_root/.venv/bin/python"
if [ ! -x "$classifier_python" ]; then
    classifier_python="python3"
fi

# WORKER_FAILURE_CLASSIFIER_CMD replaces the command that runs the
# classifier. It is a seam for the tests, which point it at a fake that
# prints a chosen class; it is not a runtime setting. Unset, the real entry
# point runs.
if [ -n "${WORKER_FAILURE_CLASSIFIER_CMD:-}" ]; then
    # The override is a command line — the interpreter and what it runs — and
    # is split into words on purpose.
    IFS=' ' read -r -a classifier_argv <<< "$WORKER_FAILURE_CLASSIFIER_CMD"
else
    classifier_argv=("$classifier_python" -m "src.llms.native_cli.failure_cli")
fi

# How long the classifier may take. It reads one file and prints one line,
# so a few seconds is generous; the bound is what keeps a stuck classifier
# from holding the launch open.
classify_timeout="${WORKER_FAILURE_CLASSIFIER_TIMEOUT:-10}"

# The failure class of a failed worker, on stdout — or nothing at all when
# the classifier could not be reached, did not finish in time, or did not
# answer with a class. The diagnostic is passed by path: the script itself
# never reads it.
classify_worker_failure() {
    local provider="$1" worker_status="$2" diagnostic="$3"
    local out_file err_file pid deadline classifier_status timed_out failure_class

    out_file="$log_dir/$canonical-$run_stamp.classify.out"
    err_file="$log_dir/$canonical-$run_stamp.classify.err"

    # exec: the subshell becomes the classifier, so the pid held here is the
    # classifier's own and a timeout can stop it.
    (
        cd "$repo_root" &&
            exec "${classifier_argv[@]}" \
                --provider "$provider" \
                --diagnostic-file "$diagnostic" \
                --exit-code "$worker_status"
    ) > "$out_file" 2> "$err_file" &
    pid=$!

    deadline=$(( $(date +%s) + classify_timeout ))
    timed_out=0
    while kill -0 "$pid" 2>/dev/null; do
        if [ "$(date +%s)" -ge "$deadline" ]; then
            timed_out=1
            break
        fi
        # The poll is short, so a classifier that answers at once does not
        # cost a second of the run. A sleep that cannot do fractions falls
        # back to a whole second.
        sleep 0.2 2>/dev/null || sleep 1
    done
    if [ "$timed_out" -eq 1 ]; then
        # A classifier that hangs must not hold the launch open: it is
        # stopped, and the run continues without a classification.
        kill -TERM "$pid" 2>/dev/null || true
        sleep 1
        kill -KILL "$pid" 2>/dev/null || true
    fi

    classifier_status=0
    wait "$pid" 2>/dev/null || classifier_status=$?

    # Only the class is the caller's business. The files the classifier
    # wrote are removed, so a classifier that echoed the diagnostic leaves
    # nothing behind.
    failure_class=""
    if [ "$timed_out" -eq 0 ] && [ "$classifier_status" -eq 0 ]; then
        failure_class="$("$classifier_python" -c '
import json
import sys

try:
    payload = json.loads(sys.stdin.read() or "{}")
except ValueError:
    raise SystemExit(0)
if isinstance(payload, dict):
    value = payload.get("failure_class")
    if isinstance(value, str):
        sys.stdout.write(value)
' < "$out_file" 2>/dev/null || true)"
    fi
    rm -f "$out_file" "$err_file"

    printf '%s' "$failure_class"
}

# ------------------------------------------------------------- the exit code

if [ "$status" -ne 0 ]; then
    # Only a worker that wrote something has a diagnostic to classify. A
    # worker that failed in silence gave the caller no diagnostic at all,
    # and its own exit code is the only information there is.
    if [ -s "$log_file" ]; then
        # The classifier runs with the repository root as its working
        # directory, so the diagnostic is named by an absolute path.
        absolute_log_dir="$(cd "$log_dir" && pwd)"
        failure_class="$(classify_worker_failure \
            "$canonical" "$status" "$absolute_log_dir/$canonical-$run_stamp.log")"
        case "$failure_class" in
            rate_limited)      exit "$EXIT_RATE_LIMITED" ;;
            quota_exhausted)   exit "$EXIT_QUOTA" ;;
            auth_failed)       exit "$EXIT_AUTH" ;;
            context_exhausted) exit "$EXIT_CONTEXT" ;;
            # A class the classifier did not name, and no class at all: the
            # worker failed, and nothing more is known about why.
            *)                 exit "$EXIT_WORKER_FAILED" ;;
        esac
    fi
    # The codes 75 to 78 belong to the classes the classifier names. A
    # worker that exited with one of them classified nothing, and a caller
    # must not read a rate limit that was never classified.
    case "$status" in
        75|76|77|78) exit "$EXIT_WORKER_FAILED" ;;
    esac
fi

exit "$status"
