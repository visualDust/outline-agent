from __future__ import annotations

from contextlib import asynccontextmanager
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import __version__
from .clients.model_client import ModelClient, ModelClientError
from .clients.outline_client import OutlineClient, OutlineClientError
from .core.config import get_settings, get_user_config_path
from .core.logging import configure_logging, logger
from .models.model_profiles import ModelProfileError, ModelProfileResolver
from .models.webhook_models import WebhookEnvelope
from .processing.processor import CommentProcessor
from .state.store import ProcessedEventStore
from .utils.error_reporting import format_failure_comment, generate_error_id
from .utils.rich_text import extract_mentions, extract_plain_text
from .utils.signature import verify_outline_signature

@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    configure_logging(settings)
    logger.info(
        "Starting {} on {}:{} (trigger_mode={}, dry_run={}, document_updates={}, reactions={}, log_level={})",
        settings.app_name,
        settings.host,
        settings.port,
        settings.trigger_mode,
        settings.dry_run,
        settings.document_update_enabled,
        settings.reaction_enabled,
        settings.log_level,
    )
    yield


app = FastAPI(title="Outline Agent", version=__version__, lifespan=lifespan)


@app.get("/")
@app.get("/healthz")
def healthz() -> dict[str, Any]:
    settings = get_settings()
    model_status: dict[str, Any]
    try:
        profile = ModelProfileResolver(get_user_config_path()).resolve(settings.model_ref)
        model_status = {
            "ok": True,
            "provider": profile.provider,
            "model": profile.model,
            "alias": profile.alias,
            "base_url": profile.base_url,
        }
    except ModelProfileError as exc:
        model_status = {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "service": settings.app_name,
        "trigger_mode": settings.trigger_mode,
        "collection_allowlist": settings.collection_allowlist,
        "allow_all_collections": settings.allow_all_collections,
        "mention_aliases": settings.mention_aliases,
        "workspace_root": str(settings.workspace_root),
        "system_prompt_path": str(settings.system_prompt_path),
        "document_update_enabled": settings.document_update_enabled,
        "document_update_model_ref": (
            settings.document_update_model_ref or settings.memory_model_ref or settings.model_ref
        ),
        "tool_use_enabled": settings.tool_use_enabled,
        "tool_model_ref": settings.tool_model_ref or settings.memory_model_ref or settings.model_ref,
        "tool_execution_max_rounds": settings.tool_execution_max_rounds,
        "memory_update_enabled": settings.memory_update_enabled,
        "memory_model_ref": settings.memory_model_ref or settings.model_ref,
        "thread_session_update_enabled": settings.thread_session_update_enabled,
        "thread_session_model_ref": (
            settings.thread_session_model_ref or settings.memory_model_ref or settings.model_ref
        ),
        "reaction_enabled": settings.reaction_enabled,
        "reaction_processing_emoji": settings.reaction_processing_emoji,
        "reaction_done_emoji": settings.reaction_done_emoji,
        "log_level": settings.log_level,
        "log_file_path": str(settings.log_file_path),
        "outline_api_base_url_configured": bool(settings.outline_api_base_url),
        "outline_api_configured": bool(settings.outline_api_key),
        "signing_secret_configured": bool(settings.outline_webhook_signing_secret),
        "model": model_status,
    }


@app.post("/outline/webhook")
async def outline_webhook(request: Request) -> JSONResponse:
    settings = get_settings()
    configure_logging(settings)
    body = await request.body()
    header_value = request.headers.get("Outline-Signature") or request.headers.get("outline-signature")
    signature_verified, signature_status = verify_outline_signature(
        settings.outline_webhook_signing_secret,
        header_value,
        body,
    )

    parsed_json = _try_parse_json(body)
    record = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "path": str(request.url.path),
        "headers": dict(request.headers.items()),
        "json": parsed_json,
        "signature_verified": signature_verified,
        "signature_status": signature_status,
    }
    _append_event_log(settings.webhook_log_dir, record)
    logger.debug(
        "Webhook received: path={}, signature_verified={}, signature_status={}, event={}",
        request.url.path,
        signature_verified,
        signature_status,
        parsed_json.get("event"),
    )

    if settings.outline_webhook_signing_secret and signature_verified is not True:
        raise HTTPException(status_code=401, detail=f"invalid webhook signature: {signature_status}")

    try:
        envelope = WebhookEnvelope.model_validate(parsed_json)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=f"invalid webhook payload: {exc}") from exc

    if envelope.event != "comments.create":
        return JSONResponse(
            {
                "ok": True,
                "event": envelope.event,
                "signature_verified": signature_verified,
                "signature_status": signature_status,
                "action": "ignored",
                "reason": "unsupported-event",
            }
        )

    if not settings.outline_api_base_url:
        raise HTTPException(status_code=500, detail="OUTLINE_API_BASE_URL is not configured")

    outline_client = OutlineClient(
        base_url=settings.outline_api_base_url,
        api_key=settings.outline_api_key,
        timeout=settings.outline_timeout_seconds,
    )
    store = ProcessedEventStore(settings.dedupe_store_path)

    try:
        model_resolver = ModelProfileResolver(get_user_config_path())
        profile = model_resolver.resolve(settings.model_ref)
        memory_profile = (
            model_resolver.resolve(settings.memory_model_ref or settings.model_ref)
            if settings.memory_update_enabled
            else profile
        )
        action_router_profile = model_resolver.resolve(
            settings.action_router_model_ref or settings.model_ref
        )
        document_update_profile = (
            model_resolver.resolve(
                settings.document_update_model_ref or settings.memory_model_ref or settings.model_ref
            )
            if settings.document_update_enabled
            else memory_profile
        )
        tool_profile = (
            model_resolver.resolve(
                settings.tool_model_ref or settings.memory_model_ref or settings.model_ref
            )
            if settings.tool_use_enabled
            else memory_profile
        )
        thread_session_profile = (
            model_resolver.resolve(
                settings.thread_session_model_ref or settings.memory_model_ref or settings.model_ref
            )
            if settings.thread_session_update_enabled
            else memory_profile
        )
        model_client = ModelClient(
            profile=profile,
            timeout=settings.model_timeout_seconds,
            max_output_tokens=settings.max_output_tokens,
        )
        memory_model_client = ModelClient(
            profile=memory_profile,
            timeout=settings.model_timeout_seconds,
            max_output_tokens=settings.max_output_tokens,
        )
        action_router_model_client = ModelClient(
            profile=action_router_profile,
            timeout=settings.model_timeout_seconds,
            max_output_tokens=settings.max_output_tokens,
        )
        document_update_model_client = ModelClient(
            profile=document_update_profile,
            timeout=settings.model_timeout_seconds,
            max_output_tokens=settings.max_output_tokens,
        )
        tool_model_client = ModelClient(
            profile=tool_profile,
            timeout=settings.model_timeout_seconds,
            max_output_tokens=settings.max_output_tokens,
        )
        thread_session_model_client = ModelClient(
            profile=thread_session_profile,
            timeout=settings.model_timeout_seconds,
            max_output_tokens=settings.max_output_tokens,
        )
        processor = CommentProcessor(
            settings=settings,
            store=store,
            outline_client=outline_client,
            model_client=model_client,
            memory_model_client=memory_model_client,
            thread_session_model_client=thread_session_model_client,
            document_update_model_client=document_update_model_client,
            tool_model_client=tool_model_client,
            action_router_model_client=action_router_model_client,
        )
        result = await processor.handle(envelope)
        logger.info(
            "Webhook processed: action={}, reason={}, comment_id={}, document_id={}, collection_id={}",
            result.action,
            result.reason,
            result.comment_id,
            result.document_id,
            result.collection_id,
        )
    except (ModelProfileError, ModelClientError, OutlineClientError) as exc:
        logger.exception("Webhook processing failed")
        await _maybe_post_failure_comment(
            settings=settings,
            envelope=envelope,
            outline_client=outline_client,
            store=store,
            exc=exc,
        )
        return JSONResponse(
            {
                "ok": True,
                "event": envelope.event,
                "signature_verified": signature_verified,
                "signature_status": signature_status,
                "action": "error",
                "reason": "internal-error",
                "detail": str(exc),
            }
        )

    return JSONResponse(
        {
            "ok": True,
            "event": envelope.event,
            "signature_verified": signature_verified,
            "signature_status": signature_status,
            **result.as_dict(),
        }
    )


async def _maybe_post_failure_comment(
    *,
    settings: Any,
    envelope: WebhookEnvelope,
    outline_client: OutlineClient,
    store: ProcessedEventStore,
    exc: BaseException,
) -> None:
    if envelope.event != "comments.create":
        return
    comment = envelope.payload.model
    semantic_key = f"comments.create:{comment.id}"
    if store.contains(semantic_key):
        return

    self_authored_user_ids = {
        user_id
        for user_id in (settings.outline_agent_user_id, settings.runtime_outline_user_id)
        if user_id
    }
    if envelope.actorId in self_authored_user_ids or comment.createdById in self_authored_user_ids:
        return

    if settings.trigger_mode == "all":
        triggered = True
    else:
        comment_text = extract_plain_text(comment.data)
        mentions = extract_mentions(comment.data)
        triggered = False
        if settings.outline_agent_user_id and any(m.model_id == settings.outline_agent_user_id for m in mentions):
            triggered = True
        elif settings.mention_alias_fallback_enabled:
            lowered = comment_text.lower()
            triggered = any(alias.lower() in lowered for alias in settings.mention_aliases)

    if not triggered:
        return

    error_id = generate_error_id()
    text = format_failure_comment(error_id=error_id, exc=exc)
    parent_comment_id = comment.parentCommentId or comment.id
    try:
        await outline_client.create_comment(
            document_id=comment.documentId,
            text=text,
            parent_comment_id=parent_comment_id,
        )
    except OutlineClientError as post_exc:
        logger.warning("Failed to post failure comment for request {}: {}", comment.id, post_exc)
        return
    store.add(semantic_key)


def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    uvicorn.run(
        "outline_agent.app:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level=settings.log_level.lower(),
    )


def _append_event_log(log_dir: Path, record: dict[str, Any]) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    events_path = log_dir / "events.jsonl"
    last_event_path = log_dir / "last_event.json"
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    last_event_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def _try_parse_json(body: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(body.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"request body is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    return parsed
