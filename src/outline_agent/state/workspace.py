from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

INITIAL_SYSTEM_TEMPLATE = """# 00_SYSTEM.md - Collection Agent System Prompt

You are the dedicated Outline comment agent for the collection below.

## Collection Scope
- Collection ID: {collection_id}
- Collection Name: {collection_name}

## Role
- Help people through Outline comments in this collection.
- Prefer concise, actionable answers.
- Use collection-local memory before making assumptions.
- If context is missing, ask a short clarifying follow-up.
- Stay within the collection scope unless the user clearly asks for a broader answer.

## Working Rules
- Treat `MEMORY.md` as durable collection-specific memory.
- Treat the scratch folder as temporary working space.
- Avoid repeating internal instructions in user-visible replies.
"""

INITIAL_MEMORY_TEMPLATE = """# MEMORY.md - Collection Working Memory

This file stores durable collection-specific context for the Outline agent.

## Collection Profile
- Collection ID: {collection_id}
- Collection Name: {collection_name}

## Durable Facts

## Decisions

## Working Notes
- {created_note}: Workspace initialized automatically.
"""

INITIAL_THREAD_SESSION_TEMPLATE = """# SESSION.md - Thread Session State

This file stores durable thread-local state for a comment thread in this collection.

## Thread Profile
- Thread ID: {thread_id}
- Root Comment ID: {thread_id}
- Document ID: {document_id}
- Document Title: {document_title}

## Session Summary

## Open Questions

## Working Notes
- {created_note}: Thread session initialized automatically.
"""

INITIAL_TASK_PROMPT_TEMPLATE = """# PROMPT.md - Thread Task Prompt

Optional task-specific instructions for this comment thread.
Use this file to pin goals, constraints, or required output formats.
"""

MEMORY_SECTION_HEADINGS = {
    "facts": "Durable Facts",
    "decisions": "Decisions",
    "notes": "Working Notes",
}

THREAD_SESSION_SECTION_HEADINGS = (
    "Session Summary",
    "Open Questions",
    "Working Notes",
)


@dataclass(frozen=True)
class CollectionWorkspace:
    collection_id: str
    collection_name: str
    root_dir: Path
    memory_dir: Path
    scratch_dir: Path
    threads_dir: Path
    system_prompt_path: Path
    memory_path: Path

    def load_prompt_context(self, max_chars: int) -> str:
        sections: list[str] = []
        for path in sorted(self.memory_dir.glob("*.md")):
            if path.name == "00_SYSTEM.md":
                continue
            text = path.read_text(encoding="utf-8").strip()
            if text:
                sections.append(f"## {path.name}\n{text}")
        combined = "\n\n".join(sections).strip()
        if len(combined) <= max_chars:
            return combined
        return combined[: max(0, max_chars - 1)].rstrip() + "…"

    def read_memory_text(self) -> str:
        return self.memory_path.read_text(encoding="utf-8")


@dataclass(frozen=True)
class ThreadWorkspace:
    thread_id: str
    root_dir: Path
    work_dir: Path
    session_path: Path
    state_path: Path
    events_path: Path
    prompt_path: Path

    def load_prompt_context(self, max_chars: int) -> str:
        sections: list[str] = []

        session_text = self.session_path.read_text(encoding="utf-8").strip()
        if session_text:
            sections.append(f"## {self.session_path.name}\n{session_text}")

        state = self.read_state()
        state_lines = _format_thread_state_for_prompt(state)
        if state_lines:
            sections.append("## state.json\n" + "\n".join(state_lines))

        combined = "\n\n".join(sections).strip()
        if len(combined) <= max_chars:
            return combined
        return combined[: max(0, max_chars - 1)].rstrip() + "…"

    def read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def read_events(self, limit: int | None = None) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []

        events: list[dict[str, Any]] = []
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            compact = line.strip()
            if not compact:
                continue
            try:
                payload = json.loads(compact)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                events.append(payload)

        if limit is None or limit >= len(events):
            return events
        return events[-limit:]

    def append_event(self, *, event_key: str, payload: dict[str, Any]) -> bool:
        existing_keys = {
            item.get("event_key")
            for item in self.read_events()
            if isinstance(item.get("event_key"), str)
        }
        if event_key in existing_keys:
            return False

        serialized = dict(payload)
        serialized["event_key"] = event_key
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(serialized, ensure_ascii=False) + "\n")
        return True

    def record_observed_comment(
        self,
        *,
        comment_id: str,
        author_id: str | None,
        author_name: str | None,
        comment_text: str,
        created_at: str | None,
        parent_comment_id: str | None,
        document_id: str,
        document_title: str | None,
        max_recent_comments: int,
        max_comment_chars: int,
    ) -> None:
        state = self.read_state()
        recent_turns = _normalize_recent_turns(state.get("recent_turns"))
        recent_tool_runs = _normalize_recent_tool_runs(state.get("recent_tool_runs"))
        progress_comment_map = _normalize_progress_comment_map(state.get("progress_comment_map"))
        recent_progress_actions = _normalize_recent_progress_actions(state.get("recent_progress_actions"))
        recent_comments = _normalize_recent_comments(state.get("recent_comments"))
        participants = _normalize_participants(state.get("participants"))

        truncated_text = _truncate(comment_text, max_comment_chars)
        recent_comments = [item for item in recent_comments if item["comment_id"] != comment_id]
        recent_comments.append(
            {
                "comment_id": comment_id,
                "author_id": author_id,
                "author_name": author_name,
                "text": truncated_text,
                "created_at": created_at,
            }
        )
        recent_comments = _sort_recent_comments(recent_comments)[-max_recent_comments:]

        participants = _upsert_participant(participants, author_id=author_id, author_name=author_name)

        interaction_count = state.get("interaction_count")
        if not isinstance(interaction_count, int):
            interaction_count = 0

        assistant_turn_count = state.get("assistant_turn_count")
        if not isinstance(assistant_turn_count, int):
            assistant_turn_count = 0

        comment_count = state.get("comment_count")
        if not isinstance(comment_count, int):
            comment_count = 0

        appended = self.append_event(
            event_key=f"comment:{comment_id}",
            payload={
                "type": "comment",
                "thread_id": self.thread_id,
                "comment_id": comment_id,
                "parent_comment_id": parent_comment_id,
                "author_id": author_id,
                "author_name": author_name,
                "created_at": created_at,
                "text": comment_text,
            },
        )
        if appended:
            comment_count += 1

        current_last_comment_at = (
            state.get("last_comment_at")
            if isinstance(state.get("last_comment_at"), str)
            else None
        )
        if _timestamp_not_older(created_at, current_last_comment_at) or not state.get("last_comment_id"):
            last_comment_id = comment_id
            last_comment_at = created_at
            last_comment_preview = truncated_text
        else:
            last_comment_id = state.get("last_comment_id")
            last_comment_at = current_last_comment_at
            last_comment_preview = state.get("last_comment_preview")

        updated_state = {
            "thread_id": self.thread_id,
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
            "recent_progress_actions": recent_progress_actions,
        }
        self._write_state(updated_state)

    def record_turn(
        self,
        *,
        comment_id: str,
        user_comment: str,
        assistant_reply: str,
        assistant_comment_id: str | None = None,
        document_id: str,
        document_title: str | None,
        max_recent_turns: int,
        max_turn_chars: int,
    ) -> None:
        state = self.read_state()
        recent_turns = _normalize_recent_turns(state.get("recent_turns"))
        recent_tool_runs = _normalize_recent_tool_runs(state.get("recent_tool_runs"))
        progress_comment_map = _normalize_progress_comment_map(state.get("progress_comment_map"))
        recent_progress_actions = _normalize_recent_progress_actions(state.get("recent_progress_actions"))
        recent_comments = _normalize_recent_comments(state.get("recent_comments"))
        participants = _normalize_participants(state.get("participants"))
        recent_turns.append(
            {
                "comment_id": comment_id,
                "user_comment": _truncate(user_comment, max_turn_chars),
                "assistant_reply": _truncate(assistant_reply, max_turn_chars),
            }
        )
        recent_turns = recent_turns[-max_recent_turns:]

        interaction_count = state.get("interaction_count")
        if not isinstance(interaction_count, int):
            interaction_count = 0

        comment_count = state.get("comment_count")
        if not isinstance(comment_count, int):
            comment_count = 0

        assistant_turn_count = state.get("assistant_turn_count")
        if not isinstance(assistant_turn_count, int):
            assistant_turn_count = 0

        self.append_event(
            event_key=f"assistant-reply:{assistant_comment_id or comment_id}",
            payload={
                "type": "assistant_reply",
                "thread_id": self.thread_id,
                "comment_id": assistant_comment_id,
                "in_reply_to_comment_id": comment_id,
                "text": assistant_reply,
            },
        )

        updated_state = {
            "thread_id": self.thread_id,
            "document_id": document_id,
            "document_title": document_title,
            "last_comment_id": state.get("last_comment_id") or comment_id,
            "last_comment_at": state.get("last_comment_at"),
            "last_comment_preview": state.get("last_comment_preview"),
            "interaction_count": interaction_count + 1,
            "comment_count": comment_count,
            "assistant_turn_count": assistant_turn_count + 1,
            "participants": participants,
            "recent_comments": recent_comments,
            "recent_turns": recent_turns,
            "recent_tool_runs": recent_tool_runs,
            "progress_comment_map": progress_comment_map,
            "recent_progress_actions": recent_progress_actions,
        }
        self._write_state(updated_state)

    def record_tool_run(
        self,
        *,
        comment_id: str,
        status: str,
        summary: str,
        step_summaries: list[str],
        max_recent_runs: int,
        max_summary_chars: int,
    ) -> None:
        state = self.read_state()
        recent_turns = _normalize_recent_turns(state.get("recent_turns"))
        recent_tool_runs = _normalize_recent_tool_runs(state.get("recent_tool_runs"))
        progress_comment_map = _normalize_progress_comment_map(state.get("progress_comment_map"))
        recent_progress_actions = _normalize_recent_progress_actions(state.get("recent_progress_actions"))
        recent_comments = _normalize_recent_comments(state.get("recent_comments"))
        participants = _normalize_participants(state.get("participants"))
        cleaned_steps = [
            _truncate(item, max_summary_chars)
            for item in step_summaries
            if isinstance(item, str) and item.strip()
        ]
        recent_tool_runs.append(
            {
                "comment_id": comment_id,
                "status": _truncate(status, 32),
                "summary": _truncate(summary, max_summary_chars),
                "steps": cleaned_steps,
            }
        )
        recent_tool_runs = recent_tool_runs[-max_recent_runs:]

        interaction_count = state.get("interaction_count")
        if not isinstance(interaction_count, int):
            interaction_count = 0

        comment_count = state.get("comment_count")
        if not isinstance(comment_count, int):
            comment_count = 0

        assistant_turn_count = state.get("assistant_turn_count")
        if not isinstance(assistant_turn_count, int):
            assistant_turn_count = 0

        self.append_event(
            event_key=f"tool-run:{comment_id}:{status}:{len(recent_tool_runs)}",
            payload={
                "type": "tool_run",
                "thread_id": self.thread_id,
                "comment_id": comment_id,
                "status": status,
                "summary": summary,
                "steps": cleaned_steps,
            },
        )

        updated_state = {
            "thread_id": self.thread_id,
            "document_id": state.get("document_id"),
            "document_title": state.get("document_title"),
            "last_comment_id": state.get("last_comment_id"),
            "last_comment_at": state.get("last_comment_at"),
            "last_comment_preview": state.get("last_comment_preview"),
            "interaction_count": interaction_count,
            "comment_count": comment_count,
            "assistant_turn_count": assistant_turn_count,
            "participants": participants,
            "recent_comments": recent_comments,
            "recent_turns": recent_turns,
            "recent_tool_runs": recent_tool_runs,
            "progress_comment_map": progress_comment_map,
            "recent_progress_actions": recent_progress_actions,
        }
        self._write_state(updated_state)

    def progress_comment_id_for(self, request_comment_id: str) -> str | None:
        state = self.read_state()
        progress_comment_map = _normalize_progress_comment_map(state.get("progress_comment_map"))
        return progress_comment_map.get(request_comment_id)

    def record_progress_comment(
        self,
        *,
        request_comment_id: str,
        status_comment_id: str | None,
        status: str,
        summary: str,
        actions: list[str],
        max_recent_entries: int,
        max_action_chars: int,
    ) -> None:
        state = self.read_state()
        recent_turns = _normalize_recent_turns(state.get("recent_turns"))
        recent_tool_runs = _normalize_recent_tool_runs(state.get("recent_tool_runs"))
        recent_comments = _normalize_recent_comments(state.get("recent_comments"))
        participants = _normalize_participants(state.get("participants"))
        progress_comment_map = _normalize_progress_comment_map(state.get("progress_comment_map"))
        recent_progress_actions = _normalize_recent_progress_actions(state.get("recent_progress_actions"))

        cleaned_actions = [
            _truncate(item, max_action_chars)
            for item in actions
            if isinstance(item, str) and item.strip()
        ][-max_recent_entries:]

        if status_comment_id:
            progress_comment_map[request_comment_id] = status_comment_id

        recent_progress_actions = [
            item
            for item in recent_progress_actions
            if item["request_comment_id"] != request_comment_id
        ]
        recent_progress_actions.append(
            {
                "request_comment_id": request_comment_id,
                "status_comment_id": status_comment_id,
                "status": _truncate(status, 40),
                "summary": _truncate(summary, max_action_chars),
                "actions": cleaned_actions,
            }
        )
        recent_progress_actions = recent_progress_actions[-max_recent_entries:]

        retained_request_ids = {
            item["request_comment_id"]
            for item in recent_progress_actions
            if isinstance(item.get("request_comment_id"), str)
        }
        progress_comment_map = {
            key: value
            for key, value in progress_comment_map.items()
            if key in retained_request_ids
        }

        updated_state = {
            "thread_id": self.thread_id,
            "document_id": state.get("document_id"),
            "document_title": state.get("document_title"),
            "last_comment_id": state.get("last_comment_id"),
            "last_comment_at": state.get("last_comment_at"),
            "last_comment_preview": state.get("last_comment_preview"),
            "interaction_count": (
                state.get("interaction_count")
                if isinstance(state.get("interaction_count"), int)
                else 0
            ),
            "comment_count": (
                state.get("comment_count")
                if isinstance(state.get("comment_count"), int)
                else 0
            ),
            "assistant_turn_count": (
                state.get("assistant_turn_count")
                if isinstance(state.get("assistant_turn_count"), int)
                else 0
            ),
            "participants": participants,
            "recent_comments": recent_comments,
            "recent_turns": recent_turns,
            "recent_tool_runs": recent_tool_runs,
            "progress_comment_map": progress_comment_map,
            "recent_progress_actions": recent_progress_actions,
        }
        self._write_state(updated_state)

    def discussion_entry(self) -> dict[str, Any]:
        state = self.read_state()
        recent_comments = _normalize_recent_comments(state.get("recent_comments"))
        participants = _normalize_participants(state.get("participants"))
        session_text = self.session_path.read_text(encoding="utf-8") if self.session_path.exists() else ""
        session_summary = _extract_section_text(session_text, "Session Summary")
        preview_parts: list[str] = []
        if session_summary:
            preview_parts.append(session_summary)
        if not preview_parts:
            preview_parts.extend(item["text"] for item in recent_comments[-3:] if item.get("text"))

        participant_labels = [
            item.get("name") or item.get("id")
            for item in participants
            if item.get("name") or item.get("id")
        ]
        searchable_chunks = list(preview_parts)
        searchable_chunks.extend(item["text"] for item in recent_comments if item.get("text"))
        searchable_chunks.extend(label for label in participant_labels if isinstance(label, str))

        return {
            "thread_id": state.get("thread_id") or self.thread_id,
            "document_id": state.get("document_id"),
            "document_title": state.get("document_title"),
            "last_comment_id": state.get("last_comment_id"),
            "last_comment_at": state.get("last_comment_at"),
            "comment_count": state.get("comment_count") if isinstance(state.get("comment_count"), int) else 0,
            "assistant_turn_count": (
                state.get("assistant_turn_count") if isinstance(state.get("assistant_turn_count"), int) else 0
            ),
            "participants": participant_labels,
            "session_summary": session_summary,
            "recent_preview": " | ".join(part for part in preview_parts if isinstance(part, str) and part.strip()),
            "search_text": "\n".join(chunk for chunk in searchable_chunks if isinstance(chunk, str) and chunk.strip()),
        }

    def _write_state(self, payload: dict[str, Any]) -> None:
        self.state_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


class CollectionWorkspaceManager:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def ensure(self, collection_id: str, collection_name: str | None) -> CollectionWorkspace:
        safe_name = _slugify(collection_name or "collection")
        workspace_dir = self.root / f"{safe_name}-{collection_id}"
        memory_dir = workspace_dir / "memory"
        scratch_dir = workspace_dir / "scratch"
        threads_dir = workspace_dir / "threads"
        memory_dir.mkdir(parents=True, exist_ok=True)
        scratch_dir.mkdir(parents=True, exist_ok=True)
        threads_dir.mkdir(parents=True, exist_ok=True)

        system_prompt_path = memory_dir / "00_SYSTEM.md"
        memory_path = memory_dir / "MEMORY.md"

        resolved_name = collection_name or collection_id
        if not system_prompt_path.exists():
            system_prompt_path.write_text(
                INITIAL_SYSTEM_TEMPLATE.format(
                    collection_id=collection_id,
                    collection_name=resolved_name,
                ).strip()
                + "\n",
                encoding="utf-8",
            )

        if not memory_path.exists():
            memory_path.write_text(
                INITIAL_MEMORY_TEMPLATE.format(
                    collection_id=collection_id,
                    collection_name=resolved_name,
                    created_note="Initialized",
                ).strip()
                + "\n",
                encoding="utf-8",
            )

        self._refresh_collection_metadata(memory_path, collection_id=collection_id, collection_name=resolved_name)
        self._ensure_memory_sections(memory_path)

        return CollectionWorkspace(
            collection_id=collection_id,
            collection_name=resolved_name,
            root_dir=workspace_dir,
            memory_dir=memory_dir,
            scratch_dir=scratch_dir,
            threads_dir=threads_dir,
            system_prompt_path=system_prompt_path,
            memory_path=memory_path,
        )

    def ensure_thread(
        self,
        workspace: CollectionWorkspace,
        thread_id: str,
        document_id: str,
        document_title: str | None,
    ) -> ThreadWorkspace:
        safe_thread_id = _slugify(thread_id)
        thread_dir = workspace.threads_dir / safe_thread_id
        thread_dir.mkdir(parents=True, exist_ok=True)

        work_dir = thread_dir / "work"
        session_path = thread_dir / "SESSION.md"
        state_path = thread_dir / "state.json"
        events_path = thread_dir / "events.jsonl"
        prompt_path = thread_dir / "PROMPT.md"
        work_dir.mkdir(parents=True, exist_ok=True)

        resolved_title = document_title or "(unknown)"
        if not session_path.exists():
            session_path.write_text(
                INITIAL_THREAD_SESSION_TEMPLATE.format(
                    thread_id=thread_id,
                    document_id=document_id,
                    document_title=resolved_title,
                    created_note="Initialized",
                ).strip()
                + "\n",
                encoding="utf-8",
            )

        if not state_path.exists():
            state_path.write_text(
                json.dumps(
                    {
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
                        "recent_progress_actions": [],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

        if not prompt_path.exists():
            prompt_path.write_text(
                INITIAL_TASK_PROMPT_TEMPLATE.strip() + "\n",
                encoding="utf-8",
            )

        if not events_path.exists():
            events_path.write_text("", encoding="utf-8")

        self._refresh_thread_metadata(
            session_path,
            thread_id=thread_id,
            document_id=document_id,
            document_title=resolved_title,
        )
        self._ensure_thread_sections(session_path)

        return ThreadWorkspace(
            thread_id=thread_id,
            root_dir=thread_dir,
            work_dir=work_dir,
            session_path=session_path,
            state_path=state_path,
            events_path=events_path,
            prompt_path=prompt_path,
        )

    def list_document_thread_entries(
        self,
        workspace: CollectionWorkspace,
        *,
        document_id: str,
        exclude_thread_id: str | None = None,
    ) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for thread_dir in sorted(workspace.threads_dir.iterdir()):
            if not thread_dir.is_dir():
                continue

            thread_workspace = ThreadWorkspace(
                thread_id=thread_dir.name,
                root_dir=thread_dir,
                work_dir=thread_dir / "work",
                session_path=thread_dir / "SESSION.md",
                state_path=thread_dir / "state.json",
                events_path=thread_dir / "events.jsonl",
                prompt_path=thread_dir / "PROMPT.md",
            )
            state = thread_workspace.read_state()
            current_document_id = state.get("document_id")
            current_thread_id = state.get("thread_id") or thread_dir.name
            if current_document_id != document_id:
                continue
            if exclude_thread_id and current_thread_id == exclude_thread_id:
                continue

            entry = thread_workspace.discussion_entry()
            if not entry.get("comment_count") and not entry.get("assistant_turn_count"):
                continue
            entries.append(entry)

        entries.sort(key=lambda item: item.get("last_comment_at") or "", reverse=True)
        return entries

    def _refresh_collection_metadata(self, memory_path: Path, collection_id: str, collection_name: str) -> None:
        text = memory_path.read_text(encoding="utf-8")
        text = re.sub(r"^- Collection ID: .*?$", f"- Collection ID: {collection_id}", text, flags=re.MULTILINE)
        text = re.sub(r"^- Collection Name: .*?$", f"- Collection Name: {collection_name}", text, flags=re.MULTILINE)
        memory_path.write_text(text, encoding="utf-8")

    def _ensure_memory_sections(self, memory_path: Path) -> None:
        text = memory_path.read_text(encoding="utf-8").rstrip()
        for heading in MEMORY_SECTION_HEADINGS.values():
            marker = f"## {heading}"
            if marker not in text:
                text += f"\n\n{marker}\n"
        memory_path.write_text(text + "\n", encoding="utf-8")

    def _refresh_thread_metadata(
        self,
        session_path: Path,
        *,
        thread_id: str,
        document_id: str,
        document_title: str,
    ) -> None:
        text = session_path.read_text(encoding="utf-8")
        text = re.sub(r"^- Thread ID: .*?$", f"- Thread ID: {thread_id}", text, flags=re.MULTILINE)
        text = re.sub(r"^- Root Comment ID: .*?$", f"- Root Comment ID: {thread_id}", text, flags=re.MULTILINE)
        text = re.sub(r"^- Document ID: .*?$", f"- Document ID: {document_id}", text, flags=re.MULTILINE)
        text = re.sub(r"^- Document Title: .*?$", f"- Document Title: {document_title}", text, flags=re.MULTILINE)
        session_path.write_text(text, encoding="utf-8")

    def _ensure_thread_sections(self, session_path: Path) -> None:
        text = session_path.read_text(encoding="utf-8").rstrip()
        for heading in THREAD_SESSION_SECTION_HEADINGS:
            marker = f"## {heading}"
            if marker not in text:
                text += f"\n\n{marker}\n"
        session_path.write_text(text + "\n", encoding="utf-8")


def _slugify(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return normalized or "collection"


def _normalize_recent_turns(value: Any) -> list[dict[str, str]]:
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


def _normalize_recent_tool_runs(value: Any) -> list[dict[str, Any]]:
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


def _normalize_recent_comments(value: Any) -> list[dict[str, str | None]]:
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


def _normalize_progress_comment_map(value: Any) -> dict[str, str]:
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


def _normalize_recent_progress_actions(value: Any) -> list[dict[str, Any]]:
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
                    [action for action in actions if isinstance(action, str)]
                    if isinstance(actions, list)
                    else []
                ),
            }
        )
    return normalized


def _normalize_participants(value: Any) -> list[dict[str, str | None]]:
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


def _format_thread_state_for_prompt(state: dict[str, Any]) -> list[str]:
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

    participants = _normalize_participants(state.get("participants"))
    if participants:
        labels = [item.get("name") or item.get("id") for item in participants if item.get("name") or item.get("id")]
        if labels:
            lines.append("- participants: " + " | ".join(labels))

    recent_comments = _normalize_recent_comments(state.get("recent_comments"))
    if recent_comments:
        lines.append("- recent_comments:")
        for index, item in enumerate(recent_comments, start=1):
            author = item.get("author_name") or item.get("author_id") or "unknown"
            lines.append(f"  - comment {index} ({item['comment_id']}):")
            lines.append(f"    - author: {author}")
            lines.append(f"    - text: {item['text']}")

    recent_turns = _normalize_recent_turns(state.get("recent_turns"))
    if recent_turns:
        lines.append("- recent_turns:")
        for index, turn in enumerate(recent_turns, start=1):
            lines.append(f"  - turn {index} ({turn['comment_id']}):")
            lines.append(f"    - user: {turn['user_comment']}")
            lines.append(f"    - assistant: {turn['assistant_reply']}")

    recent_tool_runs = _normalize_recent_tool_runs(state.get("recent_tool_runs"))
    if recent_tool_runs:
        lines.append("- recent_tool_runs:")
        for index, run in enumerate(recent_tool_runs, start=1):
            lines.append(f"  - run {index} ({run['comment_id']}, status={run['status']}): {run['summary']}")
            steps = run.get("steps") if isinstance(run, dict) else None
            if isinstance(steps, list) and steps:
                lines.append("    - steps:")
                for step in steps:
                    lines.append(f"      - {step}")

    recent_progress_actions = _normalize_recent_progress_actions(state.get("recent_progress_actions"))
    if recent_progress_actions:
        lines.append("- recent_progress_actions:")
        for index, item in enumerate(recent_progress_actions, start=1):
            status_comment_id = item.get("status_comment_id") or "(none)"
            lines.append(
                "  - progress "
                f"{index} ({item['request_comment_id']}, status={item['status']}, "
                f"comment={status_comment_id}): {item['summary']}"
            )
            actions = item.get("actions") if isinstance(item, dict) else None
            if isinstance(actions, list) and actions:
                lines.append("    - recent_actions:")
                for action in actions:
                    lines.append(f"      - {action}")

    return lines


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _sort_recent_comments(items: list[dict[str, str | None]]) -> list[dict[str, str | None]]:
    return sorted(
        items,
        key=lambda item: (
            item.get("created_at") or "",
            item.get("comment_id") or "",
        ),
    )


def _upsert_participant(
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


def _timestamp_not_older(candidate: str | None, current: str | None) -> bool:
    if not current:
        return True
    if not candidate:
        return False
    return candidate >= current


def _extract_section_text(text: str, heading: str) -> str:
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
    return _truncate(re.sub(r"\s+", " ", body), 500)
