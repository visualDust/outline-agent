from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from ..clients.model_client import ModelClient, ModelInputImage
from ..clients.outline_client import OutlineCollection, OutlineDocument
from ..core.config import AppSettings
from ..core.prompt_registry import PromptRegistry
from ..state.workspace import DocumentWorkspace, ThreadWorkspace
from ..utils.json_utils import JsonExtractionError, extract_json_object
from ..utils.markdown_sections import (
    MarkdownEditOperation,
    MarkdownOperationError,
    MarkdownSection,
    apply_markdown_operations,
    find_section,
    format_document_outline,
    normalize_markdown_text,
    parse_markdown_sections,
)

DOCUMENT_UPDATE_SYSTEM_PROMPT = """You decide whether to directly update the current Outline document for an agent.

Return `decision = \"edit\"` only when the latest user comment is explicitly asking
to modify the current document title or body, and you can safely apply the change
using the provided document outline and editable Markdown context.

Return strict JSON only with this schema:
{{
  "decision": "no-edit|edit|blocked",
  "reason": "short explanation",
  "title": "updated document title or null",
  "operations": [
    {{
      "op": "replace_section|insert_after_section|insert_before_section|append_document|replace_document",
      "target_section_id": "S2 or null",
      "new_markdown": "markdown block"
    }}
  ],
  "summary": "short summary of the change for the follow-up comment"
}}

Rules:
- `decision = \"no-edit\"` when the user is asking a question, requesting analysis,
  or not clearly asking for a direct document change
- `decision = \"blocked\"` when the request is too ambiguous or the available
  document context is insufficient for a safe direct edit
- If document markdown is unavailable, only use `replace_document` when the user is
  clearly asking you to write or rewrite the current document as a standalone whole;
  otherwise return `blocked`
- Prefer section operations over `replace_document`, especially for long documents
- Use `target_section_id` exactly as listed in the provided document outline
- `replace_section` replaces the entire target section, including nested subsections
- `insert_after_section` and `insert_before_section` require `target_section_id`
- `append_document` adds new Markdown at the end of the document
- `replace_document` is allowed only when the document is short or lacks useful section structure
- Keep operations minimal and focused; at most {max_operations} operations
"""


class DocumentUpdateOperationProposal(BaseModel):
    op: Literal[
        "replace_section",
        "insert_after_section",
        "insert_before_section",
        "append_document",
        "replace_document",
    ]
    target_section_id: str | None = None
    new_markdown: str | None = None


class DocumentUpdateProposal(BaseModel):
    decision: Literal["no-edit", "edit", "blocked"] = "no-edit"
    reason: str | None = None
    title: str | None = None
    operations: list[DocumentUpdateOperationProposal] = Field(default_factory=list)
    summary: str | None = None
    text: str | None = None
    operation_previews: list[str] = Field(default_factory=list)


class DocumentUpdateManager:
    def __init__(
        self,
        settings: AppSettings,
        model_client: ModelClient,
        *,
        prompt_registry: PromptRegistry | None = None,
    ):
        self.settings = settings
        self.model_client = model_client
        self.prompt_registry = prompt_registry or PromptRegistry.from_settings(settings)

    async def propose_update(
        self,
        *,
        document_workspace: DocumentWorkspace,
        thread_workspace: ThreadWorkspace,
        collection: OutlineCollection | None,
        document: OutlineDocument,
        user_comment: str,
        comment_context: str,
        related_documents_context: str | None,
        local_workspace_context: str | None,
        current_comment_image_count: int = 0,
        input_images: list[ModelInputImage] | None = None,
    ) -> DocumentUpdateProposal:
        document_text = normalize_markdown_text(document.text)
        sections = parse_markdown_sections(document_text or "")
        document_is_long = bool(document_text) and len(document_text) > self.settings.max_document_update_chars
        relevant_sections = _select_relevant_sections(
            sections,
            user_comment=user_comment,
            max_sections=self.settings.document_update_max_context_sections,
        )
        system_prompt = self.prompt_registry.compose_internal_prompt(
            DOCUMENT_UPDATE_SYSTEM_PROMPT.format(
                max_operations=self.settings.document_update_max_operations,
            ),
            "document_update_policy.md",
        )
        user_prompt = self._build_user_prompt(
            document_workspace=document_workspace,
            thread_workspace=thread_workspace,
            collection=collection,
            document=document,
            user_comment=user_comment,
            comment_context=comment_context,
            related_documents_context=related_documents_context,
            local_workspace_context=local_workspace_context,
            current_comment_image_count=current_comment_image_count,
            sections=sections,
            relevant_sections=relevant_sections,
            document_text=document_text,
            document_is_long=document_is_long,
        )
        raw = await self._generate_with_optional_images(
            system_prompt,
            user_prompt,
            input_images=input_images or [],
        )
        try:
            payload = extract_json_object(raw)
        except JsonExtractionError:
            return DocumentUpdateProposal(
                decision="no-edit",
                reason="model-output-not-json",
            )

        proposal = DocumentUpdateProposal.model_validate(payload)
        return self._sanitize(
            proposal,
            document=document,
            sections=sections,
            document_text=document_text,
            document_is_long=document_is_long,
        )

    def preview(self, proposal: DocumentUpdateProposal) -> str | None:
        if proposal.decision == "no-edit":
            return None
        if proposal.decision == "blocked":
            return f"blocked: {proposal.reason or 'document update could not be applied'}"

        parts: list[str] = []
        if proposal.title:
            parts.append(f"title={proposal.title}")
        if proposal.summary:
            parts.append(f"summary={proposal.summary}")
        if proposal.operation_previews:
            parts.append("ops=" + " | ".join(proposal.operation_previews))
        return " ; ".join(parts) if parts else "document update prepared"

    def format_reply_context(self, proposal: DocumentUpdateProposal, status: str) -> str | None:
        if proposal.decision == "no-edit":
            return None

        lines = [f"- status: {status}"]
        if proposal.summary:
            lines.append(f"- summary: {proposal.summary}")
        if proposal.reason:
            lines.append(f"- reason: {proposal.reason}")
        if proposal.title:
            lines.append(f"- updated title: {proposal.title}")
        if proposal.operation_previews:
            lines.append(f"- operations: {' ; '.join(proposal.operation_previews)}")
        return "\n".join(lines)

    def build_updated_document(
        self,
        document: OutlineDocument,
        proposal: DocumentUpdateProposal,
    ) -> OutlineDocument:
        if proposal.decision != "edit":
            return document
        return OutlineDocument(
            id=document.id,
            title=proposal.title or document.title,
            collection_id=document.collection_id,
            url=document.url,
            text=proposal.text if proposal.text is not None else document.text,
        )

    def _build_user_prompt(
        self,
        *,
        document_workspace: DocumentWorkspace,
        thread_workspace: ThreadWorkspace,
        collection: OutlineCollection | None,
        document: OutlineDocument,
        user_comment: str,
        comment_context: str,
        related_documents_context: str | None,
        local_workspace_context: str | None,
        current_comment_image_count: int,
        sections: list[MarkdownSection],
        relevant_sections: list[MarkdownSection],
        document_text: str | None,
        document_is_long: bool,
    ) -> str:
        collection_name = collection.name if collection and collection.name else document.collection_id or "(unknown)"
        document_memory = _truncate(
            document_workspace.load_prompt_context(self.settings.max_document_memory_chars),
            self.settings.max_document_memory_chars,
        )
        outline = format_document_outline(
            sections,
            max_sections=self.settings.document_update_outline_max_sections,
        )

        if document_text and not document_is_long:
            editable_context = (
                f"Full current document markdown:\n{_truncate(document_text, self.settings.max_document_update_chars)}"
            )
            context_mode = "full-document"
        else:
            editable_context = (
                "Relevant editable section markdown:\n"
                f"{_format_relevant_sections(relevant_sections, self.settings.document_update_section_max_chars)}"
            )
            context_mode = "sectioned"

        if not document_text:
            editable_context = "Document markdown is empty or unavailable."
            context_mode = "unavailable"

        related_documents_section = ""
        if related_documents_context:
            related_documents_section = f"Related documents in this collection:\n{related_documents_context}\n\n"
        local_workspace_section = ""
        if local_workspace_context:
            local_workspace_section = (
                "Reliable local workspace observations from attachment/file processing:\n"
                f"{_truncate(local_workspace_context, self.settings.max_prompt_chars)}\n\n"
            )
        current_comment_image_section = ""
        if current_comment_image_count > 0:
            noun = "image" if current_comment_image_count == 1 else "images"
            current_comment_image_section = (
                f"The latest user comment also includes {current_comment_image_count} embedded {noun}. "
                "If image inputs are attached to this request, use them when deciding document edits.\n\n"
            )

        return (
            f"Collection: {collection_name}\n"
            f"Collection ID: {document.collection_id or '(unknown)'}\n"
            f"Thread ID: {thread_workspace.thread_id}\n"
            f"Document ID: {document.id}\n"
            f"Current document title: {document.title or '(unknown)'}\n"
            f"Document context mode: {context_mode}\n\n"
            "Persisted document memory:\n"
            f"{document_memory or '(no document memory)'}\n\n"
            "Relevant comment context:\n"
            f"{comment_context or '(no comment context)'}\n\n"
            f"{current_comment_image_section}"
            f"{related_documents_section}"
            f"{local_workspace_section}"
            "Document outline (use section IDs exactly as listed):\n"
            f"{outline}\n\n"
            f"{editable_context}\n\n"
            "Latest user comment:\n"
            f"{_truncate(user_comment, self.settings.max_prompt_chars)}"
        )

    async def _generate_with_optional_images(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        input_images: list[ModelInputImage],
    ) -> str:
        if input_images:
            multimodal_generate = getattr(self.model_client, "generate_reply_with_images", None)
            if callable(multimodal_generate):
                return await multimodal_generate(system_prompt, user_prompt, input_images=input_images)
        return await self.model_client.generate_reply(system_prompt, user_prompt)

    def _sanitize(
        self,
        proposal: DocumentUpdateProposal,
        *,
        document: OutlineDocument,
        sections: list[MarkdownSection],
        document_text: str | None,
        document_is_long: bool,
    ) -> DocumentUpdateProposal:
        decision = proposal.decision
        reason = _normalize_short_text(proposal.reason)
        summary = _normalize_short_text(proposal.summary)
        title = _normalize_title(proposal.title)
        current_title = _normalize_title(document.title)
        if title and current_title and title == current_title:
            title = None

        if decision == "blocked":
            return DocumentUpdateProposal(
                decision="blocked",
                reason=reason or "The requested document edit was too ambiguous to apply safely.",
                summary=summary,
            )

        operations = self._sanitize_operations(proposal.operations)

        if decision == "edit" and operations and not document_text:
            if not _can_apply_without_current_document_text(operations):
                return DocumentUpdateProposal(
                    decision="blocked",
                    reason=(
                        "The current document body is empty or unavailable, so only a full-document "
                        "replacement can be applied safely."
                    ),
                    title=title,
                    summary=summary,
                )

        if decision == "edit" and document_is_long and any(op.op == "replace_document" for op in operations):
            return DocumentUpdateProposal(
                decision="blocked",
                reason="This document is too long for a safe full-document rewrite; use section-level edits instead.",
                title=title,
                summary=summary,
            )

        updated_text = document_text
        operation_previews: list[str] = []
        if operations:
            markdown_operations = [
                MarkdownEditOperation(
                    op=operation.op,
                    target_section_id=operation.target_section_id,
                    new_markdown=operation.new_markdown,
                )
                for operation in operations
            ]
            try:
                updated_text = apply_markdown_operations(document_text or "", markdown_operations)
                operation_previews = _build_operation_previews(operations, sections)
            except MarkdownOperationError as exc:
                return DocumentUpdateProposal(
                    decision="blocked",
                    reason=str(exc),
                    title=title,
                    summary=summary,
                )

        if updated_text == document_text:
            updated_text = None

        if decision == "edit" and not title and not updated_text:
            return DocumentUpdateProposal(decision="no-edit", reason=reason)

        if decision == "edit":
            return DocumentUpdateProposal(
                decision="edit",
                reason=reason,
                title=title,
                operations=operations,
                summary=summary or reason or "Updated the current Outline document.",
                text=updated_text,
                operation_previews=operation_previews,
            )

        return DocumentUpdateProposal(decision="no-edit", reason=reason)

    def _sanitize_operations(
        self,
        operations: list[DocumentUpdateOperationProposal],
    ) -> list[DocumentUpdateOperationProposal]:
        cleaned: list[DocumentUpdateOperationProposal] = []
        seen: set[tuple[str, str | None, str | None]] = set()
        for operation in operations:
            target_section_id = _normalize_section_id(operation.target_section_id)
            new_markdown = normalize_markdown_text(operation.new_markdown)
            if (
                operation.op
                in {
                    "replace_section",
                    "insert_after_section",
                    "insert_before_section",
                }
                and not target_section_id
            ):
                continue
            if (
                operation.op
                in {
                    "replace_section",
                    "insert_after_section",
                    "insert_before_section",
                    "append_document",
                    "replace_document",
                }
                and new_markdown is None
            ):
                continue

            key = (operation.op, target_section_id, new_markdown)
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(
                DocumentUpdateOperationProposal(
                    op=operation.op,
                    target_section_id=target_section_id,
                    new_markdown=new_markdown,
                )
            )
            if len(cleaned) >= self.settings.document_update_max_operations:
                break
        return cleaned


def _build_operation_previews(
    operations: list[DocumentUpdateOperationProposal],
    sections: list[MarkdownSection],
) -> list[str]:
    previews: list[str] = []
    for operation in operations:
        target_label = None
        if operation.target_section_id:
            try:
                target_label = find_section(sections, operation.target_section_id).label
            except MarkdownOperationError:
                target_label = operation.target_section_id
        if target_label:
            previews.append(f"{operation.op}[{operation.target_section_id}:{target_label}]")
        else:
            previews.append(operation.op)
    return previews


def _format_relevant_sections(sections: list[MarkdownSection], max_chars_per_section: int) -> str:
    if not sections:
        return "(no relevant sections selected)"

    blocks: list[str] = []
    for section in sections:
        blocks.append(f"[{section.section_id}] {section.label}\n{_truncate(section.markdown, max_chars_per_section)}")
    return "\n\n".join(blocks)


def _select_relevant_sections(
    sections: list[MarkdownSection],
    *,
    user_comment: str,
    max_sections: int,
) -> list[MarkdownSection]:
    if not sections:
        return []

    normalized_comment = user_comment.casefold()
    comment_tokens = _extract_tokens(normalized_comment)
    scored: list[tuple[int, int, MarkdownSection]] = []
    for index, section in enumerate(sections):
        label = section.label.casefold()
        score = 0
        if label and label in normalized_comment:
            score += 8

        heading_tokens = _extract_tokens(label)
        score += 2 * len(comment_tokens & heading_tokens)

        preview_tokens = _extract_tokens(section.preview.casefold())
        score += min(2, len(comment_tokens & preview_tokens))
        scored.append((score, -index, section))

    positive = [item for item in scored if item[0] > 0]
    if positive:
        positive.sort(reverse=True)
        return [item[2] for item in positive[:max_sections]]

    fallback: list[MarkdownSection] = sections[: min(2, len(sections))]
    if len(sections) > 2:
        fallback.append(sections[-1])
    deduped: list[MarkdownSection] = []
    seen: set[str] = set()
    for section in fallback:
        if section.section_id in seen:
            continue
        seen.add(section.section_id)
        deduped.append(section)
        if len(deduped) >= max_sections:
            break
    return deduped


def _extract_tokens(text: str) -> set[str]:
    tokens = {token for token in re.findall(r"[a-z0-9]{3,}", text)}
    tokens.update(re.findall(r"[\u4e00-\u9fff]{2,}", text))
    return tokens


def _normalize_section_id(text: str | None) -> str | None:
    if not text:
        return None
    compact = re.sub(r"\s+", "", text).upper()
    return compact or None


def _can_apply_without_current_document_text(
    operations: list[DocumentUpdateOperationProposal],
) -> bool:
    if len(operations) != 1:
        return False
    operation = operations[0]
    return operation.op == "replace_document" and bool(operation.new_markdown)


def _normalize_short_text(text: str | None) -> str | None:
    if not text:
        return None
    compact = re.sub(r"\s+", " ", text).strip()
    return compact or None


def _normalize_title(text: str | None) -> str | None:
    if not text:
        return None
    title = re.sub(r"\s+", " ", text).strip()
    return title or None


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"
