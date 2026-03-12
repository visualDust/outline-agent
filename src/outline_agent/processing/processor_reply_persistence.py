from __future__ import annotations

from ..core.config import AppSettings
from ..core.logging import logger
from ..managers.memory_manager import CollectionMemoryManager
from ..managers.document_memory_manager import DocumentMemoryManager
from .processor_context import (
    format_action_route_preview as _format_action_route_preview,
)
from .processor_outcomes import (
    resolve_dry_run_reason as _resolve_dry_run_reason,
)
from .processor_outcomes import (
    resolve_success_action as _resolve_success_action,
)
from .processor_outcomes import (
    resolve_success_reason as _resolve_success_reason,
)
from .processor_prompting import preview as _preview
from .processor_types import (
    PreparedActionOutcome,
    PreparedRequest,
    PreparedThreadContext,
    ProcessingResult,
    ReplyPersistenceOutcome,
)


async def maybe_update_memory(
    *,
    settings: AppSettings,
    manager: CollectionMemoryManager,
    prepared: PreparedRequest,
    thread_context: PreparedThreadContext,
    action_outcome: PreparedActionOutcome,
    reply: str,
) -> str | None:
    if not settings.memory_update_enabled:
        return None
    try:
        proposal = await manager.propose_update(
            workspace=prepared.workspace,
            collection=prepared.collection,
            document=action_outcome.effective_document,
            user_comment=thread_context.prompt_text,
            assistant_reply=reply,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Memory update proposal failed: {}", exc)
        return f"memory-update-error: {exc}"

    if settings.dry_run:
        return manager.preview(proposal)

    applied = manager.apply_update(prepared.workspace, proposal)
    if applied:
        return " ; ".join(applied)
    return manager.preview(proposal)


async def maybe_update_document_memory(
    *,
    settings: AppSettings,
    manager: DocumentMemoryManager,
    prepared: PreparedRequest,
    thread_context: PreparedThreadContext,
    action_outcome: PreparedActionOutcome,
    reply: str,
) -> str | None:
    if not settings.document_memory_update_enabled:
        return None
    try:
        proposal = await manager.propose_update(
            document_workspace=prepared.document_workspace,
            collection=prepared.collection,
            document=action_outcome.effective_document,
            user_comment=thread_context.prompt_text,
            assistant_reply=reply,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Document memory update proposal failed: {}", exc)
        return f"document-memory-update-error: {exc}"

    preview = manager.preview(proposal)
    applied_preview = manager.apply_update(prepared.document_workspace, proposal)
    return applied_preview or preview


def record_thread_turn(
    *,
    settings: AppSettings,
    prepared: PreparedRequest,
    thread_context: PreparedThreadContext,
    action_outcome: PreparedActionOutcome,
    reply: str,
    assistant_comment_id: str | None = None,
) -> None:
    prepared.thread_workspace.record_turn(
        comment_id=prepared.comment.id,
        user_comment=thread_context.prompt_text,
        assistant_reply=reply,
        assistant_comment_id=assistant_comment_id,
        document_id=action_outcome.effective_document.id,
        document_title=action_outcome.effective_document.title,
        max_recent_turns=settings.thread_recent_turns,
        max_turn_chars=settings.thread_turn_max_chars,
    )


async def persist_workspace_updates(
    *,
    settings: AppSettings,
    memory_manager: CollectionMemoryManager,
    document_memory_manager: DocumentMemoryManager,
    prepared: PreparedRequest,
    thread_context: PreparedThreadContext,
    action_outcome: PreparedActionOutcome,
    reply: str,
    skip_memory_update: bool,
) -> ReplyPersistenceOutcome:
    document_memory_update_preview = await maybe_update_document_memory(
        settings=settings,
        manager=document_memory_manager,
        prepared=prepared,
        thread_context=thread_context,
        action_outcome=action_outcome,
        reply=reply,
    )
    memory_update_preview = (
        await maybe_update_memory(
            settings=settings,
            manager=memory_manager,
            prepared=prepared,
            thread_context=thread_context,
            action_outcome=action_outcome,
            reply=reply,
        )
        if not skip_memory_update
        else None
    )
    return ReplyPersistenceOutcome(
        memory_update_preview=memory_update_preview,
        document_memory_update_preview=document_memory_update_preview,
    )


def build_processing_result(
    *,
    settings: AppSettings,
    prepared: PreparedRequest,
    thread_context: PreparedThreadContext,
    action_outcome: PreparedActionOutcome,
    reply: str,
    persistence: ReplyPersistenceOutcome,
) -> ProcessingResult:
    action = (
        "dry-run"
        if settings.dry_run
        else _resolve_success_action(
            document_creation_status=action_outcome.document_creation_status,
            document_update_status=action_outcome.document_update_status,
            tool_execution_status=action_outcome.tool_execution_status,
        )
    )
    reason = (
        _resolve_dry_run_reason(
            document_creation_status=action_outcome.document_creation_status,
            document_update_status=action_outcome.document_update_status,
            tool_execution_status=action_outcome.tool_execution_status,
        )
        if settings.dry_run
        else _resolve_success_reason(
            document_creation_status=action_outcome.document_creation_status,
            document_update_status=action_outcome.document_update_status,
            tool_execution_status=action_outcome.tool_execution_status,
        )
    )
    return ProcessingResult(
        action=action,
        reason=reason,
        comment_id=prepared.comment.id,
        document_id=prepared.comment.documentId,
        collection_id=prepared.document.collection_id,
        collection_workspace=str(prepared.workspace.root_dir),
        document_workspace=str(prepared.document_workspace.root_dir),
        thread_workspace=str(prepared.thread_workspace.root_dir),
        triggered_alias=prepared.triggered_alias,
        reply_preview=_preview(reply),
        action_route_preview=_format_action_route_preview(thread_context.action_route),
        document_creation_preview=action_outcome.document_creation_preview,
        document_update_preview=action_outcome.document_update_preview,
        tool_execution_preview=action_outcome.tool_execution_preview,
        memory_action_preview=action_outcome.memory_action_preview,
        same_document_comment_preview=thread_context.same_document_comment_preview,
        memory_update_preview=persistence.memory_update_preview,
        document_memory_update_preview=persistence.document_memory_update_preview,
        handoff_preview=thread_context.handoff.preview if thread_context.handoff else None,
    )
