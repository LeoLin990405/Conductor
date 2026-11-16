"""Central provider roles and task eligibility rules.

Some providers do not expose the tools needed to act on a workspace. Keep
those providers on explicit, narrow paths instead of treating every model as
a general native agent.
"""

from __future__ import annotations

import re
from types import MappingProxyType

# Providers absent from this table use the normal native-agent route. Entries
# here are restrictions, so a caller cannot widen them with a local override.
PROVIDER_ROLES = MappingProxyType({"manus": "research"})

_CODE_TASK_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(write|implement|build|modify|edit|refactor|patch|fix|debug|develop|create|code)\b.{0,100}\b(code|script|software|program|function|class|module|parser|bug|test|repo|repository|app|application|tool|website|site|python|javascript|typescript|java|rust|go)\b",
        r"\b(code|script|software|program|function|class|module|parser|bug|test|repo|repository|app|application|tool|website|site|python|javascript|typescript|java|rust|go)\b.{0,100}\b(write|implement|build|modify|edit|refactor|patch|fix|debug|develop|create)\b",
        r"\b(write|implement|build|modify|edit|refactor|patch|fix|debug|develop|create)\b.{0,80}\b(a|an|the)?\s*(app|application|tool|website|site|program|script|software)\b",
        r"\bcode\s+(?:a|an|the)\s+[\w-]+",
        r"\bcode\s+[\w-]+\s+in\s+(python|javascript|typescript|java|rust|go)\b",
        r"\b(write|implement|build|modify|edit|refactor|patch|fix|debug|develop)\b.{0,60}\bin\s+(python|javascript|typescript|java|rust|go)\b",
        r"\b(run|execute|launch)\b.{0,80}\b(command|shell|bash|python|script|code|test suite|tests?)\b",
        r"\b(run|execute|launch)\b.{0,40}\b(tests?|pytest|npm|pnpm|cargo|make)\b",
        r"\b(how to|plan|design|think through|deliberate on)\b.{0,100}\b(implement|refactor|debug|fix|write|change)\b.{0,60}\b(code|software|repo|repository|parser|test|bug)\b",
        r"\b(code|software|repo|repository|test|bug)\b.{0,100}\b(implementation plan|design the implementation|deliberate|think through implementation)\b",
        r"\b(think through|deliberate on|plan|design)\b.{0,80}\b(implement|refactor|debug|fix|write|change|coding)\b.{0,80}\b(code|software|repo|repository|test|bug|plan|approach)\b",
        r"\b(code|software|repo|repository|test|bug)\b.{0,80}\b(implementation plan|refactor plan|coding strategy|design the implementation|deliberate|think through implementation)\b",
        r"\b(execute|run|launch)\s+(ls|pwd|cd|cat|grep|find|git|python\b|node\b|npm\b|pnpm\b|pytest\b|make\b|bash\b|sh\b|curl\b|command\b|script\b|code\b)",
        r"(写|编写|实现|修改|编辑|重构|修复|调试|开发|创建|构建).{0,30}(代码|脚本|程序|软件|函数|类|模块|解析器|测试|仓库|应用|工具|网站|python|javascript|typescript|java|rust|go)",
        r"(代码|脚本|程序|软件|函数|类|模块|解析器|测试|仓库|应用|工具|网站).{0,30}(写|编写|实现|修改|编辑|重构|修复|调试|开发|创建|构建)",
        r"(写|编写|实现|修改|编辑|重构|修复|调试|开发|创建|构建).{0,24}(一个|一份|个)?\s*(app|application|python|javascript|typescript|java|rust|go)",
        r"(执行|运行|启动).{0,20}(命令|shell|bash|python|脚本|代码|测试|ls|pwd|git|npm|pnpm|pytest|make)",
        r"(怎么|如何|计划|设计|推演|思考|讨论).{0,30}(实现|重构|调试|修复|编写|修改).{0,30}(代码|软件|仓库|测试|程序|应用)",
    )
)


class ProviderRoleError(ValueError):
    """The requested provider role cannot perform the task safely."""


def provider_role(provider: str) -> str:
    """Return a provider's restricted role or ``native`` for normal agents."""
    return PROVIDER_ROLES.get(str(provider or "").strip().lower(), "native")


def is_code_task(task: str) -> bool:
    """Recognize coding, execution, and implementation-deliberation requests."""
    text = re.sub(r"\s+", " ", str(task or ""))
    return any(pattern.search(text) for pattern in _CODE_TASK_PATTERNS)


_RESEARCH_TASK_KINDS = frozenset(
    {"research", "information_retrieval", "reference_lookup", "literature_review"}
)
_CODE_TASK_KINDS = frozenset(
    {"coding", "code_writing", "execution", "deliberation_coding", "implementation"}
)


def _task_kind_allowed(task_kind: str | None) -> bool:
    if task_kind is None:
        return True
    kind = task_kind.strip().lower().replace("-", "_")
    if kind in _CODE_TASK_KINDS:
        return False
    return kind in _RESEARCH_TASK_KINDS


def provider_task_eligible(
    provider: str,
    task: str,
    *,
    role: str | None = None,
    task_kind: str | None = None,
) -> bool:
    """Check a provider's immutable role against a requested task."""
    actual_role = provider_role(provider)
    if role is not None and actual_role != role:
        return False
    return not (
        actual_role == "research"
        and (is_code_task(task) or not _task_kind_allowed(task_kind))
    )


def require_provider_role(
    provider: str,
    role: str,
    task: str = "",
    *,
    task_kind: str | None = None,
) -> str:
    """Enforce a provider role at the execution boundary and return it."""
    actual_role = provider_role(provider)
    if actual_role != role:
        raise ProviderRoleError(
            f"provider {provider!r} has role {actual_role!r}, not {role!r}"
        )
    if actual_role == "research" and (
        is_code_task(task) or not _task_kind_allowed(task_kind)
    ):
        raise ProviderRoleError(
            f"provider {provider!r} is research-only and cannot handle coding or execution tasks"
        )
    return actual_role


def route_research_provider(
    task: str,
    *,
    preferred_provider: str | None = None,
    task_kind: str | None = None,
) -> str | None:
    """Choose a research-role provider without exposing it as a coding agent."""
    preferred = str(preferred_provider or "").strip().lower()
    if preferred and provider_role(preferred) != "research":
        return None
    provider = preferred or "manus"
    return (
        provider
        if provider_task_eligible(
            provider, task, role="research", task_kind=task_kind
        )
        else None
    )
