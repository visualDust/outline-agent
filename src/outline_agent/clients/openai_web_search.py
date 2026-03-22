from __future__ import annotations

from typing import Any

import httpx

from .web_search_base import DEFAULT_WEB_SEARCH_SYSTEM_INSTRUCTION, WebSearchClientError


class OpenAIWebSearchClient:
    provider = "openai-web-search"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-5",
        timeout: float = 120.0,
        system_instruction: str = DEFAULT_WEB_SEARCH_SYSTEM_INSTRUCTION,
    ) -> None:
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.model = model.strip()
        self.timeout = timeout
        self.system_instruction = system_instruction.strip()

    async def ask(self, query: str) -> str:
        cleaned_query = query.strip()
        if not cleaned_query:
            raise WebSearchClientError("query is required")

        url = f"{self.base_url}/responses"
        payload = {
            "model": self.model,
            "tools": [{"type": "web_search"}],
            "input": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": self.system_instruction}],
                },
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": cleaned_query}],
                },
            ],
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise WebSearchClientError(_format_httpx_error(exc, url=url)) from exc

        if response.is_error:
            raise WebSearchClientError(
                f"OpenAI web search API error {response.status_code}: {_extract_error_message(response)}"
            )

        data = response.json()
        if not isinstance(data, dict):
            raise WebSearchClientError("OpenAI web search API returned a non-object JSON response")

        output_text = data.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text.strip()

        text = _extract_text(data)
        if not text:
            raise WebSearchClientError("OpenAI web search returned no text content")
        return text


def _extract_text(response_data: dict[str, Any]) -> str:
    output = response_data.get("output")
    if not isinstance(output, list):
        return ""

    fragments: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                fragments.append(text.strip())
    return "\n".join(fragments).strip()


def _extract_error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text or response.reason_phrase

    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    return response.text or response.reason_phrase


def _format_httpx_error(exc: httpx.HTTPError, *, url: str) -> str:
    error_type = type(exc).__name__
    message = str(exc).strip()
    request = getattr(exc, "request", None)
    request_summary = ""
    if request is not None:
        method = getattr(request, "method", None) or "POST"
        request_url = getattr(request, "url", None) or url
        request_summary = f" during {method} {request_url}"
    else:
        request_summary = f" during POST {url}"

    if message:
        return f"OpenAI web search request failed ({error_type}){request_summary}: {message}"
    return f"OpenAI web search request failed ({error_type}){request_summary}"
