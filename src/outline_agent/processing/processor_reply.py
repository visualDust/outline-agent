from __future__ import annotations

from ..clients.model_client import ModelClient
from ..clients.outline_client import OutlineClient, OutlineClientError
from ..clients.outline_models import OutlineCollection, OutlineDocument
from ..core.config import AppSettings
from ..core.logging import logger
from ..core.prompt_registry import PromptPack, PromptRegistry
from ..managers.collection_memory_sync import CollectionMemorySync
from ..managers.document_memory_manager import DocumentMemoryManager
from ..managers.memory_action_manager import MemoryActionManager
from ..managers.memory_manager import CollectionMemoryManager
from ..state.store import ProcessedEventStore
from ..state.workspace import CollectionWorkspace
from .processor_artifacts import (
    append_uploaded_attachment_links as _append_uploaded_attachment_links,
)
from .processor_prompting import (
    build_system_prompt as _build_system_prompt,
)
from .processor_prompting import (
    build_user_prompt as _build_user_prompt,
)
from .processor_prompting import (
    created_comment_id as _created_comment_id,
)
from .processor_prompting import (
    preview as _preview,
)
from .processor_reply_persistence import (
    build_processing_result as _build_processing_result,
)
from .processor_reply_persistence import (
    persist_workspace_updates as _persist_workspace_updates,
)
from .processor_reply_persistence import (
    record_thread_turn as _record_thread_turn,
)
from .processor_side_effects import (
    post_reply_comment as _post_reply_comment,
)
from .processor_side_effects import (
    record_progress_comment_state as _record_progress_comment_state,
)
from .reply_stream_coordinator import ReplyStreamCoordinator
from .processor_types import (
    PreparedActionOutcome,
    PreparedRequest,
    PreparedThreadContext,
    ProcessingResult,
)


def empty_action_outcome(document: OutlineDocument) -> PreparedActionOutcome:
    return PreparedActionOutcome(
        memory_action_status=None,
        memory_action_preview=None,
        memory_action_context=None,
        document_creation_status=None,
        document_creation_preview=None,
        document_creation_context=None,
        created_document=None,
        document_update_status=None,
        document_update_preview=None,
        document_update_context=None,
        tool_execution_status=None,
        tool_execution_preview=None,
        tool_execution_context=None,
        effective_document=document,
        uploaded_attachments=[],
    )


async def maybe_apply_memory_actions(
    *,
    settings: AppSettings,
    manager: MemoryActionManager,
    memory_sync: CollectionMemorySync,
    workspace: CollectionWorkspace,
    collection: OutlineCollection | None,
    document: OutlineDocument,
    user_comment: str,
    should_attempt: bool,
) -> tuple[str | None, str | None, str | None]:
    if not settings.memory_action_enabled or not should_attempt:
        return None, None, None

    try:
        plan = await manager.propose_actions(
            workspace=workspace,
            collection=collection,
            document=document,
            user_comment=user_comment,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Memory action proposal failed: {}", exc)
        return None, None, None

    if not plan.actions:
        return None, None, None

    preview = manager.preview(plan)
    if settings.dry_run:
        context = manager.format_reply_context(
            plan,
            status="planned-dry-run",
            applied=[],
            errors=[],
        )
        return "planned-dry-run", preview, context

    apply_result = await memory_sync.persist_actions(
        workspace=workspace,
        collection=collection,
        plan=plan,
    )
    status = apply_result.status
    context = manager.format_reply_context(
        plan,
        status=status,
        applied=apply_result.applied,
        errors=apply_result.errors,
    )
    return status, preview, context


async def generate_reply_text(
    *,
    settings: AppSettings,
    model_client: ModelClient,
    outline_client: OutlineClient,
    prompt_registry: PromptRegistry,
    prompt_packs: list[PromptPack],
    prepared: PreparedRequest,
    thread_context: PreparedThreadContext,
    action_outcome: PreparedActionOutcome,
) -> str:
    system_prompt = _build_system_prompt(
        system_prompt=settings.system_prompt,
        workspace=prepared.workspace,
        prompt_packs=prompt_packs,
        max_memory_chars=settings.max_memory_chars,
    )
    user_prompt = _build_user_prompt(
        comment=prepared.comment,
        document=action_outcome.effective_document,
        collection=prepared.collection,
        workspace=prepared.workspace,
        document_workspace=prepared.document_workspace,
        thread_workspace=prepared.thread_workspace,
        prompt_text=thread_context.prompt_text,
        comment_context=thread_context.comment_context,
        document_creation_context=action_outcome.document_creation_context,
        document_update_context=action_outcome.document_update_context,
        tool_execution_context=action_outcome.tool_execution_context,
        memory_action_context=action_outcome.memory_action_context,
        same_document_comment_context=thread_context.same_document_comment_context,
        related_documents_context=thread_context.related_documents_context,
        handoff=thread_context.handoff,
        current_comment_image_count=len(prepared.comment_image_inputs),
        reply_policy_text=prompt_registry.load_user_optional("reply_policy.md"),
        max_document_chars=settings.max_document_chars,
        max_document_memory_chars=settings.max_document_memory_chars,
        max_prompt_chars=settings.max_prompt_chars,
    )
    placeholder_comment_id = (
        prepared.thread_workspace.progress_comment_id_for(prepared.comment.id)
        if settings.progress_comment_enabled
        else None
    )
    coordinator = ReplyStreamCoordinator(
        model_client=model_client,
        outline_client=outline_client,
        document_id=prepared.comment.documentId,
        placeholder_comment_id=placeholder_comment_id,
    )
    reply = await coordinator.generate_reply(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        input_images=prepared.comment_image_inputs,
    )
    return _append_uploaded_attachment_links(reply, action_outcome.uploaded_attachments)


async def persist_reply_and_build_result(
    *,
    settings: AppSettings,
    store: ProcessedEventStore,
    outline_client: OutlineClient,
    memory_manager: CollectionMemoryManager,
    collection_memory_sync: CollectionMemorySync,
    document_memory_manager: DocumentMemoryManager,
    prepared: PreparedRequest,
    thread_context: PreparedThreadContext,
    action_outcome: PreparedActionOutcome,
    reply: str,
) -> ProcessingResult:
    skip_memory_update = action_outcome.memory_action_status is not None

    if settings.dry_run:
        _record_thread_turn(
            settings=settings,
            prepared=prepared,
            thread_context=thread_context,
            action_outcome=action_outcome,
            reply=reply,
        )
        persistence = await _persist_workspace_updates(
            settings=settings,
            memory_manager=memory_manager,
            collection_memory_sync=collection_memory_sync,
            document_memory_manager=document_memory_manager,
            prepared=prepared,
            thread_context=thread_context,
            action_outcome=action_outcome,
            reply=reply,
            skip_memory_update=skip_memory_update,
        )
        store.add(prepared.semantic_key)
        return _build_processing_result(
            settings=settings,
            prepared=prepared,
            thread_context=thread_context,
            action_outcome=action_outcome,
            reply=reply,
            persistence=persistence,
        )

    reply_parent_comment_id = prepared.comment.parentCommentId or prepared.comment.id
    placeholder_comment_id = (
        prepared.thread_workspace.progress_comment_id_for(prepared.comment.id)
        if settings.progress_comment_enabled
        else None
    )
    try:
        reply_result = await _post_reply_comment(
            outline_client=outline_client,
            document_id=prepared.comment.documentId,
            parent_comment_id=reply_parent_comment_id,
            placeholder_comment_id=placeholder_comment_id,
            reply=reply,
        )
    except OutlineClientError:
        logger.warning(
            ("Failed to post reply comment for request {} in thread {} (document {}, reply_len={}, preview={})"),
            prepared.comment.id,
            prepared.thread_workspace.thread_id,
            prepared.comment.documentId,
            len(reply),
            _preview(reply),
        )
        raise

    if placeholder_comment_id:
        _record_progress_comment_state(
            settings=settings,
            thread_workspace=prepared.thread_workspace,
            request_comment_id=prepared.comment.id,
            status_comment_id=placeholder_comment_id,
            status="replied",
            summary=_preview(reply),
            actions=[],
        )

    _record_thread_turn(
        settings=settings,
        prepared=prepared,
        thread_context=thread_context,
        action_outcome=action_outcome,
        reply=reply,
        assistant_comment_id=_created_comment_id(reply_result),
    )
    persistence = await _persist_workspace_updates(
        settings=settings,
        memory_manager=memory_manager,
        collection_memory_sync=collection_memory_sync,
        document_memory_manager=document_memory_manager,
        prepared=prepared,
        thread_context=thread_context,
        action_outcome=action_outcome,
        reply=reply,
        skip_memory_update=skip_memory_update,
    )
    store.add(prepared.semantic_key)
    return _build_processing_result(
        settings=settings,
        prepared=prepared,
        thread_context=thread_context,
        action_outcome=action_outcome,
        reply=reply,
        persistence=persistence,
    )
