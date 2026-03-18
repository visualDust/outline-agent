from __future__ import annotations

import asyncio
from pathlib import Path

from outline_agent.clients.outline_exceptions import OutlineClientError
from outline_agent.clients.outline_models import OutlineCollection, OutlineComment, OutlineDocument
from outline_agent.core.config import AppSettings
from outline_agent.doctor.workspace_sync import (
    apply_workspace_sync_repair_plan,
    build_workspace_sync_repair_plan,
    format_workspace_sync_repair_plan_text,
    format_workspace_sync_report_text,
    run_workspace_sync_diagnostics,
)
from outline_agent.state.workspace import CollectionWorkspaceManager


class FakeOutlineClient:
    def __init__(
        self,
        *,
        existing_collections: set[str] | None = None,
        existing_documents: set[str] | None = None,
        comments_by_document: dict[str, list[OutlineComment]] | None = None,
        inaccessible_collections: set[str] | None = None,
        inaccessible_documents: set[str] | None = None,
        inaccessible_comment_documents: set[str] | None = None,
        deleted_documents: set[str] | None = None,
    ) -> None:
        self.existing_collections = existing_collections or set()
        self.existing_documents = existing_documents or set()
        self.comments_by_document = comments_by_document or {}
        self.inaccessible_collections = inaccessible_collections or set()
        self.inaccessible_documents = inaccessible_documents or set()
        self.inaccessible_comment_documents = inaccessible_comment_documents or set()
        self.deleted_documents = deleted_documents or set()
        self.comments_list_calls: list[tuple[str, int, int]] = []

    async def collection_info(self, collection_id: str) -> OutlineCollection:
        if collection_id in self.inaccessible_collections:
            raise OutlineClientError("Outline API error 403: Authorization error")
        if collection_id not in self.existing_collections:
            raise OutlineClientError("Outline API error 404: Collection not found")
        return OutlineCollection(id=collection_id, name="Demo", description=None, url=None)

    async def document_info(self, document_id: str) -> OutlineDocument:
        if document_id in self.inaccessible_documents:
            raise OutlineClientError("Outline API error 403: Authorization error")
        if document_id not in self.existing_documents:
            raise OutlineClientError("Outline API error 404: Document not found")
        return OutlineDocument(
            id=document_id,
            title="Doc",
            collection_id="collection-1",
            url=None,
            text=None,
            deleted_at="2026-03-17T23:39:04.221Z" if document_id in self.deleted_documents else None,
        )

    async def comments_list(self, document_id: str, limit: int = 25, offset: int = 0) -> list[OutlineComment]:
        self.comments_list_calls.append((document_id, limit, offset))
        if document_id in self.inaccessible_comment_documents:
            raise OutlineClientError("Outline API error 403: Authorization error")
        if document_id not in self.existing_documents:
            raise OutlineClientError("Outline API error 404: Document not found")
        comments = self.comments_by_document.get(document_id, [])
        return comments[offset : offset + limit]



def _settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        outline_api_base_url="https://outline.example.com/api",
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        comment_list_limit=50,
    )



def _comment(comment_id: str, document_id: str, parent_comment_id: str | None = None) -> OutlineComment:
    return OutlineComment(
        id=comment_id,
        document_id=document_id,
        parent_comment_id=parent_comment_id,
        created_by_id="user-1",
        created_by_name="User",
        created_at="2026-03-17T12:00:00Z",
        data={"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": comment_id}]}]},
    )



def test_workspace_sync_coarse_reports_missing_remote_and_local_drift(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    manager = CollectionWorkspaceManager(settings.workspace_root)
    workspace = manager.ensure("collection-1", "Demo")

    manager.ensure_document(
        workspace,
        document_id="doc-missing-remote",
        document_title="Missing Remote",
    )
    archived_document = manager.ensure_document(
        workspace,
        document_id="doc-archived",
        document_title="Archived",
    )
    manager.archive_document(workspace, archived_document, reason="test")

    orphan_thread = manager.ensure_thread(
        workspace,
        thread_id="thread-archived-parent",
        document_id="doc-archived",
        document_title="Archived",
    )
    orphan_thread.comments_path.unlink()

    outline_client = FakeOutlineClient(
        existing_collections={"collection-1"},
        existing_documents=set(),
    )

    report = asyncio.run(
        run_workspace_sync_diagnostics(
            settings=settings,
            outline_client=outline_client,
            depth="coarse",
        )
    )

    kinds = {finding.kind for finding in report.findings}
    assert report.exit_code() == 1
    assert report.checked_collections == 1
    assert report.checked_documents == 1
    assert report.checked_threads == 1
    assert "missing_remote_document" in kinds
    assert "archived_parent_with_active_child" in kinds
    assert "missing_local_metadata" in kinds

    text = format_workspace_sync_report_text(report)
    assert "missing_remote_document" in text
    assert "archived_parent_with_active_child" in text

    payload = report.to_dict()
    assert payload["finding_counts"]["missing_remote_document"] == 1


def test_workspace_sync_treats_deleted_remote_document_as_missing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    manager = CollectionWorkspaceManager(settings.workspace_root)
    workspace = manager.ensure("collection-1", "Demo")
    manager.ensure_document(workspace, document_id="doc-deleted", document_title="Deleted")

    outline_client = FakeOutlineClient(
        existing_collections={"collection-1"},
        existing_documents={"doc-deleted"},
        deleted_documents={"doc-deleted"},
    )

    report = asyncio.run(
        run_workspace_sync_diagnostics(
            settings=settings,
            outline_client=outline_client,
            depth="coarse",
        )
    )

    findings = [finding for finding in report.findings if finding.document_id == "doc-deleted"]
    assert len(findings) == 1
    assert findings[0].kind == "missing_remote_document"
    assert "deleted" in findings[0].message.lower()



def test_workspace_sync_deep_reports_orphaned_threads_and_batches_by_document(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    manager = CollectionWorkspaceManager(settings.workspace_root)
    workspace = manager.ensure("collection-1", "Demo")
    manager.ensure_document(workspace, document_id="doc-1", document_title="Doc")

    thread_orphaned = manager.ensure_thread(
        workspace,
        thread_id="thread-missing-root",
        document_id="doc-1",
        document_title="Doc",
    )
    thread_deleted = manager.ensure_thread(
        workspace,
        thread_id="thread-deleted",
        document_id="doc-1",
        document_title="Doc",
    )

    thread_orphaned.sync_transcript_from_comments(
        document_id="doc-1",
        document_title="Doc",
        comments=[_comment("thread-missing-root", "doc-1")],
        max_recent_comments=5,
        max_comment_chars=200,
    )
    thread_deleted.mark_deleted(document_id="doc-1", document_title="Doc")

    outline_client = FakeOutlineClient(
        existing_collections={"collection-1"},
        existing_documents={"doc-1"},
        comments_by_document={
            "doc-1": [
                _comment("other-root", "doc-1"),
                _comment("other-reply", "doc-1", parent_comment_id="other-root"),
            ]
        },
    )

    report = asyncio.run(
        run_workspace_sync_diagnostics(
            settings=settings,
            outline_client=outline_client,
            depth="deep",
        )
    )

    kinds = [finding.kind for finding in report.findings]
    assert report.exit_code() == 1
    assert "orphaned_active_thread" in kinds
    assert "deleted_thread_not_archived" in kinds
    assert outline_client.comments_list_calls == [("doc-1", 50, 0)]


def test_workspace_sync_repair_plan_and_apply_archive_safe_local_state(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    manager = CollectionWorkspaceManager(settings.workspace_root)
    workspace = manager.ensure("collection-1", "Demo")
    manager.ensure_document(
        workspace,
        document_id="doc-missing-remote",
        document_title="Missing Remote",
    )
    thread_workspace = manager.ensure_thread(
        workspace,
        thread_id="thread-missing-root",
        document_id="doc-1",
        document_title="Doc",
    )
    manager.ensure_document(workspace, document_id="doc-1", document_title="Doc")
    thread_workspace.sync_transcript_from_comments(
        document_id="doc-1",
        document_title="Doc",
        comments=[_comment("thread-missing-root", "doc-1")],
        max_recent_comments=5,
        max_comment_chars=200,
    )
    broken_document = manager.ensure_document(
        workspace,
        document_id="doc-broken-metadata",
        document_title="Broken Metadata",
    )
    broken_document.memory_path.unlink()

    outline_client = FakeOutlineClient(
        existing_collections={"collection-1"},
        existing_documents={"doc-1", "doc-broken-metadata"},
        comments_by_document={"doc-1": [_comment("different-root", "doc-1")]},
    )

    report = asyncio.run(
        run_workspace_sync_diagnostics(
            settings=settings,
            outline_client=outline_client,
            depth="deep",
        )
    )
    plan = build_workspace_sync_repair_plan(settings=settings, report=report)

    assert [action.operation for action in plan.actions] == [
        "archive_document_workspace",
        "archive_thread_workspace",
    ]
    assert [finding.kind for finding in plan.skipped_findings] == ["missing_local_metadata"]

    plan_text = format_workspace_sync_repair_plan_text(plan)
    assert "archive_document_workspace" in plan_text
    assert "skipped findings" in plan_text

    repair_run = apply_workspace_sync_repair_plan(settings=settings, plan=plan)

    assert repair_run.has_failures is False
    assert [result.status for result in repair_run.results] == ["applied", "applied"]
    assert manager.find_document(workspace, document_id="doc-missing-remote") is None
    assert manager.find_archived_document_dir(workspace, document_id="doc-missing-remote") is not None
    assert not thread_workspace.root_dir.exists()
    assert any(item.thread_id == "thread-missing-root" for item in manager.list_archived_threads(workspace))


def test_workspace_sync_reports_inaccessible_collection_and_continues(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    manager = CollectionWorkspaceManager(settings.workspace_root)
    inaccessible_workspace = manager.ensure("collection-blocked", "Blocked")
    accessible_workspace = manager.ensure("collection-ok", "Allowed")
    manager.ensure_document(accessible_workspace, document_id="doc-ok", document_title="Allowed Doc")

    outline_client = FakeOutlineClient(
        existing_collections={"collection-ok"},
        existing_documents={"doc-ok"},
        inaccessible_collections={"collection-blocked"},
    )

    report = asyncio.run(
        run_workspace_sync_diagnostics(
            settings=settings,
            outline_client=outline_client,
            depth="coarse",
        )
    )

    kinds = [finding.kind for finding in report.findings]
    assert report.checked_collections == 2
    assert "inaccessible_remote_collection" in kinds
    assert "missing_remote_document" not in kinds
    assert inaccessible_workspace.collection_id == "collection-blocked"


def test_workspace_sync_reports_inaccessible_document_and_comments_without_aborting(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    manager = CollectionWorkspaceManager(settings.workspace_root)
    workspace = manager.ensure("collection-1", "Demo")
    manager.ensure_document(workspace, document_id="doc-blocked", document_title="Blocked")
    manager.ensure_document(workspace, document_id="doc-comments-blocked", document_title="Comments Blocked")

    thread = manager.ensure_thread(
        workspace,
        thread_id="thread-comments-blocked",
        document_id="doc-comments-blocked",
        document_title="Comments Blocked",
    )
    thread.sync_transcript_from_comments(
        document_id="doc-comments-blocked",
        document_title="Comments Blocked",
        comments=[_comment("thread-comments-blocked", "doc-comments-blocked")],
        max_recent_comments=5,
        max_comment_chars=200,
    )

    outline_client = FakeOutlineClient(
        existing_collections={"collection-1"},
        existing_documents={"doc-comments-blocked"},
        inaccessible_documents={"doc-blocked"},
        inaccessible_comment_documents={"doc-comments-blocked"},
    )

    report = asyncio.run(
        run_workspace_sync_diagnostics(
            settings=settings,
            outline_client=outline_client,
            depth="deep",
        )
    )

    kinds = [finding.kind for finding in report.findings]
    assert "inaccessible_remote_document" in kinds
    assert "inaccessible_remote_comments" in kinds
    assert "orphaned_active_thread" not in kinds


def test_repair_plan_skips_inaccessible_by_default_and_can_include_them(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    manager = CollectionWorkspaceManager(settings.workspace_root)
    blocked_workspace = manager.ensure("collection-blocked", "Blocked")
    manager.ensure_document(blocked_workspace, document_id="doc-blocked", document_title="Blocked")

    outline_client = FakeOutlineClient(
        inaccessible_collections={"collection-blocked"},
    )

    report = asyncio.run(
        run_workspace_sync_diagnostics(
            settings=settings,
            outline_client=outline_client,
            depth="coarse",
        )
    )

    default_plan = build_workspace_sync_repair_plan(settings=settings, report=report)
    assert default_plan.actions == []
    assert [finding.kind for finding in default_plan.skipped_findings] == ["inaccessible_remote_collection"]

    include_plan = build_workspace_sync_repair_plan(settings=settings, report=report, include_inaccessible=True)
    assert [action.operation for action in include_plan.actions] == ["archive_collection_workspace"]
