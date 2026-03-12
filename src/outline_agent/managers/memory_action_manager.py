from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from ..clients.model_client import ModelClient
from ..clients.outline_client import OutlineCollection, OutlineDocument
from ..core.config import AppSettings
from ..state.workspace import MEMORY_SECTION_HEADINGS, CollectionWorkspace
from ..utils.json_utils import JsonExtractionError, extract_json_object
from .memory_manager import write_memory_index

MEMORY_ACTION_SYSTEM_PROMPT = """You manage explicit memory actions for an Outline agent.

Only propose actions when the user explicitly asks to remember, correct, delete, or promote
collection memory. Do not invent memory actions for casual conversation.

Return strict JSON only with this schema:
{{
  "reason": "short explanation",
  "actions": [
    {{
      "action": "add|update|delete|move",
      "section": "facts|decisions|notes or null",
      "target": "exact existing memory item text or null",
      "text": "new memory text or null"
    }}
  ]
}}

Rules:
- If the user did not request a memory change, return actions = [].
- For update/delete/move, target must match an existing memory item exactly.
- Keep each text short and concrete.
- At most {max_actions} actions.
"""


class MemoryAction(BaseModel):
    action: Literal["add", "update", "delete", "move"]
    section: Literal["facts", "decisions", "notes"] | None = None
    target: str | None = None
    text: str | None = None


class MemoryActionPlan(BaseModel):
    reason: str | None = None
    actions: list[MemoryAction] = Field(default_factory=list)


@dataclass(frozen=True)
class MemoryActionApplyResult:
    text: str
    applied: list[str]
    errors: list[str]


@dataclass(frozen=True)
class MemoryEntryLocation:
    section: str
    text: str
    line_index: int


class MemoryActionManager:
    def __init__(self, settings: AppSettings, model_client: ModelClient):
        self.settings = settings
        self.model_client = model_client

    async def propose_actions(
        self,
        *,
        workspace: CollectionWorkspace,
        collection: OutlineCollection | None,
        document: OutlineDocument,
        user_comment: str,
    ) -> MemoryActionPlan:
        system_prompt = MEMORY_ACTION_SYSTEM_PROMPT.format(
            max_actions=self.settings.memory_update_max_entries,
        )
        user_prompt = self._build_user_prompt(
            workspace=workspace,
            collection=collection,
            document=document,
            user_comment=user_comment,
        )
        raw = await self.model_client.generate_reply(system_prompt, user_prompt)
        try:
            payload = extract_json_object(raw)
        except JsonExtractionError:
            return MemoryActionPlan(reason="model-output-not-json")

        plan = MemoryActionPlan.model_validate(payload)
        return self._sanitize(plan)

    def apply_actions(
        self,
        workspace: CollectionWorkspace,
        plan: MemoryActionPlan,
    ) -> MemoryActionApplyResult:
        text = workspace.read_memory_text()
        applied: list[str] = []
        errors: list[str] = []
        for action in plan.actions:
            text, applied_entry, error = _apply_action(
                text,
                action,
                max_chars=self.settings.memory_update_entry_max_chars,
            )
            if applied_entry:
                applied.append(applied_entry)
            if error:
                errors.append(error)

        if applied:
            workspace.memory_path.write_text(_ensure_trailing_newline(text), encoding="utf-8")
            write_memory_index(workspace, text)

        return MemoryActionApplyResult(text=text, applied=applied, errors=errors)

    def preview(self, plan: MemoryActionPlan) -> str | None:
        if not plan.actions:
            return None
        return " ; ".join(_describe_action(action) for action in plan.actions)

    def format_reply_context(
        self,
        plan: MemoryActionPlan,
        *,
        status: str,
        applied: list[str],
        errors: list[str],
    ) -> str | None:
        if not plan.actions and not errors:
            return None
        lines = [f"- status: {status}"]
        if plan.reason:
            lines.append(f"- reason: {plan.reason}")
        if plan.actions:
            lines.append(f"- planned: {self.preview(plan) or 'none'}")
        if applied:
            lines.append(f"- applied: {' ; '.join(applied)}")
        if errors:
            lines.append(f"- errors: {' ; '.join(errors)}")
        return "\n".join(lines)

    def _build_user_prompt(
        self,
        *,
        workspace: CollectionWorkspace,
        collection: OutlineCollection | None,
        document: OutlineDocument,
        user_comment: str,
    ) -> str:
        collection_name = collection.name if collection and collection.name else workspace.collection_name
        memory_entries = _format_memory_entries(workspace.read_memory_text())
        return (
            f"Collection: {collection_name}\n"
            f"Collection ID: {workspace.collection_id}\n"
            f"Document title: {document.title or '(unknown)'}\n\n"
            "Current MEMORY.md entries (use exact text for targets):\n"
            f"{memory_entries}\n\n"
            "Latest user comment:\n"
            f"{_truncate(user_comment, self.settings.max_prompt_chars)}"
        )

    def _sanitize(self, plan: MemoryActionPlan) -> MemoryActionPlan:
        cleaned: list[MemoryAction] = []
        for action in plan.actions:
            normalized = _sanitize_action(action, max_chars=self.settings.memory_update_entry_max_chars)
            if normalized is None:
                continue
            cleaned.append(normalized)
            if len(cleaned) >= self.settings.memory_update_max_entries:
                break
        return MemoryActionPlan(reason=plan.reason, actions=cleaned)


def _sanitize_action(action: MemoryAction, *, max_chars: int) -> MemoryAction | None:
    if action.action == "add":
        if not action.section or not action.text:
            return None
        cleaned = _clean_text(action.text, max_chars=max_chars)
        if not cleaned:
            return None
        return MemoryAction(action="add", section=action.section, text=cleaned)

    if action.action == "update":
        if not action.target or not action.text:
            return None
        cleaned = _clean_text(action.text, max_chars=max_chars)
        if not cleaned:
            return None
        return MemoryAction(
            action="update",
            section=action.section,
            target=action.target.strip(),
            text=cleaned,
        )

    if action.action == "delete":
        if not action.target:
            return None
        return MemoryAction(action="delete", target=action.target.strip())

    if action.action == "move":
        if not action.target or not action.section:
            return None
        return MemoryAction(
            action="move",
            section=action.section,
            target=action.target.strip(),
            text=_clean_text(action.text, max_chars=max_chars) if action.text else None,
        )

    return None


def _apply_action(
    text: str,
    action: MemoryAction,
    *,
    max_chars: int,
) -> tuple[str, str | None, str | None]:
    if action.action == "add":
        return _apply_add(text, action, max_chars=max_chars)
    if action.action == "update":
        return _apply_update(text, action, max_chars=max_chars)
    if action.action == "delete":
        return _apply_delete(text, action)
    if action.action == "move":
        return _apply_move(text, action, max_chars=max_chars)
    return text, None, "unsupported action"


def _apply_add(text: str, action: MemoryAction, *, max_chars: int) -> tuple[str, str | None, str | None]:
    if not action.section or not action.text:
        return text, None, "add action missing section or text"
    cleaned = _clean_text(action.text, max_chars=max_chars)
    if not cleaned:
        return text, None, "add action missing usable text"
    heading = MEMORY_SECTION_HEADINGS[action.section]
    updated, appended = _append_entries_to_section(text, heading, [cleaned])
    if not appended:
        return updated, None, f"duplicate memory entry ignored: {cleaned}"
    return updated, f"add[{action.section}]: {cleaned}", None


def _apply_update(text: str, action: MemoryAction, *, max_chars: int) -> tuple[str, str | None, str | None]:
    if not action.target or not action.text:
        return text, None, "update action missing target or text"
    cleaned = _clean_text(action.text, max_chars=max_chars)
    if not cleaned:
        return text, None, "update action missing usable text"
    entries = _parse_memory_entries(text)
    match = _find_matching_entry(entries, action.target)
    if isinstance(match, str):
        return text, None, match
    new_section = action.section or match.section
    lines = text.splitlines()
    if new_section == match.section:
        lines[match.line_index] = f"- {cleaned}"
        updated = "\n".join(lines)
        return updated, f"update[{match.section}]: {match.text} -> {cleaned}", None

    lines.pop(match.line_index)
    updated = "\n".join(lines)
    heading = MEMORY_SECTION_HEADINGS[new_section]
    updated, appended = _append_entries_to_section(updated, heading, [cleaned])
    if not appended:
        return updated, None, f"duplicate memory entry ignored: {cleaned}"
    return updated, f"move[{match.section}->{new_section}]: {match.text} -> {cleaned}", None


def _apply_delete(text: str, action: MemoryAction) -> tuple[str, str | None, str | None]:
    if not action.target:
        return text, None, "delete action missing target"
    entries = _parse_memory_entries(text)
    match = _find_matching_entry(entries, action.target)
    if isinstance(match, str):
        return text, None, match
    lines = text.splitlines()
    lines.pop(match.line_index)
    updated = "\n".join(lines)
    return updated, f"delete[{match.section}]: {match.text}", None


def _apply_move(text: str, action: MemoryAction, *, max_chars: int) -> tuple[str, str | None, str | None]:
    if not action.target or not action.section:
        return text, None, "move action missing target or section"
    entries = _parse_memory_entries(text)
    match = _find_matching_entry(entries, action.target)
    if isinstance(match, str):
        return text, None, match
    new_text = _clean_text(action.text or match.text, max_chars=max_chars)
    if not new_text:
        return text, None, "move action missing usable text"
    lines = text.splitlines()
    lines.pop(match.line_index)
    updated = "\n".join(lines)
    heading = MEMORY_SECTION_HEADINGS[action.section]
    updated, appended = _append_entries_to_section(updated, heading, [new_text])
    if not appended:
        return updated, None, f"duplicate memory entry ignored: {new_text}"
    return updated, f"move[{match.section}->{action.section}]: {match.text} -> {new_text}", None


def _describe_action(action: MemoryAction) -> str:
    if action.action == "add" and action.section and action.text:
        return f"add[{action.section}]: {action.text}"
    if action.action == "update" and action.target and action.text:
        label = f"{action.section}" if action.section else "keep-section"
        return f"update[{label}]: {action.target} -> {action.text}"
    if action.action == "delete" and action.target:
        return f"delete: {action.target}"
    if action.action == "move" and action.target and action.section:
        return f"move[{action.section}]: {action.target}"
    return "unsupported action"


def _format_memory_entries(text: str) -> str:
    entries = _parse_memory_entries(text)
    if not entries:
        return "(no stored memory items)"
    lines: list[str] = []
    for entry in entries:
        lines.append(f"[{entry.section}] {entry.text}")
    return "\n".join(lines)


def _parse_memory_entries(text: str) -> list[MemoryEntryLocation]:
    reverse_headings = {heading: section for section, heading in MEMORY_SECTION_HEADINGS.items()}
    entries: list[MemoryEntryLocation] = []
    current_section: str | None = None
    for index, line in enumerate(text.splitlines()):
        if line.startswith("## "):
            heading = line[3:].strip()
            current_section = reverse_headings.get(heading)
            continue
        if current_section and line.startswith("- "):
            entry_text = line[2:].strip()
            if entry_text:
                entries.append(
                    MemoryEntryLocation(
                        section=current_section,
                        text=entry_text,
                        line_index=index,
                    )
                )
    return entries


def _find_matching_entry(
    entries: list[MemoryEntryLocation],
    target: str,
) -> MemoryEntryLocation | str:
    normalized_target = _normalize_match(target)
    if not normalized_target:
        return "target text is empty"
    matches = [entry for entry in entries if _normalize_match(entry.text) == normalized_target]
    if not matches:
        return f"target not found: {target}"
    if len(matches) > 1:
        return f"target ambiguous: {target}"
    return matches[0]


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

    existing = {_normalize_match(line[2:]) for line in lines[heading_index + 1 : section_end] if line.startswith("- ")}
    appended: list[str] = []
    for entry in entries:
        normalized = _normalize_match(entry)
        if not normalized or normalized in existing:
            continue
        existing.add(normalized)
        appended.append(entry)

    if not appended:
        return "\n".join(lines), []

    insertion = [f"- {entry}" for entry in appended]
    lines[section_end:section_end] = insertion
    return "\n".join(lines), appended


def _normalize_match(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _clean_text(text: str, *, max_chars: int) -> str:
    compact = re.sub(r"\s+", " ", text or "").strip()
    if len(compact) < 4:
        return ""
    return _truncate(compact, max_chars)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _ensure_trailing_newline(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"
