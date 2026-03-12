from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..clients.outline_models import OutlineComment, OutlineDocument
from .rich_text import extract_attachment_refs

_ATTACHMENT_URL_RE = re.compile(r"(?:https?://[^\s)]+)?(?:/api/)?attachments\.redirect\?id=[^\s)]+", re.IGNORECASE)
_MARKDOWN_ATTACHMENT_LINK_RE = re.compile(
    r"\[(?P<label>[^\]]+)\]\((?P<url>(?:https?://[^\s)]+)?(?:/api/)?attachments\.redirect\?id=[^\s)]+)\)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class AttachmentContextItem:
    source_url: str
    suggested_path: str
    origin: str
    kind: str
    label: str | None = None
    comment_id: str | None = None
    author_name: str | None = None


@dataclass(frozen=True, slots=True)
class AttachmentRef:
    source_url: str
    kind: str
    label: str | None = None


def collect_attachment_context(
    *,
    document: OutlineDocument,
    comments: list[OutlineComment],
    current_comment_id: str,
) -> list[AttachmentContextItem]:
    items: list[AttachmentContextItem] = []
    seen_urls: set[str] = set()
    used_paths: set[str] = set()

    def add_item(
        *,
        source_url: str,
        origin: str,
        kind: str,
        label: str | None = None,
        comment_id: str | None = None,
        author_name: str | None = None,
    ) -> None:
        normalized_url = _normalize_source_url(source_url)
        if not normalized_url or normalized_url in seen_urls:
            return
        suggested_path = _make_suggested_path(
            origin=origin,
            kind=kind,
            label=label,
            comment_id=comment_id,
            used_paths=used_paths,
        )
        items.append(
            AttachmentContextItem(
                source_url=normalized_url,
                suggested_path=suggested_path,
                origin=origin,
                kind=kind,
                label=_clean_optional_text(label),
                comment_id=comment_id,
                author_name=_clean_optional_text(author_name),
            )
        )
        seen_urls.add(normalized_url)

    current_comments = [item for item in comments if item.id == current_comment_id]
    other_comments = [item for item in comments if item.id != current_comment_id]

    for comment in current_comments + other_comments:
        origin = "current_comment" if comment.id == current_comment_id else "thread_comment"
        for ref in extract_attachment_refs(comment.data):
            add_item(
                source_url=ref.source_url,
                origin=origin,
                kind=ref.kind,
                label=ref.label,
                comment_id=comment.id,
                author_name=comment.created_by_name,
            )

    for ref in _extract_document_attachment_refs(document.text or ""):
        add_item(source_url=ref.source_url, origin="document", kind=ref.kind, label=ref.label)

    return items


def format_attachment_context_for_prompt(items: list[AttachmentContextItem]) -> str | None:
    if not items:
        return None
    lines = [
        "Available attachment candidates for `download_attachment` (use these exact values when needed):"
    ]
    for item in items:
        parts = [f"origin={item.origin}", f"source_url={item.source_url}", f"path={item.suggested_path}"]
        if item.label:
            parts.insert(1, f"label={item.label}")
        if item.comment_id:
            parts.append(f"comment_id={item.comment_id}")
        if item.author_name:
            parts.append(f"author={item.author_name}")
        if item.kind:
            parts.append(f"kind={item.kind}")
        lines.append("- " + " ; ".join(parts))
    return "\n".join(lines)


def repair_download_attachment_args(
    tool_name: str,
    args: dict[str, Any],
    context_extra: dict[str, Any],
) -> dict[str, Any]:
    if tool_name != "download_attachment":
        return dict(args)

    raw_items = context_extra.get("available_attachment_context", [])
    items = [item for item in raw_items if isinstance(item, AttachmentContextItem)]
    if not items:
        return dict(args)

    repaired = dict(args)
    source_url = (
        _clean_optional_text(repaired.get("source_url"))
        or _clean_optional_text(repaired.get("attachment_url"))
    )
    path = _clean_optional_text(repaired.get("path"))

    matched_by_source = None
    if source_url:
        normalized_source = _normalize_source_url(source_url)
        matched_by_source = next((item for item in items if item.source_url == normalized_source), None)
        repaired["source_url"] = normalized_source
        repaired.pop("attachment_url", None)
    elif path:
        matched_by_source = next((item for item in items if item.suggested_path == path), None)
        if matched_by_source is not None:
            repaired["source_url"] = matched_by_source.source_url
    else:
        preferred = _prefer_single_attachment_candidate(items)
        if preferred is not None:
            repaired["source_url"] = preferred.source_url
            matched_by_source = preferred

    if not path:
        candidate = matched_by_source
        if candidate is None:
            source_now = _clean_optional_text(repaired.get("source_url"))
            if source_now:
                normalized_source = _normalize_source_url(source_now)
                candidate = next((item for item in items if item.source_url == normalized_source), None)
            if candidate is None:
                candidate = _prefer_single_attachment_candidate(items)
        if candidate is not None:
            repaired["path"] = candidate.suggested_path

    return repaired


def _prefer_single_attachment_candidate(items: list[AttachmentContextItem]) -> AttachmentContextItem | None:
    current_items = [item for item in items if item.origin == "current_comment"]
    if len(current_items) == 1:
        return current_items[0]
    if len(items) == 1:
        return items[0]
    return None


def _extract_document_attachment_refs(text: str) -> list[AttachmentRef]:
    refs: list[AttachmentRef] = []
    seen: set[str] = set()

    for match in _MARKDOWN_ATTACHMENT_LINK_RE.finditer(text):
        url = _normalize_source_url(match.group("url"))
        if not url or url in seen:
            continue
        refs.append(AttachmentRef(source_url=url, kind="attachment", label=_clean_optional_text(match.group("label"))))
        seen.add(url)

    for match in _ATTACHMENT_URL_RE.finditer(text):
        url = _normalize_source_url(match.group(0))
        if not url or url in seen:
            continue
        refs.append(AttachmentRef(source_url=url, kind="attachment"))
        seen.add(url)

    return refs


def _make_suggested_path(
    *,
    origin: str,
    kind: str,
    label: str | None,
    comment_id: str | None,
    used_paths: set[str],
) -> str:
    suffix = _filename_suffix(label, kind)
    stem_parts = ["attachments"]
    if origin == "current_comment":
        stem_parts.append("current")
    elif origin == "thread_comment":
        stem_parts.append("thread")
    else:
        stem_parts.append("document")
    cleaned_label = _slugify_filename_stem(label)
    if cleaned_label:
        stem_parts.append(cleaned_label)
    elif comment_id:
        stem_parts.append(comment_id[:12])
    else:
        stem_parts.append(kind)
    candidate = "/".join(stem_parts) + suffix
    if candidate not in used_paths:
        used_paths.add(candidate)
        return candidate

    counter = 2
    while True:
        deduped = "/".join(stem_parts[:-1] + [f"{stem_parts[-1]}-{counter}"]) + suffix
        if deduped not in used_paths:
            used_paths.add(deduped)
            return deduped
        counter += 1


def _filename_suffix(label: str | None, kind: str) -> str:
    cleaned = _clean_optional_text(label)
    if cleaned:
        match = re.search(r"(\.[A-Za-z0-9]{1,8})$", cleaned)
        if match:
            return match.group(1).lower()
    if kind == "image":
        return ".img"
    return ".bin"


def _slugify_filename_stem(value: str | None) -> str | None:
    cleaned = _clean_optional_text(value)
    if not cleaned:
        return None
    stem = re.sub(r"\.[A-Za-z0-9]{1,8}$", "", cleaned)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-._")
    return slug[:48] if slug else None


def _normalize_source_url(value: str | None) -> str | None:
    cleaned = _clean_optional_text(value)
    if not cleaned:
        return None
    if cleaned.startswith("attachments.redirect?"):
        return "/api/" + cleaned
    if cleaned.startswith("/attachments.redirect?"):
        return "/api" + cleaned
    return cleaned


def _clean_optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    compact = value.strip()
    return compact or None
