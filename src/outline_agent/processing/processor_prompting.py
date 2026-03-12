from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..clients.outline_client import OutlineCollection, OutlineComment, OutlineDocument
from ..core.prompt_registry import PromptPack
from ..models.webhook_models import CommentModel, WebhookEnvelope
from ..state.workspace import CollectionWorkspace, DocumentWorkspace, ThreadWorkspace
from ..utils.rich_text import MentionRef, extract_prompt_text
from .processor_types import CrossThreadHandoff


def build_system_prompt(
    *,
    system_prompt: str,
    workspace: CollectionWorkspace,
    prompt_packs: list[PromptPack],
    max_memory_chars: int,
) -> str:
    sections: list[str] = [system_prompt.strip()]

    pack_section = _format_prompt_packs(prompt_packs)
    if pack_section:
        sections.append(pack_section)

    collection_prompt = _load_optional_text_file(workspace.system_prompt_path)
    if collection_prompt:
        sections.append(_format_prompt_section("Collection prompt", collection_prompt))

    workspace_context = workspace.load_prompt_context(max_chars=max_memory_chars)
    if workspace_context:
        sections.append(
            f"Collection workspace context follows. Treat it as local durable memory.\n\n{workspace_context}"
        )

    return "\n\n".join(section for section in sections if section).strip()


def build_user_prompt(
    *,
    comment: CommentModel,
    document: OutlineDocument,
    collection: OutlineCollection | None,
    workspace: CollectionWorkspace,
    document_workspace: DocumentWorkspace,
    thread_workspace: ThreadWorkspace,
    prompt_text: str,
    comment_context: str,
    document_creation_context: str | None,
    document_update_context: str | None,
    tool_execution_context: str | None,
    memory_action_context: str | None,
    same_document_comment_context: str | None,
    related_documents_context: str | None,
    handoff: CrossThreadHandoff | None,
    current_comment_image_count: int,
    reply_policy_text: str | None,
    max_document_chars: int,
    max_document_memory_chars: int,
    max_prompt_chars: int,
) -> str:
    collection_name = collection.name if collection and collection.name else document.collection_id or "(unknown)"
    document_excerpt = truncate(document.text or "", max_document_chars) or "(document text unavailable)"
    document_memory_context = document_workspace.load_prompt_context(max_chars=max_document_memory_chars)
    thread_runtime_context = thread_workspace.load_prompt_context(max_chars=max_document_memory_chars)
    document_update_section = ""
    document_update_reply_instruction = ""
    document_creation_section = ""
    document_creation_reply_instruction = ""
    if document_creation_context:
        document_creation_section = f"Document creation outcome:\n{document_creation_context}\n\n"
        if any(
            status_line in document_creation_context
            for status_line in ("- status: applied", "- status: planned-dry-run")
        ):
            document_creation_reply_instruction = (
                "Because a new document was created, keep the comment reply short: "
                "confirm creation in 1-2 sentences, mention the new document briefly, "
                "and do not paste the full new document body. "
            )
    if document_update_context:
        document_update_section = f"Document update outcome:\n{document_update_context}\n\n"
        if any(
            status_line in document_update_context for status_line in ("- status: applied", "- status: planned-dry-run")
        ):
            document_update_reply_instruction = (
                "Because the document itself was updated, keep the comment reply very short: "
                "confirm the write in 1-2 sentences, summarize only the change at a high level, "
                "and do not paste the new document body, outline, diagram source, or long excerpts. "
            )
    tool_execution_section = ""
    if tool_execution_context:
        tool_execution_section = f"Tool execution outcome:\n{tool_execution_context}\n\n"
    memory_action_section = ""
    if memory_action_context:
        memory_action_section = f"Memory action outcome:\n{memory_action_context}\n\n"
    same_document_comment_section = ""
    if same_document_comment_context:
        same_document_comment_section = f"Same-document comment lookup outcome:\n{same_document_comment_context}\n\n"
    related_documents_section = ""
    if related_documents_context:
        related_documents_section = f"Related documents in this collection:\n{related_documents_context}\n\n"
    handoff_section = ""
    if handoff is not None:
        handoff_section = f"Potential cross-thread handoff context:\n{handoff.prompt_section}\n\n"
    current_comment_image_section = ""
    if current_comment_image_count > 0:
        noun = "image" if current_comment_image_count == 1 else "images"
        current_comment_image_section = (
            f"Current user comment also includes {current_comment_image_count} embedded {noun}. "
            "If image inputs are attached to this model request, use them directly.\n\n"
        )
    return (
        "Draft a single helpful reply for an Outline comment thread.\n\n"
        f"Collection: {collection_name}\n"
        f"Collection ID: {document.collection_id or '(unknown)'}\n"
        f"Collection workspace: {workspace.root_dir}\n"
        f"Collection work dir: {workspace.workspace_dir}\n"
        f"Collection scratch dir: {workspace.scratch_dir}\n"
        f"Document workspace: {document_workspace.root_dir}\n"
        f"Thread workspace: {thread_workspace.root_dir}\n"
        f"Document title: {document.title or '(unknown)'}\n"
        f"Document URL: {document.url or '(unknown)'}\n"
        f"Comment ID: {comment.id}\n"
        f"Parent comment ID: {comment.parentCommentId or '(none)'}\n\n"
        "Persisted document memory:\n"
        f"{document_memory_context or '(no prior document memory)'}\n\n"
        "Thread runtime state:\n"
        f"{thread_runtime_context or '(no thread runtime state)'}\n\n"
        f"{handoff_section}"
        "Document excerpt:\n"
        f"{document_excerpt}\n\n"
        f"{document_creation_section}"
        f"{document_update_section}"
        f"{tool_execution_section}"
        f"{memory_action_section}"
        f"{same_document_comment_section}"
        f"{related_documents_section}"
        "Relevant comment context:\n"
        f"{comment_context}\n\n"
        f"{current_comment_image_section}"
        "Current user comment:\n"
        f"{truncate(prompt_text, max_prompt_chars)}\n\n"
        f"{(reply_policy_text.strip() + ' ') if reply_policy_text else ''}"
        f"{document_creation_reply_instruction}"
        f"{document_update_reply_instruction}"
        "If a document creation outcome, document update outcome, tool execution outcome, "
        "memory action outcome, or same-document comment lookup outcome is provided, "
        "acknowledge it accurately and briefly. "
        "If cross-thread handoff context is provided, use it carefully: restate your "
        "understanding, ask a concise clarification if it is ambiguous, and do not "
        "pretend the referenced discussion is certain when it is not."
    )


def _format_prompt_packs(prompt_packs: list[PromptPack]) -> str | None:
    if not prompt_packs:
        return None
    blocks = [_format_prompt_section(f"Prompt pack: {pack.name}", pack.text) for pack in prompt_packs if pack.text]
    return "\n\n".join(blocks) if blocks else None


def _format_prompt_section(label: str, text: str) -> str:
    return f"[{label}]\n{text}"


@lru_cache(maxsize=32)
def _load_optional_text_file(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return text or None


def select_context_comments(
    comments: list[OutlineComment],
    current_comment: CommentModel,
    limit: int,
) -> list[OutlineComment]:
    current_root_id = current_comment.parentCommentId or current_comment.id
    related = [
        item
        for item in comments
        if item.id == current_root_id or item.parent_comment_id == current_root_id or item.id == current_comment.id
    ]

    if not any(item.id == current_comment.id for item in related):
        related.append(
            OutlineComment(
                id=current_comment.id,
                document_id=current_comment.documentId,
                parent_comment_id=current_comment.parentCommentId,
                created_by_id=current_comment.createdById,
                created_by_name=None,
                created_at=None,
                data=current_comment.data,
            )
        )

    if not related:
        related = comments[-limit:]

    related.sort(key=lambda item: (item.created_at or "", item.id))
    return related


def format_comment_context(
    *,
    thread_workspace: ThreadWorkspace,
    current_comment_id: str,
    max_full_thread_chars: int,
    tail_comment_count: int,
    summary_max_chars: int,
) -> str:
    return thread_workspace.build_comment_context(
        current_comment_id=current_comment_id,
        max_full_thread_chars=max_full_thread_chars,
        tail_comment_count=tail_comment_count,
        summary_max_chars=summary_max_chars,
    )


def strip_trigger_tokens(text: str, aliases: list[str], mentions: list[MentionRef]) -> str:
    result = text
    for mention in mentions:
        if mention.label:
            result = result.replace(mention.label, " ")
    for alias in aliases:
        escaped = re.escape(alias)
        result = re.sub(escaped, " ", result, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", result).strip()


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def preview(text: str, limit: int = 240) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def thread_root_id(comment: CommentModel) -> str:
    return comment.parentCommentId or comment.id


def comment_author_name(comment: CommentModel) -> str | None:
    created_by = getattr(comment, "createdBy", None)
    if isinstance(created_by, dict):
        name = created_by.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def comment_created_at(comment: CommentModel, envelope: WebhookEnvelope) -> str | None:
    created_at = getattr(comment, "createdAt", None)
    if isinstance(created_at, str) and created_at.strip():
        return created_at
    return envelope.createdAt if isinstance(envelope.createdAt, str) and envelope.createdAt.strip() else None


def created_comment_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None

    comment_id = payload.get("id")
    if isinstance(comment_id, str) and comment_id.strip():
        return comment_id

    data = payload.get("data")
    if isinstance(data, dict):
        nested_id = data.get("id")
        if isinstance(nested_id, str) and nested_id.strip():
            return nested_id
    return None
