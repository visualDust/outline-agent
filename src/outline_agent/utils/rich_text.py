from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MentionRef:
    model_id: str | None
    label: str | None
    actor_id: str | None


@dataclass(frozen=True)
class ImageRef:
    src: str
    alt: str | None = None


def extract_plain_text(value: Any) -> str:
    pieces: list[str] = []
    _walk(value, pieces)
    text = "".join(pieces)
    lines = [line.rstrip() for line in text.splitlines()]
    cleaned = "\n".join(line for line in _collapse_blank_lines(lines))
    return cleaned.strip()


def extract_mentions(value: Any) -> list[MentionRef]:
    mentions: list[MentionRef] = []
    _collect_mentions(value, mentions)
    return mentions


def extract_image_refs(value: Any) -> list[ImageRef]:
    images: list[ImageRef] = []
    _collect_images(value, images)
    return images


def extract_prompt_text(value: Any) -> str:
    text = extract_plain_text(value)
    image_count = len(extract_image_refs(value))
    if image_count <= 0:
        return text

    image_marker = "[attached image]" if image_count == 1 else f"[attached images: {image_count}]"
    if text:
        return f"{text}\n{image_marker}"
    return image_marker


def _walk(node: Any, pieces: list[str]) -> None:
    if node is None:
        return

    if isinstance(node, list):
        for item in node:
            _walk(item, pieces)
        return

    if not isinstance(node, dict):
        return

    node_type = node.get("type")
    if node_type == "text":
        text = node.get("text")
        if isinstance(text, str):
            pieces.append(text)
        return

    if node_type == "hardBreak":
        pieces.append("\n")
        return

    if node_type in {"mention", "userMention"}:
        pieces.append(_mention_text(node))
        return

    children = node.get("content")
    if isinstance(children, list):
        for child in children:
            _walk(child, pieces)

    if node_type in {"paragraph", "heading", "blockquote", "listItem", "codeBlock"}:
        pieces.append("\n")


def _collect_mentions(node: Any, mentions: list[MentionRef]) -> None:
    if node is None:
        return

    if isinstance(node, list):
        for item in node:
            _collect_mentions(item, mentions)
        return

    if not isinstance(node, dict):
        return

    node_type = node.get("type")
    if node_type in {"mention", "userMention"}:
        attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
        mentions.append(
            MentionRef(
                model_id=_as_optional_str(attrs.get("modelId") or attrs.get("id")),
                label=_as_optional_str(attrs.get("label") or attrs.get("name") or attrs.get("title")),
                actor_id=_as_optional_str(attrs.get("actorId")),
            )
        )

    children = node.get("content")
    if isinstance(children, list):
        for child in children:
            _collect_mentions(child, mentions)


def _collect_images(node: Any, images: list[ImageRef]) -> None:
    if node is None:
        return

    if isinstance(node, list):
        for item in node:
            _collect_images(item, images)
        return

    if not isinstance(node, dict):
        return

    node_type = node.get("type")
    if node_type == "image":
        attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
        src = _as_optional_str(attrs.get("src"))
        if src:
            images.append(ImageRef(src=src, alt=_as_optional_str(attrs.get("alt"))))

    children = node.get("content")
    if isinstance(children, list):
        for child in children:
            _collect_images(child, images)


def _mention_text(node: dict[str, Any]) -> str:
    attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
    for key in ("label", "name", "title", "username", "text"):
        value = attrs.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return "@mention"


def _collapse_blank_lines(lines: list[str]) -> list[str]:
    output: list[str] = []
    previous_blank = False
    for line in lines:
        is_blank = not line.strip()
        if is_blank and previous_blank:
            continue
        output.append(line)
        previous_blank = is_blank
    return output


def _as_optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None
