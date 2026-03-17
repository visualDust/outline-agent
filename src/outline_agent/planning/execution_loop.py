from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..tools import ToolContext, ToolRegistry, ToolResult
from ..utils.attachment_context import repair_download_attachment_args
from .tool_plan_schema import ToolPlanValidationError, UnifiedToolPlan, sanitize_unified_tool_plan


@dataclass(frozen=True)
class ExecutedPlanStep:
    index: int
    tool: str
    args: dict[str, Any]
    result: ToolResult


@dataclass(frozen=True)
class PlanExecutionSummary:
    status: str
    goal: str | None
    final_response_strategy: str | None
    steps: list[ExecutedPlanStep]
    error: str | None = None

    @property
    def preview(self) -> str | None:
        if not self.steps:
            return None
        return " ; ".join(step.result.summary for step in self.steps if step.result.summary)


class UnifiedExecutionLoop:
    def __init__(self, registry: ToolRegistry, *, max_steps: int) -> None:
        self.registry = registry
        self.max_steps = max_steps

    async def execute(
        self,
        plan: UnifiedToolPlan,
        context: ToolContext,
        *,
        on_step: Callable[
            [str, UnifiedToolPlan, int, str, dict[str, Any], bool, ToolResult | None], Awaitable[None] | None
        ]
        | None = None,
    ) -> PlanExecutionSummary:
        sanitized = sanitize_unified_tool_plan(
            plan,
            allowed_tools={spec.name for spec in self.registry.list_specs()},
            max_steps=self.max_steps,
        )
        if not sanitized.should_act:
            return PlanExecutionSummary(
                status="skipped",
                goal=sanitized.goal,
                final_response_strategy=sanitized.final_response_strategy,
                steps=[],
            )

        executed: list[ExecutedPlanStep] = []
        step_context: list[dict[str, Any]] = []
        for index, step in enumerate(sanitized.steps, start=1):
            try:
                normalized_args = _sanitize_document_action_args(step.tool, step.args)
                normalized_args = repair_download_attachment_args(step.tool, normalized_args, context.extra)
                normalized_args = _hydrate_drafting_context_args(
                    step.tool,
                    normalized_args,
                    step_context,
                    context.extra,
                )
                hydrated_args = _hydrate_document_action_args(
                    step.tool,
                    normalized_args,
                    step_context,
                    context.extra,
                )
                resolved_args = _resolve_templates(hydrated_args, step_context)
                resolved_args = _sanitize_document_action_args(step.tool, resolved_args)
                resolved_args = _hydrate_document_action_args(
                    step.tool,
                    resolved_args,
                    step_context,
                    context.extra,
                )
                document_action_error = _validate_document_action_args(
                    step.tool,
                    resolved_args,
                    step_context,
                    context.extra,
                )
            except ToolPlanValidationError as exc:
                return PlanExecutionSummary(
                    status="failed",
                    goal=sanitized.goal,
                    final_response_strategy=sanitized.final_response_strategy,
                    steps=executed,
                    error=str(exc),
                )
            if document_action_error is not None:
                return PlanExecutionSummary(
                    status="failed",
                    goal=sanitized.goal,
                    final_response_strategy=sanitized.final_response_strategy,
                    steps=executed,
                    error=document_action_error,
                )
            spec = self.registry.get(step.tool).spec
            if on_step is not None:
                callback_result = on_step(
                    "before_step",
                    sanitized,
                    index,
                    step.tool,
                    resolved_args,
                    spec.requires_confirmation,
                    None,
                )
                if callback_result is not None:
                    await callback_result
            result = await self.registry.execute(step.tool, resolved_args, context)
            if on_step is not None:
                callback_result = on_step(
                    "after_step",
                    sanitized,
                    index,
                    step.tool,
                    resolved_args,
                    spec.requires_confirmation,
                    result,
                )
                if callback_result is not None:
                    await callback_result
            executed_step = ExecutedPlanStep(index=index, tool=step.tool, args=resolved_args, result=result)
            executed.append(executed_step)
            step_context.append(
                {
                    "tool": step.tool,
                    "args": resolved_args,
                    "ok": result.ok,
                    "summary": result.summary,
                    "data": result.data,
                    "artifacts": result.artifacts,
                    "preview": result.preview,
                    "error": result.error,
                }
            )
            if not result.ok:
                return PlanExecutionSummary(
                    status="failed",
                    goal=sanitized.goal,
                    final_response_strategy=sanitized.final_response_strategy,
                    steps=executed,
                    error=result.error or result.summary,
                )

        return PlanExecutionSummary(
            status="applied",
            goal=sanitized.goal,
            final_response_strategy=sanitized.final_response_strategy,
            steps=executed,
        )


def _resolve_templates(value: Any, step_context: list[dict[str, Any]]) -> Any:
    if isinstance(value, str):
        return _resolve_string_template(value, step_context)
    if isinstance(value, list):
        return [_resolve_templates(item, step_context) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_templates(item, step_context) for key, item in value.items()}
    return value


def _resolve_string_template(value: str, step_context: list[dict[str, Any]]) -> Any:
    text = value.strip()
    if not (text.startswith("{{") and text.endswith("}}")):
        return value
    expression = text[2:-2].strip()
    if not expression.startswith("steps."):
        raise ToolPlanValidationError(f"unsupported template expression: {expression}")
    parts = expression.split(".")
    if len(parts) < 3:
        raise ToolPlanValidationError(f"invalid template expression: {expression}")
    try:
        step_index = int(parts[1])
    except ValueError as exc:
        raise ToolPlanValidationError(f"invalid step reference: {expression}") from exc
    if step_index < 1 or step_index > len(step_context):
        raise ToolPlanValidationError(f"template references unavailable step: {expression}")
    current: Any = step_context[step_index - 1]
    for part in parts[2:]:
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        raise ToolPlanValidationError(f"template path not found: {expression}")
    return current


def _hydrate_document_action_args(
    tool_name: str,
    args: dict[str, Any],
    step_context: list[dict[str, Any]],
    context_extra: dict[str, Any],
) -> dict[str, Any]:
    draft_tool_name: str | None = None
    if tool_name == "create_document":
        draft_tool_name = "draft_new_document"
    elif tool_name == "apply_document_update":
        draft_tool_name = "draft_document_update"
    else:
        return args

    draft_data = _latest_document_draft_data(step_context, context_extra, draft_tool_name)
    if not draft_data:
        return args

    hydrated = dict(args)
    fallback_text = draft_data.get("text") or draft_data.get("content")

    if _should_fill_document_field_from_draft(hydrated.get("title")):
        hydrated["title"] = draft_data.get("title")
    if _should_fill_document_field_from_draft(hydrated.get("text")) and fallback_text is not None:
        hydrated["text"] = fallback_text
    if _should_fill_document_field_from_draft(hydrated.get("content")):
        if hydrated.get("text") is not None:
            hydrated["content"] = hydrated.get("text")
        elif fallback_text is not None:
            hydrated["content"] = fallback_text
    return hydrated


def _sanitize_document_action_args(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    allowed_fields: set[str] | None = None
    if tool_name == "create_document":
        allowed_fields = {"title", "text", "content", "collection_id", "parent_document_id", "publish"}
    elif tool_name == "apply_document_update":
        allowed_fields = {"title", "text", "content"}

    if allowed_fields is None:
        return dict(args)
    return {key: value for key, value in args.items() if key in allowed_fields}


def _hydrate_drafting_context_args(
    tool_name: str,
    args: dict[str, Any],
    step_context: list[dict[str, Any]],
    context_extra: dict[str, Any],
) -> dict[str, Any]:
    if tool_name not in {"draft_document_update", "draft_new_document"}:
        return dict(args)
    if _has_nonempty_text(args.get("local_workspace_context")):
        return dict(args)

    snippets = _collect_local_workspace_context_snippets(step_context, context_extra)
    if not snippets:
        return dict(args)

    hydrated = dict(args)
    hydrated["local_workspace_context"] = "\n\n".join(snippets)
    return hydrated


def _validate_document_action_args(
    tool_name: str,
    args: dict[str, Any],
    step_context: list[dict[str, Any]],
    context_extra: dict[str, Any],
) -> str | None:
    if tool_name == "apply_document_update":
        draft_tool_name = "draft_document_update"
        blocked_decisions = {"blocked", "no-edit"}
        if not any(_has_nonempty_text(args.get(field)) for field in ("title", "text", "content")):
            return _document_action_error_message(
                tool_name=tool_name,
                draft_tool_name=draft_tool_name,
                step_context=step_context,
                context_extra=context_extra,
                blocked_decisions=blocked_decisions,
                missing_fields="title or text",
            )
        return None

    if tool_name == "create_document":
        draft_tool_name = "draft_new_document"
        blocked_decisions = {"blocked", "no-create"}
        if not _has_nonempty_text(args.get("title")) or not any(
            _has_nonempty_text(args.get(field)) for field in ("text", "content")
        ):
            return _document_action_error_message(
                tool_name=tool_name,
                draft_tool_name=draft_tool_name,
                step_context=step_context,
                context_extra=context_extra,
                blocked_decisions=blocked_decisions,
                missing_fields="title and text",
            )
        return None

    return None


def _document_action_error_message(
    *,
    tool_name: str,
    draft_tool_name: str,
    step_context: list[dict[str, Any]],
    context_extra: dict[str, Any],
    blocked_decisions: set[str],
    missing_fields: str,
) -> str:
    draft_data = _latest_document_draft_data(step_context, context_extra, draft_tool_name)
    if not draft_data:
        return f"{tool_name} requires {missing_fields}"

    decision = draft_data.get("decision")
    reason = draft_data.get("reason")
    if isinstance(decision, str) and decision in blocked_decisions:
        reason_suffix = f": {reason}" if isinstance(reason, str) and reason.strip() else ""
        return f"{tool_name} blocked by {draft_tool_name} decision={decision}{reason_suffix}"

    decision_suffix = f" decision={decision}" if isinstance(decision, str) and decision.strip() else ""
    reason_suffix = f"; reason={reason}" if isinstance(reason, str) and reason.strip() else ""
    return (
        f"{tool_name} requires {missing_fields}; latest {draft_tool_name} did not provide them"
        f"{decision_suffix}{reason_suffix}"
    )


def _collect_local_workspace_context_snippets(
    step_context: list[dict[str, Any]],
    context_extra: dict[str, Any],
) -> list[str]:
    snippets: list[str] = []

    prior_round_observations = context_extra.get("prior_round_observations")
    if isinstance(prior_round_observations, list):
        recent = [item for item in prior_round_observations[-2:] if isinstance(item, str) and item.strip()]
        if recent:
            snippets.append("Prior round observations:\n" + "\n\n".join(recent))

    current_round_snippets: list[str] = []
    for step in step_context:
        if step.get("ok") is not True:
            continue
        tool_name = step.get("tool")
        data = step.get("data")
        if not isinstance(tool_name, str) or not isinstance(data, dict):
            continue
        snippet = _format_local_workspace_step_snippet(tool_name, data)
        if snippet:
            current_round_snippets.append(snippet)
    if current_round_snippets:
        snippets.append("Current round local file observations:\n" + "\n\n".join(current_round_snippets[-4:]))

    return snippets


def _format_local_workspace_step_snippet(tool_name: str, data: dict[str, Any]) -> str | None:
    path = data.get("path") if isinstance(data.get("path"), str) else None
    target = data.get("target") if isinstance(data.get("target"), str) else None

    if tool_name.startswith("extract_text_from_"):
        text = data.get("text")
        if isinstance(text, str) and text.strip():
            source = path or target or "workspace file"
            return f"{tool_name}[{source}] extracted text:\n{_truncate_text(text, 4000)}"
        return None

    if tool_name == "read_file":
        stdout = data.get("stdout")
        if isinstance(stdout, str) and stdout.strip():
            source = path or target or "workspace file"
            return f"read_file[{source}] contents:\n{_truncate_text(stdout, 4000)}"
        return None

    if tool_name == "run_shell":
        stdout = data.get("stdout")
        stderr = data.get("stderr")
        exit_code = data.get("exit_code")
        pieces: list[str] = []
        if isinstance(stdout, str) and stdout.strip():
            pieces.append(f"stdout:\n{_truncate_text(stdout, 2000)}")
        if isinstance(stderr, str) and stderr.strip():
            pieces.append(f"stderr:\n{_truncate_text(stderr, 1200)}")
        if not pieces:
            return None
        exit_code_text = f" exit={exit_code}" if isinstance(exit_code, int) else ""
        source = target or "shell command"
        return f"run_shell[{source}]{exit_code_text}:\n" + "\n\n".join(pieces)

    if tool_name == "download_attachment":
        source = path or target or "download"
        content_type = data.get("content_type")
        size = data.get("size")
        details: list[str] = [f"downloaded attachment -> {source}"]
        if isinstance(content_type, str) and content_type.strip():
            details.append(f"content_type={content_type}")
        if isinstance(size, int):
            details.append(f"size={size}")
        return " ; ".join(details)

    return None


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"




def _latest_document_draft_data(
    step_context: list[dict[str, Any]],
    context_extra: dict[str, Any],
    draft_tool_name: str,
) -> dict[str, Any] | None:
    draft_data = _latest_successful_step_data(step_context, draft_tool_name)
    if draft_data:
        return draft_data

    if draft_tool_name == "draft_document_update":
        prior = context_extra.get("prior_draft_update_data")
    elif draft_tool_name == "draft_new_document":
        prior = context_extra.get("prior_draft_creation_data")
    else:
        prior = None
    return prior if isinstance(prior, dict) and prior else None

def _latest_successful_step_data(
    step_context: list[dict[str, Any]],
    tool_name: str,
) -> dict[str, Any] | None:
    for step in reversed(step_context):
        if step.get("tool") != tool_name or step.get("ok") is not True:
            continue
        data = step.get("data")
        if isinstance(data, dict):
            return data
    return None


def _has_nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _should_fill_document_field_from_draft(value: Any) -> bool:
    return not _has_nonempty_text(value) or _is_template_expression(value)


def _is_template_expression(value: Any) -> bool:
    return isinstance(value, str) and value.strip().startswith("{{") and value.strip().endswith("}}")
