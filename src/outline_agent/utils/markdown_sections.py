from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

HEADING_RE = re.compile(r"(?m)^(#{1,6})\s+(.*?)\s*$")


class MarkdownOperationError(ValueError):
    """Raised when a structured Markdown edit cannot be applied safely."""


@dataclass(frozen=True)
class MarkdownSection:
    section_id: str
    heading_path: tuple[str, ...]
    level: int
    start: int
    end: int
    markdown: str

    @property
    def label(self) -> str:
        return " > ".join(self.heading_path) if self.heading_path else "(document body)"

    @property
    def preview(self) -> str:
        compact = re.sub(r"\s+", " ", self.markdown).strip()
        return compact if len(compact) <= 160 else compact[:159] + "…"

    @property
    def char_count(self) -> int:
        return len(self.markdown)


@dataclass(frozen=True)
class MarkdownEditOperation:
    op: str
    target_section_id: str | None
    new_markdown: str | None


def parse_markdown_sections(text: str) -> list[MarkdownSection]:
    normalized = normalize_markdown_text(text) or ""
    if not normalized:
        return []

    matches = list(HEADING_RE.finditer(normalized))
    sections: list[MarkdownSection] = []
    next_index = 1

    if not matches:
        return [
            MarkdownSection(
                section_id="S1",
                heading_path=(),
                level=0,
                start=0,
                end=len(normalized),
                markdown=normalized,
            )
        ]

    first_start = matches[0].start()
    if normalized[:first_start].strip():
        sections.append(
            MarkdownSection(
                section_id=f"S{next_index}",
                heading_path=(),
                level=0,
                start=0,
                end=first_start,
                markdown=normalized[:first_start].strip("\n"),
            )
        )
        next_index += 1

    heading_paths: list[tuple[str, ...]] = []
    stack: list[tuple[int, str]] = []
    for match in matches:
        level = len(match.group(1))
        title = match.group(2).strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        heading_paths.append(tuple(item[1] for item in stack))

    for index, match in enumerate(matches):
        level = len(match.group(1))
        end = len(normalized)
        for later in matches[index + 1 :]:
            later_level = len(later.group(1))
            if later_level <= level:
                end = later.start()
                break

        sections.append(
            MarkdownSection(
                section_id=f"S{next_index}",
                heading_path=heading_paths[index],
                level=level,
                start=match.start(),
                end=end,
                markdown=normalized[match.start() : end].strip("\n"),
            )
        )
        next_index += 1

    return sections


def apply_markdown_operations(text: str, operations: Sequence[MarkdownEditOperation]) -> str:
    current = normalize_markdown_text(text) or ""
    for operation in operations:
        current = _apply_single_operation(current, operation)
    return normalize_markdown_text(current) or ""


def find_section(sections: Sequence[MarkdownSection], section_id: str) -> MarkdownSection:
    matches = [section for section in sections if section.section_id == section_id]
    if not matches:
        raise MarkdownOperationError(f"Unknown target section id: {section_id}")
    if len(matches) > 1:
        raise MarkdownOperationError(f"Ambiguous target section id: {section_id}")
    return matches[0]


def format_document_outline(sections: Iterable[MarkdownSection], max_sections: int) -> str:
    lines: list[str] = []
    for index, section in enumerate(sections):
        if index >= max_sections:
            lines.append("- …")
            break
        path = section.label
        lines.append(
            "- "
            f"{section.section_id} | level={section.level} | chars={section.char_count} "
            f"| path={path} | preview={section.preview}"
        )
    return "\n".join(lines) or "(no document sections found)"


def normalize_markdown_text(text: str | None) -> str | None:
    if text is None:
        return None
    normalized = text.replace("\r\n", "\n").strip("\n")
    return normalized or None


def _apply_single_operation(text: str, operation: MarkdownEditOperation) -> str:
    if operation.op == "replace_document":
        replacement = normalize_markdown_text(operation.new_markdown)
        if replacement is None:
            raise MarkdownOperationError("replace_document requires non-empty new_markdown")
        return replacement

    if operation.op == "append_document":
        addition = normalize_markdown_text(operation.new_markdown)
        if addition is None:
            raise MarkdownOperationError("append_document requires non-empty new_markdown")
        return _join_parts([text, addition])

    if not operation.target_section_id:
        raise MarkdownOperationError(f"{operation.op} requires target_section_id")

    sections = parse_markdown_sections(text)
    section = find_section(sections, operation.target_section_id)

    if operation.op == "replace_section":
        replacement = normalize_markdown_text(operation.new_markdown)
        if replacement is None:
            raise MarkdownOperationError("replace_section requires non-empty new_markdown")
        return _replace_span(text, section.start, section.end, replacement)

    if operation.op == "insert_after_section":
        addition = normalize_markdown_text(operation.new_markdown)
        if addition is None:
            raise MarkdownOperationError("insert_after_section requires non-empty new_markdown")
        return _insert_at(text, section.end, addition, after=True)

    if operation.op == "insert_before_section":
        addition = normalize_markdown_text(operation.new_markdown)
        if addition is None:
            raise MarkdownOperationError("insert_before_section requires non-empty new_markdown")
        return _insert_at(text, section.start, addition, after=False)

    raise MarkdownOperationError(f"Unsupported markdown edit operation: {operation.op}")


def _replace_span(text: str, start: int, end: int, replacement: str) -> str:
    before = text[:start]
    after = text[end:]
    return _join_parts([before, replacement, after])


def _insert_at(text: str, index: int, addition: str, *, after: bool) -> str:
    before = text[:index] if after else text[:index]
    after_text = text[index:]
    return _join_parts([before, addition, after_text])


def _join_parts(parts: Sequence[str]) -> str:
    cleaned = [part.strip("\n") for part in parts if part and part.strip("\n")]
    return "\n\n".join(cleaned)
