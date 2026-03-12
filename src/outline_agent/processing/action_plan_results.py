from __future__ import annotations

from pathlib import Path
from typing import Any

from ..clients.outline_models import OutlineDocument
from ..planning import UnifiedToolPlan
from ..runtime.tool_runtime import ToolExecutionReport, ToolStepResult, UploadedAttachment, describe_work_dir
from .action_plan_structure import proposal_includes_tool, step_counts_as_tool_execution_name
from .processor_progress import describe_tool_result_for_progress
from .processor_prompting import preview


def describe_action_result_for_progress(tool_name: str, args: dict[str, Any], result: Any) -> str:
    step_result = tool_result_to_step_result(tool_name, args, result)
    return describe_tool_result_for_progress(step_result)


def plan_execution_summary_to_tool_report(summary: Any) -> ToolExecutionReport:
    step_results = [
        tool_result_to_step_result(step.tool, step.args, step.result)
        for step in summary.steps
        if step_counts_as_tool_execution_name(step.tool)
    ]
    if summary.status == "failed":
        return ToolExecutionReport(status="failed", step_results=step_results, error=summary.error)
    return ToolExecutionReport(
        status="applied" if summary.status == "applied" else summary.status,
        step_results=step_results,
    )


def format_round_observation_for_planner(
    *,
    round_index: int,
    plan_preview: str,
    summary: Any,
    work_dir: Path,
    max_work_dir_entries: int,
) -> str:
    lines = [
        f"round {round_index} (status={summary.status}): {plan_preview or '(none)'}",
        f"planned_steps={plan_preview or '(none)'}",
    ]
    if summary.error:
        lines.append(f"error={preview(str(summary.error), 240)}")

    if not summary.steps:
        lines.append("observations=(none)")
        return "\n".join(lines)

    for step in summary.steps:
        step_result = tool_result_to_step_result(step.tool, step.args, step.result)
        step_lines = [
            f"step {step.index}: tool={step.tool}; ok={step_result.ok}; summary={preview(step_result.summary, 200)}"
        ]
        if step_result.target:
            step_lines.append(f"  target={preview(step_result.target, 160)}")
        if step_result.exit_code is not None:
            step_lines.append(f"  exit_code={step_result.exit_code}")
        if step_result.stdout:
            step_lines.append(f"  stdout={preview(step_result.stdout, 240)}")
        if step_result.stderr:
            step_lines.append(f"  stderr={preview(step_result.stderr, 240)}")
        if step.result.error:
            step_lines.append(f"  error={preview(step.result.error, 240)}")
        if step_result.attachment and step_result.attachment.url:
            step_lines.append(
                "  uploaded_attachment="
                f"{preview(step_result.attachment.name or step_result.attachment.path, 120)}"
                f" -> {preview(step_result.attachment.url, 200)}"
            )
        lines.extend(step_lines)
    workspace_observation = _format_workspace_observation(
        summary=summary,
        work_dir=work_dir,
        max_entries=max_work_dir_entries,
    )
    if workspace_observation:
        lines.append(workspace_observation)
    return "\n".join(lines)


def _format_workspace_observation(
    *,
    summary: Any,
    work_dir: Path,
    max_entries: int,
) -> str | None:
    inventory = describe_work_dir(work_dir, max_entries=max_entries)
    path_lines = _format_step_path_observations(summary=summary, work_dir=work_dir)
    lines = [
        "workspace_after_round:",
        *(f"  {line}" for line in inventory.splitlines() if line.strip()),
    ]
    if path_lines:
        lines.append("observed_paths:")
        lines.extend(f"  {line}" for line in path_lines)
    return "\n".join(lines)


def _format_step_path_observations(
    *,
    summary: Any,
    work_dir: Path,
) -> list[str]:
    observed: list[str] = []
    seen: set[str] = set()
    for step in summary.steps:
        candidate = _extract_step_relative_path(step.tool, step.args, step.result)
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        description = _describe_work_dir_path(work_dir, candidate)
        if description:
            observed.append(description)
    return observed[:6]


def _extract_step_relative_path(tool_name: str, args: dict[str, Any], result: Any) -> str | None:
    if tool_name not in {
        "list_dir",
        "read_file",
        "write_file",
        "edit_file",
        "download_attachment",
        "upload_attachment",
    }:
        return None
    for value in (
        result.data.get("path") if isinstance(result.data, dict) else None,
        result.data.get("target") if isinstance(result.data, dict) else None,
        args.get("path"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _describe_work_dir_path(work_dir: Path, relative_path: str) -> str | None:
    root = work_dir.resolve()
    candidate = (root / relative_path).resolve()
    if candidate != root and root not in candidate.parents:
        return None
    if not candidate.exists():
        return f"{relative_path}: missing"
    kind = "dir" if candidate.is_dir() else "file"
    if candidate.is_file():
        try:
            size = candidate.stat().st_size
        except OSError:
            return f"{relative_path}: {kind}"
        return f"{relative_path}: {kind}, {size} bytes"
    return f"{relative_path}: {kind}"


def tool_result_to_step_result(tool_name: str, args: dict[str, Any], result: Any) -> ToolStepResult:
    attachment: UploadedAttachment | None = None
    for artifact in result.artifacts:
        if artifact.get("type") != "uploaded_attachment":
            continue
        attachment = UploadedAttachment(
            path=str(artifact.get("path") or args.get("path") or ""),
            name=str(artifact.get("name") or args.get("path") or "download"),
            url=artifact.get("url") if isinstance(artifact.get("url"), str) else None,
            attachment_id=(artifact.get("attachment_id") if isinstance(artifact.get("attachment_id"), str) else None),
            file_hash=artifact.get("file_hash") if isinstance(artifact.get("file_hash"), str) else None,
        )
        break

    data = result.data if isinstance(result.data, dict) else {}
    target = data.get("target") if isinstance(data.get("target"), str) else None
    stdout = data.get("stdout") if isinstance(data.get("stdout"), str) else None
    stderr = data.get("stderr") if isinstance(data.get("stderr"), str) else None
    exit_code = data.get("exit_code") if isinstance(data.get("exit_code"), int) else None
    if target is None:
        if tool_name in {
            "list_dir",
            "read_file",
            "write_file",
            "edit_file",
            "download_attachment",
            "upload_attachment",
        }:
            target = args.get("path") if isinstance(args.get("path"), str) else None
        elif tool_name == "run_shell":
            target = args.get("command") if isinstance(args.get("command"), str) else None
    return ToolStepResult(
        tool=tool_name,
        ok=result.ok,
        summary=result.summary,
        target=target,
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        attachment=attachment,
    )


def tool_result_data_text(data: dict[str, Any] | None, key: str) -> str | None:
    if not isinstance(data, dict):
        return None
    value = data.get(key)
    return value if isinstance(value, str) and value.strip() else None


def tool_result_data_lines(data: dict[str, Any] | None, key: str) -> list[str]:
    if not isinstance(data, dict):
        return []
    value = data.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def document_failure_status(
    *,
    proposal: UnifiedToolPlan,
    tool_name: str,
    current_status: str | None,
    report_status: str,
) -> str | None:
    if current_status is not None:
        return current_status
    if proposal_includes_tool(proposal, tool_name):
        return report_status
    return None


def document_failure_preview(
    *,
    proposal: UnifiedToolPlan,
    tool_name: str,
    current_preview: str | None,
    report: ToolExecutionReport,
    failed_step: Any,
) -> str | None:
    if current_preview is not None:
        return current_preview
    if not proposal_includes_tool(proposal, tool_name):
        return current_preview
    if failed_step is not None and failed_step.tool == tool_name:
        return failed_step.result.summary
    return report.error or report.preview


def document_failure_context(
    *,
    proposal: UnifiedToolPlan,
    tool_name: str,
    current_context: str | None,
    status: str | None,
    draft_data: dict[str, Any] | None,
    report: ToolExecutionReport,
    failed_step: Any,
) -> str | None:
    if current_context is not None:
        return current_context
    if status is None or not proposal_includes_tool(proposal, tool_name):
        return current_context

    lines = [f"- status: {status}"]
    decision = tool_result_data_text(draft_data, "decision")
    summary = tool_result_data_text(draft_data, "summary")
    reason = tool_result_data_text(draft_data, "reason")
    title = tool_result_data_text(draft_data, "title")
    operation_previews = tool_result_data_lines(draft_data, "operation_previews")
    if decision:
        lines.append(f"- decision: {decision}")
    if summary:
        lines.append(f"- summary: {summary}")
    if reason:
        lines.append(f"- reason: {reason}")
    if title and tool_name == "create_document":
        lines.append(f"- planned title: {title}")
    if operation_previews:
        lines.append("- operations:")
        lines.extend(f"  - {item}" for item in operation_previews)

    failure_summary = report.error or report.preview or "Action execution failed."
    if failed_step is not None and failed_step.tool == tool_name:
        failure_summary = failed_step.result.summary
    lines.append(f"- failure: {failure_summary}")
    return "\n".join(lines)


def build_document_creation_success(
    *,
    result_data: dict[str, Any],
    fallback_collection_id: str | None,
    draft_data: dict[str, Any] | None,
    report_status: str,
    result_summary: str,
) -> tuple[OutlineDocument, str, str, str]:
    created_document = OutlineDocument(
        id=str(result_data.get("document_id") or ""),
        title=result_data.get("title") if isinstance(result_data.get("title"), str) else None,
        collection_id=(
            result_data.get("collection_id")
            if isinstance(result_data.get("collection_id"), str)
            else fallback_collection_id
        ),
        url=result_data.get("url") if isinstance(result_data.get("url"), str) else None,
        text=result_data.get("text") if isinstance(result_data.get("text"), str) else None,
    )
    status = "applied" if report_status == "applied" else report_status
    draft_summary = tool_result_data_text(draft_data, "summary")
    draft_reason = tool_result_data_text(draft_data, "reason")
    draft_decision = tool_result_data_text(draft_data, "decision")
    preview_parts: list[str] = []
    created_title = created_document.title or tool_result_data_text(draft_data, "title")
    if created_title:
        preview_parts.append(f"title={created_title}")
    if draft_summary:
        preview_parts.append(f"summary={draft_summary}")
    if created_document.url:
        preview_parts.append(f"url={created_document.url}")
    preview_text = " ; ".join(preview_parts) or result_summary

    lines = [f"- status: {status}"]
    if draft_decision:
        lines.append(f"- decision: {draft_decision}")
    if draft_summary:
        lines.append(f"- summary: {draft_summary}")
    if draft_reason:
        lines.append(f"- reason: {draft_reason}")
    lines.append(f"- created title: {created_document.title or '(unknown)'}")
    lines.append(f"- created document id: {created_document.id}")
    if created_document.url:
        lines.append(f"- created document url: {created_document.url}")
    context = "\n".join(lines)
    return created_document, status, preview_text, context


def build_document_update_success(
    *,
    current_document: OutlineDocument,
    result_data: dict[str, Any],
    draft_data: dict[str, Any] | None,
    report_status: str,
    result_summary: str,
) -> tuple[OutlineDocument, str, str, str]:
    updated_title = result_data.get("title") if isinstance(result_data.get("title"), str) else current_document.title
    updated_text = result_data.get("text") if isinstance(result_data.get("text"), str) else current_document.text
    updated_document = OutlineDocument(
        id=current_document.id,
        title=updated_title,
        collection_id=current_document.collection_id,
        url=current_document.url,
        text=updated_text,
    )
    status = "applied" if report_status == "applied" else report_status
    draft_summary = tool_result_data_text(draft_data, "summary")
    draft_reason = tool_result_data_text(draft_data, "reason")
    draft_decision = tool_result_data_text(draft_data, "decision")
    operation_previews = tool_result_data_lines(draft_data, "operation_previews")
    preview_parts = [part for part in [draft_summary] if part]
    preview_parts.extend(operation_previews)
    preview_text = " ; ".join(preview_parts) or result_summary

    lines = [f"- status: {status}"]
    if draft_decision:
        lines.append(f"- decision: {draft_decision}")
    if draft_summary:
        lines.append(f"- summary: {draft_summary}")
    if draft_reason:
        lines.append(f"- reason: {draft_reason}")
    if updated_document.title:
        lines.append(f"- updated title: {updated_document.title}")
    if operation_previews:
        lines.append("- operations:")
        lines.extend(f"  - {item}" for item in operation_previews)
    context = "\n".join(lines)
    return updated_document, status, preview_text, context


def uploaded_attachments_from_artifacts(artifacts: list[dict[str, Any]]) -> list[UploadedAttachment]:
    attachments: list[UploadedAttachment] = []
    for artifact in artifacts:
        if artifact.get("type") != "uploaded_attachment":
            continue
        attachments.append(
            UploadedAttachment(
                path=str(artifact.get("path") or ""),
                name=str(artifact.get("name") or artifact.get("path") or "download"),
                url=artifact.get("url") if isinstance(artifact.get("url"), str) else None,
                attachment_id=(
                    artifact.get("attachment_id") if isinstance(artifact.get("attachment_id"), str) else None
                ),
                file_hash=artifact.get("file_hash") if isinstance(artifact.get("file_hash"), str) else None,
            )
        )
    return attachments
