from __future__ import annotations

import re
from typing import Any

HANDOFF_STOPWORDS = {
    "a",
    "an",
    "and",
    "answer",
    "based",
    "comment",
    "continue",
    "conversation",
    "discussion",
    "earlier",
    "follow",
    "from",
    "in",
    "it",
    "of",
    "on",
    "other",
    "previous",
    "prior",
    "refer",
    "resume",
    "same",
    "that",
    "the",
    "thread",
    "to",
    "up",
    "we",
    "what",
    "you",
    "之前",
    "另外",
    "继续",
    "参考",
    "讨论",
    "线程",
    "评论串",
}


def select_cross_thread_candidates(
    prompt_text: str,
    candidates: list[dict[str, Any]],
    *,
    limit: int,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if not candidates:
        return None, []
    if len(candidates) == 1:
        return candidates[0], candidates[:1]

    scored: list[tuple[int, dict[str, Any]]] = []
    query_terms = extract_handoff_query_terms(prompt_text)
    lowered_prompt = prompt_text.lower()

    for index, candidate in enumerate(candidates):
        search_text = str(candidate.get("search_text") or "")
        lowered_search = search_text.lower()
        score = 0

        for term in query_terms:
            if term in lowered_search:
                score += 3 if len(term) > 4 else 1

        document_title = candidate.get("document_title")
        if isinstance(document_title, str) and document_title.strip() and document_title.lower() in lowered_prompt:
            score += 2

        participants = candidate.get("participants")
        if isinstance(participants, list):
            for participant in participants:
                if not isinstance(participant, str):
                    continue
                normalized = participant.strip().lower()
                if normalized and normalized in lowered_prompt:
                    score += 2

        last_comment_at = candidate.get("last_comment_at")
        if isinstance(last_comment_at, str) and last_comment_at:
            score += 1

        score += max(0, limit - index) // max(1, limit)
        scored.append((score, candidate))

    scored.sort(
        key=lambda item: (
            item[0],
            str(item[1].get("last_comment_at") or ""),
        ),
        reverse=True,
    )

    top_candidates = [item for _, item in scored[: max(1, limit)]]
    top_score = scored[0][0]
    second_score = scored[1][0] if len(scored) > 1 else -1

    if top_score > 0 and (top_score >= second_score + 2 or second_score <= 0):
        return scored[0][1], top_candidates
    return None, top_candidates


def extract_handoff_query_terms(text: str) -> list[str]:
    lowered = text.lower()
    ascii_terms = re.findall(r"[a-z0-9][a-z0-9_-]+", lowered)
    cjk_terms = re.findall(r"[\u4e00-\u9fff]{2,}", text)
    deduped: list[str] = []
    for term in ascii_terms + cjk_terms:
        normalized = term.strip().lower()
        if not normalized or normalized in HANDOFF_STOPWORDS:
            continue
        if normalized not in deduped:
            deduped.append(normalized)
    return deduped
