"""Built-in subagent definitions (Chinese-first prompts for the CN build).

These are the default subagents shipped with the agent. A definition in
``agent_config.yaml`` with one's name changes only the fields it sets.
Descriptions are what the parent agent reads when choosing whom to delegate
to, so they say when to use the role and what it can do; the role prompts
themselves live under ``prompts/templates/subagents/roles/``.
"""

from __future__ import annotations

from ptc_agent.agent.subagent_registry.definition import SubagentDefinition

BUILTIN_SUBAGENTS: dict[str, SubagentDefinition] = {
    "research": SubagentDefinition(
        name="research",
        description=(
            "把研究任务交给研究子 Agent。一次只给它一个明确主题或问题；"
            "它会检索公开信息，并给出带引用的发现。适合资料查找、事实核对、"
            "行业或公司背景研究。"
        ),
        role_prompt_template="roles/researcher.md.j2",
        tools=["web_search", "filesystem"],
        sections={
            "workspace_paths": False,
            "tool_guide": False,
            "data_processing": False,
            "visualizations": False,
        },
    ),
    "general-purpose": SubagentDefinition(
        name="general-purpose",
        description=(
            "把复杂任务交给通用子 Agent。它拥有全部文件工具（read/write/edit/"
            "glob/grep/bash），并能运行 Python 代码调用 MCP 工具；适合多步操作、"
            "数据处理、文件生成与改写，或任何需要完整工具权限的任务。"
        ),
        role_prompt_template="roles/general.md.j2",
        tools=["execute_code", "filesystem", "bash", "web_search"],
        memory=True,
        sections={
            "workspace_paths": True,
            "tool_guide": True,
            "data_processing": True,
            "visualizations": True,
        },
    ),
    "report-builder": SubagentDefinition(
        name="report-builder",
        description=(
            "把文档制作交给报告构建子 Agent。它专门产出成品 DOCX 报告、XLSX 模型、"
            "PPTX 演示与 PDF；把分析结果与目标格式交给它即可，它会先加载对应格式技能"
            "（xlsx/docx/pptx/pdf）。"
        ),
        role_prompt_template="roles/report_builder.md.j2",
        tools=["execute_code", "filesystem", "bash"],
        sections={
            "workspace_paths": True,
            "tool_guide": True,
            "data_processing": False,
            "visualizations": True,
        },
    ),
}
