from __future__ import annotations

import json
from typing import Any

from ..clients.outline_models import OutlineComment
from ..utils.rich_text import extract_prompt_text
from .thread_state import truncate_text


def build_thread_transcript(
    *,
    thread_id: str,
    document_id: str,
    document_title: str | None,
    comments: list[OutlineComment],
) -> dict[str, Any]:
    ordered = sorted(comments, key=lambda item: (item.created_at or "", item.id))
    return {
        "thread_id": thread_id,
        "root_comment_id": thread_id,
        "document_id": document_id,
        "document_title": document_title,
        "deleted": False,
        "comments": [
            {
                "id": item.id,
                "parent_comment_id": item.parent_comment_id,
                "author_id": item.created_by_id,
                "author_name": item.created_by_name,
                "created_at": item.created_at,
                "updated_at": None,
                "deleted_at": None,
                "body_rich": item.data,
                "body_plain": extract_prompt_text(item.data),
            }
            for item in ordered
        ],
    }



def build_deleted_thread_transcript(
    *,
    thread_id: str,
    document_id: str,
    document_title: str | None,
) -> dict[str, Any]:
    return {
        "thread_id": thread_id,
        "root_comment_id": thread_id,
        "document_id": document_id,
        "document_title": document_title,
        "deleted": True,
        "comments": [],
    }



def active_comments(transcript: dict[str, Any]) -> list[dict[str, Any]]:
    value = transcript.get("comments")
    if not isinstance(value, list):
        return []

    comments: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        if item.get("deleted_at"):
            continue
        comment_id = item.get("id")
        if not isinstance(comment_id, str) or not comment_id.strip():
            continue
        comments.append(item)
    comments.sort(key=lambda item: (_as_optional_str(item.get("created_at")) or "", item.get("id") or ""))
    return comments



def transcript_comment_count(transcript: dict[str, Any]) -> int:
    return len(active_comments(transcript))



def transcript_root_exists(transcript: dict[str, Any]) -> bool:
    root_comment_id = transcript.get("root_comment_id")
    comments = active_comments(transcript)
    if not comments:
        return False
    if isinstance(root_comment_id, str) and root_comment_id:
        if any(item.get("id") == root_comment_id for item in comments):
            return True
    return any(item.get("parent_comment_id") in (None, "") for item in comments)



def render_comments_for_prompt(
    *,
    comments: list[dict[str, Any]],
    current_comment_id: str,
) -> str:
    if not comments:
        return "(no additional comment context)"

    lines: list[str] = []
    for item in comments:
        comment_id = _as_optional_str(item.get("id")) or "(unknown-comment)"
        parent_comment_id = _as_optional_str(item.get("parent_comment_id"))
        label = "current" if comment_id == current_comment_id else ("reply" if parent_comment_id else "top-level")
        author = _as_optional_str(item.get("author_name")) or _as_optional_str(item.get("author_id")) or "unknown"
        created_at = _as_optional_str(item.get("created_at"))
        body = _as_optional_str(item.get("body_plain")) or "(empty comment)"
        line = f"- [{label}] {author}"
        if created_at:
            line += f" @ {created_at}"
        line += f": {body}"
        lines.append(line)
    return "\n".join(lines)



def summarize_comments_for_prompt(comments: list[dict[str, Any]], *, max_chars: int) -> str:
    if not comments:
        return ""
    bullets: list[str] = []
    for item in comments:
        author = _as_optional_str(item.get("author_name")) or _as_optional_str(item.get("author_id")) or "unknown"
        body = truncate_text(_as_optional_str(item.get("body_plain")) or "(empty comment)", 180)
        bullets.append(f"- {author}: {body}")
        if len("\n".join(bullets)) >= max_chars:
            break
    text = "\n".join(bullets).strip()
    if len(text) <= max_chars:
        return text
    return truncate_text(text, max_chars)



def to_json_text(transcript: dict[str, Any]) -> str:
    return json.dumps(transcript, ensure_ascii=False, indent=2) + "\n"



def load_json_text(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}



def _as_optional_str(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
