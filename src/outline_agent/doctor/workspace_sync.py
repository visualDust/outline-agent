from __future__ import annotations

import asyncio
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..clients.outline_exceptions import OutlineClientError
from ..clients.outline_models import OutlineComment
from ..core.config import AppSettings
from ..core.logging import logger
from ..state.workspace import CollectionWorkspace, CollectionWorkspaceManager, DocumentWorkspace, ThreadWorkspace

WorkspaceSyncDepth = Literal["coarse", "deep"]


@dataclass(frozen=True)
class DiagnosticFinding:
    kind: str
    severity: str
    collection_id: str | None
    document_id: str | None
    thread_id: str | None
    local_path: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WorkspaceSyncReport:
    depth: WorkspaceSyncDepth
    collection_filter: str | None = None
    document_filter: str | None = None
    checked_collections: int = 0
    checked_documents: int = 0
    checked_threads: int = 0
    findings: list[DiagnosticFinding] = field(default_factory=list)

    def exit_code(self) -> int:
        return 1 if self.findings else 0

    def finding_counts(self) -> dict[str, int]:
        counts = Counter(finding.kind for finding in self.findings)
        return dict(sorted(counts.items()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "depth": self.depth,
            "collection_filter": self.collection_filter,
            "document_filter": self.document_filter,
            "checked": {
                "collections": self.checked_collections,
                "documents": self.checked_documents,
                "threads": self.checked_threads,
            },
            "finding_counts": self.finding_counts(),
            "findings": [finding.to_dict() for finding in _sorted_findings(self.findings)],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


@dataclass(frozen=True)
class RepairAction:
    operation: str
    reason: str
    collection_id: str | None
    document_id: str | None
    thread_id: str | None
    local_path: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WorkspaceSyncRepairPlan:
    actions: list[RepairAction] = field(default_factory=list)
    skipped_findings: list[DiagnosticFinding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_counts": self.action_counts(),
            "actions": [action.to_dict() for action in self.actions],
            "skipped_findings": [finding.to_dict() for finding in _sorted_findings(self.skipped_findings)],
        }

    def action_counts(self) -> dict[str, int]:
        counts = Counter(action.operation for action in self.actions)
        return dict(sorted(counts.items()))


@dataclass(frozen=True)
class RepairActionResult:
    action: RepairAction
    status: str
    message: str
    destination_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["action"] = self.action.to_dict()
        return payload


@dataclass
class WorkspaceSyncRepairRun:
    results: list[RepairActionResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "counts": self.counts(),
            "results": [result.to_dict() for result in self.results],
        }

    def counts(self) -> dict[str, int]:
        counts = Counter(result.status for result in self.results)
        return dict(sorted(counts.items()))

    @property
    def has_failures(self) -> bool:
        return any(result.status == "failed" for result in self.results)


@dataclass(frozen=True)
class _CollectionInventory:
    workspace: CollectionWorkspace
    active_documents: list[DocumentWorkspace]
    archived_documents: list[DocumentWorkspace]
    active_threads: list[ThreadWorkspace]
    archived_threads: list[ThreadWorkspace]


async def run_workspace_sync_diagnostics(
    *,
    settings: AppSettings,
    outline_client,
    depth: WorkspaceSyncDepth = "coarse",
    concurrency: int = 5,
    collection_id: str | None = None,
    document_id: str | None = None,
) -> WorkspaceSyncReport:
    depth = "deep" if depth == "deep" else "coarse"
    concurrency = max(1, concurrency)
    manager = CollectionWorkspaceManager(settings.workspace_root)
    inventories = _build_inventories(manager, collection_id=collection_id, document_id=document_id)
    report = WorkspaceSyncReport(
        depth=depth,
        collection_filter=collection_id,
        document_filter=document_id,
        checked_collections=len(inventories),
        checked_documents=sum(len(item.active_documents) for item in inventories),
        checked_threads=sum(len(item.active_threads) for item in inventories),
    )

    logger.info(
        "Doctor workspace-sync inventory prepared: depth={}, concurrency={}, collections={}, documents={}, threads={}",
        depth,
        concurrency,
        report.checked_collections,
        report.checked_documents,
        report.checked_threads,
    )

    findings: list[DiagnosticFinding] = []
    for inventory in inventories:
        findings.extend(_check_local_document_consistency(inventory))
        findings.extend(_check_local_thread_consistency(inventory))

    findings.extend(
        await _check_remote_collections(
            outline_client=outline_client,
            inventories=inventories,
            concurrency=concurrency,
        )
    )
    inaccessible_collection_ids = {
        finding.collection_id
        for finding in findings
        if finding.kind == "inaccessible_remote_collection" and finding.collection_id
    }
    findings.extend(
        await _check_remote_documents(
            outline_client=outline_client,
            inventories=inventories,
            concurrency=concurrency,
            skipped_collection_ids=inaccessible_collection_ids,
        )
    )

    if depth == "deep":
        findings.extend(
            await _check_remote_threads(
                settings=settings,
                outline_client=outline_client,
                inventories=inventories,
                existing_findings=findings,
                concurrency=concurrency,
                skipped_collection_ids=inaccessible_collection_ids,
            )
        )

    report.findings = _dedupe_findings(findings)
    logger.info(
        "Doctor workspace-sync finished: depth={}, findings={}",
        depth,
        len(report.findings),
    )
    return report



def build_workspace_sync_repair_plan(
    *,
    settings: AppSettings,
    report: WorkspaceSyncReport,
    include_inaccessible: bool = False,
) -> WorkspaceSyncRepairPlan:
    del settings
    plan = WorkspaceSyncRepairPlan()
    missing_collection_ids = {
        finding.collection_id
        for finding in report.findings
        if finding.kind == "missing_remote_collection" and finding.collection_id
    }
    missing_document_ids = {
        (finding.collection_id, finding.document_id)
        for finding in report.findings
        if finding.kind == "missing_remote_document" and finding.collection_id and finding.document_id
    }
    planned_keys: set[tuple[str, str | None, str | None, str | None, str]] = set()

    for finding in _sorted_findings(report.findings):
        if finding.kind == "missing_remote_collection" and finding.collection_id:
            _append_repair_action(
                plan.actions,
                planned_keys,
                RepairAction(
                    operation="archive_collection_workspace",
                    reason=finding.kind,
                    collection_id=finding.collection_id,
                    document_id=None,
                    thread_id=None,
                    local_path=finding.local_path,
                    message="Archive the local collection workspace because the remote collection is gone.",
                ),
            )
            continue

        if include_inaccessible and finding.kind == "inaccessible_remote_collection" and finding.collection_id:
            _append_repair_action(
                plan.actions,
                planned_keys,
                RepairAction(
                    operation="archive_collection_workspace",
                    reason=finding.kind,
                    collection_id=finding.collection_id,
                    document_id=None,
                    thread_id=None,
                    local_path=finding.local_path,
                    message="Archive the local collection workspace even though the remote collection is inaccessible.",
                ),
            )
            continue

        if finding.kind == "missing_remote_document" and finding.collection_id and finding.document_id:
            if finding.collection_id in missing_collection_ids:
                continue
            _append_repair_action(
                plan.actions,
                planned_keys,
                RepairAction(
                    operation="archive_document_workspace",
                    reason=finding.kind,
                    collection_id=finding.collection_id,
                    document_id=finding.document_id,
                    thread_id=None,
                    local_path=finding.local_path,
                    message="Archive the local document workspace because the remote document is gone.",
                ),
            )
            continue

        if (
            include_inaccessible
            and finding.kind == "inaccessible_remote_document"
            and finding.collection_id
            and finding.document_id
        ):
            if finding.collection_id in missing_collection_ids:
                continue
            _append_repair_action(
                plan.actions,
                planned_keys,
                RepairAction(
                    operation="archive_document_workspace",
                    reason=finding.kind,
                    collection_id=finding.collection_id,
                    document_id=finding.document_id,
                    thread_id=None,
                    local_path=finding.local_path,
                    message="Archive the local document workspace even though the remote document is inaccessible.",
                ),
            )
            continue

        if finding.kind in {
            "orphaned_active_thread",
            "deleted_thread_not_archived",
            "archived_parent_with_active_child",
        } and finding.collection_id and finding.thread_id:
            if finding.collection_id in missing_collection_ids:
                continue
            if finding.document_id and (finding.collection_id, finding.document_id) in missing_document_ids:
                continue
            _append_repair_action(
                plan.actions,
                planned_keys,
                RepairAction(
                    operation="archive_thread_workspace",
                    reason=finding.kind,
                    collection_id=finding.collection_id,
                    document_id=finding.document_id,
                    thread_id=finding.thread_id,
                    local_path=finding.local_path,
                    message="Archive the local thread workspace because it is no longer trustworthy.",
                ),
            )
            continue

        if include_inaccessible and finding.kind == "inaccessible_remote_comments" and finding.collection_id:
            plan.skipped_findings.append(finding)
            continue

        plan.skipped_findings.append(finding)

    return plan



def apply_workspace_sync_repair_plan(
    *,
    settings: AppSettings,
    plan: WorkspaceSyncRepairPlan,
) -> WorkspaceSyncRepairRun:
    manager = CollectionWorkspaceManager(settings.workspace_root)
    run = WorkspaceSyncRepairRun()

    for action in plan.actions:
        try:
            result = _apply_repair_action(manager, action)
        except Exception as exc:
            logger.exception(
                "Doctor workspace-sync repair action failed: operation={}, collection_id={}, document_id={}, "
                "thread_id={}, local_path={}",
                action.operation,
                action.collection_id,
                action.document_id,
                action.thread_id,
                action.local_path,
            )
            result = RepairActionResult(
                action=action,
                status="failed",
                message=str(exc),
            )
        run.results.append(result)

    return run



def format_workspace_sync_report_text(report: WorkspaceSyncReport) -> str:
    lines = [
        "Workspace sync diagnostics",
        f"- depth: {report.depth}",
        f"- checked collections: {report.checked_collections}",
        f"- checked documents: {report.checked_documents}",
        f"- checked threads: {report.checked_threads}",
        f"- findings: {len(report.findings)}",
    ]
    if report.collection_filter:
        lines.append(f"- collection filter: {report.collection_filter}")
    if report.document_filter:
        lines.append(f"- document filter: {report.document_filter}")

    if not report.findings:
        lines.append("")
        lines.append("No workspace drift detected.")
        return "\n".join(lines)

    lines.append("")
    for kind, count in report.finding_counts().items():
        lines.append(f"- {kind}: {count}")

    grouped: dict[str, list[DiagnosticFinding]] = defaultdict(list)
    for finding in _sorted_findings(report.findings):
        grouped[finding.kind].append(finding)

    for kind in _finding_kind_order(grouped.keys()):
        lines.append("")
        lines.append(f"[{kind}]")
        for finding in grouped[kind]:
            target_parts = [
                f"collection={finding.collection_id}" if finding.collection_id else None,
                f"document={finding.document_id}" if finding.document_id else None,
                f"thread={finding.thread_id}" if finding.thread_id else None,
            ]
            target = " ".join(part for part in target_parts if part)
            prefix = f"- {target}: " if target else "- "
            lines.append(f"{prefix}{finding.message}")
            lines.append(f"  path: {finding.local_path}")
    return "\n".join(lines)



def format_workspace_sync_repair_plan_text(plan: WorkspaceSyncRepairPlan) -> str:
    lines = ["Repair plan"]
    if not plan.actions:
        lines.append("- no automatic repairs available")
    else:
        lines.append(f"- planned actions: {len(plan.actions)}")
        for operation, count in plan.action_counts().items():
            lines.append(f"- {operation}: {count}")
        lines.append("")
        for action in plan.actions:
            target_parts = [
                f"collection={action.collection_id}" if action.collection_id else None,
                f"document={action.document_id}" if action.document_id else None,
                f"thread={action.thread_id}" if action.thread_id else None,
            ]
            target = " ".join(part for part in target_parts if part)
            prefix = f"- {target}: " if target else "- "
            lines.append(f"{prefix}{action.message}")
            lines.append(f"  path: {action.local_path}")

    if plan.skipped_findings:
        lines.append("")
        lines.append(f"- skipped findings (manual review): {len(plan.skipped_findings)}")
        for finding in _sorted_findings(plan.skipped_findings):
            lines.append(f"  - {finding.kind}: {finding.message}")
    return "\n".join(lines)



def format_workspace_sync_repair_run_text(run: WorkspaceSyncRepairRun) -> str:
    lines = ["Repair results"]
    if not run.results:
        lines.append("- no changes applied")
        return "\n".join(lines)

    lines.append(f"- attempted actions: {len(run.results)}")
    for status, count in run.counts().items():
        lines.append(f"- {status}: {count}")
    lines.append("")
    for result in run.results:
        target_parts = [
            f"collection={result.action.collection_id}" if result.action.collection_id else None,
            f"document={result.action.document_id}" if result.action.document_id else None,
            f"thread={result.action.thread_id}" if result.action.thread_id else None,
        ]
        target = " ".join(part for part in target_parts if part)
        prefix = f"- [{result.status}] {target}: " if target else f"- [{result.status}] "
        lines.append(f"{prefix}{result.message}")
        lines.append(f"  path: {result.action.local_path}")
        if result.destination_path:
            lines.append(f"  archived_to: {result.destination_path}")
    return "\n".join(lines)



def _build_inventories(
    manager: CollectionWorkspaceManager,
    *,
    collection_id: str | None,
    document_id: str | None,
) -> list[_CollectionInventory]:
    inventories: list[_CollectionInventory] = []
    for workspace in manager.list_active_collections():
        if collection_id and workspace.collection_id != collection_id:
            continue
        active_documents = manager.list_active_documents(workspace)
        archived_documents = manager.list_archived_documents(workspace)
        active_threads = manager.list_active_threads(workspace)
        archived_threads = manager.list_archived_threads(workspace)
        if document_id:
            active_documents = [item for item in active_documents if item.document_id == document_id]
            archived_documents = [item for item in archived_documents if item.document_id == document_id]
            active_threads = [item for item in active_threads if _thread_document_id(item) == document_id]
            archived_threads = [item for item in archived_threads if _thread_document_id(item) == document_id]
            if not active_documents and not archived_documents and not active_threads and not archived_threads:
                continue
        inventories.append(
            _CollectionInventory(
                workspace=workspace,
                active_documents=active_documents,
                archived_documents=archived_documents,
                active_threads=active_threads,
                archived_threads=archived_threads,
            )
        )
    return inventories



def _check_local_document_consistency(inventory: _CollectionInventory) -> list[DiagnosticFinding]:
    findings: list[DiagnosticFinding] = []
    for document in inventory.active_documents:
        if not document.memory_path.exists():
            findings.append(
                DiagnosticFinding(
                    kind="missing_local_metadata",
                    severity="warning",
                    collection_id=inventory.workspace.collection_id,
                    document_id=document.document_id,
                    thread_id=None,
                    local_path=str(document.memory_path),
                    message="Active document workspace is missing MEMORY.md.",
                    details={"missing": "MEMORY.md"},
                )
            )
        if not document.state_path.exists():
            findings.append(
                DiagnosticFinding(
                    kind="missing_local_metadata",
                    severity="warning",
                    collection_id=inventory.workspace.collection_id,
                    document_id=document.document_id,
                    thread_id=None,
                    local_path=str(document.state_path),
                    message="Active document workspace is missing state.json.",
                    details={"missing": "state.json"},
                )
            )
    return findings



def _check_local_thread_consistency(inventory: _CollectionInventory) -> list[DiagnosticFinding]:
    findings: list[DiagnosticFinding] = []
    archived_document_ids = {item.document_id for item in inventory.archived_documents}
    for thread in inventory.active_threads:
        state = thread.read_state()
        transcript = thread.read_transcript()
        document_id = _thread_document_id(thread, state=state, transcript=transcript)
        thread_id = _thread_root_id(thread, state=state, transcript=transcript)

        if not thread.state_path.exists():
            findings.append(
                DiagnosticFinding(
                    kind="missing_local_metadata",
                    severity="warning",
                    collection_id=inventory.workspace.collection_id,
                    document_id=document_id,
                    thread_id=thread_id,
                    local_path=str(thread.state_path),
                    message="Active thread workspace is missing state.json.",
                    details={"missing": "state.json"},
                )
            )
        if not thread.comments_path.exists():
            findings.append(
                DiagnosticFinding(
                    kind="missing_local_metadata",
                    severity="warning",
                    collection_id=inventory.workspace.collection_id,
                    document_id=document_id,
                    thread_id=thread_id,
                    local_path=str(thread.comments_path),
                    message="Active thread workspace is missing comments.json.",
                    details={"missing": "comments.json"},
                )
            )
        if transcript.get("deleted") is True:
            findings.append(
                DiagnosticFinding(
                    kind="deleted_thread_not_archived",
                    severity="warning",
                    collection_id=inventory.workspace.collection_id,
                    document_id=document_id,
                    thread_id=thread_id,
                    local_path=str(thread.root_dir),
                    message="Thread transcript is marked deleted but the workspace is still active.",
                    details={"transcript_deleted": True},
                )
            )
        if document_id and document_id in archived_document_ids:
            findings.append(
                DiagnosticFinding(
                    kind="archived_parent_with_active_child",
                    severity="warning",
                    collection_id=inventory.workspace.collection_id,
                    document_id=document_id,
                    thread_id=thread_id,
                    local_path=str(thread.root_dir),
                    message="Active thread workspace points to a document already archived locally.",
                    details={"archived_document": document_id},
                )
            )
    return findings


async def _check_remote_collections(
    *,
    outline_client,
    inventories: list[_CollectionInventory],
    concurrency: int,
) -> list[DiagnosticFinding]:
    semaphore = asyncio.Semaphore(concurrency)

    async def check(inventory: _CollectionInventory) -> DiagnosticFinding | None:
        async with semaphore:
            try:
                await outline_client.collection_info(inventory.workspace.collection_id)
            except OutlineClientError as exc:
                if _is_missing_remote_error(exc):
                    return DiagnosticFinding(
                        kind="missing_remote_collection",
                        severity="error",
                        collection_id=inventory.workspace.collection_id,
                        document_id=None,
                        thread_id=None,
                        local_path=str(inventory.workspace.root_dir),
                        message="Local collection workspace exists but the remote collection is missing.",
                        details={"error": str(exc)},
                    )
                if _is_permission_error(exc):
                    return DiagnosticFinding(
                        kind="inaccessible_remote_collection",
                        severity="warning",
                        collection_id=inventory.workspace.collection_id,
                        document_id=None,
                        thread_id=None,
                        local_path=str(inventory.workspace.root_dir),
                        message="Remote collection is not accessible with the current API key.",
                        details={"error": str(exc)},
                    )
                raise
            return None

    results = await asyncio.gather(*(check(item) for item in inventories))
    return [item for item in results if item is not None]


async def _check_remote_documents(
    *,
    outline_client,
    inventories: list[_CollectionInventory],
    concurrency: int,
    skipped_collection_ids: set[str],
) -> list[DiagnosticFinding]:
    semaphore = asyncio.Semaphore(concurrency)
    document_entries = [
        (inventory.workspace, document)
        for inventory in inventories
        if inventory.workspace.collection_id not in skipped_collection_ids
        for document in inventory.active_documents
    ]

    async def check(entry: tuple[CollectionWorkspace, DocumentWorkspace]) -> DiagnosticFinding | None:
        workspace, document = entry
        async with semaphore:
            try:
                remote_document = await outline_client.document_info(document.document_id)
            except OutlineClientError as exc:
                if _is_missing_remote_error(exc):
                    return DiagnosticFinding(
                        kind="missing_remote_document",
                        severity="error",
                        collection_id=workspace.collection_id,
                        document_id=document.document_id,
                        thread_id=None,
                        local_path=str(document.root_dir),
                        message="Local document workspace exists but the remote document is missing.",
                        details={"error": str(exc)},
                    )
                if _is_permission_error(exc):
                    return DiagnosticFinding(
                        kind="inaccessible_remote_document",
                        severity="warning",
                        collection_id=workspace.collection_id,
                        document_id=document.document_id,
                        thread_id=None,
                        local_path=str(document.root_dir),
                        message="Remote document is not accessible with the current API key.",
                        details={"error": str(exc)},
                    )
                raise
            if getattr(remote_document, "deleted_at", None):
                return DiagnosticFinding(
                    kind="missing_remote_document",
                    severity="error",
                    collection_id=workspace.collection_id,
                    document_id=document.document_id,
                    thread_id=None,
                    local_path=str(document.root_dir),
                    message="Local document workspace exists but the remote document is deleted.",
                    details={"deleted_at": remote_document.deleted_at},
                )
            return None

    results = await asyncio.gather(*(check(item) for item in document_entries))
    return [item for item in results if item is not None]


async def _check_remote_threads(
    *,
    settings: AppSettings,
    outline_client,
    inventories: list[_CollectionInventory],
    existing_findings: list[DiagnosticFinding],
    concurrency: int,
    skipped_collection_ids: set[str],
) -> list[DiagnosticFinding]:
    findings: list[DiagnosticFinding] = []
    missing_remote_documents = {
        finding.document_id
        for finding in existing_findings
        if finding.kind == "missing_remote_document" and finding.document_id
    }
    inaccessible_remote_documents = {
        finding.document_id
        for finding in existing_findings
        if finding.kind == "inaccessible_remote_document" and finding.document_id
    }
    threads_by_document: dict[str, list[tuple[CollectionWorkspace, ThreadWorkspace]]] = defaultdict(list)
    for inventory in inventories:
        if inventory.workspace.collection_id in skipped_collection_ids:
            continue
        for thread in inventory.active_threads:
            document_id = _thread_document_id(thread)
            if not document_id:
                continue
            threads_by_document[document_id].append((inventory.workspace, thread))

    semaphore = asyncio.Semaphore(concurrency)

    async def check_document_threads(
        document_id: str,
        items: list[tuple[CollectionWorkspace, ThreadWorkspace]],
    ) -> list[DiagnosticFinding]:
        async with semaphore:
            if document_id in missing_remote_documents:
                return [
                    _build_orphaned_thread_finding(
                        workspace=workspace,
                        thread=thread,
                        message="Active thread workspace references a document missing remotely.",
                        details={"reason": "missing_remote_document"},
                    )
                    for workspace, thread in items
                    if _can_validate_thread_remotely(thread)
                ]
            if document_id in inaccessible_remote_documents:
                return []

            try:
                comments = await _fetch_document_comments(
                    settings=settings,
                    outline_client=outline_client,
                    document_id=document_id,
                )
            except OutlineClientError as exc:
                if _is_missing_remote_error(exc):
                    local_findings: list[DiagnosticFinding] = []
                    if document_id not in missing_remote_documents:
                        workspace = items[0][0]
                        local_findings.append(
                            DiagnosticFinding(
                                kind="missing_remote_document",
                                severity="error",
                                collection_id=workspace.collection_id,
                                document_id=document_id,
                                thread_id=None,
                                local_path=str(workspace.documents_dir / document_id),
                                message="Remote document disappeared while validating thread roots.",
                                details={"error": str(exc)},
                            )
                        )
                    local_findings.extend(
                        _build_orphaned_thread_finding(
                            workspace=workspace,
                            thread=thread,
                            message="Active thread workspace references a document missing remotely.",
                            details={"reason": "missing_remote_document"},
                        )
                        for workspace, thread in items
                        if _can_validate_thread_remotely(thread)
                    )
                    return local_findings
                if _is_permission_error(exc):
                    workspace = items[0][0]
                    return [
                        DiagnosticFinding(
                            kind="inaccessible_remote_comments",
                            severity="warning",
                            collection_id=workspace.collection_id,
                            document_id=document_id,
                            thread_id=None,
                            local_path=str(workspace.documents_dir / document_id),
                            message=(
                                "Remote document comments are not accessible with the current API key, "
                                "so thread roots could not be verified."
                            ),
                            details={"error": str(exc)},
                        )
                    ]
                raise

            remote_comment_ids = {comment.id for comment in comments}
            local_findings: list[DiagnosticFinding] = []
            for workspace, thread in items:
                if not _can_validate_thread_remotely(thread):
                    continue
                root_comment_id = _thread_root_id(thread)
                if not root_comment_id or root_comment_id not in remote_comment_ids:
                    local_findings.append(
                        _build_orphaned_thread_finding(
                            workspace=workspace,
                            thread=thread,
                            message="Active thread workspace root comment no longer exists remotely.",
                            details={"reason": "missing_remote_thread_root", "document_id": document_id},
                        )
                    )
            return local_findings

    results = await asyncio.gather(
        *(check_document_threads(document_id, items) for document_id, items in threads_by_document.items())
    )
    for batch in results:
        findings.extend(batch)
    return findings


async def _fetch_document_comments(
    *,
    settings: AppSettings,
    outline_client,
    document_id: str,
) -> list[OutlineComment]:
    comments: list[OutlineComment] = []
    offset = 0
    page_size = max(1, settings.comment_list_limit)
    while True:
        batch = await outline_client.comments_list(document_id, limit=page_size, offset=offset)
        comments.extend(batch)
        if len(batch) < page_size:
            break
        offset += len(batch)
    logger.debug(
        "Doctor deep thread validation fetched comments: document_id={}, count={}, page_size={}",
        document_id,
        len(comments),
        page_size,
    )
    return comments



def _build_orphaned_thread_finding(
    *,
    workspace: CollectionWorkspace,
    thread: ThreadWorkspace,
    message: str,
    details: dict[str, Any],
) -> DiagnosticFinding:
    return DiagnosticFinding(
        kind="orphaned_active_thread",
        severity="error",
        collection_id=workspace.collection_id,
        document_id=_thread_document_id(thread),
        thread_id=_thread_root_id(thread),
        local_path=str(thread.root_dir),
        message=message,
        details=details,
    )



def _thread_document_id(
    thread: ThreadWorkspace,
    *,
    state: dict[str, Any] | None = None,
    transcript: dict[str, Any] | None = None,
) -> str | None:
    state = state if state is not None else thread.read_state()
    transcript = transcript if transcript is not None else thread.read_transcript()
    state_document_id = state.get("document_id") if isinstance(state.get("document_id"), str) else None
    transcript_document_id = transcript.get("document_id") if isinstance(transcript.get("document_id"), str) else None
    return state_document_id or transcript_document_id



def _thread_root_id(
    thread: ThreadWorkspace,
    *,
    state: dict[str, Any] | None = None,
    transcript: dict[str, Any] | None = None,
) -> str | None:
    state = state if state is not None else thread.read_state()
    transcript = transcript if transcript is not None else thread.read_transcript()
    transcript_root_value = transcript.get("root_comment_id")
    transcript_root_id = transcript_root_value if isinstance(transcript_root_value, str) else None
    state_thread_id = state.get("thread_id") if isinstance(state.get("thread_id"), str) else None
    return transcript_root_id or state_thread_id or thread.thread_id



def _can_validate_thread_remotely(thread: ThreadWorkspace) -> bool:
    if not thread.state_path.exists() or not thread.comments_path.exists():
        return False
    transcript = thread.read_transcript()
    if transcript.get("deleted") is True:
        return False
    return _thread_document_id(thread) is not None



def _is_missing_remote_error(exc: OutlineClientError) -> bool:
    text = str(exc).lower()
    return "404" in text or "not found" in text or "not_found" in text



def _is_permission_error(exc: OutlineClientError) -> bool:
    text = str(exc).lower()
    return "403" in text or "authorization error" in text or "forbidden" in text or "permission" in text



def _dedupe_findings(findings: list[DiagnosticFinding]) -> list[DiagnosticFinding]:
    seen: set[tuple[str, str | None, str | None, str | None, str, str]] = set()
    unique: list[DiagnosticFinding] = []
    for finding in _sorted_findings(findings):
        key = (
            finding.kind,
            finding.collection_id,
            finding.document_id,
            finding.thread_id,
            finding.local_path,
            finding.message,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(finding)
    return unique



def _sorted_findings(findings: list[DiagnosticFinding]) -> list[DiagnosticFinding]:
    return sorted(
        findings,
        key=lambda item: (
            _finding_kind_rank(item.kind),
            item.collection_id or "",
            item.document_id or "",
            item.thread_id or "",
            item.local_path,
            item.message,
        ),
    )



def _finding_kind_order(kinds: Any) -> list[str]:
    return sorted(kinds, key=_finding_kind_rank)



def _finding_kind_rank(kind: str) -> tuple[int, str]:
    order = {
        "missing_remote_collection": 0,
        "inaccessible_remote_collection": 1,
        "missing_remote_document": 2,
        "inaccessible_remote_document": 3,
        "inaccessible_remote_comments": 4,
        "orphaned_active_thread": 5,
        "deleted_thread_not_archived": 6,
        "archived_parent_with_active_child": 7,
        "missing_local_metadata": 8,
    }
    return (order.get(kind, 999), kind)



def _append_repair_action(
    actions: list[RepairAction],
    planned_keys: set[tuple[str, str | None, str | None, str | None, str]],
    action: RepairAction,
) -> None:
    key = (
        action.operation,
        action.collection_id,
        action.document_id,
        action.thread_id,
        action.local_path,
    )
    if key in planned_keys:
        return
    planned_keys.add(key)
    actions.append(action)



def _apply_repair_action(manager: CollectionWorkspaceManager, action: RepairAction) -> RepairActionResult:
    if action.operation == "archive_collection_workspace":
        return _archive_collection_workspace(manager, action)
    if action.operation == "archive_document_workspace":
        return _archive_document_workspace(manager, action)
    if action.operation == "archive_thread_workspace":
        return _archive_thread_workspace(manager, action)
    return RepairActionResult(
        action=action,
        status="skipped",
        message=f"Unsupported repair operation: {action.operation}",
    )



def _archive_collection_workspace(
    manager: CollectionWorkspaceManager,
    action: RepairAction,
) -> RepairActionResult:
    if not action.collection_id:
        return RepairActionResult(action=action, status="failed", message="Missing collection_id for repair action.")
    workspace = manager.find_collection(action.collection_id)
    if workspace is None:
        if manager.find_archived_collection_dir(action.collection_id):
            return RepairActionResult(
                action=action,
                status="skipped",
                message="Collection workspace was already archived.",
            )
        return RepairActionResult(
            action=action,
            status="skipped",
            message="Collection workspace no longer exists locally.",
        )
    destination = manager.archive_collection(workspace, reason=f"doctor:{action.reason}")
    return RepairActionResult(
        action=action,
        status="applied",
        message="Archived collection workspace.",
        destination_path=str(destination),
    )



def _archive_document_workspace(
    manager: CollectionWorkspaceManager,
    action: RepairAction,
) -> RepairActionResult:
    if not action.collection_id or not action.document_id:
        return RepairActionResult(
            action=action,
            status="failed",
            message="Missing collection_id or document_id for repair action.",
        )
    workspace = manager.find_collection(action.collection_id)
    if workspace is None:
        return RepairActionResult(
            action=action,
            status="skipped",
            message="Collection workspace is already archived or missing.",
        )

    archived_threads = 0
    for thread in manager.list_active_thread_workspaces_for_document(workspace, document_id=action.document_id):
        manager.archive_thread(workspace, thread, reason=f"doctor:{action.reason}")
        archived_threads += 1

    document_workspace = manager.find_document(workspace, document_id=action.document_id)
    if document_workspace is None:
        if manager.find_archived_document_dir(workspace, document_id=action.document_id):
            return RepairActionResult(
                action=action,
                status="skipped",
                message="Document workspace was already archived.",
            )
        return RepairActionResult(
            action=action,
            status="skipped",
            message="Document workspace no longer exists locally.",
        )

    destination = manager.archive_document(workspace, document_workspace, reason=f"doctor:{action.reason}")
    thread_message = f" Archived {archived_threads} active child threads first." if archived_threads else ""
    return RepairActionResult(
        action=action,
        status="applied",
        message=f"Archived document workspace.{thread_message}",
        destination_path=str(destination),
    )



def _archive_thread_workspace(
    manager: CollectionWorkspaceManager,
    action: RepairAction,
) -> RepairActionResult:
    if not action.collection_id:
        return RepairActionResult(action=action, status="failed", message="Missing collection_id for repair action.")
    workspace = manager.find_collection(action.collection_id)
    if workspace is None:
        return RepairActionResult(
            action=action,
            status="skipped",
            message="Collection workspace is already archived or missing.",
        )

    thread_path = Path(action.local_path)
    if not thread_path.is_dir():
        return RepairActionResult(
            action=action,
            status="skipped",
            message="Thread workspace no longer exists locally.",
        )
    thread_workspace = ThreadWorkspace(
        thread_id=action.thread_id or thread_path.name,
        root_dir=thread_path,
        state_path=thread_path / "state.json",
        events_path=thread_path / "events.jsonl",
        comments_path=thread_path / "comments.json",
    )
    destination = manager.archive_thread(workspace, thread_workspace, reason=f"doctor:{action.reason}")
    return RepairActionResult(
        action=action,
        status="applied",
        message="Archived thread workspace.",
        destination_path=str(destination),
    )
