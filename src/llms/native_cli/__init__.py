"""Native agent CLI adapters.

Most adapters wrap coding-agent CLIs with native tool and session behavior.
Doubao's ArkCLI, MiniMax's ``mmx`` and Zhipu's ``zcode -p`` adapters expose
text results without native tool or session events. Their output does not
establish what tools a vendor CLI may use internally.

The DeepSeek adapter is the first reference because DSH's headless profile is
the official native invocation mode. Codex, Claude Code and Cursor cover the
international agent CLIs; Kimi, ZCode, MiniMax Code, MiMo Code,
Grok and StepFun add the Chinese and other vendor backends. Adapter registration
describes a command protocol; it does not assert that a CLI is installed or logged
in on this host.
"""

from src.llms.native_cli.base import (
    NativeCliError,
    NativeCliResult,
    NativeCliUnavailable,
    NativeCliUsage,
    ReadOnlyUnavailable,
)
from src.llms.native_cli.claude import ClaudeCodeAdapter
from src.llms.native_cli.cursor import CursorAgentAdapter
from src.llms.native_cli.codex import CodexExecAdapter
from src.llms.native_cli.deepseek import DeepSeekDshAdapter
from src.llms.native_cli.doubao import DoubaoArkCliAdapter
from src.llms.native_cli.failure import (
    FailureClassification,
    FailureClass,
    FailureRule,
    LearnedRule,
    RuleSet,
    Signature,
    classify_failure,
    failure_rules,
    failure_signature,
    register_failure_rule,
)
from src.llms.native_cli.gemini import GeminiCliAdapter
from src.llms.native_cli.kimi import KimiCliAdapter
from src.llms.native_cli.minimax import MiniMaxCodeAdapter
from src.llms.native_cli.minimax_mmx import MiniMaxMmxAdapter
from src.llms.native_cli.mimo import MiMoCodeAdapter
from src.llms.native_cli.qwen import QwenCliAdapter
from src.llms.native_cli.zcode import ZCodeCliAdapter
from src.llms.native_cli.zhipu import ZhipuCliAdapter
from src.llms.native_cli.registry import (
    context_window_tokens,
    get_adapter,
    get_read_only_adapter,
    list_adapters,
)
from src.llms.native_cli.session_manager import (
    RotationResult,
    SessionRecord,
    WorkerSessionManager,
)
from src.llms.native_cli.sessions import NativeCliSessionStore, run_with_session

__all__ = [
    "ClaudeCodeAdapter",
    "CursorAgentAdapter",
    "CodexExecAdapter",
    "DeepSeekDshAdapter",
    "DoubaoArkCliAdapter",
    "FailureClassification",
    "FailureClass",
    "FailureRule",
    "GeminiCliAdapter",
    "KimiCliAdapter",
    "LearnedRule",
    "MiniMaxCodeAdapter",
    "MiniMaxMmxAdapter",
    "MiMoCodeAdapter",
    "QwenCliAdapter",
    "RotationResult",
    "RuleSet",
    "SessionRecord",
    "Signature",
    "WorkerSessionManager",
    "ZCodeCliAdapter",
    "ZhipuCliAdapter",
    "NativeCliError",
    "NativeCliResult",
    "NativeCliUnavailable",
    "NativeCliSessionStore",
    "NativeCliUsage",
    "ReadOnlyUnavailable",
    "classify_failure",
    "context_window_tokens",
    "failure_rules",
    "failure_signature",
    "get_adapter",
    "get_read_only_adapter",
    "register_failure_rule",
    "run_with_session",
    "list_adapters",
]
