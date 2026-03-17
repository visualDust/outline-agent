from __future__ import annotations

from ..clients.outline_client import OutlineClient
from ..clients.outline_models import OutlineComment
from ..core.config import AppSettings
from ..core.logging import logger
from ..models.webhook_models import CollectionDeleteModel, CommentModel, DocumentDeleteModel, WebhookEnvelope
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

SUPPORTED_COMMENT_EVENTS = {"comments.create", "comments.update", "comments.delete"}
SUPPORTED_DELETE_EVENTS = {"documents.delete", "collections.delete"}
SUPPORTED_EVENTS = SUPPORTED_COMMENT_EVENTS | SUPPORTED_DELETE_EVENTS


async def prepare_request(
    *,
    settings: AppSettings,
    store: ProcessedEventStore,
    outline_client: OutlineClient,
    workspace_manager: CollectionWorkspaceManager,
    envelope: WebhookEnvelope,
) -> PreparedRequest | ProcessingResult:
    if envelope.event not in SUPPORTED_EVENTS:
        return ProcessingResult(action="ignored", reason="unsupported-event")

    if envelope.event == "documents.delete":
        return _handle_document_delete(
            store=store,
            workspace_manager=workspace_manager,
            envelope=envelope,
        )

    if envelope.event == "collections.delete":
        return _handle_collection_delete(
            store=store,
            workspace_manager=workspace_manager,
            envelope=envelope,
        )

    comment = envelope.payload.model
    if not isinstance(comment, CommentModel):
        return ProcessingResult(action="ignored", reason="invalid-comment-payload")

    semantic_key = (
        f"{envelope.event}:{comment.id}"
        if envelope.event == "comments.create"
        else f"{envelope.event}:{envelope.id}"
    )
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
    document_workspace = workspace_manager.ensure_document(
        workspace,
        document_id=document.id,
        document_title=document.title,
    )
    thread_root_id = _thread_root_id(comment)
    thread_workspace = workspace_manager.ensure_thread(
        workspace,
        thread_id=thread_root_id,
        document_id=document.id,
        document_title=document.title,
    )

    document_comments = await _fetch_document_comments(
        settings=settings,
        outline_client=outline_client,
        document_id=document.id,
    )
    thread_comments = _select_thread_comments(document_comments, thread_root_id)
    if envelope.event != "comments.delete" and not any(item.id == comment.id for item in thread_comments):
        thread_comments.append(
            OutlineComment(
                id=comment.id,
                document_id=comment.documentId,
                parent_comment_id=comment.parentCommentId,
                created_by_id=comment.createdById,
                created_by_name=_comment_author_name(comment),
                created_at=_comment_created_at(comment, envelope),
                data=comment.data,
            )
        )
        thread_comments.sort(key=lambda item: (item.created_at or "", item.id))
    logger.debug(
        "Prepared comment event: event={}, comment_id={}, document_id={}, thread_id={}, "
        "document_comments={}, thread_comments={}",
        envelope.event,
        comment.id,
        document.id,
        thread_root_id,
        len(document_comments),
        len(thread_comments),
    )

    root_exists = any(item.id == thread_root_id for item in thread_comments)
    if envelope.event == "comments.delete" and not root_exists:
        logger.debug(
            "Comment delete removed thread root; archiving thread workspace: comment_id={}, document_id={}, "
            "thread_id={}, workspace={}, thread_workspace={}",
            comment.id,
            document.id,
            thread_root_id,
            workspace.root_dir,
            thread_workspace.root_dir,
        )
        thread_workspace.mark_deleted(document_id=document.id, document_title=document.title)
        archived_path = workspace_manager.archive_thread(workspace, thread_workspace, reason="root-comment-deleted")
        logger.debug(
            "Thread workspace archived after root comment delete: comment_id={}, document_id={}, thread_id={}, "
            "archived_path={}",
            comment.id,
            document.id,
            thread_root_id,
            archived_path,
        )
        store.add(semantic_key)
        return ProcessingResult(
            action="synced",
            reason="thread-deleted",
            comment_id=comment.id,
            document_id=document.id,
            collection_id=document.collection_id,
            collection_workspace=str(workspace.root_dir),
            thread_workspace=str(thread_workspace.root_dir),
        )

    thread_workspace.sync_transcript_from_comments(
        document_id=document.id,
        document_title=document.title,
        comments=thread_comments,
        max_recent_comments=settings.thread_recent_comments,
        max_comment_chars=settings.thread_comment_max_chars,
    )

    if envelope.event == "comments.update":
        store.add(semantic_key)
        return ProcessingResult(
            action="synced",
            reason="comment-updated",
            comment_id=comment.id,
            document_id=document.id,
            collection_id=document.collection_id,
            collection_workspace=str(workspace.root_dir),
            thread_workspace=str(thread_workspace.root_dir),
        )

    if envelope.event == "comments.delete":
        logger.debug(
            "Comment delete synchronized without thread archive: comment_id={}, document_id={}, "
            "thread_id={}, root_exists={}, thread_comments={}",
            comment.id,
            document.id,
            thread_root_id,
            root_exists,
            len(thread_comments),
        )
        store.add(semantic_key)
        return ProcessingResult(
            action="synced",
            reason="comment-deleted",
            comment_id=comment.id,
            document_id=document.id,
            collection_id=document.collection_id,
            collection_workspace=str(workspace.root_dir),
            thread_workspace=str(thread_workspace.root_dir),
        )

    self_authored_user_ids = {user_id for user_id in (agent_user_id,) if user_id}
    if envelope.actorId in self_authored_user_ids or comment.createdById in self_authored_user_ids:
        store.add(semantic_key)
        return ProcessingResult(
            action="ignored",
            reason="self-authored-event",
            comment_id=comment.id,
            document_id=comment.documentId,
            collection_id=document.collection_id,
            collection_workspace=str(workspace.root_dir),
            thread_workspace=str(thread_workspace.root_dir),
        )

    comment_text = extract_prompt_text(comment.data)
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
        workspace=workspace,
        thread_workspace=thread_workspace,
        comment_id=comment.id,
        image_sources=comment_image_sources,
    )

    return PreparedRequest(
        semantic_key=semantic_key,
        event=envelope.event,
        comment=comment,
        document=document,
        collection=collection,
        workspace=workspace,
        document_workspace=document_workspace,
        thread_workspace=thread_workspace,
        document_comments=document_comments,
        thread_comments=thread_comments,
        comment_text=comment_text,
        comment_image_sources=comment_image_sources,
        comment_image_inputs=comment_image_inputs,
        mentions=mentions,
        agent_user_id=agent_user_id,
        triggered_alias=triggered_alias,
        reply_trigger_pending=reply_trigger_pending,
    )


def _handle_document_delete(
    *,
    store: ProcessedEventStore,
    workspace_manager: CollectionWorkspaceManager,
    envelope: WebhookEnvelope,
) -> ProcessingResult:
    model = envelope.payload.model
    if not isinstance(model, DocumentDeleteModel):
        return ProcessingResult(action="ignored", reason="invalid-document-delete-payload")

    semantic_key = f"{envelope.event}:{envelope.id}"
    document_id = model.resolved_document_id
    collection_id = model.collectionId
    if store.contains(semantic_key):
        return ProcessingResult(
            action="ignored",
            reason="duplicate-delete-event",
            document_id=document_id,
            collection_id=collection_id,
        )

    logger.debug(
        "Document delete event received: event_id={}, payload_id={}, document_id={}, collection_id={}, title={}",
        envelope.id,
        envelope.payload.id,
        document_id,
        collection_id,
        model.title,
    )

    workspace = (
        workspace_manager.find_collection(collection_id)
        if collection_id
        else workspace_manager.find_collection_for_document(document_id)
    )
    archived_collection_dir = (
        workspace_manager.find_archived_collection_dir(collection_id) if collection_id else None
    )
    archived_document_dir = workspace_manager.find_archived_document_globally(document_id=document_id)

    if workspace is None:
        reason = (
            "collection-already-archived"
            if archived_collection_dir is not None
            else "document-already-archived"
            if archived_document_dir is not None
            else "document-delete-noop"
        )
        logger.debug(
            "Document delete no-op before archival: event_id={}, document_id={}, collection_id={}, reason={}, "
            "archived_collection_dir={}, archived_document_dir={}",
            envelope.id,
            document_id,
            collection_id,
            reason,
            archived_collection_dir,
            archived_document_dir,
        )
        store.add(semantic_key)
        return ProcessingResult(
            action="synced",
            reason=reason,
            document_id=document_id,
            collection_id=collection_id,
            collection_workspace=str(archived_collection_dir) if archived_collection_dir is not None else None,
            document_workspace=str(archived_document_dir) if archived_document_dir is not None else None,
        )

    document_workspace = workspace_manager.find_document(workspace, document_id=document_id)
    if document_workspace is None:
        candidate_document_path = workspace_manager.document_workspace_path(workspace, document_id=document_id)
        candidate_archived_document_path = workspace_manager.archived_document_workspace_path(
            workspace,
            document_id=document_id,
        )
        archived_document_dir = workspace_manager.find_archived_document_dir(workspace, document_id=document_id)
        reason = "document-already-archived" if archived_document_dir is not None else "document-delete-noop"
        logger.debug(
            "Document delete no-op inside active collection: event_id={}, document_id={}, collection_id={}, "
            "workspace={}, candidate_document_path={}, candidate_archived_document_path={}, reason={}, "
            "archived_document_dir={}",
            envelope.id,
            document_id,
            workspace.collection_id,
            workspace.root_dir,
            candidate_document_path,
            candidate_archived_document_path,
            reason,
            archived_document_dir,
        )
        store.add(semantic_key)
        return ProcessingResult(
            action="synced",
            reason=reason,
            document_id=document_id,
            collection_id=workspace.collection_id,
            collection_workspace=str(workspace.root_dir),
            document_workspace=str(archived_document_dir) if archived_document_dir is not None else None,
        )

    document_state = document_workspace.read_state()
    document_title = (
        model.title
        if isinstance(model.title, str) and model.title.strip()
        else document_state.get("document_title")
        if isinstance(document_state.get("document_title"), str)
        else None
    )

    thread_workspaces = workspace_manager.list_active_thread_workspaces_for_document(
        workspace,
        document_id=document_id,
    )
    logger.debug(
        "Document delete resolved active workspace: event_id={}, document_id={}, collection_id={}, workspace={}, "
        "document_workspace={}, thread_count={}",
        envelope.id,
        document_id,
        workspace.collection_id,
        workspace.root_dir,
        document_workspace.root_dir,
        len(thread_workspaces),
    )

    archived_thread_paths: list[str] = []
    for thread_workspace in thread_workspaces:
        logger.debug(
            "Document delete archiving thread workspace: event_id={}, document_id={}, "
            "thread_id={}, thread_workspace={}",
            envelope.id,
            document_id,
            thread_workspace.thread_id,
            thread_workspace.root_dir,
        )
        thread_workspace.mark_deleted(document_id=document_id, document_title=document_title)
        archived_thread_paths.append(
            str(
                workspace_manager.archive_thread(
                    workspace,
                    thread_workspace,
                    reason=f"document-deleted:{document_id}",
                )
            )
        )

    document_workspace.mark_deleted(document_title=document_title, reason="document-deleted")
    archived_document_path = workspace_manager.archive_document(
        workspace,
        document_workspace,
        reason="document-deleted",
    )
    logger.debug(
        "Document delete archived document workspace: event_id={}, document_id={}, collection_id={}, "
        "archived_threads={}, archived_document_path={}",
        envelope.id,
        document_id,
        workspace.collection_id,
        archived_thread_paths,
        archived_document_path,
    )

    store.add(semantic_key)
    return ProcessingResult(
        action="synced",
        reason="document-deleted",
        document_id=document_id,
        collection_id=workspace.collection_id,
        collection_workspace=str(workspace.root_dir),
        document_workspace=str(archived_document_path),
    )


def _handle_collection_delete(
    *,
    store: ProcessedEventStore,
    workspace_manager: CollectionWorkspaceManager,
    envelope: WebhookEnvelope,
) -> ProcessingResult:
    model = envelope.payload.model
    if not isinstance(model, CollectionDeleteModel):
        return ProcessingResult(action="ignored", reason="invalid-collection-delete-payload")

    semantic_key = f"{envelope.event}:{envelope.id}"
    collection_id = model.resolved_collection_id
    if store.contains(semantic_key):
        return ProcessingResult(
            action="ignored",
            reason="duplicate-delete-event",
            collection_id=collection_id,
        )

    logger.debug(
        "Collection delete event received: event_id={}, payload_id={}, collection_id={}, name={}",
        envelope.id,
        envelope.payload.id,
        collection_id,
        model.name,
    )

    workspace = workspace_manager.find_collection(collection_id)
    if workspace is None:
        archived_collection_dir = workspace_manager.find_archived_collection_dir(collection_id)
        reason = "collection-already-archived" if archived_collection_dir is not None else "collection-delete-noop"
        logger.debug(
            "Collection delete no-op: event_id={}, collection_id={}, reason={}, archived_collection_dir={}",
            envelope.id,
            collection_id,
            reason,
            archived_collection_dir,
        )
        store.add(semantic_key)
        return ProcessingResult(
            action="synced",
            reason=reason,
            collection_id=collection_id,
            collection_workspace=str(archived_collection_dir) if archived_collection_dir is not None else None,
        )

    logger.debug(
        "Collection delete resolved active workspace: event_id={}, collection_id={}, workspace={}",
        envelope.id,
        collection_id,
        workspace.root_dir,
    )
    archived_collection_path = workspace_manager.archive_collection(workspace, reason="collection-deleted")
    logger.debug(
        "Collection delete archived collection workspace: event_id={}, collection_id={}, archived_collection_path={}",
        envelope.id,
        collection_id,
        archived_collection_path,
    )

    store.add(semantic_key)
    return ProcessingResult(
        action="synced",
        reason="collection-deleted",
        collection_id=collection_id,
        collection_workspace=str(archived_collection_path),
    )


async def _fetch_document_comments(
    *,
    settings: AppSettings,
    outline_client: OutlineClient,
    document_id: str,
) -> list[OutlineComment]:
    comments: list[OutlineComment] = []
    offset = 0
    page_size = max(1, settings.comment_list_limit)
    while True:
        batch = await outline_client.comments_list(document_id, limit=page_size, offset=offset)
        comments.extend(batch)
        if len(batch) < page_size:
            break
        offset += len(batch)
    logger.debug(
        "Fetched document comments for transcript sync: document_id={}, total_comments={}, page_size={}",
        document_id,
        len(comments),
        page_size,
    )
    return comments



def _select_thread_comments(comments: list[OutlineComment], thread_root_id: str) -> list[OutlineComment]:
    related = [
        item
        for item in comments
        if item.id == thread_root_id or item.parent_comment_id == thread_root_id
    ]
    related.sort(key=lambda item: (item.created_at or "", item.id))
    return related
