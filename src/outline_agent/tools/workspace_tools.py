from __future__ import annotations

from typing import Any

from ..runtime.tool_runtime import ToolExecutionStep, ToolRuntime
from .base import ToolContext, ToolError, ToolResult, ToolSpec


class _RuntimeBackedWorkspaceTool:
    tool_name: str
    description: str
    when_to_use: str
    input_schema: dict[str, Any]
    side_effect_level: str

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.tool_name,
            description=self.description,
            when_to_use=self.when_to_use,
            input_schema=self.input_schema,
            output_schema={
                "type": "object",
                "properties": {
                    "target": {"type": ["string", "null"]},
                    "stdout": {"type": ["string", "null"]},
                    "stderr": {"type": ["string", "null"]},
                    "exit_code": {"type": ["integer", "null"]},
                },
            },
            side_effect_level=self.side_effect_level,  # type: ignore[arg-type]
        )

    async def run(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        if context.work_dir is None:
            raise ToolError(f"{self.tool_name} requires a work_dir")
        runtime = ToolRuntime(context.settings, outline_client=context.outline_client)
        step = self._build_step(args)
        document_id = context.document.id if context.document is not None else None
        step_result = await runtime.execute_step(context.work_dir, step, document_id=document_id)
        artifacts: list[dict[str, Any]] = []
        if step_result.attachment is not None:
            artifacts.append(
                {
                    "type": "uploaded_attachment",
                    "path": step_result.attachment.path,
                    "name": step_result.attachment.name,
                    "url": step_result.attachment.url,
                    "attachment_id": step_result.attachment.attachment_id,
                    "file_hash": step_result.attachment.file_hash,
                }
            )
        data = {
            "path": step_result.target,
            "target": step_result.target,
            "stdout": step_result.stdout,
            "stderr": step_result.stderr,
            "exit_code": step_result.exit_code,
        }
        return ToolResult(
            ok=step_result.ok,
            tool=self.tool_name,
            summary=step_result.summary,
            data=data,
            artifacts=artifacts,
            preview=step_result.summary,
            error=None if step_result.ok else step_result.summary,
        )

    def _build_step(self, args: dict[str, Any]) -> ToolExecutionStep:
        raise NotImplementedError


class ListDirTool(_RuntimeBackedWorkspaceTool):
    tool_name = "list_dir"
    description = "List files in the current thread workspace."
    when_to_use = "Use to inspect the local work directory before reading or editing files."
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": ["string", "null"]},
            "recursive": {"type": "boolean"},
        },
    }
    side_effect_level = "read"

    def _build_step(self, args: dict[str, Any]) -> ToolExecutionStep:
        return ToolExecutionStep(
            tool=self.tool_name,
            path=_as_optional_str(args.get("path")) or ".",
            recursive=bool(args.get("recursive", False)),
        )


class ReadFileTool(_RuntimeBackedWorkspaceTool):
    tool_name = "read_file"
    description = "Read a text file from the current thread workspace."
    when_to_use = "Use after locating a file whose contents are needed for reasoning."
    input_schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }
    side_effect_level = "read"

    def _build_step(self, args: dict[str, Any]) -> ToolExecutionStep:
        return ToolExecutionStep(tool=self.tool_name, path=_required_str(args, "path"))


class WriteFileTool(_RuntimeBackedWorkspaceTool):
    tool_name = "write_file"
    description = "Create or overwrite a file in the current thread workspace."
    when_to_use = "Use to create a local artifact or write intermediate text."
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "append": {"type": "boolean"},
        },
        "required": ["path", "content"],
    }
    side_effect_level = "write"

    def _build_step(self, args: dict[str, Any]) -> ToolExecutionStep:
        return ToolExecutionStep(
            tool=self.tool_name,
            path=_required_str(args, "path"),
            content=_required_text(args, "content"),
            append=bool(args.get("append", False)),
        )


class EditFileTool(_RuntimeBackedWorkspaceTool):
    tool_name = "edit_file"
    description = "Replace one exact text occurrence in a workspace file."
    when_to_use = "Use for small precise edits after reading a file."
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_text": {"type": "string"},
            "new_text": {"type": "string"},
        },
        "required": ["path", "old_text", "new_text"],
    }
    side_effect_level = "write"

    def _build_step(self, args: dict[str, Any]) -> ToolExecutionStep:
        return ToolExecutionStep(
            tool=self.tool_name,
            path=_required_str(args, "path"),
            old_text=_required_str(args, "old_text"),
            new_text=_required_str(args, "new_text"),
        )


class RunShellTool(_RuntimeBackedWorkspaceTool):
    tool_name = "run_shell"
    description = "Run a shell command inside the current thread workspace."
    when_to_use = (
        "Use when local file operations alone are insufficient, especially for format conversion, "
        "PDF extraction, rendering, or other multi-step fallback work."
    )
    input_schema = {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    }
    side_effect_level = "external"

    def _build_step(self, args: dict[str, Any]) -> ToolExecutionStep:
        return ToolExecutionStep(tool=self.tool_name, command=_required_str(args, "command"))


class UploadAttachmentTool(_RuntimeBackedWorkspaceTool):
    tool_name = "upload_attachment"
    description = "Upload a local workspace file back to the current Outline document as an attachment."
    when_to_use = "Use after generating a local artifact that should be available in Outline."
    input_schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }
    side_effect_level = "write"

    def _build_step(self, args: dict[str, Any]) -> ToolExecutionStep:
        return ToolExecutionStep(tool=self.tool_name, path=_required_str(args, "path"))


class DownloadAttachmentTool(_RuntimeBackedWorkspaceTool):
    tool_name = "download_attachment"
    description = "Download an Outline attachment or URL into the current thread workspace."
    when_to_use = (
        "Use before extracting, reading, converting, or otherwise processing attachment content locally."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "source_url": {"type": ["string", "null"]},
            "attachment_url": {"type": ["string", "null"]},
        },
        "required": ["path"],
    }
    side_effect_level = "external"

    def _build_step(self, args: dict[str, Any]) -> ToolExecutionStep:
        source_url = _as_optional_str(args.get("source_url")) or _as_optional_str(args.get("attachment_url"))
        if not source_url:
            raise ToolError("download_attachment requires source_url or attachment_url")
        return ToolExecutionStep(tool=self.tool_name, path=_required_str(args, "path"), source_url=source_url)


def build_workspace_tools() -> list[_RuntimeBackedWorkspaceTool]:
    return [
        ListDirTool(),
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        RunShellTool(),
        UploadAttachmentTool(),
        DownloadAttachmentTool(),
    ]


def _required_str(args: dict[str, Any], key: str) -> str:
    value = _as_optional_str(args.get(key))
    if value is None:
        raise ToolError(f"{key} is required")
    return value


def _required_text(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str):
        raise ToolError(f"{key} is required")
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
