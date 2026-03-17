from __future__ import annotations

from typing import Any

from ..clients.model_client import ModelClient, ModelInputImage
from ..clients.outline_models import OutlineCollection, OutlineDocument
from ..core.config import AppSettings
from ..core.prompt_registry import PromptRegistry
from ..managers.document_creation_manager import DocumentCreationManager
from ..managers.document_update_manager import DocumentUpdateManager
from ..state.workspace import DocumentWorkspace, ThreadWorkspace
from .base import ToolContext, ToolError, ToolResult, ToolSpec


class DraftDocumentUpdateTool:
    def __init__(
        self,
        settings: AppSettings,
        model_client: ModelClient,
        *,
        prompt_registry: PromptRegistry | None = None,
    ):
        self.manager = DocumentUpdateManager(settings, model_client, prompt_registry=prompt_registry)

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="draft_document_update",
            description="Draft a safe update for the current Outline document based on the user request.",
            when_to_use="Use before applying changes to the current document.",
            input_schema={
                "type": "object",
                "properties": {
                    "user_comment": {"type": "string"},
                    "comment_context": {"type": ["string", "null"]},
                    "related_documents_context": {"type": ["string", "null"]},
                    "local_workspace_context": {"type": ["string", "null"]},
                },
                "required": ["user_comment"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "decision": {"type": "string"},
                    "reason": {"type": ["string", "null"]},
                    "title": {"type": ["string", "null"]},
                    "text": {"type": ["string", "null"]},
                    "content": {"type": ["string", "null"]},
                    "summary": {"type": ["string", "null"]},
                    "operation_previews": {"type": "array"},
                },
            },
            side_effect_level="read",
        )

    async def run(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        document_workspace, thread_workspace, collection, document = _require_document_context(context)
        proposal = await self.manager.propose_update(
            document_workspace=document_workspace,
            thread_workspace=thread_workspace,
            collection=collection,
            document=document,
            user_comment=_get_user_comment(args, context),
            comment_context=_get_text_arg(args, "comment_context") or _context_extra_str(context, "comment_context"),
            related_documents_context=(
                _get_text_arg(args, "related_documents_context")
                or _context_extra_str(context, "related_documents_context")
            ),
            local_workspace_context=(
                _get_text_arg(args, "local_workspace_context")
                or _context_extra_str(context, "local_workspace_context")
            ),
            current_comment_image_count=_context_extra_int(context, "current_comment_image_count"),
            input_images=_context_extra_images(context),
        )
        preview = self.manager.preview(proposal)
        return ToolResult(
            ok=True,
            tool=self.spec.name,
            summary=preview or proposal.decision,
            data={
                "decision": proposal.decision,
                "reason": proposal.reason,
                "title": proposal.title,
                "text": proposal.text,
                "content": proposal.text,
                "summary": proposal.summary,
                "operation_previews": proposal.operation_previews,
            },
            preview=preview,
        )


class ApplyDocumentUpdateTool:
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="apply_document_update",
            description="Apply a drafted update to the current Outline document.",
            when_to_use="Use after draft_document_update returns decision=edit with non-empty content.",
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": ["string", "null"]},
                    "text": {"type": ["string", "null"]},
                    "content": {"type": ["string", "null"]},
                },
            },
            output_schema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "string"},
                    "title": {"type": ["string", "null"]},
                    "text": {"type": ["string", "null"]},
                },
            },
            side_effect_level="write",
            requires_confirmation=True,
        )

    async def run(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        if context.outline_client is None:
            raise ToolError("apply_document_update requires an Outline client")
        _, _, _, document = _require_document_context(context)
        title = _get_text_arg(args, "title")
        text = _get_text_arg(args, "text") or _get_text_arg(args, "content")
        if title is None and text is None:
            raise ToolError("apply_document_update requires title or text")
        await context.outline_client.update_document(document.id, title=title, text=text)
        return ToolResult(
            ok=True,
            tool=self.spec.name,
            summary=f"Updated current document '{title or document.title or document.id}'.",
            data={"document_id": document.id, "title": title or document.title, "text": text},
            preview=title or document.id,
        )


class DraftNewDocumentTool:
    def __init__(
        self,
        settings: AppSettings,
        model_client: ModelClient,
        *,
        prompt_registry: PromptRegistry | None = None,
    ):
        self.manager = DocumentCreationManager(settings, model_client, prompt_registry=prompt_registry)

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="draft_new_document",
            description="Draft a standalone new Outline document from the current request.",
            when_to_use="Use before create_document when the user clearly asked for a separate document.",
            input_schema={
                "type": "object",
                "properties": {
                    "user_comment": {"type": "string"},
                    "comment_context": {"type": ["string", "null"]},
                    "related_documents_context": {"type": ["string", "null"]},
                    "local_workspace_context": {"type": ["string", "null"]},
                },
                "required": ["user_comment"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "decision": {"type": "string"},
                    "reason": {"type": ["string", "null"]},
                    "title": {"type": ["string", "null"]},
                    "text": {"type": ["string", "null"]},
                    "content": {"type": ["string", "null"]},
                    "summary": {"type": ["string", "null"]},
                },
            },
            side_effect_level="read",
        )

    async def run(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        document_workspace, thread_workspace, collection, document = _require_document_context(context)
        proposal = await self.manager.propose_create(
            document_workspace=document_workspace,
            thread_workspace=thread_workspace,
            collection=collection,
            document=document,
            user_comment=_get_user_comment(args, context),
            comment_context=_get_text_arg(args, "comment_context") or _context_extra_str(context, "comment_context"),
            related_documents_context=(
                _get_text_arg(args, "related_documents_context")
                or _context_extra_str(context, "related_documents_context")
            ),
            local_workspace_context=(
                _get_text_arg(args, "local_workspace_context")
                or _context_extra_str(context, "local_workspace_context")
            ),
            current_comment_image_count=_context_extra_int(context, "current_comment_image_count"),
            input_images=_context_extra_images(context),
        )
        preview = self.manager.preview(proposal)
        return ToolResult(
            ok=True,
            tool=self.spec.name,
            summary=preview or proposal.decision,
            data={
                "decision": proposal.decision,
                "reason": proposal.reason,
                "title": proposal.title,
                "text": proposal.text,
                "content": proposal.text,
                "summary": proposal.summary,
            },
            preview=preview,
        )


def _require_document_context(
    context: ToolContext,
) -> tuple[DocumentWorkspace, ThreadWorkspace, OutlineCollection | None, OutlineDocument]:
    document_workspace = context.extra.get("document_workspace")
    if not isinstance(document_workspace, DocumentWorkspace):
        raise ToolError("document drafting tools require document_workspace in context.extra")
    thread_workspace = context.extra.get("thread_workspace")
    if not isinstance(thread_workspace, ThreadWorkspace):
        raise ToolError("document drafting tools require thread_workspace in context.extra")
    document = context.document
    if document is None:
        raise ToolError("document drafting tools require current document")
    if isinstance(context.collection, OutlineCollection) or context.collection is None:
        collection = context.collection
    else:
        collection = None
    return document_workspace, thread_workspace, collection, document


def _get_user_comment(args: dict[str, Any], context: ToolContext) -> str:
    user_comment = _get_text_arg(args, "user_comment") or _context_extra_str(context, "user_comment")
    if not user_comment:
        raise ToolError("user_comment is required")
    return user_comment


def _get_text_arg(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolError(f"{key} must be a string")
    compact = value.strip()
    return compact or None


def _context_extra_str(context: ToolContext, key: str) -> str | None:
    value = context.extra.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _context_extra_int(context: ToolContext, key: str) -> int:
    value = context.extra.get(key)
    return value if isinstance(value, int) and value >= 0 else 0


def _context_extra_images(context: ToolContext) -> list[ModelInputImage]:
    value = context.extra.get("input_images")
    if isinstance(value, list) and all(isinstance(item, ModelInputImage) for item in value):
        return value
    return []
