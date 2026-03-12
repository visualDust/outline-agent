from __future__ import annotations


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
    document_creation_attempted = document_creation_status in {
        "applied",
        "failed",
        "blocked",
        "stopped-max-rounds",
    }
    document_applied = document_update_status == "applied"
    document_update_attempted = document_update_status in {
        "applied",
        "failed",
        "blocked",
        "stopped-max-rounds",
    }
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
    if document_creation_attempted:
        return "document-creation-attempted-and-replied"
    if document_applied and tools_applied:
        return "edited-used-tools-and-replied"
    if document_applied and tool_attempted:
        return "edited-tool-attempted-and-replied"
    if document_applied:
        return "edited-and-replied"
    if document_update_attempted:
        return "document-update-attempted-and-replied"
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
    if document_creation_status == "failed":
        return "document-creation-failed-and-replied"
    if document_creation_status == "blocked":
        return "document-creation-blocked-and-replied"
    if document_creation_status == "stopped-max-rounds":
        return "document-creation-stopped-at-max-rounds-and-replied"
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
    if document_update_status == "failed":
        return "document-update-failed-and-replied"
    if document_update_status == "blocked":
        return "document-update-blocked-and-replied"
    if document_update_status == "stopped-max-rounds":
        return "document-update-stopped-at-max-rounds-and-replied"
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
