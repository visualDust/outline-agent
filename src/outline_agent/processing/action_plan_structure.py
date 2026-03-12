from __future__ import annotations

from pathlib import Path
from typing import Any

from ..planning import UnifiedToolPlan
from ..runtime.tool_runtime import ToolExecutionStep, UploadedAttachment
from ..tools.base import ToolSpec
from .processor_artifacts import find_redundant_upload_paths
from .processor_prompting import preview, truncate
from .processor_types import ExecutedToolRound

_DOCUMENT_TOOLS = {
    "get_current_document",
    "draft_new_document",
    "create_document",
    "draft_document_update",
    "apply_document_update",
}

_READ_ONLY_TOOLS = {
    "get_current_document",
    "list_dir",
    "read_file",
    "download_attachment",
    "extract_text_from_txt",
    "extract_text_from_md",
    "extract_text_from_csv",
    "extract_text_from_pdf",
    "draft_new_document",
    "draft_document_update",
}


def action_plan_steps_preview(proposal: UnifiedToolPlan) -> str:
    if not proposal.steps:
        return "(none)"
    return " ; ".join(preview_action_plan_step(step.tool, step.args) for step in proposal.steps)




def select_next_action_plan_chunk(
    proposal: UnifiedToolPlan,
    *,
    max_chunk_steps: int,
) -> UnifiedToolPlan:
    if not proposal.should_act or not proposal.steps:
        return proposal
    chunk_limit = max(1, int(max_chunk_steps))
    chunked_steps = proposal.steps[:chunk_limit]
    if len(chunked_steps) == len(proposal.steps):
        return proposal
    return UnifiedToolPlan(
        should_act=True,
        goal=proposal.goal,
        steps=chunked_steps,
        final_response_strategy=proposal.final_response_strategy,
    )

def proposal_includes_tool(proposal: UnifiedToolPlan, tool_name: str) -> bool:
    return any(step.tool == tool_name for step in proposal.steps)


def proposal_local_steps(proposal: UnifiedToolPlan) -> list:
    return [step for step in proposal.steps if step_counts_as_tool_execution_name(step.tool)]


def step_counts_as_tool_execution_name(tool_name: str) -> bool:
    return tool_name not in _DOCUMENT_TOOLS


def find_redundant_upload_paths_for_unified_plan(
    *,
    proposal: UnifiedToolPlan,
    uploaded_attachments: list[UploadedAttachment],
    work_dir: Path,
) -> list[str]:
    upload_steps = [step for step in proposal.steps if step.tool == "upload_attachment"]
    if not upload_steps or len(upload_steps) != len(proposal.steps):
        return []

    upload_execution_steps: list[ToolExecutionStep] = []
    for step in upload_steps:
        path = step.args.get("path")
        if not isinstance(path, str):
            return []
        upload_execution_steps.append(ToolExecutionStep(tool="upload_attachment", path=path))
    return find_redundant_upload_paths(upload_execution_steps, uploaded_attachments, work_dir)


def action_plan_fingerprint(proposal: UnifiedToolPlan) -> tuple[tuple[object, ...], ...]:
    items: list[tuple[object, ...]] = []
    for step in proposal.steps:
        normalized_args = tuple(sorted((key, repr(value)) for key, value in step.args.items()))
        items.append((step.tool, *normalized_args))
    return tuple(items)


def find_repeated_plan_without_intervening_state_change(
    plan_fingerprint: tuple[tuple[object, ...], ...],
    executed_rounds: list[ExecutedToolRound],
) -> ExecutedToolRound | None:
    last_match_index: int | None = None
    for index, item in enumerate(executed_rounds):
        if item.status == "applied" and item.plan_fingerprint == plan_fingerprint:
            last_match_index = index

    if last_match_index is None:
        return None

    intervening_rounds = executed_rounds[last_match_index + 1 :]
    if any(item.status == "applied" and item.may_change_state for item in intervening_rounds):
        return None
    return executed_rounds[last_match_index]


def action_plan_is_read_only(proposal: UnifiedToolPlan) -> bool:
    return bool(proposal.steps) and all(step.tool in _READ_ONLY_TOOLS for step in proposal.steps)


def action_plan_may_change_state(proposal: UnifiedToolPlan) -> bool:
    return any(step.tool not in _READ_ONLY_TOOLS for step in proposal.steps)


def preview_action_plan_step(tool_name: str, args: dict[str, Any]) -> str:
    if tool_name in {"list_dir", "read_file", "write_file", "edit_file", "upload_attachment"}:
        return f"{tool_name}[{str(args.get('path') or '?')}]"
    if tool_name == "run_shell":
        return f"run_shell[{truncate(str(args.get('command') or '?'), 60)}]"
    if tool_name == "download_attachment":
        source = args.get("source_url") or args.get("attachment_url") or "?"
        return f"download_attachment[{str(args.get('path') or '?')} <- {truncate(str(source), 40)}]"
    if tool_name == "create_document":
        return f"create_document[{str(args.get('title') or '?')}]"
    if tool_name == "apply_document_update":
        return f"apply_document_update[{str(args.get('title') or '(body update)')}]"
    return tool_name


def describe_action_plan_for_progress(round_index: int, proposal: UnifiedToolPlan) -> str:
    descriptions = [describe_action_step_for_progress(step.tool, step.args) for step in proposal.steps]
    if not descriptions:
        return f"Planned round {round_index}: no actions."
    return f"Planned round {round_index}: {'; '.join(descriptions)}."


def describe_action_step_for_progress(tool_name: str, args: dict[str, Any]) -> str:
    if tool_name == "get_current_document":
        return "load the current Outline document"
    if tool_name == "draft_new_document":
        return "draft a new Outline document"
    if tool_name == "create_document":
        return f"create the new Outline document `{args.get('title') or '?'}`"
    if tool_name == "draft_document_update":
        return "draft an update to the current Outline document"
    if tool_name == "apply_document_update":
        return "apply the drafted update to the current Outline document"
    if tool_name == "list_dir":
        return f"list files in `{args.get('path') or '.'}`"
    if tool_name == "read_file":
        return f"read `{args.get('path') or '?'}`"
    if tool_name == "write_file":
        if args.get("append"):
            return f"append to `{args.get('path') or '?'}`"
        return f"create or update `{args.get('path') or '?'}`"
    if tool_name == "edit_file":
        return f"edit `{args.get('path') or '?'}`"
    if tool_name == "run_shell":
        return f"run `{preview(str(args.get('command') or '?'), 80)}`"
    if tool_name == "download_attachment":
        source = preview(str(args.get("source_url") or args.get("attachment_url") or "attachment"), 80)
        return f"download `{source}` to `{args.get('path') or '?'}`"
    if tool_name == "upload_attachment":
        return f"upload `{args.get('path') or '?'}` back to Outline as an attachment"
    return tool_name


def available_action_tool_specs(
    specs: list[ToolSpec],
    *,
    document_update_enabled: bool,
    tool_use_enabled: bool,
) -> list[ToolSpec]:
    specs_by_name = {spec.name: spec for spec in specs}
    ordered_names: list[str] = []
    if document_update_enabled:
        ordered_names.extend(
            [
                "get_current_document",
                "draft_new_document",
                "create_document",
                "draft_document_update",
                "apply_document_update",
            ]
        )
    if tool_use_enabled:
        ordered_names.extend(
            [
                "list_dir",
                "read_file",
                "write_file",
                "edit_file",
                "run_shell",
                "upload_attachment",
                "download_attachment",
                "extract_text_from_txt",
                "extract_text_from_md",
                "extract_text_from_csv",
                "extract_text_from_pdf",
            ]
        )
    seen: set[str] = set()
    return [
        specs_by_name[name] for name in ordered_names if name in specs_by_name and not (name in seen or seen.add(name))
    ]
