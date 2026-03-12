from __future__ import annotations

from typing import Iterable

from ..clients.model_client import ModelClientError
from ..clients.outline_client import OutlineClientError
from ..core.logging import logger
from .base import AgentTool, ToolContext, ToolError, ToolResult, ToolSpec


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, AgentTool] = {}

    def register(self, tool: AgentTool) -> None:
        name = tool.spec.name
        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
        self._tools[name] = tool

    def register_many(self, tools: Iterable[AgentTool]) -> None:
        for tool in tools:
            self.register(tool)

    def get(self, name: str) -> AgentTool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"unknown tool: {name}") from exc

    def has(self, name: str) -> bool:
        return name in self._tools

    def list_specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self._tools.values()]

    async def execute(self, name: str, args: dict[str, object], context: ToolContext) -> ToolResult:
        try:
            tool = self.get(name)
        except KeyError as exc:
            return ToolResult(ok=False, tool=name, summary=str(exc), error=str(exc))

        try:
            result = await tool.run(dict(args), context)
        except (ToolError, OutlineClientError, ModelClientError, ValueError, FileNotFoundError) as exc:
            return ToolResult(ok=False, tool=name, summary=f"{name}: {exc}", error=str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected tool failure for {}", name)
            return ToolResult(ok=False, tool=name, summary=f"{name}: unexpected error: {exc}", error=str(exc))

        if result.tool != name:
            return ToolResult(
                ok=False,
                tool=name,
                summary=f"{name}: tool returned mismatched result name {result.tool}",
                error="mismatched-tool-name",
            )
        return result
