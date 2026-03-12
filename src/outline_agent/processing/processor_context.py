from __future__ import annotations

import mimetypes
from typing import Any

from ..clients.model_client import ModelClient, ModelInputImage
from ..clients.outline_client import (
    OutlineClient,
    OutlineClientError,
    OutlineCollection,
    OutlineComment,
    OutlineDocument,
)
from ..core.config import AppSettings
from ..core.logging import logger
from ..managers.action_router_manager import ActionRoutingDecision
from ..models.webhook_models import CommentModel
from ..state.workspace import CollectionWorkspace, CollectionWorkspaceManager, ThreadWorkspace
from ..utils.rich_text import MentionRef, extract_prompt_text
from .processor_detection import (
    select_cross_thread_candidates as _select_cross_thread_candidates,
)
from .processor_prompting import (
    truncate as _truncate,
)
from .processor_types import CrossThreadHandoff


def format_action_route_preview(action_route: ActionRoutingDecision | None) -> str | None:
    if action_route is None:
        return None

    enabled: list[str] = []
    if action_route.memory_action:
        enabled.append("memory_action")
    if action_route.cross_thread_handoff:
        enabled.append("cross_thread_handoff")
    if action_route.same_document_comment_lookup:
        enabled.append("same_document_comment_lookup")

    status = ", ".join(enabled) if enabled else "none"
    reason = (action_route.reason or "").strip()
    return f"enabled={status}" + (f" ; reason={reason}" if reason else "")


async def generate_reply_with_optional_images(
    *,
    model_client: ModelClient,
    system_prompt: str,
    user_prompt: str,
    comment_id: str,
    image_inputs: list[ModelInputImage],
) -> str:
    if not image_inputs:
        return await model_client.generate_reply(system_prompt, user_prompt)

    multimodal_generate = getattr(model_client, "generate_reply_with_images", None)
    if not callable(multimodal_generate):
        return await model_client.generate_reply(system_prompt, user_prompt)
    try:
        return await multimodal_generate(system_prompt, user_prompt, input_images=image_inputs)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Multimodal reply generation failed for comment {}. Falling back to text-only reply: {}",
            comment_id,
            exc,
        )
        return await model_client.generate_reply(system_prompt, user_prompt)


async def prepare_comment_image_inputs(
    *,
    outline_client: OutlineClient,
    workspace: CollectionWorkspace,
    thread_workspace: ThreadWorkspace,
    comment_id: str,
    image_sources: list[str],
) -> list[ModelInputImage]:
    image_inputs: list[ModelInputImage] = []
    del thread_workspace
    image_dir = workspace.workspace_dir / "comment_images"
    image_dir.mkdir(parents=True, exist_ok=True)

    for index, source in enumerate(image_sources[:4], start=1):
        target_path = image_dir / f"{comment_id}-{index}.img"
        try:
            result = await outline_client.download_attachment(source, target_path)
        except OutlineClientError as exc:
            logger.warning("Failed to download comment image {} for {}: {}", source, comment_id, exc)
            continue

        try:
            data = target_path.read_bytes()
        except OSError as exc:
            logger.warning("Failed to read downloaded comment image {} for {}: {}", target_path, comment_id, exc)
            continue

        media_type = resolve_comment_image_media_type(
            data=data,
            target_path=target_path,
            content_type=result.get("content_type"),
        )
        if not media_type.startswith("image/"):
            logger.warning(
                "Skipping comment image {} for {} because downloaded MIME type is not an image: {}",
                source,
                comment_id,
                media_type,
            )
            continue
        image_inputs.append(ModelInputImage(data=data, media_type=media_type))

    return image_inputs


def resolve_comment_image_media_type(*, data: bytes, target_path: Any, content_type: Any) -> str:
    if isinstance(content_type, str):
        normalized = content_type.split(";", 1)[0].strip()
        if normalized.startswith("image/"):
            return normalized
    sniffed = sniff_image_media_type(data)
    if sniffed:
        return sniffed
    guessed, _ = mimetypes.guess_type(str(target_path))
    return guessed or "application/octet-stream"


def sniff_image_media_type(data: bytes) -> str | None:
    if len(data) >= 8 and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 3 and data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 6 and (data.startswith(b"GIF87a") or data.startswith(b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 2 and data.startswith(b"BM"):
        return "image/bmp"
    return None


def detect_direct_trigger(
    *,
    settings: AppSettings,
    comment_text: str,
    mentions: list[MentionRef],
    agent_user_id: str | None,
) -> str | None:
    if settings.trigger_mode == "all":
        return "*"

    if agent_user_id:
        for mention in mentions:
            if mention.model_id == agent_user_id:
                return mention.label or agent_user_id

    if not settings.mention_alias_fallback_enabled:
        return None

    lowered = comment_text.lower()
    for alias in settings.mention_aliases:
        if alias.lower() in lowered:
            return alias
    return None


def detect_reply_trigger(
    *,
    settings: AppSettings,
    current_comment: CommentModel,
    comments: list[OutlineComment],
    agent_user_id: str | None,
) -> str | None:
    if not (settings.trigger_on_reply_to_agent and agent_user_id and current_comment.parentCommentId):
        return None

    thread_root_id = current_comment.parentCommentId
    related_comments = [
        item
        for item in comments
        if item.id != current_comment.id and (item.id == thread_root_id or item.parent_comment_id == thread_root_id)
    ]
    if any(item.created_by_id == agent_user_id for item in related_comments):
        return "reply-to-agent"
    return None


def archive_context_comments(
    *,
    settings: AppSettings,
    thread_workspace: ThreadWorkspace,
    context_comments: list[OutlineComment],
    document: OutlineDocument,
) -> None:
    for item in context_comments:
        body = extract_prompt_text(item.data)
        if not body.strip():
            continue
        thread_workspace.record_observed_comment(
            comment_id=item.id,
            author_id=item.created_by_id,
            author_name=item.created_by_name,
            comment_text=body,
            created_at=item.created_at,
            parent_comment_id=item.parent_comment_id,
            document_id=document.id,
            document_title=document.title,
            max_recent_comments=settings.thread_recent_comments,
            max_comment_chars=settings.thread_comment_max_chars,
        )


def resolve_cross_thread_handoff(
    *,
    settings: AppSettings,
    workspace_manager: CollectionWorkspaceManager,
    workspace: CollectionWorkspace,
    thread_workspace: ThreadWorkspace,
    document: OutlineDocument,
    prompt_text: str,
    context_comments: list[OutlineComment],
) -> CrossThreadHandoff | None:
    if len(context_comments) > 1:
        return None

    candidates = workspace_manager.list_document_thread_entries(
        workspace,
        document_id=document.id,
        exclude_thread_id=thread_workspace.thread_id,
    )
    logger.debug(
        "Cross-thread handoff candidates resolved: document_id={}, current_thread_id={}, candidate_count={}",
        document.id,
        thread_workspace.thread_id,
        len(candidates),
    )
    if not candidates:
        return None

    selected, alternatives = _select_cross_thread_candidates(
        prompt_text,
        candidates,
        limit=settings.cross_thread_handoff_candidate_limit,
    )
    logger.debug(
        "Cross-thread handoff selection: document_id={}, current_thread_id={}, selected_thread_id={}, alternative_count={}",
        document.id,
        thread_workspace.thread_id,
        selected.get("thread_id") if selected else None,
        len(alternatives),
    )
    if selected is not None:
        participants = selected.get("participants") or []
        preview = selected.get("session_summary") or selected.get("recent_preview") or "(no prior summary available)"
        prompt_section = (
            "The current comment appears to refer to a different discussion thread in this same document.\n"
            "Most likely referenced thread:\n"
            f"- thread_id: {selected.get('thread_id')}\n"
            f"- participants: {', '.join(participants) if participants else '(unknown)'}\n"
            f"- prior discussion summary: {preview}\n"
            "Instruction: first restate your understanding of that earlier discussion. "
            "If any detail is uncertain, ask for confirmation instead of assuming. "
            "Do not directly perform document edits or local tool actions in this turn."
        )
        return CrossThreadHandoff(
            mode="resolved",
            preview=f"selected {selected.get('thread_id')}: {_truncate(str(preview), 240)}",
            prompt_section=prompt_section,
        )

    lines = [
        (
            "The current comment appears to refer to a different discussion thread in "
            "this same document, but multiple candidates exist."
        ),
        "Possible referenced threads:",
    ]
    preview_parts: list[str] = []
    for index, item in enumerate(alternatives, start=1):
        participants = item.get("participants") or []
        preview = item.get("session_summary") or item.get("recent_preview") or "(no prior summary available)"
        lines.append(
            (
                f"{index}. thread_id={item.get('thread_id')} | "
                f"participants={', '.join(participants) if participants else '(unknown)'} | "
                f"summary={preview}"
            )
        )
        preview_parts.append(f"{item.get('thread_id')}: {_truncate(str(preview), 120)}")
    lines.append(
        "Instruction: ask the user which prior discussion they want you to use, "
        "or ask them to @mention you inside that thread. Do not perform document "
        "edits or local tool actions in this turn."
    )
    return CrossThreadHandoff(
        mode="ambiguous",
        preview=" ; ".join(preview_parts),
        prompt_section="\n".join(lines),
    )


async def resolve_collection(
    *,
    outline_client: OutlineClient,
    document: OutlineDocument,
) -> OutlineCollection | None:
    if not document.collection_id:
        return None
    try:
        return await outline_client.collection_info(document.collection_id)
    except OutlineClientError as exc:
        logger.warning(
            "Falling back without collection metadata for collection {}: {}",
            document.collection_id,
            exc,
        )
        return None
