from __future__ import annotations

from typing import Any

from ..clients.gemini_web_search import GeminiWebSearchClient, GeminiWebSearchClientError
from .base import ToolContext, ToolError, ToolResult, ToolSpec


class AskGeminiWebSearchTool:
    def __init__(self, client: GeminiWebSearchClient):
        self.client = client

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="ask_gemini_web_search",
            description="Use Gemini with Google Search enabled to answer fresh web-search questions.",
            when_to_use=(
                "Use when the user asks for recent, current, or web-dependent information that is not reliably "
                "available from the local Outline document, thread, or workspace alone."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                },
                "required": ["query"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "answer": {"type": "string"},
                    "provider": {"type": "string"},
                    "model": {"type": "string"},
                },
            },
            side_effect_level="external",
        )

    async def run(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        del context
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ToolError("query is required")

        try:
            answer = await self.client.ask(query)
        except GeminiWebSearchClientError as exc:
            raise ToolError(str(exc)) from exc

        return ToolResult(
            ok=True,
            tool=self.spec.name,
            summary=f"ask_gemini_web_search[{query.strip()}] -> {len(answer)} chars",
            data={
                "query": query.strip(),
                "answer": answer,
                "provider": "gemini-google-search",
                "model": self.client.model,
            },
            preview=answer[:200] if answer else None,
        )
