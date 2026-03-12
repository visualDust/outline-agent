from __future__ import annotations

from typing import Any

from ..clients.outline_client import OutlineClient, OutlineClientError
from ..clients.outline_comments import prepare_comment_chunks as _prepare_comment_chunks
from ..clients.outline_models import OutlineDocument
from ..core.config import AppSettings
from ..core.logging import logger
from ..runtime.tool_runtime import UploadedAttachment
from ..state.workspace import ThreadWorkspace
from ..utils.error_reporting import format_failure_comment
from .processor_artifacts import (
    format_registered_attachment_context as _format_registered_attachment_context,
)
from .processor_artifacts import (
    preview_registered_attachments as _preview_registered_attachments,
)
from .processor_artifacts import (
    register_uploaded_attachments_in_document_text as _register_uploaded_attachments_in_document_text,
)
from .processor_progress import (
    format_progress_comment_text as _format_progress_comment_text,
)
from .processor_progress import (
    progress_comment_headline as _progress_comment_headline,
)
from .processor_prompting import created_comment_id as _created_comment_id
from .processor_types import (
    ArtifactRegistrationResult,
)


async def register_uploaded_attachments_in_document(
    *,
    settings: AppSettings,
    outline_client: OutlineClient,
    document: OutlineDocument,
    uploaded_attachments: list[UploadedAttachment],
) -> ArtifactRegistrationResult:
    if settings.dry_run or not uploaded_attachments:
        return ArtifactRegistrationResult(
            status="skipped",
            preview=None,
            context=None,
            effective_document=document,
        )

    updated_text, registered_items = _register_uploaded_attachments_in_document_text(
        document.text,
        uploaded_attachments,
    )
    if not updated_text or not registered_items:
        return ArtifactRegistrationResult(
            status="skipped",
            preview=None,
            context=None,
            effective_document=document,
        )

    try:
        await outline_client.update_document(
            document_id=document.id,
            text=updated_text,
        )
    except OutlineClientError as exc:
        logger.warning(
            "Failed to register uploaded attachment links in document {}: {}",
            document.id,
            exc,
        )
        return ArtifactRegistrationResult(
            status="failed",
            preview="document artifact registration failed",
            context=(
                "- artifact link registration: failed\n- reason: Uploaded files were not added into the document body."
            ),
            effective_document=document,
        )

    preview = _preview_registered_attachments(registered_items)
    context = _format_registered_attachment_context(registered_items)
    return ArtifactRegistrationResult(
        status="applied",
        preview=preview,
        context=context,
        effective_document=OutlineDocument(
            id=document.id,
            title=document.title,
            collection_id=document.collection_id,
            url=document.url,
            text=updated_text,
        ),
    )


async def sync_progress_comment(
    *,
    settings: AppSettings,
    outline_client: OutlineClient,
    thread_workspace: ThreadWorkspace,
    request_comment_id: str,
    document_id: str,
    status_comment_id: str | None,
    status: str,
    headline: str,
    actions: list[str],
) -> str | None:
    if settings.dry_run or not settings.progress_comment_enabled:
        return status_comment_id

    recent_actions = [item for item in actions if item.strip()][-settings.progress_comment_recent_actions :]
    text = _format_progress_comment_text(
        headline=headline,
        status=status,
        recent_actions=recent_actions,
    )

    resolved_comment_id = status_comment_id or thread_workspace.progress_comment_id_for(request_comment_id)
    try:
        if resolved_comment_id:
            await outline_client.update_comment(resolved_comment_id, text)
        else:
            result = await outline_client.create_comment(
                document_id=document_id,
                text=text,
                parent_comment_id=thread_workspace.thread_id,
            )
            resolved_comment_id = _created_comment_id(result)
            if not resolved_comment_id:
                logger.warning(
                    "Progress comment for request {} was created without a returned id",
                    request_comment_id,
                )

        record_progress_comment_state(
            settings=settings,
            thread_workspace=thread_workspace,
            request_comment_id=request_comment_id,
            status_comment_id=resolved_comment_id,
            status=status,
            summary=headline,
            actions=recent_actions,
        )
    except OutlineClientError as exc:
        logger.warning(
            "Failed to sync progress comment for request {} in thread {}: {}",
            request_comment_id,
            thread_workspace.thread_id,
            exc,
        )
    return resolved_comment_id


async def ensure_reply_placeholder_comment(
    *,
    settings: AppSettings,
    outline_client: OutlineClient,
    thread_workspace: ThreadWorkspace,
    request_comment_id: str,
    document_id: str,
) -> str | None:
    existing_comment_id = thread_workspace.progress_comment_id_for(request_comment_id)
    if existing_comment_id:
        return existing_comment_id
    return await sync_progress_comment(
        settings=settings,
        outline_client=outline_client,
        thread_workspace=thread_workspace,
        request_comment_id=request_comment_id,
        document_id=document_id,
        status_comment_id=None,
        status="thinking",
        headline=_progress_comment_headline("thinking"),
        actions=[],
    )


async def post_reply_comment(
    *,
    outline_client: OutlineClient,
    document_id: str,
    parent_comment_id: str,
    placeholder_comment_id: str | None,
    reply: str,
) -> dict[str, Any]:
    if not placeholder_comment_id:
        return await outline_client.create_comment(
            document_id=document_id,
            text=reply,
            parent_comment_id=parent_comment_id,
        )

    reply_chunks = _prepare_comment_chunks(reply)
    if not reply_chunks:
        raise OutlineClientError("Failed to prepare reply comment chunks")

    try:
        await outline_client.update_comment(placeholder_comment_id, reply_chunks[0])
    except OutlineClientError as exc:
        logger.warning(
            "Failed to update placeholder comment {} with final reply; posting a new reply comment instead: {}",
            placeholder_comment_id,
            exc,
        )
        return await outline_client.create_comment(
            document_id=document_id,
            text=reply,
            parent_comment_id=parent_comment_id,
        )

    for chunk in reply_chunks[1:]:
        await outline_client.create_comment(
            document_id=document_id,
            text=chunk,
            parent_comment_id=parent_comment_id,
        )
    return {"id": placeholder_comment_id}


def record_progress_comment_state(
    *,
    settings: AppSettings,
    thread_workspace: ThreadWorkspace,
    request_comment_id: str,
    status_comment_id: str | None,
    status: str,
    summary: str,
    actions: list[str],
) -> None:
    thread_workspace.record_progress_comment(
        request_comment_id=request_comment_id,
        status_comment_id=status_comment_id,
        status=status,
        summary=summary,
        actions=actions,
        max_recent_entries=settings.tool_recent_runs,
        max_action_chars=settings.tool_run_summary_max_chars,
    )


async def notify_failure(
    *,
    settings: AppSettings,
    outline_client: OutlineClient,
    thread_workspace: ThreadWorkspace,
    request_comment_id: str,
    document_id: str,
    parent_comment_id: str | None,
    error_id: str,
    exc: BaseException,
) -> None:
    if settings.dry_run:
        return

    text = format_failure_comment(error_id=error_id, exc=exc)
    status_comment_id = thread_workspace.progress_comment_id_for(request_comment_id)
    reply_parent_comment_id = parent_comment_id or request_comment_id
    try:
        if status_comment_id:
            try:
                await outline_client.update_comment(status_comment_id, text)
                record_progress_comment_state(
                    settings=settings,
                    thread_workspace=thread_workspace,
                    request_comment_id=request_comment_id,
                    status_comment_id=status_comment_id,
                    status="failed",
                    summary="Internal error while processing the request.",
                    actions=[],
                )
                return
            except OutlineClientError as update_exc:
                logger.warning(
                    "Failed to update failure status comment {} for request {}: {}",
                    status_comment_id,
                    request_comment_id,
                    update_exc,
                )

        await outline_client.create_comment(
            document_id=document_id,
            text=text,
            parent_comment_id=reply_parent_comment_id,
        )
    except OutlineClientError as post_exc:
        logger.warning(
            "Failed to post failure comment for request {} (error_id={}): {}",
            request_comment_id,
            error_id,
            post_exc,
        )


async def mark_processing(
    *,
    settings: AppSettings,
    outline_client: OutlineClient,
    comment_id: str,
) -> bool:
    if not settings.reaction_enabled:
        return False
    try:
        await outline_client.add_comment_reaction(comment_id, settings.reaction_processing_emoji)
        return True
    except OutlineClientError as exc:
        logger.warning("Failed to add processing reaction to comment {}: {}", comment_id, exc)
        return False


async def mark_done(
    *,
    settings: AppSettings,
    outline_client: OutlineClient,
    comment_id: str,
) -> None:
    if not settings.reaction_enabled:
        return
    await safe_remove_reaction(
        outline_client=outline_client,
        comment_id=comment_id,
        emoji=settings.reaction_processing_emoji,
    )
    await safe_add_reaction(
        outline_client=outline_client,
        comment_id=comment_id,
        emoji=settings.reaction_done_emoji,
    )


async def clear_processing(
    *,
    settings: AppSettings,
    outline_client: OutlineClient,
    comment_id: str,
) -> None:
    if not settings.reaction_enabled:
        return
    await safe_remove_reaction(
        outline_client=outline_client,
        comment_id=comment_id,
        emoji=settings.reaction_processing_emoji,
    )


async def safe_add_reaction(
    *,
    outline_client: OutlineClient,
    comment_id: str,
    emoji: str,
) -> None:
    try:
        await outline_client.add_comment_reaction(comment_id, emoji)
    except OutlineClientError as exc:
        logger.warning("Failed to add reaction {} to comment {}: {}", emoji, comment_id, exc)


async def safe_remove_reaction(
    *,
    outline_client: OutlineClient,
    comment_id: str,
    emoji: str,
) -> None:
    try:
        await outline_client.remove_comment_reaction(comment_id, emoji)
    except OutlineClientError as exc:
        logger.warning("Failed to remove reaction {} from comment {}: {}", emoji, comment_id, exc)
