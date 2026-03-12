from __future__ import annotations

import asyncio
from pathlib import Path

from outline_agent.clients.outline_models import OutlineCollection, OutlineComment, OutlineDocument
from outline_agent.core.config import AppSettings
from outline_agent.planning.execution_loop import UnifiedExecutionLoop
from outline_agent.planning.tool_plan_schema import UnifiedToolPlan, UnifiedToolPlanStep
from outline_agent.state.workspace import CollectionWorkspaceManager
from outline_agent.tools import ToolContext, ToolRegistry, build_workspace_tools
from outline_agent.utils.attachment_context import (
    AttachmentContextItem,
    collect_attachment_context,
    format_attachment_context_for_prompt,
    repair_download_attachment_args,
)


class DummyOutlineClient:
    def __init__(self) -> None:
        self.downloads: list[dict[str, str]] = []

    async def download_attachment(self, url_or_path: str, file_path: Path) -> dict:
        self.downloads.append({"url_or_path": url_or_path, "file_path": str(file_path)})
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("downloaded", encoding="utf-8")
        return {
            "ok": True,
            "url": url_or_path,
            "file_path": str(file_path),
            "size": file_path.stat().st_size,
            "content_type": "application/octet-stream",
        }


def test_collect_attachment_context_prefers_current_comment_and_document_refs() -> None:
    current = OutlineComment(
        id="comment-current",
        document_id="doc-1",
        parent_comment_id=None,
        created_by_id="user-1",
        created_by_name="Alice",
        created_at="2026-03-12T10:00:00Z",
        data={
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "看这张图"},
                        {"type": "image", "attrs": {"src": "/api/attachments.redirect?id=image-123", "alt": "diagram"}},
                    ],
                }
            ],
        },
    )
    earlier = OutlineComment(
        id="comment-earlier",
        document_id="doc-1",
        parent_comment_id="comment-current",
        created_by_id="user-2",
        created_by_name="Bob",
        created_at="2026-03-12T09:00:00Z",
        data={
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "paper.pdf",
                            "marks": [
                                {
                                    "type": "link",
                                    "attrs": {
                                        "href": "https://outline.example/api/attachments.redirect?id=paper-thread"
                                    },
                                }
                            ],
                        }
                    ],
                }
            ],
        },
    )
    document = OutlineDocument(
        id="doc-1",
        title="Doc",
        collection_id="collection-1",
        url=None,
        text="[report.pdf](https://outline.example/api/attachments.redirect?id=paper-doc)",
    )

    items = collect_attachment_context(
        document=document,
        comments=[earlier, current],
        current_comment_id="comment-current",
    )

    assert [item.origin for item in items] == ["current_comment", "thread_comment", "document"]
    assert items[0].source_url == "/api/attachments.redirect?id=image-123"
    assert items[0].suggested_path.endswith("diagram.img")
    assert items[1].source_url == "https://outline.example/api/attachments.redirect?id=paper-thread"
    assert items[2].suggested_path.endswith("report.pdf")

    prompt = format_attachment_context_for_prompt(items)
    assert prompt is not None
    assert "source_url=/api/attachments.redirect?id=image-123" in prompt
    assert "path=attachments/current/diagram.img" in prompt
    assert "label=report.pdf" in prompt


def test_repair_download_attachment_args_uses_single_current_comment_candidate() -> None:
    items = [
        AttachmentContextItem(
            source_url="/api/attachments.redirect?id=image-123",
            suggested_path="attachments/current/diagram.img",
            origin="current_comment",
            kind="image",
            label="diagram",
            comment_id="comment-current",
            author_name="Alice",
        )
    ]

    repaired = repair_download_attachment_args(
        "download_attachment",
        {},
        {"available_attachment_context": items},
    )

    assert repaired == {
        "source_url": "/api/attachments.redirect?id=image-123",
        "path": "attachments/current/diagram.img",
    }


def test_execution_loop_repairs_download_attachment_path_from_attachment_context(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register_many(build_workspace_tools())
    outline_client = DummyOutlineClient()
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=False,
    )
    manager = CollectionWorkspaceManager(settings.workspace_root)
    workspace = manager.ensure("collection-1", "Test")
    thread_workspace = manager.ensure_thread(workspace, thread_id="thread-1", document_id="doc-1", document_title="Doc")
    context = ToolContext(
        settings=settings,
        outline_client=outline_client,
        work_dir=thread_workspace.work_dir,
        collection=OutlineCollection(id="collection-1", name="Test", description=None, url=None),
        document=OutlineDocument(id="doc-1", title="Doc", collection_id="collection-1", url=None, text=""),
        extra={
            "available_attachment_context": [
                AttachmentContextItem(
                    source_url="/api/attachments.redirect?id=paper-1",
                    suggested_path="attachments/current/paper.pdf",
                    origin="current_comment",
                    kind="attachment",
                    label="paper.pdf",
                    comment_id="comment-1",
                    author_name="Alice",
                )
            ]
        },
    )
    loop = UnifiedExecutionLoop(registry, max_steps=4)
    plan = UnifiedToolPlan(
        should_act=True,
        steps=[
            UnifiedToolPlanStep(
                tool="download_attachment",
                args={"source_url": "/api/attachments.redirect?id=paper-1"},
            )
        ],
    )

    summary = asyncio.run(loop.execute(plan, context))

    assert summary.status == "applied"
    assert outline_client.downloads == [
        {
            "url_or_path": "/api/attachments.redirect?id=paper-1",
            "file_path": str(thread_workspace.work_dir / "attachments" / "current" / "paper.pdf"),
        }
    ]
