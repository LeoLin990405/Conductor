"""N73 assignment records: frozen choices, feasibility verdicts, and ERROR."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from src.tools.auto_dispatch.superagent_assign.types import (
    ASSIGNMENT_VERSION,
    DEFAULT_MODE,
    AssignmentPlan,
    Confidence,
    Effort,
    ErrorKind,
    ErrorResult,
    FeasibilityResult,
    LineChoice,
    Mode,
    Source,
    call_error,
    format_error,
    is_error,
    timeout_error,
)

_REPO = Path(__file__).resolve().parents[5]
_PACKAGE = _REPO / "src" / "tools" / "auto_dispatch" / "superagent_assign"
_SIBLINGS = ("effort", "feasibility", "context", "backend", "record", "modes")


def _choice(**overrides: object) -> LineChoice:
    raw: dict[str, object] = {
        "line_id": "line-0",
        "model": "stepfun",
        "model_id": "step-3.7-flash",
        "effort": Effort.HIGH,
        "runtime": "native_cli",
        "reason": "stepfun passed 9/10 on src/web; grok headroom is 0.25",
        "confidence": Confidence.MEDIUM,
    }
    raw.update(overrides)
    return LineChoice(**raw)  # type: ignore[arg-type]


def _resolve_schema(schema: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    """Follow one local ``$ref``. Pydantic puts nested models and enums in ``$defs``."""
    if "$ref" in node:
        name = node["$ref"].rsplit("/", 1)[-1]
        return schema["$defs"][name]
    return node


def _imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.append(node.module or "")
    return found


def test_the_vocabularies_are_the_prd_sets() -> None:
    assert ASSIGNMENT_VERSION == 1
    assert DEFAULT_MODE is Mode.SHADOW
    assert tuple(Effort) == (
        Effort.DEFAULT,
        Effort.LOW,
        Effort.MEDIUM,
        Effort.HIGH,
        Effort.MAX,
    )
    assert [item.value for item in Effort] == [
        "default",
        "low",
        "medium",
        "high",
        "max",
    ]
    assert [item.value for item in Confidence] == ["high", "medium", "low"]
    assert [item.value for item in Mode] == ["off", "shadow", "enforce"]
    assert [item.value for item in Source] == [
        "superagent",
        "superagent_reask",
        "selector_fallback",
        "selector",
    ]
    assert [item.value for item in ErrorKind] == ["call_error", "timeout", "bad_format"]


@pytest.mark.parametrize("effort", ["n/a", "xhigh", "ultra", "minimal", "off", ""])
def test_effort_rejects_cli_spellings_and_blanks(effort: str) -> None:
    with pytest.raises(ValueError, match="effort"):
        _choice(effort=effort)


def test_a_line_choice_is_frozen_and_round_trips() -> None:
    choice = _choice(model="  glm ", model_id="", effort="high", confidence="low")
    assert choice.model == "glm"
    assert choice.model_id == ""
    assert choice.effort is Effort.HIGH
    assert choice.confidence is Confidence.LOW
    with pytest.raises(ValidationError, match="frozen"):
        choice.model = "grok"  # type: ignore[misc]
    again = LineChoice.from_dict(json.loads(json.dumps(choice.to_dict())))
    assert again == choice
    assert hash(again) == hash(choice)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("line_id", "  "),
        ("model", ""),
        ("runtime", None),
        ("reason", "   "),
        ("confidence", "sure"),
        ("model_id", 5),
    ],
)
def test_a_line_choice_rejects_a_blank_or_mistyped_field(
    field: str, value: object
) -> None:
    with pytest.raises(ValueError):
        _choice(**{field: value})


def test_a_line_choice_rejects_a_field_the_model_does_not_pick() -> None:
    payload = _choice().to_dict()
    payload["cli_effort"] = "high"
    with pytest.raises(ValueError, match="unknown line choice field"):
        LineChoice.from_dict(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("line_id", "line 0"),
        ("model", "step fun"),
        ("model_id", "GLM 5.3"),
        ("runtime", "native cli"),
    ],
)
def test_identity_fields_are_single_tokens(field: str, value: str) -> None:
    """A roster name, model id, runtime, or line id has no internal space."""
    with pytest.raises(ValueError, match="single token"):
        _choice(**{field: value})


def test_surrounding_space_is_stripped_from_identity_tokens() -> None:
    choice = _choice(model_id="  step-3.7-flash  ", runtime=" native_cli ")
    assert choice.model_id == "step-3.7-flash"
    assert choice.runtime == "native_cli"


def test_a_plan_keeps_one_choice_per_line_and_round_trips() -> None:
    plan = AssignmentPlan(
        choices=(
            _choice(),
            _choice(line_id="line-1", model="kimi", effort=Effort.DEFAULT),
        )
    )
    assert plan.by_line()["line-1"].model == "kimi"
    assert AssignmentPlan.from_dict(json.loads(json.dumps(plan.to_dict()))) == plan
    assert AssignmentPlan().choices == ()
    with pytest.raises(ValidationError, match="frozen"):
        plan.choices = ()  # type: ignore[misc]
    with pytest.raises(ValueError, match="duplicate line_id"):
        AssignmentPlan(choices=(_choice(), _choice()))
    with pytest.raises(ValueError, match="duplicate line_id"):
        AssignmentPlan(choices=(_choice(), _choice(line_id=" line-0 ")))
    with pytest.raises(ValueError, match="LineChoice"):
        AssignmentPlan(choices=("line-0",))  # type: ignore[arg-type]


def test_a_plan_accepts_the_json_objects_the_fallback_validates() -> None:
    """Manual JSON fallback calls ``AssignmentPlan(**payload)``."""
    plan = AssignmentPlan(
        choices=[
            {
                "line_id": "line-0",
                "model": "stepfun",
                "effort": "high",
                "runtime": "native_cli",
                "reason": "success rate 9/10",
                "confidence": "high",
            }
        ]
    )
    choice = plan.choices[0]
    assert isinstance(choice, LineChoice)
    assert choice.model == "stepfun"
    assert choice.effort is Effort.HIGH
    assert choice.confidence is Confidence.HIGH
    assert choice.model_id == ""
    with pytest.raises(ValidationError, match="frozen"):
        plan.choices = ()  # type: ignore[misc]
    schema = AssignmentPlan.model_json_schema()
    choice_schema = _resolve_schema(schema, schema["properties"]["choices"]["items"])
    effort_schema = _resolve_schema(schema, choice_schema["properties"]["effort"])
    assert effort_schema["enum"] == [
        "default",
        "low",
        "medium",
        "high",
        "max",
    ]
    assert "confidence" in choice_schema["required"]
    assert "model_id" not in choice_schema["required"]
    # A failed parse retries by interpolating this schema. The call must exist.
    # The published text is the contract the model fills, not the implementation.
    published = f"{schema}"
    assert "AssignmentPlan" in published
    assert "One entry per line" in published
    assert "LangChain" not in published
    assert "dataclass" not in published
    with pytest.raises(ValueError, match="duplicate line_id"):
        AssignmentPlan(
            choices=[
                {
                    "line_id": "line-0",
                    "model": "stepfun",
                    "effort": "high",
                    "runtime": "native_cli",
                    "reason": "first",
                    "confidence": "high",
                },
                {
                    "line_id": "line-0",
                    "model": "kimi",
                    "effort": "low",
                    "runtime": "native_cli",
                    "reason": "second",
                    "confidence": "low",
                },
            ]
        )


class _FallbackMessage:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage_metadata = None
        self.response_metadata: dict[str, Any] = {}
        self.additional_kwargs: dict[str, Any] = {}


class _FallbackLLM:
    """Structured output misses, then one bad JSON object, then a valid plan."""

    def __init__(self) -> None:
        self.prompts: list[Any] = []
        self._calls = 0

    def with_structured_output(self, *_args: Any, **_kwargs: Any) -> "_FallbackLLM":
        return self

    async def ainvoke(self, messages: Any, *_args: Any, **_kwargs: Any) -> Any:
        self._calls += 1
        self.prompts.append(messages)
        if self._calls == 1:
            return {"parsed": None, "raw": _FallbackMessage("unused")}
        if self._calls == 2:
            return _FallbackMessage('{"choices": [{"line_id": "line-0"}]}')
        return _FallbackMessage(
            '{"choices": [{"line_id": "line-0", "model": "stepfun", '
            '"effort": "high", "runtime": "native_cli", '
            '"reason": "success rate 9/10", "confidence": "high"}]}'
        )


@pytest.mark.asyncio
async def test_manual_json_fallback_parses_a_plan_and_quotes_its_schema() -> None:
    from src.llms.api_call import make_api_call

    llm = _FallbackLLM()

    async def _no_sleep(*_args: Any, **_kwargs: Any) -> None:
        return None

    with (
        patch("langsmith.tracing_context") as ctx,
        patch("src.llms.api_call.asyncio.sleep", _no_sleep),
    ):
        ctx.return_value.__enter__ = lambda self: None
        ctx.return_value.__exit__ = lambda self, *a: None
        result = await make_api_call(
            llm,
            system_prompt="sys",
            user_prompt="user",
            response_schema=AssignmentPlan,
            max_parsing_retries=3,
        )

    assert isinstance(result, AssignmentPlan)
    assert result.choices[0].model == "stepfun"
    assert result.choices[0].effort is Effort.HIGH
    assert llm._calls == 3
    retry_prompt = str(llm.prompts[-1])
    assert "AssignmentPlan" in retry_prompt
    # Message repr escapes the schema dict. Drop those escapes, then compare quotes.
    normalized = retry_prompt.replace("\\", "").replace("'", '"')
    assert '"enum": ["default", "low", "medium", "high", "max"]' in normalized


def test_feasibility_is_ok_or_one_reason_token() -> None:
    ok = FeasibilityResult.ok()
    assert ok.is_ok is True
    assert ok.to_dict() == {"status": "ok"}
    assert FeasibilityResult.from_dict({"status": "ok"}) == ok
    blocked = FeasibilityResult.infeasible(" binary_missing ")
    assert blocked.is_ok is False
    assert blocked.reason_code == "binary_missing"
    assert blocked.to_dict() == {
        "status": "infeasible",
        "reason_code": "binary_missing",
    }
    assert FeasibilityResult.from_dict(blocked.to_dict()) == blocked
    with pytest.raises(FrozenInstanceError):
        blocked.reason_code = "other"  # type: ignore[misc]
    with pytest.raises(ValueError, match="reason_code"):
        FeasibilityResult(status="ok", reason_code="binary_missing")
    with pytest.raises(ValueError, match="reason_code"):
        FeasibilityResult.infeasible("binary missing")
    with pytest.raises(ValueError, match="reason_code"):
        FeasibilityResult.infeasible("  ")
    with pytest.raises(ValueError, match="status"):
        FeasibilityResult.from_dict({"status": "error"})


def test_error_results_are_not_choices() -> None:
    results = (
        call_error("connection reset"),
        timeout_error("deadline"),
        format_error("choices was a string"),
    )
    assert [item.kind for item in results] == [
        ErrorKind.CALL_ERROR,
        ErrorKind.TIMEOUT,
        ErrorKind.BAD_FORMAT,
    ]
    for item in results:
        assert item.status == "error"
        assert is_error(item)
        assert not isinstance(item, (AssignmentPlan, LineChoice))
        payload = item.to_dict()
        assert payload.keys() == {"status", "kind", "reason"}
        assert "model" not in payload
        assert "choices" not in payload
        assert ErrorResult.from_dict(json.loads(json.dumps(payload))) == item
    plan = AssignmentPlan(choices=(_choice(),))
    assert is_error(plan) is False
    with pytest.raises(ValueError, match="status"):
        ErrorResult.from_dict(plan.to_dict())
    with pytest.raises(ValueError, match="kind"):
        ErrorResult(kind="infeasible", reason="no")  # type: ignore[arg-type]
    with pytest.raises(FrozenInstanceError):
        results[0].reason = "changed"  # type: ignore[misc]


def test_package_init_imports_the_types_and_not_sibling_modules() -> None:
    imported = [
        name
        for name in _imported_modules(_PACKAGE / "__init__.py")
        if name != "__future__"
    ]
    assert imported == ["src.tools.auto_dispatch.superagent_assign.types"]
    for name in _SIBLINGS:
        assert not any(
            name == module or module.endswith("." + name) for module in imported
        )
    type_imports = [
        name
        for name in _imported_modules(_PACKAGE / "types.py")
        if name != "__future__"
    ]
    assert type_imports
    assert all(
        module.split(".")[0] in {"dataclasses", "enum", "typing", "pydantic"}
        for module in type_imports
    )
    import src.tools.auto_dispatch.superagent_assign as package

    # Combined L1 collection imports sibling modules into this process and
    # attaches them on the package. Whether this package's own import loads
    # them is checked in a fresh interpreter.
    probe = "\n".join(
        (
            "import json, sys",
            "import src.tools.auto_dispatch.superagent_assign as package",
            f"siblings = {list(_SIBLINGS)!r}",
            "loaded = [",
            "    name for name in siblings",
            "    if f'src.tools.auto_dispatch.superagent_assign.{name}' in sys.modules",
            "    or hasattr(package, name)",
            "]",
            "print(json.dumps(loaded))",
        )
    )
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = os.pathsep.join([str(_REPO), env.get("PYTHONPATH", "")]).rstrip(
        os.pathsep
    )
    completed = subprocess.run(
        [sys.executable, "-B", "-c", probe],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    # Importing the package can log before the probe's JSON line.
    report = completed.stdout.strip().splitlines()[-1]
    assert json.loads(report) == []
    assert package.LineChoice is LineChoice
    assert package.AssignmentPlan is AssignmentPlan
    assert package.FeasibilityResult is FeasibilityResult
    assert package.ErrorResult is ErrorResult


def _plan_payload(effort: str) -> dict[str, Any]:
    return {
        "choices": [
            {
                "line_id": "l0",
                "model": "stepfun",
                "effort": effort,
                "runtime": "native_cli",
                "reason": "test",
                "confidence": "high",
            }
        ]
    }


def _tool_message(effort: str, *, content: str | None = None) -> Any:
    from langchain_core.messages import AIMessage

    payload = _plan_payload(effort)
    return AIMessage(
        content="" if content is None else content,
        tool_calls=[{"name": "AssignmentPlan", "args": payload, "id": "1"}],
    )


class _NativeFake:
    """GenericFakeChatModel whose bind_tools keeps the canned tool call."""

    def __new__(cls, messages: Any) -> Any:
        from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

        class Fake(GenericFakeChatModel):
            def bind_tools(self, *_args: Any, **_kwargs: Any) -> Fake:
                return self

        return Fake(messages=messages)


@pytest.mark.asyncio
async def test_native_structured_output_returns_a_validated_frozen_plan() -> None:
    from src.llms.api_call import make_api_call

    llm = _NativeFake(iter([_tool_message("high")]))
    with patch("langsmith.tracing_context") as ctx:
        ctx.return_value.__enter__ = lambda self: None
        ctx.return_value.__exit__ = lambda self, *a: None
        result = await make_api_call(
            llm,
            "sys",
            "user",
            response_schema=AssignmentPlan,
            disable_tracing=True,
        )

    assert isinstance(result, AssignmentPlan)
    assert result.choices[0].effort is Effort.HIGH
    assert result.choices[0].model == "stepfun"
    with pytest.raises(ValidationError, match="frozen"):
        result.choices[0].effort = Effort.LOW  # type: ignore[misc]


@pytest.mark.asyncio
async def test_native_structured_output_rejects_an_effort_outside_the_scale() -> None:
    from src.llms.api_call import make_api_call

    # Native parsing rejects the tool args (parsed is None). The same JSON is
    # in the message so the manual fallback also validates effort instead of
    # reporting an empty body. Either way the call must not return the dict.
    body = json.dumps(_plan_payload("ultra"))
    llm = _NativeFake(
        iter(
            [_tool_message("ultra", content=body), _tool_message("ultra", content=body)]
        )
    )

    async def _no_sleep(*_args: Any, **_kwargs: Any) -> None:
        return None

    with (
        patch("langsmith.tracing_context") as ctx,
        patch("src.llms.api_call.asyncio.sleep", _no_sleep),
    ):
        ctx.return_value.__enter__ = lambda self: None
        ctx.return_value.__exit__ = lambda self, *a: None
        with pytest.raises(ValueError, match="effort"):
            await make_api_call(
                llm,
                "sys",
                "user",
                response_schema=AssignmentPlan,
                disable_tracing=True,
                max_parsing_retries=1,
            )
