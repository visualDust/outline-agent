from __future__ import annotations

from pydantic import BaseModel

from ..clients.model_client import ModelClient
from ..clients.outline_models import OutlineCollection, OutlineDocument
from ..core.config import AppSettings
from ..core.prompt_registry import PromptRegistry
from ..state.workspace import DocumentWorkspace, ThreadWorkspace
from ..utils.json_utils import JsonExtractionError, extract_json_object

ACTION_ROUTER_SYSTEM_PROMPT = (
    "You decide which special routing subsystems should be invoked for a single "
    "Outline comment.\n\n"
    "Main document creation, document update, and local tool work are handled "
    "elsewhere by a unified tool planner.\n"
    "This router is only for special control paths.\n\n"
    "Return strict JSON only with this schema:\n"
    "{\n"
    '  "memory_action": false,\n'
    '  "cross_thread_handoff": false,\n'
    '  "same_document_comment_lookup": false,\n'
    '  "reason": "short explanation"\n'
    "}\n\n"
    "Rules:\n"
    "- `memory_action = true` only when the user explicitly asks to remember, "
    "forget, correct, or manage collection memory\n"
    "- If `cross_thread_handoff = true`, set "
    "`same_document_comment_lookup = false`\n"
)


class ActionRoutingDecision(BaseModel):
    memory_action: bool = False
    cross_thread_handoff: bool = False
    same_document_comment_lookup: bool = False
    reason: str | None = None


class ActionRouterManager:
    def __init__(
        self,
        settings: AppSettings,
        model_client: ModelClient,
        *,
        prompt_registry: PromptRegistry | None = None,
    ):
        self.settings = settings
        self.model_client = model_client
        self.prompt_registry = prompt_registry or PromptRegistry.from_settings(settings)

    async def decide(
        self,
        *,
        document_workspace: DocumentWorkspace,
        thread_workspace: ThreadWorkspace,
        collection: OutlineCollection | None,
        document: OutlineDocument,
        user_comment: str,
        comment_context: str,
    ) -> ActionRoutingDecision:
        user_prompt = self._build_user_prompt(
            document_workspace=document_workspace,
            thread_workspace=thread_workspace,
            collection=collection,
            document=document,
            user_comment=user_comment,
            comment_context=comment_context,
        )
        system_prompt = self.prompt_registry.compose_internal_prompt(
            ACTION_ROUTER_SYSTEM_PROMPT,
            "action_router_policy.md",
        )
        raw = await self.model_client.generate_reply(system_prompt, user_prompt)
        try:
            payload = extract_json_object(raw)
        except JsonExtractionError:
            return ActionRoutingDecision()

        decision = ActionRoutingDecision.model_validate(payload)
        if decision.cross_thread_handoff:
            return ActionRoutingDecision(
                memory_action=decision.memory_action,
                cross_thread_handoff=True,
                same_document_comment_lookup=False,
                reason=decision.reason,
            )
        return decision

    def _build_user_prompt(
        self,
        *,
        document_workspace: DocumentWorkspace,
        thread_workspace: ThreadWorkspace,
        collection: OutlineCollection | None,
        document: OutlineDocument,
        user_comment: str,
        comment_context: str,
    ) -> str:
        collection_name = collection.name if collection and collection.name else document.collection_id or "(unknown)"
        document_memory = _truncate(
            document_workspace.load_prompt_context(self.settings.max_document_memory_chars),
            self.settings.max_document_memory_chars,
        )
        return (
            f"Collection: {collection_name}\n"
            f"Collection ID: {document.collection_id or '(unknown)'}\n"
            f"Document title: {document.title or '(unknown)'}\n"
            f"Document ID: {document.id}\n"
            f"Thread ID: {thread_workspace.thread_id}\n\n"
            "Persisted document memory:\n"
            f"{document_memory or '(no document memory)'}\n\n"
            "Relevant comment context:\n"
            f"{comment_context or '(no comment context)'}\n\n"
            "Latest user comment:\n"
            f"{_truncate(user_comment, self.settings.max_prompt_chars)}"
        )


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"
