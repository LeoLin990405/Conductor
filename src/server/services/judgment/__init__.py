"""Typed fast-judgment port for the Super-agent (N61).

System One answers narrow typed questions with probabilities; System Two
(the Super-agent's LLM roles) handles open reasoning. Every use ships in
shadow: it records what it would have decided and changes nothing.
"""

from .backends.llm import LLMJudgmentBackend
from .backends.local import LocalJudgmentBackend, unavailable_reason
from .port import JudgmentBackend, JudgmentPort, default_port
from .record import (
    JsonlRecorder,
    JudgmentRecord,
    MemoryRecorder,
    NullRecorder,
    Recorder,
    default_recorder,
    digest_state,
)
from .types import (
    BoolQuestion,
    ChoiceQuestion,
    JudgmentRequest,
    JudgmentResult,
    JudgmentSkipped,
    JudgmentStatus,
    Question,
    ScoreQuestion,
)
from .uses import (
    COMPLETION_EVIDENCE,
    ROUTER_PRESELECT,
    USES,
    WATCHDOG_PROGRESS,
    Fallback,
    UseSpec,
    get_use,
    in_sample,
    use_mode,
    use_sample,
)

__all__ = [
    "BoolQuestion",
    "COMPLETION_EVIDENCE",
    "ChoiceQuestion",
    "Fallback",
    "JsonlRecorder",
    "JudgmentBackend",
    "JudgmentPort",
    "JudgmentRecord",
    "JudgmentRequest",
    "JudgmentResult",
    "JudgmentSkipped",
    "JudgmentStatus",
    "LLMJudgmentBackend",
    "LocalJudgmentBackend",
    "MemoryRecorder",
    "NullRecorder",
    "Question",
    "ROUTER_PRESELECT",
    "Recorder",
    "ScoreQuestion",
    "USES",
    "UseSpec",
    "WATCHDOG_PROGRESS",
    "default_port",
    "default_recorder",
    "digest_state",
    "get_use",
    "in_sample",
    "unavailable_reason",
    "use_mode",
    "use_sample",
]
