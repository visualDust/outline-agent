from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from ..clients.outline_client import OutlineClient, OutlineClientError, OutlineComment, OutlineDocument
from ..core.config import AppSettings
from ..core.logging import logger
from ..models.webhook_models import CommentModel
from ..processing.processor_detection import (
    extract_handoff_query_terms,
    select_cross_thread_candidates,
)
from ..state.workspace import CollectionWorkspace, CollectionWorkspaceManager
from ..utils.rich_text import extract_prompt_text


@dataclass(frozen=True)
class SameDocumentCommentContext:
    mode: str
    prompt_section: str | None
    preview: str | None


class SameDocumentCommentManager:
    def __init__(
        self,
        settings: AppSettings,
        outline_client: OutlineClient,
        workspace_manager: CollectionWorkspaceManager,
    ) -> None:
        self.settings = settings
        self.outline_client = outline_client
        self.workspace_manager = workspace_manager

    async def fetch_context(
        self,
        *,
        workspace: CollectionWorkspace,
        document: OutlineDocument,
        current_comment: CommentModel,
        prompt_text: str,
    ) -> SameDocumentCommentContext:
        if not self.settings.same_document_comment_lookup_enabled:
            return SameDocumentCommentContext(mode="disabled", prompt_section=None, preview=None)

        try:
            comments = await self._fetch_document_comments(document.id)
        except OutlineClientError as exc:
            logger.warning("Same-document comment lookup failed for {}: {}", document.id, exc)
            return SameDocumentCommentContext(
                mode="error",
                prompt_section=(
                    "I attempted to inspect other comment threads in this same document because the user asked, "
                    f"but the retrieval failed: {exc}."
                ),
                preview=f"lookup error: {exc}",
            )

        current_thread_id = current_comment.parentCommentId or current_comment.id
        candidates = self._build_thread_candidates(
            workspace=workspace,
            document=document,
            current_thread_id=current_thread_id,
            comments=comments,
        )
        if not candidates:
            return SameDocumentCommentContext(
                mode="no-results",
                prompt_section=(
                    "I inspected other comment threads in this same document because the user explicitly asked, "
                    "but I did not find any other comment threads besides the current one."
                ),
                preview="no other same-document comment threads found",
            )

        if len(candidates) > 1 and not _has_specific_lookup_terms(prompt_text):
            top_candidates = candidates[: max(1, self.settings.same_document_comment_lookup_thread_limit)]
            return SameDocumentCommentContext(
                mode="ambiguous",
                prompt_section=self._format_ambiguous_prompt_section(top_candidates),
                preview=(
                    "ambiguous same-document threads: "
                    + "; ".join(str(item.get("thread_id") or "(unknown)") for item in top_candidates)
                ),
            )

        specific_selection, ranked_candidates = _select_specific_lookup_candidate(
            prompt_text,
            candidates,
            limit=self.settings.same_document_comment_lookup_thread_limit,
        )
        if specific_selection is not None:
            return SameDocumentCommentContext(
                mode="resolved",
                prompt_section=self._format_resolved_prompt_section(
                    selected=specific_selection,
                    alternatives=ranked_candidates,
                ),
                preview=(
                    f"selected {specific_selection.get('thread_id')}: "
                    f"{specific_selection.get('preview') or '(no preview)'}"
                ),
            )

        selected, alternatives = select_cross_thread_candidates(
            prompt_text,
            candidates,
            limit=self.settings.same_document_comment_lookup_thread_limit,
        )
        if selected is not None:
            return SameDocumentCommentContext(
                mode="resolved",
                prompt_section=self._format_resolved_prompt_section(selected=selected, alternatives=alternatives),
                preview=f"selected {selected.get('thread_id')}: {selected.get('preview') or '(no preview)'}",
            )

        return SameDocumentCommentContext(
            mode="ambiguous",
            prompt_section=self._format_ambiguous_prompt_section(alternatives),
            preview=(
                "ambiguous same-document threads: "
                + "; ".join(str(item.get("thread_id") or "(unknown)") for item in alternatives)
            ),
        )

    async def _fetch_document_comments(self, document_id: str) -> list[OutlineComment]:
        fetch_limit = max(1, self.settings.same_document_comment_lookup_fetch_limit)
        page_size = max(1, min(self.settings.comment_list_limit, fetch_limit))
        offset = 0
        items: list[OutlineComment] = []

        while len(items) < fetch_limit:
            remaining = fetch_limit - len(items)
            batch = await self.outline_client.comments_list(
                document_id,
                limit=min(page_size, remaining),
                offset=offset,
            )
            if not batch:
                break
            items.extend(batch)
            if len(batch) < min(page_size, remaining):
                break
            offset += len(batch)

        return items[:fetch_limit]

    def _build_thread_candidates(
        self,
        *,
        workspace: CollectionWorkspace,
        document: OutlineDocument,
        current_thread_id: str,
        comments: list[OutlineComment],
    ) -> list[dict[str, Any]]:
        grouped: dict[str, list[OutlineComment]] = {}
        for item in comments:
            thread_id = item.parent_comment_id or item.id
            if thread_id == current_thread_id:
                continue
            grouped.setdefault(thread_id, []).append(item)

        local_entries = {
            str(entry.get("thread_id") or ""): entry
            for entry in self.workspace_manager.list_document_thread_entries(
                workspace,
                document_id=document.id,
                exclude_thread_id=current_thread_id,
            )
        }

        candidates: list[dict[str, Any]] = []
        for thread_id, thread_comments in grouped.items():
            thread_comments.sort(key=lambda item: (item.created_at or "", item.id))
            if not thread_comments:
                continue

            local_entry = local_entries.get(thread_id, {})
            participants = _collect_participants(thread_comments, local_entry)
            summary = _as_optional_str(local_entry.get("session_summary"))
            recent_preview = _as_optional_str(local_entry.get("recent_preview"))
            preview = summary or recent_preview or _build_thread_preview(
                thread_comments,
                excerpt_chars=self.settings.same_document_comment_lookup_excerpt_chars,
            )
            search_parts = [preview or ""]
            if summary:
                search_parts.append(summary)
            for comment in thread_comments:
                body = extract_prompt_text(comment.data)
                if body:
                    search_parts.append(body)
            search_parts.extend(participants)

            candidates.append(
                {
                    "thread_id": thread_id,
                    "document_id": document.id,
                    "document_title": document.title,
                    "participants": participants,
                    "comment_count": len(thread_comments),
                    "last_comment_at": thread_comments[-1].created_at,
                    "session_summary": summary,
                    "recent_preview": recent_preview,
                    "preview": preview,
                    "thread_comments": thread_comments,
                    "search_text": "\n".join(part for part in search_parts if part),
                }
            )

        candidates.sort(key=lambda item: str(item.get("last_comment_at") or ""), reverse=True)
        return candidates

    def _format_resolved_prompt_section(
        self,
        *,
        selected: dict[str, Any],
        alternatives: list[dict[str, Any]],
    ) -> str:
        lines = [
            "I inspected other comment threads in this same document because the user explicitly asked about earlier or other comments.",
            "Most relevant matching thread:",
            f"- thread_id: {selected.get('thread_id')}",
            f"- participants: {_format_participants(selected.get('participants'))}",
            f"- comment_count: {selected.get('comment_count') or 0}",
        ]
        last_comment_at = _as_optional_str(selected.get("last_comment_at"))
        if last_comment_at:
            lines.append(f"- last_comment_at: {last_comment_at}")

        summary = _as_optional_str(selected.get("session_summary"))
        preview = _as_optional_str(selected.get("preview"))
        if summary:
            lines.append(f"- local_summary: {summary}")
        elif preview:
            lines.append(f"- preview: {preview}")

        lines.append("Recent comments from that other thread:")
        recent_comments = selected.get("thread_comments")
        if isinstance(recent_comments, list):
            for item in recent_comments[-self.settings.same_document_comment_lookup_comment_limit :]:
                if not isinstance(item, OutlineComment):
                    continue
                body = _truncate(extract_prompt_text(item.data), self.settings.same_document_comment_lookup_excerpt_chars)
                if not body:
                    continue
                author = item.created_by_name or item.created_by_id or "unknown"
                lines.append(f"- {author}: {body}")

        other_candidates = [item for item in alternatives if item.get("thread_id") != selected.get("thread_id")]
        if other_candidates:
            lines.append("Other candidate same-document threads:")
            for item in other_candidates:
                lines.append(
                    "- "
                    + _format_candidate_line(
                        item,
                        excerpt_chars=self.settings.same_document_comment_lookup_excerpt_chars,
                    )
                )

        lines.append(
            "Instruction: use this retrieved same-document comment context when it helps answer the user, but if their reference could still mean a different thread, say so briefly instead of overclaiming."
        )
        return "\n".join(lines)

    def _format_ambiguous_prompt_section(self, alternatives: list[dict[str, Any]]) -> str:
        lines = [
            "I inspected other comment threads in this same document because the user explicitly asked about earlier or other comments.",
            "Multiple candidate same-document threads may match:",
        ]
        for index, item in enumerate(alternatives, start=1):
            lines.append(
                f"{index}. "
                + _format_candidate_line(
                    item,
                    excerpt_chars=self.settings.same_document_comment_lookup_excerpt_chars,
                )
            )

        lines.append(
            "Instruction: explain that you can inspect earlier same-document comments, briefly summarize these candidates, and ask the user which thread or topic they mean before relying on one of them."
        )
        return "\n".join(lines)


def _collect_participants(thread_comments: list[OutlineComment], local_entry: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for item in thread_comments:
        label = item.created_by_name or item.created_by_id
        if label and label not in labels:
            labels.append(label)

    raw_local = local_entry.get("participants")
    if isinstance(raw_local, list):
        for item in raw_local:
            if isinstance(item, str) and item.strip() and item not in labels:
                labels.append(item)
    return labels


def _build_thread_preview(thread_comments: list[OutlineComment], *, excerpt_chars: int) -> str | None:
    snippets: list[str] = []
    for item in thread_comments[-3:]:
        body = _truncate(extract_prompt_text(item.data), excerpt_chars)
        if not body:
            continue
        snippets.append(body)
    if not snippets:
        return None
    return " | ".join(snippets)


def _format_participants(value: Any) -> str:
    if not isinstance(value, list):
        return "(unknown)"
    participants = [item for item in value if isinstance(item, str) and item.strip()]
    return ", ".join(participants) if participants else "(unknown)"


def _format_candidate_line(item: dict[str, Any], *, excerpt_chars: int) -> str:
    thread_id = _as_optional_str(item.get("thread_id")) or "(unknown-thread)"
    participants = _format_participants(item.get("participants"))
    comment_count = item.get("comment_count") if isinstance(item.get("comment_count"), int) else 0
    last_comment_at = _as_optional_str(item.get("last_comment_at"))
    preview = _as_optional_str(item.get("preview")) or "(no preview)"
    preview = _truncate(preview, excerpt_chars)

    parts = [f"thread_id={thread_id}", f"participants={participants}", f"comment_count={comment_count}"]
    if last_comment_at:
        parts.append(f"last_comment_at={last_comment_at}")
    parts.append(f"preview={preview}")
    return " ; ".join(parts)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _as_optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


GENERIC_LOOKUP_TERMS = {
    "another",
    "comment",
    "comments",
    "discussion",
    "document",
    "earlier",
    "find",
    "inspect",
    "look",
    "older",
    "other",
    "previous",
    "prior",
    "read",
    "reply",
    "replies",
    "review",
    "same",
    "search",
    "see",
    "this",
    "thread",
    "文档",
    "之前",
    "以前",
    "其他",
    "别的",
    "另一个",
    "评论",
    "回复",
    "讨论",
    "线程",
}


def _has_specific_lookup_terms(prompt_text: str) -> bool:
    return bool(_extract_specific_lookup_terms(prompt_text))


LOOKUP_NOISE_TERMS = {
    "about",
    "can",
    "help",
    "mean",
    "please",
    "show",
    "summaries",
    "summarize",
    "summary",
    "tell",
    "them",
    "those",
    "what",
    "which",
}


def _extract_specific_lookup_terms(prompt_text: str) -> list[str]:
    terms = extract_handoff_query_terms(prompt_text)
    return [term for term in terms if term not in GENERIC_LOOKUP_TERMS and term not in LOOKUP_NOISE_TERMS]


def _select_specific_lookup_candidate(
    prompt_text: str,
    candidates: list[dict[str, Any]],
    *,
    limit: int,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    specific_terms = _extract_specific_lookup_terms(prompt_text)
    if not specific_terms or not candidates:
        return None, []

    scored: list[tuple[int, int, int, dict[str, Any]]] = []
    for candidate in candidates:
        search_text = str(candidate.get("search_text") or "")
        preview = str(candidate.get("preview") or "")
        matched_terms: list[str] = []
        score = 0
        for term in specific_terms:
            if not _term_matches_text(term, search_text):
                continue
            matched_terms.append(term)
            score += 4 if _term_matches_text(term, preview) else 3
            if len(term) >= 5:
                score += 1

        scored.append(
            (
                score,
                len(matched_terms),
                max((len(term) for term in matched_terms), default=0),
                candidate,
            )
        )

    scored.sort(
        key=lambda item: (
            item[0],
            item[1],
            item[2],
            str(item[3].get("last_comment_at") or ""),
        ),
        reverse=True,
    )

    top_candidates = [item[3] for item in scored[: max(1, limit)]]
    top_score, top_match_count, _, top_candidate = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else -1
    second_match_count = scored[1][1] if len(scored) > 1 else -1

    if top_score > 0 and (top_score > second_score or top_match_count > second_match_count):
        return top_candidate, top_candidates
    return None, top_candidates


def _term_matches_text(term: str, text: str) -> bool:
    if not term or not text:
        return False
    if all("\u4e00" <= char <= "\u9fff" for char in term):
        return term in text
    return bool(re.search(rf"\b{re.escape(term)}\b", text, re.IGNORECASE))
