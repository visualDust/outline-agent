from __future__ import annotations

import asyncio

import pytest

from outline_agent.clients.model_client import ModelClient, ModelInputImage
from outline_agent.models.model_profiles import ResolvedModelProfile


def test_model_client_openai_responses_sends_input_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = ResolvedModelProfile(
        alias="demo",
        provider="openai-responses",
        base_url="https://example.test/v1",
        api_key="test-key",
        model="gpt-4.1-mini",
    )
    client = ModelClient(profile=profile, timeout=5, max_output_tokens=123)
    captured: dict[str, object] = {}

    class FakeResponse:
        is_error = False
        status_code = 200

        def json(self):
            return {"output_text": "It looks like a chart."}

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

    monkeypatch.setattr("outline_agent.clients.model_client.httpx.AsyncClient", FakeAsyncClient)

    result = asyncio.run(
        client.generate_reply_with_images(
            "You are helpful.",
            "What is in this image?",
            input_images=[ModelInputImage(data=b"\x89PNG\r\n\x1a\nfake-image", media_type="image/png")],
        )
    )

    assert result == "It looks like a chart."
    assert captured["url"] == "https://example.test/v1/responses"
    payload = captured["json"]
    assert isinstance(payload, dict)
    assert payload["model"] == "gpt-4.1-mini"
    user_message = payload["input"][1]
    assert user_message["role"] == "user"
    assert user_message["content"][0] == {"type": "input_text", "text": "What is in this image?"}
    image_item = user_message["content"][1]
    assert image_item["type"] == "input_image"
    assert image_item["image_url"].startswith("data:image/png;base64,")
