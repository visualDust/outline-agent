from __future__ import annotations

import mimetypes
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from .. import __version__
from ..core.logging import logger
from .outline_attachments import build_multipart_body as _build_multipart_body
from .outline_comments import (
    OUTLINE_COMMENT_MAX_CHARS,
    build_comment_data,
    build_markdown_comment_data,
    is_comment_too_long_error as _is_comment_too_long_error,
    normalize_comment_markdown,
    prepare_comment_chunks,
    should_retry_comment_create_as_data as _should_retry_comment_create_as_data,
    should_retry_comment_create_as_plain_data as _should_retry_comment_create_as_plain_data,
    should_retry_comment_update_as_text as _should_retry_comment_update_as_text,
    split_comment_text,
)
from .outline_documents import extract_document_items as _extract_document_items
from .outline_documents import parse_document_item as _parse_document_item
from .outline_exceptions import OutlineClientError
from .outline_http import extract_error_message as _extract_error_message
from .outline_models import OutlineCollection, OutlineComment, OutlineDocument, OutlineUser
from .outline_utils import as_optional_str as _as_optional_str

USER_AGENT = f"outline-agent/{__version__}"


class OutlineClient:
    def __init__(self, base_url: str, api_key: str | None = None, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    async def auth_info(self) -> dict[str, Any]:
        payload = await self._call("auth.info", {})
        if not isinstance(payload, dict):
            raise OutlineClientError("Unexpected auth.info response format")
        return payload

    async def current_user(self) -> OutlineUser:
        payload = await self.auth_info()
        data = payload.get("data")
        if not isinstance(data, dict):
            raise OutlineClientError("Unexpected auth.info data payload")
        user = data.get("user")
        if not isinstance(user, dict):
            raise OutlineClientError("Unexpected auth.info user payload")
        user_id = _as_optional_str(user.get("id"))
        if not user_id:
            raise OutlineClientError("auth.info did not include a user id")
        return OutlineUser(
            id=user_id,
            name=_as_optional_str(user.get("name")),
            email=_as_optional_str(user.get("email")),
        )

    async def collection_info(self, collection_id: str) -> OutlineCollection:
        payload = await self._call("collections.info", {"id": collection_id})
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if not isinstance(data, dict):
            raise OutlineClientError("Unexpected collections.info response format")
        return OutlineCollection(
            id=collection_id,
            name=_as_optional_str(data.get("name")),
            description=_as_optional_str(data.get("description")),
            url=_as_optional_str(data.get("url")),
        )

    async def document_info(self, document_id: str) -> OutlineDocument:
        payload = await self._call("documents.info", {"id": document_id})
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if not isinstance(data, dict):
            raise OutlineClientError("Unexpected documents.info response format")
        return OutlineDocument(
            id=document_id,
            title=_as_optional_str(data.get("title")),
            collection_id=_as_optional_str(data.get("collectionId")),
            url=_as_optional_str(data.get("url")),
            text=_as_optional_str(data.get("text")),
        )

    async def documents_search(
        self,
        query: str,
        *,
        collection_id: str | None = None,
        limit: int = 25,
    ) -> list[OutlineDocument]:
        payload: dict[str, Any] = {"query": query, "limit": limit}
        if collection_id:
            payload["collectionId"] = collection_id
        result = await self._call("documents.search", payload)
        items = _extract_document_items(result)
        documents: list[OutlineDocument] = []
        for item in items:
            parsed = _parse_document_item(item)
            if parsed is not None:
                documents.append(parsed)
        return documents

    async def update_document(
        self,
        document_id: str,
        *,
        title: str | None = None,
        text: str | None = None,
        publish: bool | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"id": document_id}
        if title is not None:
            payload["title"] = title
        if text is not None:
            payload["text"] = text
        if publish is not None:
            payload["publish"] = publish
        result = await self._call("documents.update", payload)
        if not isinstance(result, dict):
            raise OutlineClientError("Unexpected documents.update response format")
        return result

    async def create_document(
        self,
        *,
        title: str,
        text: str,
        collection_id: str,
        parent_document_id: str | None = None,
        publish: bool = True,
    ) -> OutlineDocument:
        payload: dict[str, Any] = {
            "title": title,
            "text": text,
            "collectionId": collection_id,
            "publish": publish,
        }
        if parent_document_id is not None:
            payload["parentDocumentId"] = parent_document_id
        result = await self._call("documents.create", payload)
        data = result.get("data") if isinstance(result.get("data"), dict) else result
        if not isinstance(data, dict):
            raise OutlineClientError("Unexpected documents.create response format")
        return OutlineDocument(
            id=_as_optional_str(data.get("id")) or "",
            title=_as_optional_str(data.get("title")) or title,
            collection_id=_as_optional_str(data.get("collectionId")) or collection_id,
            url=_as_optional_str(data.get("url")),
            text=_as_optional_str(data.get("text")) or text,
        )

    async def comments_list(self, document_id: str, limit: int = 25, offset: int = 0) -> list[OutlineComment]:
        payload = await self._call(
            "comments.list",
            {"documentId": document_id, "limit": limit, "offset": offset},
        )
        data = payload.get("data")
        if not isinstance(data, list):
            raise OutlineClientError("Unexpected comments.list response format")
        comments: list[OutlineComment] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            created_by = item.get("createdBy") if isinstance(item.get("createdBy"), dict) else {}
            comments.append(
                OutlineComment(
                    id=str(item.get("id")),
                    document_id=str(item.get("documentId")),
                    parent_comment_id=_as_optional_str(item.get("parentCommentId")),
                    created_by_id=_as_optional_str(item.get("createdById")),
                    created_by_name=_as_optional_str(created_by.get("name")),
                    created_at=_as_optional_str(item.get("createdAt")),
                    data=item.get("data") if isinstance(item.get("data"), dict) else {},
                )
            )
        return comments

    async def attachments_create(
        self,
        *,
        name: str,
        document_id: str,
        content_type: str,
        size: int,
        preset: str = "documentAttachment",
    ) -> dict[str, Any]:
        result = await self._call(
            "attachments.create",
            {
                "name": name,
                "documentId": document_id,
                "contentType": content_type,
                "size": size,
                "preset": preset,
            },
        )
        if not isinstance(result, dict):
            raise OutlineClientError("Unexpected attachments.create response format")
        return result

    async def attachments_delete(self, attachment_id: str) -> dict[str, Any]:
        result = await self._call("attachments.delete", {"id": attachment_id})
        if not isinstance(result, dict):
            raise OutlineClientError("Unexpected attachments.delete response format")
        return result

    async def upload_attachment(
        self,
        document_id: str,
        file_path: str | Path,
        *,
        name: str | None = None,
        content_type: str | None = None,
        preset: str = "documentAttachment",
    ) -> dict[str, Any]:
        path = Path(file_path)
        if not path.exists():
            raise OutlineClientError(f"Attachment file does not exist: {path}")
        if not path.is_file():
            raise OutlineClientError(f"Attachment path is not a file: {path}")

        resolved_name = (name or "").strip() or path.name
        resolved_content_type = content_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        create_result = await self.attachments_create(
            name=resolved_name,
            document_id=document_id,
            content_type=resolved_content_type,
            size=path.stat().st_size,
            preset=preset,
        )

        data = create_result.get("data")
        if not isinstance(data, dict):
            raise OutlineClientError("Unexpected attachments.create data payload")

        upload_url = _as_optional_str(data.get("uploadUrl"))
        if not upload_url:
            raise OutlineClientError("attachments.create did not return an uploadUrl")

        form_fields = data.get("form") if isinstance(data.get("form"), dict) else {}
        attachment = data.get("attachment") if isinstance(data.get("attachment"), dict) else {}
        attachment_id = _as_optional_str(attachment.get("id"))
        attachment_url = _as_optional_str(attachment.get("url"))
        if not attachment_url and attachment_id:
            attachment_url = f"/api/attachments.redirect?id={attachment_id}"
        if attachment_url:
            attachment = {**attachment, "url": self._build_url(attachment_url)}

        try:
            upload_result = await self._upload_file(
                upload_url=upload_url,
                form_fields=form_fields,
                file_path=path,
                content_type=resolved_content_type,
            )
        except Exception:
            if attachment_id:
                try:
                    await self.attachments_delete(attachment_id)
                except OutlineClientError:
                    pass
            raise

        return {
            "ok": True,
            "documentId": document_id,
            "name": resolved_name,
            "contentType": resolved_content_type,
            "attachment": attachment,
            "upload": upload_result,
        }

    async def download_attachment(
        self,
        url_or_path: str,
        file_path: str | Path,
    ) -> dict[str, Any]:
        url = self._build_url(url_or_path)
        target_path = Path(file_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        headers = {"User-Agent": USER_AGENT}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url, headers=headers, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise OutlineClientError(f"Attachment download failed: {exc}") from exc

        if response.is_error:
            message = _extract_error_message(response)
            raise OutlineClientError(f"Attachment download error {response.status_code}: {message}")

        target_path.write_bytes(response.content)
        response_headers = response.headers if hasattr(response, "headers") else None
        content_type = response_headers.get("content-type") if hasattr(response_headers, "get") else None
        return {
            "ok": True,
            "url": url,
            "file_path": str(target_path),
            "size": len(response.content),
            "content_type": content_type,
        }

    async def create_comment(self, document_id: str, text: str, parent_comment_id: str | None = None) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for chunk in prepare_comment_chunks(text, max_chars=OUTLINE_COMMENT_MAX_CHARS):
            results.extend(
                await self._create_comment_with_auto_split(
                    document_id=document_id,
                    text=chunk,
                    parent_comment_id=parent_comment_id,
                    max_chars=OUTLINE_COMMENT_MAX_CHARS,
                )
            )

        if not results:
            raise OutlineClientError("Failed to create comment: no comment chunks were produced")
        return results[-1]

    async def _create_comment_with_auto_split(
        self,
        *,
        document_id: str,
        text: str,
        parent_comment_id: str | None,
        max_chars: int,
    ) -> list[dict[str, Any]]:
        try:
            return [await self._create_comment_once(document_id, text, parent_comment_id)]
        except OutlineClientError as exc:
            if not _is_comment_too_long_error(exc) or max_chars <= 1:
                raise

            smaller_max_chars = max(1, max_chars // 2)
            smaller_chunks = split_comment_text(text, max_chars=smaller_max_chars)
            if len(smaller_chunks) <= 1:
                raise

            results: list[dict[str, Any]] = []
            for chunk in smaller_chunks:
                results.extend(
                    await self._create_comment_with_auto_split(
                        document_id=document_id,
                        text=chunk,
                        parent_comment_id=parent_comment_id,
                        max_chars=smaller_max_chars,
                    )
                )
            return results

    async def _create_comment_once(
        self,
        document_id: str,
        text: str,
        parent_comment_id: str | None = None,
    ) -> dict[str, Any]:
        normalized = normalize_comment_markdown(text)
        payload: dict[str, Any] = {"documentId": document_id}
        if parent_comment_id:
            payload["parentCommentId"] = parent_comment_id

        if self.api_key:
            try:
                rich_payload = {**payload, "data": build_markdown_comment_data(normalized)}
                result = await self._call_http("comments.create", rich_payload)
            except OutlineClientError as exc:
                if not _should_retry_comment_create_as_data(exc):
                    raise

                logger.debug("comments.create rich payload failed; falling back to text: {}", exc)
                try:
                    fallback_payload = {**payload, "text": normalized}
                    result = await self._call_http("comments.create", fallback_payload)
                except OutlineClientError as fallback_exc:
                    if not _should_retry_comment_create_as_plain_data(fallback_exc):
                        raise

                    logger.debug("comments.create text fallback failed; falling back to plain data: {}", fallback_exc)
                    plain_fallback_payload = {**payload, "data": build_comment_data(normalized)}
                    result = await self._call_http("comments.create", plain_fallback_payload)
        else:
            payload["data"] = build_comment_data(normalized)
            result = await self._call("comments.create", payload)

        if not isinstance(result, dict):
            raise OutlineClientError("Unexpected comments.create response format")
        return result

    async def update_comment(self, comment_id: str, text: str) -> dict[str, Any]:
        normalized = normalize_comment_markdown(text)
        payload: dict[str, Any] = {"id": comment_id}
        comment_data = build_markdown_comment_data(normalized)
        if self.api_key:
            payload["data"] = comment_data
            try:
                result = await self._call_http("comments.update", payload)
            except OutlineClientError as exc:
                if not _should_retry_comment_update_as_text(exc):
                    raise

                fallback_payload = {key: value for key, value in payload.items() if key != "data"}
                fallback_payload["data"] = build_comment_data(normalized)
                result = await self._call_http("comments.update", fallback_payload)
        else:
            payload["data"] = comment_data
            result = await self._call("comments.update", payload)
        if not isinstance(result, dict):
            raise OutlineClientError("Unexpected comments.update response format")
        return result

    async def add_comment_reaction(self, comment_id: str, emoji: str) -> dict[str, Any]:
        result = await self._call("comments.add_reaction", {"id": comment_id, "emoji": emoji})
        if not isinstance(result, dict):
            raise OutlineClientError("Unexpected comments.add_reaction response format")
        return result

    async def remove_comment_reaction(self, comment_id: str, emoji: str) -> dict[str, Any]:
        result = await self._call("comments.remove_reaction", {"id": comment_id, "emoji": emoji})
        if not isinstance(result, dict):
            raise OutlineClientError("Unexpected comments.remove_reaction response format")
        return result

    async def _call(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._call_http(endpoint, payload)

    async def _call_http(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise OutlineClientError("No explicit OUTLINE_API_KEY configured")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        url = f"{self.base_url}/{endpoint}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise OutlineClientError(f"Outline request failed: {exc}") from exc

        if response.is_error:
            message = _extract_error_message(response)
            raise OutlineClientError(f"Outline API error {response.status_code}: {message}")

        data = response.json()
        if not isinstance(data, dict):
            raise OutlineClientError("Outline API returned a non-object JSON response")
        return data

    async def _upload_file(
        self,
        *,
        upload_url: str,
        form_fields: dict[str, Any],
        file_path: Path,
        content_type: str,
    ) -> dict[str, Any]:
        boundary = f"outline-agent-{uuid.uuid4().hex}"
        body = _build_multipart_body(
            boundary=boundary,
            form_fields=form_fields,
            file_path=file_path,
            content_type=content_type,
        )
        headers = {"User-Agent": USER_AGENT}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    self._build_url(upload_url),
                    content=body,
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise OutlineClientError(f"Attachment upload failed: {exc}") from exc

        if response.is_error:
            message = _extract_error_message(response)
            raise OutlineClientError(f"Attachment upload error {response.status_code}: {message}")

        if not response.content:
            return {"ok": True}

        try:
            payload = response.json()
        except ValueError:
            text = response.text.strip()
            return {"ok": True, "text": text} if text else {"ok": True}

        if not isinstance(payload, dict):
            raise OutlineClientError("Attachment upload returned a non-object JSON response")
        return payload

    def _build_url(self, path_or_url: str) -> str:
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            return path_or_url
        if path_or_url.startswith("/"):
            parts = urlsplit(self.base_url)
            return urlunsplit((parts.scheme, parts.netloc, path_or_url, "", ""))
        return urljoin(f"{self.base_url}/", path_or_url.lstrip("/"))
