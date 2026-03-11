from __future__ import annotations

import re

from ..clients.model_client import ModelClient, ModelInputImage
from ..clients.outline_client import OutlineCollection, OutlineDocument
from ..core.config import AppSettings
from ..runtime.tool_runtime import ToolExecutionPlan, ToolExecutionReport, ToolExecutionStep, describe_work_dir
from ..state.workspace import ThreadWorkspace
from ..utils.json_utils import JsonExtractionError, extract_json_object

TOOL_USE_SYSTEM_PROMPT = """You decide whether an Outline comment agent should use local sandbox tools before replying.

Return strict JSON only with this schema:
{{
  "should_run": true,
  "reason": "short explanation",
  "steps": [
    {{
      "tool": "list_dir|read_file|write_file|edit_file|run_shell|upload_attachment|download_attachment",
      "path": "relative/path/or/null",
      "source_url": "attachment/url/or/null",
      "content": "file content or null",
      "append": false,
      "old_text": "exact text to replace or null",
      "new_text": "replacement text or null",
      "command": "shell command or null",
      "recursive": false
    }}
  ]
}}

Rules:
- Use tools only when the user is explicitly asking for local workspace work such as creating,
  inspecting, editing, or running files/scripts/commands
- If a normal text reply is enough, set `should_run` to false
- Use at most {max_steps} steps
- Use relative paths only; never use absolute paths or parent-directory traversal
- Prefer minimal steps and simple filenames
- `write_file` can create a new file or overwrite an existing file
- `edit_file` should be used only for exact single-location replacements
- `run_shell` runs inside the thread work dir and should stay short and focused
- `download_attachment` downloads an existing Outline attachment or URL into the thread work dir
- `upload_attachment` uploads an existing file from the thread work dir back to the current Outline document
- When you need to inspect or transform an existing attachment from Outline before replying,
  first use `download_attachment`, then any local read/process steps, then `upload_attachment` if needed
- If the user wants a generated PDF or other artifact available in Outline,
  use `upload_attachment` as the final step after creating the file
- If a prior round already completed the needed upload or other step successfully,
  do not repeat the same step again unless a later step changed the relevant file
- Do not upload the same file more than once in the same turn unless the file was changed afterwards
- Do not repeat the same inspection-only plan (for example repeated `list_dir` / `read_file` checks)
  if no later step changed the relevant workspace state
- Do not assume command output; only plan commands that are actually needed
- If the user is only asking for a document edit in Outline, or only asking a question, prefer `should_run = false`
"""


class ToolUseManager:
    def __init__(self, settings: AppSettings, model_client: ModelClient):
        self.settings = settings
        self.model_client = model_client

    async def propose_plan(
        self,
        *,
        thread_workspace: ThreadWorkspace,
        collection: OutlineCollection | None,
        document: OutlineDocument,
        user_comment: str,
        comment_context: str,
        current_round: int = 1,
        prior_round_summaries: list[str] | None = None,
        current_comment_image_count: int = 0,
        input_images: list[ModelInputImage] | None = None,
    ) -> ToolExecutionPlan:
        system_prompt = TOOL_USE_SYSTEM_PROMPT.format(max_steps=self.settings.tool_execution_max_steps)
        user_prompt = self._build_user_prompt(
            thread_workspace=thread_workspace,
            collection=collection,
            document=document,
            user_comment=user_comment,
            comment_context=comment_context,
            current_round=current_round,
            prior_round_summaries=prior_round_summaries or [],
            current_comment_image_count=current_comment_image_count,
        )
        raw = await self._generate_with_optional_images(
            system_prompt,
            user_prompt,
            input_images=input_images or [],
        )
        try:
            payload = extract_json_object(raw)
        except JsonExtractionError:
            return ToolExecutionPlan(should_run=False)

        proposal = ToolExecutionPlan.model_validate(payload)
        return self._sanitize(proposal)

    def preview(self, proposal: ToolExecutionPlan) -> str | None:
        if not proposal.should_run:
            return None

        parts: list[str] = []
        if proposal.reason:
            parts.append(f"reason={proposal.reason}")
        if proposal.steps:
            parts.append("steps=" + " | ".join(_preview_step(step) for step in proposal.steps))
        return " ; ".join(parts) if parts else None

    def format_reply_context(
        self,
        *,
        work_dir: str,
        proposal: ToolExecutionPlan,
        status: str,
        report: ToolExecutionReport | None,
    ) -> str | None:
        if not proposal.should_run:
            return None

        lines = [f"- status: {status}", f"- work_dir: {work_dir}"]
        if proposal.reason:
            lines.append(f"- reason: {proposal.reason}")

        if report is None:
            lines.append(f"- planned steps: {' ; '.join(_preview_step(step) for step in proposal.steps)}")
            return "\n".join(lines)

        for index, result in enumerate(report.step_results, start=1):
            lines.append(f"- step {index}: {result.summary}")
            if result.attachment is not None and result.attachment.url:
                lines.append(
                    f"  uploaded_file: {result.attachment.name} -> {result.attachment.url}"
                )
        return "\n".join(lines)

    def _build_user_prompt(
        self,
        *,
        thread_workspace: ThreadWorkspace,
        collection: OutlineCollection | None,
        document: OutlineDocument,
        user_comment: str,
        comment_context: str,
        current_round: int,
        prior_round_summaries: list[str],
        current_comment_image_count: int,
    ) -> str:
        collection_name = collection.name if collection and collection.name else document.collection_id or "(unknown)"
        thread_context = _truncate(
            thread_workspace.load_prompt_context(self.settings.max_thread_session_chars),
            self.settings.max_thread_session_chars,
        )
        document_excerpt = _truncate(document.text or "", self.settings.max_document_chars)
        work_dir_snapshot = describe_work_dir(
            thread_workspace.work_dir,
            max_entries=self.settings.tool_list_dir_max_entries,
        )
        prior_rounds_section = "\n".join(f"- {item}" for item in prior_round_summaries) or "(none yet)"
        current_comment_image_section = ""
        if current_comment_image_count > 0:
            noun = "image" if current_comment_image_count == 1 else "images"
            current_comment_image_section = (
                f"The latest user comment also includes {current_comment_image_count} embedded {noun}. "
                "If image inputs are attached to this request, use them when deciding the tool plan.\n\n"
            )
        return (
            f"Collection: {collection_name}\n"
            f"Collection ID: {document.collection_id or '(unknown)'}\n"
            f"Document Title: {document.title or '(unknown)'}\n"
            f"Current Outline document ID: {document.id}\n"
            f"Thread ID: {thread_workspace.thread_id}\n"
            f"Thread workspace: {thread_workspace.root_dir}\n"
            f"Thread work dir: {thread_workspace.work_dir}\n\n"
            f"Current planning round: {current_round} of {self.settings.tool_execution_max_rounds}\n\n"
            "Persisted thread context:\n"
            f"{thread_context or '(no thread context)'}\n\n"
            "Tool execution history in this turn:\n"
            f"{prior_rounds_section}\n\n"
            "Current work dir inventory:\n"
            f"{work_dir_snapshot}\n\n"
            "Relevant comment context:\n"
            f"{comment_context or '(no comment context)'}\n\n"
            f"{current_comment_image_section}"
            "Document excerpt:\n"
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

    def _sanitize(self, proposal: ToolExecutionPlan) -> ToolExecutionPlan:
        reason = _normalize_short_text(proposal.reason)
        cleaned_steps: list[ToolExecutionStep] = []
        seen: set[tuple[object, ...]] = set()
        for step in proposal.steps:
            cleaned = _sanitize_step(step, self.settings.tool_file_max_chars)
            if cleaned is None:
                continue
            key = (
                cleaned.tool,
                cleaned.path,
                cleaned.source_url,
                cleaned.content,
                cleaned.append,
                cleaned.old_text,
                cleaned.new_text,
                cleaned.command,
                cleaned.recursive,
            )
            if key in seen:
                continue
            seen.add(key)
            cleaned_steps.append(cleaned)
            if len(cleaned_steps) >= self.settings.tool_execution_max_steps:
                break

        should_run = proposal.should_run and bool(cleaned_steps)
        return ToolExecutionPlan(should_run=should_run, reason=reason, steps=cleaned_steps)


def _sanitize_step(step: ToolExecutionStep, max_file_chars: int) -> ToolExecutionStep | None:
    path = _normalize_path(step.path)
    command = _normalize_command(step.command)
    content = _normalize_content(step.content, max_file_chars)
    old_text = _normalize_content(step.old_text, max_file_chars)
    new_text = _normalize_content(step.new_text, max_file_chars)
    source_url = _normalize_url(step.source_url)

    if step.tool == "list_dir":
        return ToolExecutionStep(
            tool=step.tool,
            path=path or ".",
            recursive=bool(step.recursive),
        )
    if step.tool == "read_file":
        if not path:
            return None
        return ToolExecutionStep(tool=step.tool, path=path)
    if step.tool == "write_file":
        if not path or content is None:
            return None
        return ToolExecutionStep(tool=step.tool, path=path, content=content, append=bool(step.append))
    if step.tool == "edit_file":
        if not path or old_text is None or new_text is None:
            return None
        return ToolExecutionStep(tool=step.tool, path=path, old_text=old_text, new_text=new_text)
    if step.tool == "run_shell":
        if not command:
            return None
        return ToolExecutionStep(tool=step.tool, command=command)
    if step.tool == "download_attachment":
        if not path or not source_url:
            return None
        return ToolExecutionStep(tool=step.tool, path=path, source_url=source_url)
    if step.tool == "upload_attachment":
        if not path:
            return None
        return ToolExecutionStep(tool=step.tool, path=path)
    return None


def _preview_step(step: ToolExecutionStep) -> str:
    if step.tool == "list_dir":
        return f"list_dir[{step.path or '.'}]"
    if step.tool == "read_file":
        return f"read_file[{step.path or '?'}]"
    if step.tool == "write_file":
        return f"write_file[{step.path or '?'}]"
    if step.tool == "edit_file":
        return f"edit_file[{step.path or '?'}]"
    if step.tool == "run_shell":
        return f"run_shell[{_truncate(_normalize_command(step.command) or '?', 60)}]"
    if step.tool == "download_attachment":
        return f"download_attachment[{step.path or '?'} <- {_truncate(_normalize_url(step.source_url) or '?', 40)}]"
    if step.tool == "upload_attachment":
        return f"upload_attachment[{step.path or '?'}]"
    return step.tool


def _normalize_short_text(text: str | None) -> str | None:
    if text is None:
        return None
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) < 4:
        return None
    return compact


def _normalize_path(path: str | None) -> str | None:
    if path is None:
        return None
    compact = path.strip().replace("\\", "/")
    return compact or None


def _normalize_command(command: str | None) -> str | None:
    if command is None:
        return None
    compact = command.strip()
    if not compact:
        return None
    return _truncate(compact, 800)


def _normalize_content(content: str | None, limit: int) -> str | None:
    if content is None:
        return None
    if len(content) <= limit:
        return content
    return content[:limit]


def _normalize_url(value: str | None) -> str | None:
    if value is None:
        return None
    compact = value.strip()
    return compact or None


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"
