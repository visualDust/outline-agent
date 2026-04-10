from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from ..clients.outline_client import OutlineClient
from ..clients.outline_models import OutlineCollection, OutlineDocument
from ..core.config import AppSettings
from ..core.logging import logger
from ..state.workspace import CollectionWorkspace, CollectionWorkspaceManager
from .memory_action_manager import MemoryActionPlan, apply_actions_to_text
from .memory_manager import MemoryUpdateProposal, apply_update_to_text, write_memory_index


@dataclass(slots=True)
class CollectionMemorySessionState:
    collection_id: str
    involved: bool = False
    initial_pull_done: bool = False
    remote_document_id: str | None = None
    last_sync_reason: str | None = None


@dataclass(slots=True)
class CollectionMemoryRefreshResult:
    status: str
    reason: str
    document_id: str | None = None
    preview: str | None = None


@dataclass(slots=True)
class CollectionMemoryPersistResult:
    status: str
    preview: str | None
    applied: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    document_id: str | None = None


class CollectionMemorySync:
    def __init__(
        self,
        *,
        settings: AppSettings,
        outline_client: OutlineClient,
        workspace_manager: CollectionWorkspaceManager,
    ) -> None:
        self.settings = settings
        self.outline_client = outline_client
        self.workspace_manager = workspace_manager
        self._states: dict[str, CollectionMemorySessionState] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def state_for(self, collection_id: str) -> CollectionMemorySessionState:
        state = self._states.get(collection_id)
        if state is None:
            state = CollectionMemorySessionState(collection_id=collection_id)
            self._states[collection_id] = state
        return state

    def is_involved(self, collection_id: str | None) -> bool:
        if not collection_id:
            return False
        return self.state_for(collection_id).involved

    async def ensure_initialized_for_chat(
        self,
        *,
        workspace: CollectionWorkspace,
        collection: OutlineCollection | None,
    ) -> CollectionMemoryRefreshResult:
        state = self.state_for(workspace.collection_id)
        state.involved = True
        if state.initial_pull_done:
            return CollectionMemoryRefreshResult(status="noop", reason="initial-pull-already-done")

        async with self._lock_for(workspace.collection_id):
            state = self.state_for(workspace.collection_id)
            state.involved = True
            if state.initial_pull_done:
                return CollectionMemoryRefreshResult(status="noop", reason="initial-pull-already-done")
            remote = await self._resolve_remote_document(
                collection_id=workspace.collection_id,
                preferred_document_id=state.remote_document_id,
            )
            if remote is None:
                self.workspace_manager.delete_collection_memory(workspace)
                state.initial_pull_done = True
                state.last_sync_reason = "initial-pull-missing"
                return CollectionMemoryRefreshResult(status="noop", reason="remote-memory-document-missing")
            result = await self._pull_remote_document(
                workspace=workspace,
                collection=collection,
                remote=remote,
                reason="initial-chat-pull",
            )
            state.initial_pull_done = True
            return result

    async def refresh_from_event(
        self,
        *,
        collection_id: str,
        workspace: CollectionWorkspace,
        document_id: str,
        collection: OutlineCollection | None = None,
    ) -> CollectionMemoryRefreshResult:
        if not self.is_involved(collection_id):
            return CollectionMemoryRefreshResult(status="ignored", reason="collection-not-involved")

        async with self._lock_for(collection_id):
            remote = await self.outline_client.document_info(document_id)
            if not self._is_memory_document(remote):
                state = self.state_for(collection_id)
                if state.remote_document_id == document_id:
                    state.remote_document_id = None
                return CollectionMemoryRefreshResult(
                    status="ignored",
                    reason="not-memory-document",
                    document_id=document_id,
                )
            return await self._pull_remote_document(
                workspace=workspace,
                collection=collection,
                remote=remote,
                reason="webhook-refresh",
            )

    async def handle_deleted_document_event(
        self,
        *,
        collection_id: str,
        workspace: CollectionWorkspace,
        document_id: str,
        document_title: str | None,
    ) -> CollectionMemoryRefreshResult:
        if not self.is_involved(collection_id):
            return CollectionMemoryRefreshResult(status="ignored", reason="collection-not-involved")

        async with self._lock_for(collection_id):
            state = self.state_for(collection_id)
            is_memory_delete = (
                state.remote_document_id == document_id
                or (document_title or "").strip() == self.settings.collection_memory_document_title
            )
            if not is_memory_delete:
                return CollectionMemoryRefreshResult(
                    status="ignored",
                    reason="not-memory-document",
                    document_id=document_id,
                )

            self.workspace_manager.delete_collection_memory(workspace)
            state.involved = True
            state.initial_pull_done = True
            state.remote_document_id = None
            state.last_sync_reason = "webhook-delete"
            logger.debug(
                "Cleared collection memory after remote delete: collection_id={}, document_id={}",
                collection_id,
                document_id,
            )
            return CollectionMemoryRefreshResult(
                status="synced",
                reason="memory-document-deleted",
                document_id=document_id,
            )

    async def persist_update(
        self,
        *,
        workspace: CollectionWorkspace,
        collection: OutlineCollection | None,
        proposal: MemoryUpdateProposal,
    ) -> CollectionMemoryPersistResult:
        text = workspace.read_memory_text_or_empty()
        if not text:
            text = await self._load_or_initialize_text(workspace=workspace, collection=collection)
        updated_text, applied = apply_update_to_text(text, proposal)
        if not applied:
            return CollectionMemoryPersistResult(status="noop", preview=None)
        document = await self._persist_text(
            workspace=workspace,
            collection=collection,
            text=updated_text,
            reason="memory-update",
        )
        return CollectionMemoryPersistResult(
            status="applied",
            preview=" ; ".join(applied),
            applied=applied,
            document_id=document.id,
        )

    async def persist_actions(
        self,
        *,
        workspace: CollectionWorkspace,
        collection: OutlineCollection | None,
        plan: MemoryActionPlan,
    ) -> CollectionMemoryPersistResult:
        text = workspace.read_memory_text_or_empty()
        if not text:
            text = await self._load_or_initialize_text(workspace=workspace, collection=collection)
        apply_result = apply_actions_to_text(
            text,
            plan,
            max_chars=self.settings.memory_update_entry_max_chars,
        )
        if not apply_result.applied:
            return CollectionMemoryPersistResult(
                status="blocked",
                preview=None,
                errors=list(apply_result.errors),
            )
        document = await self._persist_text(
            workspace=workspace,
            collection=collection,
            text=apply_result.text,
            reason="memory-action",
        )
        return CollectionMemoryPersistResult(
            status="applied",
            preview=" ; ".join(apply_result.applied),
            applied=list(apply_result.applied),
            errors=list(apply_result.errors),
            document_id=document.id,
        )

    async def _load_or_initialize_text(
        self,
        *,
        workspace: CollectionWorkspace,
        collection: OutlineCollection | None,
    ) -> str:
        state = self.state_for(workspace.collection_id)
        if not state.initial_pull_done:
            await self.ensure_initialized_for_chat(workspace=workspace, collection=collection)
        text = workspace.read_memory_text_or_empty()
        if text:
            return text
        return self.workspace_manager.build_initial_memory_text(
            collection_id=workspace.collection_id,
            collection_name=collection.name if collection and collection.name else workspace.collection_name,
        )

    async def _persist_text(
        self,
        *,
        workspace: CollectionWorkspace,
        collection: OutlineCollection | None,
        text: str,
        reason: str,
    ) -> OutlineDocument:
        async with self._lock_for(workspace.collection_id):
            state = self.state_for(workspace.collection_id)
            remote = await self._resolve_remote_document(
                collection_id=workspace.collection_id,
                preferred_document_id=state.remote_document_id,
            )
            if remote is None:
                remote = await self.outline_client.create_document(
                    title=self.settings.collection_memory_document_title,
                    text=text,
                    collection_id=workspace.collection_id,
                    publish=True,
                )
            else:
                await self.outline_client.update_document(
                    remote.id,
                    title=self.settings.collection_memory_document_title,
                    text=text,
                )
                remote = await self.outline_client.document_info(remote.id)

            self.workspace_manager.write_collection_memory(
                workspace,
                text=text,
                collection_id=workspace.collection_id,
                collection_name=collection.name if collection and collection.name else workspace.collection_name,
            )
            write_memory_index(workspace, workspace.read_memory_text())
            state.involved = True
            state.initial_pull_done = True
            state.remote_document_id = remote.id
            state.last_sync_reason = reason
            logger.debug(
                "Persisted collection memory: collection_id={}, document_id={}, reason={}",
                workspace.collection_id,
                remote.id,
                reason,
            )
            return remote

    async def _pull_remote_document(
        self,
        *,
        workspace: CollectionWorkspace,
        collection: OutlineCollection | None,
        remote: OutlineDocument,
        reason: str,
    ) -> CollectionMemoryRefreshResult:
        self.workspace_manager.write_collection_memory(
            workspace,
            text=remote.text or "",
            collection_id=workspace.collection_id,
            collection_name=collection.name if collection and collection.name else workspace.collection_name,
        )
        write_memory_index(workspace, workspace.read_memory_text())
        state = self.state_for(workspace.collection_id)
        state.involved = True
        state.initial_pull_done = True
        state.remote_document_id = remote.id
        state.last_sync_reason = reason
        logger.debug(
            "Pulled collection memory: collection_id={}, document_id={}, reason={}",
            workspace.collection_id,
            remote.id,
            reason,
        )
        return CollectionMemoryRefreshResult(
            status="synced",
            reason=reason,
            document_id=remote.id,
            preview=remote.title or remote.id,
        )

    async def _resolve_remote_document(
        self,
        *,
        collection_id: str,
        preferred_document_id: str | None,
    ) -> OutlineDocument | None:
        if preferred_document_id:
            try:
                remote = await self.outline_client.document_info(preferred_document_id)
            except Exception:
                remote = None
            if remote is not None and remote.collection_id == collection_id and self._is_memory_document(remote):
                return remote

        results = await self.outline_client.documents_search(
            self.settings.collection_memory_document_title,
            collection_id=collection_id,
            limit=10,
        )
        exact = [
            item
            for item in results
            if item.collection_id == collection_id
            and (item.title or "").strip() == self.settings.collection_memory_document_title
        ]
        if not exact:
            return None
        if len(exact) > 1:
            raise ValueError(
                f"Multiple collection memory documents named {self.settings.collection_memory_document_title!r} "
                f"exist in collection {collection_id}"
            )
        return await self.outline_client.document_info(exact[0].id)

    def _is_memory_document(self, document: OutlineDocument) -> bool:
        return (document.title or "").strip() == self.settings.collection_memory_document_title

    def _lock_for(self, collection_id: str) -> asyncio.Lock:
        lock = self._locks.get(collection_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[collection_id] = lock
        return lock
