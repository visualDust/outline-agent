from __future__ import annotations

from ..clients.model_client import ModelClient, ModelInputImage
from ..clients.outline_models import OutlineCollection, OutlineDocument
from ..core.config import AppSettings
from ..runtime.tool_runtime import describe_work_dir
from ..state.workspace import ThreadWorkspace
from ..tools import ToolSpec
from ..utils.attachment_context import AttachmentContextItem, format_attachment_context_for_prompt
from ..utils.json_utils import JsonExtractionError, extract_json_object
from .tool_plan_schema import UnifiedToolPlan, sanitize_unified_tool_plan

UNIFIED_TOOL_PLANNER_SYSTEM_PROMPT = """You plan bounded tool use for an Outline agent.

You may use local sandbox tools and Outline document tools.

Return strict JSON only with this schema:
{
  "should_act": true,
  "goal": "short goal",
  "steps": [
    {"tool": "tool_name", "args": {"key": "value"}}
  ],
  "final_response_strategy": "brief_confirmation|detailed_summary|ask_for_clarification"
}

Rules:
- Prefer no action when a normal text reply is enough
- Plan only the next smallest executable chunk, not the whole workflow
- Prefer 1 step; use 2 steps only when the second directly follows from the first
- Trust the outer loop to replan after each executed chunk
- Use only the provided tool names
- Keep the plan within the stated step budget
- Use relative paths only for workspace paths
- Prefer read steps before write steps unless a direct write is clearly needed
- Use `draft_document_update` before `apply_document_update`
- Use `draft_new_document` before `create_document` when drafting from the current request and document context
- `apply_document_update` only needs `title`, `text`, or `content`; never use or reference `draft_id`
- `create_document` only needs `title`, `text`, `content`
  plus optional `collection_id`, `parent_document_id`, `publish`;
  never use or reference `draft_id`
- After a successful draft step, you may omit document fields and let the executor auto-fill them from the latest draft
- Use `download_attachment` before local extraction tools when the source is an Outline attachment
- For `download_attachment`, always provide both `path` and `source_url`/`attachment_url`
- When attachment candidates are provided in the prompt, copy their `source_url`
  and suggested `path` exactly instead of inventing values
- For PDF or attachment analysis tasks, prefer a shell-first local workflow:
  `download_attachment` -> `run_shell` -> `read_file`/local inspection -> document write/update
- Prefer `run_shell` over `extract_text_from_pdf` when the task depends on reliable PDF extraction,
  multi-step fallback, or format conversion; treat `extract_text_from_pdf` as a best-effort shortcut
- Do not draft or apply a document update until you have enough reliable attachment content
  available from local files or prior tool observations
- Use `upload_attachment` only after the file already exists in the thread work dir
- Do not upload the same file more than once in the same turn unless the file was changed afterwards
- Do not repeat the same inspection-only plan if no later step changed workspace or document state
- Use template references like {{steps.1.data.text}} only when a later step needs an earlier result
- Do not invent unavailable files, IDs, URLs, or command output
- If prior rounds failed, inspect the observed error details and choose
  the smallest useful recovery step or fallback plan
- When a shell or file step fails, use the observed exit code, stdout,
  stderr, and work dir state to decide the next step
- Do not give up immediately after one failed step if the observed failure suggests a concrete recovery or fallback path
- Use structured workspace observations from prior rounds to see which files or artifacts now exist before replanning
"""


class UnifiedToolPlanner:
    def __init__(self, settings: AppSettings, model_client: ModelClient):
        self.settings = settings
        self.model_client = model_client

    async def propose_plan(
        self,
        *,
        available_tools: list[ToolSpec],
        thread_workspace: ThreadWorkspace,
        collection: OutlineCollection | None,
        document: OutlineDocument,
        user_comment: str,
        comment_context: str,
        related_documents_context: str | None,
        current_round: int,
        prior_round_summaries: list[str],
        prior_round_observations: list[str] | None = None,
        available_attachment_context: list[AttachmentContextItem] | None = None,
        current_comment_image_count: int = 0,
        input_images: list[ModelInputImage] | None = None,
    ) -> UnifiedToolPlan:
        user_prompt = self._build_user_prompt(
            available_tools=available_tools,
            thread_workspace=thread_workspace,
            collection=collection,
            document=document,
            user_comment=user_comment,
            comment_context=comment_context,
            related_documents_context=related_documents_context,
            current_round=current_round,
            prior_round_summaries=prior_round_summaries,
            prior_round_observations=prior_round_observations or [],
            available_attachment_context=available_attachment_context or [],
            current_comment_image_count=current_comment_image_count,
        )
        raw = await self._generate_with_optional_images(
            UNIFIED_TOOL_PLANNER_SYSTEM_PROMPT,
            user_prompt,
            input_images=input_images or [],
        )
        try:
            payload = extract_json_object(raw)
        except JsonExtractionError:
            return UnifiedToolPlan(should_act=False)
        try:
            plan = UnifiedToolPlan.model_validate(payload)
            return sanitize_unified_tool_plan(
                plan,
                allowed_tools={tool.name for tool in available_tools},
                max_steps=self.settings.tool_execution_max_steps,
            )
        except Exception:
            return UnifiedToolPlan(should_act=False)

    def preview(self, proposal: UnifiedToolPlan) -> str | None:
        if not proposal.should_act:
            return None
        parts: list[str] = []
        if proposal.goal:
            parts.append(f"goal={proposal.goal}")
        if proposal.steps:
            parts.append("steps=" + " | ".join(_preview_step(step.tool, step.args) for step in proposal.steps))
        return " ; ".join(parts) if parts else None

    def format_reply_context(
        self,
        *,
        work_dir: str,
        proposal: UnifiedToolPlan,
        status: str,
        step_summaries: list[str] | None = None,
    ) -> str | None:
        if not proposal.should_act:
            return None
        lines = [f"- status: {status}", f"- work_dir: {work_dir}"]
        if proposal.goal:
            lines.append(f"- goal: {proposal.goal}")
        if step_summaries:
            for index, item in enumerate(step_summaries, start=1):
                lines.append(f"- step {index}: {item}")
        else:
            planned_steps = " ; ".join(_preview_step(step.tool, step.args) for step in proposal.steps)
            lines.append(f"- planned steps: {planned_steps}")
        return "\n".join(lines)

    def _build_user_prompt(
        self,
        *,
        available_tools: list[ToolSpec],
        thread_workspace: ThreadWorkspace,
        collection: OutlineCollection | None,
        document: OutlineDocument,
        user_comment: str,
        comment_context: str,
        related_documents_context: str | None,
        current_round: int,
        prior_round_summaries: list[str],
        prior_round_observations: list[str],
        available_attachment_context: list[AttachmentContextItem],
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
        prior_rounds_section = _format_prior_rounds(prior_round_summaries)
        prior_round_observation_section = _format_prior_rounds(prior_round_observations)
        tool_catalog = (
            "\n".join(_format_tool_catalog_entry(tool) for tool in available_tools) or "- (none)"
        )
        attachment_context_section = ""
        formatted_attachment_context = format_attachment_context_for_prompt(available_attachment_context)
        if formatted_attachment_context:
            attachment_context_section = f"{formatted_attachment_context}\n\n"

        current_comment_image_section = ""
        if current_comment_image_count > 0:
            noun = "image" if current_comment_image_count == 1 else "images"
            current_comment_image_section = (
                f"The latest user comment also includes {current_comment_image_count} embedded {noun}. "
                "If image inputs are attached to this request, use them when deciding the plan.\n\n"
            )
        related_documents_section = ""
        if related_documents_context:
            related_documents_section = f"Related documents in this collection:\n{related_documents_context}\n\n"
        return (
            f"Collection: {collection_name}\n"
            f"Collection ID: {document.collection_id or '(unknown)'}\n"
            f"Document Title: {document.title or '(unknown)'}\n"
            f"Current Outline document ID: {document.id}\n"
            f"Thread ID: {thread_workspace.thread_id}\n"
            f"Thread workspace: {thread_workspace.root_dir}\n"
            f"Thread work dir: {thread_workspace.work_dir}\n\n"
            f"Current planning round: {current_round} of {self.settings.tool_execution_max_rounds}\n\n"
            f"Planner step budget: {self.settings.tool_execution_max_steps}\n"
            f"Execution chunk size: {self.settings.tool_execution_chunk_size}\n\n"
            "Available tools:\n"
            f"{tool_catalog}\n\n"
            "Persisted thread context:\n"
            f"{thread_context or '(no thread context)'}\n\n"
            "Action execution history in this turn:\n"
            f"{prior_rounds_section}\n\n"
            "Structured observations from prior rounds:\n"
            f"{prior_round_observation_section}\n\n"
            "Current work dir inventory:\n"
            f"{work_dir_snapshot}\n\n"
            "Relevant comment context:\n"
            f"{comment_context or '(no comment context)'}\n\n"
            f"{attachment_context_section}"
            f"{related_documents_section}"
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


def _preview_step(tool: str, args: dict[str, object]) -> str:
    if tool in {"list_dir", "read_file", "write_file", "edit_file", "upload_attachment"}:
        return f"{tool}[{str(args.get('path') or '?')}]"
    if tool == "run_shell":
        return f"run_shell[{_truncate(str(args.get('command') or '?'), 60)}]"
    if tool == "download_attachment":
        source = args.get("source_url") or args.get("attachment_url") or "?"
        return f"download_attachment[{str(args.get('path') or '?')} <- {_truncate(str(source), 40)}]"
    if tool == "create_document":
        return f"create_document[{str(args.get('title') or '?')}]"
    if tool == "apply_document_update":
        return f"apply_document_update[{str(args.get('title') or '(body update)')}]"
    return tool


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _format_prior_rounds(prior_round_summaries: list[str]) -> str:
    if not prior_round_summaries:
        return "(none yet)"
    sections: list[str] = []
    for item in prior_round_summaries:
        lines = [line.rstrip() for line in item.splitlines() if line.strip()]
        if not lines:
            continue
        head, *tail = lines
        block = [f"- {head}"]
        block.extend(f"  {line}" for line in tail)
        sections.append("\n".join(block))
    return "\n\n".join(sections) if sections else "(none yet)"


def _format_tool_catalog_entry(tool: ToolSpec) -> str:
    entry = f"- {tool.name}: {tool.description}"
    if tool.when_to_use:
        entry += f" When to use: {tool.when_to_use}"
    required = _required_arg_names(tool.input_schema)
    if required:
        entry += f" Required args: {', '.join(required)}"
    return entry


def _required_arg_names(schema: dict[str, object]) -> list[str]:
    required = schema.get("required") if isinstance(schema, dict) else None
    if not isinstance(required, list):
        return []
    return [str(item) for item in required if isinstance(item, str) and item.strip()]
