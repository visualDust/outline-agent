from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.logging import logger
from .thread_state import build_initial_thread_state, build_thread_state_payload
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
from .thread_transcript import active_comments as _active_transcript_comments
from .thread_transcript import build_deleted_thread_transcript as _build_deleted_thread_transcript
from .thread_transcript import build_thread_transcript as _build_thread_transcript
from .thread_transcript import load_json_text as _load_transcript_json_text
from .thread_transcript import render_comments_for_prompt as _render_comments_for_prompt
from .thread_transcript import summarize_comments_for_prompt as _summarize_comments_for_prompt
from .thread_transcript import to_json_text as _transcript_to_json_text
from .thread_transcript import transcript_comment_count as _transcript_comment_count
from .thread_transcript import transcript_root_exists as _transcript_root_exists

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

INITIAL_DOCUMENT_MEMORY_TEMPLATE = """# MEMORY.md - Document Working Memory

This file stores durable document-local context for the Outline agent.

## Document Profile
- Document ID: {document_id}
- Document Title: {document_title}

## Summary

## Open Questions

## Working Notes
- {created_note}: Document memory initialized automatically.
"""

MEMORY_SECTION_HEADINGS = {
    "facts": "Durable Facts",
    "decisions": "Decisions",
    "notes": "Working Notes",
}

DOCUMENT_MEMORY_SECTION_HEADINGS = (
    "Summary",
    "Open Questions",
    "Working Notes",
)


@dataclass(frozen=True)
class CollectionWorkspace:
    collection_id: str
    collection_name: str
    root_dir: Path
    memory_dir: Path
    workspace_dir: Path
    attachments_dir: Path
    generated_dir: Path
    scratch_dir: Path
    documents_dir: Path
    threads_dir: Path
    archived_threads_dir: Path
    archived_documents_dir: Path
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
class DocumentWorkspace:
    document_id: str
    root_dir: Path
    memory_path: Path
    state_path: Path

    def load_prompt_context(self, max_chars: int) -> str:
        sections: list[str] = []
        memory_text = self.memory_path.read_text(encoding="utf-8").strip()
        if memory_text:
            sections.append(f"## {self.memory_path.name}\n{memory_text}")

        state = self.read_state()
        state_lines = _format_document_state_for_prompt(state)
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

    def write_state(self, payload: dict[str, Any]) -> None:
        self.state_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def mark_deleted(
        self,
        *,
        document_title: str | None,
        reason: str,
    ) -> None:
        state = self.read_state()
        state["document_id"] = self.document_id
        state["document_title"] = document_title
        state["deleted"] = True
        state["deleted_reason"] = reason
        state["deleted_at"] = datetime.now(timezone.utc).isoformat()
        self.write_state(state)
        logger.debug(
            "Document workspace marked deleted: document_id={}, root_dir={}, reason={}",
            self.document_id,
            self.root_dir,
            reason,
        )


@dataclass(frozen=True)
class ThreadWorkspace:
    thread_id: str
    root_dir: Path
    state_path: Path
    events_path: Path
    comments_path: Path

    def load_prompt_context(self, max_chars: int) -> str:
        state_lines = _format_thread_state_for_prompt(self.read_state())
        combined = "## state.json\n" + "\n".join(state_lines) if state_lines else ""
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

    def read_transcript(self) -> dict[str, Any]:
        if not self.comments_path.exists():
            return {}
        return _load_transcript_json_text(self.comments_path.read_text(encoding="utf-8"))

    def write_transcript(self, transcript: dict[str, Any]) -> None:
        self.comments_path.write_text(_transcript_to_json_text(transcript), encoding="utf-8")

    def sync_transcript_from_comments(
        self,
        *,
        document_id: str,
        document_title: str | None,
        comments: list[Any],
        max_recent_comments: int,
        max_comment_chars: int,
    ) -> dict[str, Any]:
        transcript = _build_thread_transcript(
            thread_id=self.thread_id,
            document_id=document_id,
            document_title=document_title,
            comments=comments,
        )
        self.write_transcript(transcript)
        self.rebuild_comment_state_from_transcript(
            document_id=document_id,
            document_title=document_title,
            max_recent_comments=max_recent_comments,
            max_comment_chars=max_comment_chars,
        )
        logger.debug(
            "Thread transcript synchronized: thread_id={}, document_id={}, comment_count={}",
            self.thread_id,
            document_id,
            _transcript_comment_count(transcript),
        )
        return transcript

    def mark_deleted(
        self,
        *,
        document_id: str,
        document_title: str | None,
    ) -> None:
        transcript = _build_deleted_thread_transcript(
            thread_id=self.thread_id,
            document_id=document_id,
            document_title=document_title,
        )
        self.write_transcript(transcript)
        self.rebuild_comment_state_from_transcript(
            document_id=document_id,
            document_title=document_title,
            max_recent_comments=0,
            max_comment_chars=0,
        )
        logger.debug(
            "Thread workspace marked deleted: thread_id={}, document_id={}, root_dir={}",
            self.thread_id,
            document_id,
            self.root_dir,
        )

    def rebuild_comment_state_from_transcript(
        self,
        *,
        document_id: str,
        document_title: str | None,
        max_recent_comments: int,
        max_comment_chars: int,
    ) -> dict[str, Any]:
        transcript = self.read_transcript()
        state = self.read_state()
        recent_turns = _normalize_recent_turns(state.get("recent_turns"))
        recent_tool_runs = _normalize_recent_tool_runs(state.get("recent_tool_runs"))
        progress_comment_map = _normalize_progress_comment_map(state.get("progress_comment_map"))
        progress_comment_states = _normalize_progress_comment_states(
            state.get("progress_comment_states"),
            legacy_value=state.get("recent_progress_actions"),
        )
        recent_progress_events = _normalize_recent_progress_events(state.get("recent_progress_events"))
        transcript_comments = _active_transcript_comments(transcript)

        participants: list[dict[str, str | None]] = []
        recent_comments: list[dict[str, str | None]] = []
        for item in transcript_comments:
            participants = _upsert_participant(
                participants,
                author_id=item.get("author_id") if isinstance(item.get("author_id"), str) else None,
                author_name=item.get("author_name") if isinstance(item.get("author_name"), str) else None,
            )
            body_plain = item.get("body_plain") if isinstance(item.get("body_plain"), str) else ""
            recent_comments.append(
                {
                    "comment_id": str(item.get("id") or ""),
                    "author_id": item.get("author_id") if isinstance(item.get("author_id"), str) else None,
                    "author_name": item.get("author_name") if isinstance(item.get("author_name"), str) else None,
                    "text": _truncate(body_plain, max_comment_chars) if max_comment_chars > 0 else "",
                    "created_at": item.get("created_at") if isinstance(item.get("created_at"), str) else None,
                }
            )
        if max_recent_comments > 0:
            recent_comments = _sort_recent_comments(recent_comments)[-max_recent_comments:]
        else:
            recent_comments = []

        last_comment = transcript_comments[-1] if transcript_comments else None
        last_comment_id = str(last_comment.get("id") or "") if isinstance(last_comment, dict) else None
        last_comment_at = (
            last_comment.get("created_at")
            if isinstance(last_comment, dict) and isinstance(last_comment.get("created_at"), str)
            else None
        )
        last_comment_preview = None
        if isinstance(last_comment, dict):
            preview_text = last_comment.get("body_plain")
            if isinstance(preview_text, str):
                last_comment_preview = (
                    _truncate(preview_text, max_comment_chars)
                    if max_comment_chars > 0
                    else preview_text
                )

        updated_state = build_thread_state_payload(
            thread_id=self.thread_id,
            document_id=document_id,
            document_title=document_title,
            last_comment_id=last_comment_id or None,
            last_comment_at=last_comment_at,
            last_comment_preview=last_comment_preview,
            interaction_count=(
                state.get("interaction_count") if isinstance(state.get("interaction_count"), int) else len(recent_turns)
            ),
            comment_count=len(transcript_comments),
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
        return updated_state

    def build_comment_context(
        self,
        *,
        current_comment_id: str,
        max_full_thread_chars: int,
        tail_comment_count: int,
        summary_max_chars: int,
    ) -> str:
        transcript = self.read_transcript()
        comments = _active_transcript_comments(transcript)
        if not comments:
            return "(no additional comment context)"

        full = _render_comments_for_prompt(comments=comments, current_comment_id=current_comment_id)
        if len(full) <= max_full_thread_chars:
            logger.debug(
                "Using full thread context: thread_id={}, comment_count={}, chars={}",
                self.thread_id,
                len(comments),
                len(full),
            )
            return full

        root_comments = comments[:1]
        tail_start = max(1, len(comments) - max(1, tail_comment_count))
        middle_comments = comments[1:tail_start]
        tail_comments = comments[tail_start:]

        sections = ["Earlier comment history was truncated for context budget."]
        root_text = _render_comments_for_prompt(comments=root_comments, current_comment_id=current_comment_id)
        if root_text:
            sections.append("Root comment:\n" + root_text)
        summary_text = _summarize_comments_for_prompt(middle_comments, max_chars=summary_max_chars)
        if summary_text:
            sections.append("Distilled earlier thread history:\n" + summary_text)
        tail_text = _render_comments_for_prompt(comments=tail_comments, current_comment_id=current_comment_id)
        if tail_text:
            sections.append(f"Latest {len(tail_comments)} comments:\n" + tail_text)
        sections.append(
            "If exact omitted history is needed, use the `get_thread_history` tool to inspect the truncated segment."
        )
        truncated = "\n\n".join(section for section in sections if section.strip())
        logger.debug(
            "Using truncated thread context: thread_id={}, comment_count={}, full_chars={}, final_chars={}",
            self.thread_id,
            len(comments),
            len(full),
            len(truncated),
        )
        return truncated

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
        self._upsert_transcript_comment(
            comment_id=comment_id,
            parent_comment_id=parent_comment_id,
            author_id=author_id,
            author_name=author_name,
            created_at=created_at,
            comment_text=comment_text,
            document_id=document_id,
            document_title=document_title,
        )
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
        preview_parts: list[str] = []
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
            "session_summary": None,
            "recent_preview": " | ".join(part for part in preview_parts if isinstance(part, str) and part.strip()),
            "search_text": "\n".join(chunk for chunk in searchable_chunks if isinstance(chunk, str) and chunk.strip()),
        }

    def _write_state(self, payload: dict[str, Any]) -> None:
        self.state_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _upsert_transcript_comment(
        self,
        *,
        comment_id: str,
        parent_comment_id: str | None,
        author_id: str | None,
        author_name: str | None,
        created_at: str | None,
        comment_text: str,
        document_id: str,
        document_title: str | None,
    ) -> None:
        transcript = self.read_transcript()
        comments = transcript.get("comments") if isinstance(transcript.get("comments"), list) else []
        remaining = [item for item in comments if not (isinstance(item, dict) and item.get("id") == comment_id)]
        remaining.append(
            {
                "id": comment_id,
                "parent_comment_id": parent_comment_id,
                "author_id": author_id,
                "author_name": author_name,
                "created_at": created_at,
                "updated_at": None,
                "deleted_at": None,
                "body_rich": {},
                "body_plain": comment_text,
            }
        )
        remaining.sort(key=lambda item: ((item.get("created_at") or ""), (item.get("id") or "")))
        normalized = {
            "thread_id": self.thread_id,
            "root_comment_id": self.thread_id,
            "document_id": document_id,
            "document_title": document_title,
            "deleted": False,
            "comments": remaining,
        }
        self.write_transcript(normalized)


class CollectionWorkspaceManager:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.archived_collections_dir = self.root / "archived_collections"
        self.archived_collections_dir.mkdir(parents=True, exist_ok=True)

    def ensure(self, collection_id: str, collection_name: str | None) -> CollectionWorkspace:
        safe_name = _slugify(collection_name or "collection")
        workspace_dir = self.root / f"{safe_name}-{collection_id}"
        memory_dir = workspace_dir / "memory"
        collection_workspace_dir = workspace_dir / "workspace"
        attachments_dir = collection_workspace_dir / "attachments"
        generated_dir = collection_workspace_dir / "generated"
        scratch_dir = workspace_dir / "scratch"
        documents_dir = workspace_dir / "documents"
        threads_dir = workspace_dir / "threads"
        archived_threads_dir = workspace_dir / "archived_threads"
        archived_documents_dir = workspace_dir / "archived_documents"
        memory_dir.mkdir(parents=True, exist_ok=True)
        collection_workspace_dir.mkdir(parents=True, exist_ok=True)
        attachments_dir.mkdir(parents=True, exist_ok=True)
        generated_dir.mkdir(parents=True, exist_ok=True)
        scratch_dir.mkdir(parents=True, exist_ok=True)
        documents_dir.mkdir(parents=True, exist_ok=True)
        threads_dir.mkdir(parents=True, exist_ok=True)
        archived_threads_dir.mkdir(parents=True, exist_ok=True)
        archived_documents_dir.mkdir(parents=True, exist_ok=True)

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
            workspace_dir=collection_workspace_dir,
            attachments_dir=attachments_dir,
            generated_dir=generated_dir,
            scratch_dir=scratch_dir,
            documents_dir=documents_dir,
            threads_dir=threads_dir,
            archived_threads_dir=archived_threads_dir,
            archived_documents_dir=archived_documents_dir,
            system_prompt_path=system_prompt_path,
            memory_path=memory_path,
        )

    def ensure_document(
        self,
        workspace: CollectionWorkspace,
        *,
        document_id: str,
        document_title: str | None,
    ) -> DocumentWorkspace:
        safe_document_id = _slugify(document_id)
        document_dir = workspace.documents_dir / safe_document_id
        document_dir.mkdir(parents=True, exist_ok=True)

        memory_path = document_dir / "MEMORY.md"
        state_path = document_dir / "state.json"
        resolved_title = document_title or "(unknown)"

        if not memory_path.exists():
            memory_path.write_text(
                INITIAL_DOCUMENT_MEMORY_TEMPLATE.format(
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
                    build_initial_document_state(
                        document_id=document_id,
                        document_title=document_title,
                    ),
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        else:
            try:
                state_payload = json.loads(state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                state_payload = {}
            if not isinstance(state_payload, dict):
                state_payload = {}
            state_payload["document_id"] = document_id
            state_payload["document_title"] = document_title
            state_path.write_text(
                json.dumps(state_payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

        self._refresh_document_metadata(
            memory_path,
            document_id=document_id,
            document_title=resolved_title,
        )
        self._ensure_document_memory_sections(memory_path)

        return DocumentWorkspace(
            document_id=document_id,
            root_dir=document_dir,
            memory_path=memory_path,
            state_path=state_path,
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

        state_path = thread_dir / "state.json"
        events_path = thread_dir / "events.jsonl"
        comments_path = thread_dir / "comments.json"

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

        if not events_path.exists():
            events_path.write_text("", encoding="utf-8")

        if not comments_path.exists():
            comments_path.write_text(
                _transcript_to_json_text(
                    _build_deleted_thread_transcript(
                        thread_id=thread_id,
                        document_id=document_id,
                        document_title=document_title,
                    )
                ),
                encoding="utf-8",
            )

        return ThreadWorkspace(
            thread_id=thread_id,
            root_dir=thread_dir,
            state_path=state_path,
            events_path=events_path,
            comments_path=comments_path,
        )

    def archive_thread(self, workspace: CollectionWorkspace, thread_workspace: ThreadWorkspace, *, reason: str) -> Path:
        destination = workspace.archived_threads_dir / thread_workspace.root_dir.name
        if destination.exists():
            suffix = 1
            while (workspace.archived_threads_dir / f"{thread_workspace.root_dir.name}-{suffix}").exists():
                suffix += 1
            destination = workspace.archived_threads_dir / f"{thread_workspace.root_dir.name}-{suffix}"
        thread_workspace.root_dir.rename(destination)
        logger.debug(
            "Archived thread workspace: thread_id={}, from={}, to={}, reason={}",
            thread_workspace.thread_id,
            thread_workspace.root_dir,
            destination,
            reason,
        )
        return destination

    def archive_document(
        self,
        workspace: CollectionWorkspace,
        document_workspace: DocumentWorkspace,
        *,
        reason: str,
    ) -> Path:
        workspace.archived_documents_dir.mkdir(parents=True, exist_ok=True)
        destination = workspace.archived_documents_dir / document_workspace.root_dir.name
        if destination.exists():
            suffix = 1
            while (workspace.archived_documents_dir / f"{document_workspace.root_dir.name}-{suffix}").exists():
                suffix += 1
            destination = workspace.archived_documents_dir / f"{document_workspace.root_dir.name}-{suffix}"
        document_workspace.root_dir.rename(destination)
        logger.debug(
            "Archived document workspace: document_id={}, from={}, to={}, reason={}",
            document_workspace.document_id,
            document_workspace.root_dir,
            destination,
            reason,
        )
        return destination

    def archive_collection(self, workspace: CollectionWorkspace, *, reason: str) -> Path:
        self.archived_collections_dir.mkdir(parents=True, exist_ok=True)
        destination = self.archived_collections_dir / workspace.root_dir.name
        if destination.exists():
            suffix = 1
            while (self.archived_collections_dir / f"{workspace.root_dir.name}-{suffix}").exists():
                suffix += 1
            destination = self.archived_collections_dir / f"{workspace.root_dir.name}-{suffix}"
        workspace.root_dir.rename(destination)
        logger.debug(
            "Archived collection workspace: collection_id={}, from={}, to={}, reason={}",
            workspace.collection_id,
            workspace.root_dir,
            destination,
            reason,
        )
        return destination

    def find_collection(self, collection_id: str) -> CollectionWorkspace | None:
        safe_collection_id = _slugify(collection_id)
        suffix = f"-{safe_collection_id}"
        matched_dirs: list[str] = []
        for workspace_dir in sorted(self.root.iterdir()):
            if not workspace_dir.is_dir() or workspace_dir == self.archived_collections_dir:
                continue
            if workspace_dir.name.endswith(suffix):
                matched_dirs.append(str(workspace_dir))
                logger.debug(
                    "Collection workspace lookup matched: collection_id={}, search_root={}, suffix={}, matched_dir={}",
                    collection_id,
                    self.root,
                    suffix,
                    workspace_dir,
                )
                return self._load_collection_workspace(workspace_dir, collection_id=collection_id)
        logger.debug(
            "Collection workspace lookup missed: collection_id={}, search_root={}, suffix={}, matched_dirs={}",
            collection_id,
            self.root,
            suffix,
            matched_dirs or None,
        )
        return None

    def find_archived_collection_dir(self, collection_id: str) -> Path | None:
        safe_collection_id = _slugify(collection_id)
        suffix = f"-{safe_collection_id}"
        matched_dirs: list[str] = []
        for workspace_dir in sorted(self.archived_collections_dir.iterdir()):
            if workspace_dir.is_dir() and workspace_dir.name.endswith(suffix):
                matched_dirs.append(str(workspace_dir))
                logger.debug(
                    "Archived collection lookup matched: collection_id={}, search_root={}, suffix={}, matched_dir={}",
                    collection_id,
                    self.archived_collections_dir,
                    suffix,
                    workspace_dir,
                )
                return workspace_dir
        logger.debug(
            "Archived collection lookup missed: collection_id={}, search_root={}, suffix={}, matched_dirs={}",
            collection_id,
            self.archived_collections_dir,
            suffix,
            matched_dirs or None,
        )
        return None

    def find_collection_for_document(self, document_id: str) -> CollectionWorkspace | None:
        scanned_collections: list[str] = []
        for workspace in self.list_active_collections():
            scanned_collections.append(str(workspace.root_dir))
            if self.find_document(workspace, document_id=document_id) is not None:
                logger.debug(
                    "Collection lookup by document matched: document_id={}, matched_collection_id={}, "
                    "matched_workspace={}, scanned_collections={}",
                    document_id,
                    workspace.collection_id,
                    workspace.root_dir,
                    scanned_collections,
                )
                return workspace
        logger.debug(
            "Collection lookup by document missed: document_id={}, scanned_collections={}",
            document_id,
            scanned_collections or None,
        )
        return None

    def list_active_collections(self) -> list[CollectionWorkspace]:
        workspaces: list[CollectionWorkspace] = []
        for workspace_dir in sorted(self.root.iterdir()):
            if not workspace_dir.is_dir() or workspace_dir == self.archived_collections_dir:
                continue
            collection_id = self._read_collection_metadata_value(
                workspace_dir / "memory" / "MEMORY.md",
                "Collection ID",
            )
            if collection_id:
                workspaces.append(self._load_collection_workspace(workspace_dir, collection_id=collection_id))
        return workspaces

    def find_document(self, workspace: CollectionWorkspace, *, document_id: str) -> DocumentWorkspace | None:
        document_dir = self.document_workspace_path(workspace, document_id=document_id)
        logger.debug(
            "Document workspace lookup: collection_id={}, document_id={}, candidate_path={}, exists={}",
            workspace.collection_id,
            document_id,
            document_dir,
            document_dir.is_dir(),
        )
        if not document_dir.is_dir():
            return None
        return DocumentWorkspace(
            document_id=document_id,
            root_dir=document_dir,
            memory_path=document_dir / "MEMORY.md",
            state_path=document_dir / "state.json",
        )

    def find_archived_document_dir(self, workspace: CollectionWorkspace, *, document_id: str) -> Path | None:
        document_dir = self.archived_document_workspace_path(workspace, document_id=document_id)
        logger.debug(
            "Archived document lookup: collection_id={}, document_id={}, candidate_path={}, exists={}",
            workspace.collection_id,
            document_id,
            document_dir,
            document_dir.is_dir(),
        )
        if document_dir.is_dir():
            return document_dir
        return None

    def find_archived_document_globally(self, *, document_id: str) -> Path | None:
        safe_document_id = _slugify(document_id)
        checked_paths: list[str] = []
        for workspace in self.list_active_collections():
            archived_document_dir = workspace.archived_documents_dir / safe_document_id
            checked_paths.append(str(archived_document_dir))
            if archived_document_dir.is_dir():
                logger.debug(
                    "Global archived document lookup matched active collection archive: document_id={}, "
                    "candidate_path={}, checked_paths={}",
                    document_id,
                    archived_document_dir,
                    checked_paths,
                )
                return archived_document_dir
        for collection_dir in sorted(self.archived_collections_dir.iterdir()):
            archived_document_dir = collection_dir / "archived_documents" / safe_document_id
            checked_paths.append(str(archived_document_dir))
            if archived_document_dir.is_dir():
                logger.debug(
                    "Global archived document lookup matched archived collection archive: document_id={}, "
                    "candidate_path={}, checked_paths={}",
                    document_id,
                    archived_document_dir,
                    checked_paths,
                )
                return archived_document_dir
            active_document_dir = collection_dir / "documents" / safe_document_id
            checked_paths.append(str(active_document_dir))
            if active_document_dir.is_dir():
                logger.debug(
                    "Global archived document lookup matched archived collection active-doc subtree: "
                    "document_id={}, candidate_path={}, checked_paths={}",
                    document_id,
                    active_document_dir,
                    checked_paths,
                )
                return active_document_dir
        logger.debug(
            "Global archived document lookup missed: document_id={}, checked_paths={}",
            document_id,
            checked_paths or None,
        )
        return None

    def document_workspace_path(self, workspace: CollectionWorkspace, *, document_id: str) -> Path:
        return workspace.documents_dir / _slugify(document_id)

    def archived_document_workspace_path(self, workspace: CollectionWorkspace, *, document_id: str) -> Path:
        return workspace.archived_documents_dir / _slugify(document_id)

    def list_active_thread_workspaces_for_document(
        self,
        workspace: CollectionWorkspace,
        *,
        document_id: str,
    ) -> list[ThreadWorkspace]:
        thread_workspaces: list[ThreadWorkspace] = []
        if not workspace.threads_dir.exists():
            return thread_workspaces

        for thread_dir in sorted(workspace.threads_dir.iterdir()):
            if not thread_dir.is_dir():
                continue
            thread_workspace = ThreadWorkspace(
                thread_id=thread_dir.name,
                root_dir=thread_dir,
                state_path=thread_dir / "state.json",
                events_path=thread_dir / "events.jsonl",
                comments_path=thread_dir / "comments.json",
            )
            state = thread_workspace.read_state()
            if state.get("document_id") == document_id:
                thread_workspaces.append(thread_workspace)

        logger.debug(
            "Listed active thread workspaces for document archive: collection_id={}, document_id={}, count={}",
            workspace.collection_id,
            document_id,
            len(thread_workspaces),
        )
        return thread_workspaces

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
                state_path=thread_dir / "state.json",
                events_path=thread_dir / "events.jsonl",
                comments_path=thread_dir / "comments.json",
            )
            state = thread_workspace.read_state()
            transcript = thread_workspace.read_transcript()
            if transcript.get("deleted") is True or not _transcript_root_exists(transcript):
                logger.debug(
                    "Skipping thread entry candidate: thread_id={}, deleted={}, root_exists={}",
                    thread_dir.name,
                    transcript.get("deleted") is True,
                    _transcript_root_exists(transcript),
                )
                continue
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
        logger.debug(
            "Listed document thread entries: document_id={}, exclude_thread_id={}, count={}",
            document_id,
            exclude_thread_id,
            len(entries),
        )
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

    def _refresh_document_metadata(
        self,
        memory_path: Path,
        *,
        document_id: str,
        document_title: str,
    ) -> None:
        text = memory_path.read_text(encoding="utf-8")
        text = re.sub(r"^- Document ID: .*?$", f"- Document ID: {document_id}", text, flags=re.MULTILINE)
        text = re.sub(r"^- Document Title: .*?$", f"- Document Title: {document_title}", text, flags=re.MULTILINE)
        memory_path.write_text(text, encoding="utf-8")

    def _ensure_document_memory_sections(self, memory_path: Path) -> None:
        text = memory_path.read_text(encoding="utf-8").rstrip()
        for heading in DOCUMENT_MEMORY_SECTION_HEADINGS:
            marker = f"## {heading}"
            if marker not in text:
                text += f"\n\n{marker}\n"
        memory_path.write_text(text + "\n", encoding="utf-8")

    def _load_collection_workspace(self, workspace_dir: Path, *, collection_id: str) -> CollectionWorkspace:
        memory_dir = workspace_dir / "memory"
        collection_workspace_dir = workspace_dir / "workspace"
        collection_name = (
            self._read_collection_metadata_value(memory_dir / "MEMORY.md", "Collection Name") or collection_id
        )
        return CollectionWorkspace(
            collection_id=collection_id,
            collection_name=collection_name,
            root_dir=workspace_dir,
            memory_dir=memory_dir,
            workspace_dir=collection_workspace_dir,
            attachments_dir=collection_workspace_dir / "attachments",
            generated_dir=collection_workspace_dir / "generated",
            scratch_dir=workspace_dir / "scratch",
            documents_dir=workspace_dir / "documents",
            threads_dir=workspace_dir / "threads",
            archived_threads_dir=workspace_dir / "archived_threads",
            archived_documents_dir=workspace_dir / "archived_documents",
            system_prompt_path=memory_dir / "00_SYSTEM.md",
            memory_path=memory_dir / "MEMORY.md",
        )

    def _read_collection_metadata_value(self, memory_path: Path, field_name: str) -> str | None:
        if not memory_path.exists():
            return None
        text = memory_path.read_text(encoding="utf-8")
        match = re.search(rf"^- {re.escape(field_name)}: (.+?)$", text, flags=re.MULTILINE)
        if match:
            value = match.group(1).strip()
            if value:
                return value
        return None


def build_initial_document_state(*, document_id: str, document_title: str | None) -> dict[str, Any]:
    return {
        "document_id": document_id,
        "document_title": document_title,
    }


def _format_document_state_for_prompt(state: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    document_id = state.get("document_id")
    if isinstance(document_id, str) and document_id:
        lines.append(f"- document_id: {document_id}")
    document_title = state.get("document_title")
    if isinstance(document_title, str) and document_title:
        lines.append(f"- document_title: {document_title}")
    return lines
