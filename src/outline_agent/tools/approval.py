from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

from ..core.config import AppSettings

if TYPE_CHECKING:
    from .base import ToolContext, ToolSpec


@dataclass(frozen=True)
class ToolApprovalRequest:
    tool_name: str
    args: dict[str, Any]
    side_effect_level: Literal["read", "write", "external"] = "read"
    requires_confirmation: bool = False
    spec: ToolSpec | None = None


@dataclass(frozen=True)
class ToolApprovalDecision:
    approved: bool
    reason: str | None = None


class ToolApprovalPolicy(Protocol):
    async def authorize(
        self,
        request: ToolApprovalRequest,
        context: ToolContext,
    ) -> ToolApprovalDecision: ...


class AlwaysAllowToolApprovalPolicy:
    async def authorize(
        self,
        request: ToolApprovalRequest,
        context: ToolContext,
    ) -> ToolApprovalDecision:
        del request, context
        return ToolApprovalDecision(approved=True, reason="approved by always-allow policy")


def build_tool_approval_policy(settings: AppSettings) -> ToolApprovalPolicy:
    if settings.tool_approval_mode == "always_allow":
        return AlwaysAllowToolApprovalPolicy()
    return AlwaysAllowToolApprovalPolicy()
