from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class OutlineCollection:
    id: str
    name: str | None
    description: str | None
    url: str | None


@dataclass
class OutlineDocument:
    id: str
    title: str | None
    collection_id: str | None
    url: str | None
    text: str | None
    deleted_at: str | None = None
    archived_at: str | None = None


@dataclass
class OutlineComment:
    id: str
    document_id: str
    parent_comment_id: str | None
    created_by_id: str | None
    created_by_name: str | None
    created_at: str | None
    data: dict[str, Any]


@dataclass
class OutlineUser:
    id: str
    name: str | None
    email: str | None
