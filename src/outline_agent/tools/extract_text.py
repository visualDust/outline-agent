from __future__ import annotations

import csv
import os
import re
import zlib
from pathlib import Path
from typing import Any

from .base import ToolContext, ToolError, ToolResult, ToolSpec


class ExtractTextFromTxtTool:
    @property
    def spec(self) -> ToolSpec:
        return _build_spec(
            name="extract_text_from_txt",
            description="Read plain text from a UTF-8 or best-effort text file.",
        )

    async def run(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        path, relative_path = _resolve_input_path(args, context)
        text = path.read_text(encoding="utf-8", errors="replace")
        return _text_result(self.spec.name, relative_path, text, format_name="txt")


class ExtractTextFromMdTool:
    @property
    def spec(self) -> ToolSpec:
        return _build_spec(
            name="extract_text_from_md",
            description="Read Markdown as normalized plain text while preserving headings and lists.",
        )

    async def run(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        path, relative_path = _resolve_input_path(args, context)
        text = path.read_text(encoding="utf-8", errors="replace")
        normalized = re.sub(r"\n{3,}", "\n\n", text).strip()
        return _text_result(self.spec.name, relative_path, normalized, format_name="md")


class ExtractTextFromCsvTool:
    @property
    def spec(self) -> ToolSpec:
        return _build_spec(
            name="extract_text_from_csv",
            description="Read CSV content and normalize rows into tab-separated text.",
        )

    async def run(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        path, relative_path = _resolve_input_path(args, context)
        with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
            reader = csv.reader(handle)
            rows = ["\t".join(cell.strip() for cell in row) for row in reader]
        text = "\n".join(row for row in rows if row.strip())
        return _text_result(self.spec.name, relative_path, text, format_name="csv")


class ExtractTextFromPdfTool:
    @property
    def spec(self) -> ToolSpec:
        return _build_spec(
            name="extract_text_from_pdf",
            description="Best-effort extraction of visible text from a simple PDF file.",
        )

    async def run(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        path, relative_path = _resolve_input_path(args, context)
        data = path.read_bytes()
        text = _extract_pdf_text(data)
        if not text.strip():
            raise ToolError(f"no extractable text found in PDF: {relative_path}")
        return _text_result(self.spec.name, relative_path, text, format_name="pdf")


def build_default_extract_text_tools() -> list[object]:
    return [
        ExtractTextFromTxtTool(),
        ExtractTextFromMdTool(),
        ExtractTextFromCsvTool(),
        ExtractTextFromPdfTool(),
    ]


def _build_spec(*, name: str, description: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        when_to_use="Use after downloading or locating a local file whose text needs to be read by the planner.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "format": {"type": "string"},
                "text": {"type": "string"},
                "char_count": {"type": "integer"},
                "truncated": {"type": "boolean"},
            },
        },
        side_effect_level="read",
    )


def _resolve_input_path(args: dict[str, Any], context: ToolContext) -> tuple[Path, str]:
    if context.work_dir is None:
        raise ToolError("extraction tool requires a work_dir in context")
    raw = args.get("path")
    if not isinstance(raw, str) or not raw.strip():
        raise ToolError("path is required")
    relative_path = raw.strip().replace("\\", "/")
    path = Path(os.path.normpath(str(context.work_dir / relative_path)))
    base = Path(os.path.normpath(str(context.work_dir)))
    if path != base and base not in path.parents:
        raise ToolError(f"path escapes work dir: {relative_path}")
    if not path.exists() or not path.is_file():
        raise ToolError(f"file does not exist: {relative_path}")
    return path, relative_path


def _text_result(tool_name: str, path: str, text: str, *, format_name: str) -> ToolResult:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    truncated = False
    if len(normalized) > 12000:
        normalized = normalized[:12000].rstrip() + "…"
        truncated = True
    return ToolResult(
        ok=True,
        tool=tool_name,
        summary=f"Extracted {len(normalized)} chars from {path}.",
        data={
            "path": path,
            "format": format_name,
            "text": normalized,
            "char_count": len(normalized),
            "truncated": truncated,
        },
        preview=normalized[:200] if normalized else None,
    )


def _extract_pdf_text(data: bytes) -> str:
    fragments: list[str] = []
    for stream in _iter_pdf_streams(data):
        fragments.extend(_extract_pdf_text_fragments(stream))
    if not fragments:
        fragments.extend(_extract_pdf_text_fragments(data))
    text = "\n".join(fragment.strip() for fragment in fragments if fragment and fragment.strip())
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _iter_pdf_streams(data: bytes) -> list[bytes]:
    streams: list[bytes] = []
    for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.DOTALL):
        payload = match.group(1)
        streams.append(payload)
        try:
            streams.append(zlib.decompress(payload))
        except zlib.error:
            continue
    return streams


def _extract_pdf_text_fragments(data: bytes) -> list[str]:
    fragments: list[str] = []
    for match in re.finditer(rb"\((?:\\.|[^\\)])*\)\s*Tj", data):
        fragments.append(_decode_pdf_literal_string(match.group(0).rsplit(b")", 1)[0][1:]))
    for match in re.finditer(rb"\[(.*?)\]\s*TJ", data, re.DOTALL):
        inner = match.group(1)
        for item in re.finditer(rb"\((?:\\.|[^\\)])*\)", inner):
            fragments.append(_decode_pdf_literal_string(item.group(0)[1:-1]))
    for match in re.finditer(rb"\((?:\\.|[^\\)])*\)\s*'", data):
        fragments.append(_decode_pdf_literal_string(match.group(0).rsplit(b")", 1)[0][1:]))
    return [fragment for fragment in fragments if fragment.strip()]


def _decode_pdf_literal_string(data: bytes) -> str:
    output = bytearray()
    index = 0
    while index < len(data):
        current = data[index]
        if current != 0x5C:  # backslash
            output.append(current)
            index += 1
            continue
        index += 1
        if index >= len(data):
            break
        escaped = data[index]
        if escaped in b"nrtbf":
            mapping = {
                ord("n"): b"\n",
                ord("r"): b"\r",
                ord("t"): b"\t",
                ord("b"): b"\b",
                ord("f"): b"\f",
            }
            output.extend(mapping[escaped])
            index += 1
            continue
        if escaped in b"()\\":
            output.append(escaped)
            index += 1
            continue
        if 48 <= escaped <= 55:
            octal_digits = bytes([escaped])
            index += 1
            for _ in range(2):
                if index < len(data) and 48 <= data[index] <= 55:
                    octal_digits += bytes([data[index]])
                    index += 1
                else:
                    break
            output.append(int(octal_digits, 8))
            continue
        output.append(escaped)
        index += 1
    return output.decode("utf-8", errors="replace")
