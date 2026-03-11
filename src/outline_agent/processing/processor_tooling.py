from __future__ import annotations

import hashlib
from pathlib import Path

from ..runtime.tool_runtime import ToolExecutionStep, ToolStepResult, UploadedAttachment
from ..utils.markdown_sections import normalize_markdown_text, parse_markdown_sections
from .processor_prompting import preview, truncate
from .processor_types import ExecutedToolRound, ToolRoundSummary


def append_uploaded_attachment_links(reply: str, attachments: list[UploadedAttachment]) -> str:
    unique_items: list[UploadedAttachment] = []
    seen: set[tuple[str, str]] = set()

    for item in attachments:
        url = (item.url or "").strip()
        if not url:
            continue
        name = (item.name or item.path or "download").strip() or "download"
        key = (name, url)
        if key in seen:
            continue
        seen.add(key)
        unique_items.append(item)

    if not unique_items:
        return reply
    missing_items = [item for item in unique_items if (item.url or "") not in reply]
    if not missing_items:
        return reply

    lines = ["Uploaded files:"]
    for item in missing_items:
        url = (item.url or "").strip()
        name = (item.name or item.path or "download").strip() or "download"
        lines.append(f"- [{name}]({url})")

    suffix = "\n\n" + "\n".join(lines)
    return reply.rstrip() + suffix


def tool_plan_fingerprint(steps: list[ToolExecutionStep]) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            step.tool,
            step.path,
            step.source_url,
            step.content,
            step.append,
            step.old_text,
            step.new_text,
            step.command,
            step.recursive,
        )
        for step in steps
    )


def find_redundant_upload_paths(
    steps: list[ToolExecutionStep],
    uploaded_attachments: list[UploadedAttachment],
    work_dir: Path,
) -> list[str]:
    if not steps or any(step.tool != "upload_attachment" for step in steps):
        return []

    uploaded_hashes_by_path: dict[str, set[str]] = {}
    for item in uploaded_attachments:
        path = (item.path or "").strip()
        file_hash = (item.file_hash or "").strip()
        if not path or not file_hash:
            continue
        uploaded_hashes_by_path.setdefault(path, set()).add(file_hash)
    if not uploaded_hashes_by_path:
        return []

    repeated_paths: list[str] = []
    seen: set[str] = set()
    for step in steps:
        path = (step.path or "").strip()
        current_hash = hash_work_dir_file(work_dir, path)
        if not path or not current_hash:
            return []
        if current_hash not in uploaded_hashes_by_path.get(path, set()):
            return []
        if path in seen:
            continue
        seen.add(path)
        repeated_paths.append(path)
    return repeated_paths


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


def tool_plan_is_read_only(steps: list[ToolExecutionStep]) -> bool:
    return bool(steps) and all(step.tool in {"list_dir", "read_file"} for step in steps)


def tool_plan_may_change_state(steps: list[ToolExecutionStep]) -> bool:
    return any(step.tool not in {"list_dir", "read_file"} for step in steps)


def hash_work_dir_file(work_dir: Path, relative_path: str) -> str | None:
    candidate = (work_dir / relative_path).resolve()
    root = work_dir.resolve()
    if candidate != root and root not in candidate.parents:
        return None
    if not candidate.exists() or not candidate.is_file():
        return None

    digest = hashlib.sha256()
    with candidate.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def append_status_preview(base: str | None, addition: str | None) -> str | None:
    if addition is None:
        return base
    if base is None:
        return addition
    return f"{base} ; {addition}"


def append_status_context(base: str | None, addition: str | None) -> str | None:
    if addition is None:
        return base
    if base is None:
        return addition
    return f"{base}\n{addition}"


def preview_registered_attachments(attachments: list[UploadedAttachment]) -> str | None:
    if not attachments:
        return None
    names = ", ".join((item.name or item.path or "download").strip() or "download" for item in attachments)
    return f"registered uploaded files in document: {names}"


def format_registered_attachment_context(attachments: list[UploadedAttachment]) -> str | None:
    if not attachments:
        return None

    lines = ["- artifact link registration: applied"]
    for item in attachments:
        url = (item.url or "").strip()
        if not url:
            continue
        name = (item.name or item.path or "download").strip() or "download"
        lines.append(f"  registered_file: {name} -> {url}")
    return "\n".join(lines)


def register_uploaded_attachments_in_document_text(
    document_text: str | None,
    attachments: list[UploadedAttachment],
) -> tuple[str | None, list[UploadedAttachment]]:
    if document_text is None:
        return None, []

    normalized = normalize_markdown_text(document_text) or ""
    unique_items: list[UploadedAttachment] = []
    seen: set[tuple[str, str]] = set()

    for item in attachments:
        url = (item.url or "").strip()
        if not url:
            continue
        name = (item.name or item.path or "download").strip() or "download"
        key = (name, url)
        if key in seen:
            continue
        seen.add(key)
        unique_items.append(item)

    registered_items = [item for item in unique_items if not document_already_references_attachment(normalized, item)]
    if not registered_items:
        return None, []

    bullet_lines = [
        f"- [{(item.name or item.path or 'download').strip() or 'download'}]({(item.url or '').strip()})"
        for item in registered_items
    ]
    if not bullet_lines:
        return None, []

    updated = insert_artifact_lines_into_document(normalized, bullet_lines)
    return updated, registered_items


def document_already_references_attachment(document_text: str, attachment: UploadedAttachment) -> bool:
    url = (attachment.url or "").strip()
    if url and url in document_text:
        return True
    attachment_id = (attachment.attachment_id or "").strip()
    return bool(attachment_id and attachment_id in document_text)


def insert_artifact_lines_into_document(document_text: str, bullet_lines: list[str]) -> str:
    artifact_section = find_artifact_section(document_text)
    block = "\n".join(bullet_lines)

    if artifact_section is not None:
        before = document_text[: artifact_section.end].rstrip("\n")
        after = document_text[artifact_section.end :].lstrip("\n")
        updated = before + "\n\n" + block
        if after:
            updated += "\n\n" + after
        return updated

    if not document_text.strip():
        return "## Uploaded Artifacts\n\n" + block
    return document_text.rstrip("\n") + "\n\n## Uploaded Artifacts\n\n" + block


def find_artifact_section(document_text: str):
    section_titles = {"artifacts", "uploaded artifacts", "generated artifacts"}
    for section in parse_markdown_sections(document_text):
        if not section.heading_path:
            continue
        heading = section.heading_path[-1].strip().casefold()
        if heading in section_titles:
            return section
    return None


def format_tool_preview(round_summaries: list[ToolRoundSummary]) -> str | None:
    parts = [f"round {item.round_index}: {item.preview}" for item in round_summaries if item.preview]
    return " ; ".join(parts) if parts else None


def format_tool_context(round_summaries: list[ToolRoundSummary]) -> str | None:
    sections: list[str] = []
    for item in round_summaries:
        if not item.context:
            continue
        sections.append(f"Round {item.round_index}:\n{item.context}")
    return "\n\n".join(sections) if sections else None


def preview_tool_step(step: ToolExecutionStep) -> str:
    if step.tool == "list_dir":
        return f"list_dir[{step.path or '.'}]"
    if step.tool == "read_file":
        return f"read_file[{step.path or '?'}]"
    if step.tool == "write_file":
        return f"write_file[{step.path or '?'}]"
    if step.tool == "edit_file":
        return f"edit_file[{step.path or '?'}]"
    if step.tool == "run_shell":
        return f"run_shell[{truncate((step.command or '').strip() or '?', 60)}]"
    if step.tool == "download_attachment":
        return (
            f"download_attachment[{step.path or '?'} <- "
            f"{truncate((step.source_url or '').strip() or '?', 40)}]"
        )
    if step.tool == "upload_attachment":
        return f"upload_attachment[{step.path or '?'}]"
    return step.tool


def describe_tool_plan_for_progress(round_index: int, steps: list[ToolExecutionStep]) -> str:
    descriptions = [describe_tool_step_for_progress(step) for step in steps]
    if not descriptions:
        return f"Planned round {round_index}: no local actions."
    return f"Planned round {round_index}: {'; '.join(descriptions)}."


def describe_tool_step_for_progress(step: ToolExecutionStep) -> str:
    if step.tool == "list_dir":
        return f"list files in `{step.path or '.'}`"
    if step.tool == "read_file":
        return f"read `{step.path or '?'}`"
    if step.tool == "write_file":
        if step.append:
            return f"append to `{step.path or '?'}`"
        return f"create or update `{step.path or '?'}`"
    if step.tool == "edit_file":
        return f"edit `{step.path or '?'}`"
    if step.tool == "run_shell":
        return f"run `{progress_inline(step.command or '?', limit=80)}`"
    if step.tool == "download_attachment":
        source = progress_inline(step.source_url or "attachment", limit=80)
        return f"download `{source}` to `{step.path or '?'}`"
    if step.tool == "upload_attachment":
        return f"upload `{step.path or '?'}` back to Outline as an attachment"
    return step.tool


def describe_tool_result_for_progress(result: ToolStepResult) -> str:
    if result.tool == "list_dir":
        target = result.target or "."
        preview_tail = extract_result_tail(result.summary)
        if preview_tail:
            return f"Finished: listed `{target}` → {progress_inline(preview_tail, limit=120)}."
        return f"Finished: listed `{target}`."

    if result.tool == "read_file":
        target = result.target or "file"
        if result.stdout:
            return f"Finished: read `{target}` → `{progress_inline(result.stdout, limit=100)}`."
        return f"Finished: read `{target}`."

    if result.tool == "write_file":
        target = result.target or "file"
        action = "appended to" if result.summary.startswith("append_file[") else "created or updated"
        return f"Finished: {action} `{target}`."

    if result.tool == "edit_file":
        target = result.target or "file"
        return f"Finished: edited `{target}`."

    if result.tool == "run_shell":
        command = progress_inline(result.target or "command", limit=80)
        if result.ok:
            if result.stdout:
                return f"Finished: ran `{command}` → output `{progress_inline(result.stdout, limit=100)}`."
            if result.stderr:
                return f"Finished: ran `{command}` → stderr `{progress_inline(result.stderr, limit=100)}`."
            return f"Finished: ran `{command}`."

        details: list[str] = []
        if result.exit_code is not None:
            details.append(f"exit {result.exit_code}")
        if result.stderr:
            details.append(f"stderr `{progress_inline(result.stderr, limit=80)}`")
        elif result.stdout:
            details.append(f"output `{progress_inline(result.stdout, limit=80)}`")
        detail_text = f" ({'; '.join(details)})" if details else ""
        return f"Stopped: running `{command}` failed{detail_text}."

    if result.tool == "download_attachment":
        target = result.target or "file"
        if result.ok:
            preview_tail = extract_result_tail(result.summary)
            if preview_tail:
                return f"Finished: downloaded attachment to `{target}` → `{progress_inline(preview_tail, limit=100)}`."
            return f"Finished: downloaded attachment to `{target}`."
        return f"Stopped: downloading attachment to `{target}` failed."

    if result.tool == "upload_attachment":
        target = result.target or "file"
        if result.ok:
            return f"Finished: uploaded `{target}` back to the Outline document as an attachment."
        return f"Stopped: uploading `{target}` as an Outline attachment failed."

    return f"Finished: {result.summary}."


def describe_round_stop_for_progress(round_index: int, status: str) -> str:
    if status == "failed":
        return f"Stopped in round {round_index}: one of the requested local actions failed."
    if status == "blocked":
        return f"Stopped in round {round_index}: I couldn't safely continue the requested local actions."
    return f"Stopped in round {round_index}: local actions ended with status `{status}`."


def progress_comment_headline(
    status: str,
    *,
    round_index: int | None = None,
    total_rounds: int | None = None,
) -> str:
    if status == "thinking":
        return "Thinking…"
    if status == "running":
        if round_index is not None and total_rounds is not None:
            return (
                "Working on it — I'm carrying out the requested local workspace actions "
                f"(round {round_index} of {total_rounds})."
            )
        return "Working on it — I'm carrying out the requested local workspace actions."
    if status == "applied":
        return "Done — I finished the requested local workspace actions."
    if status == "failed":
        return "Stopped — one of the requested local workspace actions failed."
    if status == "blocked":
        return "Stopped — I couldn't safely continue the requested local workspace actions."
    if status == "stopped-max-rounds":
        return "Paused — I reached the configured limit for local workspace action rounds."
    return f"Status update — local workspace actions are now `{status}`."


def progress_comment_footer(status: str) -> str:
    return ""


def format_progress_comment_text(
    *,
    headline: str,
    status: str,
    recent_actions: list[str],
    max_chars: int = 900,
) -> str:
    actions = [preview(item, limit=180) for item in recent_actions if item.strip()]
    footer = progress_comment_footer(status)
    while True:
        lines = [headline]
        if actions:
            lines.extend(["", "Recent progress:"])
            lines.extend(f"  - {item}" for item in actions)
        if footer:
            lines.extend(["", footer])
        text = "\n".join(lines).strip()
        if len(text) <= max_chars or not actions:
            return truncate(text, max_chars)
        actions = actions[1:]


def extract_result_tail(summary: str) -> str | None:
    if " -> " not in summary:
        return None
    _, _, tail = summary.partition(" -> ")
    return tail.strip() or None


def progress_inline(text: str, limit: int) -> str:
    compact = preview(text, limit=limit).replace("`", "'")
    return compact.strip()


def resolve_dry_run_reason(
    document_creation_status: str | None,
    document_update_status: str | None,
    tool_execution_status: str | None,
) -> str:
    creation_planned = document_creation_status == "planned-dry-run"
    document_planned = document_update_status == "planned-dry-run"
    tool_planned = tool_execution_status == "planned-dry-run"
    if creation_planned and document_planned and tool_planned:
        return "document-creation-document-update-and-tool-use-planned-and-reply-generated"
    if creation_planned and document_planned:
        return "document-creation-and-document-update-planned-and-reply-generated"
    if creation_planned and tool_planned:
        return "document-creation-and-tool-use-planned-and-reply-generated"
    if creation_planned:
        return "document-creation-planned-and-reply-generated"
    if document_planned and tool_planned:
        return "document-update-and-tool-use-planned-and-reply-generated"
    if document_planned:
        return "document-update-planned-and-reply-generated"
    if tool_planned:
        return "tool-use-planned-and-reply-generated"
    return "reply-generated-without-posting"


def resolve_success_action(
    document_creation_status: str | None,
    document_update_status: str | None,
    tool_execution_status: str | None,
) -> str:
    document_created = document_creation_status == "applied"
    document_applied = document_update_status == "applied"
    tools_applied = tool_execution_status == "applied"
    tool_attempted = tool_execution_status in {"applied", "failed", "stopped-max-rounds", "blocked"}
    if document_created and document_applied and tools_applied:
        return "created-document-edited-used-tools-and-replied"
    if document_created and document_applied:
        return "created-document-edited-and-replied"
    if document_created and tools_applied:
        return "created-document-used-tools-and-replied"
    if document_created and tool_attempted:
        return "created-document-tool-attempted-and-replied"
    if document_created:
        return "created-document-and-replied"
    if document_applied and tools_applied:
        return "edited-used-tools-and-replied"
    if document_applied and tool_attempted:
        return "edited-tool-attempted-and-replied"
    if document_applied:
        return "edited-and-replied"
    if tools_applied:
        return "used-tools-and-replied"
    if tool_attempted:
        return "tool-attempted-and-replied"
    return "replied"


def resolve_success_reason(
    document_creation_status: str | None,
    document_update_status: str | None,
    tool_execution_status: str | None,
) -> str:
    document_created = document_creation_status == "applied"
    document_applied = document_update_status == "applied"
    tools_applied = tool_execution_status == "applied"
    if document_created and tool_execution_status == "failed":
        return "document-created-tool-execution-failed-and-replied"
    if document_created and tool_execution_status == "blocked":
        return "document-created-tool-planning-blocked-and-replied"
    if document_created and tool_execution_status == "stopped-max-rounds":
        return "document-created-tool-loop-stopped-at-max-rounds-and-replied"
    if document_created and document_applied and tools_applied:
        return "document-created-document-updated-tools-executed-and-replied"
    if document_created and document_applied:
        return "document-created-document-updated-and-replied"
    if document_created and tools_applied:
        return "document-created-tools-executed-and-replied"
    if document_created:
        return "document-created-and-replied"
    if document_applied and tool_execution_status == "failed":
        return "document-updated-tool-execution-failed-and-replied"
    if document_applied and tool_execution_status == "blocked":
        return "document-updated-tool-planning-blocked-and-replied"
    if document_applied and tool_execution_status == "stopped-max-rounds":
        return "document-updated-tool-loop-stopped-at-max-rounds-and-replied"
    if tool_execution_status == "failed":
        return "tool-execution-failed-and-replied"
    if tool_execution_status == "blocked":
        return "tool-planning-blocked-and-replied"
    if tool_execution_status == "stopped-max-rounds":
        return "tool-loop-stopped-at-max-rounds-and-replied"
    if document_applied and tools_applied:
        return "document-updated-tools-executed-and-replied"
    if document_applied:
        return "document-updated-and-replied"
    if tools_applied:
        return "tools-executed-and-replied"
    return "reply-posted"
