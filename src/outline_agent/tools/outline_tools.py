from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .base import ToolContext, ToolError, ToolResult, ToolSpec


class GetCurrentDocumentTool:
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="get_current_document",
            description="Read the current Outline document that the comment thread belongs to.",
            when_to_use="Use before editing, summarizing, or creating related content from the active document.",
            input_schema={"type": "object", "properties": {}},
            output_schema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "string"},
                    "title": {"type": ["string", "null"]},
                    "text": {"type": ["string", "null"]},
                    "collection_id": {"type": ["string", "null"]},
                    "url": {"type": ["string", "null"]},
                },
            },
            side_effect_level="read",
        )

    async def run(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        del args
        if context.document is None:
            raise ToolError("no current document is available in tool context")
        document = context.document
        return ToolResult(
            ok=True,
            tool=self.spec.name,
            summary=f"Loaded current document '{document.title or document.id}'.",
            data={
                "document_id": document.id,
                "title": document.title,
                "text": document.text,
                "collection_id": document.collection_id,
                "url": document.url,
            },
            preview=document.title or document.id,
        )


class DownloadAttachmentTool:
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="download_attachment",
            description="Download an Outline attachment or attachment URL into the current collection workspace.",
            when_to_use="Use before reading or extracting content from an attachment.",
            input_schema={
                "type": "object",
                "properties": {
                    "attachment_url": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["attachment_url", "path"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "size": {"type": ["integer", "null"]},
                    "content_type": {"type": ["string", "null"]},
                },
            },
            side_effect_level="external",
        )

    async def run(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        if context.outline_client is None:
            raise ToolError("download_attachment requires an Outline client")
        work_dir = _require_work_dir(context)
        attachment_url = _as_required_str(args, "attachment_url")
        relative_path = _as_required_str(args, "path")
        target_path = _resolve_relative_path(work_dir, relative_path)
        result = await context.outline_client.download_attachment(attachment_url, target_path)
        return ToolResult(
            ok=True,
            tool=self.spec.name,
            summary=f"Downloaded attachment to {relative_path}.",
            data={
                "path": relative_path,
                "absolute_path": str(target_path),
                "size": result.get("size"),
                "content_type": result.get("content_type"),
                "source_url": attachment_url,
            },
            artifacts=[{"type": "file", "path": relative_path}],
            preview=relative_path,
        )


class CreateDocumentTool:
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="create_document",
            description="Create a new Outline document in the current collection or a specified collection.",
            when_to_use="Use when the user clearly asked for a separate new document.",
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "text": {"type": "string"},
                    "content": {"type": ["string", "null"]},
                    "collection_id": {"type": ["string", "null"]},
                    "parent_document_id": {"type": ["string", "null"]},
                    "publish": {"type": "boolean"},
                },
                "required": ["title"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "string"},
                    "title": {"type": "string"},
                    "url": {"type": ["string", "null"]},
                    "collection_id": {"type": ["string", "null"]},
                },
            },
            side_effect_level="write",
        )

    async def run(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        if context.outline_client is None:
            raise ToolError("create_document requires an Outline client")
        title = _as_required_str(args, "title")
        text = _as_optional_str(args.get("text")) or _as_optional_str(args.get("content"))
        if not text:
            raise ToolError("text is required")
        collection_id = _as_optional_str(args.get("collection_id")) or _resolve_collection_id(context)
        if not collection_id:
            raise ToolError("no collection_id available for create_document")
        parent_document_id = _as_optional_str(args.get("parent_document_id"))
        publish = bool(args.get("publish", True))
        created = await context.outline_client.create_document(
            title=title,
            text=text,
            collection_id=collection_id,
            parent_document_id=parent_document_id,
            publish=publish,
        )
        return ToolResult(
            ok=True,
            tool=self.spec.name,
            summary=f"Created document '{created.title or title}'.",
            data={
                "document_id": created.id,
                "title": created.title or title,
                "url": created.url,
                "collection_id": created.collection_id,
                "text": created.text,
            },
            preview=created.url or created.id,
        )


def _require_work_dir(context: ToolContext) -> Path:
    if context.work_dir is None:
        raise ToolError("tool requires a work_dir in context")
    return context.work_dir


def _resolve_relative_path(work_dir: Path, value: str) -> Path:
    raw = value.strip().replace("\\", "/")
    if not raw:
        raise ToolError("path cannot be empty")
    candidate = Path(os.path.normpath(str(work_dir / raw)))
    base = Path(os.path.normpath(str(work_dir)))
    if candidate != base and base not in candidate.parents:
        raise ToolError(f"path escapes work dir: {value}")
    candidate.parent.mkdir(parents=True, exist_ok=True)
    return candidate


def _resolve_collection_id(context: ToolContext) -> str | None:
    if context.collection and context.collection.id:
        return context.collection.id
    if context.document and context.document.collection_id:
        return context.document.collection_id
    return None


def _as_required_str(args: dict[str, Any], key: str) -> str:
    value = _as_optional_str(args.get(key))
    if not value:
        raise ToolError(f"{key} is required")
    return value


def _as_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolError("expected a string argument")
    compact = value.strip()
    return compact or None
