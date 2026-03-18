from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from outline_agent.clients.gemini_web_search import GeminiWebSearchClient, get_gemini_web_search_probe
from outline_agent.core.config import AppSettings
from outline_agent.core.prompt_registry import PromptRegistry
from outline_agent.tools import ToolContext, build_default_tool_registry
from outline_agent.tools.gemini_web_search import AskGeminiWebSearchTool


class DummyDraftingClient:
    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        del system_prompt, user_prompt
        return "unused"


def _settings(
    tmp_path: Path,
    *,
    gemini_api_key: str | None = None,
    gemini_base_url: str = "https://generativelanguage.googleapis.com",
) -> AppSettings:
    return AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=False,
        gemini_api_key=gemini_api_key,
        gemini_base_url=gemini_base_url,
    )


def test_default_tool_registry_hides_ask_gemini_web_search_without_api_key(tmp_path: Path) -> None:
    settings = _settings(tmp_path, gemini_api_key=None)

    registry = build_default_tool_registry(
        settings=settings,
        drafting_model_client=DummyDraftingClient(),  # type: ignore[arg-type]
        prompt_registry=PromptRegistry.from_settings(settings),
    )

    assert registry.has("ask_gemini_web_search") is False

    probe = get_gemini_web_search_probe(settings)
    assert probe.available is False
    assert probe.backend == "gemini-web-search"
    assert probe.reason == "GEMINI_API_KEY / GOOGLE_API_KEY is not configured"


def test_default_tool_registry_registers_ask_gemini_web_search_with_api_key(tmp_path: Path) -> None:
    settings = _settings(tmp_path, gemini_api_key="gemini-test-key")

    registry = build_default_tool_registry(
        settings=settings,
        drafting_model_client=DummyDraftingClient(),  # type: ignore[arg-type]
        prompt_registry=PromptRegistry.from_settings(settings),
    )

    assert registry.has("ask_gemini_web_search") is True
    probe = get_gemini_web_search_probe(settings)
    assert probe.available is True
    assert probe.model == "gemini-3-flash-preview"


def test_ask_gemini_web_search_tool_calls_gemini_api(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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

    tool = AskGeminiWebSearchTool(
        GeminiWebSearchClient(api_key="gemini-test-key", model="gemini-3-flash-preview", timeout=45)
    )
    context = ToolContext(settings=_settings(tmp_path, gemini_api_key="gemini-test-key"))

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


def test_ask_gemini_web_search_tool_uses_custom_base_url(
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
    tool = AskGeminiWebSearchTool(
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
            gemini_api_key="gemini-test-key",
            gemini_base_url=custom_base_url,
        )
    )

    result = asyncio.run(tool.run({"query": "latest release?"}, context))

    assert result.ok is True
    assert captured["url"] == "https://gemini-gateway.example.com/v1beta/models/gemini-3-flash-preview:generateContent"
