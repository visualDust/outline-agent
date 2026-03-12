from __future__ import annotations

from typing import Any

from .clients.model_client import ModelClient
from .clients.outline_client import OutlineClient, OutlineUser
from .core.config import AppSettings, get_user_config_path
from .models.model_profiles import ModelProfileError, ModelProfileResolver
from .processing.processor import CommentProcessor
from .processing.processor_identity import cache_runtime_identity
from .state.store import ProcessedEventStore


def resolve_primary_model_status(settings: AppSettings) -> dict[str, Any]:
    try:
        profile = ModelProfileResolver(get_user_config_path()).resolve(settings.model_ref)
    except ModelProfileError as exc:
        return {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "provider": profile.provider,
        "model": profile.model,
        "alias": profile.alias,
        "base_url": profile.base_url,
    }


def build_outline_client(settings: AppSettings) -> OutlineClient:
    if not settings.outline_api_base_url:
        raise ValueError("OUTLINE_API_BASE_URL is not configured")

    return OutlineClient(
        base_url=settings.outline_api_base_url,
        api_key=settings.outline_api_key,
        timeout=settings.outline_timeout_seconds,
    )


def build_request_resources(settings: AppSettings) -> tuple[OutlineClient, ProcessedEventStore]:
    outline_client = build_outline_client(settings)
    store = ProcessedEventStore(settings.dedupe_store_path)
    return outline_client, store


async def validate_outline_runtime_identity(settings: AppSettings) -> OutlineUser:
    current_user = await build_outline_client(settings).current_user()
    cache_runtime_identity(settings=settings, current_user=current_user)
    return current_user


def build_comment_processor(
    *,
    settings: AppSettings,
    outline_client: OutlineClient,
    store: ProcessedEventStore,
) -> CommentProcessor:
    model_resolver = ModelProfileResolver(get_user_config_path())
    profile = model_resolver.resolve(settings.model_ref)
    memory_profile = (
        model_resolver.resolve(settings.memory_model_ref or settings.model_ref)
        if settings.memory_update_enabled
        else profile
    )
    action_router_profile = model_resolver.resolve(settings.action_router_model_ref or settings.model_ref)
    document_update_profile = (
        model_resolver.resolve(settings.document_update_model_ref or settings.memory_model_ref or settings.model_ref)
        if settings.document_update_enabled
        else memory_profile
    )
    tool_profile = (
        model_resolver.resolve(settings.tool_model_ref or settings.memory_model_ref or settings.model_ref)
        if settings.tool_use_enabled
        else memory_profile
    )
    document_memory_profile = (
        model_resolver.resolve(settings.document_memory_model_ref or settings.memory_model_ref or settings.model_ref)
        if settings.document_memory_update_enabled
        else memory_profile
    )

    return CommentProcessor(
        settings=settings,
        store=store,
        outline_client=outline_client,
        model_client=_build_model_client(profile, settings),
        memory_model_client=_build_model_client(memory_profile, settings),
        document_memory_model_client=_build_model_client(document_memory_profile, settings),
        document_update_model_client=_build_model_client(document_update_profile, settings),
        tool_model_client=_build_model_client(tool_profile, settings),
        action_router_model_client=_build_model_client(action_router_profile, settings),
    )


def _build_model_client(profile: Any, settings: AppSettings) -> ModelClient:
    return ModelClient(
        profile=profile,
        timeout=settings.model_timeout_seconds,
        max_output_tokens=settings.max_output_tokens,
    )
