from __future__ import annotations

import asyncio
import json
from pathlib import Path

from outline_agent.app import _maybe_post_failure_comment
from outline_agent.clients.outline_client import (
    OutlineClientError,
    OutlineCollection,
    OutlineComment,
    OutlineDocument,
    OutlineUser,
)
from outline_agent.core.config import AppSettings
from outline_agent.models.webhook_models import WebhookEnvelope
from outline_agent.processing.processor import CommentProcessor
from outline_agent.state.store import ProcessedEventStore
from outline_agent.state.workspace import CollectionWorkspaceManager

AGENT_USER_ID = "6142fd16-1614-4062-b9ad-211cfef651e6"
AGENT_USER_LABEL = "Sincerely, Your Agent 😘"


def _maybe_document_update_no_edit(system_prompt: str) -> str | None:
    if "You decide whether to directly update the current Outline document for an agent." not in system_prompt:
        return None
    return json.dumps(
        {
            "decision": "no-edit",
            "reason": "No direct document change requested.",
            "operations": [],
            "summary": None,
        }
    )


def _maybe_document_creation_no_create(system_prompt: str) -> str | None:
    if "You decide whether to create a new Outline document for an agent." not in system_prompt:
        return None
    return json.dumps(
        {
            "decision": "no-create",
            "reason": "No separate new document was requested.",
            "title": None,
            "text": None,
            "summary": None,
        }
    )


def _maybe_nonreply_planner_response(system_prompt: str) -> str | None:
    if "You decide which agent subsystems should be invoked for a single Outline comment." in system_prompt:
        return json.dumps(
            {
                "document_creation": False,
                "document_update": False,
                "tool_use": False,
                "memory_action": False,
                "cross_thread_handoff": False,
                "same_document_comment_lookup": False,
                "reason": "No extra subsystem is needed.",
            }
        )
    document_creation_response = _maybe_document_creation_no_create(system_prompt)
    if document_creation_response is not None:
        return document_creation_response
    document_update_response = _maybe_document_update_no_edit(system_prompt)
    if document_update_response is not None:
        return document_update_response
    if "You decide whether an Outline comment agent should use local sandbox tools before replying." in system_prompt:
        return json.dumps(
            {
                "should_run": False,
                "reason": "No local tool work requested.",
                "steps": [],
            }
        )
    if "You manage explicit memory actions for an Outline agent." in system_prompt:
        return json.dumps(
            {
                "reason": "No memory change requested.",
                "actions": [],
            }
        )
    if "You decide whether an Outline comment needs extra thread-context retrieval before replying." in system_prompt:
        return json.dumps(
            {
                "cross_thread_handoff": False,
                "same_document_comment_lookup": False,
                "reason": "No extra thread-context retrieval is needed.",
            }
        )
    return None


class DummyOutlineClient:
    def __init__(self) -> None:
        self.posted: list[dict[str, str | None]] = []
        self.updated_comments: list[dict[str, str]] = []
        self.updated_documents: list[dict[str, str | bool | None]] = []
        self.created_documents: list[dict[str, str | bool | None]] = []
        self.uploaded_attachments: list[dict[str, str]] = []
        self.downloaded_attachments: list[dict[str, str]] = []
        self.reactions: list[tuple[str, str, str]] = []
        self.comment_items: list[OutlineComment] | None = None
        self.search_results: list[OutlineDocument] = []
        self.extra_documents: dict[str, OutlineDocument] = {}

    async def collection_info(self, collection_id: str) -> OutlineCollection:
        return OutlineCollection(
            id=collection_id,
            name="Outline Agent Dev Sandbox",
            description="Development and webhook testing sandbox.",
            url="/collection/outline-agent-dev-sandbox-pP7z24H2Ae",
        )

    async def document_info(self, document_id: str) -> OutlineDocument:
        if document_id in self.extra_documents:
            return self.extra_documents[document_id]
        return OutlineDocument(
            id=document_id,
            title="Outline Agent Kickoff",
            collection_id="107b2669-e0ad-4abd-a66e-28305124edc8",
            url="/doc/outline-agent-kickoff-IBD2Her2RI",
            text="# Outline Agent Kickoff\n\nA kickoff doc for the new Outline agent project.",
        )

    async def documents_search(
        self,
        query: str,
        *,
        collection_id: str | None = None,
        limit: int = 25,
    ) -> list[OutlineDocument]:
        return self.search_results[:limit]

    async def comments_list(self, document_id: str, limit: int = 25, offset: int = 0) -> list[OutlineComment]:
        if self.comment_items is not None:
            return self.comment_items[offset : offset + limit]
        return [
            OutlineComment(
                id="older-comment",
                document_id=document_id,
                parent_comment_id=None,
                created_by_id="user-1",
                created_by_name="Gavin Gong",
                created_at="2026-03-09T04:49:00.000Z",
                data={
                    "type": "doc",
                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Earlier note"}]}],
                },
            )
        ]

    async def current_user(self) -> OutlineUser:
        return OutlineUser(
            id=AGENT_USER_ID,
            name=AGENT_USER_LABEL,
            email="agent@example.com",
        )

    async def create_comment(self, document_id: str, text: str, parent_comment_id: str | None = None) -> dict:
        self.posted.append(
            {
                "document_id": document_id,
                "text": text,
                "parent_comment_id": parent_comment_id,
            }
        )
        return {"ok": True, "id": f"comment-{len(self.posted)}"}

    async def update_comment(self, comment_id: str, text: str) -> dict:
        self.updated_comments.append({"comment_id": comment_id, "text": text})
        return {"ok": True, "id": comment_id}

    async def update_document(
        self,
        document_id: str,
        *,
        title: str | None = None,
        text: str | None = None,
        publish: bool | None = None,
    ) -> dict:
        self.updated_documents.append(
            {
                "document_id": document_id,
                "title": title,
                "text": text,
                "publish": publish,
            }
        )
        return {"ok": True}

    async def create_document(
        self,
        *,
        title: str,
        text: str,
        collection_id: str,
        parent_document_id: str | None = None,
        publish: bool = True,
    ) -> OutlineDocument:
        created_id = f"created-doc-{len(self.created_documents) + 1}"
        self.created_documents.append(
            {
                "title": title,
                "text": text,
                "collection_id": collection_id,
                "parent_document_id": parent_document_id,
                "publish": publish,
            }
        )
        return OutlineDocument(
            id=created_id,
            title=title,
            collection_id=collection_id,
            url=f"/doc/{created_id}",
            text=text,
        )

    async def upload_attachment(self, document_id: str, file_path: Path) -> dict:
        self.uploaded_attachments.append({"document_id": document_id, "file_path": str(file_path)})
        attachment_id = f"attachment-{len(self.uploaded_attachments)}"
        return {
            "ok": True,
            "attachment": {
                "id": attachment_id,
                "name": file_path.name,
                "url": f"https://outline.example/api/attachments.redirect?id={attachment_id}",
            },
        }

    async def download_attachment(self, url_or_path: str, file_path: Path) -> dict:
        self.downloaded_attachments.append({"url_or_path": url_or_path, "file_path": str(file_path)})
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(b"\x89PNG\r\n\x1a\nfake-image")
        return {
            "ok": True,
            "url": url_or_path,
            "file_path": str(file_path),
            "size": len(b"\x89PNG\r\n\x1a\nfake-image"),
            "content_type": "image/png",
        }

    async def add_comment_reaction(self, comment_id: str, emoji: str) -> dict:
        self.reactions.append(("add", comment_id, emoji))
        return {"ok": True}

    async def remove_comment_reaction(self, comment_id: str, emoji: str) -> dict:
        self.reactions.append(("remove", comment_id, emoji))
        return {"ok": True}


class FailingCollectionInfoOutlineClient(DummyOutlineClient):
    async def collection_info(self, collection_id: str) -> OutlineCollection:
        raise OutlineClientError("forbidden")


class LongDocumentOutlineClient(DummyOutlineClient):
    async def document_info(self, document_id: str) -> OutlineDocument:
        return OutlineDocument(
            id=document_id,
            title="Outline Agent Design Spec",
            collection_id="107b2669-e0ad-4abd-a66e-28305124edc8",
            url="/doc/outline-agent-design-spec-xyz",
            text=(
                "# Outline Agent Design Spec\n\n"
                "This design document captures the current service architecture and delivery plan.\n\n"
                "## Overview\n\n"
                "The service receives Outline webhooks and replies in comment threads.\n\n"
                "## Roadmap\n\n"
                "- Validate webhook flow end to end\n"
                "- Improve thread memory handling\n"
                "- Add direct document editing\n\n"
                "## Risks\n\n"
                "- Ambiguous edit requests may cause unsafe changes\n"
                "- Long documents require scoped editing"
            ),
        )


class MissingBodyOutlineClient(DummyOutlineClient):
    async def document_info(self, document_id: str) -> OutlineDocument:
        return OutlineDocument(
            id=document_id,
            title="Untitled Draft",
            collection_id="107b2669-e0ad-4abd-a66e-28305124edc8",
            url="/doc/untitled-draft-xyz",
            text=None,
        )


class DummyModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        nonreply_response = _maybe_nonreply_planner_response(system_prompt)
        if nonreply_response is not None:
            return nonreply_response
        if "You maintain durable thread-local SESSION.md state" in system_prompt:
            assert "Latest user comment:" in user_prompt
            return json.dumps(
                {
                    "should_write": True,
                    "summary": "The user asked for a concise summary of the kickoff document.",
                    "open_questions": ["Whether the user wants a deeper breakdown next."],
                    "working_notes": ["Keep the answer concise and action-oriented."],
                }
            )

        if "Draft a single helpful reply for an Outline comment thread." in user_prompt:
            assert "Outline Agent Kickoff" in user_prompt
            assert "please summarize this" in user_prompt.lower()
            assert "Collection workspace context follows" in system_prompt
            assert "limited markdown rich text" in system_prompt
            assert "Do not use headings, markdown tables, fenced code blocks" in system_prompt
            assert (
                "Collection Name: Outline Agent Dev Sandbox" in system_prompt
                or "Collection Name: 107b2669-e0ad-4abd-a66e-28305124edc8" in system_prompt
            )
            return "Sure — here is a short summary."

        if "Return strict JSON only" in system_prompt:
            assert "Assistant reply:" in user_prompt
            return json.dumps(
                {
                    "should_write": True,
                    "reason": "Collection purpose was reinforced",
                    "entries": [
                        {
                            "section": "facts",
                            "text": "This collection is used for developing and testing the Outline comment agent.",
                        }
                    ],
                }
            )

        raise AssertionError("Unexpected model invocation")


class MultimodalReplyModelClient:
    def __init__(self) -> None:
        self.text_calls: list[tuple[str, str]] = []
        self.image_calls: list[tuple[str, str, int]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.text_calls.append((system_prompt, user_prompt))
        nonreply_response = _maybe_nonreply_planner_response(system_prompt)
        if nonreply_response is not None:
            return nonreply_response
        raise AssertionError("Expected generate_reply_with_images to be used for image comments")

    async def generate_reply_with_images(self, system_prompt: str, user_prompt: str, *, input_images) -> str:
        self.image_calls.append((system_prompt, user_prompt, len(input_images)))
        assert len(input_images) == 1
        assert input_images[0].media_type == "image/png"
        assert input_images[0].data.startswith(b"\x89PNG")
        assert "Current user comment also includes 1 embedded image." in user_prompt
        assert "你能看懂这张图吗" in user_prompt
        return "可以，我看到了这张图片。它看起来像一张测试图片。"


class SimpleModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        nonreply_response = _maybe_nonreply_planner_response(system_prompt)
        if nonreply_response is not None:
            return nonreply_response
        if "You maintain durable thread-local SESSION.md state" in system_prompt:
            return json.dumps(
                {
                    "should_write": False,
                    "summary": "",
                    "open_questions": [],
                    "working_notes": [],
                }
            )
        if "Return strict JSON only" in system_prompt:
            return json.dumps(
                {
                    "should_write": False,
                    "reason": "No durable memory update needed",
                    "entries": [],
                }
            )
        return "Acknowledged."


class MemoryActionModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        if "You manage explicit memory actions" in system_prompt:
            assert "Latest user comment:" in user_prompt
            return json.dumps(
                {
                    "reason": "The user explicitly asked to remember a constraint.",
                    "actions": [
                        {
                            "action": "add",
                            "section": "facts",
                            "target": None,
                            "text": "Keep responses under five bullets unless asked otherwise.",
                        }
                    ],
                }
            )
        if "Return strict JSON only" in system_prompt:
            return json.dumps(
                {
                    "should_write": False,
                    "reason": "No durable memory update needed",
                    "entries": [],
                }
            )
        return "Acknowledged."


class MemoryActionReplyModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        nonreply_response = _maybe_nonreply_planner_response(system_prompt)
        if nonreply_response is not None:
            return nonreply_response
        if "Draft a single helpful reply for an Outline comment thread." in user_prompt:
            assert "Memory action outcome:" in user_prompt
            assert "status: applied" in user_prompt
            assert "add[facts]: Keep responses under five bullets unless asked otherwise." in user_prompt
            return "Got it — I’ll remember that constraint for this collection."
        return "Acknowledged."


class RelatedDocModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        nonreply_response = _maybe_nonreply_planner_response(system_prompt)
        if nonreply_response is not None:
            return nonreply_response
        if "You maintain durable thread-local SESSION.md state" in system_prompt:
            return json.dumps(
                {
                    "should_write": False,
                    "summary": "",
                    "open_questions": [],
                    "working_notes": [],
                }
            )
        if "Return strict JSON only" in system_prompt:
            return json.dumps(
                {
                    "should_write": False,
                    "reason": "No durable memory update needed",
                    "entries": [],
                }
            )
        if "Draft a single helpful reply for an Outline comment thread." in user_prompt:
            assert "Related documents in this collection:" in user_prompt
            assert "Outline Agent Architecture" in user_prompt
            assert "doc-related" in user_prompt
            return "Got it."
        return "Acknowledged."


class ExplodingModelClient:
    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        raise RuntimeError("kaboom")


class DocumentEditDecisionModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        assert "directly update the current Outline document" in system_prompt
        assert "Latest user comment:" in user_prompt
        assert "Document outline (use section IDs exactly as listed):" in user_prompt
        return json.dumps(
            {
                "decision": "edit",
                "reason": "The user explicitly asked for a direct rewrite of the kickoff doc.",
                "title": "Outline Agent Kickoff",
                "operations": [
                    {
                        "op": "replace_section",
                        "target_section_id": "S1",
                        "new_markdown": (
                            "# Outline Agent Kickoff\n\n"
                            "This document introduces the Outline agent project in a more formal tone.\n\n"
                            "## Roadmap\n\n"
                            "- Validate webhook flow\n"
                            "- Add direct document editing\n"
                            "- Improve collection-scoped memory"
                        ),
                    }
                ],
                "summary": "I rewrote the kickoff document in a more formal tone and added a short roadmap.",
            }
        )


class LongDocumentSectionEditModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        assert "Document context mode: sectioned" in user_prompt
        assert "Document outline (use section IDs exactly as listed):" in user_prompt
        assert "S3 | level=2 |" in user_prompt
        assert "[S3] Outline Agent Design Spec > Roadmap" in user_prompt
        return json.dumps(
            {
                "decision": "edit",
                "reason": "The user explicitly asked to shorten the roadmap section.",
                "operations": [
                    {
                        "op": "replace_section",
                        "target_section_id": "S3",
                        "new_markdown": (
                            "## Roadmap\n\n"
                            "- Validate webhook flow\n"
                            "- Improve thread memory\n"
                            "- Add safe section-level document editing"
                        ),
                    }
                ],
                "summary": "I tightened the roadmap section and kept the rest of the document unchanged.",
            }
        )


class ImageDrivenDocumentEditModelClient:
    def __init__(self) -> None:
        self.text_calls: list[tuple[str, str]] = []
        self.image_calls: list[tuple[str, str, int]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.text_calls.append((system_prompt, user_prompt))
        raise AssertionError("Expected multimodal document update planning")

    async def generate_reply_with_images(self, system_prompt: str, user_prompt: str, *, input_images) -> str:
        self.image_calls.append((system_prompt, user_prompt, len(input_images)))
        assert "directly update the current Outline document" in system_prompt
        assert "The latest user comment also includes 1 embedded image." in user_prompt
        assert len(input_images) == 1
        assert input_images[0].media_type == "image/png"
        return json.dumps(
            {
                "decision": "edit",
                "reason": "The user asked to rewrite the document based on the image.",
                "title": "Outline Agent Kickoff",
                "operations": [
                    {
                        "op": "replace_document",
                        "new_markdown": (
                            "# Outline Agent Kickoff\n\n"
                            "This document now includes a short description derived from the attached image.\n\n"
                            "## Image Summary\n\n"
                            "- The image appears to be a simple test picture.\n"
                            "- It contains a clear central subject for visual verification."
                        ),
                    }
                ],
                "summary": "I rewrote the document and added an image-based summary section.",
            }
        )


class DocumentCreateDecisionModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        assert "create a new Outline document" in system_prompt
        assert "Current document title:" in user_prompt
        return json.dumps(
            {
                "decision": "create",
                "reason": "The user explicitly asked for a separate new document.",
                "title": "Attachment Summary Draft",
                "text": (
                    "# Attachment Summary Draft\n\n"
                    "This is a new standalone summary document.\n\n"
                    "## Summary\n\n"
                    "- Key point one\n"
                    "- Key point two"
                ),
                "summary": "I created a new summary document in this collection.",
            }
        )


class ReplyAfterDocumentEditModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        nonreply_response = _maybe_nonreply_planner_response(system_prompt)
        if nonreply_response is not None:
            return nonreply_response
        if "You maintain durable thread-local SESSION.md state" in system_prompt:
            return json.dumps(
                {
                    "should_write": False,
                    "summary": "",
                    "open_questions": [],
                    "working_notes": [],
                }
            )
        if "Return strict JSON only" in system_prompt:
            return json.dumps(
                {
                    "should_write": False,
                    "reason": "No durable memory update needed",
                    "entries": [],
                }
            )
        assert "Document update outcome:" in user_prompt
        assert "status: applied" in user_prompt
        assert "I rewrote the kickoff document in a more formal tone and added a short roadmap." in user_prompt
        assert "keep the comment reply very short" in user_prompt
        assert "do not paste the new document body" in user_prompt
        return "Done — I updated the document directly and added a short roadmap section."


class ReplyAfterImageDrivenDocumentEditModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        nonreply_response = _maybe_nonreply_planner_response(system_prompt)
        if nonreply_response is not None:
            return nonreply_response
        if "You maintain durable thread-local SESSION.md state" in system_prompt:
            return json.dumps(
                {
                    "should_write": False,
                    "summary": "",
                    "open_questions": [],
                    "working_notes": [],
                }
            )
        if "Return strict JSON only" in system_prompt:
            return json.dumps(
                {
                    "should_write": False,
                    "reason": "No durable memory update needed",
                    "entries": [],
                }
            )
        assert "Document update outcome:" in user_prompt
        assert "I rewrote the document and added an image-based summary section." in user_prompt
        return "好了，我已经把图里的内容整理进文档。"


class ReplyAfterDocumentCreateModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        nonreply_response = _maybe_nonreply_planner_response(system_prompt)
        if nonreply_response is not None:
            return nonreply_response
        if "You maintain durable thread-local SESSION.md state" in system_prompt:
            return json.dumps(
                {
                    "should_write": False,
                    "summary": "",
                    "open_questions": [],
                    "working_notes": [],
                }
            )
        if "Return strict JSON only" in system_prompt:
            return json.dumps(
                {
                    "should_write": False,
                    "reason": "No durable memory update needed",
                    "entries": [],
                }
            )
        assert "Document creation outcome:" in user_prompt
        assert "I created a new summary document in this collection." in user_prompt
        assert "created document id: created-doc-1" in user_prompt
        return "好了，我已经新建了一篇总结文档。"


class ReplyAfterLongDocumentEditModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        nonreply_response = _maybe_nonreply_planner_response(system_prompt)
        if nonreply_response is not None:
            return nonreply_response
        if "You maintain durable thread-local SESSION.md state" in system_prompt:
            return json.dumps(
                {
                    "should_write": False,
                    "summary": "",
                    "open_questions": [],
                    "working_notes": [],
                }
            )
        if "Return strict JSON only" in system_prompt:
            return json.dumps(
                {
                    "should_write": False,
                    "reason": "No durable memory update needed",
                    "entries": [],
                }
            )
        assert "Outline Agent Design Spec" in user_prompt
        assert "Add safe section-level document editing" in user_prompt
        assert "replace_section[S3:Outline Agent Design Spec > Roadmap]" in user_prompt
        assert "keep the comment reply very short" in user_prompt
        return "Done — I tightened the roadmap section and left the rest of the document unchanged."


class FollowupDocumentEditDecisionModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        assert "directly update the current Outline document" in system_prompt
        assert "Latest user comment:" in user_prompt
        assert "yes, a diagram-ready outline document please." in user_prompt
        assert "turn this into" in user_prompt.lower()
        return json.dumps(
            {
                "decision": "edit",
                "reason": "The user approved turning the previous draft into a document-ready outline.",
                "operations": [
                    {
                        "op": "append_document",
                        "target_section_id": None,
                        "new_markdown": (
                            "## LLM MoE Structure\n\n"
                            "### Diagram-ready outline\n\n"
                            "```mermaid\n"
                            "flowchart TD\n"
                            "    A[Input tokens] --> B[Router]\n"
                            "    B --> C[Top-k expert selection]\n"
                            "    C --> D[Expert FFNs]\n"
                            "    D --> E[Combine outputs]\n"
                            "```\n\n"
                            "- Router scores each token against experts.\n"
                            "- Top-k routing sends each token to a small subset of experts.\n"
                            "- Expert outputs are merged back into the main residual stream."
                        ),
                    }
                ],
                "summary": "I added a diagram-ready LLM MoE outline to the document.",
            }
        )


class ReplyAfterFollowupDocumentEditModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        nonreply_response = _maybe_nonreply_planner_response(system_prompt)
        if nonreply_response is not None:
            return nonreply_response
        if "You maintain durable thread-local SESSION.md state" in system_prompt:
            return json.dumps(
                {
                    "should_write": False,
                    "summary": "",
                    "open_questions": [],
                    "working_notes": [],
                }
            )
        if "Return strict JSON only" in system_prompt:
            return json.dumps(
                {
                    "should_write": False,
                    "reason": "No durable memory update needed",
                    "entries": [],
                }
            )
        assert "Document update outcome:" in user_prompt
        assert "I added a diagram-ready LLM MoE outline to the document." in user_prompt
        assert "flowchart TD" in user_prompt
        assert "do not paste the new document body" in user_prompt
        return "Done — I wrote the diagram-ready outline into the document."


class MissingBodyReplaceDocumentDecisionModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        assert "Document context mode: unavailable" in user_prompt
        assert "Document markdown is empty or unavailable." in user_prompt
        return json.dumps(
            {
                "decision": "edit",
                "reason": "The user explicitly asked for a standalone Outline document on LLM MoE.",
                "operations": [
                    {
                        "op": "replace_document",
                        "target_section_id": None,
                        "new_markdown": (
                            "# LLM MoE\n\n"
                            "## What it is\n\n"
                            "Mixture of Experts routes each token to a small subset of experts.\n\n"
                            "## Core structure\n\n"
                            "- Token embedding / hidden states\n"
                            "- Router or gating network\n"
                            "- Top-k expert selection\n"
                            "- Sparse expert FFNs\n"
                            "- Output merge and residual path\n"
                        ),
                    }
                ],
                "summary": "I wrote a standalone LLM MoE outline into the document.",
            }
        )


class ReplyAfterMissingBodyReplaceDocumentModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        nonreply_response = _maybe_nonreply_planner_response(system_prompt)
        if nonreply_response is not None:
            return nonreply_response
        if "You maintain durable thread-local SESSION.md state" in system_prompt:
            return json.dumps(
                {
                    "should_write": False,
                    "summary": "",
                    "open_questions": [],
                    "working_notes": [],
                }
            )
        if "Return strict JSON only" in system_prompt:
            return json.dumps(
                {
                    "should_write": False,
                    "reason": "No durable memory update needed",
                    "entries": [],
                }
            )
        assert "Document update outcome:" in user_prompt
        assert "I wrote a standalone LLM MoE outline into the document." in user_prompt
        assert "keep the comment reply very short" in user_prompt
        return "Done — I replaced the document with a standalone LLM MoE outline."


class DiagramDocumentEditDecisionModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        assert "can you draw diagram in the document" in user_prompt.lower()
        return json.dumps(
            {
                "decision": "edit",
                "reason": "The user asked for a diagram to be added to the current document.",
                "operations": [
                    {
                        "op": "append_document",
                        "target_section_id": None,
                        "new_markdown": (
                            "## MoE Routing Diagram\n\n"
                            "```mermaid\n"
                            "flowchart TD\n"
                            "    A[Token hidden states] --> B[Router / gating network]\n"
                            "    B --> C[Top-k selection]\n"
                            "    C --> D[Selected experts]\n"
                            "    D --> E[Weighted combine]\n"
                            "    E --> F[MoE layer output]\n"
                            "```\n"
                        ),
                    }
                ],
                "summary": "I added a Mermaid MoE routing diagram to the document.",
            }
        )


class ReplyAfterDiagramDocumentEditModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        nonreply_response = _maybe_nonreply_planner_response(system_prompt)
        if nonreply_response is not None:
            return nonreply_response
        if "You maintain durable thread-local SESSION.md state" in system_prompt:
            return json.dumps(
                {
                    "should_write": False,
                    "summary": "",
                    "open_questions": [],
                    "working_notes": [],
                }
            )
        if "Return strict JSON only" in system_prompt:
            return json.dumps(
                {
                    "should_write": False,
                    "reason": "No durable memory update needed",
                    "entries": [],
                }
            )
        assert "I added a Mermaid MoE routing diagram to the document." in user_prompt
        assert "flowchart TD" in user_prompt
        assert "do not paste the new document body" in user_prompt
        return "Done — I added the diagram directly into the document."


class ThreadStateAwareModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        nonreply_response = _maybe_nonreply_planner_response(system_prompt)
        if nonreply_response is not None:
            return nonreply_response
        if "You maintain durable thread-local SESSION.md state" in system_prompt:
            reply_call_count = sum(
                1 for _, prompt in self.calls if "Draft a single helpful reply for an Outline comment thread." in prompt
            )
            if reply_call_count == 1:
                return json.dumps(
                    {
                        "should_write": True,
                        "summary": "The user asked for a concise summary of the kickoff document.",
                        "open_questions": ["Whether they want more detail afterward."],
                        "working_notes": ["Start with a concise summary."],
                    }
                )
            return json.dumps(
                {
                    "should_write": True,
                    "summary": "The thread started with a summary request and then asked for more detail.",
                    "open_questions": ["What level of expansion the user wants next."],
                    "working_notes": ["Build on the earlier summary instead of restarting."],
                }
            )

        if "Return strict JSON only" in system_prompt:
            return json.dumps(
                {
                    "should_write": False,
                    "reason": "No durable memory update needed",
                    "entries": [],
                }
            )

        reply_call_count = sum(
            1 for _, prompt in self.calls if "Draft a single helpful reply for an Outline comment thread." in prompt
        )
        if reply_call_count == 1:
            assert "Persisted thread session context:" in user_prompt
            assert "interaction_count: 0" in user_prompt
            return "First reply."

        assert "please summarize this" in user_prompt.lower()
        assert "First reply." in user_prompt
        assert "The user asked for a concise summary of the kickoff document." in user_prompt
        assert "interaction_count: 1" in user_prompt
        return "Second reply."


def _load_fixture() -> WebhookEnvelope:
    fixture_path = Path(__file__).parent / "fixtures" / "comments.create.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    payload["actorId"] = "fafb5aee-1f7c-4bca-a524-eb99afa30ed0"
    payload["payload"]["model"]["createdById"] = "fafb5aee-1f7c-4bca-a524-eb99afa30ed0"
    payload["payload"]["model"]["createdBy"] = {
        "id": "fafb5aee-1f7c-4bca-a524-eb99afa30ed0",
        "name": "Gavin Gong",
    }
    payload["payload"]["model"]["data"] = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "mention",
                        "attrs": {
                            "id": "some-node-id",
                            "type": "user",
                            "label": AGENT_USER_LABEL,
                            "actorId": "fafb5aee-1f7c-4bca-a524-eb99afa30ed0",
                            "modelId": AGENT_USER_ID,
                        },
                    },
                    {"type": "text", "text": " please summarize this"},
                ],
            }
        ],
    }
    return WebhookEnvelope.model_validate(payload)


def test_comment_processor_replies_to_real_user_mention_and_updates_reactions(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        outline_agent_user_id=AGENT_USER_ID,
        mention_aliases=["@agent"],
        trigger_mode="mention",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=False,
    )
    outline_client = DummyOutlineClient()
    model_client = DummyModelClient()
    processor = CommentProcessor(
        settings=settings,
        store=ProcessedEventStore(tmp_path / "processed.json"),
        outline_client=outline_client,
        model_client=model_client,
    )

    result = asyncio.run(processor.handle(_load_fixture()))

    assert result.action == "replied"
    assert result.triggered_alias == AGENT_USER_LABEL
    assert result.collection_workspace is not None
    assert result.thread_workspace is not None
    assert result.memory_update_preview is not None
    assert result.thread_session_update_preview is not None
    assert Path(result.collection_workspace).exists()
    memory_path = Path(result.collection_workspace) / "memory" / "MEMORY.md"
    session_path = Path(result.thread_workspace) / "SESSION.md"
    assert memory_path.exists()
    assert session_path.exists()
    memory_text = memory_path.read_text(encoding="utf-8")
    session_text = session_path.read_text(encoding="utf-8")
    assert "This collection is used for developing and testing the Outline comment agent." in memory_text
    assert "The user asked for a concise summary of the kickoff document." in session_text
    assert "Whether the user wants a deeper breakdown next." in session_text
    assert outline_client.posted[0]["text"] == "Thinking…"
    assert outline_client.posted[0]["parent_comment_id"] == "cad435c3-1cb9-4dd5-9254-d355b02fd795"
    assert outline_client.updated_comments == [
        {"comment_id": "comment-1", "text": "Sure — here is a short summary."}
    ]
    assert outline_client.reactions == [
        ("add", "cad435c3-1cb9-4dd5-9254-d355b02fd795", "👀"),
        ("remove", "cad435c3-1cb9-4dd5-9254-d355b02fd795", "👀"),
        ("add", "cad435c3-1cb9-4dd5-9254-d355b02fd795", "👍"),
    ]
    assert len(model_client.calls) == 4


def test_comment_processor_passes_embedded_comment_images_to_multimodal_reply_model(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        outline_agent_user_id=AGENT_USER_ID,
        mention_aliases=["@agent"],
        trigger_mode="mention",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=False,
    )
    outline_client = DummyOutlineClient()
    reply_model_client = MultimodalReplyModelClient()
    no_op_model_client = SimpleModelClient()
    processor = CommentProcessor(
        settings=settings,
        store=ProcessedEventStore(tmp_path / "processed.json"),
        outline_client=outline_client,
        model_client=reply_model_client,
        memory_model_client=no_op_model_client,
        thread_session_model_client=no_op_model_client,
        action_router_model_client=ActionRouterModelClient(),
    )

    payload = _load_fixture().model_dump()
    payload["payload"]["model"]["data"] = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "mention",
                        "attrs": {
                            "id": "some-node-id",
                            "type": "user",
                            "label": AGENT_USER_LABEL,
                            "actorId": "fafb5aee-1f7c-4bca-a524-eb99afa30ed0",
                            "modelId": AGENT_USER_ID,
                        },
                    },
                    {"type": "text", "text": " 你能看懂这张图吗"},
                    {
                        "type": "image",
                        "attrs": {
                            "src": "/api/attachments.redirect?id=image-123",
                            "alt": None,
                        },
                    },
                ],
            }
        ],
    }

    result = asyncio.run(processor.handle(WebhookEnvelope.model_validate(payload)))

    assert result.action == "replied"
    assert reply_model_client.image_calls
    assert not reply_model_client.text_calls
    assert outline_client.downloaded_attachments == [
        {
            "url_or_path": "/api/attachments.redirect?id=image-123",
            "file_path": str(
                Path(result.thread_workspace) / "work" / "comment_images" / "cad435c3-1cb9-4dd5-9254-d355b02fd795-1.img"
            ),
        }
    ]
    assert outline_client.updated_comments == [
        {"comment_id": "comment-1", "text": "可以，我看到了这张图片。它看起来像一张测试图片。"}
    ]


def test_comment_processor_sniffs_png_image_when_download_reports_text_html(tmp_path: Path) -> None:
    class HtmlTypedImageOutlineClient(DummyOutlineClient):
        async def download_attachment(self, url_or_path: str, file_path: Path) -> dict:
            self.downloaded_attachments.append({"url_or_path": url_or_path, "file_path": str(file_path)})
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(b"\x89PNG\r\n\x1a\nfake-image")
            return {
                "ok": True,
                "url": url_or_path,
                "file_path": str(file_path),
                "size": len(b"\x89PNG\r\n\x1a\nfake-image"),
                "content_type": "text/html; charset=utf-8",
            }

    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        outline_agent_user_id=AGENT_USER_ID,
        mention_aliases=["@agent"],
        trigger_mode="mention",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=False,
    )
    outline_client = HtmlTypedImageOutlineClient()
    reply_model_client = MultimodalReplyModelClient()
    no_op_model_client = SimpleModelClient()
    processor = CommentProcessor(
        settings=settings,
        store=ProcessedEventStore(tmp_path / "processed.json"),
        outline_client=outline_client,
        model_client=reply_model_client,
        memory_model_client=no_op_model_client,
        thread_session_model_client=no_op_model_client,
        action_router_model_client=ActionRouterModelClient(),
    )

    payload = _load_fixture().model_dump()
    payload["payload"]["model"]["data"] = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "mention",
                        "attrs": {
                            "id": "some-node-id",
                            "type": "user",
                            "label": AGENT_USER_LABEL,
                            "actorId": "fafb5aee-1f7c-4bca-a524-eb99afa30ed0",
                            "modelId": AGENT_USER_ID,
                        },
                    },
                    {"type": "text", "text": " 你能看懂这张图吗"},
                    {"type": "image", "attrs": {"src": "/api/attachments.redirect?id=image-123", "alt": None}},
                ],
            }
        ],
    }

    result = asyncio.run(processor.handle(WebhookEnvelope.model_validate(payload)))

    assert result.action == "replied"
    assert reply_model_client.image_calls


def test_comment_processor_applies_explicit_memory_actions(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        outline_agent_user_id=AGENT_USER_ID,
        mention_aliases=["@agent"],
        trigger_mode="mention",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=False,
    )
    outline_client = DummyOutlineClient()
    reply_model_client = MemoryActionReplyModelClient()
    memory_model_client = MemoryActionModelClient()
    no_op_model_client = SimpleModelClient()
    processor = CommentProcessor(
        settings=settings,
        store=ProcessedEventStore(tmp_path / "processed.json"),
        outline_client=outline_client,
        model_client=reply_model_client,
        memory_model_client=memory_model_client,
        thread_session_model_client=no_op_model_client,
        action_router_model_client=ActionRouterModelClient(),
    )

    payload = _load_fixture().model_dump()
    payload["payload"]["model"]["data"] = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "mention",
                        "attrs": {
                            "id": "some-node-id",
                            "type": "user",
                            "label": AGENT_USER_LABEL,
                            "actorId": "fafb5aee-1f7c-4bca-a524-eb99afa30ed0",
                            "modelId": AGENT_USER_ID,
                        },
                    },
                    {
                        "type": "text",
                        "text": " please remember that replies should stay under five bullets.",
                    },
                ],
            }
        ],
    }

    result = asyncio.run(processor.handle(WebhookEnvelope.model_validate(payload)))

    assert result.action == "replied"
    assert result.memory_action_preview == "add[facts]: Keep responses under five bullets unless asked otherwise."
    assert result.memory_update_preview is None

    assert result.collection_workspace is not None
    memory_path = Path(result.collection_workspace) / "memory" / "MEMORY.md"
    assert memory_path.exists()
    memory_text = memory_path.read_text(encoding="utf-8")
    assert "Keep responses under five bullets unless asked otherwise." in memory_text

    index_path = Path(result.collection_workspace) / "memory" / "index.json"
    assert index_path.exists()
    index_payload = json.loads(index_path.read_text(encoding="utf-8"))
    assert {
        "section": "facts",
        "text": "Keep responses under five bullets unless asked otherwise.",
    } in index_payload.get("items", [])


def test_comment_processor_injects_related_documents_context(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        outline_agent_user_id=AGENT_USER_ID,
        mention_aliases=["@agent"],
        trigger_mode="mention",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=True,
    )
    outline_client = DummyOutlineClient()
    outline_client.search_results = [
        OutlineDocument(
            id="doc-related",
            title="Outline Agent Architecture",
            collection_id="107b2669-e0ad-4abd-a66e-28305124edc8",
            url="/doc/outline-agent-architecture-123",
            text=None,
        )
    ]
    outline_client.extra_documents["doc-related"] = OutlineDocument(
        id="doc-related",
        title="Outline Agent Architecture",
        collection_id="107b2669-e0ad-4abd-a66e-28305124edc8",
        url="/doc/outline-agent-architecture-123",
        text="This document describes the outline agent architecture and its core components.",
    )
    model_client = RelatedDocModelClient()
    no_op_model_client = SimpleModelClient()
    processor = CommentProcessor(
        settings=settings,
        store=ProcessedEventStore(tmp_path / "processed.json"),
        outline_client=outline_client,
        model_client=model_client,
        memory_model_client=no_op_model_client,
        thread_session_model_client=no_op_model_client,
    )

    result = asyncio.run(processor.handle(_load_fixture()))

    assert result.action == "dry-run"


def test_comment_processor_can_update_document_directly_before_replying(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        outline_agent_user_id=AGENT_USER_ID,
        trigger_mode="mention",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=False,
        document_update_enabled=True,
    )
    outline_client = DummyOutlineClient()
    reply_model_client = ReplyAfterDocumentEditModelClient()
    document_update_model_client = DocumentEditDecisionModelClient()
    no_op_model_client = SimpleModelClient()
    processor = CommentProcessor(
        settings=settings,
        store=ProcessedEventStore(tmp_path / "processed.json"),
        outline_client=outline_client,
        model_client=reply_model_client,
        memory_model_client=no_op_model_client,
        thread_session_model_client=no_op_model_client,
        document_update_model_client=document_update_model_client,
        action_router_model_client=ActionRouterModelClient(),
    )

    payload = _load_fixture().model_dump()
    payload["payload"]["model"]["data"] = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "mention",
                        "attrs": {
                            "id": "some-node-id",
                            "type": "user",
                            "label": AGENT_USER_LABEL,
                            "actorId": "fafb5aee-1f7c-4bca-a524-eb99afa30ed0",
                            "modelId": AGENT_USER_ID,
                        },
                    },
                    {
                        "type": "text",
                        "text": " please rewrite the kickoff document in a more formal tone and add a short roadmap",
                    },
                ],
            }
        ],
    }

    result = asyncio.run(processor.handle(WebhookEnvelope.model_validate(payload)))

    assert result.action == "edited-and-replied"
    assert result.reason == "document-updated-and-replied"
    assert result.document_update_preview is not None
    assert len(outline_client.updated_documents) == 1
    assert outline_client.updated_documents[0]["document_id"] == result.document_id
    assert outline_client.updated_documents[0]["title"] is None
    assert "## Roadmap" in str(outline_client.updated_documents[0]["text"])
    assert outline_client.posted[0]["text"] == "Thinking…"
    assert outline_client.updated_comments == [
        {
            "comment_id": "comment-1",
            "text": "Done — I updated the document directly and added a short roadmap section.",
        }
    ]


def test_comment_processor_can_drive_document_edit_from_current_comment_image(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        outline_agent_user_id=AGENT_USER_ID,
        mention_aliases=["@agent"],
        trigger_mode="mention",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=False,
        document_update_enabled=True,
    )
    outline_client = DummyOutlineClient()
    reply_model_client = ReplyAfterImageDrivenDocumentEditModelClient()
    document_update_model_client = ImageDrivenDocumentEditModelClient()
    no_op_model_client = SimpleModelClient()
    processor = CommentProcessor(
        settings=settings,
        store=ProcessedEventStore(tmp_path / "processed.json"),
        outline_client=outline_client,
        model_client=reply_model_client,
        memory_model_client=no_op_model_client,
        thread_session_model_client=no_op_model_client,
        document_update_model_client=document_update_model_client,
        action_router_model_client=ActionRouterModelClient(),
    )

    payload = _load_fixture().model_dump()
    payload["payload"]["model"]["data"] = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "mention",
                        "attrs": {
                            "id": "some-node-id",
                            "type": "user",
                            "label": AGENT_USER_LABEL,
                            "actorId": "fafb5aee-1f7c-4bca-a524-eb99afa30ed0",
                            "modelId": AGENT_USER_ID,
                        },
                    },
                    {"type": "text", "text": " please rewrite the kickoff document based on this image"},
                    {"type": "image", "attrs": {"src": "/api/attachments.redirect?id=image-doc-1", "alt": None}},
                ],
            }
        ],
    }

    result = asyncio.run(processor.handle(WebhookEnvelope.model_validate(payload)))

    assert result.action == "edited-and-replied"
    assert document_update_model_client.image_calls
    assert len(outline_client.updated_documents) == 1
    assert "## Image Summary" in str(outline_client.updated_documents[0]["text"])
    assert outline_client.updated_comments == [
        {"comment_id": "comment-1", "text": "好了，我已经把图里的内容整理进文档。"}
    ]


def test_comment_processor_can_create_new_document(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        outline_agent_user_id=AGENT_USER_ID,
        mention_aliases=["@agent"],
        trigger_mode="mention",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=False,
        document_update_enabled=True,
    )
    outline_client = DummyOutlineClient()
    reply_model_client = ReplyAfterDocumentCreateModelClient()
    document_create_model_client = DocumentCreateDecisionModelClient()
    no_op_model_client = SimpleModelClient()
    processor = CommentProcessor(
        settings=settings,
        store=ProcessedEventStore(tmp_path / "processed.json"),
        outline_client=outline_client,
        model_client=reply_model_client,
        memory_model_client=no_op_model_client,
        thread_session_model_client=no_op_model_client,
        document_update_model_client=document_create_model_client,
        action_router_model_client=ActionRouterModelClient(),
    )

    payload = _load_fixture().model_dump()
    payload["payload"]["model"]["data"] = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "mention",
                        "attrs": {
                            "id": "some-node-id",
                            "type": "user",
                            "label": AGENT_USER_LABEL,
                            "actorId": "fafb5aee-1f7c-4bca-a524-eb99afa30ed0",
                            "modelId": AGENT_USER_ID,
                        },
                    },
                    {"type": "text", "text": " please create a new document that summarizes this work"},
                ],
            }
        ],
    }

    result = asyncio.run(processor.handle(WebhookEnvelope.model_validate(payload)))

    assert result.action == "created-document-and-replied"
    assert result.reason == "document-created-and-replied"
    assert result.document_creation_preview is not None
    assert "Attachment Summary Draft" in result.document_creation_preview
    assert outline_client.created_documents == [
        {
            "title": "Attachment Summary Draft",
            "text": "# Attachment Summary Draft\n\nThis is a new standalone summary document.\n\n## Summary\n\n- Key point one\n- Key point two",
            "collection_id": "107b2669-e0ad-4abd-a66e-28305124edc8",
            "parent_document_id": None,
            "publish": True,
        }
    ]
    assert outline_client.updated_comments == [
        {"comment_id": "comment-1", "text": "好了，我已经新建了一篇总结文档。"}
    ]


def test_comment_processor_uses_section_level_editing_for_long_documents(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        outline_agent_user_id=AGENT_USER_ID,
        trigger_mode="mention",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=False,
        document_update_enabled=True,
        max_document_update_chars=160,
    )
    outline_client = LongDocumentOutlineClient()
    reply_model_client = ReplyAfterLongDocumentEditModelClient()
    document_update_model_client = LongDocumentSectionEditModelClient()
    no_op_model_client = SimpleModelClient()
    processor = CommentProcessor(
        settings=settings,
        store=ProcessedEventStore(tmp_path / "processed.json"),
        outline_client=outline_client,
        model_client=reply_model_client,
        memory_model_client=no_op_model_client,
        thread_session_model_client=no_op_model_client,
        document_update_model_client=document_update_model_client,
        action_router_model_client=ActionRouterModelClient(),
    )

    payload = _load_fixture().model_dump()
    payload["payload"]["model"]["data"] = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "mention",
                        "attrs": {
                            "id": "some-node-id",
                            "type": "user",
                            "label": AGENT_USER_LABEL,
                            "actorId": "fafb5aee-1f7c-4bca-a524-eb99afa30ed0",
                            "modelId": AGENT_USER_ID,
                        },
                    },
                    {
                        "type": "text",
                        "text": " please shorten the roadmap section and keep the rest of the document unchanged",
                    },
                ],
            }
        ],
    }

    result = asyncio.run(processor.handle(WebhookEnvelope.model_validate(payload)))

    assert result.action == "edited-and-replied"
    assert result.document_update_preview is not None
    assert "replace_section[S3:Outline Agent Design Spec > Roadmap]" in result.document_update_preview
    assert len(outline_client.updated_documents) == 1
    updated_text = str(outline_client.updated_documents[0]["text"])
    assert "## Overview" in updated_text
    assert "## Risks" in updated_text
    assert "Add safe section-level document editing" in updated_text
    assert "Long documents require scoped editing" in updated_text
    assert "Improve thread memory handling" not in updated_text


def test_comment_processor_can_apply_document_edit_from_followup_confirmation(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        outline_agent_user_id=AGENT_USER_ID,
        trigger_mode="mention",
        trigger_on_reply_to_agent=True,
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=False,
        document_update_enabled=True,
    )
    outline_client = DummyOutlineClient()
    outline_client.comment_items = [
        OutlineComment(
            id="root-user-comment",
            document_id="cad435c3-1cb9-4dd5-9254-d355b02fd795",
            parent_comment_id=None,
            created_by_id="user-1",
            created_by_name="Gavin Gong",
            created_at="2026-03-09T04:48:00.000Z",
            data={
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": "write something about llm moe structure in this document?",
                            }
                        ],
                    }
                ],
            },
        ),
        OutlineComment(
            id="agent-sibling-reply",
            document_id="cad435c3-1cb9-4dd5-9254-d355b02fd795",
            parent_comment_id="root-user-comment",
            created_by_id=AGENT_USER_ID,
            created_by_name=AGENT_USER_LABEL,
            created_at="2026-03-09T04:49:00.000Z",
            data={
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": "If you want, I can also turn this into a diagram-ready outline for the document.",
                            }
                        ],
                    }
                ],
            },
        ),
    ]
    reply_model_client = ReplyAfterFollowupDocumentEditModelClient()
    document_update_model_client = FollowupDocumentEditDecisionModelClient()
    no_op_model_client = SimpleModelClient()
    processor = CommentProcessor(
        settings=settings,
        store=ProcessedEventStore(tmp_path / "processed.json"),
        outline_client=outline_client,
        model_client=reply_model_client,
        memory_model_client=no_op_model_client,
        thread_session_model_client=no_op_model_client,
        document_update_model_client=document_update_model_client,
        action_router_model_client=ActionRouterModelClient(),
    )

    envelope = _load_fixture()
    payload = envelope.model_dump()
    payload["payload"]["model"]["id"] = "follow-up-comment"
    payload["payload"]["model"]["parentCommentId"] = "root-user-comment"
    payload["payload"]["model"]["data"] = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": "yes, a diagram-ready outline document please.",
                    }
                ],
            }
        ],
    }

    result = asyncio.run(processor.handle(WebhookEnvelope.model_validate(payload)))

    assert result.action == "edited-and-replied"
    assert result.reason == "document-updated-and-replied"
    assert result.triggered_alias == "reply-to-agent"
    assert len(outline_client.updated_documents) == 1
    updated_text = str(outline_client.updated_documents[0]["text"])
    assert "## LLM MoE Structure" in updated_text
    assert "flowchart TD" in updated_text
    assert outline_client.updated_comments == [
        {
            "comment_id": "comment-1",
            "text": "Done — I wrote the diagram-ready outline into the document.",
        }
    ]


def test_comment_processor_can_replace_document_when_current_body_is_unavailable(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        outline_agent_user_id=AGENT_USER_ID,
        trigger_mode="mention",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=False,
        document_update_enabled=True,
    )
    outline_client = MissingBodyOutlineClient()
    reply_model_client = ReplyAfterMissingBodyReplaceDocumentModelClient()
    document_update_model_client = MissingBodyReplaceDocumentDecisionModelClient()
    no_op_model_client = SimpleModelClient()
    processor = CommentProcessor(
        settings=settings,
        store=ProcessedEventStore(tmp_path / "processed.json"),
        outline_client=outline_client,
        model_client=reply_model_client,
        memory_model_client=no_op_model_client,
        thread_session_model_client=no_op_model_client,
        document_update_model_client=document_update_model_client,
        action_router_model_client=ActionRouterModelClient(),
    )

    payload = _load_fixture().model_dump()
    payload["payload"]["model"]["data"] = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "mention",
                        "attrs": {
                            "id": "some-node-id",
                            "type": "user",
                            "label": AGENT_USER_LABEL,
                            "actorId": "fafb5aee-1f7c-4bca-a524-eb99afa30ed0",
                            "modelId": AGENT_USER_ID,
                        },
                    },
                    {
                        "type": "text",
                        "text": " 写一篇关于 LLM MOE 的 outline 文档到这个文档里。",
                    },
                ],
            }
        ],
    }

    result = asyncio.run(processor.handle(WebhookEnvelope.model_validate(payload)))

    assert result.action == "edited-and-replied"
    assert result.reason == "document-updated-and-replied"
    assert len(outline_client.updated_documents) == 1
    assert outline_client.updated_documents[0]["text"] is not None
    assert "# LLM MoE" in str(outline_client.updated_documents[0]["text"])
    assert "Top-k expert selection" in str(outline_client.updated_documents[0]["text"])
    assert outline_client.updated_comments == [
        {
            "comment_id": "comment-1",
            "text": "Done — I replaced the document with a standalone LLM MoE outline.",
        }
    ]


def test_comment_processor_can_add_diagram_to_document_without_heuristic_keyword_match(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        outline_agent_user_id=AGENT_USER_ID,
        trigger_mode="mention",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=False,
        document_update_enabled=True,
    )
    outline_client = DummyOutlineClient()
    reply_model_client = ReplyAfterDiagramDocumentEditModelClient()
    document_update_model_client = DiagramDocumentEditDecisionModelClient()
    no_op_model_client = SimpleModelClient()
    processor = CommentProcessor(
        settings=settings,
        store=ProcessedEventStore(tmp_path / "processed.json"),
        outline_client=outline_client,
        model_client=reply_model_client,
        memory_model_client=no_op_model_client,
        thread_session_model_client=no_op_model_client,
        document_update_model_client=document_update_model_client,
        action_router_model_client=ActionRouterModelClient(),
    )

    payload = _load_fixture().model_dump()
    payload["payload"]["model"]["data"] = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "mention",
                        "attrs": {
                            "id": "some-node-id",
                            "type": "user",
                            "label": AGENT_USER_LABEL,
                            "actorId": "fafb5aee-1f7c-4bca-a524-eb99afa30ed0",
                            "modelId": AGENT_USER_ID,
                        },
                    },
                    {
                        "type": "text",
                        "text": " can you draw diagram in the document",
                    },
                ],
            }
        ],
    }

    result = asyncio.run(processor.handle(WebhookEnvelope.model_validate(payload)))

    assert result.action == "edited-and-replied"
    assert result.reason == "document-updated-and-replied"
    assert len(outline_client.updated_documents) == 1
    assert "## MoE Routing Diagram" in str(outline_client.updated_documents[0]["text"])
    assert "flowchart TD" in str(outline_client.updated_documents[0]["text"])
    assert outline_client.updated_comments == [
        {
            "comment_id": "comment-1",
            "text": "Done — I added the diagram directly into the document.",
        }
    ]


def test_comment_processor_replies_to_thread_root_for_follow_up_comment(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        outline_agent_user_id=AGENT_USER_ID,
        trigger_mode="mention",
        trigger_on_reply_to_agent=True,
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=False,
    )
    outline_client = DummyOutlineClient()
    outline_client.comment_items = [
        OutlineComment(
            id="root-user-comment",
            document_id="cad435c3-1cb9-4dd5-9254-d355b02fd795",
            parent_comment_id=None,
            created_by_id="user-1",
            created_by_name="Gavin Gong",
            created_at="2026-03-09T04:48:00.000Z",
            data={
                "type": "doc",
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Original prompt"}]}],
            },
        ),
        OutlineComment(
            id="agent-sibling-reply",
            document_id="cad435c3-1cb9-4dd5-9254-d355b02fd795",
            parent_comment_id="root-user-comment",
            created_by_id=AGENT_USER_ID,
            created_by_name=AGENT_USER_LABEL,
            created_at="2026-03-09T04:49:00.000Z",
            data={
                "type": "doc",
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Agent reply"}]}],
            },
        ),
    ]
    model_client = SimpleModelClient()
    processor = CommentProcessor(
        settings=settings,
        store=ProcessedEventStore(tmp_path / "processed.json"),
        outline_client=outline_client,
        model_client=model_client,
    )

    envelope = _load_fixture()
    payload = envelope.model_dump()
    payload["payload"]["model"]["parentCommentId"] = "root-user-comment"
    payload["payload"]["model"]["data"] = {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "What else can you do?"}]}],
    }
    payload["payload"]["model"]["id"] = "follow-up-comment"

    result = asyncio.run(processor.handle(WebhookEnvelope.model_validate(payload)))

    assert result.action == "replied"
    assert outline_client.posted[0]["parent_comment_id"] == "root-user-comment"


def test_comment_processor_tolerates_collection_info_failure(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        outline_agent_user_id=AGENT_USER_ID,
        mention_aliases=["@agent"],
        trigger_mode="mention",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=True,
    )
    outline_client = FailingCollectionInfoOutlineClient()
    model_client = DummyModelClient()
    processor = CommentProcessor(
        settings=settings,
        store=ProcessedEventStore(tmp_path / "processed.json"),
        outline_client=outline_client,
        model_client=model_client,
    )

    result = asyncio.run(processor.handle(_load_fixture()))

    assert result.action == "dry-run"
    assert result.reason == "reply-generated-without-posting"
    assert result.collection_id == "107b2669-e0ad-4abd-a66e-28305124edc8"


def test_comment_processor_persists_thread_session_context_across_turns(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        outline_agent_user_id=AGENT_USER_ID,
        trigger_mode="mention",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=True,
    )
    outline_client = DummyOutlineClient()
    model_client = ThreadStateAwareModelClient()
    processor = CommentProcessor(
        settings=settings,
        store=ProcessedEventStore(tmp_path / "processed.json"),
        outline_client=outline_client,
        model_client=model_client,
    )

    first_result = asyncio.run(processor.handle(_load_fixture()))
    assert first_result.action == "dry-run"
    assert first_result.thread_workspace is not None

    second_payload = _load_fixture().model_dump()
    second_payload["id"] = "webhook-2"
    second_payload["payload"]["id"] = "comment-event-2"
    second_payload["payload"]["model"]["id"] = "comment-2"
    second_payload["payload"]["model"]["parentCommentId"] = "cad435c3-1cb9-4dd5-9254-d355b02fd795"
    second_payload["payload"]["model"]["data"] = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "mention",
                        "attrs": {
                            "id": "some-node-id-2",
                            "type": "user",
                            "label": AGENT_USER_LABEL,
                            "actorId": "fafb5aee-1f7c-4bca-a524-eb99afa30ed0",
                            "modelId": AGENT_USER_ID,
                        },
                    },
                    {"type": "text", "text": " can you expand on that?"},
                ],
            }
        ],
    }

    second_result = asyncio.run(processor.handle(WebhookEnvelope.model_validate(second_payload)))

    assert second_result.action == "dry-run"
    assert second_result.thread_workspace == first_result.thread_workspace

    state_path = Path(second_result.thread_workspace) / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["interaction_count"] == 2
    assert len(state["recent_turns"]) == 2
    assert state["recent_turns"][0]["user_comment"] == "please summarize this"
    assert state["recent_turns"][0]["assistant_reply"] == "First reply."

    session_path = Path(second_result.thread_workspace) / "SESSION.md"
    session_text = session_path.read_text(encoding="utf-8")
    assert "The thread started with a summary request and then asked for more detail." in session_text
    assert "What level of expansion the user wants next." in session_text


def test_comment_processor_resolves_runtime_user_id_when_config_missing(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        outline_agent_user_id=None,
        trigger_mode="mention",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=True,
    )
    outline_client = DummyOutlineClient()
    model_client = SimpleModelClient()
    processor = CommentProcessor(
        settings=settings,
        store=ProcessedEventStore(tmp_path / "processed.json"),
        outline_client=outline_client,
        model_client=model_client,
    )

    result = asyncio.run(processor.handle(_load_fixture()))

    assert result.action == "dry-run"
    assert result.triggered_alias == AGENT_USER_LABEL
    assert settings.outline_agent_user_id == AGENT_USER_ID
    assert settings.runtime_outline_user_id == AGENT_USER_ID
    assert settings.runtime_outline_user_name == AGENT_USER_LABEL


def test_comment_processor_triggers_on_reply_to_agent_without_explicit_mention(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        outline_agent_user_id=AGENT_USER_ID,
        trigger_mode="mention",
        trigger_on_reply_to_agent=True,
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=True,
    )
    outline_client = DummyOutlineClient()
    outline_client.comment_items = [
        OutlineComment(
            id="agent-parent-comment",
            document_id="cad435c3-1cb9-4dd5-9254-d355b02fd795",
            parent_comment_id=None,
            created_by_id=AGENT_USER_ID,
            created_by_name=AGENT_USER_LABEL,
            created_at="2026-03-09T04:48:00.000Z",
            data={
                "type": "doc",
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Agent reply"}]}],
            },
        )
    ]
    model_client = SimpleModelClient()
    processor = CommentProcessor(
        settings=settings,
        store=ProcessedEventStore(tmp_path / "processed.json"),
        outline_client=outline_client,
        model_client=model_client,
    )

    envelope = _load_fixture()
    payload = envelope.model_dump()
    payload["payload"]["model"]["parentCommentId"] = "agent-parent-comment"
    payload["payload"]["model"]["data"] = {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Can you expand on that?"}]}],
    }

    result = asyncio.run(processor.handle(WebhookEnvelope.model_validate(payload)))

    assert result.action == "dry-run"
    assert result.triggered_alias == "reply-to-agent"


def test_comment_processor_triggers_on_sibling_reply_after_agent_replied_in_thread(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        outline_agent_user_id=AGENT_USER_ID,
        trigger_mode="mention",
        trigger_on_reply_to_agent=True,
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=True,
    )
    outline_client = DummyOutlineClient()
    outline_client.comment_items = [
        OutlineComment(
            id="root-user-comment",
            document_id="cad435c3-1cb9-4dd5-9254-d355b02fd795",
            parent_comment_id=None,
            created_by_id="user-1",
            created_by_name="Gavin Gong",
            created_at="2026-03-09T04:48:00.000Z",
            data={
                "type": "doc",
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Original prompt"}]}],
            },
        ),
        OutlineComment(
            id="agent-sibling-reply",
            document_id="cad435c3-1cb9-4dd5-9254-d355b02fd795",
            parent_comment_id="root-user-comment",
            created_by_id=AGENT_USER_ID,
            created_by_name=AGENT_USER_LABEL,
            created_at="2026-03-09T04:49:00.000Z",
            data={
                "type": "doc",
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Agent reply"}]}],
            },
        ),
    ]
    model_client = SimpleModelClient()
    processor = CommentProcessor(
        settings=settings,
        store=ProcessedEventStore(tmp_path / "processed.json"),
        outline_client=outline_client,
        model_client=model_client,
    )

    envelope = _load_fixture()
    payload = envelope.model_dump()
    payload["payload"]["model"]["parentCommentId"] = "root-user-comment"
    payload["payload"]["model"]["data"] = {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "What else can you do?"}]}],
    }

    result = asyncio.run(processor.handle(WebhookEnvelope.model_validate(payload)))

    assert result.action == "dry-run"
    assert result.triggered_alias == "reply-to-agent"
    assert outline_client.reactions == [
        ("add", "cad435c3-1cb9-4dd5-9254-d355b02fd795", "👀"),
        ("remove", "cad435c3-1cb9-4dd5-9254-d355b02fd795", "👀"),
        ("add", "cad435c3-1cb9-4dd5-9254-d355b02fd795", "👍"),
    ]


def test_comment_processor_does_not_leave_processing_reaction_for_untriggered_reply(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        outline_agent_user_id=AGENT_USER_ID,
        trigger_mode="mention",
        trigger_on_reply_to_agent=True,
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=True,
    )
    outline_client = DummyOutlineClient()
    outline_client.comment_items = [
        OutlineComment(
            id="root-user-comment",
            document_id="cad435c3-1cb9-4dd5-9254-d355b02fd795",
            parent_comment_id=None,
            created_by_id="user-1",
            created_by_name="Gavin Gong",
            created_at="2026-03-09T04:48:00.000Z",
            data={
                "type": "doc",
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Original prompt"}]}],
            },
        )
    ]
    model_client = SimpleModelClient()
    processor = CommentProcessor(
        settings=settings,
        store=ProcessedEventStore(tmp_path / "processed.json"),
        outline_client=outline_client,
        model_client=model_client,
    )

    envelope = _load_fixture()
    payload = envelope.model_dump()
    payload["payload"]["model"]["parentCommentId"] = "root-user-comment"
    payload["payload"]["model"]["data"] = {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "What else can you do?"}]}],
    }

    result = asyncio.run(processor.handle(WebhookEnvelope.model_validate(payload)))

    assert result.action == "ignored"
    assert result.reason == "no-trigger-mention"
    assert outline_client.reactions == []


def test_comment_processor_posts_failure_comment_and_clears_reaction_on_internal_error(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        outline_agent_user_id=AGENT_USER_ID,
        mention_aliases=["@agent"],
        trigger_mode="mention",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=False,
    )
    outline_client = DummyOutlineClient()
    store = ProcessedEventStore(tmp_path / "processed.json")
    processor = CommentProcessor(
        settings=settings,
        store=store,
        outline_client=outline_client,
        model_client=ExplodingModelClient(),
    )

    envelope = _load_fixture()
    result = asyncio.run(processor.handle(envelope))

    assert result.action == "error"
    assert result.reason == "internal-error"
    assert outline_client.reactions == [
        ("add", "cad435c3-1cb9-4dd5-9254-d355b02fd795", "👀"),
        ("remove", "cad435c3-1cb9-4dd5-9254-d355b02fd795", "👀"),
    ]
    assert len(outline_client.posted) == 1
    posted = outline_client.posted[0]
    assert posted["parent_comment_id"] == "cad435c3-1cb9-4dd5-9254-d355b02fd795"
    assert posted["text"] == "Thinking…"
    assert len(outline_client.updated_comments) == 1
    assert outline_client.updated_comments[0]["comment_id"] == "comment-1"
    assert "Sorry — I hit an internal error" in outline_client.updated_comments[0]["text"]
    assert "error_id:" in outline_client.updated_comments[0]["text"]
    assert "error_type:" in outline_client.updated_comments[0]["text"]
    assert store.contains("comments.create:cad435c3-1cb9-4dd5-9254-d355b02fd795")


def test_app_failure_handler_posts_failure_comment_for_triggered_comment(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        outline_agent_user_id=AGENT_USER_ID,
        mention_aliases=["@agent"],
        trigger_mode="mention",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=False,
    )
    outline_client = DummyOutlineClient()
    store = ProcessedEventStore(tmp_path / "processed.json")
    envelope = _load_fixture()

    asyncio.run(
        _maybe_post_failure_comment(
            settings=settings,
            envelope=envelope,
            outline_client=outline_client,
            store=store,
            exc=RuntimeError("model config error"),
        )
    )

    assert len(outline_client.posted) == 1
    posted = outline_client.posted[0]
    assert posted["parent_comment_id"] == "cad435c3-1cb9-4dd5-9254-d355b02fd795"
    assert "Sorry — I hit an internal error" in (posted["text"] or "")
    assert store.contains("comments.create:cad435c3-1cb9-4dd5-9254-d355b02fd795")


class ToolPlanModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        assert "local sandbox tools" in system_prompt
        assert "Thread work dir:" in user_prompt
        assert "Latest user comment:" in user_prompt
        if len(self.calls) == 1:
            return json.dumps(
                {
                    "should_run": True,
                    "reason": "The user explicitly asked me to create and run a shell script in the local work dir.",
                    "steps": [
                        {
                            "tool": "write_file",
                            "path": "hello.sh",
                            "content": "#!/usr/bin/env bash\necho hello-from-tool\n",
                        },
                        {
                            "tool": "run_shell",
                            "command": "bash hello.sh",
                        },
                    ],
                }
            )
        return json.dumps(
            {
                "should_run": False,
                "reason": "The requested local work is complete.",
                "steps": [],
            }
        )


class ImageDrivenToolPlanModelClient:
    def __init__(self) -> None:
        self.text_calls: list[tuple[str, str]] = []
        self.image_calls: list[tuple[str, str, int]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.text_calls.append((system_prompt, user_prompt))
        raise AssertionError("Expected multimodal tool planning")

    async def generate_reply_with_images(self, system_prompt: str, user_prompt: str, *, input_images) -> str:
        self.image_calls.append((system_prompt, user_prompt, len(input_images)))
        assert "local sandbox tools" in system_prompt
        assert "The latest user comment also includes 1 embedded image." in user_prompt
        assert len(input_images) == 1
        assert input_images[0].media_type == "image/png"
        if len(self.image_calls) == 1:
            return json.dumps(
                {
                    "should_run": True,
                    "reason": "The user asked me to create a local summary file based on the attached image.",
                    "steps": [
                        {
                            "tool": "write_file",
                            "path": "summary.txt",
                            "content": "This image appears to be a simple visual test image.",
                        }
                    ],
                }
            )
        return json.dumps(
            {
                "should_run": False,
                "reason": "The local image-based summary file is ready.",
                "steps": [],
            }
        )


class ReplyAfterToolUseModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        nonreply_response = _maybe_nonreply_planner_response(system_prompt)
        if nonreply_response is not None:
            return nonreply_response
        assert "Tool execution outcome:" in user_prompt
        assert "status: applied" in user_prompt
        assert "write_file[hello.sh]" in user_prompt
        assert "stdout=hello-from-tool" in user_prompt
        return "Done — I created `hello.sh`, ran it in the thread work dir, and it printed `hello-from-tool`."


class ReplyAfterImageDrivenToolUseModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        nonreply_response = _maybe_nonreply_planner_response(system_prompt)
        if nonreply_response is not None:
            return nonreply_response
        assert "Tool execution outcome:" in user_prompt
        assert "write_file[summary.txt]" in user_prompt
        return "好了，我已经根据图片内容在工作目录里写好了 `summary.txt`。"


class PdfUploadToolPlanModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        assert "upload_attachment" in system_prompt
        assert "Current Outline document ID:" in user_prompt
        if len(self.calls) == 1:
            return json.dumps(
                {
                    "should_run": True,
                    "reason": "The user asked for a PDF artifact to be generated and attached back into Outline.",
                    "steps": [
                        {
                            "tool": "write_file",
                            "path": "artifacts/report.pdf",
                            "content": "%PDF-1.7\nfake-pdf\n",
                        },
                        {
                            "tool": "upload_attachment",
                            "path": "artifacts/report.pdf",
                        },
                    ],
                }
            )
        return json.dumps(
            {
                "should_run": False,
                "reason": "The PDF artifact has already been uploaded.",
                "steps": [],
            }
        )


class ReplyAfterPdfUploadModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        nonreply_response = _maybe_nonreply_planner_response(system_prompt)
        if nonreply_response is not None:
            return nonreply_response
        assert "Tool execution outcome:" in user_prompt
        assert "upload_attachment[artifacts/report.pdf] -> attachment_id=attachment-1" in user_prompt
        assert (
            "uploaded_file: report.pdf -> "
            "https://outline.example/api/attachments.redirect?id=attachment-1"
        ) in user_prompt
        return (
            "Done — I generated `artifacts/report.pdf` in the thread work dir and "
            "uploaded it back to this Outline document as an attachment."
        )


class RepeatingUploadToolPlanModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        assert "Do not upload the same file more than once in the same turn" in system_prompt

        if len(self.calls) == 1:
            return json.dumps(
                {
                    "should_run": True,
                    "reason": "Create the PDF and upload it.",
                    "steps": [
                        {
                            "tool": "write_file",
                            "path": "repo/main.pdf",
                            "content": "%PDF-1.7\nloop-test\n",
                        },
                        {
                            "tool": "upload_attachment",
                            "path": "repo/main.pdf",
                        },
                    ],
                }
            )

        assert len(self.calls) == 2
        assert "round 1 (status=applied):" in user_prompt
        assert "upload_attachment[repo/main.pdf] -> attachment_id=attachment-1" in user_prompt
        return json.dumps(
            {
                "should_run": True,
                "reason": "Upload it again.",
                "steps": [
                    {
                        "tool": "upload_attachment",
                        "path": "repo/main.pdf",
                    }
                ],
            }
        )


class ReplyAfterRepeatedUploadLoopGuardModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        nonreply_response = _maybe_nonreply_planner_response(system_prompt)
        if nonreply_response is not None:
            return nonreply_response
        assert "Tool execution outcome:" in user_prompt
        assert "Round 1:" in user_prompt
        assert "Round 2:" in user_prompt
        assert "repeated attachment upload plan detected" in user_prompt
        assert (
            "registered_file: main.pdf -> "
            "https://outline.example/api/attachments.redirect?id=attachment-1"
        ) in user_prompt
        return (
            "I uploaded `repo/main.pdf` successfully, then stopped because the next tool round "
            "was trying to upload the same file again without any new changes."
        )


class AlternatingReadOnlyToolPlanModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        assert "Do not repeat the same inspection-only plan" in system_prompt

        if len(self.calls) == 1:
            return json.dumps(
                {
                    "should_run": True,
                    "reason": "Create a file first.",
                    "steps": [
                        {
                            "tool": "write_file",
                            "path": "notes.txt",
                            "content": "loop-guard-demo\n",
                        }
                    ],
                }
            )

        if len(self.calls) == 2:
            assert "round 1 (status=applied):" in user_prompt
            return json.dumps(
                {
                    "should_run": True,
                    "reason": "Inspect the file contents.",
                    "steps": [
                        {
                            "tool": "read_file",
                            "path": "notes.txt",
                        }
                    ],
                }
            )

        if len(self.calls) == 3:
            assert "round 2 (status=applied):" in user_prompt
            return json.dumps(
                {
                    "should_run": True,
                    "reason": "Inspect the work dir.",
                    "steps": [
                        {
                            "tool": "list_dir",
                            "path": ".",
                        }
                    ],
                }
            )

        assert len(self.calls) == 4
        assert "round 3 (status=applied):" in user_prompt
        return json.dumps(
            {
                "should_run": True,
                "reason": "Read the same file again.",
                "steps": [
                    {
                        "tool": "read_file",
                        "path": "notes.txt",
                    }
                ],
            }
        )


class ReplyAfterRepeatedNoChangeToolLoopGuardModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        nonreply_response = _maybe_nonreply_planner_response(system_prompt)
        if nonreply_response is not None:
            return nonreply_response
        assert "Tool execution outcome:" in user_prompt
        assert "Round 4:" in user_prompt
        assert "repeated tool plan detected with no intervening state change" in user_prompt
        return (
            "I stopped because the next local tool round was repeating an earlier inspection step "
            "without any new workspace changes."
        )


class ChangedFileReuploadToolPlanModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        assert (
            "Do not upload the same file more than once in the same turn unless the file was changed afterwards"
            in system_prompt
        )

        if len(self.calls) == 1:
            return json.dumps(
                {
                    "should_run": True,
                    "reason": "Create the first artifact version and upload it.",
                    "steps": [
                        {
                            "tool": "write_file",
                            "path": "repo/main.pdf",
                            "content": "%PDF-1.7\nversion-one\n",
                        },
                        {
                            "tool": "upload_attachment",
                            "path": "repo/main.pdf",
                        },
                    ],
                }
            )

        if len(self.calls) == 2:
            assert "upload_attachment[repo/main.pdf] -> attachment_id=attachment-1" in user_prompt
            return json.dumps(
                {
                    "should_run": True,
                    "reason": "Update the artifact contents.",
                    "steps": [
                        {
                            "tool": "write_file",
                            "path": "repo/main.pdf",
                            "content": "%PDF-1.7\nversion-two\n",
                        }
                    ],
                }
            )

        if len(self.calls) == 3:
            assert "write_file[repo/main.pdf] -> 21 chars" in user_prompt
            return json.dumps(
                {
                    "should_run": True,
                    "reason": "Upload the updated artifact.",
                    "steps": [
                        {
                            "tool": "upload_attachment",
                            "path": "repo/main.pdf",
                        }
                    ],
                }
            )

        return json.dumps(
            {
                "should_run": False,
                "reason": "The updated artifact is already uploaded.",
                "steps": [],
            }
        )


class ReplyAfterChangedFileReuploadModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        assert "Tool execution outcome:" in user_prompt
        assert "upload_attachment[repo/main.pdf] -> attachment_id=attachment-1" in user_prompt
        assert "upload_attachment[repo/main.pdf] -> attachment_id=attachment-2" in user_prompt
        return "Done — I uploaded the original artifact, updated it, and uploaded the new version as well."


def test_comment_processor_can_use_local_thread_tools_before_replying(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        outline_agent_user_id=AGENT_USER_ID,
        trigger_mode="mention",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=False,
        document_update_enabled=False,
        memory_update_enabled=False,
        thread_session_update_enabled=False,
        tool_use_enabled=True,
        progress_comment_enabled=False,
    )
    outline_client = DummyOutlineClient()
    reply_model_client = ReplyAfterToolUseModelClient()
    tool_model_client = ToolPlanModelClient()
    processor = CommentProcessor(
        settings=settings,
        store=ProcessedEventStore(tmp_path / "processed.json"),
        outline_client=outline_client,
        model_client=reply_model_client,
        tool_model_client=tool_model_client,
        action_router_model_client=ActionRouterModelClient(),
    )

    payload = _load_fixture().model_dump()
    payload["payload"]["model"]["data"] = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "mention",
                        "attrs": {
                            "id": "some-node-id",
                            "type": "user",
                            "label": AGENT_USER_LABEL,
                            "actorId": "fafb5aee-1f7c-4bca-a524-eb99afa30ed0",
                            "modelId": AGENT_USER_ID,
                        },
                    },
                    {
                        "type": "text",
                        "text": " please create a shell script in your work dir that prints hello-from-tool and run it",
                    },
                ],
            }
        ],
    }

    result = asyncio.run(processor.handle(WebhookEnvelope.model_validate(payload)))

    assert result.action == "used-tools-and-replied"
    assert result.reason == "tools-executed-and-replied"
    assert result.tool_execution_preview is not None
    assert "write_file[hello.sh]" in result.tool_execution_preview
    assert "stdout=hello-from-tool" in result.tool_execution_preview
    assert outline_client.posted[0]["text"] == (
        "Done — I created `hello.sh`, ran it in the thread work dir, and it printed `hello-from-tool`."
    )
    assert result.thread_workspace is not None
    work_dir = Path(result.thread_workspace) / "work"
    assert work_dir.exists()
    assert (work_dir / "hello.sh").read_text(encoding="utf-8") == "#!/usr/bin/env bash\necho hello-from-tool\n"
    state = json.loads((Path(result.thread_workspace) / "state.json").read_text(encoding="utf-8"))
    assert state["recent_tool_runs"]
    assert state["recent_tool_runs"][-1]["status"] == "applied"


def test_comment_processor_can_drive_tool_plan_from_current_comment_image(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        outline_agent_user_id=AGENT_USER_ID,
        mention_aliases=["@agent"],
        trigger_mode="mention",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=False,
    )
    outline_client = DummyOutlineClient()
    reply_model_client = ReplyAfterImageDrivenToolUseModelClient()
    tool_model_client = ImageDrivenToolPlanModelClient()
    no_op_model_client = SimpleModelClient()
    processor = CommentProcessor(
        settings=settings,
        store=ProcessedEventStore(tmp_path / "processed.json"),
        outline_client=outline_client,
        model_client=reply_model_client,
        memory_model_client=no_op_model_client,
        thread_session_model_client=no_op_model_client,
        tool_model_client=tool_model_client,
        action_router_model_client=ActionRouterModelClient(),
    )

    payload = _load_fixture().model_dump()
    payload["payload"]["model"]["data"] = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "mention",
                        "attrs": {
                            "id": "some-node-id",
                            "type": "user",
                            "label": AGENT_USER_LABEL,
                            "actorId": "fafb5aee-1f7c-4bca-a524-eb99afa30ed0",
                            "modelId": AGENT_USER_ID,
                        },
                    },
                    {"type": "text", "text": " please use your work dir to write summary.txt based on this image"},
                    {"type": "image", "attrs": {"src": "/api/attachments.redirect?id=image-tool-1", "alt": None}},
                ],
            }
        ],
    }

    result = asyncio.run(processor.handle(WebhookEnvelope.model_validate(payload)))

    assert result.action == "used-tools-and-replied"
    assert tool_model_client.image_calls
    assert result.thread_workspace is not None
    work_dir = Path(result.thread_workspace) / "work"
    assert (work_dir / "summary.txt").read_text(encoding="utf-8") == (
        "This image appears to be a simple visual test image."
    )
    all_comment_texts = [str(item.get("text")) for item in outline_client.posted] + [
        item["text"] for item in outline_client.updated_comments
    ]
    assert "好了，我已经根据图片内容在工作目录里写好了 `summary.txt`。" in all_comment_texts


def test_comment_processor_can_upload_generated_pdf_artifact_to_outline(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        outline_agent_user_id=AGENT_USER_ID,
        trigger_mode="mention",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=False,
        document_update_enabled=False,
        memory_update_enabled=False,
        thread_session_update_enabled=False,
        tool_use_enabled=True,
        progress_comment_enabled=False,
    )
    outline_client = DummyOutlineClient()
    reply_model_client = ReplyAfterPdfUploadModelClient()
    tool_model_client = PdfUploadToolPlanModelClient()
    processor = CommentProcessor(
        settings=settings,
        store=ProcessedEventStore(tmp_path / "processed.json"),
        outline_client=outline_client,
        model_client=reply_model_client,
        tool_model_client=tool_model_client,
        action_router_model_client=ActionRouterModelClient(),
    )

    payload = _load_fixture().model_dump()
    document_id = payload["payload"]["model"]["documentId"]
    payload["payload"]["model"]["data"] = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "mention",
                        "attrs": {
                            "id": "some-node-id",
                            "type": "user",
                            "label": AGENT_USER_LABEL,
                            "actorId": "fafb5aee-1f7c-4bca-a524-eb99afa30ed0",
                            "modelId": AGENT_USER_ID,
                        },
                    },
                    {
                        "type": "text",
                        "text": " please generate a PDF artifact and upload it back to this document as an attachment",
                    },
                ],
            }
        ],
    }

    result = asyncio.run(processor.handle(WebhookEnvelope.model_validate(payload)))

    assert result.action == "used-tools-and-replied"
    assert result.reason == "tools-executed-and-replied"
    assert result.tool_execution_preview is not None
    assert "upload_attachment[artifacts/report.pdf] -> attachment_id=attachment-1" in result.tool_execution_preview
    assert "registered uploaded files in document: report.pdf" in result.tool_execution_preview
    assert outline_client.uploaded_attachments == [
        {
            "document_id": document_id,
            "file_path": str(Path(result.thread_workspace or "") / "work" / "artifacts" / "report.pdf"),
        }
    ]
    assert outline_client.updated_documents == [
        {
            "document_id": document_id,
            "title": None,
            "text": (
                "# Outline Agent Kickoff\n\n"
                "A kickoff doc for the new Outline agent project.\n\n"
                "## Uploaded Artifacts\n\n"
                "- [report.pdf](https://outline.example/api/attachments.redirect?id=attachment-1)"
            ),
            "publish": None,
        }
    ]
    assert outline_client.posted[-1]["text"] == (
        "Done — I generated `artifacts/report.pdf` in the thread work dir and "
        "uploaded it back to this Outline document as an attachment.\n\n"
        "Uploaded files:\n"
        "- [report.pdf](https://outline.example/api/attachments.redirect?id=attachment-1)"
    )
    assert result.thread_workspace is not None
    uploaded_file = Path(result.thread_workspace) / "work" / "artifacts" / "report.pdf"
    assert uploaded_file.read_text(encoding="utf-8") == "%PDF-1.7\nfake-pdf\n"
    state = json.loads((Path(result.thread_workspace) / "state.json").read_text(encoding="utf-8"))
    assert state["recent_tool_runs"]
    assert (
        "upload_attachment[artifacts/report.pdf] -> attachment_id=attachment-1"
        in state["recent_tool_runs"][-1]["summary"]
    )


def test_comment_processor_stops_redundant_repeated_attachment_upload_loop(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        outline_agent_user_id=AGENT_USER_ID,
        trigger_mode="mention",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=False,
        document_update_enabled=False,
        memory_update_enabled=False,
        thread_session_update_enabled=False,
        tool_use_enabled=True,
        tool_execution_max_rounds=100,
        progress_comment_enabled=False,
    )
    outline_client = DummyOutlineClient()
    reply_model_client = ReplyAfterRepeatedUploadLoopGuardModelClient()
    tool_model_client = RepeatingUploadToolPlanModelClient()
    processor = CommentProcessor(
        settings=settings,
        store=ProcessedEventStore(tmp_path / "processed.json"),
        outline_client=outline_client,
        model_client=reply_model_client,
        tool_model_client=tool_model_client,
        action_router_model_client=ActionRouterModelClient(),
    )

    payload = _load_fixture().model_dump()
    payload["payload"]["model"]["data"] = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "mention",
                        "attrs": {
                            "id": "some-node-id",
                            "type": "user",
                            "label": AGENT_USER_LABEL,
                            "actorId": "fafb5aee-1f7c-4bca-a524-eb99afa30ed0",
                            "modelId": AGENT_USER_ID,
                        },
                    },
                    {
                        "type": "text",
                        "text": " please build a PDF in repo/main.pdf and upload it back into Outline",
                    },
                ],
            }
        ],
    }

    result = asyncio.run(processor.handle(WebhookEnvelope.model_validate(payload)))

    assert result.action == "tool-attempted-and-replied"
    assert result.reason == "tool-planning-blocked-and-replied"
    assert result.tool_execution_preview is not None
    assert "round 1:" in result.tool_execution_preview.lower()
    assert "round 2: blocked: repeated attachment upload plan detected" in result.tool_execution_preview.lower()
    assert len(tool_model_client.calls) == 2
    assert len(outline_client.uploaded_attachments) == 1
    assert outline_client.posted[-1]["text"] == (
        "I uploaded `repo/main.pdf` successfully, then stopped because the next tool round "
        "was trying to upload the same file again without any new changes.\n\n"
        "Uploaded files:\n"
        "- [main.pdf](https://outline.example/api/attachments.redirect?id=attachment-1)"
    )
    assert outline_client.updated_documents == [
        {
            "document_id": payload["payload"]["model"]["documentId"],
            "title": None,
            "text": (
                "# Outline Agent Kickoff\n\n"
                "A kickoff doc for the new Outline agent project.\n\n"
                "## Uploaded Artifacts\n\n"
                "- [main.pdf](https://outline.example/api/attachments.redirect?id=attachment-1)"
            ),
            "publish": None,
        }
    ]
    assert result.thread_workspace is not None
    state = json.loads((Path(result.thread_workspace) / "state.json").read_text(encoding="utf-8"))
    assert len(state["recent_tool_runs"]) == 2
    assert state["recent_tool_runs"][0]["status"] == "applied"
    assert state["recent_tool_runs"][1]["status"] == "blocked"


def test_comment_processor_stops_repeated_no_change_inspection_loop(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        outline_agent_user_id=AGENT_USER_ID,
        trigger_mode="mention",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=False,
        document_update_enabled=False,
        memory_update_enabled=False,
        thread_session_update_enabled=False,
        tool_use_enabled=True,
        tool_execution_max_rounds=10,
        progress_comment_enabled=False,
    )
    outline_client = DummyOutlineClient()
    reply_model_client = ReplyAfterRepeatedNoChangeToolLoopGuardModelClient()
    tool_model_client = AlternatingReadOnlyToolPlanModelClient()
    processor = CommentProcessor(
        settings=settings,
        store=ProcessedEventStore(tmp_path / "processed.json"),
        outline_client=outline_client,
        model_client=reply_model_client,
        tool_model_client=tool_model_client,
        action_router_model_client=ActionRouterModelClient(),
    )

    payload = _load_fixture().model_dump()
    payload["payload"]["model"]["data"] = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "mention",
                        "attrs": {
                            "id": "some-node-id",
                            "type": "user",
                            "label": AGENT_USER_LABEL,
                            "actorId": "fafb5aee-1f7c-4bca-a524-eb99afa30ed0",
                            "modelId": AGENT_USER_ID,
                        },
                    },
                    {
                        "type": "text",
                        "text": " please inspect your work dir and the generated notes file until you are done",
                    },
                ],
            }
        ],
    }

    result = asyncio.run(processor.handle(WebhookEnvelope.model_validate(payload)))

    assert result.action == "tool-attempted-and-replied"
    assert result.reason == "tool-planning-blocked-and-replied"
    assert result.tool_execution_preview is not None
    assert "round 4: blocked: repeated tool plan detected with no intervening state change" in (
        result.tool_execution_preview.lower()
    )
    assert len(tool_model_client.calls) == 4
    assert outline_client.posted[-1]["text"] == (
        "I stopped because the next local tool round was repeating an earlier inspection step "
        "without any new workspace changes."
    )
    assert result.thread_workspace is not None
    work_dir = Path(result.thread_workspace) / "work"
    assert (work_dir / "notes.txt").read_text(encoding="utf-8") == "loop-guard-demo\n"
    state = json.loads((Path(result.thread_workspace) / "state.json").read_text(encoding="utf-8"))
    assert len(state["recent_tool_runs"]) == 4
    assert state["recent_tool_runs"][0]["status"] == "applied"
    assert state["recent_tool_runs"][1]["status"] == "applied"
    assert state["recent_tool_runs"][2]["status"] == "applied"
    assert state["recent_tool_runs"][3]["status"] == "blocked"


def test_comment_processor_allows_reupload_after_file_changes(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        outline_agent_user_id=AGENT_USER_ID,
        trigger_mode="mention",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=False,
        document_update_enabled=False,
        memory_update_enabled=False,
        thread_session_update_enabled=False,
        tool_use_enabled=True,
        tool_execution_max_rounds=10,
        progress_comment_enabled=False,
    )
    outline_client = DummyOutlineClient()
    reply_model_client = ReplyAfterChangedFileReuploadModelClient()
    tool_model_client = ChangedFileReuploadToolPlanModelClient()
    processor = CommentProcessor(
        settings=settings,
        store=ProcessedEventStore(tmp_path / "processed.json"),
        outline_client=outline_client,
        model_client=reply_model_client,
        tool_model_client=tool_model_client,
        action_router_model_client=ActionRouterModelClient(),
    )

    payload = _load_fixture().model_dump()
    document_id = payload["payload"]["model"]["documentId"]
    payload["payload"]["model"]["data"] = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "mention",
                        "attrs": {
                            "id": "some-node-id",
                            "type": "user",
                            "label": AGENT_USER_LABEL,
                            "actorId": "fafb5aee-1f7c-4bca-a524-eb99afa30ed0",
                            "modelId": AGENT_USER_ID,
                        },
                    },
                    {
                        "type": "text",
                        "text": " please build repo/main.pdf, upload it, revise it, and upload the revised file again",
                    },
                ],
            }
        ],
    }

    result = asyncio.run(processor.handle(WebhookEnvelope.model_validate(payload)))

    assert result.action == "used-tools-and-replied"
    assert result.reason == "tools-executed-and-replied"
    assert result.tool_execution_preview is not None
    assert "upload_attachment[repo/main.pdf] -> attachment_id=attachment-1" in result.tool_execution_preview
    assert "upload_attachment[repo/main.pdf] -> attachment_id=attachment-2" in result.tool_execution_preview
    assert len(tool_model_client.calls) == 4
    assert len(outline_client.uploaded_attachments) == 2
    assert outline_client.uploaded_attachments == [
        {
            "document_id": document_id,
            "file_path": str(Path(result.thread_workspace or "") / "work" / "repo" / "main.pdf"),
        },
        {
            "document_id": document_id,
            "file_path": str(Path(result.thread_workspace or "") / "work" / "repo" / "main.pdf"),
        },
    ]
    assert outline_client.updated_documents == [
        {
            "document_id": document_id,
            "title": None,
            "text": (
                "# Outline Agent Kickoff\n\n"
                "A kickoff doc for the new Outline agent project.\n\n"
                "## Uploaded Artifacts\n\n"
                "- [main.pdf](https://outline.example/api/attachments.redirect?id=attachment-1)\n"
                "- [main.pdf](https://outline.example/api/attachments.redirect?id=attachment-2)"
            ),
            "publish": None,
        }
    ]
    assert outline_client.posted[-1]["text"] == (
        "Done — I uploaded the original artifact, updated it, and uploaded the new version as well.\n\n"
        "Uploaded files:\n"
        "- [main.pdf](https://outline.example/api/attachments.redirect?id=attachment-1)\n"
        "- [main.pdf](https://outline.example/api/attachments.redirect?id=attachment-2)"
    )
    assert result.thread_workspace is not None
    uploaded_file = Path(result.thread_workspace) / "work" / "repo" / "main.pdf"
    assert uploaded_file.read_text(encoding="utf-8") == "%PDF-1.7\nversion-two\n"
    state = json.loads((Path(result.thread_workspace) / "state.json").read_text(encoding="utf-8"))
    assert len(state["recent_tool_runs"]) == 3
    assert state["recent_tool_runs"][0]["status"] == "applied"
    assert state["recent_tool_runs"][1]["status"] == "applied"
    assert state["recent_tool_runs"][2]["status"] == "applied"


def test_comment_processor_posts_and_updates_progress_comment_for_tool_execution(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        outline_agent_user_id=AGENT_USER_ID,
        trigger_mode="mention",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=False,
        document_update_enabled=False,
        memory_update_enabled=False,
        thread_session_update_enabled=False,
        tool_use_enabled=True,
        reaction_enabled=False,
        progress_comment_enabled=True,
    )
    outline_client = DummyOutlineClient()
    reply_model_client = ReplyAfterToolUseModelClient()
    tool_model_client = ToolPlanModelClient()
    processor = CommentProcessor(
        settings=settings,
        store=ProcessedEventStore(tmp_path / "processed.json"),
        outline_client=outline_client,
        model_client=reply_model_client,
        tool_model_client=tool_model_client,
        action_router_model_client=ActionRouterModelClient(),
    )

    payload = _load_fixture().model_dump()
    payload["payload"]["model"]["data"] = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "mention",
                        "attrs": {
                            "id": "some-node-id",
                            "type": "user",
                            "label": AGENT_USER_LABEL,
                            "actorId": "fafb5aee-1f7c-4bca-a524-eb99afa30ed0",
                            "modelId": AGENT_USER_ID,
                        },
                    },
                    {
                        "type": "text",
                        "text": " please create a shell script in your work dir that prints hello-from-tool and run it",
                    },
                ],
            }
        ],
    }

    result = asyncio.run(processor.handle(WebhookEnvelope.model_validate(payload)))

    assert result.action == "used-tools-and-replied"
    assert len(outline_client.posted) == 2
    assert outline_client.posted[0]["text"] == "Thinking…"
    assert outline_client.posted[0]["parent_comment_id"] == "cad435c3-1cb9-4dd5-9254-d355b02fd795"
    assert outline_client.posted[-1]["text"] == (
        "Done — I created `hello.sh`, ran it in the thread work dir, and it printed `hello-from-tool`."
    )
    assert outline_client.updated_comments
    assert outline_client.updated_comments[0]["comment_id"] == "comment-1"
    assert outline_client.updated_comments[0]["text"].startswith("Working on it —")
    assert "Recent progress:" in outline_client.updated_comments[0]["text"]
    assert "Planned round 1: create or update `hello.sh`; run `bash hello.sh`." in outline_client.updated_comments[0]["text"]
    assert outline_client.updated_comments[-1]["text"].startswith(
        "Done — I finished the requested local workspace actions."
    )
    assert "Finished: ran `bash hello.sh` → output `hello-from-tool`." in outline_client.updated_comments[-1]["text"]

    assert result.thread_workspace is not None
    state = json.loads((Path(result.thread_workspace) / "state.json").read_text(encoding="utf-8"))
    assert state["progress_comment_map"]["cad435c3-1cb9-4dd5-9254-d355b02fd795"] == "comment-1"
    assert state["recent_progress_actions"]
    assert state["recent_progress_actions"][-1]["status"] == "applied"
    assert state["recent_progress_actions"][-1]["status_comment_id"] == "comment-1"
    assert any(
        "Finished: ran `bash hello.sh`" in item for item in state["recent_progress_actions"][-1]["actions"]
    )


class MultiRoundToolPlanModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        assert "local sandbox tools" in system_prompt

        if len(self.calls) == 1:
            assert "Current planning round: 1 of 3" in user_prompt
            assert "Tool execution history in this turn:\n(none yet)" in user_prompt
            return json.dumps(
                {
                    "should_run": True,
                    "reason": "First create the requested artifact inside the work dir.",
                    "steps": [
                        {
                            "tool": "write_file",
                            "path": "report.txt",
                            "content": "artifact-generated-in-round-one\n",
                        }
                    ],
                }
            )

        if len(self.calls) == 2:
            assert "Current planning round: 2 of 3" in user_prompt
            assert "round 1 (status=applied):" in user_prompt
            assert "report.txt" in user_prompt
            return json.dumps(
                {
                    "should_run": True,
                    "reason": "Now inspect the artifact that was created in round 1.",
                    "steps": [
                        {
                            "tool": "read_file",
                            "path": "report.txt",
                        }
                    ],
                }
            )

        assert len(self.calls) == 3
        assert "Current planning round: 3 of 3" in user_prompt
        assert "round 2 (status=applied):" in user_prompt
        return json.dumps(
            {
                "should_run": False,
                "reason": "The necessary file work is complete, so I can now answer the user.",
                "steps": [],
            }
        )


class ReplyAfterMultiRoundToolUseModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        nonreply_response = _maybe_nonreply_planner_response(system_prompt)
        if nonreply_response is not None:
            return nonreply_response
        assert "Tool execution outcome:" in user_prompt
        assert "Round 1:" in user_prompt
        assert "Round 2:" in user_prompt
        assert "write_file[report.txt]" in user_prompt
        assert "read_file[report.txt] -> artifact-generated-in-round-one" in user_prompt
        return (
            "Done — I created `report.txt`, read it back in a second tool round, "
            "and confirmed it contains `artifact-generated-in-round-one`."
        )


class MaxRoundsToolPlanModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        assert "local sandbox tools" in system_prompt

        if len(self.calls) == 1:
            assert "Current planning round: 1 of 2" in user_prompt
            return json.dumps(
                {
                    "should_run": True,
                    "reason": "Create an artifact first.",
                    "steps": [
                        {
                            "tool": "write_file",
                            "path": "round1.txt",
                            "content": "round-one-output\n",
                        }
                    ],
                }
            )

        assert len(self.calls) == 2
        assert "Current planning round: 2 of 2" in user_prompt
        assert "round 1 (status=applied):" in user_prompt
        return json.dumps(
            {
                "should_run": True,
                "reason": "Inspect the artifact, but keep the loop going.",
                "steps": [
                    {
                        "tool": "read_file",
                        "path": "round1.txt",
                    }
                ],
            }
        )


class ReplyAfterMaxRoundsToolUseModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        nonreply_response = _maybe_nonreply_planner_response(system_prompt)
        if nonreply_response is not None:
            return nonreply_response
        assert "Tool execution outcome:" in user_prompt
        assert "Round 1:" in user_prompt
        assert "Round 2:" in user_prompt
        assert "Round 3:" in user_prompt
        assert "stopped-max-rounds" in user_prompt
        return (
            "I completed the first two local tool rounds, but stopped before any more "
            "because the configured tool-round limit was reached."
        )


class UnexpectedDocumentEditModelClient:
    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        raise AssertionError("Document update planner should not run during cross-thread handoff")


class ActionRouterModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        assert "agent subsystems should be invoked" in system_prompt
        lowered = user_prompt.lower()
        if "remember that replies should stay under five bullets" in lowered:
            return json.dumps(
                {
                    "document_creation": False,
                    "document_update": False,
                    "tool_use": False,
                    "memory_action": True,
                    "cross_thread_handoff": False,
                    "same_document_comment_lookup": False,
                    "reason": "The user explicitly asked for a memory change.",
                }
            )
        if "rewrite the kickoff document" in lowered or "shorten the roadmap section" in lowered:
            return json.dumps(
                {
                    "document_creation": False,
                    "document_update": True,
                    "tool_use": False,
                    "memory_action": False,
                    "cross_thread_handoff": False,
                    "same_document_comment_lookup": False,
                    "reason": "The user asked for a direct document edit.",
                }
            )
        if "diagram-ready outline document please" in lowered or "draw diagram in the document" in lowered:
            return json.dumps(
                {
                    "document_creation": False,
                    "document_update": True,
                    "tool_use": False,
                    "memory_action": False,
                    "cross_thread_handoff": False,
                    "same_document_comment_lookup": False,
                    "reason": "The user asked to add document content directly.",
                }
            )
        if "写一篇关于 llm moe" in lowered:
            return json.dumps(
                {
                    "document_creation": False,
                    "document_update": True,
                    "tool_use": False,
                    "memory_action": False,
                    "cross_thread_handoff": False,
                    "same_document_comment_lookup": False,
                    "reason": "The user asked to write the current document.",
                }
            )
        if "new document" in lowered or "新的文档" in lowered or "separate document" in lowered:
            return json.dumps(
                {
                    "document_creation": True,
                    "document_update": False,
                    "tool_use": False,
                    "memory_action": False,
                    "cross_thread_handoff": False,
                    "same_document_comment_lookup": False,
                    "reason": "The user asked for a separate new document.",
                }
            )
        if (
            "hello.sh" in lowered
            or "report.txt" in lowered
            or "pdf artifact" in lowered
            or "shell script" in lowered
            or "your work dir" in lowered
            or "repo/main.pdf" in lowered
            or "upload it back" in lowered
        ):
            return json.dumps(
                {
                    "document_creation": False,
                    "document_update": False,
                    "tool_use": True,
                    "memory_action": False,
                    "cross_thread_handoff": False,
                    "same_document_comment_lookup": False,
                    "reason": "The user asked for local workspace actions.",
                }
            )
        if "previous discussion thread" in lowered or "continue the previous discussion thread" in lowered:
            return json.dumps(
                {
                    "document_creation": False,
                    "document_update": False,
                    "tool_use": False,
                    "memory_action": False,
                    "cross_thread_handoff": True,
                    "same_document_comment_lookup": False,
                    "reason": "The user is referring to another thread in this document.",
                }
            )
        if "earlier pdf comments in this document" in lowered or "previous comments in this document" in lowered:
            return json.dumps(
                {
                    "document_creation": False,
                    "document_update": False,
                    "tool_use": False,
                    "memory_action": False,
                    "cross_thread_handoff": False,
                    "same_document_comment_lookup": True,
                    "reason": "The user asked to inspect other comments in this same document.",
                }
            )
        return json.dumps(
            {
                "document_creation": False,
                "document_update": False,
                "tool_use": False,
                "memory_action": False,
                "cross_thread_handoff": False,
                "same_document_comment_lookup": False,
                "reason": "No extra thread-context retrieval is needed.",
            }
        )


class ResolvedHandoffReplyModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        assert "Potential cross-thread handoff context:" in user_prompt
        assert "Most likely referenced thread:" in user_prompt
        assert "roadmap rollout plan for direct document editing" in user_prompt
        assert "Do not directly perform document edits or local tool actions in this turn." in user_prompt
        assert "Document update outcome:" not in user_prompt
        return "I believe you mean the earlier roadmap rollout thread. Do you want me to continue from that plan?"


class AmbiguousHandoffReplyModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        assert "Potential cross-thread handoff context:" in user_prompt
        assert "multiple candidates exist" in user_prompt
        assert "thread-alpha" in user_prompt
        assert "thread-beta" in user_prompt
        assert "@mention you inside that thread" in user_prompt
        assert "Document update outcome:" not in user_prompt
        return (
            "I found multiple earlier threads that might match. "
            "Please tell me which one you mean, or @mention me there."
        )


class SameDocumentResolvedReplyModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        assert "Same-document comment lookup outcome:" in user_prompt
        assert "Most relevant matching thread:" in user_prompt
        assert "thread_id: thread-pdf" in user_prompt
        assert "PDF build is failing on CI because the fonts package is missing." in user_prompt
        assert "We should upload the generated artifact link in the final reply." in user_prompt
        assert "Relevant comment context:" in user_prompt
        return "I checked another thread in this same document. It discussed the PDF build failure and suggested uploading the generated artifact link in the final reply."


class SameDocumentAmbiguousReplyModelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        assert "Same-document comment lookup outcome:" in user_prompt
        assert "Multiple candidate same-document threads may match:" in user_prompt
        assert "thread_id=thread-alpha" in user_prompt
        assert "thread_id=thread-beta" in user_prompt
        assert "ask the user which thread or topic they mean" in user_prompt
        return "I can inspect earlier comments in this same document, but I found multiple likely threads. Tell me which one you mean."


def _seed_thread_summary(
    manager: CollectionWorkspaceManager,
    *,
    document_id: str,
    thread_id: str,
    summary: str,
    comment_text: str,
    created_at: str,
) -> None:
    workspace = manager.ensure(
        collection_id="107b2669-e0ad-4abd-a66e-28305124edc8",
        collection_name="Outline Agent Dev Sandbox",
    )
    thread_workspace = manager.ensure_thread(
        workspace,
        thread_id=thread_id,
        document_id=document_id,
        document_title="Outline Agent Kickoff",
    )
    thread_workspace.record_observed_comment(
        comment_id=f"{thread_id}-comment",
        author_id="user-1",
        author_name="Gavin Gong",
        comment_text=comment_text,
        created_at=created_at,
        parent_comment_id=None,
        document_id=document_id,
        document_title="Outline Agent Kickoff",
        max_recent_comments=8,
        max_comment_chars=280,
    )
    thread_workspace.record_turn(
        comment_id=f"{thread_id}-comment",
        user_comment=comment_text,
        assistant_reply="Prior assistant reply.",
        document_id=document_id,
        document_title="Outline Agent Kickoff",
        max_recent_turns=6,
        max_turn_chars=280,
    )
    thread_workspace.session_path.write_text(
        (
            "# SESSION.md - Thread Session State\n\n"
            "This file stores durable thread-local state for a comment thread in this collection.\n\n"
            "## Thread Profile\n"
            f"- Thread ID: {thread_id}\n"
            f"- Root Comment ID: {thread_id}\n"
            f"- Document ID: {document_id}\n"
            "- Document Title: Outline Agent Kickoff\n\n"
            "## Session Summary\n\n"
            f"{summary}\n\n"
            "## Open Questions\n\n"
            "## Working Notes\n"
            "- Seeded by test.\n"
        ),
        encoding="utf-8",
    )


def test_comment_processor_can_run_multiple_tool_rounds_before_replying(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        outline_agent_user_id=AGENT_USER_ID,
        trigger_mode="mention",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=False,
        document_update_enabled=False,
        memory_update_enabled=False,
        thread_session_update_enabled=False,
        tool_use_enabled=True,
        tool_execution_max_rounds=3,
        progress_comment_enabled=False,
    )
    outline_client = DummyOutlineClient()
    reply_model_client = ReplyAfterMultiRoundToolUseModelClient()
    tool_model_client = MultiRoundToolPlanModelClient()
    processor = CommentProcessor(
        settings=settings,
        store=ProcessedEventStore(tmp_path / "processed.json"),
        outline_client=outline_client,
        model_client=reply_model_client,
        tool_model_client=tool_model_client,
        action_router_model_client=ActionRouterModelClient(),
    )

    payload = _load_fixture().model_dump()
    payload["payload"]["model"]["data"] = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "mention",
                        "attrs": {
                            "id": "some-node-id",
                            "type": "user",
                            "label": AGENT_USER_LABEL,
                            "actorId": "fafb5aee-1f7c-4bca-a524-eb99afa30ed0",
                            "modelId": AGENT_USER_ID,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            " please create a report file in your work dir, inspect it, "
                            "and then tell me what it contains"
                        ),
                    },
                ],
            }
        ],
    }

    result = asyncio.run(processor.handle(WebhookEnvelope.model_validate(payload)))

    assert result.action == "used-tools-and-replied"
    assert result.reason == "tools-executed-and-replied"
    assert result.tool_execution_preview is not None
    assert "round 1:" in result.tool_execution_preview.lower()
    assert "round 2:" in result.tool_execution_preview.lower()
    assert outline_client.posted[0]["text"] == (
        "Done — I created `report.txt`, read it back in a second tool round, "
        "and confirmed it contains `artifact-generated-in-round-one`."
    )
    assert len(tool_model_client.calls) == 3
    assert result.thread_workspace is not None
    work_dir = Path(result.thread_workspace) / "work"
    assert (work_dir / "report.txt").read_text(encoding="utf-8") == "artifact-generated-in-round-one\n"
    state = json.loads((Path(result.thread_workspace) / "state.json").read_text(encoding="utf-8"))
    assert len(state["recent_tool_runs"]) == 2
    assert state["recent_tool_runs"][0]["status"] == "applied"
    assert state["recent_tool_runs"][1]["status"] == "applied"


def test_comment_processor_stops_tool_loop_at_max_rounds_and_records_it(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        outline_agent_user_id=AGENT_USER_ID,
        trigger_mode="mention",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=False,
        document_update_enabled=False,
        memory_update_enabled=False,
        thread_session_update_enabled=False,
        tool_use_enabled=True,
        tool_execution_max_rounds=2,
        progress_comment_enabled=False,
    )
    outline_client = DummyOutlineClient()
    reply_model_client = ReplyAfterMaxRoundsToolUseModelClient()
    tool_model_client = MaxRoundsToolPlanModelClient()
    processor = CommentProcessor(
        settings=settings,
        store=ProcessedEventStore(tmp_path / "processed.json"),
        outline_client=outline_client,
        model_client=reply_model_client,
        tool_model_client=tool_model_client,
        action_router_model_client=ActionRouterModelClient(),
    )

    payload = _load_fixture().model_dump()
    payload["payload"]["model"]["data"] = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "mention",
                        "attrs": {
                            "id": "some-node-id",
                            "type": "user",
                            "label": AGENT_USER_LABEL,
                            "actorId": "fafb5aee-1f7c-4bca-a524-eb99afa30ed0",
                            "modelId": AGENT_USER_ID,
                        },
                    },
                    {
                        "type": "text",
                        "text": " please create a file in your work dir, inspect it, and keep going",
                    },
                ],
            }
        ],
    }

    result = asyncio.run(processor.handle(WebhookEnvelope.model_validate(payload)))

    assert result.action == "tool-attempted-and-replied"
    assert result.reason == "tool-loop-stopped-at-max-rounds-and-replied"
    assert result.tool_execution_preview is not None
    assert "round 1:" in result.tool_execution_preview.lower()
    assert "round 2:" in result.tool_execution_preview.lower()
    assert "round 3:" in result.tool_execution_preview.lower()
    assert "maximum local tool planning rounds (2)" in result.tool_execution_preview
    assert outline_client.posted[0]["text"] == (
        "I completed the first two local tool rounds, but stopped before any more "
        "because the configured tool-round limit was reached."
    )
    assert len(tool_model_client.calls) == 2
    assert result.thread_workspace is not None
    state = json.loads((Path(result.thread_workspace) / "state.json").read_text(encoding="utf-8"))
    assert len(state["recent_tool_runs"]) == 3
    assert state["recent_tool_runs"][0]["status"] == "applied"
    assert state["recent_tool_runs"][1]["status"] == "applied"
    assert state["recent_tool_runs"][2]["status"] == "stopped-max-rounds"


def test_comment_processor_uses_resolved_cross_thread_handoff_context(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        outline_agent_user_id=AGENT_USER_ID,
        trigger_mode="mention",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=True,
        document_update_enabled=True,
        tool_use_enabled=False,
        memory_update_enabled=False,
        thread_session_update_enabled=False,
        reaction_enabled=False,
    )
    manager = CollectionWorkspaceManager(settings.workspace_root)
    document_id = "d8119461-65ae-4218-9f70-514c06ca4d2a"
    _seed_thread_summary(
        manager,
        document_id=document_id,
        thread_id="prior-roadmap-thread",
        summary="Discussed the roadmap rollout plan for direct document editing.",
        comment_text="Let's plan the roadmap rollout for direct document editing.",
        created_at="2026-03-09T04:48:00.000Z",
    )

    outline_client = DummyOutlineClient()
    reply_model_client = ResolvedHandoffReplyModelClient()
    router_model_client = ActionRouterModelClient()
    processor = CommentProcessor(
        settings=settings,
        store=ProcessedEventStore(tmp_path / "processed.json"),
        outline_client=outline_client,
        model_client=reply_model_client,
        document_update_model_client=UnexpectedDocumentEditModelClient(),
        action_router_model_client=router_model_client,
    )

    payload = _load_fixture().model_dump()
    payload["payload"]["model"]["id"] = "handoff-comment"
    payload["payload"]["model"]["data"] = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "mention",
                        "attrs": {
                            "id": "some-node-id",
                            "type": "user",
                            "label": AGENT_USER_LABEL,
                            "actorId": "fafb5aee-1f7c-4bca-a524-eb99afa30ed0",
                            "modelId": AGENT_USER_ID,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            " based on the previous discussion thread about roadmap rollout, "
                            "please update the roadmap section directly"
                        ),
                    },
                ],
            }
        ],
    }

    result = asyncio.run(processor.handle(WebhookEnvelope.model_validate(payload)))

    assert result.action == "dry-run"
    assert result.reason == "reply-generated-without-posting"
    assert result.handoff_preview is not None
    assert "prior-roadmap-thread" in result.handoff_preview
    assert result.document_update_preview is None
    assert outline_client.updated_documents == []
    assert reply_model_client.calls


def test_comment_processor_requests_clarification_for_ambiguous_cross_thread_handoff(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        outline_agent_user_id=AGENT_USER_ID,
        trigger_mode="mention",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=True,
        document_update_enabled=True,
        tool_use_enabled=False,
        memory_update_enabled=False,
        thread_session_update_enabled=False,
        reaction_enabled=False,
    )
    manager = CollectionWorkspaceManager(settings.workspace_root)
    document_id = "d8119461-65ae-4218-9f70-514c06ca4d2a"
    _seed_thread_summary(
        manager,
        document_id=document_id,
        thread_id="thread-alpha",
        summary="Discussed a rollout plan for direct editing.",
        comment_text="Let's discuss the rollout plan.",
        created_at="2026-03-09T04:48:00.000Z",
    )
    _seed_thread_summary(
        manager,
        document_id=document_id,
        thread_id="thread-beta",
        summary="Discussed testing and validation steps.",
        comment_text="Let's discuss testing and validation.",
        created_at="2026-03-09T04:49:00.000Z",
    )

    outline_client = DummyOutlineClient()
    reply_model_client = AmbiguousHandoffReplyModelClient()
    router_model_client = ActionRouterModelClient()
    processor = CommentProcessor(
        settings=settings,
        store=ProcessedEventStore(tmp_path / "processed.json"),
        outline_client=outline_client,
        model_client=reply_model_client,
        document_update_model_client=UnexpectedDocumentEditModelClient(),
        action_router_model_client=router_model_client,
    )

    payload = _load_fixture().model_dump()
    payload["payload"]["model"]["id"] = "handoff-comment-2"
    payload["payload"]["model"]["data"] = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "mention",
                        "attrs": {
                            "id": "some-node-id",
                            "type": "user",
                            "label": AGENT_USER_LABEL,
                            "actorId": "fafb5aee-1f7c-4bca-a524-eb99afa30ed0",
                            "modelId": AGENT_USER_ID,
                        },
                    },
                    {
                        "type": "text",
                        "text": " continue the previous discussion thread and update the document directly",
                    },
                ],
            }
        ],
    }

    result = asyncio.run(processor.handle(WebhookEnvelope.model_validate(payload)))

    assert result.action == "dry-run"
    assert result.reason == "reply-generated-without-posting"
    assert result.handoff_preview is not None
    assert "thread-alpha" in result.handoff_preview
    assert "thread-beta" in result.handoff_preview
    assert result.document_update_preview is None
    assert outline_client.updated_documents == []
    assert reply_model_client.calls


def test_comment_processor_can_retrieve_other_same_document_comment_thread_on_demand(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        outline_agent_user_id=AGENT_USER_ID,
        trigger_mode="mention",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=True,
        document_update_enabled=False,
        tool_use_enabled=False,
        memory_update_enabled=False,
        thread_session_update_enabled=False,
        reaction_enabled=False,
        same_document_comment_lookup_enabled=True,
        same_document_comment_lookup_fetch_limit=20,
        same_document_comment_lookup_thread_limit=3,
        same_document_comment_lookup_comment_limit=4,
    )
    outline_client = DummyOutlineClient()
    document_id = "d8119461-65ae-4218-9f70-514c06ca4d2a"
    outline_client.comment_items = [
        _make_outline_comment(
            comment_id="current-thread-root",
            document_id=document_id,
            parent_comment_id=None,
            author_name="Gavin Gong",
            created_at="2026-03-11T10:00:00.000Z",
            text="Current thread root.",
        ),
        _make_outline_comment(
            comment_id="thread-pdf",
            document_id=document_id,
            parent_comment_id=None,
            author_name="Gavin Gong",
            created_at="2026-03-10T10:00:00.000Z",
            text="PDF build is failing on CI because the fonts package is missing.",
        ),
        _make_outline_comment(
            comment_id="thread-pdf-r1",
            document_id=document_id,
            parent_comment_id="thread-pdf",
            author_name=AGENT_USER_LABEL,
            created_at="2026-03-10T10:05:00.000Z",
            text="We should upload the generated artifact link in the final reply.",
        ),
        _make_outline_comment(
            comment_id="thread-edit",
            document_id=document_id,
            parent_comment_id=None,
            author_name="Gavin Gong",
            created_at="2026-03-09T10:00:00.000Z",
            text="Discuss the direct document editing approval boundary.",
        ),
    ]
    reply_model_client = SameDocumentResolvedReplyModelClient()
    router_model_client = ActionRouterModelClient()
    processor = CommentProcessor(
        settings=settings,
        store=ProcessedEventStore(tmp_path / "processed.json"),
        outline_client=outline_client,
        model_client=reply_model_client,
        action_router_model_client=router_model_client,
    )

    payload = _load_fixture().model_dump()
    payload["payload"]["model"]["id"] = "current-thread-root"
    payload["payload"]["model"]["documentId"] = document_id
    payload["payload"]["model"]["parentCommentId"] = None
    payload["payload"]["model"]["data"] = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "mention",
                        "attrs": {
                            "id": "some-node-id",
                            "type": "user",
                            "label": AGENT_USER_LABEL,
                            "actorId": "fafb5aee-1f7c-4bca-a524-eb99afa30ed0",
                            "modelId": AGENT_USER_ID,
                        },
                    },
                    {
                        "type": "text",
                        "text": " can you read the earlier PDF comments in this document and summarize them?",
                    },
                ],
            }
        ],
    }

    result = asyncio.run(processor.handle(WebhookEnvelope.model_validate(payload)))

    assert result.action == "dry-run"
    assert result.reason == "reply-generated-without-posting"
    assert result.same_document_comment_preview is not None
    assert "thread-pdf" in result.same_document_comment_preview
    assert "thread-edit" not in result.same_document_comment_preview
    assert reply_model_client.calls


def test_comment_processor_requests_clarification_for_ambiguous_same_document_comment_lookup(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        outline_agent_user_id=AGENT_USER_ID,
        trigger_mode="mention",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=True,
        document_update_enabled=False,
        tool_use_enabled=False,
        memory_update_enabled=False,
        thread_session_update_enabled=False,
        reaction_enabled=False,
        same_document_comment_lookup_enabled=True,
        same_document_comment_lookup_fetch_limit=20,
        same_document_comment_lookup_thread_limit=3,
    )
    outline_client = DummyOutlineClient()
    document_id = "d8119461-65ae-4218-9f70-514c06ca4d2a"
    outline_client.comment_items = [
        _make_outline_comment(
            comment_id="current-thread-root",
            document_id=document_id,
            parent_comment_id=None,
            author_name="Gavin Gong",
            created_at="2026-03-11T10:00:00.000Z",
            text="Current thread root.",
        ),
        _make_outline_comment(
            comment_id="thread-alpha",
            document_id=document_id,
            parent_comment_id=None,
            author_name="Gavin Gong",
            created_at="2026-03-10T10:00:00.000Z",
            text="We discussed rollout sequencing for the roadmap.",
        ),
        _make_outline_comment(
            comment_id="thread-beta",
            document_id=document_id,
            parent_comment_id=None,
            author_name="Gavin Gong",
            created_at="2026-03-10T09:00:00.000Z",
            text="We discussed how to validate generated artifacts before upload.",
        ),
    ]
    reply_model_client = SameDocumentAmbiguousReplyModelClient()
    router_model_client = ActionRouterModelClient()
    processor = CommentProcessor(
        settings=settings,
        store=ProcessedEventStore(tmp_path / "processed.json"),
        outline_client=outline_client,
        model_client=reply_model_client,
        action_router_model_client=router_model_client,
    )

    payload = _load_fixture().model_dump()
    payload["payload"]["model"]["id"] = "current-thread-root"
    payload["payload"]["model"]["documentId"] = document_id
    payload["payload"]["model"]["parentCommentId"] = None
    payload["payload"]["model"]["data"] = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "mention",
                        "attrs": {
                            "id": "some-node-id",
                            "type": "user",
                            "label": AGENT_USER_LABEL,
                            "actorId": "fafb5aee-1f7c-4bca-a524-eb99afa30ed0",
                            "modelId": AGENT_USER_ID,
                        },
                    },
                    {
                        "type": "text",
                        "text": " can you look at the previous comments in this document?",
                    },
                ],
            }
        ],
    }

    result = asyncio.run(processor.handle(WebhookEnvelope.model_validate(payload)))

    assert result.action == "dry-run"
    assert result.reason == "reply-generated-without-posting"
    assert result.same_document_comment_preview is not None
    assert "thread-alpha" in result.same_document_comment_preview
    assert "thread-beta" in result.same_document_comment_preview
    assert reply_model_client.calls


def _make_outline_comment(
    *,
    comment_id: str,
    document_id: str,
    parent_comment_id: str | None,
    author_name: str,
    created_at: str,
    text: str,
) -> OutlineComment:
    return OutlineComment(
        id=comment_id,
        document_id=document_id,
        parent_comment_id=parent_comment_id,
        created_by_id=author_name.lower().replace(" ", "-"),
        created_by_name=author_name,
        created_at=created_at,
        data={
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": text}],
                }
            ],
        },
    )
