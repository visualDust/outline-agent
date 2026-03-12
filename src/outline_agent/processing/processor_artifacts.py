from __future__ import annotations

import hashlib
from pathlib import Path

from ..runtime.tool_runtime import ToolExecutionStep, UploadedAttachment
from ..utils.markdown_sections import normalize_markdown_text, parse_markdown_sections


def append_uploaded_attachment_links(reply: str, attachments: list[UploadedAttachment]) -> str:
    unique_items: list[UploadedAttachment] = []
    seen: set[tuple[str, str]] = set()

    for item in attachments:
        url = (item.url or "").strip()
        if not url:
            continue
        name = (item.name or item.path or "download").strip() or "download"
        key = (name, url)
        if key in seen:
            continue
        seen.add(key)
        unique_items.append(item)

    if not unique_items:
        return reply
    missing_items = [item for item in unique_items if (item.url or "") not in reply]
    if not missing_items:
        return reply

    lines = ["Uploaded files:"]
    for item in missing_items:
        url = (item.url or "").strip()
        name = (item.name or item.path or "download").strip() or "download"
        lines.append(f"- [{name}]({url})")

    suffix = "\n\n" + "\n".join(lines)
    return reply.rstrip() + suffix


def find_redundant_upload_paths(
    steps: list[ToolExecutionStep],
    uploaded_attachments: list[UploadedAttachment],
    work_dir: Path,
) -> list[str]:
    if not steps or any(step.tool != "upload_attachment" for step in steps):
        return []

    uploaded_hashes_by_path: dict[str, set[str]] = {}
    for item in uploaded_attachments:
        path = (item.path or "").strip()
        file_hash = (item.file_hash or "").strip()
        if not path or not file_hash:
            continue
        uploaded_hashes_by_path.setdefault(path, set()).add(file_hash)
    if not uploaded_hashes_by_path:
        return []

    repeated_paths: list[str] = []
    seen: set[str] = set()
    for step in steps:
        path = (step.path or "").strip()
        current_hash = hash_work_dir_file(work_dir, path)
        if not path or not current_hash:
            return []
        if current_hash not in uploaded_hashes_by_path.get(path, set()):
            return []
        if path in seen:
            continue
        seen.add(path)
        repeated_paths.append(path)
    return repeated_paths


def hash_work_dir_file(work_dir: Path, relative_path: str) -> str | None:
    candidate = (work_dir / relative_path).resolve()
    root = work_dir.resolve()
    if candidate != root and root not in candidate.parents:
        return None
    if not candidate.exists() or not candidate.is_file():
        return None

    digest = hashlib.sha256()
    with candidate.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def preview_registered_attachments(attachments: list[UploadedAttachment]) -> str | None:
    if not attachments:
        return None
    names = ", ".join((item.name or item.path or "download").strip() or "download" for item in attachments)
    return f"registered uploaded files in document: {names}"


def format_registered_attachment_context(attachments: list[UploadedAttachment]) -> str | None:
    if not attachments:
        return None

    lines = ["- artifact link registration: applied"]
    for item in attachments:
        url = (item.url or "").strip()
        if not url:
            continue
        name = (item.name or item.path or "download").strip() or "download"
        lines.append(f"  registered_file: {name} -> {url}")
        lines.append(f"  uploaded_file: {name} -> {url}")
    return "\n".join(lines)


def register_uploaded_attachments_in_document_text(
    document_text: str | None,
    attachments: list[UploadedAttachment],
) -> tuple[str | None, list[UploadedAttachment]]:
    if document_text is None:
        return None, []

    normalized = normalize_markdown_text(document_text) or ""
    unique_items: list[UploadedAttachment] = []
    seen: set[tuple[str, str]] = set()

    for item in attachments:
        url = (item.url or "").strip()
        if not url:
            continue
        name = (item.name or item.path or "download").strip() or "download"
        key = (name, url)
        if key in seen:
            continue
        seen.add(key)
        unique_items.append(item)

    registered_items = [item for item in unique_items if not document_already_references_attachment(normalized, item)]
    if not registered_items:
        return None, []

    bullet_lines = [
        f"- [{(item.name or item.path or 'download').strip() or 'download'}]({(item.url or '').strip()})"
        for item in registered_items
    ]
    if not bullet_lines:
        return None, []

    updated = insert_artifact_lines_into_document(normalized, bullet_lines)
    return updated, registered_items


def document_already_references_attachment(document_text: str, attachment: UploadedAttachment) -> bool:
    url = (attachment.url or "").strip()
    if url and url in document_text:
        return True
    attachment_id = (attachment.attachment_id or "").strip()
    return bool(attachment_id and attachment_id in document_text)


def insert_artifact_lines_into_document(document_text: str, bullet_lines: list[str]) -> str:
    artifact_section = find_artifact_section(document_text)
    block = "\n".join(bullet_lines)

    if artifact_section is not None:
        before = document_text[: artifact_section.end].rstrip("\n")
        after = document_text[artifact_section.end :].lstrip("\n")
        updated = before + "\n\n" + block
        if after:
            updated += "\n\n" + after
        return updated

    if not document_text.strip():
        return "## Uploaded Artifacts\n\n" + block
    return document_text.rstrip("\n") + "\n\n## Uploaded Artifacts\n\n" + block


def find_artifact_section(document_text: str):
    section_titles = {"artifacts", "uploaded artifacts", "generated artifacts"}
    for section in parse_markdown_sections(document_text):
        if not section.heading_path:
            continue
        heading = section.heading_path[-1].strip().casefold()
        if heading in section_titles:
            return section
    return None
