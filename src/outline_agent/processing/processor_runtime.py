from __future__ import annotations

from ..clients.outline_client import OutlineClientError
from ..core.logging import logger
from ..models.webhook_models import WebhookEnvelope
from ..utils.error_reporting import generate_error_id
from .processor_flow import (
    prepare_action_outcome as _prepare_action_outcome,
)
from .processor_flow import (
    prepare_thread_context as _prepare_thread_context,
)
from .processor_flow import (
    resolve_thread_trigger as _resolve_thread_trigger,
)
from .processor_identity import invalidate_runtime_identity as _invalidate_runtime_identity
from .processor_identity import is_outline_auth_error as _is_outline_auth_error
from .processor_reply import (
    generate_reply_text as _generate_reply_text,
)
from .processor_reply import (
    persist_reply_and_build_result as _persist_reply_and_build_result,
)
from .processor_request import prepare_request as _prepare_request
from .processor_services import ProcessorServices
from .processor_side_effects import (
    clear_processing as _clear_processing,
)
from .processor_side_effects import (
    mark_done as _mark_done,
)
from .processor_side_effects import (
    notify_failure as _notify_failure,
)
from .processor_types import ProcessingResult


async def handle_comment(
    *,
    services: ProcessorServices,
    envelope: WebhookEnvelope,
) -> ProcessingResult:
    try:
        prepared = await _prepare_request(
            settings=services.settings,
            store=services.store,
            outline_client=services.outline_client,
            workspace_manager=services.workspace_manager,
            envelope=envelope,
        )
    except OutlineClientError as exc:
        if _is_outline_auth_error(exc):
            _invalidate_runtime_identity(settings=services.settings, reason=str(exc))
        raise
    if isinstance(prepared, ProcessingResult):
        return prepared

    processing_started = False
    processing_reaction_applied = False
    try:
        resolved_trigger = await _resolve_thread_trigger(
            services=services,
            prepared=prepared,
        )
        if isinstance(resolved_trigger, ProcessingResult):
            return resolved_trigger
        processing_started = True
        prepared.triggered_alias = resolved_trigger.triggered_alias

        processing_reaction_applied, thread_context = await _prepare_thread_context(
            services=services,
            prepared=prepared,
            resolved_trigger=resolved_trigger,
        )
        action_outcome = await _prepare_action_outcome(
            services=services,
            prepared=prepared,
            thread_context=thread_context,
        )
        reply = await _generate_reply_text(
            settings=services.settings,
            model_client=services.model_client,
            outline_client=services.outline_client,
            prompt_registry=services.prompt_registry,
            prompt_packs=services.prompt_packs,
            prepared=prepared,
            thread_context=thread_context,
            action_outcome=action_outcome,
        )
        result = await _persist_reply_and_build_result(
            settings=services.settings,
            store=services.store,
            outline_client=services.outline_client,
            memory_manager=services.memory_manager,
            document_memory_manager=services.document_memory_manager,
            prepared=prepared,
            thread_context=thread_context,
            action_outcome=action_outcome,
            reply=reply,
        )

        if processing_reaction_applied:
            await _mark_done(
                settings=services.settings,
                outline_client=services.outline_client,
                comment_id=prepared.comment.id,
            )
        return result
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, OutlineClientError) and _is_outline_auth_error(exc):
            _invalidate_runtime_identity(settings=services.settings, reason=str(exc))
        if processing_reaction_applied:
            await _clear_processing(
                settings=services.settings,
                outline_client=services.outline_client,
                comment_id=prepared.comment.id,
            )
        if not processing_started:
            raise

        error_id = generate_error_id()
        logger.exception(
            "Comment processing failed (error_id={}, comment_id={}, document_id={})",
            error_id,
            prepared.comment.id,
            prepared.comment.documentId,
        )
        await _notify_failure(
            settings=services.settings,
            outline_client=services.outline_client,
            thread_workspace=prepared.thread_workspace,
            request_comment_id=prepared.comment.id,
            document_id=prepared.comment.documentId,
            parent_comment_id=prepared.comment.parentCommentId,
            error_id=error_id,
            exc=exc,
        )
        services.store.add(prepared.semantic_key)
        return ProcessingResult(
            action="error",
            reason="internal-error",
            comment_id=prepared.comment.id,
            document_id=prepared.comment.documentId,
            collection_id=prepared.document.collection_id,
            collection_workspace=str(prepared.workspace.root_dir),
            thread_workspace=str(prepared.thread_workspace.root_dir),
            triggered_alias=prepared.triggered_alias,
        )
