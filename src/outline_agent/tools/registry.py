from __future__ import annotations

from typing import Iterable

from ..clients.model_client import ModelClientError
from ..clients.outline_client import OutlineClientError
from ..core.logging import logger
from .approval import ToolApprovalRequest, build_tool_approval_policy
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

        approval_result = await self._authorize(tool.spec, dict(args), context)
        if approval_result is not None:
            return approval_result

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
        return self._attach_approval_metadata(
            result,
            spec=tool.spec,
            context=context,
            status="approved" if tool.spec.requires_confirmation else "not-required",
            reason="approved by current policy" if tool.spec.requires_confirmation else None,
        )

    async def _authorize(
        self,
        spec: ToolSpec,
        args: dict[str, object],
        context: ToolContext,
    ) -> ToolResult | None:
        policy = context.tool_approval_policy or build_tool_approval_policy(context.settings)
        request = ToolApprovalRequest(
            tool_name=spec.name,
            args=dict(args),
            side_effect_level=spec.side_effect_level,
            requires_confirmation=spec.requires_confirmation,
            spec=spec,
        )
        try:
            decision = await policy.authorize(request, context)
        except (ToolError, ValueError) as exc:
            return ToolResult(
                ok=False,
                tool=spec.name,
                summary=f"{spec.name}: approval failed: {exc}",
                error="approval-error",
                data={
                    "approval_reason": str(exc),
                    "approval": {
                        "required": spec.requires_confirmation,
                        "status": "error",
                        "mode": context.settings.tool_approval_mode,
                        "reason": str(exc),
                    },
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected approval policy failure for {}", spec.name)
            return ToolResult(
                ok=False,
                tool=spec.name,
                summary=f"{spec.name}: approval failed: unexpected error: {exc}",
                error="approval-error",
                data={
                    "approval_reason": str(exc),
                    "approval": {
                        "required": spec.requires_confirmation,
                        "status": "error",
                        "mode": context.settings.tool_approval_mode,
                        "reason": str(exc),
                    },
                },
            )

        if decision.approved:
            return None

        reason = (decision.reason or "tool execution was not approved").strip()
        return ToolResult(
            ok=False,
            tool=spec.name,
            summary=f"{spec.name}: approval denied: {reason}",
            error="approval-denied",
            data={
                "approval_reason": reason,
                "approval": {
                    "required": spec.requires_confirmation,
                    "status": "denied",
                    "mode": context.settings.tool_approval_mode,
                    "reason": reason,
                },
            },
        )

    @staticmethod
    def _attach_approval_metadata(
        result: ToolResult,
        *,
        spec: ToolSpec,
        context: ToolContext,
        status: str,
        reason: str | None,
    ) -> ToolResult:
        data = dict(result.data)
        data["approval"] = {
            "required": spec.requires_confirmation,
            "status": status,
            "mode": context.settings.tool_approval_mode,
            "reason": reason,
        }
        return result.model_copy(update={"data": data})
