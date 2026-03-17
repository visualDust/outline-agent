from __future__ import annotations

from typing import Any

from ..runtime.tool_runtime import ToolExecutionStep, ToolRuntime
from ..state.thread_transcript import active_comments as _active_transcript_comments
from ..state.thread_transcript import render_comments_for_prompt as _render_comments_for_prompt
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
            requires_confirmation=self.side_effect_level != "read",
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
    description = "List files in the current collection workspace."
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
    description = "Read a text file from the current collection workspace."
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
    description = "Create or overwrite a file in the current collection workspace."
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
    description = "Run a shell command inside the current collection workspace."
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
    description = "Download an Outline attachment or URL into the current collection workspace."
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


class GetThreadHistoryTool:
    tool_name = "get_thread_history"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.tool_name,
            description="Read exact comment history from the current thread, including truncated sections.",
            when_to_use=(
                "Use when the thread context in the prompt was truncated and you need exact earlier comments, "
                "a range of comments, comments around a specific comment, or comment search results."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["range", "around_comment", "search"]},
                    "start_index": {"type": "integer"},
                    "end_index": {"type": "integer"},
                    "comment_id": {"type": "string"},
                    "before": {"type": "integer"},
                    "after": {"type": "integer"},
                    "query": {"type": "string"},
                },
                "required": ["mode"],
            },
            side_effect_level="read",
        )

    async def run(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        thread_workspace = context.extra.get("thread_workspace")
        if thread_workspace is None:
            raise ToolError("get_thread_history requires thread_workspace in context")
        transcript = thread_workspace.read_transcript()
        comments = _active_transcript_comments(transcript)
        mode = _required_str(args, "mode")
        selected: list[dict[str, Any]]
        if mode == "range":
            start_index = max(1, _required_int(args, "start_index"))
            end_index = max(start_index, _required_int(args, "end_index"))
            selected = comments[start_index - 1 : end_index]
        elif mode == "around_comment":
            comment_id = _required_str(args, "comment_id")
            index = next((i for i, item in enumerate(comments) if item.get("id") == comment_id), None)
            if index is None:
                raise ToolError(f"comment_id not found in current thread: {comment_id}")
            before = max(0, _optional_int(args.get("before")) or 3)
            after = max(0, _optional_int(args.get("after")) or 3)
            selected = comments[max(0, index - before) : index + after + 1]
        elif mode == "search":
            query = _required_str(args, "query").casefold()
            selected = [
                item
                for item in comments
                if query in ((_as_optional_str(item.get("body_plain")) or "").casefold())
            ][:10]
        else:
            raise ToolError(f"unsupported mode: {mode}")

        rendered = _render_comments_for_prompt(
            comments=selected,
            current_comment_id=_as_optional_str(context.extra.get("current_comment_id")) or "",
        )
        return ToolResult(
            ok=True,
            tool=self.tool_name,
            summary=f"get_thread_history[{mode}] -> {len(selected)} comment(s)",
            data={
                "text": rendered,
                "comment_count": len(selected),
                "comments": selected,
            },
            preview=rendered,
        )


def build_workspace_tools() -> list[Any]:
    return [
        ListDirTool(),
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        RunShellTool(),
        UploadAttachmentTool(),
        DownloadAttachmentTool(),
        GetThreadHistoryTool(),
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


def _required_int(args: dict[str, Any], key: str) -> int:
    value = _optional_int(args.get(key))
    if value is None:
        raise ToolError(f"{key} is required")
    return value


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        raise ToolError("expected an integer argument")
    if isinstance(value, int):
        return value
    return None


def _as_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolError("expected a string argument")
    compact = value.strip()
    return compact or None
