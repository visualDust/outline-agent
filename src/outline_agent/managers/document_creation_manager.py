from __future__ import annotations

from pydantic import BaseModel

from ..clients.model_client import ModelClient, ModelInputImage
from ..clients.outline_client import OutlineCollection, OutlineDocument
from ..core.config import AppSettings
from ..state.workspace import ThreadWorkspace
from ..utils.json_utils import JsonExtractionError, extract_json_object

DOCUMENT_CREATION_SYSTEM_PROMPT = """You decide whether to create a new Outline document for an agent.

Return `decision = "create"` only when the latest user comment is explicitly asking
for a separate new document to be created in the current collection.

Return strict JSON only with this schema:
{
  "decision": "no-create|create|blocked",
  "reason": "short explanation",
  "title": "new document title or null",
  "text": "full markdown body for the new document or null",
  "summary": "short summary of what was created for the follow-up comment"
}

Rules:
- `decision = "no-create"` when the user is only asking to edit the current
  document, asking a question, or replying conversationally
- `decision = "blocked"` when the user seems to want a new document but the request is too ambiguous to create safely
- Create a new document only when the user clearly wants a separate document, not merely changes to the current one
- The new document should belong to the current collection
- `title` and `text` are required when `decision = "create"`
- Keep the document self-contained and useful
- Keep `summary` short, concrete, and user-facing
- Do not invent unsupported facts
"""


class DocumentCreationProposal(BaseModel):
    decision: str = "no-create"
    reason: str | None = None
    title: str | None = None
    text: str | None = None
    summary: str | None = None


class DocumentCreationManager:
    def __init__(self, settings: AppSettings, model_client: ModelClient):
        self.settings = settings
        self.model_client = model_client

    async def propose_create(
        self,
        *,
        thread_workspace: ThreadWorkspace,
        collection: OutlineCollection | None,
        document: OutlineDocument,
        user_comment: str,
        comment_context: str,
        related_documents_context: str | None,
        local_workspace_context: str | None,
        current_comment_image_count: int = 0,
        input_images: list[ModelInputImage] | None = None,
    ) -> DocumentCreationProposal:
        user_prompt = self._build_user_prompt(
            thread_workspace=thread_workspace,
            collection=collection,
            document=document,
            user_comment=user_comment,
            comment_context=comment_context,
            related_documents_context=related_documents_context,
            local_workspace_context=local_workspace_context,
            current_comment_image_count=current_comment_image_count,
        )
        raw = await self._generate_with_optional_images(
            DOCUMENT_CREATION_SYSTEM_PROMPT,
            user_prompt,
            input_images=input_images or [],
        )
        try:
            payload = extract_json_object(raw)
        except JsonExtractionError:
            return DocumentCreationProposal(decision="no-create", reason="model-output-not-json")

        proposal = DocumentCreationProposal.model_validate(payload)
        return self._sanitize(proposal)

    def preview(
        self,
        proposal: DocumentCreationProposal,
        *,
        created_document: OutlineDocument | None = None,
    ) -> str | None:
        if proposal.decision == "no-create":
            return None
        if proposal.decision == "blocked":
            return f"blocked: {proposal.reason or 'document creation could not be applied'}"

        parts: list[str] = []
        if proposal.title:
            parts.append(f"title={proposal.title}")
        if proposal.summary:
            parts.append(f"summary={proposal.summary}")
        if created_document and created_document.url:
            parts.append(f"url={created_document.url}")
        return " ; ".join(parts) if parts else "document creation prepared"

    def format_reply_context(
        self,
        proposal: DocumentCreationProposal,
        *,
        status: str,
        created_document: OutlineDocument | None = None,
    ) -> str | None:
        if proposal.decision == "no-create":
            return None

        lines = [f"- status: {status}"]
        if proposal.summary:
            lines.append(f"- summary: {proposal.summary}")
        if proposal.reason:
            lines.append(f"- reason: {proposal.reason}")
        if created_document is not None:
            lines.append(f"- created title: {created_document.title or proposal.title or '(unknown)'}")
            lines.append(f"- created document id: {created_document.id}")
            if created_document.url:
                lines.append(f"- created document url: {created_document.url}")
        elif proposal.title:
            lines.append(f"- planned title: {proposal.title}")
        return "\n".join(lines)

    def _build_user_prompt(
        self,
        *,
        thread_workspace: ThreadWorkspace,
        collection: OutlineCollection | None,
        document: OutlineDocument,
        user_comment: str,
        comment_context: str,
        related_documents_context: str | None,
        local_workspace_context: str | None,
        current_comment_image_count: int,
    ) -> str:
        collection_name = collection.name if collection and collection.name else document.collection_id or "(unknown)"
        thread_context = _truncate(
            thread_workspace.load_prompt_context(self.settings.max_thread_session_chars),
            self.settings.max_thread_session_chars,
        )
        current_comment_image_section = ""
        if current_comment_image_count > 0:
            noun = "image" if current_comment_image_count == 1 else "images"
            current_comment_image_section = (
                f"The latest user comment also includes {current_comment_image_count} embedded {noun}. "
                "If image inputs are attached to this request, use them when drafting the new document.\n\n"
            )
        related_documents_section = ""
        if related_documents_context:
            related_documents_section = f"Related documents in this collection:\n{related_documents_context}\n\n"
        local_workspace_section = ""
        if local_workspace_context:
            local_workspace_section = (
                "Reliable local workspace observations from attachment/file processing:\n"
                f"{_truncate(local_workspace_context, self.settings.max_prompt_chars)}\n\n"
            )
        document_excerpt = _truncate(document.text or "", self.settings.max_document_chars)
        return (
            f"Collection: {collection_name}\n"
            f"Collection ID: {document.collection_id or '(unknown)'}\n"
            f"Thread ID: {thread_workspace.thread_id}\n"
            f"Current document ID: {document.id}\n"
            f"Current document title: {document.title or '(unknown)'}\n\n"
            "Persisted thread context:\n"
            f"{thread_context or '(no thread context)'}\n\n"
            "Relevant comment context:\n"
            f"{comment_context or '(no comment context)'}\n\n"
            f"{current_comment_image_section}"
            f"{related_documents_section}"
            f"{local_workspace_section}"
            "Current document excerpt:\n"
            f"{document_excerpt or '(document text unavailable)'}\n\n"
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

    def _sanitize(self, proposal: DocumentCreationProposal) -> DocumentCreationProposal:
        decision = proposal.decision if proposal.decision in {"no-create", "create", "blocked"} else "no-create"
        reason = _normalize_short_text(proposal.reason)
        title = _normalize_title(proposal.title)
        text = proposal.text.strip() if isinstance(proposal.text, str) and proposal.text.strip() else None
        summary = _normalize_short_text(proposal.summary)

        if decision == "blocked":
            return DocumentCreationProposal(
                decision="blocked",
                reason=reason or "The requested new document was too ambiguous to create safely.",
                summary=summary,
            )
        if decision != "create" or not title or not text:
            return DocumentCreationProposal(decision="no-create", reason=reason)
        return DocumentCreationProposal(
            decision="create",
            reason=reason,
            title=title,
            text=text,
            summary=summary,
        )


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _normalize_short_text(text: str | None) -> str | None:
    if text is None:
        return None
    compact = " ".join(text.split()).strip()
    if len(compact) < 4:
        return None
    return compact


def _normalize_title(text: str | None) -> str | None:
    if text is None:
        return None
    compact = " ".join(text.split()).strip()
    return compact or None
