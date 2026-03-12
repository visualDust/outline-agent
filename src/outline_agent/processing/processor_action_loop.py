from __future__ import annotations

from ..clients.outline_client import OutlineClient
from ..core.config import AppSettings
from ..core.logging import logger
from ..planning import UnifiedExecutionLoop, UnifiedToolPlanner
from ..tools import ToolSpec
from .action_plan_structure import (
    available_action_tool_specs as _available_action_tool_specs,
)
from .action_plan_structure import (
    find_redundant_upload_paths_for_unified_plan as _find_redundant_upload_paths_for_unified_plan,
)
from .action_plan_structure import (
    find_repeated_plan_without_intervening_state_change as _find_repeated_plan_without_intervening_state_change,
)
from .action_plan_structure import (
    preview_action_plan_step as _preview_action_plan_step,
)
from .processor_action_rounds import (
    apply_dry_run_round as _apply_dry_run_round,
)
from .processor_action_rounds import (
    apply_failure_effects as _apply_failure_effects,
)
from .processor_action_rounds import (
    apply_step_effects as _apply_step_effects,
)
from .processor_action_rounds import (
    execute_action_round as _execute_action_round,
)
from .processor_action_rounds import (
    progress_action_for_round as _progress_action_for_round,
)
from .processor_action_rounds import (
    proposal_document_steps as _proposal_document_steps,
)
from .processor_action_rounds import (
    propose_action_round as _propose_action_round,
)
from .processor_action_rounds import (
    record_local_round_summary as _record_local_round_summary,
)
from .processor_action_session import ActionLoopSession
from .processor_prompting import preview as _preview
from .processor_types import (
    ActionPlanOutcome,
    ActionPlanRequest,
)


async def execute_action_plan(
    *,
    settings: AppSettings,
    outline_client: OutlineClient,
    action_planner: UnifiedToolPlanner,
    unified_execution_loop: UnifiedExecutionLoop,
    tool_specs: list[ToolSpec],
    request: ActionPlanRequest,
) -> ActionPlanOutcome:
    comment_id = request.comment_id
    thread_workspace = request.thread_workspace
    collection = request.collection
    document = request.document
    user_comment = request.user_comment
    comment_context = request.comment_context
    related_documents_context = request.related_documents_context
    available_attachment_context = request.available_attachment_context
    current_comment_image_count = request.current_comment_image_count
    input_images = request.input_images
    available_tools = _available_action_tool_specs(
        tool_specs,
        document_update_enabled=settings.document_update_enabled,
        tool_use_enabled=settings.tool_use_enabled,
    )
    if not available_tools:
        logger.debug(
            "Unified action planner skipped for comment {} because no action tools are available.",
            comment_id,
        )
        return ActionPlanOutcome(effective_document=document)
    session = ActionLoopSession(
        settings=settings,
        outline_client=outline_client,
        thread_workspace=thread_workspace,
        comment_id=comment_id,
        document_id=document.id,
        effective_document=document,
    )

    for round_index in range(1, settings.tool_execution_max_rounds + 1):
        logger.debug(
            "Unified action planner input: comment_id={}, document_id={}, round={}/{}, "
            "tools=[{}], prior_rounds={}, images={}, planner_step_budget={}, execution_chunk_size={}, prompt={}",
            comment_id,
            session.effective_document.id,
            round_index,
            settings.tool_execution_max_rounds,
            ", ".join(spec.name for spec in available_tools),
            len(session.round_history),
            current_comment_image_count,
            settings.tool_execution_max_steps,
            settings.tool_execution_chunk_size,
            _preview(user_comment, 160),
        )
        try:
            proposed_round = await _propose_action_round(
                action_planner=action_planner,
                available_tools=available_tools,
                thread_workspace=thread_workspace,
                collection=collection,
                document=session.effective_document,
                user_comment=user_comment,
                comment_context=comment_context,
                related_documents_context=related_documents_context,
                round_index=round_index,
                prior_round_summaries=session.round_history,
                prior_round_observations=session.round_observations,
                available_attachment_context=available_attachment_context,
                current_comment_image_count=current_comment_image_count,
                input_images=input_images,
            )
        except Exception as exc:  # noqa: BLE001
            error_detail = str(exc).strip() or repr(exc)
            logger.warning(
                "Unified action planning failed: comment_id={}, document_id={}, round={}/{}, error_type={}, detail={}",
                comment_id,
                session.effective_document.id,
                round_index,
                settings.tool_execution_max_rounds,
                type(exc).__name__,
                error_detail,
            )
            if not session.round_summaries:
                return session.outcome()
            return await session.stop_with_blocked_round(
                round_index=round_index,
                preview=f"blocked: action planning failed: {error_detail}",
                context=(
                    "- status: blocked\n"
                    "- reason: Could not prepare a safe action plan.\n"
                    f"- planner_error: {error_detail}"
                ),
                progress_action=(f"Stopped: I couldn't safely prepare the next action plan in round {round_index}."),
                record_tool_run=False,
            )

        proposal = proposed_round.proposal
        preview = proposed_round.preview
        local_steps = proposed_round.local_steps
        logger.debug(
            "Unified action planner output: comment_id={}, round={}, should_act={}, goal={}, "
            "strategy={}, steps={}, full_steps={}, chunked={}",
            comment_id,
            round_index,
            proposal.should_act,
            proposal.goal or "(none)",
            proposal.final_response_strategy or "(none)",
            proposed_round.plan_preview,
            proposed_round.full_plan_preview,
            proposed_round.was_chunked,
        )
        if not proposal.should_act:
            logger.debug(
                "Unified action planner returned no-op for comment {} in round {} after {} prior action rounds.",
                comment_id,
                round_index,
                len(session.round_summaries),
            )
            if not session.round_summaries:
                return session.outcome()
            return await session.finish_local_actions()

        redundant_upload_paths = _find_redundant_upload_paths_for_unified_plan(
            proposal=proposal,
            uploaded_attachments=session.uploaded_attachments,
            work_dir=thread_workspace.work_dir,
        )
        if redundant_upload_paths:
            logger.debug(
                "Unified action planner blocked redundant upload loop for comment {} in round {}: {}",
                comment_id,
                round_index,
                ", ".join(redundant_upload_paths),
            )
            blocked_preview = (
                "blocked: repeated attachment upload plan detected for already-uploaded file(s): "
                + ", ".join(redundant_upload_paths)
            )
            blocked_context = (
                "- status: blocked\n"
                "- reason: repeated attachment upload plan detected for already-uploaded file(s). "
                "The next action plan only repeated attachment upload steps that were already "
                "completed earlier in this turn, so execution stopped to avoid a loop.\n"
                f"- repeated uploads: {', '.join(redundant_upload_paths)}"
            )
            return await session.stop_with_blocked_round(
                round_index=round_index,
                preview=blocked_preview,
                context=blocked_context,
                progress_action=(
                    "Stopped: the next action plan only repeated attachment uploads that were already complete."
                ),
                record_tool_run=True,
            )

        prior_repeat_round = _find_repeated_plan_without_intervening_state_change(
            proposed_round.plan_fingerprint,
            session.executed_rounds,
        )
        if prior_repeat_round is not None:
            logger.debug(
                "Unified action planner blocked repeated round for comment {} in round {} "
                "(repeated_from_round={}, read_only={}): {}",
                comment_id,
                round_index,
                prior_repeat_round.round_index,
                prior_repeat_round.read_only,
                proposed_round.plan_preview,
            )
            repeated_preview = (
                "blocked: repeated tool plan detected with no intervening state change; "
                "execution stopped to avoid a loop "
                f"({'; '.join(_preview_action_plan_step(step.tool, step.args) for step in proposal.steps)})"
            )
            repeated_reason = (
                "The next inspection-only tool plan repeated an earlier successful inspection round"
                if prior_repeat_round.read_only
                else "The next tool plan exactly repeated an earlier successful round"
            )
            repeated_steps_preview = " ; ".join(
                _preview_action_plan_step(step.tool, step.args) for step in proposal.steps
            )
            repeated_context = (
                "- status: blocked\n"
                "- reason: repeated tool plan detected with no intervening state change. "
                f"{repeated_reason} without any intervening state-changing round, "
                "so execution stopped to avoid an infinite loop.\n"
                f"- repeated_from_round: {prior_repeat_round.round_index}\n"
                f"- repeated steps: {repeated_steps_preview}"
            )
            return await session.stop_with_blocked_round(
                round_index=round_index,
                preview=repeated_preview,
                context=repeated_context,
                progress_action=(
                    "Stopped: the next action plan repeated an earlier successful round without any new state change."
                ),
                record_tool_run=True,
            )

        if settings.dry_run:
            logger.debug(
                "Unified action planner dry-run for comment {} in round {}: {}",
                comment_id,
                round_index,
                preview or proposed_round.plan_preview,
            )
            return await _apply_dry_run_round(
                session=session,
                action_planner=action_planner,
                proposal=proposal,
                preview=preview,
                local_steps=local_steps,
                round_index=round_index,
            )

        if proposal.should_act:
            await session.start_round(
                round_index=round_index,
                action_description=_progress_action_for_round(round_index, proposal),
            )

        logger.debug(
            "Executing unified action plan for comment {} in round {}: local_steps={}, document_steps={}, steps={}",
            comment_id,
            round_index,
            len(local_steps),
            len(proposal.steps) - len(local_steps),
            proposed_round.plan_preview,
        )

        executed_round = await _execute_action_round(
            settings=settings,
            outline_client=outline_client,
            unified_execution_loop=unified_execution_loop,
            session=session,
            collection=collection,
            user_comment=user_comment,
            comment_context=comment_context,
            related_documents_context=related_documents_context,
            available_attachment_context=available_attachment_context,
            current_comment_image_count=current_comment_image_count,
            input_images=input_images,
            action_planner=action_planner,
            round_index=round_index,
            proposal=proposal,
            preview=preview,
        )
        report = executed_round.report
        report_preview = executed_round.report_preview
        context = executed_round.context
        summary = executed_round.summary
        logger.debug(
            "Unified action execution summary: comment_id={}, round={}, status={}, preview={}, "
            "error={}, executed_steps={}",
            comment_id,
            round_index,
            report.status,
            report_preview,
            report.error or "(none)",
            len(summary.steps),
        )

        _record_local_round_summary(
            session=session,
            round_index=round_index,
            local_steps=local_steps,
            report=report,
            report_preview=report_preview,
            context=context,
        )
        failed_step = _apply_step_effects(
            session=session,
            summary=summary,
            report=report,
        )

        if report.status != "applied":
            _apply_failure_effects(
                session=session,
                proposal=proposal,
                report=report,
                failed_step=failed_step,
            )
            logger.debug(
                "Unified action execution stopped for comment {} in round {} with status {} "
                "(failed_step={}, error={}).",
                comment_id,
                round_index,
                report.status,
                failed_step.tool if failed_step is not None else "(none)",
                report.error or "(none)",
            )
            if round_index < settings.tool_execution_max_rounds:
                await session.note_retry_after_failure(round_index=round_index, status=report.status)
                continue
            if proposal.should_act:
                return await session.stop_failed_round(round_index=round_index, status=report.status)
            return session.outcome()

        document_steps = _proposal_document_steps(proposal)
        if document_steps and not local_steps:
            terminal_document_steps = {"apply_document_update", "create_document"}
            if any(step in terminal_document_steps for step in document_steps):
                logger.debug(
                    "Unified action plan completed for comment {} with terminal document steps in round {}: {}",
                    comment_id,
                    round_index,
                    proposed_round.plan_preview,
                )
                return session.outcome()
            logger.debug(
                "Unified action round for comment {} finished draft-only document steps in round {}; "
                "replanning next round: {}",
                comment_id,
                round_index,
                proposed_round.plan_preview,
            )
            continue

    if session.round_history:
        return await session.stop_max_rounds()

    return session.outcome()
