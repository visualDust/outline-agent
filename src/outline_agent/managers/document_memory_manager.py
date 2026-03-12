from __future__ import annotations

import json
import re

from pydantic import BaseModel, Field

from ..clients.model_client import ModelClient
from ..clients.outline_client import OutlineCollection, OutlineDocument
from ..core.config import AppSettings
from ..core.logging import logger
from ..core.prompt_registry import PromptRegistry
from ..state.workspace import DocumentWorkspace

DOCUMENT_MEMORY_UPDATE_SYSTEM_PROMPT = """You maintain durable document-local MEMORY.md state for an Outline agent.

Decide whether the document MEMORY.md should be updated based on the latest interaction.
Keep the memory useful for future follow-up turns anywhere in the same document.
Do NOT store raw transcripts, long verbatim excerpts, or thread-specific temporary details unless they are clearly useful across multiple threads in this document.

Return strict JSON only with this schema:
{{
  "should_write": true,
  "summary": "short document summary",
  "open_questions": ["short unresolved document-level question"],
  "working_notes": ["short reusable document-level note"]
}}

Rules:
- If no update is needed, set should_write to false
- Keep the summary concise and standalone
- At most {max_open_questions} open questions
- At most {max_working_notes} working notes
"""


class DocumentMemoryUpdateProposal(BaseModel):
    should_write: bool = False
    summary: str | None = None
    open_questions: list[str] = Field(default_factory=list)
    working_notes: list[str] = Field(default_factory=list)


class DocumentMemoryManager:
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
        collection: OutlineCollection | None,
        document: OutlineDocument,
        user_comment: str,
        assistant_reply: str,
    ) -> DocumentMemoryUpdateProposal:
        logger.debug(
            "Proposing document memory update: document_id={}, document_title={}, workspace={}",
            document.id,
            document.title or "",
            document_workspace.root_dir,
        )
        system_prompt = self.prompt_registry.compose_internal_prompt(
            DOCUMENT_MEMORY_UPDATE_SYSTEM_PROMPT.format(
                max_open_questions=self.settings.document_memory_max_open_questions,
                max_working_notes=self.settings.document_memory_max_working_notes,
            ),
            "document_memory_update_policy.md",
        )
        user_prompt = self._build_user_prompt(
            document_workspace=document_workspace,
            collection=collection,
            document=document,
            user_comment=user_comment,
            assistant_reply=assistant_reply,
        )
        raw = await self.model_client.generate_reply(system_prompt, user_prompt)
        payload = _extract_json_object(raw)
        if payload is None:
            logger.debug(
                "Document memory update returned non-JSON payload: document_id={}, raw_preview={}",
                document.id,
                _truncate(raw, 240),
            )
            return DocumentMemoryUpdateProposal(should_write=False)
        proposal = DocumentMemoryUpdateProposal.model_validate(payload)
        sanitized = self._sanitize(proposal)
        logger.debug(
            "Document memory proposal ready: document_id={}, should_write={}, open_questions={}, working_notes={}",
            document.id,
            sanitized.should_write,
            len(sanitized.open_questions),
            len(sanitized.working_notes),
        )
        return sanitized

    def apply_update(self, document_workspace: DocumentWorkspace, proposal: DocumentMemoryUpdateProposal) -> str | None:
        if not proposal.should_write:
            logger.debug(
                "Skipping document memory write: document_id={}, workspace={}",
                document_workspace.document_id,
                document_workspace.root_dir,
            )
            return None

        text = document_workspace.memory_path.read_text(encoding="utf-8")
        text = _replace_section(
            text=text,
            heading="Summary",
            body_lines=[proposal.summary] if proposal.summary else [],
        )
        text = _replace_section(
            text=text,
            heading="Open Questions",
            body_lines=[f"- {item}" for item in proposal.open_questions],
        )
        text = _replace_section(
            text=text,
            heading="Working Notes",
            body_lines=[f"- {item}" for item in proposal.working_notes],
        )
        document_workspace.memory_path.write_text(_ensure_trailing_newline(text), encoding="utf-8")
        current_state = document_workspace.read_state()
        document_workspace.write_state(
            {
                "document_id": document_workspace.document_id,
                "document_title": current_state.get("document_title"),
            }
        )
        preview = self.preview(proposal)
        logger.debug(
            "Applied document memory update: document_id={}, workspace={}, preview={}",
            document_workspace.document_id,
            document_workspace.root_dir,
            preview or "",
        )
        return preview

    def preview(self, proposal: DocumentMemoryUpdateProposal) -> str | None:
        if not proposal.should_write:
            return None
        parts: list[str] = []
        if proposal.summary:
            parts.append(f"summary={proposal.summary}")
        if proposal.open_questions:
            parts.append("open_questions=" + " | ".join(proposal.open_questions))
        if proposal.working_notes:
            parts.append("working_notes=" + " | ".join(proposal.working_notes))
        return " ; ".join(parts) if parts else None

    def _build_user_prompt(
        self,
        *,
        document_workspace: DocumentWorkspace,
        collection: OutlineCollection | None,
        document: OutlineDocument,
        user_comment: str,
        assistant_reply: str,
    ) -> str:
        collection_name = collection.name if collection and collection.name else document.collection_id or "(unknown)"
        memory_excerpt = _truncate(
            document_workspace.memory_path.read_text(encoding="utf-8"),
            self.settings.max_document_memory_chars,
        )
        document_excerpt = _truncate(document.text or "", self.settings.max_document_chars)
        return (
            f"Collection: {collection_name}\n"
            f"Collection ID: {document.collection_id or '(unknown)'}\n"
            f"Document ID: {document.id}\n"
            f"Document title: {document.title or '(unknown)'}\n\n"
            "Current document MEMORY.md excerpt:\n"
            f"{memory_excerpt}\n\n"
            "Document excerpt:\n"
            f"{document_excerpt or '(document text unavailable)'}\n\n"
            "Latest user comment:\n"
            f"{_truncate(user_comment, self.settings.max_prompt_chars)}\n\n"
            "Assistant reply:\n"
            f"{_truncate(assistant_reply, self.settings.max_prompt_chars)}"
        )

    def _sanitize(self, proposal: DocumentMemoryUpdateProposal) -> DocumentMemoryUpdateProposal:
        summary = _normalize_text(proposal.summary or "")
        if summary:
            summary = _truncate(summary, self.settings.document_memory_summary_max_chars)

        open_questions = _normalize_unique_items(
            proposal.open_questions,
            limit=self.settings.document_memory_max_open_questions,
            item_max_chars=self.settings.document_memory_item_max_chars,
        )
        working_notes = _normalize_unique_items(
            proposal.working_notes,
            limit=self.settings.document_memory_max_working_notes,
            item_max_chars=self.settings.document_memory_item_max_chars,
        )

        should_write = proposal.should_write and bool(summary or open_questions or working_notes)
        return DocumentMemoryUpdateProposal(
            should_write=should_write,
            summary=summary or None,
            open_questions=open_questions,
            working_notes=working_notes,
        )


def _extract_json_object(raw: str) -> dict[str, object] | None:
    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _replace_section(text: str, heading: str, body_lines: list[str]) -> str:
    lines = text.splitlines()
    marker = f"## {heading}"
    try:
        start_index = lines.index(marker)
    except ValueError:
        if lines and lines[-1] != "":
            lines.append("")
        lines.append(marker)
        start_index = len(lines) - 1

    end_index = start_index + 1
    while end_index < len(lines) and not lines[end_index].startswith("## "):
        end_index += 1

    replacement = [marker]
    replacement.extend(body_lines)
    replacement.append("")
    return "\n".join(lines[:start_index] + replacement + lines[end_index:]).rstrip() + "\n"


def _normalize_unique_items(items: list[str], *, limit: int, item_max_chars: int) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in items:
        normalized = _normalize_text(item)
        if not normalized:
            continue
        normalized = _truncate(normalized, item_max_chars)
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(normalized)
        if len(cleaned) >= limit:
            break
    return cleaned


def _normalize_text(text: str) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) < 5:
        return ""
    return compact


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _ensure_trailing_newline(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"
