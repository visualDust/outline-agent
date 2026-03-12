from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from outline_agent.clients.outline_client import (
    OutlineClient,
    OutlineClientError,
    build_comment_data,
    build_markdown_comment_data,
    normalize_comment_markdown,
    prepare_comment_chunks,
    split_comment_text,
)
from outline_agent.utils.rich_text import extract_plain_text

def test_build_markdown_comment_data_uses_safe_blocks_after_markdown_normalization() -> None:
    result = build_markdown_comment_data("# Summary\n\n```bash\nnpm test\n```")

    assert result == {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "Summary", "marks": [{"type": "strong"}]}],
            },
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "Code:"}],
            },
            {
                "type": "bullet_list",
                "content": [
                    {
                        "type": "list_item",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "npm test"}],
                            }
                        ],
                    }
                ],
            },
        ],
    }


def test_split_comment_text_breaks_long_reply_on_line_boundaries() -> None:
    text = "\n".join(
        [
            "A" * 450,
            "B" * 450,
            "C" * 450,
        ]
    )

    chunks = split_comment_text(text, max_chars=1000)

    assert len(chunks) == 2
    assert chunks[0] == "\n".join(["A" * 450, "B" * 450])
    assert chunks[1] == "C" * 450
    assert all(len(extract_plain_text(build_comment_data(chunk))) <= 1000 for chunk in chunks)

class RecordingOutlineClient(OutlineClient):
    def __init__(self) -> None:
        super().__init__(base_url="https://outline.example/api", api_key="test-token", timeout=5)
        self.calls: list[tuple[str, dict]] = []

    def _build_shared_client(self):  # type: ignore[override]
        return None

    async def _call_http(self, endpoint: str, payload: dict):  # type: ignore[override]
        self.calls.append((endpoint, payload))
        return {"ok": True, "id": f"comment-{len(self.calls)}"}


class FallbackRecordingOutlineClient(OutlineClient):
    def __init__(self) -> None:
        super().__init__(base_url="https://outline.example/api", api_key="test-token", timeout=5)
        self.calls: list[tuple[str, dict]] = []

    def _build_shared_client(self):  # type: ignore[override]
        return None

    async def _call_http(self, endpoint: str, payload: dict):  # type: ignore[override]
        self.calls.append((endpoint, payload))
        if len(self.calls) == 1:
            if endpoint != "comments.create":
                raise RuntimeError("unexpected endpoint")
            raise OutlineClientError("Outline API error 500: Internal error")
        return {"ok": True, "id": f"comment-{len(self.calls)}"}


class PlainFallbackRecordingOutlineClient(OutlineClient):
    def __init__(self) -> None:
        super().__init__(base_url="https://outline.example/api", api_key="test-token", timeout=5)
        self.calls: list[tuple[str, dict]] = []

    def _build_shared_client(self):  # type: ignore[override]
        return None

    async def _call_http(self, endpoint: str, payload: dict):  # type: ignore[override]
        self.calls.append((endpoint, payload))
        if len(self.calls) == 1:
            raise OutlineClientError("Outline API error 500: Internal error")
        if len(self.calls) == 2:
            raise OutlineClientError("Outline API error 400: data: Invalid data")
        return {"ok": True, "id": f"comment-{len(self.calls)}"}


class AttachmentRecordingOutlineClient(OutlineClient):
    def __init__(self) -> None:
        super().__init__(base_url="https://outline.example/api", api_key="test-token", timeout=5)
        self.calls: list[tuple[str, dict]] = []
        self.uploads: list[dict] = []

    def _build_shared_client(self):  # type: ignore[override]
        return None

    async def _call_http(self, endpoint: str, payload: dict):  # type: ignore[override]
        self.calls.append((endpoint, payload))
        if endpoint == "attachments.create":
            return {
                "data": {
                    "uploadUrl": "/api/files.create",
                    "form": {"key": "uploads/test/report.pdf"},
                    "attachment": {
                        "id": "attachment-1",
                        "name": payload["name"],
                        "url": "/api/attachments.redirect?id=attachment-1",
                    },
                }
            }
        return {"ok": True}

    async def _upload_file(self, *, upload_url: str, form_fields: dict, file_path: Path, content_type: str):  # type: ignore[override]
        self.uploads.append(
            {
                "upload_url": upload_url,
                "form_fields": form_fields,
                "file_path": file_path,
                "content_type": content_type,
            }
        )
        return {"ok": True}


class UpdateFallbackRecordingOutlineClient(OutlineClient):
    def __init__(self) -> None:
        super().__init__(base_url="https://outline.example/api", api_key="test-token", timeout=5)
        self.calls: list[tuple[str, dict]] = []

    def _build_shared_client(self):  # type: ignore[override]
        return None

    async def _call_http(self, endpoint: str, payload: dict):  # type: ignore[override]
        self.calls.append((endpoint, payload))
        if len(self.calls) == 1:
            raise OutlineClientError("Outline API error 400: data: Invalid data")
        return {"ok": True, "id": payload.get("id", "comment-1")}


class MultipartUploadOutlineClient(OutlineClient):
    def __init__(self) -> None:
        super().__init__(base_url="https://outline.example/api", api_key="test-token", timeout=5)

    def _build_shared_client(self):  # type: ignore[override]
        return None

    async def _call_http(self, endpoint: str, payload: dict):  # type: ignore[override]
        if endpoint != "attachments.create":
            raise RuntimeError(f"unexpected endpoint: {endpoint}")
        return {
            "data": {
                "uploadUrl": "/api/files.create",
                "form": {"key": "uploads/test/report.pdf", "token": "abc123"},
                "attachment": {"id": "attachment-1", "name": payload["name"]},
            }
        }


def test_outline_client_create_comment_splits_overlong_reply_into_multiple_comments() -> None:
    client = RecordingOutlineClient()
    text = "\n".join(
        [
            "A" * 450,
            "B" * 450,
            "C" * 450,
        ]
    )

    result = asyncio.run(client.create_comment("doc-1", text, parent_comment_id="root-1"))

    assert result["id"] == "comment-2"
    assert len(client.calls) == 2
    assert all(endpoint == "comments.create" for endpoint, _ in client.calls)
    assert all(payload["documentId"] == "doc-1" for _, payload in client.calls)
    assert all(payload["parentCommentId"] == "root-1" for _, payload in client.calls)
    posted_texts = [extract_plain_text(payload["data"]) for _, payload in client.calls]
    assert posted_texts == [
        "[1/2]\n" + "\n".join(["A" * 450, "B" * 450]),
        "[2/2]\n" + "C" * 450,
    ]
    assert all(len(item) <= 1000 for item in posted_texts)


def test_outline_client_create_comment_uses_rich_text_data_payload() -> None:
    client = RecordingOutlineClient()

    result = asyncio.run(client.create_comment("doc-1", "**bold**\n\n`code`", parent_comment_id="root-1"))

    assert result["id"] == "comment-1"
    assert client.calls == [
        (
            "comments.create",
            {
                "documentId": "doc-1",
                "parentCommentId": "root-1",
                "data": {
                    "type": "doc",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "bold",
                                    "marks": [{"type": "strong"}],
                                }
                            ],
                        },
                        {
                            "type": "paragraph",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "code",
                                    "marks": [{"type": "code_inline"}],
                                }
                            ],
                        },
                    ],
                },
            },
        )
    ]

def test_outline_client_update_comment_retries_invalid_data_with_plain_data_payload() -> None:
    client = UpdateFallbackRecordingOutlineClient()

    result = asyncio.run(client.update_comment("comment-1", "**updated**\n\n`code`"))

    assert result["id"] == "comment-1"
    assert client.calls == [
        (
            "comments.update",
            {
                "id": "comment-1",
                "data": {
                    "type": "doc",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "updated",
                                    "marks": [{"type": "strong"}],
                                }
                            ],
                        },
                        {
                            "type": "paragraph",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "code",
                                    "marks": [{"type": "code_inline"}],
                                }
                            ],
                        },
                    ],
                },
            },
        ),
        (
            "comments.update",
            {
                "id": "comment-1",
                "data": {
                    "type": "doc",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "**updated**",
                                }
                            ],
                        },
                        {
                            "type": "paragraph",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "`code`",
                                }
                            ],
                        },
                    ],
                },
            },
        ),
    ]

def test_outline_client_create_comment_retries_rich_fallback_with_plain_data_payload() -> None:
    client = PlainFallbackRecordingOutlineClient()

    result = asyncio.run(client.create_comment("doc-1", "**bold**\n\n`code`", parent_comment_id="root-1"))

    assert result["id"] == "comment-3"
    assert client.calls == [
        (
            "comments.create",
            {
                "documentId": "doc-1",
                "parentCommentId": "root-1",
                "data": {
                    "type": "doc",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "bold",
                                    "marks": [{"type": "strong"}],
                                }
                            ],
                        },
                        {
                            "type": "paragraph",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "code",
                                    "marks": [{"type": "code_inline"}],
                                }
                            ],
                        },
                    ],
                },
            },
        ),
        (
            "comments.create",
            {
                "documentId": "doc-1",
                "parentCommentId": "root-1",
                "text": "**bold**\n\n`code`",
            },
        ),
        (
            "comments.create",
            {
                "documentId": "doc-1",
                "parentCommentId": "root-1",
                "data": {
                    "type": "doc",
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "**bold**"}]},
                        {"type": "paragraph", "content": [{"type": "text", "text": "`code`"}]},
                    ],
                },
            },
        ),
    ]


def test_outline_client_upload_attachment_creates_attachment_and_uploads_file(tmp_path: Path) -> None:
    client = AttachmentRecordingOutlineClient()
    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF-1.7\nfake-pdf\n")

    result = asyncio.run(client.upload_attachment("doc-1", source))

    assert result["ok"] is True
    assert result["attachment"]["id"] == "attachment-1"
    assert result["attachment"]["url"] == "https://outline.example/api/attachments.redirect?id=attachment-1"
    assert client.calls == [
        (
            "attachments.create",
            {
                "name": "report.pdf",
                "documentId": "doc-1",
                "contentType": "application/pdf",
                "size": source.stat().st_size,
                "preset": "documentAttachment",
            },
        )
    ]
    assert client.uploads == [
        {
            "upload_url": "/api/files.create",
            "form_fields": {"key": "uploads/test/report.pdf"},
            "file_path": source,
            "content_type": "application/pdf",
        }
    ]

def test_outline_client_upload_attachment_builds_async_safe_multipart_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MultipartUploadOutlineClient()
    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF-1.7\nfake-pdf\n")
    captured: dict[str, object] = {}

    class FakeResponse:
        is_error = False
        content = b'{"ok": true}'
        text = '{"ok": true}'
        reason_phrase = "OK"

        def json(self):
            return {"ok": True}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, *, content=None, headers=None, **kwargs):
            captured["url"] = url
            captured["content"] = content
            captured["headers"] = headers
            captured["kwargs"] = kwargs
            return FakeResponse()

    monkeypatch.setattr("outline_agent.clients.outline_client.httpx.AsyncClient", FakeAsyncClient)

    result = asyncio.run(client.upload_attachment("doc-1", source))

    assert result["attachment"]["id"] == "attachment-1"
    assert captured["url"] == "https://outline.example/api/files.create"
    assert captured["kwargs"] == {}
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["Authorization"] == "Bearer test-token"
    assert headers["Content-Type"].startswith("multipart/form-data; boundary=")
    body = captured["content"]
    assert isinstance(body, bytes)
    assert b'name="key"' in body
    assert b"uploads/test/report.pdf" in body
    assert b'name="token"' in body
    assert b"abc123" in body
    assert b'name="file"; filename="report.pdf"' in body
    assert b"%PDF-1.7\nfake-pdf\n" in body


def test_outline_client_create_document_posts_documents_create_payload() -> None:
    client = RecordingOutlineClient()

    result = asyncio.run(
        client.create_document(
            title="New Summary Doc",
            text="# Summary\n\nHello",
            collection_id="coll-1",
        )
    )

    assert result.id == "comment-1"
    assert result.title == "New Summary Doc"
    assert result.collection_id == "coll-1"
    assert result.text == "# Summary\n\nHello"
    assert client.calls == [
        (
            "documents.create",
            {
                "title": "New Summary Doc",
                "text": "# Summary\n\nHello",
                "collectionId": "coll-1",
                "publish": True,
            },
        )
    ]


def test_outline_client_download_attachment_saves_response_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = OutlineClient(base_url="https://outline.example/api", api_key="test-token", timeout=5)
    target = tmp_path / "downloads" / "report.pdf"
    captured: dict[str, object] = {}

    class FakeResponse:
        is_error = False
        status_code = 200
        content = b"%PDF-1.7\nfake-pdf\n"
        headers = {"content-type": "application/pdf"}

        def json(self):
            return {"ok": True}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, *, headers=None, follow_redirects=False, **kwargs):
            captured["url"] = url
            captured["headers"] = headers
            captured["follow_redirects"] = follow_redirects
            captured["kwargs"] = kwargs
            return FakeResponse()

    monkeypatch.setattr("outline_agent.clients.outline_client.httpx.AsyncClient", FakeAsyncClient)

    result = asyncio.run(client.download_attachment("/api/attachments.redirect?id=attachment-1", target))

    assert result == {
        "ok": True,
        "url": "https://outline.example/api/attachments.redirect?id=attachment-1",
        "file_path": str(target),
        "size": len(b"%PDF-1.7\nfake-pdf\n"),
        "content_type": "application/pdf",
    }
    assert target.read_bytes() == b"%PDF-1.7\nfake-pdf\n"
    assert captured["url"] == "https://outline.example/api/attachments.redirect?id=attachment-1"
    assert captured["follow_redirects"] is True
    assert captured["kwargs"] == {}
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["Authorization"] == "Bearer test-token"
    assert headers["User-Agent"].startswith("outline-agent/")


def test_outline_client_download_attachment_resolves_relative_path_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = OutlineClient(base_url="https://outline.example/api", api_key="test-token", timeout=5)
    target = tmp_path / "report.txt"
    captured: dict[str, object] = {}

    class FakeResponse:
        is_error = False
        status_code = 200
        content = b"hello"
        headers = {"content-type": "text/plain"}

        def json(self):
            return {"ok": True}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, *, headers=None, follow_redirects=False, **kwargs):
            captured["url"] = url
            captured["follow_redirects"] = follow_redirects
            return FakeResponse()

    monkeypatch.setattr("outline_agent.clients.outline_client.httpx.AsyncClient", FakeAsyncClient)

    asyncio.run(client.download_attachment("attachments.redirect?id=attachment-1", target))

    assert captured["url"] == "https://outline.example/api/attachments.redirect?id=attachment-1"
    assert captured["follow_redirects"] is True
