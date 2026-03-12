from __future__ import annotations

from ..clients.model_client import ModelClient
from ..clients.outline_client import (
    OutlineClient,
)
from ..core.config import AppSettings
from ..models.webhook_models import WebhookEnvelope
from ..state.store import ProcessedEventStore
from .processor_runtime import (
    handle_comment as _handle_comment,
)
from .processor_services import (
    build_processor_services,
)
from .processor_types import ProcessingResult


class CommentProcessor:
    def __init__(
        self,
        settings: AppSettings,
        store: ProcessedEventStore,
        outline_client: OutlineClient,
        model_client: ModelClient,
        memory_model_client: ModelClient | None = None,
        document_memory_model_client: ModelClient | None = None,
        document_update_model_client: ModelClient | None = None,
        tool_model_client: ModelClient | None = None,
        action_router_model_client: ModelClient | None = None,
    ):
        self.services = build_processor_services(
            settings=settings,
            store=store,
            outline_client=outline_client,
            model_client=model_client,
            memory_model_client=memory_model_client,
            document_memory_model_client=document_memory_model_client,
            document_update_model_client=document_update_model_client,
            tool_model_client=tool_model_client,
            action_router_model_client=action_router_model_client,
        )

    async def handle(self, envelope: WebhookEnvelope) -> ProcessingResult:
        return await _handle_comment(services=self.services, envelope=envelope)
