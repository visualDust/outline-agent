from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, Field

from ..clients.outline_client import OutlineClient
from ..clients.outline_models import OutlineCollection, OutlineDocument
from ..core.config import AppSettings

if TYPE_CHECKING:
    from .approval import ToolApprovalPolicy


class ToolSpec(BaseModel):
    name: str
    description: str
    when_to_use: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    side_effect_level: Literal["read", "write", "external"] = "read"
    requires_confirmation: bool = False


class ToolResult(BaseModel):
    ok: bool = True
    tool: str
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    preview: str | None = None
    error: str | None = None


@dataclass(slots=True)
class ToolContext:
    settings: AppSettings
    outline_client: OutlineClient | None = None
    work_dir: Path | None = None
    document: OutlineDocument | None = None
    collection: OutlineCollection | None = None
    tool_approval_policy: ToolApprovalPolicy | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class AgentTool(Protocol):
    @property
    def spec(self) -> ToolSpec: ...

    async def run(self, args: dict[str, Any], context: ToolContext) -> ToolResult: ...


class ToolError(RuntimeError):
    """Raised when a tool cannot complete safely."""
