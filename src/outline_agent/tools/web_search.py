from __future__ import annotations

from typing import Any

from ..clients.web_search_base import WebSearchClient, WebSearchClientError
from .base import ToolContext, ToolError, ToolResult, ToolSpec


class AskWebSearchTool:
    def __init__(self, client: WebSearchClient):
        self.client = client

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="ask_web_search",
            description="Use the configured web search provider to answer fresh web-search questions.",
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
        except WebSearchClientError as exc:
            raise ToolError(str(exc)) from exc

        return ToolResult(
            ok=True,
            tool=self.spec.name,
            summary=f"ask_web_search[{query.strip()}] -> {len(answer)} chars",
            data={
                "query": query.strip(),
                "answer": answer,
                "provider": self.client.provider,
                "model": self.client.model,
            },
            preview=answer[:200] if answer else None,
        )
