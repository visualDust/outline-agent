from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from outline_agent.clients.gemini_web_search import GeminiWebSearchClient
from outline_agent.clients.openai_web_search import OpenAIWebSearchClient
from outline_agent.clients.web_search import get_web_search_probe
from outline_agent.core.config import AppSettings
from outline_agent.core.prompt_registry import PromptRegistry
from outline_agent.tools import ToolContext, build_default_tool_registry
from outline_agent.tools.web_search import AskWebSearchTool


class DummyDraftingClient:
    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        del system_prompt, user_prompt
        return "unused"


def _settings(
    tmp_path: Path,
    *,
    web_search_provider: str = "gemini",
    gemini_api_key: str | None = None,
    gemini_base_url: str = "https://generativelanguage.googleapis.com",
    openai_web_search_api_key: str | None = None,
    openai_web_search_base_url: str = "https://api.openai.com/v1",
) -> AppSettings:
    return AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=False,
        web_search_provider=web_search_provider,
        gemini_api_key=gemini_api_key,
        gemini_base_url=gemini_base_url,
        openai_web_search_api_key=openai_web_search_api_key,
        openai_web_search_base_url=openai_web_search_base_url,
    )


def test_default_tool_registry_hides_ask_web_search_without_provider_key(tmp_path: Path) -> None:
    settings = _settings(tmp_path, web_search_provider="gemini", gemini_api_key=None)

    registry = build_default_tool_registry(
        settings=settings,
        drafting_model_client=DummyDraftingClient(),  # type: ignore[arg-type]
        prompt_registry=PromptRegistry.from_settings(settings),
    )

    assert registry.has("ask_web_search") is False

    probe = get_web_search_probe(settings)
    assert probe.available is False
    assert probe.provider == "gemini"
    assert probe.backend == "gemini-web-search"
    assert probe.reason == "GEMINI_API_KEY / GOOGLE_API_KEY is not configured"


def test_default_tool_registry_registers_ask_web_search_with_gemini_provider(tmp_path: Path) -> None:
    settings = _settings(tmp_path, web_search_provider="gemini", gemini_api_key="gemini-test-key")

    registry = build_default_tool_registry(
        settings=settings,
        drafting_model_client=DummyDraftingClient(),  # type: ignore[arg-type]
        prompt_registry=PromptRegistry.from_settings(settings),
    )

    assert registry.has("ask_web_search") is True
    probe = get_web_search_probe(settings)
    assert probe.available is True
    assert probe.provider == "gemini"
    assert probe.model == "gemini-3-flash-preview"


def test_default_tool_registry_registers_ask_web_search_with_openai_provider(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        web_search_provider="openai",
        openai_web_search_api_key="openai-test-key",
    )

    registry = build_default_tool_registry(
        settings=settings,
        drafting_model_client=DummyDraftingClient(),  # type: ignore[arg-type]
        prompt_registry=PromptRegistry.from_settings(settings),
    )

    assert registry.has("ask_web_search") is True
    probe = get_web_search_probe(settings)
    assert probe.available is True
    assert probe.provider == "openai"
    assert probe.backend == "openai-responses-web-search"
    assert probe.model == "gpt-5"


def test_ask_web_search_tool_calls_gemini_api(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        is_error = False
        status_code = 200

        def json(self):
            return {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": "As of March 17, 2026, the current answer is from Gemini search.",
                                }
                            ]
                        }
                    }
                ]
            }

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, *, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr("outline_agent.clients.gemini_web_search.httpx.AsyncClient", FakeAsyncClient)

    tool = AskWebSearchTool(
        GeminiWebSearchClient(api_key="gemini-test-key", model="gemini-3-flash-preview", timeout=45)
    )
    context = ToolContext(settings=_settings(tmp_path, web_search_provider="gemini", gemini_api_key="gemini-test-key"))

    result = asyncio.run(tool.run({"query": "What's the latest TypeScript stable version?"}, context))

    assert result.ok is True
    assert result.data["provider"] == "gemini-google-search"
    assert result.data["model"] == "gemini-3-flash-preview"
    assert "Gemini search" in result.data["answer"]
    assert captured["url"] == "https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview:generateContent"
    assert captured["headers"] == {
        "x-goog-api-key": "gemini-test-key",
        "Content-Type": "application/json",
    }
    payload = captured["json"]
    assert isinstance(payload, dict)
    assert payload["tools"] == [{"google_search": {}}]
    assert payload["contents"][0]["parts"][0]["text"] == "What's the latest TypeScript stable version?"


def test_ask_web_search_tool_uses_custom_gemini_base_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        is_error = False
        status_code = 200

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": "gateway answer"}]}}]}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, *, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr("outline_agent.clients.gemini_web_search.httpx.AsyncClient", FakeAsyncClient)

    custom_base_url = "https://gemini-gateway.example.com"
    tool = AskWebSearchTool(
        GeminiWebSearchClient(
            api_key="gemini-test-key",
            base_url=custom_base_url,
            model="gemini-3-flash-preview",
            timeout=45,
        )
    )
    context = ToolContext(
        settings=_settings(
            tmp_path,
            web_search_provider="gemini",
            gemini_api_key="gemini-test-key",
            gemini_base_url=custom_base_url,
        )
    )

    result = asyncio.run(tool.run({"query": "latest release?"}, context))

    assert result.ok is True
    assert captured["url"] == "https://gemini-gateway.example.com/v1beta/models/gemini-3-flash-preview:generateContent"


def test_ask_web_search_tool_calls_openai_responses_api(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        is_error = False
        status_code = 200

        def json(self):
            return {
                "output_text": "As of March 17, 2026, OpenAI web search says TypeScript 5.9 is current.",
            }

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, *, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr("outline_agent.clients.openai_web_search.httpx.AsyncClient", FakeAsyncClient)

    tool = AskWebSearchTool(OpenAIWebSearchClient(api_key="openai-test-key", model="gpt-5", timeout=45))
    context = ToolContext(
        settings=_settings(
            tmp_path,
            web_search_provider="openai",
            openai_web_search_api_key="openai-test-key",
        )
    )

    result = asyncio.run(tool.run({"query": "What is the latest TypeScript stable version?"}, context))

    assert result.ok is True
    assert result.data["provider"] == "openai-web-search"
    assert result.data["model"] == "gpt-5"
    assert "OpenAI web search" in result.data["answer"]
    assert captured["url"] == "https://api.openai.com/v1/responses"
    assert captured["headers"] == {
        "Authorization": "Bearer openai-test-key",
        "Content-Type": "application/json",
    }
    payload = captured["json"]
    assert isinstance(payload, dict)
    assert payload["tools"] == [{"type": "web_search"}]
    assert payload["input"][1]["content"][0]["text"] == "What is the latest TypeScript stable version?"


def test_ask_web_search_tool_uses_custom_openai_base_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        is_error = False
        status_code = 200

        def json(self):
            return {"output_text": "gateway answer"}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, *, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr("outline_agent.clients.openai_web_search.httpx.AsyncClient", FakeAsyncClient)

    custom_base_url = "https://openai-gateway.example.com/v1"
    tool = AskWebSearchTool(
        OpenAIWebSearchClient(
            api_key="openai-test-key",
            base_url=custom_base_url,
            model="gpt-5",
            timeout=45,
        )
    )
    context = ToolContext(
        settings=_settings(
            tmp_path,
            web_search_provider="openai",
            openai_web_search_api_key="openai-test-key",
            openai_web_search_base_url=custom_base_url,
        )
    )

    result = asyncio.run(tool.run({"query": "latest release?"}, context))

    assert result.ok is True
    assert captured["url"] == "https://openai-gateway.example.com/v1/responses"
