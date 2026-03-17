from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CommentModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    data: dict[str, Any] = Field(default_factory=dict)
    documentId: str
    parentCommentId: str | None = None
    createdById: str | None = None


class DocumentDeleteModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    documentId: str | None = None
    collectionId: str | None = None
    title: str | None = None

    @property
    def resolved_document_id(self) -> str:
        return self.documentId or self.id


class CollectionDeleteModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    collectionId: str | None = None
    name: str | None = None

    @property
    def resolved_collection_id(self) -> str:
        return self.collectionId or self.id


WebhookEventModel = CommentModel | DocumentDeleteModel | CollectionDeleteModel


class WebhookPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    model: WebhookEventModel


class WebhookEnvelope(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    event: str
    actorId: str | None = None
    webhookSubscriptionId: str | None = None
    createdAt: str | None = None
    payload: WebhookPayload

    @model_validator(mode="before")
    @classmethod
    def _coerce_payload_model(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value

        event = value.get("event")
        payload = value.get("payload")
        if not isinstance(event, str) or not isinstance(payload, dict):
            return value

        model = payload.get("model")
        if not isinstance(model, dict):
            return value

        model_cls: type[BaseModel] | None = None
        if event.startswith("comments."):
            model_cls = CommentModel
        elif event == "documents.delete":
            model_cls = DocumentDeleteModel
        elif event == "collections.delete":
            model_cls = CollectionDeleteModel

        if model_cls is None:
            return value

        updated_payload = dict(payload)
        updated_payload["model"] = model_cls.model_validate(model)
        updated_value = dict(value)
        updated_value["payload"] = updated_payload
        return updated_value
