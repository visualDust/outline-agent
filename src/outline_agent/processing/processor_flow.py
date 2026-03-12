from __future__ import annotations

from ..core.logging import logger
from ..utils.attachment_context import (
    collect_attachment_context as _collect_attachment_context,
)
from .processor_context import (
    archive_context_comments as _archive_context_comments,
)
from .processor_context import (
    detect_reply_trigger as _detect_reply_trigger,
)
from .processor_context import (
    format_action_route_preview as _format_action_route_preview,
)
from .processor_context import (
    resolve_cross_thread_handoff as _resolve_cross_thread_handoff,
)
from .processor_outcomes import (
    append_status_context as _append_status_context,
)
from .processor_outcomes import (
    append_status_preview as _append_status_preview,
)
from .processor_prompting import (
    format_comment_context as _format_comment_context,
)
from .processor_prompting import (
    select_context_comments as _select_context_comments,
)
from .processor_prompting import (
    strip_trigger_tokens as _strip_trigger_tokens,
)
from .processor_reply import (
    empty_action_outcome as _empty_action_outcome,
)
from .processor_reply import (
    maybe_apply_memory_actions as _maybe_apply_memory_actions,
)
from .processor_services import ProcessorServices
from .processor_side_effects import (
    ensure_reply_placeholder_comment as _ensure_reply_placeholder_comment,
)
from .processor_side_effects import (
    mark_processing as _mark_processing,
)
from .processor_side_effects import (
    register_uploaded_attachments_in_document as _register_uploaded_attachments_in_document,
)
from .processor_types import (
    ActionPlanRequest,
    PreparedActionOutcome,
    PreparedRequest,
    PreparedThreadContext,
    ProcessingResult,
    ResolvedThreadTrigger,
)


async def resolve_thread_trigger(
    *,
    services: ProcessorServices,
    prepared: PreparedRequest,
) -> ResolvedThreadTrigger | ProcessingResult:
    comments = await services.outline_client.comments_list(
        prepared.comment.documentId,
        limit=services.settings.comment_list_limit,
    )
    triggered_alias = prepared.triggered_alias
    if not triggered_alias:
        triggered_alias = _detect_reply_trigger(
            settings=services.settings,
            current_comment=prepared.comment,
            comments=comments,
            agent_user_id=prepared.agent_user_id,
        )
        if services.settings.trigger_mode == "mention" and not triggered_alias:
            return ProcessingResult(
                action="ignored",
                reason="no-trigger-mention",
                comment_id=prepared.comment.id,
                document_id=prepared.comment.documentId,
                collection_id=prepared.document.collection_id,
                collection_workspace=str(prepared.workspace.root_dir),
                thread_workspace=str(prepared.thread_workspace.root_dir),
            )

    return ResolvedThreadTrigger(triggered_alias=triggered_alias, comments=comments)


async def prepare_thread_context(
    *,
    services: ProcessorServices,
    prepared: PreparedRequest,
    resolved_trigger: ResolvedThreadTrigger,
) -> tuple[bool, PreparedThreadContext]:
    processing_reaction_applied = await _mark_processing(
        settings=services.settings,
        outline_client=services.outline_client,
        comment_id=prepared.comment.id,
    )
    await _ensure_reply_placeholder_comment(
        settings=services.settings,
        outline_client=services.outline_client,
        thread_workspace=prepared.thread_workspace,
        request_comment_id=prepared.comment.id,
        document_id=prepared.comment.documentId,
    )

    context_comments = _select_context_comments(
        resolved_trigger.comments,
        prepared.comment,
        limit=services.settings.max_context_comments,
    )
    _archive_context_comments(
        settings=services.settings,
        thread_workspace=prepared.thread_workspace,
        context_comments=context_comments,
        document=prepared.document,
    )
    comment_context = _format_comment_context(context_comments, current_comment_id=prepared.comment.id)
    available_attachment_context = _collect_attachment_context(
        document=prepared.document,
        comments=context_comments,
        current_comment_id=prepared.comment.id,
    )

    cleaned_text = _strip_trigger_tokens(
        text=prepared.comment_text,
        aliases=services.settings.mention_aliases if services.settings.mention_alias_fallback_enabled else [],
        mentions=prepared.mentions,
    )
    prompt_text = cleaned_text.strip() or ("The user only pinged the agent. Ask a short clarifying follow-up question.")
    try:
        action_route = await services.action_router.decide(
            thread_workspace=prepared.thread_workspace,
            collection=prepared.collection,
            document=prepared.document,
            user_comment=prompt_text,
            comment_context=comment_context,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Action routing failed: {}", exc)
        action_route = None
    logger.debug(
        "Action route for comment {} in document {}: {}",
        prepared.comment.id,
        prepared.comment.documentId,
        _format_action_route_preview(action_route) or "(no route)",
    )

    handoff = (
        _resolve_cross_thread_handoff(
            settings=services.settings,
            workspace_manager=services.workspace_manager,
            workspace=prepared.workspace,
            thread_workspace=prepared.thread_workspace,
            document=prepared.document,
            prompt_text=prompt_text,
            context_comments=context_comments,
        )
        if action_route and action_route.cross_thread_handoff
        else None
    )
    same_document_comment_lookup = (
        await services.same_document_comment_manager.fetch_context(
            workspace=prepared.workspace,
            document=prepared.document,
            current_comment=prepared.comment,
            prompt_text=prompt_text,
        )
        if handoff is None and action_route and action_route.same_document_comment_lookup
        else None
    )
    related_documents = await services.related_document_manager.fetch_context(
        document=prepared.document,
        prompt_text=prompt_text,
    )
    return processing_reaction_applied, PreparedThreadContext(
        triggered_alias=prepared.triggered_alias,
        context_comments=context_comments,
        comment_context=comment_context,
        prompt_text=prompt_text,
        action_route=action_route,
        handoff=handoff,
        same_document_comment_context=(
            same_document_comment_lookup.prompt_section if same_document_comment_lookup else None
        ),
        same_document_comment_preview=(same_document_comment_lookup.preview if same_document_comment_lookup else None),
        related_documents_context=related_documents.prompt_section,
        available_attachment_context=available_attachment_context,
    )


async def prepare_action_outcome(
    *,
    services: ProcessorServices,
    prepared: PreparedRequest,
    thread_context: PreparedThreadContext,
) -> PreparedActionOutcome:
    if thread_context.handoff is not None:
        return _empty_action_outcome(prepared.document)

    (
        memory_action_status,
        memory_action_preview,
        memory_action_context,
    ) = await _maybe_apply_memory_actions(
        settings=services.settings,
        manager=services.memory_action_manager,
        workspace=prepared.workspace,
        collection=prepared.collection,
        document=prepared.document,
        user_comment=thread_context.prompt_text,
        should_attempt=bool(thread_context.action_route and thread_context.action_route.memory_action),
    )

    action_plan_outcome = await services.execute_action_plan(
        ActionPlanRequest(
            comment_id=prepared.comment.id,
            thread_workspace=prepared.thread_workspace,
            collection=prepared.collection,
            document=prepared.document,
            user_comment=thread_context.prompt_text,
            comment_context=thread_context.comment_context,
            related_documents_context=thread_context.related_documents_context,
            available_attachment_context=thread_context.available_attachment_context,
            current_comment_image_count=len(prepared.comment_image_inputs),
            input_images=prepared.comment_image_inputs,
        )
    )
    artifact_registration = await _register_uploaded_attachments_in_document(
        settings=services.settings,
        outline_client=services.outline_client,
        document=action_plan_outcome.effective_document,
        uploaded_attachments=action_plan_outcome.uploaded_attachments,
    )
    effective_document = artifact_registration.effective_document
    tool_execution_preview = _append_status_preview(
        action_plan_outcome.tool_execution_preview,
        artifact_registration.preview,
    )
    tool_execution_context = _append_status_context(
        action_plan_outcome.tool_execution_context,
        artifact_registration.context,
    )
    return PreparedActionOutcome(
        memory_action_status=memory_action_status,
        memory_action_preview=memory_action_preview,
        memory_action_context=memory_action_context,
        document_creation_status=action_plan_outcome.document_creation_status,
        document_creation_preview=action_plan_outcome.document_creation_preview,
        document_creation_context=action_plan_outcome.document_creation_context,
        created_document=action_plan_outcome.created_document,
        document_update_status=action_plan_outcome.document_update_status,
        document_update_preview=action_plan_outcome.document_update_preview,
        document_update_context=action_plan_outcome.document_update_context,
        tool_execution_status=action_plan_outcome.tool_execution_status,
        tool_execution_preview=tool_execution_preview,
        tool_execution_context=tool_execution_context,
        effective_document=effective_document,
        uploaded_attachments=action_plan_outcome.uploaded_attachments,
    )
