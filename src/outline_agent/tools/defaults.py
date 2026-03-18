from __future__ import annotations

from ..clients.gemini_web_search import GeminiWebSearchClient, has_gemini_web_search_api_key
from ..clients.model_client import ModelClient
from ..core.config import AppSettings
from ..core.prompt_registry import PromptRegistry
from .document_actions import ApplyDocumentUpdateTool, DraftDocumentUpdateTool, DraftNewDocumentTool
from .extract_text import build_default_extract_text_tools
from .gemini_web_search import AskGeminiWebSearchTool
from .outline_tools import CreateDocumentTool, GetCurrentDocumentTool
from .registry import ToolRegistry
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
    if has_gemini_web_search_api_key(settings):
        registry.register(
            AskGeminiWebSearchTool(
                GeminiWebSearchClient(
                    api_key=settings.gemini_api_key or "",
                    base_url=settings.gemini_base_url,
                    model=settings.gemini_model,
                    timeout=settings.model_timeout_seconds,
                )
            )
        )
    return registry
