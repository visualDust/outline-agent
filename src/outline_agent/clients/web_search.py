from __future__ import annotations

from .gemini_web_search import GeminiWebSearchClient
from .openai_web_search import OpenAIWebSearchClient
from .web_search_base import WebSearchClient, WebSearchProbe
from ..core.config import AppSettings


def build_web_search_client(settings: AppSettings) -> WebSearchClient | None:
    provider = settings.web_search_provider
    if provider == "gemini":
        if not _has_gemini_web_search_api_key(settings):
            return None
        return GeminiWebSearchClient(
            api_key=settings.gemini_api_key or "",
            base_url=settings.gemini_base_url,
            model=settings.gemini_model,
            timeout=settings.model_timeout_seconds,
        )
    if provider == "openai":
        if not _has_openai_web_search_api_key(settings):
            return None
        return OpenAIWebSearchClient(
            api_key=settings.openai_web_search_api_key or "",
            base_url=settings.openai_web_search_base_url,
            model=settings.openai_web_search_model,
            timeout=settings.model_timeout_seconds,
        )
    return None


def get_web_search_probe(settings: AppSettings) -> WebSearchProbe:
    provider = settings.web_search_provider
    if provider == "gemini":
        if _has_gemini_web_search_api_key(settings):
            return WebSearchProbe(
                available=True,
                provider="gemini",
                backend="gemini-web-search",
                base_url=settings.gemini_base_url,
                model=settings.gemini_model,
                reason=None,
            )
        return WebSearchProbe(
            available=False,
            provider="gemini",
            backend="gemini-web-search",
            base_url=settings.gemini_base_url,
            model=settings.gemini_model,
            reason="GEMINI_API_KEY / GOOGLE_API_KEY is not configured",
        )
    if provider == "openai":
        if _has_openai_web_search_api_key(settings):
            return WebSearchProbe(
                available=True,
                provider="openai",
                backend="openai-responses-web-search",
                base_url=settings.openai_web_search_base_url,
                model=settings.openai_web_search_model,
                reason=None,
            )
        return WebSearchProbe(
            available=False,
            provider="openai",
            backend="openai-responses-web-search",
            base_url=settings.openai_web_search_base_url,
            model=settings.openai_web_search_model,
            reason="OPENAI_WEB_SEARCH_API_KEY / OPENAI_API_KEY is not configured",
        )
    return WebSearchProbe(
        available=False,
        provider=provider,
        backend=f"{provider}-web-search",
        base_url="",
        model="",
        reason=f"Unsupported web search provider: {provider}",
    )


def has_active_web_search_api_key(settings: AppSettings) -> bool:
    return get_web_search_probe(settings).available


def _has_gemini_web_search_api_key(settings: AppSettings) -> bool:
    return bool(settings.gemini_api_key and settings.gemini_api_key.strip())


def _has_openai_web_search_api_key(settings: AppSettings) -> bool:
    return bool(settings.openai_web_search_api_key and settings.openai_web_search_api_key.strip())
