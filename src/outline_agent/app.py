from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import __version__
from .bootstrap import (
    build_comment_processor,
    build_request_resources,
    resolve_primary_model_status,
    validate_outline_runtime_identity,
)
from .clients.model_client import ModelClientError
from .clients.outline_client import OutlineClient, OutlineClientError
from .clients.web_search import get_web_search_probe, has_active_web_search_api_key
from .core.config import AppSettings, get_settings
from .core.logging import configure_logging, logger
from .models.model_profiles import ModelProfileError
from .models.webhook_models import WebhookEnvelope
from .processing.processor_identity import invalidate_runtime_identity, is_outline_auth_error
from .state.store import ProcessedEventStore
from .utils.error_reporting import format_failure_comment, generate_error_id
from .utils.mermaid_validation import get_mermaid_validator_probe
from .utils.rich_text import extract_mentions, extract_plain_text
from .utils.signature import verify_outline_signature


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    configure_logging(settings)
    if not settings.runtime_outline_user_id:
        await validate_outline_runtime_identity(settings)
    mermaid_probe = get_mermaid_validator_probe(settings, log_warning=True)
    web_search_probe = get_web_search_probe(settings)
    logger.info(
        "Starting {} on {}:{} (trigger_mode={}, dry_run={}, document_updates={}, "
        "reactions={}, mermaid_validation={}::{}::{}, mermaid_available={}, tool_rounds={}, "
        "planner_step_budget={}, execution_chunk_size={}, log_level={})",
        settings.app_name,
        settings.host,
        settings.port,
        settings.trigger_mode,
        settings.dry_run,
        settings.document_update_enabled,
        settings.reaction_enabled,
        settings.mermaid_validation_enabled,
        settings.mermaid_validation_mode,
        settings.mermaid_validation_exhausted_action,
        mermaid_probe.available,
        settings.tool_execution_max_rounds,
        settings.tool_execution_max_steps,
        settings.tool_execution_chunk_size,
        settings.log_level,
    )
    if settings.tool_use_enabled and web_search_probe.available:
        logger.info(
            "Web search ready: provider={}, backend={}, model={}, base_url={}",
            web_search_probe.provider,
            web_search_probe.backend,
            web_search_probe.model,
            web_search_probe.base_url,
        )
    elif settings.tool_use_enabled:
        logger.warning("ask_web_search is unavailable: {}", web_search_probe.reason or "unknown reason")
    outline_client = None
    store = None
    processor = None
    try:
        outline_client, store = build_request_resources(settings)
        processor = build_comment_processor(
            settings=settings,
            outline_client=outline_client,
            store=store,
        )
    except Exception:
        logger.exception("Failed to initialize shared Outline processor resources")
    app.state.outline_client = outline_client
    app.state.store = store
    app.state.processor = processor
    yield


app = FastAPI(title="Outline Agent", version=__version__, lifespan=lifespan)


@app.get("/")
@app.get("/healthz")
def healthz() -> dict[str, Any]:
    settings = get_settings()
    model_status = resolve_primary_model_status(settings)
    mermaid_probe = get_mermaid_validator_probe(settings)
    web_search_probe = get_web_search_probe(settings)

    return {
        "ok": True,
        "service": settings.app_name,
        "trigger_mode": settings.trigger_mode,
        "collection_allowlist": settings.collection_allowlist,
        "allow_all_collections": settings.allow_all_collections,
        "mention_aliases": settings.mention_aliases,
        "workspace_root": str(settings.workspace_root),
        "system_prompt_path": str(settings.system_prompt_path),
        "prompt_pack_dir": str(settings.prompt_pack_dir),
        "internal_prompt_dir": str(settings.internal_prompt_dir),
        "document_update_enabled": settings.document_update_enabled,
        "document_update_model_ref": (
            settings.document_update_model_ref or settings.memory_model_ref or settings.model_ref
        ),
        "tool_use_enabled": settings.tool_use_enabled,
        "ask_web_search_available": has_active_web_search_api_key(settings),
        "web_search_provider": settings.web_search_provider,
        "web_search": {
            "available": web_search_probe.available,
            "provider": web_search_probe.provider,
            "backend": web_search_probe.backend,
            "base_url": web_search_probe.base_url,
            "model": web_search_probe.model,
            "reason": web_search_probe.reason,
        },
        "gemini_base_url": settings.gemini_base_url,
        "gemini_model": settings.gemini_model,
        "openai_web_search_base_url": settings.openai_web_search_base_url,
        "openai_web_search_model": settings.openai_web_search_model,
        "tool_model_ref": settings.tool_model_ref or settings.memory_model_ref or settings.model_ref,
        "tool_execution_max_rounds": settings.tool_execution_max_rounds,
        "tool_execution_max_steps": settings.tool_execution_max_steps,
        "tool_execution_chunk_size": settings.tool_execution_chunk_size,
        "memory_update_enabled": settings.memory_update_enabled,
        "memory_model_ref": settings.memory_model_ref or settings.model_ref,
        "document_memory_update_enabled": settings.document_memory_update_enabled,
        "document_memory_model_ref": (
            settings.document_memory_model_ref or settings.memory_model_ref or settings.model_ref
        ),
        "reaction_enabled": settings.reaction_enabled,
        "mermaid_validation_enabled": settings.mermaid_validation_enabled,
        "mermaid_validation_mode": settings.mermaid_validation_mode,
        "mermaid_validation_max_retries": settings.mermaid_validation_max_retries,
        "mermaid_validation_exhausted_action": settings.mermaid_validation_exhausted_action,
        "mermaid_validation_timeout_seconds": settings.mermaid_validation_timeout_seconds,
        "mermaid_validator": {
            "available": mermaid_probe.available,
            "backend": mermaid_probe.backend,
            "reason": mermaid_probe.reason,
            "version": mermaid_probe.version,
        },
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

    event_name = parsed_json.get("event")
    if not isinstance(event_name, str):
        raise HTTPException(status_code=400, detail="invalid webhook payload: missing event")

    if event_name not in {
        "comments.create",
        "comments.update",
        "comments.delete",
        "documents.create",
        "documents.update",
        "documents.delete",
        "collections.delete",
    }:
        return JSONResponse(
            {
                "ok": True,
                "event": event_name,
                "signature_verified": signature_verified,
                "signature_status": signature_status,
                "action": "ignored",
                "reason": "unsupported-event",
            }
        )

    try:
        envelope = WebhookEnvelope.model_validate(parsed_json)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=f"invalid webhook payload: {exc}") from exc

    try:
        outline_client = getattr(app.state, "outline_client", None)
        store = getattr(app.state, "store", None)
        processor = getattr(app.state, "processor", None)
        if outline_client is None or store is None or processor is None:
            outline_client, store = build_request_resources(settings)
            processor = build_comment_processor(
                settings=settings,
                outline_client=outline_client,
                store=store,
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
        if isinstance(exc, OutlineClientError) and is_outline_auth_error(exc):
            invalidate_runtime_identity(settings=settings, reason=str(exc))
        logger.exception("Webhook processing failed")
        if outline_client is not None and store is not None:
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
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

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
    settings: AppSettings,
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

    self_authored_user_ids = {user_id for user_id in (settings.runtime_outline_user_id,) if user_id}
    if envelope.actorId in self_authored_user_ids or comment.createdById in self_authored_user_ids:
        return

    if settings.trigger_mode == "all":
        triggered = True
    else:
        comment_text = extract_plain_text(comment.data)
        mentions = extract_mentions(comment.data)
        triggered = False
        if settings.runtime_outline_user_id and any(m.model_id == settings.runtime_outline_user_id for m in mentions):
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
        if is_outline_auth_error(post_exc):
            invalidate_runtime_identity(settings=settings, reason=str(post_exc))
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
