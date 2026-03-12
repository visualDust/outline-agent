from __future__ import annotations

from typing import Any

from .outline_models import OutlineDocument
from .outline_utils import as_optional_str


def extract_document_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[Any] = []
    data = payload.get("data")
    if isinstance(data, list):
        candidates = data
    elif isinstance(data, dict):
        if isinstance(data.get("data"), list):
            candidates = data.get("data")
        elif isinstance(data.get("documents"), list):
            candidates = data.get("documents")
    elif isinstance(payload.get("documents"), list):
        candidates = payload.get("documents")

    return [item for item in candidates if isinstance(item, dict)]


def parse_document_item(item: dict[str, Any]) -> OutlineDocument | None:
    document_id = as_optional_str(item.get("id")) or as_optional_str(item.get("documentId"))
    if not document_id:
        return None
    return OutlineDocument(
        id=document_id,
        title=as_optional_str(item.get("title")),
        collection_id=as_optional_str(item.get("collectionId")),
        url=as_optional_str(item.get("url")),
        text=as_optional_str(item.get("text")) or as_optional_str(item.get("excerpt")),
    )
