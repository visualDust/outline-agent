from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from ..clients.model_client import ModelClient
from ..clients.outline_client import OutlineCollection, OutlineDocument
from ..core.config import AppSettings
from ..state.workspace import MEMORY_SECTION_HEADINGS, CollectionWorkspace
from ..utils.json_utils import JsonExtractionError, extract_json_object

MEMORY_UPDATE_SYSTEM_PROMPT = """You maintain durable collection-specific memory for an Outline agent.

Decide whether the agent should update its local MEMORY.md based on the latest interaction.
Only keep information that is likely to remain useful across future interactions in the same collection.
Do NOT store transient requests, raw conversation logs, vague summaries,
or sensitive personal information unless it is clearly necessary for the collection's work.

Return strict JSON only with this schema:
{{
  "should_write": true,
  "reason": "short explanation",
  "entries": [
    {{"section": "facts|decisions|notes", "text": "single durable memory item"}}
  ]
}}

Rules:
- If nothing should be saved, set should_write to false and entries to []
- At most {max_entries} entries
- Each entry must be short, concrete, and standalone
- Prefer facts and decisions over generic notes
- Avoid duplicating information already present in MEMORY.md
"""


class MemoryEntryProposal(BaseModel):
    section: Literal["facts", "decisions", "notes"]
    text: str


class MemoryUpdateProposal(BaseModel):
    should_write: bool = False
    reason: str | None = None
    entries: list[MemoryEntryProposal] = Field(default_factory=list)


class CollectionMemoryManager:
    def __init__(self, settings: AppSettings, model_client: ModelClient):
        self.settings = settings
        self.model_client = model_client

    async def propose_update(
        self,
        workspace: CollectionWorkspace,
        collection: OutlineCollection | None,
        document: OutlineDocument,
        user_comment: str,
        assistant_reply: str,
    ) -> MemoryUpdateProposal:
        system_prompt = MEMORY_UPDATE_SYSTEM_PROMPT.format(
            max_entries=self.settings.memory_update_max_entries,
        )
        user_prompt = self._build_user_prompt(
            workspace=workspace,
            collection=collection,
            document=document,
            user_comment=user_comment,
            assistant_reply=assistant_reply,
        )
        raw = await self.model_client.generate_reply(system_prompt, user_prompt)
        try:
            payload = extract_json_object(raw)
        except JsonExtractionError:
            return MemoryUpdateProposal(should_write=False, reason="model-output-not-json")

        proposal = MemoryUpdateProposal.model_validate(payload)
        return self._sanitize(proposal)

    def apply_update(self, workspace: CollectionWorkspace, proposal: MemoryUpdateProposal) -> list[str]:
        if not proposal.should_write or not proposal.entries:
            return []

        text = workspace.read_memory_text()
        applied: list[str] = []
        for section in ("facts", "decisions", "notes"):
            section_entries = [entry.text for entry in proposal.entries if entry.section == section]
            if not section_entries:
                continue
            text, appended = _append_entries_to_section(
                text=text,
                heading=MEMORY_SECTION_HEADINGS[section],
                entries=section_entries,
            )
            applied.extend(f"[{section}] {item}" for item in appended)

        if applied:
            workspace.memory_path.write_text(_ensure_trailing_newline(text), encoding="utf-8")
            write_memory_index(workspace, text)
        return applied

    def preview(self, proposal: MemoryUpdateProposal) -> str | None:
        if not proposal.entries:
            return None
        items = [f"[{entry.section}] {entry.text}" for entry in proposal.entries]
        return " ; ".join(items)

    def _build_user_prompt(
        self,
        workspace: CollectionWorkspace,
        collection: OutlineCollection | None,
        document: OutlineDocument,
        user_comment: str,
        assistant_reply: str,
    ) -> str:
        collection_name = collection.name if collection and collection.name else workspace.collection_name
        memory_excerpt = _truncate(workspace.read_memory_text(), self.settings.max_memory_chars)
        document_excerpt = _truncate(document.text or "", self.settings.max_document_chars)
        return (
            f"Collection: {collection_name}\n"
            f"Collection ID: {workspace.collection_id}\n"
            f"Document title: {document.title or '(unknown)'}\n\n"
            "Current MEMORY.md excerpt:\n"
            f"{memory_excerpt}\n\n"
            "Document excerpt:\n"
            f"{document_excerpt or '(document text unavailable)'}\n\n"
            "Latest user comment:\n"
            f"{_truncate(user_comment, self.settings.max_prompt_chars)}\n\n"
            "Assistant reply:\n"
            f"{_truncate(assistant_reply, self.settings.max_prompt_chars)}"
        )

    def _sanitize(self, proposal: MemoryUpdateProposal) -> MemoryUpdateProposal:
        cleaned: list[MemoryEntryProposal] = []
        seen: set[tuple[str, str]] = set()
        for entry in proposal.entries:
            text = _normalize_memory_text(entry.text)
            if not text:
                continue
            text = _truncate(text, self.settings.memory_update_entry_max_chars)
            key = (entry.section, text.casefold())
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(MemoryEntryProposal(section=entry.section, text=text))
            if len(cleaned) >= self.settings.memory_update_max_entries:
                break

        should_write = proposal.should_write and bool(cleaned)
        return MemoryUpdateProposal(
            should_write=should_write,
            reason=proposal.reason,
            entries=cleaned,
        )


def _append_entries_to_section(text: str, heading: str, entries: list[str]) -> tuple[str, list[str]]:
    lines = text.splitlines()
    marker = f"## {heading}"
    try:
        heading_index = lines.index(marker)
    except ValueError:
        if lines and lines[-1] != "":
            lines.append("")
        lines.extend([marker, *[f"- {entry}" for entry in entries]])
        return "\n".join(lines), entries

    section_end = heading_index + 1
    while section_end < len(lines) and not lines[section_end].startswith("## "):
        section_end += 1

    existing = {
        _normalize_memory_text(line[2:]).casefold()
        for line in lines[heading_index + 1 : section_end]
        if line.startswith("- ")
    }
    appended: list[str] = []
    for entry in entries:
        normalized = _normalize_memory_text(entry).casefold()
        if normalized in existing:
            continue
        existing.add(normalized)
        appended.append(entry)

    if not appended:
        return "\n".join(lines), []

    insertion = [f"- {entry}" for entry in appended]
    if section_end > heading_index + 1 and lines[section_end - 1] != "":
        lines[section_end:section_end] = insertion
    else:
        lines[section_end:section_end] = insertion
    return "\n".join(lines), appended


def _normalize_memory_text(text: str) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) < 8:
        return ""
    return compact


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _ensure_trailing_newline(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"


def write_memory_index(workspace: CollectionWorkspace, text: str | None = None) -> None:
    memory_text = text if text is not None else workspace.read_memory_text()
    index = build_memory_index(memory_text)
    index_path = workspace.memory_dir / "index.json"
    index_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def build_memory_index(text: str) -> dict[str, object]:
    reverse_headings = {heading: section for section, heading in MEMORY_SECTION_HEADINGS.items()}
    items: list[dict[str, str]] = []
    current_section: str | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            heading = line[3:].strip()
            current_section = reverse_headings.get(heading)
            continue
        if current_section and line.startswith("- "):
            entry = line[2:].strip()
            if entry:
                items.append({"section": current_section, "text": entry})
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
    }
