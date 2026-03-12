from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .thread_state import build_initial_thread_state, build_thread_state_payload
from .thread_state import extract_section_text as _extract_section_text
from .thread_state import format_thread_state_for_prompt as _format_thread_state_for_prompt
from .thread_state import normalize_participants as _normalize_participants
from .thread_state import normalize_progress_comment_map as _normalize_progress_comment_map
from .thread_state import normalize_progress_comment_states as _normalize_progress_comment_states
from .thread_state import normalize_recent_comments as _normalize_recent_comments
from .thread_state import normalize_recent_progress_events as _normalize_recent_progress_events
from .thread_state import normalize_recent_tool_runs as _normalize_recent_tool_runs
from .thread_state import normalize_recent_turns as _normalize_recent_turns
from .thread_state import slugify as _slugify
from .thread_state import sort_recent_comments as _sort_recent_comments
from .thread_state import timestamp_not_older as _timestamp_not_older
from .thread_state import truncate_text as _truncate
from .thread_state import upsert_participant as _upsert_participant

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
        existing_keys = {item.get("event_key") for item in self.read_events() if isinstance(item.get("event_key"), str)}
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
        progress_comment_states = _normalize_progress_comment_states(
            state.get("progress_comment_states"),
            legacy_value=state.get("recent_progress_actions"),
        )
        recent_progress_events = _normalize_recent_progress_events(state.get("recent_progress_events"))
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
            state.get("last_comment_at") if isinstance(state.get("last_comment_at"), str) else None
        )
        if _timestamp_not_older(created_at, current_last_comment_at) or not state.get("last_comment_id"):
            last_comment_id = comment_id
            last_comment_at = created_at
            last_comment_preview = truncated_text
        else:
            last_comment_id = state.get("last_comment_id")
            last_comment_at = current_last_comment_at
            last_comment_preview = state.get("last_comment_preview")

        updated_state = build_thread_state_payload(
            thread_id=self.thread_id,
            document_id=document_id,
            document_title=document_title,
            last_comment_id=last_comment_id,
            last_comment_at=last_comment_at,
            last_comment_preview=last_comment_preview,
            interaction_count=interaction_count,
            comment_count=comment_count,
            assistant_turn_count=assistant_turn_count,
            participants=participants,
            recent_comments=recent_comments,
            recent_turns=recent_turns,
            recent_tool_runs=recent_tool_runs,
            progress_comment_map=progress_comment_map,
            progress_comment_states=progress_comment_states,
            recent_progress_events=recent_progress_events,
        )
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
        progress_comment_states = _normalize_progress_comment_states(
            state.get("progress_comment_states"),
            legacy_value=state.get("recent_progress_actions"),
        )
        recent_progress_events = _normalize_recent_progress_events(state.get("recent_progress_events"))
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

        updated_state = build_thread_state_payload(
            thread_id=self.thread_id,
            document_id=document_id,
            document_title=document_title,
            last_comment_id=state.get("last_comment_id") or comment_id,
            last_comment_at=state.get("last_comment_at"),
            last_comment_preview=state.get("last_comment_preview"),
            interaction_count=interaction_count + 1,
            comment_count=comment_count,
            assistant_turn_count=assistant_turn_count + 1,
            participants=participants,
            recent_comments=recent_comments,
            recent_turns=recent_turns,
            recent_tool_runs=recent_tool_runs,
            progress_comment_map=progress_comment_map,
            progress_comment_states=progress_comment_states,
            recent_progress_events=recent_progress_events,
        )
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
        progress_comment_states = _normalize_progress_comment_states(
            state.get("progress_comment_states"),
            legacy_value=state.get("recent_progress_actions"),
        )
        recent_progress_events = _normalize_recent_progress_events(state.get("recent_progress_events"))
        recent_comments = _normalize_recent_comments(state.get("recent_comments"))
        participants = _normalize_participants(state.get("participants"))
        cleaned_steps = [
            _truncate(item, max_summary_chars) for item in step_summaries if isinstance(item, str) and item.strip()
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

        updated_state = build_thread_state_payload(
            thread_id=self.thread_id,
            document_id=state.get("document_id"),
            document_title=state.get("document_title"),
            last_comment_id=state.get("last_comment_id"),
            last_comment_at=state.get("last_comment_at"),
            last_comment_preview=state.get("last_comment_preview"),
            interaction_count=interaction_count,
            comment_count=comment_count,
            assistant_turn_count=assistant_turn_count,
            participants=participants,
            recent_comments=recent_comments,
            recent_turns=recent_turns,
            recent_tool_runs=recent_tool_runs,
            progress_comment_map=progress_comment_map,
            progress_comment_states=progress_comment_states,
            recent_progress_events=recent_progress_events,
        )
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
        progress_comment_states = _normalize_progress_comment_states(
            state.get("progress_comment_states"),
            legacy_value=state.get("recent_progress_actions"),
        )
        recent_progress_events = _normalize_recent_progress_events(state.get("recent_progress_events"))

        cleaned_actions = [
            _truncate(item, max_action_chars) for item in actions if isinstance(item, str) and item.strip()
        ][-max_recent_entries:]

        if status_comment_id:
            progress_comment_map[request_comment_id] = status_comment_id

        progress_comment_states = [
            item for item in progress_comment_states if item["request_comment_id"] != request_comment_id
        ]
        progress_entry = {
            "request_comment_id": request_comment_id,
            "status_comment_id": status_comment_id,
            "status": _truncate(status, 40),
            "summary": _truncate(summary, max_action_chars),
            "actions": cleaned_actions,
        }
        progress_comment_states.append(progress_entry)
        progress_comment_states = progress_comment_states[-max_recent_entries:]

        recent_progress_events.append(
            {
                "request_comment_id": request_comment_id,
                "status_comment_id": status_comment_id,
                "status": _truncate(status, 40),
                "summary": _truncate(summary, max_action_chars),
                "actions": cleaned_actions,
            }
        )
        recent_progress_events = recent_progress_events[-max_recent_entries:]

        retained_request_ids = {
            item["request_comment_id"]
            for item in progress_comment_states
            if isinstance(item.get("request_comment_id"), str)
        }
        progress_comment_map = {
            key: value for key, value in progress_comment_map.items() if key in retained_request_ids
        }

        updated_state = build_thread_state_payload(
            thread_id=self.thread_id,
            document_id=state.get("document_id"),
            document_title=state.get("document_title"),
            last_comment_id=state.get("last_comment_id"),
            last_comment_at=state.get("last_comment_at"),
            last_comment_preview=state.get("last_comment_preview"),
            interaction_count=(
                state.get("interaction_count") if isinstance(state.get("interaction_count"), int) else 0
            ),
            comment_count=(state.get("comment_count") if isinstance(state.get("comment_count"), int) else 0),
            assistant_turn_count=(
                state.get("assistant_turn_count") if isinstance(state.get("assistant_turn_count"), int) else 0
            ),
            participants=participants,
            recent_comments=recent_comments,
            recent_turns=recent_turns,
            recent_tool_runs=recent_tool_runs,
            progress_comment_map=progress_comment_map,
            progress_comment_states=progress_comment_states,
            recent_progress_events=recent_progress_events,
        )
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
            item.get("name") or item.get("id") for item in participants if item.get("name") or item.get("id")
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
                    build_initial_thread_state(
                        thread_id=thread_id,
                        document_id=document_id,
                        document_title=document_title,
                    ),
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
