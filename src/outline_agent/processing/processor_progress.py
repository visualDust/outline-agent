from __future__ import annotations

from ..runtime.tool_runtime import ToolStepResult
from .processor_prompting import preview, truncate
from .processor_types import ToolRoundSummary


def describe_tool_start_for_progress(description: str, *, requires_confirmation: bool) -> str:
    message = f"Started: {description}."
    if requires_confirmation:
        return message[:-1] + " (approval-capable step auto-approved by current policy)."
    return message


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


def describe_tool_result_for_progress(result: ToolStepResult) -> str:
    if result.approval_status == "denied":
        target = progress_inline(result.target or result.tool, limit=80)
        return f"Paused: `{target}` is awaiting approval."
    if result.approval_status == "error":
        target = progress_inline(result.target or result.tool, limit=80)
        return f"Stopped: approval check for `{target}` failed."

    if result.tool == "list_dir":
        target = result.target or "."
        preview_tail = extract_result_tail(result.summary)
        if preview_tail:
            return _append_auto_approval_note(
                f"Finished: listed `{target}` → {progress_inline(preview_tail, limit=120)}.",
                result,
            )
        return _append_auto_approval_note(f"Finished: listed `{target}`.", result)

    if result.tool == "read_file":
        target = result.target or "file"
        if result.stdout:
            return _append_auto_approval_note(
                f"Finished: read `{target}` → `{progress_inline(result.stdout, limit=100)}`.",
                result,
            )
        return _append_auto_approval_note(f"Finished: read `{target}`.", result)

    if result.tool == "write_file":
        target = result.target or "file"
        action = "appended to" if result.summary.startswith("append_file[") else "created or updated"
        return _append_auto_approval_note(f"Finished: {action} `{target}`.", result)

    if result.tool == "edit_file":
        target = result.target or "file"
        return _append_auto_approval_note(f"Finished: edited `{target}`.", result)

    if result.tool == "run_shell":
        command = progress_inline(result.target or "command", limit=80)
        if result.ok:
            if result.stdout:
                return _append_auto_approval_note(
                    f"Finished: ran `{command}` → output `{progress_inline(result.stdout, limit=100)}`.",
                    result,
                )
            if result.stderr:
                return _append_auto_approval_note(
                    f"Finished: ran `{command}` → stderr `{progress_inline(result.stderr, limit=100)}`.",
                    result,
                )
            return _append_auto_approval_note(f"Finished: ran `{command}`.", result)

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
                return _append_auto_approval_note(
                    f"Finished: downloaded attachment to `{target}` → `{progress_inline(preview_tail, limit=100)}`.",
                    result,
                )
            return _append_auto_approval_note(f"Finished: downloaded attachment to `{target}`.", result)
        return f"Stopped: downloading attachment to `{target}` failed."

    if result.tool == "upload_attachment":
        target = result.target or "file"
        if result.ok:
            return _append_auto_approval_note(
                f"Finished: uploaded `{target}` back to the Outline document as an attachment.",
                result,
            )
        return f"Stopped: uploading `{target}` as an Outline attachment failed."

    if result.tool == "ask_gemini_web_search":
        target = result.target or "query"
        if result.ok and result.stdout:
            return f"Finished: Gemini web search answered `{target}` → `{progress_inline(result.stdout, limit=100)}`."
        if result.ok:
            return f"Finished: Gemini web search answered `{target}`."
        return f"Stopped: Gemini web search failed for `{target}`."

    return _append_auto_approval_note(f"Finished: {result.summary}.", result)


def describe_round_stop_for_progress(round_index: int, status: str) -> str:
    if status == "failed":
        return f"Stopped in round {round_index}: one of the requested actions failed."
    if status == "blocked":
        return f"Stopped in round {round_index}: I couldn't safely continue the requested actions."
    return f"Stopped in round {round_index}: actions ended with status `{status}`."


def describe_round_retry_for_progress(round_index: int, status: str) -> str:
    if status == "failed":
        return f"Round {round_index} failed, but I'm using the error details to replan the next step."
    return f"Round {round_index} ended with status `{status}`, and I'm replanning the next step."


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
            return f"Working on it — I'm carrying out the requested actions (round {round_index} of {total_rounds})."
        return "Working on it — I'm carrying out the requested actions."
    if status == "applied":
        return "Done — I finished the requested actions."
    if status == "failed":
        return "Stopped — one of the requested actions failed."
    if status == "blocked":
        return "Stopped — I couldn't safely continue the requested actions."
    if status == "stopped-max-rounds":
        return "Paused — I reached the configured limit for action rounds."
    return f"Status update — actions are now `{status}`."


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


def progress_comment_footer(status: str) -> str:
    return ""


def extract_result_tail(summary: str) -> str | None:
    if " -> " not in summary:
        return None
    _, _, tail = summary.partition(" -> ")
    return tail.strip() or None


def progress_inline(text: str, limit: int) -> str:
    compact = preview(text, limit=limit).replace("`", "'")
    return compact.strip()


def _append_auto_approval_note(message: str, result: ToolStepResult) -> str:
    if not (result.requires_confirmation and result.approval_status == "approved"):
        return message
    if message.endswith("."):
        return message[:-1] + " (auto-approved by current policy)."
    return message + " (auto-approved by current policy)."
