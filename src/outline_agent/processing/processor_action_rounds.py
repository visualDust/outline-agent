from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..clients.outline_client import OutlineClient
from ..core.config import AppSettings
from ..planning import UnifiedExecutionLoop, UnifiedToolPlan, UnifiedToolPlanner
from ..tools import ToolContext
from .action_plan_results import (
    build_document_creation_success,
    build_document_update_success,
    document_failure_context,
    document_failure_preview,
    document_failure_status,
    format_round_observation_for_planner,
    plan_execution_summary_to_tool_report,
    uploaded_attachments_from_artifacts,
)
from .action_plan_structure import (
    action_plan_fingerprint,
    action_plan_is_read_only,
    action_plan_may_change_state,
    action_plan_steps_preview,
    describe_action_plan_for_progress,
    proposal_local_steps,
    select_next_action_plan_chunk,
)
from .processor_action_session import ActionLoopSession
from .processor_types import ExecutedToolRound, ToolRoundSummary


@dataclass(slots=True)
class ProposedActionRound:
    proposal: UnifiedToolPlan
    preview: str | None
    local_steps: list[Any]
    plan_preview: str
    plan_fingerprint: tuple[tuple[object, ...], ...]
    full_plan_preview: str
    was_chunked: bool


@dataclass(slots=True)
class ExecutedActionRound:
    report: Any
    report_preview: str
    context: str
    summary: Any


async def propose_action_round(
    *,
    action_planner: UnifiedToolPlanner,
    available_tools: list[Any],
    workspace: Any,
    document_workspace: Any,
    thread_workspace: Any,
    collection: Any,
    document: Any,
    user_comment: str,
    comment_context: str | None,
    related_documents_context: str | None,
    round_index: int,
    prior_round_summaries: list[str],
    prior_round_observations: list[str],
    available_attachment_context: list[Any],
    current_comment_image_count: int,
    input_images: list[Any],
) -> ProposedActionRound:
    full_proposal = await action_planner.propose_plan(
        available_tools=available_tools,
        workspace=workspace,
        document_workspace=document_workspace,
        thread_workspace=thread_workspace,
        collection=collection,
        document=document,
        user_comment=user_comment,
        comment_context=comment_context,
        related_documents_context=related_documents_context,
        current_round=round_index,
        prior_round_summaries=prior_round_summaries,
        prior_round_observations=prior_round_observations,
        available_attachment_context=available_attachment_context,
        current_comment_image_count=current_comment_image_count,
        input_images=input_images,
    )
    proposal = select_next_action_plan_chunk(
        full_proposal,
        max_chunk_steps=action_planner.settings.tool_execution_chunk_size,
    )
    preview = action_planner.preview(proposal)
    full_plan_preview = action_plan_steps_preview(full_proposal)
    return ProposedActionRound(
        proposal=proposal,
        preview=preview,
        local_steps=proposal_local_steps(proposal),
        plan_preview=action_plan_steps_preview(proposal),
        plan_fingerprint=action_plan_fingerprint(proposal),
        full_plan_preview=full_plan_preview,
        was_chunked=full_plan_preview != action_plan_steps_preview(proposal),
    )


async def apply_dry_run_round(
    *,
    session: ActionLoopSession,
    action_planner: UnifiedToolPlanner,
    proposal: UnifiedToolPlan,
    preview: str | None,
    local_steps: list[Any],
    round_index: int,
) -> Any:
    session.round_summaries.append(
        ToolRoundSummary(
            round_index=round_index,
            status="planned-dry-run",
            preview=preview,
            context=action_planner.format_reply_context(
                work_dir=str(session.workspace.workspace_dir),
                proposal=proposal,
                status="planned-dry-run",
                step_summaries=None,
            ),
        )
    )
    if any(step.tool == "create_document" for step in proposal.steps):
        session.document_creation_status = "planned-dry-run"
        session.document_creation_preview = preview
        session.document_creation_context = "- status: planned-dry-run"
    if any(step.tool == "apply_document_update" for step in proposal.steps):
        session.document_update_status = "planned-dry-run"
        session.document_update_preview = preview
        session.document_update_context = "- status: planned-dry-run"
    if local_steps:
        session.tool_execution_status = "planned-dry-run"
        session.refresh_tool_execution_summary()
    return session.outcome()


async def execute_action_round(
    *,
    settings: AppSettings,
    outline_client: OutlineClient,
    unified_execution_loop: UnifiedExecutionLoop,
    session: ActionLoopSession,
    collection: Any,
    user_comment: str,
    comment_context: str | None,
    related_documents_context: str | None,
    available_attachment_context: list[Any],
    current_comment_image_count: int,
    input_images: list[Any],
    action_planner: UnifiedToolPlanner,
    round_index: int,
    proposal: UnifiedToolPlan,
    preview: str | None,
) -> ExecutedActionRound:
    async def on_progress(
        stage: str,
        _proposal: UnifiedToolPlan,
        _index: int,
        tool_name: str,
        resolved_args: dict[str, object],
        requires_confirmation: bool,
        result: Any,
    ) -> None:
        await session.note_step_progress(
            round_index=round_index,
            stage=stage,
            tool_name=tool_name,
            resolved_args=resolved_args,
            requires_confirmation=requires_confirmation,
            result=result,
        )

    summary = await unified_execution_loop.execute(
        proposal,
        ToolContext(
            settings=settings,
            outline_client=outline_client,
            work_dir=session.workspace.workspace_dir,
            document=session.effective_document,
            collection=collection,
            extra={
                "thread_workspace": session.thread_workspace,
                "document_workspace": getattr(session, "document_workspace", None),
                "current_comment_id": session.comment_id,
                "user_comment": user_comment,
                "comment_context": comment_context,
                "related_documents_context": related_documents_context,
                "prior_round_observations": list(session.round_observations),
                "prior_draft_update_data": dict(session.draft_update_data or {}),
                "prior_draft_creation_data": dict(session.draft_creation_data or {}),
                "available_attachment_context": list(available_attachment_context),
                "current_comment_image_count": current_comment_image_count,
                "input_images": input_images,
            },
        ),
        on_step=on_progress,
    )
    report = plan_execution_summary_to_tool_report(summary)
    report_preview = report.preview or preview or report.status
    context = action_planner.format_reply_context(
        work_dir=str(session.workspace.workspace_dir),
        proposal=proposal,
        status=report.status,
        step_summaries=[result.summary for result in report.step_results],
    )
    round_observation = format_round_observation_for_planner(
        round_index=round_index,
        plan_preview=action_plan_steps_preview(proposal),
        summary=summary,
        work_dir=session.workspace.workspace_dir,
        max_work_dir_entries=settings.tool_list_dir_max_entries,
    )
    session.round_history.append(f"round {round_index} (status={report.status}): {report_preview}")
    session.round_observations.append(round_observation)
    session.executed_rounds.append(
        ExecutedToolRound(
            round_index=round_index,
            plan_fingerprint=action_plan_fingerprint(proposal),
            status=report.status,
            may_change_state=action_plan_may_change_state(proposal),
            read_only=action_plan_is_read_only(proposal),
        )
    )
    return ExecutedActionRound(
        report=report,
        report_preview=report_preview,
        context=context,
        summary=summary,
    )


def record_local_round_summary(
    *,
    session: ActionLoopSession,
    round_index: int,
    local_steps: list[Any],
    report: Any,
    report_preview: str,
    context: str,
) -> None:
    if not local_steps:
        return
    session.round_summaries.append(
        ToolRoundSummary(
            round_index=round_index,
            status=report.status,
            preview=report_preview,
            context=context,
        )
    )
    session.record_tool_run(
        status=report.status,
        summary=report_preview,
        step_summaries=[result.summary for result in report.step_results],
    )
    session.tool_execution_status = report.status
    session.refresh_tool_execution_summary()


def apply_step_effects(
    *,
    session: ActionLoopSession,
    summary: Any,
    report: Any,
) -> Any:
    failed_step = next((step for step in reversed(summary.steps) if not step.result.ok), None)

    for step in summary.steps:
        tool_name = step.tool
        result = step.result
        if tool_name == "draft_new_document" and result.ok:
            session.draft_creation_data = result.data if isinstance(result.data, dict) else {}
        elif tool_name == "draft_document_update" and result.ok:
            session.draft_update_data = result.data if isinstance(result.data, dict) else {}
        elif tool_name == "create_document" and result.ok:
            (
                session.created_document,
                session.document_creation_status,
                session.document_creation_preview,
                session.document_creation_context,
            ) = build_document_creation_success(
                result_data=result.data,
                fallback_collection_id=session.effective_document.collection_id,
                draft_data=session.draft_creation_data,
                report_status=report.status,
                result_summary=result.summary,
            )
        elif tool_name == "apply_document_update" and result.ok:
            (
                session.effective_document,
                session.document_update_status,
                session.document_update_preview,
                session.document_update_context,
            ) = build_document_update_success(
                current_document=session.effective_document,
                result_data=result.data,
                draft_data=session.draft_update_data,
                report_status=report.status,
                result_summary=result.summary,
            )
        if result.artifacts:
            session.uploaded_attachments.extend(uploaded_attachments_from_artifacts(result.artifacts))

    return failed_step


def apply_failure_effects(
    *,
    session: ActionLoopSession,
    proposal: UnifiedToolPlan,
    report: Any,
    failed_step: Any,
) -> None:
    session.document_creation_status = document_failure_status(
        proposal=proposal,
        tool_name="create_document",
        current_status=session.document_creation_status,
        report_status=report.status,
    )
    session.document_creation_preview = document_failure_preview(
        proposal=proposal,
        tool_name="create_document",
        current_preview=session.document_creation_preview,
        report=report,
        failed_step=failed_step,
    )
    session.document_creation_context = document_failure_context(
        proposal=proposal,
        tool_name="create_document",
        current_context=session.document_creation_context,
        status=session.document_creation_status,
        draft_data=session.draft_creation_data,
        report=report,
        failed_step=failed_step,
    )
    session.document_update_status = document_failure_status(
        proposal=proposal,
        tool_name="apply_document_update",
        current_status=session.document_update_status,
        report_status=report.status,
    )
    session.document_update_preview = document_failure_preview(
        proposal=proposal,
        tool_name="apply_document_update",
        current_preview=session.document_update_preview,
        report=report,
        failed_step=failed_step,
    )
    session.document_update_context = document_failure_context(
        proposal=proposal,
        tool_name="apply_document_update",
        current_context=session.document_update_context,
        status=session.document_update_status,
        draft_data=session.draft_update_data,
        report=report,
        failed_step=failed_step,
    )


def proposal_document_steps(proposal: UnifiedToolPlan) -> list[str]:
    return [
        step.tool
        for step in proposal.steps
        if step.tool
        in {
            "draft_new_document",
            "create_document",
            "draft_document_update",
            "apply_document_update",
        }
    ]


def progress_action_for_round(round_index: int, proposal: UnifiedToolPlan) -> str:
    return describe_action_plan_for_progress(round_index, proposal)
