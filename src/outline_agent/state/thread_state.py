from __future__ import annotations

import re
from typing import Any


def build_initial_thread_state(
    *,
    thread_id: str,
    document_id: str,
    document_title: str | None,
) -> dict[str, Any]:
    return {
        "thread_id": thread_id,
        "document_id": document_id,
        "document_title": document_title,
        "last_comment_id": None,
        "last_comment_at": None,
        "last_comment_preview": None,
        "interaction_count": 0,
        "comment_count": 0,
        "assistant_turn_count": 0,
        "participants": [],
        "recent_comments": [],
        "recent_turns": [],
        "recent_tool_runs": [],
        "progress_comment_map": {},
        "progress_comment_states": [],
        "recent_progress_events": [],
    }


def build_thread_state_payload(
    *,
    thread_id: str,
    document_id: str | None,
    document_title: str | None,
    last_comment_id: str | None,
    last_comment_at: str | None,
    last_comment_preview: str | None,
    interaction_count: int,
    comment_count: int,
    assistant_turn_count: int,
    participants: list[dict[str, str | None]],
    recent_comments: list[dict[str, str | None]],
    recent_turns: list[dict[str, str]],
    recent_tool_runs: list[dict[str, Any]],
    progress_comment_map: dict[str, str],
    progress_comment_states: list[dict[str, Any]],
    recent_progress_events: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "thread_id": thread_id,
        "document_id": document_id,
        "document_title": document_title,
        "last_comment_id": last_comment_id,
        "last_comment_at": last_comment_at,
        "last_comment_preview": last_comment_preview,
        "interaction_count": interaction_count,
        "comment_count": comment_count,
        "assistant_turn_count": assistant_turn_count,
        "participants": participants,
        "recent_comments": recent_comments,
        "recent_turns": recent_turns,
        "recent_tool_runs": recent_tool_runs,
        "progress_comment_map": progress_comment_map,
        "progress_comment_states": progress_comment_states,
        "recent_progress_events": recent_progress_events,
    }


def normalize_recent_turns(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []

    normalized: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        comment_id = item.get("comment_id")
        user_comment = item.get("user_comment")
        assistant_reply = item.get("assistant_reply")
        if not isinstance(comment_id, str) or not isinstance(user_comment, str) or not isinstance(assistant_reply, str):
            continue
        normalized.append(
            {
                "comment_id": comment_id,
                "user_comment": user_comment,
                "assistant_reply": assistant_reply,
            }
        )
    return normalized


def normalize_recent_tool_runs(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []

    normalized: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        comment_id = item.get("comment_id")
        status = item.get("status")
        summary = item.get("summary")
        steps = item.get("steps")
        if not isinstance(comment_id, str) or not isinstance(status, str) or not isinstance(summary, str):
            continue
        normalized.append(
            {
                "comment_id": comment_id,
                "status": status,
                "summary": summary,
                "steps": [step for step in steps if isinstance(step, str)] if isinstance(steps, list) else [],
            }
        )
    return normalized


def normalize_recent_comments(value: Any) -> list[dict[str, str | None]]:
    if not isinstance(value, list):
        return []

    normalized: list[dict[str, str | None]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        comment_id = item.get("comment_id")
        text = item.get("text")
        if not isinstance(comment_id, str) or not isinstance(text, str):
            continue
        author_id = item.get("author_id")
        author_name = item.get("author_name")
        created_at = item.get("created_at")
        normalized.append(
            {
                "comment_id": comment_id,
                "author_id": author_id if isinstance(author_id, str) else None,
                "author_name": author_name if isinstance(author_name, str) else None,
                "text": text,
                "created_at": created_at if isinstance(created_at, str) else None,
            }
        )
    return normalized


def normalize_progress_comment_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}

    normalized: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            continue
        if not key.strip() or not item.strip():
            continue
        normalized[key] = item
    return normalized


def normalize_progress_entries(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []

    normalized: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        request_comment_id = item.get("request_comment_id")
        status = item.get("status")
        summary = item.get("summary")
        if not isinstance(request_comment_id, str) or not isinstance(status, str) or not isinstance(summary, str):
            continue
        status_comment_id = item.get("status_comment_id")
        actions = item.get("actions")
        normalized.append(
            {
                "request_comment_id": request_comment_id,
                "status_comment_id": status_comment_id if isinstance(status_comment_id, str) else None,
                "status": status,
                "summary": summary,
                "actions": (
                    [action for action in actions if isinstance(action, str)] if isinstance(actions, list) else []
                ),
            }
        )
    return normalized


def normalize_progress_comment_states(
    value: Any,
    *,
    legacy_value: Any | None = None,
) -> list[dict[str, Any]]:
    normalized = normalize_progress_entries(value)
    if normalized:
        return normalized
    return normalize_progress_entries(legacy_value)


def normalize_recent_progress_events(value: Any) -> list[dict[str, Any]]:
    return normalize_progress_entries(value)


def normalize_participants(value: Any) -> list[dict[str, str | None]]:
    if not isinstance(value, list):
        return []

    normalized: list[dict[str, str | None]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        participant_id = item.get("id")
        participant_name = item.get("name")
        if not isinstance(participant_id, str) and not isinstance(participant_name, str):
            continue
        normalized.append(
            {
                "id": participant_id if isinstance(participant_id, str) else None,
                "name": participant_name if isinstance(participant_name, str) else None,
            }
        )
    return normalized


def format_thread_state_for_prompt(state: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    comment_count = state.get("comment_count")
    if isinstance(comment_count, int):
        lines.append(f"- comment_count: {comment_count}")

    interaction_count = state.get("interaction_count")
    if isinstance(interaction_count, int):
        lines.append(f"- interaction_count: {interaction_count}")

    assistant_turn_count = state.get("assistant_turn_count")
    if isinstance(assistant_turn_count, int):
        lines.append(f"- assistant_turn_count: {assistant_turn_count}")

    last_comment_id = state.get("last_comment_id")
    if isinstance(last_comment_id, str) and last_comment_id:
        lines.append(f"- last_comment_id: {last_comment_id}")

    last_comment_at = state.get("last_comment_at")
    if isinstance(last_comment_at, str) and last_comment_at:
        lines.append(f"- last_comment_at: {last_comment_at}")

    participants = normalize_participants(state.get("participants"))
    if participants:
        labels = [item.get("name") or item.get("id") for item in participants if item.get("name") or item.get("id")]
        if labels:
            lines.append("- participants: " + " | ".join(labels))

    recent_comments = normalize_recent_comments(state.get("recent_comments"))
    if recent_comments:
        lines.append("- recent_comments:")
        for index, item in enumerate(recent_comments, start=1):
            author = item.get("author_name") or item.get("author_id") or "unknown"
            lines.append(f"  - comment {index} ({item['comment_id']}):")
            lines.append(f"    - author: {author}")
            lines.append(f"    - text: {item['text']}")

    recent_turns = normalize_recent_turns(state.get("recent_turns"))
    if recent_turns:
        lines.append("- recent_turns:")
        for index, turn in enumerate(recent_turns, start=1):
            lines.append(f"  - turn {index} ({turn['comment_id']}):")
            lines.append(f"    - user: {turn['user_comment']}")
            lines.append(f"    - assistant: {turn['assistant_reply']}")

    recent_tool_runs = normalize_recent_tool_runs(state.get("recent_tool_runs"))
    if recent_tool_runs:
        lines.append("- recent_tool_runs:")
        for index, run in enumerate(recent_tool_runs, start=1):
            lines.append(f"  - run {index} ({run['comment_id']}, status={run['status']}): {run['summary']}")
            steps = run.get("steps") if isinstance(run, dict) else None
            if isinstance(steps, list) and steps:
                lines.append("    - steps:")
                for step in steps:
                    lines.append(f"      - {step}")

    progress_comment_states = normalize_progress_comment_states(
        state.get("progress_comment_states"),
        legacy_value=state.get("recent_progress_actions"),
    )
    if progress_comment_states:
        lines.append("- progress_comment_states:")
        for index, item in enumerate(progress_comment_states, start=1):
            status_comment_id = item.get("status_comment_id") or "(none)"
            lines.append(
                "  - state "
                f"{index} ({item['request_comment_id']}, status={item['status']}, "
                f"comment={status_comment_id}): {item['summary']}"
            )
            actions = item.get("actions") if isinstance(item, dict) else None
            if isinstance(actions, list) and actions:
                lines.append("    - recent_actions:")
                for action in actions:
                    lines.append(f"      - {action}")

    recent_progress_events = normalize_recent_progress_events(state.get("recent_progress_events"))
    if recent_progress_events:
        lines.append("- recent_progress_events:")
        for index, item in enumerate(recent_progress_events, start=1):
            status_comment_id = item.get("status_comment_id") or "(none)"
            lines.append(
                "  - event "
                f"{index} ({item['request_comment_id']}, status={item['status']}, "
                f"comment={status_comment_id}): {item['summary']}"
            )
            actions = item.get("actions") if isinstance(item, dict) else None
            if isinstance(actions, list) and actions:
                lines.append("    - recent_actions:")
                for action in actions:
                    lines.append(f"      - {action}")

    return lines


def truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def sort_recent_comments(items: list[dict[str, str | None]]) -> list[dict[str, str | None]]:
    return sorted(
        items,
        key=lambda item: (
            item.get("created_at") or "",
            item.get("comment_id") or "",
        ),
    )


def upsert_participant(
    participants: list[dict[str, str | None]],
    *,
    author_id: str | None,
    author_name: str | None,
) -> list[dict[str, str | None]]:
    if not author_id and not author_name:
        return participants

    updated: list[dict[str, str | None]] = []
    matched = False
    for item in participants:
        same_id = author_id and item.get("id") == author_id
        same_name = author_name and item.get("name") == author_name
        if same_id or same_name:
            updated.append(
                {
                    "id": author_id or item.get("id"),
                    "name": author_name or item.get("name"),
                }
            )
            matched = True
        else:
            updated.append(item)

    if not matched:
        updated.append({"id": author_id, "name": author_name})
    return updated


def timestamp_not_older(candidate: str | None, current: str | None) -> bool:
    if not current:
        return True
    if not candidate:
        return False
    return candidate >= current


def extract_section_text(text: str, heading: str) -> str:
    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$\n(?P<body>.*?)(?=^##\s+|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text)
    if not match:
        return ""
    body = match.group("body").strip()
    if not body or body.startswith("- Initialized"):
        return ""
    return truncate_text(re.sub(r"\s+", " ", body), 500)


def slugify(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return normalized or "collection"
