from __future__ import annotations

import re
import textwrap
from typing import Any

from .outline_exceptions import OutlineClientError

try:
    from markdown_it import MarkdownIt
except ImportError:  # pragma: no cover - optional dependency in some environments
    MarkdownIt = None

OUTLINE_COMMENT_MAX_CHARS = 1000
MARKDOWN_PARSER = MarkdownIt("commonmark") if MarkdownIt is not None else None


def build_comment_data(text: str) -> dict[str, Any]:
    normalized = text.strip()
    lines = [line.strip() for line in normalized.splitlines() if line.strip()] if normalized else []
    content: list[dict[str, Any]] = []

    for line in lines:
        content.append({"type": "paragraph", "content": [{"type": "text", "text": line}]})

    if not content:
        content.append({"type": "paragraph", "content": [{"type": "text", "text": normalized}]})

    return {"type": "doc", "content": content}


def build_markdown_comment_data(text: str) -> dict[str, Any]:
    normalized = normalize_comment_markdown(text)
    if not normalized or MARKDOWN_PARSER is None:
        return build_comment_data(normalized)

    try:
        content, _ = _parse_markdown_block_tokens(MARKDOWN_PARSER.parse(normalized), 0)
    except Exception:  # noqa: BLE001 - fallback to plain text payload if markdown parsing fails unexpectedly
        return build_comment_data(normalized)

    if not content:
        return build_comment_data(normalized)
    return {"type": "doc", "content": content}


def split_comment_text(text: str, max_chars: int = OUTLINE_COMMENT_MAX_CHARS) -> list[str]:
    normalized = text.strip()
    if max_chars < 1:
        raise ValueError("max_chars must be at least 1")
    if not normalized:
        return [normalized]
    if len(normalized) <= max_chars:
        return [normalized]

    markdown_chunks = _split_markdown_blocks(normalized, max_chars=max_chars)
    if markdown_chunks:
        return markdown_chunks

    return _split_comment_text_lines(normalized, max_chars=max_chars)


def prepare_comment_chunks(text: str, max_chars: int = OUTLINE_COMMENT_MAX_CHARS) -> list[str]:
    normalized = normalize_comment_markdown(text)
    chunks = split_comment_text(normalized, max_chars=max_chars)
    if len(chunks) <= 1:
        return chunks

    total_chunks = len(chunks)
    while True:
        marker_max_length = len(_chunk_marker(total_chunks, total_chunks))
        available_chars = max_chars - marker_max_length
        if available_chars < 1:
            raise ValueError("max_chars is too small to fit numbered comment chunks")

        adjusted_chunks = split_comment_text(normalized, max_chars=available_chars)
        if len(adjusted_chunks) == total_chunks:
            return [
                f"{_chunk_marker(index, total_chunks)}{chunk}" for index, chunk in enumerate(adjusted_chunks, start=1)
            ]
        total_chunks = len(adjusted_chunks)


def normalize_comment_markdown(text: str) -> str:
    normalized = text.strip()
    if not normalized:
        return normalized

    lines = normalized.splitlines()
    rewritten: list[str] = []
    index = 0

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if _is_fence_line(stripped):
            code_lines: list[str] = []
            fence_char = stripped[0]
            index += 1
            while index < len(lines) and not _is_matching_fence(lines[index].strip(), fence_char):
                code_lines.append(lines[index].rstrip())
                index += 1
            if index < len(lines):
                index += 1
            rewritten.extend(_rewrite_fenced_code_block(code_lines))
            continue

        if _looks_like_markdown_table(lines, index):
            table_lines, index = _consume_table_lines(lines, index)
            rewritten.extend(_rewrite_markdown_table(table_lines))
            continue

        heading_match = re.match(r"^\s{0,3}#{1,6}\s+(.*\S)\s*$", line)
        if heading_match:
            heading_text = heading_match.group(1).strip()
            rewritten.append(f"**{heading_text}**")
            index += 1
            continue

        rewritten.append(line.rstrip())
        index += 1

    return _normalize_blank_lines(rewritten)


def is_comment_too_long_error(error: OutlineClientError) -> bool:
    message = str(error).lower()
    return "comment must be less than 1000 characters" in message or "less than 1000 characters" in message


def should_retry_comment_create_as_data(error: OutlineClientError) -> bool:
    message = str(error).lower()
    return (
        "outline api error 500" in message
        or "outline api error 502" in message
        or "internal error" in message
        or "invalid data" in message
    )


def should_retry_comment_create_as_plain_data(error: OutlineClientError) -> bool:
    message = str(error).lower()
    return "invalid data" in message or "outline api error 500" in message or "internal error" in message


def should_retry_comment_update_as_text(error: OutlineClientError) -> bool:
    message = str(error).lower()
    return "invalid data" in message or "outline api error 500" in message or "internal error" in message


def _split_comment_text_lines(text: str, *, max_chars: int) -> list[str]:
    normalized = text.strip()
    if not normalized:
        return [normalized]

    lines = normalized.splitlines()
    if not lines:
        return [normalized]

    chunks: list[str] = []
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_lines
        if current_lines:
            chunks.append("\n".join(current_lines))
            current_lines = []

    for line in lines:
        for segment in _split_long_comment_line(line, max_chars=max_chars):
            if current_lines and _joined_comment_length(current_lines + [segment]) > max_chars:
                flush()
            current_lines.append(segment)
            if _joined_comment_length(current_lines) >= max_chars:
                flush()

    flush()
    return chunks or [normalized]


def _is_fence_line(text: str) -> bool:
    return text.startswith("```") or text.startswith("~~~")


def _is_matching_fence(text: str, fence_char: str) -> bool:
    if fence_char not in {"`", "~"}:
        return False
    return text.startswith(fence_char * 3)


def _rewrite_fenced_code_block(lines: list[str]) -> list[str]:
    rewritten = ["Code:"]
    if not lines:
        rewritten.append("- (empty)")
        return rewritten
    for line in lines:
        stripped = line.rstrip()
        rewritten.append(f"- {stripped}" if stripped else "-")
    return rewritten


def _looks_like_markdown_table(lines: list[str], index: int) -> bool:
    if index + 1 >= len(lines):
        return False
    header = lines[index].strip()
    separator = lines[index + 1].strip()
    if "|" not in header or "|" not in separator:
        return False
    header_cells = _split_table_cells(header)
    separator_cells = _split_table_cells(separator)
    if len(header_cells) < 2 or len(header_cells) != len(separator_cells):
        return False
    return all(re.fullmatch(r":?-{3,}:?", cell) for cell in separator_cells)


def _consume_table_lines(lines: list[str], index: int) -> tuple[list[str], int]:
    consumed = [lines[index], lines[index + 1]]
    index += 2
    while index < len(lines):
        current = lines[index].strip()
        if "|" not in current or not current:
            break
        consumed.append(lines[index])
        index += 1
    return consumed, index


def _rewrite_markdown_table(lines: list[str]) -> list[str]:
    header_cells = _split_table_cells(lines[0])
    row_lines = lines[2:]
    if not row_lines:
        return ["; ".join(header_cells)]

    rewritten = ["Table:"]
    for row_line in row_lines:
        row_cells = _split_table_cells(row_line)
        if not row_cells:
            continue
        pairs = []
        for offset, value in enumerate(row_cells):
            label = header_cells[offset] if offset < len(header_cells) else f"Column {offset + 1}"
            pairs.append(f"{label}: {value}")
        rewritten.append(f"- {'; '.join(pairs)}")
    return rewritten or ["; ".join(header_cells)]


def _split_table_cells(line: str) -> list[str]:
    stripped = line.strip().strip("|")
    return [cell.strip() for cell in stripped.split("|")] if stripped else []


def _normalize_blank_lines(lines: list[str]) -> str:
    normalized: list[str] = []
    previous_blank = False
    for line in lines:
        current = line.rstrip()
        is_blank = not current.strip()
        if is_blank and previous_blank:
            continue
        normalized.append("" if is_blank else current)
        previous_blank = is_blank
    return "\n".join(normalized).strip()


def _split_markdown_blocks(text: str, *, max_chars: int) -> list[str]:
    blocks = _extract_markdown_blocks(text)
    if not blocks:
        return []

    expanded_blocks: list[str] = []
    for block in blocks:
        if len(block) <= max_chars:
            expanded_blocks.append(block)
            continue

        nested = _split_large_markdown_block(block, max_chars=max_chars)
        if not nested:
            return []
        expanded_blocks.extend(nested)

    merged = _merge_markdown_blocks(expanded_blocks, max_chars=max_chars)
    return merged if len(merged) > 1 else []


def _extract_markdown_blocks(text: str) -> list[str]:
    if MARKDOWN_PARSER is None:
        return []

    lines = text.splitlines()
    tokens = MARKDOWN_PARSER.parse(text)
    blocks: list[str] = []
    seen_ranges: set[tuple[int, int]] = set()
    for token in tokens:
        if token.level != 0 or token.map is None or token.type.endswith("_close"):
            continue
        token_range = (token.map[0], token.map[1])
        if token_range in seen_ranges:
            continue
        seen_ranges.add(token_range)
        block = "\n".join(lines[token_range[0] : token_range[1]]).strip()
        if block:
            blocks.append(block)
    return blocks


def _split_large_markdown_block(block: str, *, max_chars: int) -> list[str]:
    if MARKDOWN_PARSER is None:
        return []

    lines = block.splitlines()
    tokens = MARKDOWN_PARSER.parse(block)
    top_level_tokens = [
        token for token in tokens if token.level == 0 and token.map is not None and not token.type.endswith("_close")
    ]
    if not top_level_tokens:
        return _split_comment_text_lines(block, max_chars=max_chars)

    first_type = top_level_tokens[0].type
    if first_type in {"ordered_list_open", "bullet_list_open"}:
        items = _extract_list_item_blocks(lines, tokens)
        if items:
            expanded_items: list[str] = []
            for item in items:
                if len(item) <= max_chars:
                    expanded_items.append(item)
                else:
                    expanded_items.extend(_split_comment_text_lines(item, max_chars=max_chars))
            return expanded_items

    if first_type == "fence":
        fenced_chunks = _split_fenced_code_block(block, max_chars=max_chars)
        if fenced_chunks:
            return fenced_chunks

    return _split_comment_text_lines(block, max_chars=max_chars)


def _extract_list_item_blocks(lines: list[str], tokens: list[Any]) -> list[str]:
    items: list[str] = []
    for token in tokens:
        if token.type != "list_item_open" or token.level != 1 or token.map is None:
            continue
        item = "\n".join(lines[token.map[0] : token.map[1]]).strip()
        if item:
            items.append(item)
    return items


def _split_fenced_code_block(block: str, *, max_chars: int) -> list[str]:
    lines = block.splitlines()
    if len(lines) < 3:
        return []

    opening = lines[0]
    closing = lines[-1]
    is_backtick_fence = opening.startswith("```") and closing.startswith("```")
    is_tilde_fence = opening.startswith("~~~") and closing.startswith("~~~")
    if not (is_backtick_fence or is_tilde_fence):
        return []

    wrapper_overhead = len(opening) + len(closing) + 2
    available_chars = max_chars - wrapper_overhead
    if available_chars < 1:
        return []

    inner_chunks = _split_comment_text_lines("\n".join(lines[1:-1]), max_chars=available_chars)
    return [f"{opening}\n{chunk}\n{closing}" for chunk in inner_chunks if chunk]


def _merge_markdown_blocks(blocks: list[str], *, max_chars: int) -> list[str]:
    chunks: list[str] = []
    current_blocks: list[str] = []

    def flush() -> None:
        nonlocal current_blocks
        if current_blocks:
            chunks.append("\n\n".join(current_blocks).strip())
            current_blocks = []

    for block in blocks:
        candidate = current_blocks + [block]
        if current_blocks and _joined_markdown_blocks_length(candidate) > max_chars:
            flush()
        current_blocks.append(block)
        if _joined_markdown_blocks_length(current_blocks) >= max_chars:
            flush()

    flush()
    return chunks


def _split_long_comment_line(line: str, *, max_chars: int) -> list[str]:
    if len(line) <= max_chars:
        return [line]

    wrapped = textwrap.wrap(
        line,
        width=max_chars,
        break_long_words=True,
        break_on_hyphens=False,
        replace_whitespace=False,
        drop_whitespace=True,
    )
    if not wrapped:
        return [line[:max_chars], *split_comment_text(line[max_chars:], max_chars=max_chars)]

    segments: list[str] = []
    for item in wrapped:
        stripped = item.strip()
        if not stripped:
            continue
        if len(stripped) <= max_chars:
            segments.append(stripped)
            continue
        for index in range(0, len(stripped), max_chars):
            segments.append(stripped[index : index + max_chars])
    return segments or [line[:max_chars]]


def _joined_comment_length(lines: list[str]) -> int:
    if not lines:
        return 0
    return sum(len(line) for line in lines) + max(0, len(lines) - 1)


def _joined_markdown_blocks_length(blocks: list[str]) -> int:
    if not blocks:
        return 0
    return sum(len(block) for block in blocks) + max(0, 2 * (len(blocks) - 1))


def _chunk_marker(index: int, total: int) -> str:
    return f"[{index}/{total}]\n\n"


def _parse_markdown_block_tokens(
    tokens: list[Any],
    start_index: int,
    *,
    stop_token_types: set[str] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    content: list[dict[str, Any]] = []
    index = start_index

    while index < len(tokens):
        token = tokens[index]
        token_type = getattr(token, "type", "")

        if stop_token_types and token_type in stop_token_types:
            return content, index + 1

        if token_type == "paragraph_open":
            node, index = _consume_paragraph_token(tokens, index)
            content.append(node)
            continue

        if token_type == "heading_open":
            node, index = _consume_heading_token(tokens, index)
            content.append(node)
            continue

        if token_type == "bullet_list_open":
            node, index = _consume_list_tokens(tokens, index, ordered=False)
            content.append(node)
            continue

        if token_type == "ordered_list_open":
            node, index = _consume_list_tokens(tokens, index, ordered=True)
            content.append(node)
            continue

        if token_type == "blockquote_open":
            nested_content, index = _parse_markdown_block_tokens(
                tokens,
                index + 1,
                stop_token_types={"blockquote_close"},
            )
            node: dict[str, Any] = {"type": "blockquote"}
            if nested_content:
                node["content"] = nested_content
            content.append(node)
            continue

        if token_type in {"fence", "code_block"}:
            content.append(_build_code_block_node(token))
            index += 1
            continue

        index += 1

    return content, index


def _consume_paragraph_token(tokens: list[Any], start_index: int) -> tuple[dict[str, Any], int]:
    index = start_index + 1
    inline_children: list[Any] = []

    while index < len(tokens):
        token = tokens[index]
        token_type = getattr(token, "type", "")
        if token_type == "inline":
            inline_children = list(getattr(token, "children", []) or [])
        if token_type == "paragraph_close":
            break
        index += 1

    paragraph_content = _parse_markdown_inline_tokens(inline_children)
    node: dict[str, Any] = {"type": "paragraph"}
    if paragraph_content:
        node["content"] = paragraph_content

    return node, min(index + 1, len(tokens))


def _consume_heading_token(tokens: list[Any], start_index: int) -> tuple[dict[str, Any], int]:
    opening = tokens[start_index]
    tag = getattr(opening, "tag", "")
    try:
        level = int(tag[1:]) if tag.startswith("h") else 1
    except ValueError:
        level = 1

    index = start_index + 1
    inline_children: list[Any] = []

    while index < len(tokens):
        token = tokens[index]
        token_type = getattr(token, "type", "")
        if token_type == "inline":
            inline_children = list(getattr(token, "children", []) or [])
        if token_type == "heading_close":
            break
        index += 1

    node: dict[str, Any] = {"type": "heading", "attrs": {"level": level}}
    heading_content = _parse_markdown_inline_tokens(inline_children)
    if heading_content:
        node["content"] = heading_content
    return node, min(index + 1, len(tokens))


def _consume_list_tokens(tokens: list[Any], start_index: int, *, ordered: bool) -> tuple[dict[str, Any], int]:
    opening = tokens[start_index]
    close_type = "ordered_list_close" if ordered else "bullet_list_close"
    index = start_index + 1
    items: list[dict[str, Any]] = []

    while index < len(tokens):
        token = tokens[index]
        token_type = getattr(token, "type", "")
        if token_type == close_type:
            index += 1
            break
        if token_type == "list_item_open":
            item, index = _consume_list_item_token(tokens, index)
            items.append(item)
            continue
        index += 1

    node: dict[str, Any] = {"type": "ordered_list" if ordered else "bullet_list", "content": items}
    if ordered:
        attrs: dict[str, Any] = {}
        start_value = _token_attr_get(opening, "start")
        if start_value is not None:
            try:
                attrs["order"] = int(start_value)
            except (TypeError, ValueError):
                pass
        if attrs:
            node["attrs"] = attrs

    return node, index


def _consume_list_item_token(tokens: list[Any], start_index: int) -> tuple[dict[str, Any], int]:
    item_content, index = _parse_markdown_block_tokens(tokens, start_index + 1, stop_token_types={"list_item_close"})
    node: dict[str, Any] = {"type": "list_item"}
    if item_content:
        node["content"] = item_content
    return node, index


def _build_code_block_node(token: Any) -> dict[str, Any]:
    text = getattr(token, "content", "") or ""
    language = (getattr(token, "info", "") or "").strip().split(maxsplit=1)[0] or "plaintext"
    node: dict[str, Any] = {
        "type": "code_block",
        "attrs": {"language": language},
    }
    if text:
        node["content"] = [{"type": "text", "text": text}]
    return node


def _parse_markdown_inline_tokens(tokens: list[Any]) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    marks: list[dict[str, Any]] = []

    for token in tokens:
        token_type = getattr(token, "type", "")

        if token_type == "text":
            _append_text_node(content, getattr(token, "content", "") or "", marks)
            continue

        if token_type == "code_inline":
            _append_text_node(content, getattr(token, "content", "") or "", [*marks, {"type": "code_inline"}])
            continue

        if token_type in {"softbreak", "hardbreak"}:
            content.append({"type": "hardBreak"})
            continue

        if token_type == "em_open":
            marks.append({"type": "em"})
            continue

        if token_type == "em_close":
            _pop_mark(marks, "em")
            continue

        if token_type == "strong_open":
            marks.append({"type": "strong"})
            continue

        if token_type == "strong_close":
            _pop_mark(marks, "strong")
            continue

        if token_type == "link_open":
            href = _token_attr_get(token, "href") or ""
            title = _token_attr_get(token, "title")
            link_mark: dict[str, Any] = {"type": "link", "attrs": {"href": href, "title": title}}
            marks.append(link_mark)
            continue

        if token_type == "link_close":
            _pop_mark(marks, "link")
            continue

        if token_type == "image":
            alt_text = (
                getattr(token, "content", "") or _token_attr_get(token, "alt") or _token_attr_get(token, "src") or ""
            )
            _append_text_node(content, alt_text, marks)
            continue

        if token_type == "html_inline":
            _append_text_node(content, getattr(token, "content", "") or "", marks)

    return content


def _append_text_node(content: list[dict[str, Any]], text: str, marks: list[dict[str, Any]]) -> None:
    if not text:
        return

    node: dict[str, Any] = {"type": "text", "text": text}
    normalized_marks = [_clone_mark(mark) for mark in marks if mark]
    if normalized_marks:
        node["marks"] = normalized_marks

    if content and _text_node_marks(content[-1]) == normalized_marks:
        content[-1]["text"] = f"{content[-1].get('text', '')}{text}"
        return

    content.append(node)


def _text_node_marks(node: dict[str, Any]) -> list[dict[str, Any]] | None:
    if node.get("type") != "text":
        return None
    marks = node.get("marks")
    return marks if isinstance(marks, list) else []


def _clone_mark(mark: dict[str, Any]) -> dict[str, Any]:
    cloned = dict(mark)
    attrs = cloned.get("attrs")
    if isinstance(attrs, dict):
        cloned["attrs"] = dict(attrs)
    return cloned


def _pop_mark(marks: list[dict[str, Any]], mark_type: str) -> None:
    for index in range(len(marks) - 1, -1, -1):
        if marks[index].get("type") == mark_type:
            marks.pop(index)
            return


def _token_attr_get(token: Any, name: str) -> Any:
    attrs = getattr(token, "attrs", None)
    if isinstance(attrs, dict):
        return attrs.get(name)

    attr_get = getattr(token, "attrGet", None)
    if callable(attr_get):
        return attr_get(name)

    return None
