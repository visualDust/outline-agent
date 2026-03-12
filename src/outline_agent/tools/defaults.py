from __future__ import annotations

from ..clients.model_client import ModelClient
from ..core.config import AppSettings
from .document_actions import ApplyDocumentUpdateTool, DraftDocumentUpdateTool, DraftNewDocumentTool
from .extract_text import build_default_extract_text_tools
from .outline_tools import CreateDocumentTool, GetCurrentDocumentTool
from .registry import ToolRegistry
from .workspace_tools import build_workspace_tools


def build_default_tool_registry(
    *,
    settings: AppSettings,
    drafting_model_client: ModelClient,
) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(GetCurrentDocumentTool())
    registry.register_many(build_workspace_tools())
    registry.register(CreateDocumentTool())
    registry.register(DraftNewDocumentTool(settings, drafting_model_client))
    registry.register(DraftDocumentUpdateTool(settings, drafting_model_client))
    registry.register(ApplyDocumentUpdateTool())
    registry.register_many(build_default_extract_text_tools())
    return registry
