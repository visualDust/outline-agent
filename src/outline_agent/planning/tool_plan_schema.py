from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class UnifiedToolPlanStep(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class UnifiedToolPlan(BaseModel):
    should_act: bool = False
    goal: str | None = None
    steps: list[UnifiedToolPlanStep] = Field(default_factory=list)
    final_response_strategy: str | None = None


class ToolPlanValidationError(ValueError):
    """Raised when a plan cannot be executed safely."""


def sanitize_unified_tool_plan(
    plan: UnifiedToolPlan,
    *,
    allowed_tools: set[str],
    max_steps: int,
) -> UnifiedToolPlan:
    cleaned_steps: list[UnifiedToolPlanStep] = []
    for step in plan.steps:
        tool = step.tool.strip()
        if not tool:
            continue
        if tool not in allowed_tools:
            raise ToolPlanValidationError(f"unknown tool: {tool}")
        cleaned_steps.append(UnifiedToolPlanStep(tool=tool, args=_normalize_json_like(step.args)))
        if len(cleaned_steps) >= max_steps:
            break

    should_act = plan.should_act and bool(cleaned_steps)
    goal = _normalize_optional_text(plan.goal)
    strategy = _normalize_optional_text(plan.final_response_strategy)
    return UnifiedToolPlan(
        should_act=should_act,
        goal=goal,
        steps=cleaned_steps,
        final_response_strategy=strategy,
    )


def _normalize_json_like(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _normalize_json_like(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_json_like(item) for item in value]
    return value


def _normalize_optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    compact = " ".join(value.split()).strip()
    return compact or None
