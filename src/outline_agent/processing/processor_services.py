from __future__ import annotations

from dataclasses import dataclass

from ..clients.model_client import ModelClient
from ..clients.outline_client import OutlineClient
from ..core.config import AppSettings
from ..managers.action_router_manager import ActionRouterManager
from ..managers.memory_action_manager import MemoryActionManager
from ..managers.memory_manager import CollectionMemoryManager
from ..managers.related_document_manager import RelatedDocumentManager
from ..managers.same_document_comment_manager import SameDocumentCommentManager
from ..managers.thread_session_manager import ThreadSessionManager
from ..planning import UnifiedExecutionLoop, UnifiedToolPlanner
from ..state.store import ProcessedEventStore
from ..state.workspace import CollectionWorkspaceManager
from ..tools import ToolRegistry, build_default_tool_registry
from .processor_action_loop import execute_action_plan as _execute_action_plan
from .processor_prompting import PromptPack, load_prompt_packs
from .processor_types import ActionPlanOutcome, ActionPlanRequest


@dataclass(slots=True)
class ProcessorServices:
    settings: AppSettings
    store: ProcessedEventStore
    outline_client: OutlineClient
    model_client: ModelClient
    memory_action_manager: MemoryActionManager
    memory_manager: CollectionMemoryManager
    workspace_manager: CollectionWorkspaceManager
    tool_registry: ToolRegistry
    related_document_manager: RelatedDocumentManager
    same_document_comment_manager: SameDocumentCommentManager
    action_router: ActionRouterManager
    action_planner: UnifiedToolPlanner
    unified_execution_loop: UnifiedExecutionLoop
    thread_session_manager: ThreadSessionManager
    prompt_packs: list[PromptPack]

    async def execute_action_plan(self, request: ActionPlanRequest) -> ActionPlanOutcome:
        return await _execute_action_plan(
            settings=self.settings,
            outline_client=self.outline_client,
            action_planner=self.action_planner,
            unified_execution_loop=self.unified_execution_loop,
            tool_specs=self.tool_registry.list_specs(),
            request=request,
        )


def build_processor_services(
    *,
    settings: AppSettings,
    store: ProcessedEventStore,
    outline_client: OutlineClient,
    model_client: ModelClient,
    memory_model_client: ModelClient | None = None,
    thread_session_model_client: ModelClient | None = None,
    document_update_model_client: ModelClient | None = None,
    tool_model_client: ModelClient | None = None,
    action_router_model_client: ModelClient | None = None,
) -> ProcessorServices:
    shared_memory_client = memory_model_client or model_client
    workspace_manager = CollectionWorkspaceManager(settings.workspace_root)
    tool_registry = build_default_tool_registry(
        settings=settings,
        drafting_model_client=document_update_model_client or shared_memory_client,
    )

    return ProcessorServices(
        settings=settings,
        store=store,
        outline_client=outline_client,
        model_client=model_client,
        memory_action_manager=MemoryActionManager(settings, shared_memory_client),
        memory_manager=CollectionMemoryManager(settings, shared_memory_client),
        workspace_manager=workspace_manager,
        tool_registry=tool_registry,
        related_document_manager=RelatedDocumentManager(settings, outline_client),
        same_document_comment_manager=SameDocumentCommentManager(
            settings,
            outline_client,
            workspace_manager,
        ),
        action_router=ActionRouterManager(
            settings,
            action_router_model_client or model_client,
        ),
        action_planner=UnifiedToolPlanner(
            settings,
            tool_model_client or action_router_model_client or document_update_model_client or shared_memory_client,
        ),
        unified_execution_loop=UnifiedExecutionLoop(
            tool_registry,
            max_steps=settings.tool_execution_max_steps,
        ),
        thread_session_manager=ThreadSessionManager(
            settings,
            thread_session_model_client or shared_memory_client,
        ),
        prompt_packs=load_prompt_packs(settings.prompt_pack_dir, settings.system_prompt_packs),
    )
