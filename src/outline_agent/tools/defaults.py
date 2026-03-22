from __future__ import annotations

from ..clients.model_client import ModelClient
from ..clients.web_search import build_web_search_client
from ..core.config import AppSettings
from ..core.prompt_registry import PromptRegistry
from .document_actions import ApplyDocumentUpdateTool, DraftDocumentUpdateTool, DraftNewDocumentTool
from .extract_text import build_default_extract_text_tools
from .outline_tools import CreateDocumentTool, GetCurrentDocumentTool
from .registry import ToolRegistry
from .web_search import AskWebSearchTool
from .workspace_tools import build_workspace_tools


def build_default_tool_registry(
    *,
    settings: AppSettings,
    drafting_model_client: ModelClient,
    prompt_registry: PromptRegistry | None = None,
) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(GetCurrentDocumentTool())
    registry.register_many(build_workspace_tools())
    registry.register(CreateDocumentTool())
    registry.register(DraftNewDocumentTool(settings, drafting_model_client, prompt_registry=prompt_registry))
    registry.register(DraftDocumentUpdateTool(settings, drafting_model_client, prompt_registry=prompt_registry))
    registry.register(ApplyDocumentUpdateTool())
    registry.register_many(build_default_extract_text_tools())
    web_search_client = build_web_search_client(settings)
    if web_search_client is not None:
        registry.register(AskWebSearchTool(web_search_client))
    return registry
