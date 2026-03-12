from __future__ import annotations

from ..clients.outline_client import OutlineClient
from ..core.config import AppSettings
from ..models.webhook_models import WebhookEnvelope
from ..state.store import ProcessedEventStore
from ..state.workspace import CollectionWorkspaceManager
from ..utils.rich_text import extract_image_refs, extract_mentions, extract_prompt_text
from .processor_context import (
    detect_direct_trigger as _detect_direct_trigger,
)
from .processor_context import (
    prepare_comment_image_inputs as _prepare_comment_image_inputs,
)
from .processor_context import (
    resolve_collection as _resolve_collection,
)
from .processor_identity import resolve_agent_identity as _resolve_agent_identity
from .processor_prompting import (
    comment_author_name as _comment_author_name,
)
from .processor_prompting import (
    comment_created_at as _comment_created_at,
)
from .processor_prompting import (
    thread_root_id as _thread_root_id,
)
from .processor_types import (
    PreparedRequest,
    ProcessingResult,
)


async def prepare_request(
    *,
    settings: AppSettings,
    store: ProcessedEventStore,
    outline_client: OutlineClient,
    workspace_manager: CollectionWorkspaceManager,
    envelope: WebhookEnvelope,
) -> PreparedRequest | ProcessingResult:
    if envelope.event != "comments.create":
        return ProcessingResult(action="ignored", reason="unsupported-event")

    comment = envelope.payload.model
    semantic_key = f"comments.create:{comment.id}"
    if store.contains(semantic_key):
        return ProcessingResult(
            action="ignored",
            reason="duplicate-comment-event",
            comment_id=comment.id,
            document_id=comment.documentId,
        )

    agent_user_id = await _resolve_agent_identity(
        settings=settings,
        outline_client=outline_client,
    )
    self_authored_user_ids = {user_id for user_id in (agent_user_id,) if user_id}
    if envelope.actorId in self_authored_user_ids or comment.createdById in self_authored_user_ids:
        return ProcessingResult(
            action="ignored",
            reason="self-authored-event",
            comment_id=comment.id,
            document_id=comment.documentId,
        )

    comment_text = extract_prompt_text(comment.data)
    comment_author_name = _comment_author_name(comment)
    comment_created_at = _comment_created_at(comment, envelope)
    mentions = extract_mentions(comment.data)
    comment_image_sources = [item.src for item in extract_image_refs(comment.data)]
    triggered_alias = _detect_direct_trigger(
        settings=settings,
        comment_text=comment_text,
        mentions=mentions,
        agent_user_id=agent_user_id,
    )
    reply_trigger_pending = (
        settings.trigger_mode == "mention"
        and not triggered_alias
        and settings.trigger_on_reply_to_agent
        and bool(agent_user_id)
        and bool(comment.parentCommentId)
    )

    document = await outline_client.document_info(comment.documentId)
    if settings.collection_allowlist and document.collection_id not in settings.collection_allowlist:
        return ProcessingResult(
            action="ignored",
            reason="collection-not-allowed",
            comment_id=comment.id,
            document_id=comment.documentId,
            collection_id=document.collection_id,
        )

    collection = await _resolve_collection(
        outline_client=outline_client,
        document=document,
    )
    workspace = workspace_manager.ensure(
        collection_id=collection.id if collection else document.collection_id or "unknown",
        collection_name=collection.name if collection else document.collection_id or "unknown",
    )
    thread_root_id = _thread_root_id(comment)
    thread_workspace = workspace_manager.ensure_thread(
        workspace,
        thread_id=thread_root_id,
        document_id=document.id,
        document_title=document.title,
    )
    thread_workspace.record_observed_comment(
        comment_id=comment.id,
        author_id=comment.createdById,
        author_name=comment_author_name,
        comment_text=comment_text,
        created_at=comment_created_at,
        parent_comment_id=comment.parentCommentId,
        document_id=document.id,
        document_title=document.title,
        max_recent_comments=settings.thread_recent_comments,
        max_comment_chars=settings.thread_comment_max_chars,
    )

    if settings.trigger_mode == "mention" and not triggered_alias and not reply_trigger_pending:
        store.add(semantic_key)
        return ProcessingResult(
            action="ignored",
            reason="no-trigger-mention",
            comment_id=comment.id,
            document_id=comment.documentId,
            collection_id=document.collection_id,
            collection_workspace=str(workspace.root_dir),
            thread_workspace=str(thread_workspace.root_dir),
        )

    comment_image_inputs = await _prepare_comment_image_inputs(
        outline_client=outline_client,
        thread_workspace=thread_workspace,
        comment_id=comment.id,
        image_sources=comment_image_sources,
    )

    return PreparedRequest(
        semantic_key=semantic_key,
        comment=comment,
        document=document,
        collection=collection,
        workspace=workspace,
        thread_workspace=thread_workspace,
        comment_text=comment_text,
        comment_image_sources=comment_image_sources,
        comment_image_inputs=comment_image_inputs,
        mentions=mentions,
        agent_user_id=agent_user_id,
        triggered_alias=triggered_alias,
        reply_trigger_pending=reply_trigger_pending,
    )
