from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from ..core.config import AppSettings

DEFAULT_GEMINI_WEB_SEARCH_SYSTEM_INSTRUCTION = " ".join(
    [
        "You are being used as a recent-information lookup helper for another AI assistant.",
        "Your output will be consumed as external reference material, not shown as a casual end-user chat reply.",
        (
            "For recent, current, latest, today, this week, this month, this year, version, release, pricing, "
            "policy, news, or other time-sensitive questions, prefer Google Search results instead of relying "
            "only on parametric memory."
        ),
        (
            "Prioritize factual accuracy, recency, professional wording, clear structure, and key dates, "
            "versions, and qualifiers when relevant."
        ),
        (
            "Unless the user explicitly requests a different format, prefer this structure when appropriate: "
            "(1) a direct answer first, (2) a compact set of key supporting details, and (3) a short "
            "uncertainty/conflict note only if needed."
        ),
        (
            "When useful, anchor claims with concrete dates, version numbers, release stage, region, or "
            "other scope conditions so the downstream assistant can reason about them reliably."
        ),
        "If search results are insufficient, ambiguous, or conflicting, say so briefly and explicitly.",
        "Do not include filler, self-referential AI disclaimers, or commentary about your internal process.",
        "Return only the answer content.",
    ]
)


class GeminiWebSearchClientError(RuntimeError):
    """Raised when Gemini web search invocation fails."""


@dataclass(frozen=True)
class GeminiWebSearchProbe:
    available: bool
    backend: str
    base_url: str
    model: str
    reason: str | None = None


class GeminiWebSearchClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://generativelanguage.googleapis.com",
        model: str = "gemini-3-flash-preview",
        timeout: float = 120.0,
        system_instruction: str = DEFAULT_GEMINI_WEB_SEARCH_SYSTEM_INSTRUCTION,
    ) -> None:
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.model = model.strip()
        self.timeout = timeout
        self.system_instruction = system_instruction.strip()

    async def ask(self, query: str) -> str:
        cleaned_query = query.strip()
        if not cleaned_query:
            raise GeminiWebSearchClientError("query is required")

        url = f"{self.base_url}/v1beta/models/{self.model}:generateContent"
        payload = {
            "system_instruction": {
                "parts": [
                    {
                        "text": self.system_instruction,
                    }
                ]
            },
            "contents": [
                {
                    "parts": [
                        {
                            "text": cleaned_query,
                        }
                    ]
                }
            ],
            "tools": [{"google_search": {}}],
            "generationConfig": {"temperature": 0.2},
        }
        headers = {
            "x-goog-api-key": self.api_key,
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise GeminiWebSearchClientError(_format_httpx_error(exc, url=url)) from exc

        if response.is_error:
            raise GeminiWebSearchClientError(
                f"Gemini API error {response.status_code}: {_extract_error_message(response)}"
            )

        data = response.json()
        if not isinstance(data, dict):
            raise GeminiWebSearchClientError("Gemini API returned a non-object JSON response")

        text = _extract_text(data)
        if not text:
            raise GeminiWebSearchClientError("Gemini returned no text content")
        return text


def has_gemini_web_search_api_key(settings: AppSettings) -> bool:
    return bool(settings.gemini_api_key and settings.gemini_api_key.strip())


def get_gemini_web_search_probe(settings: AppSettings) -> GeminiWebSearchProbe:
    if has_gemini_web_search_api_key(settings):
        return GeminiWebSearchProbe(
            available=True,
            backend="gemini-web-search",
            base_url=settings.gemini_base_url,
            model=settings.gemini_model,
            reason=None,
        )
    return GeminiWebSearchProbe(
        available=False,
        backend="gemini-web-search",
        base_url=settings.gemini_base_url,
        model=settings.gemini_model,
        reason="GEMINI_API_KEY / GOOGLE_API_KEY is not configured",
    )


def _extract_text(response_data: dict[str, Any]) -> str:
    candidates = response_data.get("candidates")
    if not isinstance(candidates, list):
        return ""

    fragments: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        if not isinstance(content, dict):
            continue
        parts = content.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
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
        return f"Gemini web search request failed ({error_type}){request_summary}: {message}"
    return f"Gemini web search request failed ({error_type}){request_summary}"
