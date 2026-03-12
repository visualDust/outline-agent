from __future__ import annotations

from pathlib import Path

from outline_agent.clients.outline_models import OutlineComment
from outline_agent.core.config import AppSettings
from outline_agent.state.workspace import CollectionWorkspaceManager
from outline_agent.tools import ToolContext
from outline_agent.tools.workspace_tools import GetThreadHistoryTool


def _comment(comment_id: str, thread_id: str, text: str, created_at: str, author: str = "user") -> OutlineComment:
    return OutlineComment(
        id=comment_id,
        document_id="doc-1",
        parent_comment_id=None if comment_id == thread_id else thread_id,
        created_by_id=author,
        created_by_name=author,
        created_at=created_at,
        data={
            "type": "doc",
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
        },
    )


def test_collection_workspace_is_shared_across_threads(tmp_path: Path) -> None:
    manager = CollectionWorkspaceManager(tmp_path)
    workspace = manager.ensure("collection-1", "Demo")
    thread_a = manager.ensure_thread(workspace, thread_id="thread-a", document_id="doc-1", document_title="Doc")
    thread_b = manager.ensure_thread(workspace, thread_id="thread-b", document_id="doc-1", document_title="Doc")

    assert workspace.workspace_dir.exists()
    assert not (thread_a.root_dir / "work").exists()
    assert not (thread_b.root_dir / "work").exists()
    assert thread_a.comments_path.exists()
    assert thread_b.comments_path.exists()


def test_thread_comment_context_truncates_with_root_and_tail(tmp_path: Path) -> None:
    manager = CollectionWorkspaceManager(tmp_path)
    workspace = manager.ensure("collection-1", "Demo")
    thread = manager.ensure_thread(workspace, thread_id="thread-1", document_id="doc-1", document_title="Doc")
    comments = [
        _comment("thread-1", "thread-1", "root task and requirements", "2026-03-12T10:00:00Z"),
        _comment("c2", "thread-1", "middle detail one", "2026-03-12T10:01:00Z"),
        _comment("c3", "thread-1", "middle detail two", "2026-03-12T10:02:00Z"),
        _comment("c4", "thread-1", "latest answer", "2026-03-12T10:03:00Z"),
    ]
    thread.sync_transcript_from_comments(
        document_id="doc-1",
        document_title="Doc",
        comments=comments,
        max_recent_comments=8,
        max_comment_chars=280,
    )

    rendered = thread.build_comment_context(
        current_comment_id="c4",
        max_full_thread_chars=80,
        tail_comment_count=1,
        summary_max_chars=120,
    )

    assert "Earlier comment history was truncated" in rendered
    assert "Root comment:" in rendered
    assert "latest 1 comments".lower() in rendered.lower()
    assert "get_thread_history" in rendered


def test_get_thread_history_tool_reads_exact_range(tmp_path: Path) -> None:
    manager = CollectionWorkspaceManager(tmp_path)
    workspace = manager.ensure("collection-1", "Demo")
    thread = manager.ensure_thread(workspace, thread_id="thread-1", document_id="doc-1", document_title="Doc")
    comments = [
        _comment("thread-1", "thread-1", "root", "2026-03-12T10:00:00Z"),
        _comment("c2", "thread-1", "two", "2026-03-12T10:01:00Z"),
        _comment("c3", "thread-1", "three", "2026-03-12T10:02:00Z"),
    ]
    thread.sync_transcript_from_comments(
        document_id="doc-1",
        document_title="Doc",
        comments=comments,
        max_recent_comments=8,
        max_comment_chars=280,
    )
    tool = GetThreadHistoryTool()
    context = ToolContext(
        settings=AppSettings(),
        work_dir=workspace.workspace_dir,
        extra={"thread_workspace": thread, "current_comment_id": "c3"},
    )

    result = __import__("asyncio").run(tool.run({"mode": "range", "start_index": 1, "end_index": 2}, context))

    assert result.ok is True
    assert result.data["comment_count"] == 2
    assert "root" in (result.data["text"] or "")
    assert "two" in (result.data["text"] or "")
