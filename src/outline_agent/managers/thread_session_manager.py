from __future__ import annotations

import json
import re

from pydantic import BaseModel, Field

from ..clients.model_client import ModelClient
from ..clients.outline_client import OutlineCollection, OutlineDocument
from ..core.config import AppSettings
from ..state.workspace import ThreadWorkspace
from ..utils.json_utils import JsonExtractionError, extract_json_object

THREAD_SESSION_UPDATE_SYSTEM_PROMPT = """You maintain durable thread-local SESSION.md state for an Outline agent.

Decide whether the thread SESSION.md should be updated based on the latest interaction.
Keep the session useful for future follow-up turns in the same comment thread.
Do NOT store raw transcripts, long verbatim excerpts, or sensitive information unless it is clearly necessary.

Return strict JSON only with this schema:
{{
  "should_write": true,
  "summary": "short thread summary",
  "open_questions": ["short unresolved question"],
  "working_notes": ["short reusable thread note"]
}}

Rules:
- If no update is needed, set should_write to false
- Keep the summary concise and standalone
- At most {max_open_questions} open questions
- At most {max_working_notes} working notes
- Use open_questions only for unresolved asks or blockers still relevant
- Working notes should be short reusable thread-local notes, not a transcript
- Prefer replacing stale session text with a better distilled summary
"""


class ThreadSessionUpdateProposal(BaseModel):
    should_write: bool = False
    summary: str | None = None
    open_questions: list[str] = Field(default_factory=list)
    working_notes: list[str] = Field(default_factory=list)


class ThreadSessionManager:
    def __init__(self, settings: AppSettings, model_client: ModelClient):
        self.settings = settings
        self.model_client = model_client

    async def propose_update(
        self,
        *,
        thread_workspace: ThreadWorkspace,
        collection: OutlineCollection | None,
        document: OutlineDocument,
        user_comment: str,
        assistant_reply: str,
    ) -> ThreadSessionUpdateProposal:
        system_prompt = THREAD_SESSION_UPDATE_SYSTEM_PROMPT.format(
            max_open_questions=self.settings.thread_session_max_open_questions,
            max_working_notes=self.settings.thread_session_max_working_notes,
        )
        user_prompt = self._build_user_prompt(
            thread_workspace=thread_workspace,
            collection=collection,
            document=document,
            user_comment=user_comment,
            assistant_reply=assistant_reply,
        )
        raw = await self.model_client.generate_reply(system_prompt, user_prompt)
        try:
            payload = extract_json_object(raw)
        except JsonExtractionError:
            return ThreadSessionUpdateProposal(should_write=False)

        proposal = ThreadSessionUpdateProposal.model_validate(payload)
        return self._sanitize(proposal)

    def apply_update(self, thread_workspace: ThreadWorkspace, proposal: ThreadSessionUpdateProposal) -> str | None:
        if not proposal.should_write:
            return None

        text = thread_workspace.session_path.read_text(encoding="utf-8")
        text = _replace_section(
            text=text,
            heading="Session Summary",
            body_lines=[proposal.summary] if proposal.summary else [],
        )
        text = _replace_section(
            text=text,
            heading="Open Questions",
            body_lines=[f"- {item}" for item in proposal.open_questions],
        )
        text = _replace_section(
            text=text,
            heading="Working Notes",
            body_lines=[f"- {item}" for item in proposal.working_notes],
        )
        thread_workspace.session_path.write_text(_ensure_trailing_newline(text), encoding="utf-8")
        return self.preview(proposal)

    def preview(self, proposal: ThreadSessionUpdateProposal) -> str | None:
        if not proposal.should_write:
            return None

        parts: list[str] = []
        if proposal.summary:
            parts.append(f"summary={proposal.summary}")
        if proposal.open_questions:
            parts.append("open_questions=" + " | ".join(proposal.open_questions))
        if proposal.working_notes:
            parts.append("working_notes=" + " | ".join(proposal.working_notes))
        return " ; ".join(parts) if parts else None

    def _build_user_prompt(
        self,
        *,
        thread_workspace: ThreadWorkspace,
        collection: OutlineCollection | None,
        document: OutlineDocument,
        user_comment: str,
        assistant_reply: str,
    ) -> str:
        collection_name = collection.name if collection and collection.name else document.collection_id or "(unknown)"
        session_excerpt = _truncate(
            thread_workspace.session_path.read_text(encoding="utf-8"),
            self.settings.max_thread_session_chars,
        )
        state_excerpt = _truncate(
            json.dumps(thread_workspace.read_state(), ensure_ascii=False, indent=2),
            self.settings.max_thread_session_chars,
        )
        document_excerpt = _truncate(document.text or "", self.settings.max_document_chars)
        return (
            f"Collection: {collection_name}\n"
            f"Collection ID: {document.collection_id or '(unknown)'}\n"
            f"Thread ID: {thread_workspace.thread_id}\n"
            f"Document title: {document.title or '(unknown)'}\n\n"
            "Current SESSION.md excerpt:\n"
            f"{session_excerpt}\n\n"
            "Current state.json excerpt:\n"
            f"{state_excerpt}\n\n"
            "Document excerpt:\n"
            f"{document_excerpt or '(document text unavailable)'}\n\n"
            "Latest user comment:\n"
            f"{_truncate(user_comment, self.settings.max_prompt_chars)}\n\n"
            "Assistant reply:\n"
            f"{_truncate(assistant_reply, self.settings.max_prompt_chars)}"
        )

    def _sanitize(self, proposal: ThreadSessionUpdateProposal) -> ThreadSessionUpdateProposal:
        summary = _normalize_text(proposal.summary or "")
        if summary:
            summary = _truncate(summary, self.settings.thread_session_summary_max_chars)

        open_questions = _normalize_unique_items(
            proposal.open_questions,
            limit=self.settings.thread_session_max_open_questions,
            item_max_chars=self.settings.thread_session_item_max_chars,
        )
        working_notes = _normalize_unique_items(
            proposal.working_notes,
            limit=self.settings.thread_session_max_working_notes,
            item_max_chars=self.settings.thread_session_item_max_chars,
        )

        should_write = proposal.should_write and bool(summary or open_questions or working_notes)
        return ThreadSessionUpdateProposal(
            should_write=should_write,
            summary=summary or None,
            open_questions=open_questions,
            working_notes=working_notes,
        )


def _replace_section(text: str, heading: str, body_lines: list[str]) -> str:
    lines = text.splitlines()
    marker = f"## {heading}"
    try:
        start_index = lines.index(marker)
    except ValueError:
        if lines and lines[-1] != "":
            lines.append("")
        lines.append(marker)
        start_index = len(lines) - 1

    end_index = start_index + 1
    while end_index < len(lines) and not lines[end_index].startswith("## "):
        end_index += 1

    replacement = [marker]
    replacement.extend(body_lines)
    replacement.append("")

    return "\n".join(lines[:start_index] + replacement + lines[end_index:]).rstrip() + "\n"


def _normalize_unique_items(items: list[str], *, limit: int, item_max_chars: int) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in items:
        normalized = _normalize_text(item)
        if not normalized:
            continue
        normalized = _truncate(normalized, item_max_chars)
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(normalized)
        if len(cleaned) >= limit:
            break
    return cleaned


def _normalize_text(text: str) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) < 5:
        return ""
    return compact


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _ensure_trailing_newline(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"
