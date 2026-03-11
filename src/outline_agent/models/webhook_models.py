from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CommentModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    data: dict[str, Any] = Field(default_factory=dict)
    documentId: str
    parentCommentId: str | None = None
    createdById: str | None = None


class WebhookPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    model: CommentModel


class WebhookEnvelope(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    event: str
    actorId: str | None = None
    webhookSubscriptionId: str | None = None
    createdAt: str | None = None
    payload: WebhookPayload
