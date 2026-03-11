from __future__ import annotations

import re
from dataclasses import dataclass

from ..clients.outline_client import OutlineClient, OutlineClientError, OutlineDocument
from ..core.config import AppSettings
from ..core.logging import logger


@dataclass(frozen=True)
class RelatedDocumentSnippet:
    document_id: str
    title: str | None
    url: str | None
    excerpt: str | None


@dataclass(frozen=True)
class RelatedDocumentsContext:
    documents: list[RelatedDocumentSnippet]
    prompt_section: str | None
    preview: str | None


class RelatedDocumentManager:
    def __init__(self, settings: AppSettings, outline_client: OutlineClient) -> None:
        self.settings = settings
        self.outline_client = outline_client

    async def fetch_context(
        self,
        *,
        document: OutlineDocument,
        prompt_text: str,
    ) -> RelatedDocumentsContext:
        if not self.settings.related_documents_enabled:
            return RelatedDocumentsContext(documents=[], prompt_section=None, preview=None)

        collection_id = document.collection_id
        if not collection_id:
            return RelatedDocumentsContext(documents=[], prompt_section=None, preview=None)

        query = _build_search_query(
            prompt_text=prompt_text,
            document_title=document.title,
            min_chars=self.settings.related_document_min_query_chars,
            max_chars=self.settings.related_document_query_max_chars,
        )
        if not query:
            return RelatedDocumentsContext(documents=[], prompt_section=None, preview=None)

        try:
            results = await self.outline_client.documents_search(
                query=query,
                collection_id=collection_id,
                limit=self.settings.related_document_search_limit,
            )
        except OutlineClientError as exc:
            logger.warning("Related document search failed: {}", exc)
            return RelatedDocumentsContext(documents=[], prompt_section=None, preview=None)

        snippets: list[RelatedDocumentSnippet] = []
        seen_ids: set[str] = {document.id}

        for item in results:
            if item.id in seen_ids:
                continue
            seen_ids.add(item.id)

            resolved = item
            if resolved.text is None:
                try:
                    resolved = await self.outline_client.document_info(item.id)
                except OutlineClientError as exc:
                    logger.warning("Related document fetch failed for {}: {}", item.id, exc)
                    continue

            excerpt = _build_excerpt(
                resolved.text,
                max_chars=self.settings.related_document_excerpt_chars,
            )
            snippets.append(
                RelatedDocumentSnippet(
                    document_id=resolved.id,
                    title=resolved.title,
                    url=resolved.url,
                    excerpt=excerpt,
                )
            )

            if len(snippets) >= self.settings.related_document_limit:
                break

        prompt_section = _format_prompt_section(snippets)
        preview = _format_preview(snippets)
        return RelatedDocumentsContext(documents=snippets, prompt_section=prompt_section, preview=preview)


def _build_search_query(
    *,
    prompt_text: str,
    document_title: str | None,
    min_chars: int,
    max_chars: int,
) -> str | None:
    cleaned = re.sub(r"\s+", " ", (prompt_text or "")).strip()
    if len(cleaned) < min_chars:
        cleaned = re.sub(r"\s+", " ", (document_title or "")).strip()
    if len(cleaned) < min_chars:
        return None
    return cleaned[:max_chars].rstrip()


def _build_excerpt(text: str | None, max_chars: int) -> str | None:
    if not text:
        return None
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= max_chars:
        return compact
    return compact[: max(0, max_chars - 1)].rstrip() + "…"


def _format_prompt_section(snippets: list[RelatedDocumentSnippet]) -> str | None:
    if not snippets:
        return None
    lines: list[str] = []
    for index, doc in enumerate(snippets, start=1):
        title = doc.title or "(untitled)"
        lines.append(f"{index}. {title}")
        lines.append(f"   id: {doc.document_id}")
        if doc.url:
            lines.append(f"   url: {doc.url}")
        if doc.excerpt:
            lines.append(f"   excerpt: {doc.excerpt}")
    return "\n".join(lines)


def _format_preview(snippets: list[RelatedDocumentSnippet]) -> str | None:
    if not snippets:
        return None
    titles = [doc.title or doc.document_id for doc in snippets]
    return "; ".join(title for title in titles if title)
