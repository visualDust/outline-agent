from __future__ import annotations

import mimetypes
from typing import Any

from ..clients.model_client import ModelClient, ModelInputImage
from ..clients.outline_comments import prepare_comment_chunks as _prepare_comment_chunks
from ..clients.outline_client import (
    OutlineClient,
    OutlineClientError,
    OutlineCollection,
    OutlineComment,
    OutlineDocument,
)
from ..managers.action_router_manager import ActionRouterManager, ActionRoutingDecision
from ..core.config import AppSettings
from ..core.logging import logger
from ..managers.document_update_manager import DocumentUpdateManager
from ..managers.document_creation_manager import DocumentCreationManager
from ..managers.memory_action_manager import MemoryActionManager
from ..managers.memory_manager import CollectionMemoryManager
from ..managers.related_document_manager import RelatedDocumentManager
from ..managers.same_document_comment_manager import SameDocumentCommentManager
from ..managers.thread_session_manager import ThreadSessionManager
from ..managers.tool_use_manager import ToolUseManager
from ..models.webhook_models import CommentModel, WebhookEnvelope
from ..runtime.tool_runtime import (
    ToolExecutionStep,
    ToolRuntime,
    UploadedAttachment,
    collect_uploaded_attachments,
)
from ..state.store import ProcessedEventStore
from ..state.workspace import CollectionWorkspace, CollectionWorkspaceManager, ThreadWorkspace
from ..utils.error_reporting import format_failure_comment, generate_error_id
from ..utils.rich_text import MentionRef, extract_image_refs, extract_mentions, extract_prompt_text
from .processor_detection import (
    select_cross_thread_candidates as _select_cross_thread_candidates,
)
from .processor_prompting import (
    build_system_prompt as _build_system_prompt,
)
from .processor_prompting import (
    build_user_prompt as _build_user_prompt,
)
from .processor_prompting import (
    comment_author_name as _comment_author_name,
)
from .processor_prompting import (
    comment_created_at as _comment_created_at,
)
from .processor_prompting import (
    created_comment_id as _created_comment_id,
)
from .processor_prompting import (
    format_comment_context as _format_comment_context,
)
from .processor_prompting import (
    load_prompt_packs as _load_prompt_packs,
)
from .processor_prompting import (
    preview as _preview,
)
from .processor_prompting import (
    select_context_comments as _select_context_comments,
)
from .processor_prompting import (
    strip_trigger_tokens as _strip_trigger_tokens,
)
from .processor_prompting import (
    thread_root_id as _thread_root_id,
)
from .processor_prompting import (
    truncate as _truncate,
)
from .processor_tooling import (
    append_status_context as _append_status_context,
)
from .processor_tooling import (
    append_status_preview as _append_status_preview,
)
from .processor_tooling import (
    append_uploaded_attachment_links as _append_uploaded_attachment_links,
)
from .processor_tooling import (
    describe_round_stop_for_progress as _describe_round_stop_for_progress,
)
from .processor_tooling import (
    describe_tool_plan_for_progress as _describe_tool_plan_for_progress,
)
from .processor_tooling import (
    describe_tool_result_for_progress as _describe_tool_result_for_progress,
)
from .processor_tooling import (
    describe_tool_step_for_progress as _describe_tool_step_for_progress,
)
from .processor_tooling import (
    find_redundant_upload_paths as _find_redundant_upload_paths,
)
from .processor_tooling import (
    find_repeated_plan_without_intervening_state_change as _find_repeated_plan_without_intervening_state_change,
)
from .processor_tooling import (
    format_progress_comment_text as _format_progress_comment_text,
)
from .processor_tooling import (
    format_registered_attachment_context as _format_registered_attachment_context,
)
from .processor_tooling import (
    format_tool_context as _format_tool_context,
)
from .processor_tooling import (
    format_tool_preview as _format_tool_preview,
)
from .processor_tooling import (
    preview_registered_attachments as _preview_registered_attachments,
)
from .processor_tooling import (
    preview_tool_step as _preview_tool_step,
)
from .processor_tooling import (
    progress_comment_headline as _progress_comment_headline,
)
from .processor_tooling import (
    register_uploaded_attachments_in_document_text as _register_uploaded_attachments_in_document_text,
)
from .processor_tooling import (
    resolve_dry_run_reason as _resolve_dry_run_reason,
)
from .processor_tooling import (
    resolve_success_action as _resolve_success_action,
)
from .processor_tooling import (
    resolve_success_reason as _resolve_success_reason,
)
from .processor_tooling import (
    tool_plan_fingerprint as _tool_plan_fingerprint,
)
from .processor_tooling import (
    tool_plan_is_read_only as _tool_plan_is_read_only,
)
from .processor_tooling import (
    tool_plan_may_change_state as _tool_plan_may_change_state,
)
from .processor_types import (
    ArtifactRegistrationResult,
    CrossThreadHandoff,
    ExecutedToolRound,
    PreparedActionOutcome,
    PreparedRequest,
    PreparedThreadContext,
    ProcessingResult,
    ResolvedThreadTrigger,
    ToolRoundSummary,
)


class CommentProcessor:
    def __init__(
        self,
        settings: AppSettings,
        store: ProcessedEventStore,
        outline_client: OutlineClient,
        model_client: ModelClient,
        memory_model_client: ModelClient | None = None,
        thread_session_model_client: ModelClient | None = None,
        document_update_model_client: ModelClient | None = None,
        tool_model_client: ModelClient | None = None,
        action_router_model_client: ModelClient | None = None,
    ):
        self.settings = settings
        self.store = store
        self.outline_client = outline_client
        self.model_client = model_client
        shared_memory_client = memory_model_client or model_client
        self.memory_action_manager = MemoryActionManager(settings, shared_memory_client)
        self.memory_manager = CollectionMemoryManager(settings, shared_memory_client)
        self.workspace_manager = CollectionWorkspaceManager(settings.workspace_root)
        self.document_update_manager = DocumentUpdateManager(
            settings,
            document_update_model_client or shared_memory_client,
        )
        self.document_creation_manager = DocumentCreationManager(
            settings,
            document_update_model_client or shared_memory_client,
        )
        self.related_document_manager = RelatedDocumentManager(settings, outline_client)
        self.same_document_comment_manager = SameDocumentCommentManager(
            settings,
            outline_client,
            self.workspace_manager,
        )
        self.action_router = ActionRouterManager(
            settings,
            action_router_model_client or model_client,
        )
        self.tool_use_manager = ToolUseManager(
            settings,
            tool_model_client or shared_memory_client,
        )
        self.tool_runtime = ToolRuntime(settings, outline_client=outline_client)
        self.thread_session_manager = ThreadSessionManager(
            settings,
            thread_session_model_client or shared_memory_client,
        )
        self.prompt_packs = _load_prompt_packs(settings.prompt_pack_dir, settings.system_prompt_packs)
        self._resolved_agent_user_id = settings.outline_agent_user_id
        self._runtime_api_user_id = settings.runtime_outline_user_id

    async def handle(self, envelope: WebhookEnvelope) -> ProcessingResult:
        prepared = await self._prepare_request(envelope)
        if isinstance(prepared, ProcessingResult):
            return prepared

        processing_started = False
        processing_reaction_applied = False
        try:
            resolved_trigger = await self._resolve_thread_trigger(prepared)
            if isinstance(resolved_trigger, ProcessingResult):
                return resolved_trigger
            processing_started = True
            prepared.triggered_alias = resolved_trigger.triggered_alias

            processing_reaction_applied, thread_context = await self._prepare_thread_context(
                prepared=prepared,
                resolved_trigger=resolved_trigger,
            )
            action_outcome = await self._prepare_action_outcome(
                prepared=prepared,
                thread_context=thread_context,
            )
            reply = await self._generate_reply_text(
                prepared=prepared,
                thread_context=thread_context,
                action_outcome=action_outcome,
            )
            result = await self._persist_reply_and_build_result(
                prepared=prepared,
                thread_context=thread_context,
                action_outcome=action_outcome,
                reply=reply,
            )

            if processing_reaction_applied:
                await self._mark_done(prepared.comment.id)
            return result
        except Exception as exc:  # noqa: BLE001
            if processing_reaction_applied:
                await self._clear_processing(prepared.comment.id)
            if not processing_started:
                raise

            error_id = generate_error_id()
            logger.exception(
                "Comment processing failed (error_id={}, comment_id={}, document_id={})",
                error_id,
                prepared.comment.id,
                prepared.comment.documentId,
            )
            await self._notify_failure(prepared=prepared, error_id=error_id, exc=exc)
            self.store.add(prepared.semantic_key)
            return ProcessingResult(
                action="error",
                reason="internal-error",
                comment_id=prepared.comment.id,
                document_id=prepared.comment.documentId,
                collection_id=prepared.document.collection_id,
                collection_workspace=str(prepared.workspace.root_dir),
                thread_workspace=str(prepared.thread_workspace.root_dir),
                triggered_alias=prepared.triggered_alias,
            )

    async def _notify_failure(self, *, prepared: PreparedRequest, error_id: str, exc: BaseException) -> None:
        if self.settings.dry_run:
            return
        text = format_failure_comment(error_id=error_id, exc=exc)
        status_comment_id = prepared.thread_workspace.progress_comment_id_for(prepared.comment.id)
        reply_parent_comment_id = prepared.comment.parentCommentId or prepared.comment.id
        try:
            if status_comment_id:
                try:
                    await self.outline_client.update_comment(status_comment_id, text)
                    self._record_progress_comment_state(
                        thread_workspace=prepared.thread_workspace,
                        request_comment_id=prepared.comment.id,
                        status_comment_id=status_comment_id,
                        status="failed",
                        summary="Internal error while processing the request.",
                        actions=[],
                    )
                    return
                except OutlineClientError as update_exc:
                    logger.warning(
                        "Failed to update failure status comment {} for request {}: {}",
                        status_comment_id,
                        prepared.comment.id,
                        update_exc,
                    )

            await self.outline_client.create_comment(
                document_id=prepared.comment.documentId,
                text=text,
                parent_comment_id=reply_parent_comment_id,
            )
        except OutlineClientError as post_exc:
            logger.warning(
                "Failed to post failure comment for request {} (error_id={}): {}",
                prepared.comment.id,
                error_id,
                post_exc,
            )

    async def _prepare_request(self, envelope: WebhookEnvelope) -> PreparedRequest | ProcessingResult:
        if envelope.event != "comments.create":
            return ProcessingResult(action="ignored", reason="unsupported-event")

        comment = envelope.payload.model
        semantic_key = f"comments.create:{comment.id}"
        if self.store.contains(semantic_key):
            return ProcessingResult(
                action="ignored",
                reason="duplicate-comment-event",
                comment_id=comment.id,
                document_id=comment.documentId,
            )

        agent_user_id = await self._get_agent_user_id()
        runtime_api_user_id = await self._get_runtime_api_user_id()
        self_authored_user_ids = {user_id for user_id in (agent_user_id, runtime_api_user_id) if user_id}
        if envelope.actorId in self_authored_user_ids or comment.createdById in self_authored_user_ids:
            return ProcessingResult(
                action="ignored",
                reason="self-authored-event",
                comment_id=comment.id,
                document_id=comment.documentId,
            )

        comment_text = extract_prompt_text(comment.data)
        comment_author_name = _comment_author_name(comment)
        comment_created_at = _comment_created_at(comment, envelope)
        mentions = extract_mentions(comment.data)
        comment_image_sources = [item.src for item in extract_image_refs(comment.data)]
        triggered_alias = self._detect_direct_trigger(
            comment_text=comment_text,
            mentions=mentions,
            agent_user_id=agent_user_id,
        )
        reply_trigger_pending = (
            self.settings.trigger_mode == "mention"
            and not triggered_alias
            and self.settings.trigger_on_reply_to_agent
            and bool(agent_user_id)
            and bool(comment.parentCommentId)
        )

        document = await self.outline_client.document_info(comment.documentId)
        if self.settings.collection_allowlist and document.collection_id not in self.settings.collection_allowlist:
            return ProcessingResult(
                action="ignored",
                reason="collection-not-allowed",
                comment_id=comment.id,
                document_id=comment.documentId,
                collection_id=document.collection_id,
            )

        collection = await self._resolve_collection(document)
        workspace = self.workspace_manager.ensure(
            collection_id=collection.id if collection else document.collection_id or "unknown",
            collection_name=collection.name if collection else document.collection_id or "unknown",
        )
        thread_root_id = _thread_root_id(comment)
        thread_workspace = self.workspace_manager.ensure_thread(
            workspace,
            thread_id=thread_root_id,
            document_id=document.id,
            document_title=document.title,
        )
        thread_workspace.record_observed_comment(
            comment_id=comment.id,
            author_id=comment.createdById,
            author_name=comment_author_name,
            comment_text=comment_text,
            created_at=comment_created_at,
            parent_comment_id=comment.parentCommentId,
            document_id=document.id,
            document_title=document.title,
            max_recent_comments=self.settings.thread_recent_comments,
            max_comment_chars=self.settings.thread_comment_max_chars,
        )

        if self.settings.trigger_mode == "mention" and not triggered_alias and not reply_trigger_pending:
            self.store.add(semantic_key)
            return ProcessingResult(
                action="ignored",
                reason="no-trigger-mention",
                comment_id=comment.id,
                document_id=comment.documentId,
                collection_id=document.collection_id,
                collection_workspace=str(workspace.root_dir),
                thread_workspace=str(thread_workspace.root_dir),
            )

        comment_image_inputs = await self._prepare_comment_image_inputs(
            thread_workspace=thread_workspace,
            comment_id=comment.id,
            image_sources=comment_image_sources,
        )

        return PreparedRequest(
            semantic_key=semantic_key,
            comment=comment,
            document=document,
            collection=collection,
            workspace=workspace,
            thread_workspace=thread_workspace,
            comment_text=comment_text,
            comment_image_sources=comment_image_sources,
            comment_image_inputs=comment_image_inputs,
            mentions=mentions,
            agent_user_id=agent_user_id,
            triggered_alias=triggered_alias,
            reply_trigger_pending=reply_trigger_pending,
        )

    async def _resolve_thread_trigger(self, prepared: PreparedRequest) -> ResolvedThreadTrigger | ProcessingResult:
        comments = await self.outline_client.comments_list(
            prepared.comment.documentId,
            limit=self.settings.comment_list_limit,
        )
        triggered_alias = prepared.triggered_alias
        if not triggered_alias:
            triggered_alias = self._detect_reply_trigger(
                current_comment=prepared.comment,
                comments=comments,
                agent_user_id=prepared.agent_user_id,
            )
            if self.settings.trigger_mode == "mention" and not triggered_alias:
                return ProcessingResult(
                    action="ignored",
                    reason="no-trigger-mention",
                    comment_id=prepared.comment.id,
                    document_id=prepared.comment.documentId,
                    collection_id=prepared.document.collection_id,
                    collection_workspace=str(prepared.workspace.root_dir),
                    thread_workspace=str(prepared.thread_workspace.root_dir),
                )

        return ResolvedThreadTrigger(triggered_alias=triggered_alias, comments=comments)

    async def _prepare_thread_context(
        self,
        *,
        prepared: PreparedRequest,
        resolved_trigger: ResolvedThreadTrigger,
    ) -> tuple[bool, PreparedThreadContext]:
        processing_reaction_applied = await self._mark_processing(prepared.comment.id)
        await self._ensure_reply_placeholder_comment(
            thread_workspace=prepared.thread_workspace,
            request_comment_id=prepared.comment.id,
            document_id=prepared.comment.documentId,
        )

        context_comments = _select_context_comments(
            resolved_trigger.comments,
            prepared.comment,
            limit=self.settings.max_context_comments,
        )
        self._archive_context_comments(
            thread_workspace=prepared.thread_workspace,
            context_comments=context_comments,
            document=prepared.document,
        )
        comment_context = _format_comment_context(context_comments, current_comment_id=prepared.comment.id)

        cleaned_text = _strip_trigger_tokens(
            text=prepared.comment_text,
            aliases=self.settings.mention_aliases if self.settings.mention_alias_fallback_enabled else [],
            mentions=prepared.mentions,
        )
        prompt_text = cleaned_text.strip() or (
            "The user only pinged the agent. Ask a short clarifying follow-up question."
        )
        try:
            action_route = await self.action_router.decide(
                thread_workspace=prepared.thread_workspace,
                collection=prepared.collection,
                document=prepared.document,
                user_comment=prompt_text,
                comment_context=comment_context,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Action routing failed: {}", exc)
            action_route = None
        logger.debug(
            "Action route for comment {} in document {}: {}",
            prepared.comment.id,
            prepared.comment.documentId,
            self._format_action_route_preview(action_route) or "(no route)",
        )

        handoff = (
            self._resolve_cross_thread_handoff(
                workspace=prepared.workspace,
                thread_workspace=prepared.thread_workspace,
                document=prepared.document,
                prompt_text=prompt_text,
                context_comments=context_comments,
            )
            if action_route and action_route.cross_thread_handoff
            else None
        )
        same_document_comment_lookup = await self.same_document_comment_manager.fetch_context(
            workspace=prepared.workspace,
            document=prepared.document,
            current_comment=prepared.comment,
            prompt_text=prompt_text,
        ) if handoff is None and action_route and action_route.same_document_comment_lookup else None
        related_documents = await self.related_document_manager.fetch_context(
            document=prepared.document,
            prompt_text=prompt_text,
        )
        return processing_reaction_applied, PreparedThreadContext(
            triggered_alias=prepared.triggered_alias,
            context_comments=context_comments,
            comment_context=comment_context,
            prompt_text=prompt_text,
            action_route=action_route,
            handoff=handoff,
            same_document_comment_context=(
                same_document_comment_lookup.prompt_section if same_document_comment_lookup else None
            ),
            same_document_comment_preview=(
                same_document_comment_lookup.preview if same_document_comment_lookup else None
            ),
            related_documents_context=related_documents.prompt_section,
        )

    async def _prepare_action_outcome(
        self,
        *,
        prepared: PreparedRequest,
        thread_context: PreparedThreadContext,
    ) -> PreparedActionOutcome:
        if thread_context.handoff is not None:
            return PreparedActionOutcome(
                memory_action_status=None,
                memory_action_preview=None,
                memory_action_context=None,
                document_creation_status=None,
                document_creation_preview=None,
                document_creation_context=None,
                created_document=None,
                document_update_status=None,
                document_update_preview=None,
                document_update_context=None,
                tool_execution_status=None,
                tool_execution_preview=None,
                tool_execution_context=None,
                effective_document=prepared.document,
                uploaded_attachments=[],
            )

        (
            memory_action_status,
            memory_action_preview,
            memory_action_context,
        ) = await self._maybe_apply_memory_actions(
            workspace=prepared.workspace,
            collection=prepared.collection,
            document=prepared.document,
            user_comment=thread_context.prompt_text,
            should_attempt=bool(thread_context.action_route and thread_context.action_route.memory_action),
        )

        (
            document_creation_status,
            document_creation_preview,
            document_creation_context,
            created_document,
        ) = await self._maybe_create_document(
            thread_workspace=prepared.thread_workspace,
            collection=prepared.collection,
            document=prepared.document,
            user_comment=thread_context.prompt_text,
            comment_context=thread_context.comment_context,
            related_documents_context=thread_context.related_documents_context,
            current_comment_image_count=len(prepared.comment_image_inputs),
            input_images=prepared.comment_image_inputs,
            should_attempt=bool(thread_context.action_route and thread_context.action_route.document_creation),
        )

        (
            document_update_status,
            document_update_preview,
            document_update_context,
            effective_document,
        ) = await self._maybe_update_document(
            thread_workspace=prepared.thread_workspace,
            collection=prepared.collection,
            document=prepared.document,
            user_comment=thread_context.prompt_text,
            comment_context=thread_context.comment_context,
            related_documents_context=thread_context.related_documents_context,
            current_comment_image_count=len(prepared.comment_image_inputs),
            input_images=prepared.comment_image_inputs,
            should_attempt=bool(thread_context.action_route and thread_context.action_route.document_update),
        )
        (
            tool_execution_status,
            tool_execution_preview,
            tool_execution_context,
            uploaded_attachments,
        ) = await self._maybe_execute_tools(
            comment_id=prepared.comment.id,
            thread_workspace=prepared.thread_workspace,
            collection=prepared.collection,
            document=effective_document,
            user_comment=thread_context.prompt_text,
            comment_context=thread_context.comment_context,
            current_comment_image_count=len(prepared.comment_image_inputs),
            input_images=prepared.comment_image_inputs,
            should_attempt=bool(thread_context.action_route and thread_context.action_route.tool_use),
        )
        artifact_registration = await self._maybe_register_uploaded_attachments_in_document(
            document=effective_document,
            uploaded_attachments=uploaded_attachments,
        )
        effective_document = artifact_registration.effective_document
        tool_execution_preview = _append_status_preview(
            tool_execution_preview,
            artifact_registration.preview,
        )
        tool_execution_context = _append_status_context(
            tool_execution_context,
            artifact_registration.context,
        )
        return PreparedActionOutcome(
            memory_action_status=memory_action_status,
            memory_action_preview=memory_action_preview,
            memory_action_context=memory_action_context,
            document_creation_status=document_creation_status,
            document_creation_preview=document_creation_preview,
            document_creation_context=document_creation_context,
            created_document=created_document,
            document_update_status=document_update_status,
            document_update_preview=document_update_preview,
            document_update_context=document_update_context,
            tool_execution_status=tool_execution_status,
            tool_execution_preview=tool_execution_preview,
            tool_execution_context=tool_execution_context,
            effective_document=effective_document,
            uploaded_attachments=uploaded_attachments,
        )

    async def _generate_reply_text(
        self,
        *,
        prepared: PreparedRequest,
        thread_context: PreparedThreadContext,
        action_outcome: PreparedActionOutcome,
    ) -> str:
        system_prompt = _build_system_prompt(
            system_prompt=self.settings.system_prompt,
            workspace=prepared.workspace,
            thread_workspace=prepared.thread_workspace,
            prompt_packs=self.prompt_packs,
            max_memory_chars=self.settings.max_memory_chars,
        )
        user_prompt = _build_user_prompt(
            comment=prepared.comment,
            document=action_outcome.effective_document,
            collection=prepared.collection,
            workspace=prepared.workspace,
            thread_workspace=prepared.thread_workspace,
            prompt_text=thread_context.prompt_text,
            context_comments=thread_context.context_comments,
            document_creation_context=action_outcome.document_creation_context,
            document_update_context=action_outcome.document_update_context,
            tool_execution_context=action_outcome.tool_execution_context,
            memory_action_context=action_outcome.memory_action_context,
            same_document_comment_context=thread_context.same_document_comment_context,
            related_documents_context=thread_context.related_documents_context,
            handoff=thread_context.handoff,
            current_comment_image_count=len(prepared.comment_image_inputs),
            max_document_chars=self.settings.max_document_chars,
            max_thread_session_chars=self.settings.max_thread_session_chars,
            max_prompt_chars=self.settings.max_prompt_chars,
        )
        reply = await self._generate_reply_with_optional_images(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            comment_id=prepared.comment.id,
            image_inputs=prepared.comment_image_inputs,
        )
        return _append_uploaded_attachment_links(reply, action_outcome.uploaded_attachments)

    async def _persist_reply_and_build_result(
        self,
        *,
        prepared: PreparedRequest,
        thread_context: PreparedThreadContext,
        action_outcome: PreparedActionOutcome,
        reply: str,
    ) -> ProcessingResult:
        if self.settings.dry_run:
            skip_memory_update = action_outcome.memory_action_status is not None
            prepared.thread_workspace.record_turn(
                comment_id=prepared.comment.id,
                user_comment=thread_context.prompt_text,
                assistant_reply=reply,
                document_id=action_outcome.effective_document.id,
                document_title=action_outcome.effective_document.title,
                max_recent_turns=self.settings.thread_recent_turns,
                max_turn_chars=self.settings.thread_turn_max_chars,
            )
            thread_session_update_preview = await self._maybe_update_thread_session(
                thread_workspace=prepared.thread_workspace,
                collection=prepared.collection,
                document=action_outcome.effective_document,
                user_comment=thread_context.prompt_text,
                assistant_reply=reply,
            )
            memory_update_preview = await self._maybe_update_memory(
                workspace=prepared.workspace,
                collection=prepared.collection,
                document=action_outcome.effective_document,
                user_comment=thread_context.prompt_text,
                assistant_reply=reply,
            ) if not skip_memory_update else None
            self.store.add(prepared.semantic_key)
            return ProcessingResult(
                action="dry-run",
                reason=_resolve_dry_run_reason(
                    document_creation_status=action_outcome.document_creation_status,
                    document_update_status=action_outcome.document_update_status,
                    tool_execution_status=action_outcome.tool_execution_status,
                ),
                comment_id=prepared.comment.id,
                document_id=prepared.comment.documentId,
                collection_id=prepared.document.collection_id,
                collection_workspace=str(prepared.workspace.root_dir),
                thread_workspace=str(prepared.thread_workspace.root_dir),
                triggered_alias=prepared.triggered_alias,
                reply_preview=_preview(reply),
                action_route_preview=self._format_action_route_preview(thread_context.action_route),
                document_creation_preview=action_outcome.document_creation_preview,
                document_update_preview=action_outcome.document_update_preview,
                tool_execution_preview=action_outcome.tool_execution_preview,
                memory_action_preview=action_outcome.memory_action_preview,
                same_document_comment_preview=thread_context.same_document_comment_preview,
                memory_update_preview=memory_update_preview,
                thread_session_update_preview=thread_session_update_preview,
                handoff_preview=thread_context.handoff.preview if thread_context.handoff else None,
            )

        reply_parent_comment_id = prepared.comment.parentCommentId or prepared.comment.id
        placeholder_comment_id = (
            prepared.thread_workspace.progress_comment_id_for(prepared.comment.id)
            if self.settings.progress_comment_enabled and action_outcome.tool_execution_status is None
            else None
        )
        try:
            reply_result = await self._post_reply_comment(
                document_id=prepared.comment.documentId,
                parent_comment_id=reply_parent_comment_id,
                placeholder_comment_id=placeholder_comment_id,
                reply=reply,
            )
        except OutlineClientError:
            logger.warning(
                (
                    "Failed to post reply comment for request {} in thread {} "
                    "(document {}, reply_len={}, preview={})"
                ),
                prepared.comment.id,
                prepared.thread_workspace.thread_id,
                prepared.comment.documentId,
                len(reply),
                _preview(reply),
            )
            raise
        if placeholder_comment_id:
            self._record_progress_comment_state(
                thread_workspace=prepared.thread_workspace,
                request_comment_id=prepared.comment.id,
                status_comment_id=placeholder_comment_id,
                status="replied",
                summary=_preview(reply),
                actions=[],
            )
        prepared.thread_workspace.record_turn(
            comment_id=prepared.comment.id,
            user_comment=thread_context.prompt_text,
            assistant_reply=reply,
            assistant_comment_id=_created_comment_id(reply_result),
            document_id=action_outcome.effective_document.id,
            document_title=action_outcome.effective_document.title,
            max_recent_turns=self.settings.thread_recent_turns,
            max_turn_chars=self.settings.thread_turn_max_chars,
        )
        thread_session_update_preview = await self._maybe_update_thread_session(
            thread_workspace=prepared.thread_workspace,
            collection=prepared.collection,
            document=action_outcome.effective_document,
            user_comment=thread_context.prompt_text,
            assistant_reply=reply,
        )
        skip_memory_update = action_outcome.memory_action_status is not None
        memory_update_preview = await self._maybe_update_memory(
            workspace=prepared.workspace,
            collection=prepared.collection,
            document=action_outcome.effective_document,
            user_comment=thread_context.prompt_text,
            assistant_reply=reply,
        ) if not skip_memory_update else None
        self.store.add(prepared.semantic_key)
        return ProcessingResult(
            action=_resolve_success_action(
                document_creation_status=action_outcome.document_creation_status,
                document_update_status=action_outcome.document_update_status,
                tool_execution_status=action_outcome.tool_execution_status,
            ),
            reason=_resolve_success_reason(
                document_creation_status=action_outcome.document_creation_status,
                document_update_status=action_outcome.document_update_status,
                tool_execution_status=action_outcome.tool_execution_status,
            ),
            comment_id=prepared.comment.id,
            document_id=prepared.comment.documentId,
            collection_id=prepared.document.collection_id,
            collection_workspace=str(prepared.workspace.root_dir),
            thread_workspace=str(prepared.thread_workspace.root_dir),
            triggered_alias=prepared.triggered_alias,
            reply_preview=_preview(reply),
            action_route_preview=self._format_action_route_preview(thread_context.action_route),
            document_creation_preview=action_outcome.document_creation_preview,
            document_update_preview=action_outcome.document_update_preview,
            tool_execution_preview=action_outcome.tool_execution_preview,
            memory_action_preview=action_outcome.memory_action_preview,
            same_document_comment_preview=thread_context.same_document_comment_preview,
            memory_update_preview=memory_update_preview,
            thread_session_update_preview=thread_session_update_preview,
            handoff_preview=thread_context.handoff.preview if thread_context.handoff else None,
        )

    def _format_action_route_preview(self, action_route: ActionRoutingDecision | None) -> str | None:
        if action_route is None:
            return None

        enabled: list[str] = []
        if action_route.document_creation:
            enabled.append("document_creation")
        if action_route.document_update:
            enabled.append("document_update")
        if action_route.tool_use:
            enabled.append("tool_use")
        if action_route.memory_action:
            enabled.append("memory_action")
        if action_route.cross_thread_handoff:
            enabled.append("cross_thread_handoff")
        if action_route.same_document_comment_lookup:
            enabled.append("same_document_comment_lookup")

        status = ", ".join(enabled) if enabled else "none"
        reason = (action_route.reason or "").strip()
        return f"enabled={status}" + (f" ; reason={reason}" if reason else "")

    async def _generate_reply_with_optional_images(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        comment_id: str,
        image_inputs: list[ModelInputImage],
    ) -> str:
        if not image_inputs:
            return await self.model_client.generate_reply(system_prompt, user_prompt)

        multimodal_generate = getattr(self.model_client, "generate_reply_with_images", None)
        if not callable(multimodal_generate):
            return await self.model_client.generate_reply(system_prompt, user_prompt)
        try:
            return await multimodal_generate(system_prompt, user_prompt, input_images=image_inputs)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Multimodal reply generation failed for comment {}. Falling back to text-only reply: {}",
                comment_id,
                exc,
            )
            return await self.model_client.generate_reply(system_prompt, user_prompt)

    async def _prepare_comment_image_inputs(
        self,
        *,
        thread_workspace: ThreadWorkspace,
        comment_id: str,
        image_sources: list[str],
    ) -> list[ModelInputImage]:
        image_inputs: list[ModelInputImage] = []
        image_dir = thread_workspace.work_dir / "comment_images"
        image_dir.mkdir(parents=True, exist_ok=True)

        for index, source in enumerate(image_sources[:4], start=1):
            target_path = image_dir / f"{comment_id}-{index}.img"
            try:
                result = await self.outline_client.download_attachment(source, target_path)
            except OutlineClientError as exc:
                logger.warning("Failed to download comment image {} for {}: {}", source, comment_id, exc)
                continue

            try:
                data = target_path.read_bytes()
            except OSError as exc:
                logger.warning("Failed to read downloaded comment image {} for {}: {}", target_path, comment_id, exc)
                continue

            media_type = self._resolve_comment_image_media_type(
                data=data,
                target_path=target_path,
                content_type=result.get("content_type"),
            )
            if not media_type.startswith("image/"):
                logger.warning(
                    "Skipping comment image {} for {} because downloaded MIME type is not an image: {}",
                    source,
                    comment_id,
                    media_type,
                )
                continue
            image_inputs.append(ModelInputImage(data=data, media_type=media_type))

        return image_inputs

    def _resolve_comment_image_media_type(self, *, data: bytes, target_path, content_type: Any) -> str:
        if isinstance(content_type, str):
            normalized = content_type.split(";", 1)[0].strip()
            if normalized.startswith("image/"):
                return normalized
        sniffed = self._sniff_image_media_type(data)
        if sniffed:
            return sniffed
        guessed, _ = mimetypes.guess_type(str(target_path))
        return guessed or "application/octet-stream"

    @staticmethod
    def _sniff_image_media_type(data: bytes) -> str | None:
        if len(data) >= 8 and data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if len(data) >= 3 and data.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if len(data) >= 6 and (data.startswith(b"GIF87a") or data.startswith(b"GIF89a")):
            return "image/gif"
        if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        if len(data) >= 2 and data.startswith(b"BM"):
            return "image/bmp"
        return None

    def _detect_direct_trigger(
        self,
        comment_text: str,
        mentions: list[MentionRef],
        agent_user_id: str | None,
    ) -> str | None:
        if self.settings.trigger_mode == "all":
            return "*"

        if agent_user_id:
            for mention in mentions:
                if mention.model_id == agent_user_id:
                    return mention.label or agent_user_id

        if not self.settings.mention_alias_fallback_enabled:
            return None

        lowered = comment_text.lower()
        for alias in self.settings.mention_aliases:
            if alias.lower() in lowered:
                return alias
        return None

    def _detect_reply_trigger(
        self,
        current_comment: CommentModel,
        comments: list[OutlineComment],
        agent_user_id: str | None,
    ) -> str | None:
        if not (self.settings.trigger_on_reply_to_agent and agent_user_id and current_comment.parentCommentId):
            return None

        thread_root_id = current_comment.parentCommentId
        related_comments = [
            item
            for item in comments
            if item.id != current_comment.id
            and (item.id == thread_root_id or item.parent_comment_id == thread_root_id)
        ]
        if any(item.created_by_id == agent_user_id for item in related_comments):
            return "reply-to-agent"
        return None

    async def _get_agent_user_id(self) -> str | None:
        if self._resolved_agent_user_id:
            await self._get_runtime_api_user_id()
            return self._resolved_agent_user_id

        runtime_api_user_id = await self._get_runtime_api_user_id()
        if not runtime_api_user_id:
            return None

        self._resolved_agent_user_id = runtime_api_user_id
        self.settings.outline_agent_user_id = runtime_api_user_id
        logger.info(
            "Resolved trigger target from runtime Outline identity: {} ({})",
            runtime_api_user_id,
            self.settings.runtime_outline_user_name or "unknown",
        )
        return self._resolved_agent_user_id

    async def _get_runtime_api_user_id(self) -> str | None:
        if self._runtime_api_user_id:
            return self._runtime_api_user_id

        try:
            current_user = await self.outline_client.current_user()
        except OutlineClientError as exc:
            logger.warning("Unable to resolve runtime Outline user identity: {}", exc)
            return None

        self._runtime_api_user_id = current_user.id
        self.settings.runtime_outline_user_id = current_user.id
        self.settings.runtime_outline_user_name = current_user.name
        if self.settings.outline_agent_user_id and self.settings.outline_agent_user_id != current_user.id:
            logger.warning(
                "Configured OUTLINE_AGENT_USER_ID {} does not match runtime API user {} ({})",
                self.settings.outline_agent_user_id,
                current_user.id,
                current_user.name or "unknown",
            )
        return self._runtime_api_user_id

    def _archive_context_comments(
        self,
        *,
        thread_workspace: ThreadWorkspace,
        context_comments: list[OutlineComment],
        document: OutlineDocument,
    ) -> None:
        for item in context_comments:
            body = extract_prompt_text(item.data)
            if not body.strip():
                continue
            thread_workspace.record_observed_comment(
                comment_id=item.id,
                author_id=item.created_by_id,
                author_name=item.created_by_name,
                comment_text=body,
                created_at=item.created_at,
                parent_comment_id=item.parent_comment_id,
                document_id=document.id,
                document_title=document.title,
                max_recent_comments=self.settings.thread_recent_comments,
                max_comment_chars=self.settings.thread_comment_max_chars,
            )

    def _resolve_cross_thread_handoff(
        self,
        *,
        workspace: CollectionWorkspace,
        thread_workspace: ThreadWorkspace,
        document: OutlineDocument,
        prompt_text: str,
        context_comments: list[OutlineComment],
    ) -> CrossThreadHandoff | None:
        if len(context_comments) > 1:
            return None

        candidates = self.workspace_manager.list_document_thread_entries(
            workspace,
            document_id=document.id,
            exclude_thread_id=thread_workspace.thread_id,
        )
        if not candidates:
            return None

        selected, alternatives = _select_cross_thread_candidates(
            prompt_text,
            candidates,
            limit=self.settings.cross_thread_handoff_candidate_limit,
        )
        if selected is not None:
            participants = selected.get("participants") or []
            preview = (
                selected.get("session_summary")
                or selected.get("recent_preview")
                or "(no prior summary available)"
            )
            prompt_section = (
                "The current comment appears to refer to a different discussion thread in this same document.\n"
                "Most likely referenced thread:\n"
                f"- thread_id: {selected.get('thread_id')}\n"
                f"- participants: {', '.join(participants) if participants else '(unknown)'}\n"
                f"- prior discussion summary: {preview}\n"
                "Instruction: first restate your understanding of that earlier discussion. "
                "If any detail is uncertain, ask for confirmation instead of assuming. "
                "Do not directly perform document edits or local tool actions in this turn."
            )
            return CrossThreadHandoff(
                mode="resolved",
                preview=f"selected {selected.get('thread_id')}: {_truncate(str(preview), 240)}",
                prompt_section=prompt_section,
            )

        lines = [
            (
                "The current comment appears to refer to a different discussion thread in "
                "this same document, but multiple candidates exist."
            ),
            "Possible referenced threads:",
        ]
        preview_parts: list[str] = []
        for index, item in enumerate(alternatives, start=1):
            participants = item.get("participants") or []
            preview = item.get("session_summary") or item.get("recent_preview") or "(no prior summary available)"
            lines.append(
                (
                    f"{index}. thread_id={item.get('thread_id')} | "
                    f"participants={', '.join(participants) if participants else '(unknown)'} | "
                    f"summary={preview}"
                )
            )
            preview_parts.append(f"{item.get('thread_id')}: {_truncate(str(preview), 120)}")
        lines.append(
            "Instruction: ask the user which prior discussion they want you to use, "
            "or ask them to @mention you inside that thread. Do not perform document "
            "edits or local tool actions in this turn."
        )
        return CrossThreadHandoff(
            mode="ambiguous",
            preview=" ; ".join(preview_parts),
            prompt_section="\n".join(lines),
        )
    async def _resolve_collection(self, document: OutlineDocument) -> OutlineCollection | None:
        if not document.collection_id:
            return None
        try:
            return await self.outline_client.collection_info(document.collection_id)
        except OutlineClientError as exc:
            logger.warning(
                "Falling back without collection metadata for collection {}: {}",
                document.collection_id,
                exc,
            )
            return None

    async def _maybe_apply_memory_actions(
        self,
        *,
        workspace: CollectionWorkspace,
        collection: OutlineCollection | None,
        document: OutlineDocument,
        user_comment: str,
        should_attempt: bool,
    ) -> tuple[str | None, str | None, str | None]:
        if not self.settings.memory_action_enabled:
            return None, None, None
        if not should_attempt:
            return None, None, None

        try:
            plan = await self.memory_action_manager.propose_actions(
                workspace=workspace,
                collection=collection,
                document=document,
                user_comment=user_comment,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Memory action proposal failed: {}", exc)
            return None, None, None

        if not plan.actions:
            return None, None, None

        preview = self.memory_action_manager.preview(plan)
        if self.settings.dry_run:
            context = self.memory_action_manager.format_reply_context(
                plan,
                status="planned-dry-run",
                applied=[],
                errors=[],
            )
            return "planned-dry-run", preview, context

        apply_result = self.memory_action_manager.apply_actions(workspace, plan)
        status = "applied" if apply_result.applied else "blocked"
        context = self.memory_action_manager.format_reply_context(
            plan,
            status=status,
            applied=apply_result.applied,
            errors=apply_result.errors,
        )
        return status, preview, context

    async def _maybe_update_memory(
        self,
        workspace: CollectionWorkspace,
        collection: OutlineCollection | None,
        document: OutlineDocument,
        user_comment: str,
        assistant_reply: str,
    ) -> str | None:
        if not self.settings.memory_update_enabled:
            return None
        try:
            proposal = await self.memory_manager.propose_update(
                workspace=workspace,
                collection=collection,
                document=document,
                user_comment=user_comment,
                assistant_reply=assistant_reply,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Memory update proposal failed: {}", exc)
            return f"memory-update-error: {exc}"

        if self.settings.dry_run:
            return self.memory_manager.preview(proposal)

        applied = self.memory_manager.apply_update(workspace, proposal)
        if applied:
            return " ; ".join(applied)
        return self.memory_manager.preview(proposal)

    async def _maybe_update_thread_session(
        self,
        *,
        thread_workspace: ThreadWorkspace,
        collection: OutlineCollection | None,
        document: OutlineDocument,
        user_comment: str,
        assistant_reply: str,
    ) -> str | None:
        if not self.settings.thread_session_update_enabled:
            return None
        try:
            proposal = await self.thread_session_manager.propose_update(
                thread_workspace=thread_workspace,
                collection=collection,
                document=document,
                user_comment=user_comment,
                assistant_reply=assistant_reply,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Thread session update proposal failed: {}", exc)
            return f"thread-session-update-error: {exc}"

        preview = self.thread_session_manager.preview(proposal)
        applied_preview = self.thread_session_manager.apply_update(thread_workspace, proposal)
        return applied_preview or preview

    async def _maybe_update_document(
        self,
        *,
        thread_workspace: ThreadWorkspace,
        collection: OutlineCollection | None,
        document: OutlineDocument,
        user_comment: str,
        comment_context: str,
        related_documents_context: str | None,
        current_comment_image_count: int,
        input_images: list[ModelInputImage],
        should_attempt: bool,
    ) -> tuple[str | None, str | None, str | None, OutlineDocument]:
        if not self.settings.document_update_enabled:
            return None, None, None, document
        if not should_attempt:
            return None, None, None, document

        try:
            proposal = await self.document_update_manager.propose_update(
                thread_workspace=thread_workspace,
                collection=collection,
                document=document,
                user_comment=user_comment,
                comment_context=comment_context,
                related_documents_context=related_documents_context,
                current_comment_image_count=current_comment_image_count,
                input_images=input_images,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Document update proposal failed: {}", exc)
            return None, None, None, document

        preview = self.document_update_manager.preview(proposal)
        effective_document = self.document_update_manager.build_updated_document(document, proposal)
        if proposal.decision == "blocked":
            return (
                "blocked",
                preview,
                self.document_update_manager.format_reply_context(proposal, "blocked"),
                document,
            )
        if proposal.decision != "edit":
            return None, None, None, document

        status = "planned-dry-run"
        if not self.settings.dry_run:
            await self.outline_client.update_document(
                document_id=document.id,
                title=proposal.title,
                text=proposal.text,
            )
            status = "applied"

        return (
            status,
            preview,
            self.document_update_manager.format_reply_context(proposal, status),
            effective_document,
        )

    async def _maybe_create_document(
        self,
        *,
        thread_workspace: ThreadWorkspace,
        collection: OutlineCollection | None,
        document: OutlineDocument,
        user_comment: str,
        comment_context: str,
        related_documents_context: str | None,
        current_comment_image_count: int,
        input_images: list[ModelInputImage],
        should_attempt: bool,
    ) -> tuple[str | None, str | None, str | None, OutlineDocument | None]:
        if not self.settings.document_update_enabled:
            return None, None, None, None
        if not should_attempt:
            return None, None, None, None

        try:
            proposal = await self.document_creation_manager.propose_create(
                thread_workspace=thread_workspace,
                collection=collection,
                document=document,
                user_comment=user_comment,
                comment_context=comment_context,
                related_documents_context=related_documents_context,
                current_comment_image_count=current_comment_image_count,
                input_images=input_images,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Document creation proposal failed: {}", exc)
            return None, None, None, None

        if proposal.decision == "blocked":
            preview = self.document_creation_manager.preview(proposal)
            return (
                "blocked",
                preview,
                self.document_creation_manager.format_reply_context(proposal, status="blocked"),
                None,
            )
        if proposal.decision != "create":
            return None, None, None, None

        collection_id = collection.id if collection else document.collection_id
        if not collection_id:
            preview = "blocked: no collection id available for document creation"
            return (
                "blocked",
                preview,
                "- status: blocked\n- reason: No collection id was available for creating the new document.",
                None,
            )

        created_document = OutlineDocument(
            id="planned-dry-run",
            title=proposal.title,
            collection_id=collection_id,
            url=None,
            text=proposal.text,
        )
        status = "planned-dry-run"
        if not self.settings.dry_run:
            created_document = await self.outline_client.create_document(
                title=proposal.title or "Untitled",
                text=proposal.text or "",
                collection_id=collection_id,
            )
            status = "applied"

        preview = self.document_creation_manager.preview(proposal, created_document=created_document)
        context = self.document_creation_manager.format_reply_context(
            proposal,
            status=status,
            created_document=created_document,
        )
        return status, preview, context, created_document

    async def _maybe_execute_tools(
        self,
        *,
        comment_id: str,
        thread_workspace: ThreadWorkspace,
        collection: OutlineCollection | None,
        document: OutlineDocument,
        user_comment: str,
        comment_context: str,
        current_comment_image_count: int,
        input_images: list[ModelInputImage],
        should_attempt: bool,
    ) -> tuple[str | None, str | None, str | None, list[UploadedAttachment]]:
        if not self.settings.tool_use_enabled:
            return None, None, None, []
        if not should_attempt:
            return None, None, None, []

        round_summaries: list[ToolRoundSummary] = []
        round_history: list[str] = []
        progress_actions: list[str] = []
        uploaded_attachments: list[UploadedAttachment] = []
        progress_comment_id = thread_workspace.progress_comment_id_for(comment_id)
        executed_rounds: list[ExecutedToolRound] = []

        def remember_progress_action(action: str) -> None:
            if not action.strip():
                return
            progress_actions.append(_truncate(action, self.settings.tool_run_summary_max_chars))
            del progress_actions[: -self.settings.progress_comment_recent_actions]

        async def sync_progress(status: str, headline: str) -> None:
            nonlocal progress_comment_id
            progress_comment_id = await self._sync_progress_comment(
                thread_workspace=thread_workspace,
                request_comment_id=comment_id,
                document_id=document.id,
                status_comment_id=progress_comment_id,
                status=status,
                headline=headline,
                actions=progress_actions,
            )

        for round_index in range(1, self.settings.tool_execution_max_rounds + 1):
            try:
                proposal = await self.tool_use_manager.propose_plan(
                    thread_workspace=thread_workspace,
                    collection=collection,
                    document=document,
                    user_comment=user_comment,
                    comment_context=comment_context,
                    current_round=round_index,
                    prior_round_summaries=round_history,
                    current_comment_image_count=current_comment_image_count,
                    input_images=input_images,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Tool execution planning failed: {}", exc)
                if not round_summaries:
                    return None, None, None, uploaded_attachments
                preview = f"blocked: tool planning failed: {exc}"
                context = "- status: blocked\n- reason: Could not prepare a safe local tool plan."
                if progress_comment_id or progress_actions:
                    remember_progress_action(
                        f"Stopped: I couldn't safely prepare the next local actions in round {round_index}."
                    )
                    await sync_progress("blocked", _progress_comment_headline("blocked"))
                round_summaries.append(
                    ToolRoundSummary(
                        round_index=round_index,
                        status="blocked",
                        preview=preview,
                        context=context,
                    )
                )
                thread_workspace.record_tool_run(
                    comment_id=comment_id,
                    status="blocked",
                    summary=preview,
                    step_summaries=[],
                    max_recent_runs=self.settings.tool_recent_runs,
                    max_summary_chars=self.settings.tool_run_summary_max_chars,
                )
                return (
                    "blocked",
                    _format_tool_preview(round_summaries),
                    _format_tool_context(round_summaries),
                    uploaded_attachments,
                )

            preview = self.tool_use_manager.preview(proposal)
            if not proposal.should_run:
                if not round_summaries:
                    return None, None, None, uploaded_attachments
                remember_progress_action("Finished: all requested local actions are complete.")
                await sync_progress("applied", _progress_comment_headline("applied"))
                return (
                    "applied",
                    _format_tool_preview(round_summaries),
                    _format_tool_context(round_summaries),
                    uploaded_attachments,
                )

            redundant_upload_paths = _find_redundant_upload_paths(
                proposal.steps,
                uploaded_attachments,
                thread_workspace.work_dir,
            )
            if redundant_upload_paths:
                preview = (
                    "blocked: repeated attachment upload plan detected for already-uploaded file(s): "
                    + ", ".join(redundant_upload_paths)
                )
                context = (
                    "- status: blocked\n"
                    "- reason: The next tool plan only repeated attachment upload steps that were already "
                    "completed earlier in this turn. Execution stopped to avoid a loop.\n"
                    f"- repeated uploads: {', '.join(redundant_upload_paths)}"
                )
                round_summaries.append(
                    ToolRoundSummary(
                        round_index=round_index,
                        status="blocked",
                        preview=preview,
                        context=context,
                    )
                )
                thread_workspace.record_tool_run(
                    comment_id=comment_id,
                    status="blocked",
                    summary=preview,
                    step_summaries=[],
                    max_recent_runs=self.settings.tool_recent_runs,
                    max_summary_chars=self.settings.tool_run_summary_max_chars,
                )
                remember_progress_action(
                    "Stopped: the next tool plan only repeated attachment uploads that were already complete."
                )
                await sync_progress("blocked", _progress_comment_headline("blocked"))
                return (
                    "blocked",
                    _format_tool_preview(round_summaries),
                    _format_tool_context(round_summaries),
                    uploaded_attachments,
                )

            plan_fingerprint = _tool_plan_fingerprint(proposal.steps)
            prior_repeat_round = _find_repeated_plan_without_intervening_state_change(
                plan_fingerprint,
                executed_rounds,
            )
            if prior_repeat_round is not None:
                preview = (
                    "blocked: repeated tool plan detected with no intervening state change; "
                    "execution stopped to avoid a loop "
                    f"({'; '.join(_preview_tool_step(step) for step in proposal.steps)})"
                )
                repeated_reason = (
                    "The next inspection-only tool plan repeated an earlier successful inspection round"
                    if prior_repeat_round.read_only
                    else "The next tool plan exactly repeated an earlier successful round"
                )
                context = (
                    "- status: blocked\n"
                    f"- reason: {repeated_reason} without any intervening state-changing round, "
                    "so execution stopped to avoid an infinite loop.\n"
                    f"- repeated_from_round: {prior_repeat_round.round_index}\n"
                    f"- repeated steps: {' ; '.join(_preview_tool_step(step) for step in proposal.steps)}"
                )
                round_summaries.append(
                    ToolRoundSummary(
                        round_index=round_index,
                        status="blocked",
                        preview=preview,
                        context=context,
                    )
                )
                thread_workspace.record_tool_run(
                    comment_id=comment_id,
                    status="blocked",
                    summary=preview,
                    step_summaries=[],
                    max_recent_runs=self.settings.tool_recent_runs,
                    max_summary_chars=self.settings.tool_run_summary_max_chars,
                )
                remember_progress_action(
                    "Stopped: the next tool plan repeated an earlier successful round without any new state change."
                )
                await sync_progress("blocked", _progress_comment_headline("blocked"))
                return (
                    "blocked",
                    _format_tool_preview(round_summaries),
                    _format_tool_context(round_summaries),
                    uploaded_attachments,
                )

            if self.settings.dry_run:
                round_summaries.append(
                    ToolRoundSummary(
                        round_index=round_index,
                        status="planned-dry-run",
                        preview=preview,
                        context=self.tool_use_manager.format_reply_context(
                            work_dir=str(thread_workspace.work_dir),
                            proposal=proposal,
                            status="planned-dry-run",
                            report=None,
                        ),
                    )
                )
                return (
                    "planned-dry-run",
                    _format_tool_preview(round_summaries),
                    _format_tool_context(round_summaries),
                    uploaded_attachments,
                )

            remember_progress_action(_describe_tool_plan_for_progress(round_index, proposal.steps))
            await sync_progress(
                "running",
                _progress_comment_headline(
                    "running",
                    round_index=round_index,
                    total_rounds=self.settings.tool_execution_max_rounds,
                ),
            )

            async def on_progress(
                stage: str,
                step: ToolExecutionStep,
                result: Any | None,
            ) -> None:
                if stage == "before_step":
                    remember_progress_action(f"Started: {_describe_tool_step_for_progress(step)}.")
                elif result is not None:
                    remember_progress_action(_describe_tool_result_for_progress(result))
                await sync_progress(
                    "running",
                    _progress_comment_headline(
                        "running",
                        round_index=round_index,
                        total_rounds=self.settings.tool_execution_max_rounds,
                    ),
                )

            report = await self.tool_runtime.execute_plan(
                thread_workspace.work_dir,
                proposal,
                document_id=document.id,
                on_progress=on_progress,
            )
            report_preview = report.preview or preview or report.status
            context = self.tool_use_manager.format_reply_context(
                work_dir=str(thread_workspace.work_dir),
                proposal=proposal,
                status=report.status,
                report=report,
            )
            uploaded_attachments.extend(collect_uploaded_attachments(report.step_results))
            round_summaries.append(
                ToolRoundSummary(
                    round_index=round_index,
                    status=report.status,
                    preview=report_preview,
                    context=context,
                )
            )
            if report.status != "applied":
                remember_progress_action(_describe_round_stop_for_progress(round_index, report.status))
            round_history.append(f"round {round_index} (status={report.status}): {report_preview}")
            executed_rounds.append(
                ExecutedToolRound(
                    round_index=round_index,
                    plan_fingerprint=plan_fingerprint,
                    status=report.status,
                    may_change_state=_tool_plan_may_change_state(proposal.steps),
                    read_only=_tool_plan_is_read_only(proposal.steps),
                )
            )
            thread_workspace.record_tool_run(
                comment_id=comment_id,
                status=report.status,
                summary=report_preview,
                step_summaries=[result.summary for result in report.step_results],
                max_recent_runs=self.settings.tool_recent_runs,
                max_summary_chars=self.settings.tool_run_summary_max_chars,
            )
            if report.status != "applied":
                await sync_progress(report.status, _progress_comment_headline(report.status))
                return (
                    report.status,
                    _format_tool_preview(round_summaries),
                    _format_tool_context(round_summaries),
                    uploaded_attachments,
                )

        if not round_summaries:
            return None, None, None, uploaded_attachments

        round_summaries.append(
            ToolRoundSummary(
                round_index=self.settings.tool_execution_max_rounds + 1,
                status="stopped-max-rounds",
                preview=(
                    "stopped after reaching the maximum local tool planning rounds "
                    f"({self.settings.tool_execution_max_rounds})"
                ),
                context=(
                    f"- status: stopped-max-rounds\n- reason: Reached the maximum local tool planning rounds "
                    f"({self.settings.tool_execution_max_rounds})."
                ),
            )
        )
        thread_workspace.record_tool_run(
            comment_id=comment_id,
            status="stopped-max-rounds",
            summary=(
                "stopped after reaching the maximum local tool planning rounds "
                f"({self.settings.tool_execution_max_rounds})"
            ),
            step_summaries=[],
            max_recent_runs=self.settings.tool_recent_runs,
            max_summary_chars=self.settings.tool_run_summary_max_chars,
        )
        remember_progress_action(
            "Paused: reached the configured limit for local action rounds before stopping naturally."
        )
        await sync_progress("stopped-max-rounds", _progress_comment_headline("stopped-max-rounds"))
        return (
            "stopped-max-rounds",
            _format_tool_preview(round_summaries),
            _format_tool_context(round_summaries),
            uploaded_attachments,
        )

    async def _maybe_register_uploaded_attachments_in_document(
        self,
        *,
        document: OutlineDocument,
        uploaded_attachments: list[UploadedAttachment],
    ) -> ArtifactRegistrationResult:
        if self.settings.dry_run or not uploaded_attachments:
            return ArtifactRegistrationResult(
                status="skipped",
                preview=None,
                context=None,
                effective_document=document,
            )

        updated_text, registered_items = _register_uploaded_attachments_in_document_text(
            document.text,
            uploaded_attachments,
        )
        if not updated_text or not registered_items:
            return ArtifactRegistrationResult(
                status="skipped",
                preview=None,
                context=None,
                effective_document=document,
            )

        try:
            await self.outline_client.update_document(
                document_id=document.id,
                text=updated_text,
            )
        except OutlineClientError as exc:
            logger.warning(
                "Failed to register uploaded attachment links in document {}: {}",
                document.id,
                exc,
            )
            return ArtifactRegistrationResult(
                status="failed",
                preview="document artifact registration failed",
                context=(
                    "- artifact link registration: failed\n"
                    "- reason: Uploaded files were not added into the document body."
                ),
                effective_document=document,
            )

        preview = _preview_registered_attachments(registered_items)
        context = _format_registered_attachment_context(registered_items)
        return ArtifactRegistrationResult(
            status="applied",
            preview=preview,
            context=context,
            effective_document=OutlineDocument(
                id=document.id,
                title=document.title,
                collection_id=document.collection_id,
                url=document.url,
                text=updated_text,
            ),
        )

    async def _sync_progress_comment(
        self,
        *,
        thread_workspace: ThreadWorkspace,
        request_comment_id: str,
        document_id: str,
        status_comment_id: str | None,
        status: str,
        headline: str,
        actions: list[str],
    ) -> str | None:
        if self.settings.dry_run or not self.settings.progress_comment_enabled:
            return status_comment_id

        recent_actions = [item for item in actions if item.strip()][-self.settings.progress_comment_recent_actions :]
        text = _format_progress_comment_text(
            headline=headline,
            status=status,
            recent_actions=recent_actions,
        )

        resolved_comment_id = status_comment_id or thread_workspace.progress_comment_id_for(request_comment_id)
        try:
            if resolved_comment_id:
                await self.outline_client.update_comment(resolved_comment_id, text)
            else:
                result = await self.outline_client.create_comment(
                    document_id=document_id,
                    text=text,
                    parent_comment_id=thread_workspace.thread_id,
                )
                resolved_comment_id = _created_comment_id(result)
                if not resolved_comment_id:
                    logger.warning(
                        "Progress comment for request {} was created without a returned id",
                        request_comment_id,
                    )

            thread_workspace.record_progress_comment(
                request_comment_id=request_comment_id,
                status_comment_id=resolved_comment_id,
                status=status,
                summary=headline,
                actions=recent_actions,
                max_recent_entries=self.settings.tool_recent_runs,
                max_action_chars=self.settings.tool_run_summary_max_chars,
            )
        except OutlineClientError as exc:
            logger.warning(
                "Failed to sync progress comment for request {} in thread {}: {}",
                request_comment_id,
                thread_workspace.thread_id,
                exc,
            )
        return resolved_comment_id

    async def _ensure_reply_placeholder_comment(
        self,
        *,
        thread_workspace: ThreadWorkspace,
        request_comment_id: str,
        document_id: str,
    ) -> str | None:
        existing_comment_id = thread_workspace.progress_comment_id_for(request_comment_id)
        if existing_comment_id:
            return existing_comment_id
        return await self._sync_progress_comment(
            thread_workspace=thread_workspace,
            request_comment_id=request_comment_id,
            document_id=document_id,
            status_comment_id=None,
            status="thinking",
            headline=_progress_comment_headline("thinking"),
            actions=[],
        )

    async def _post_reply_comment(
        self,
        *,
        document_id: str,
        parent_comment_id: str,
        placeholder_comment_id: str | None,
        reply: str,
    ) -> dict[str, Any]:
        if not placeholder_comment_id:
            return await self.outline_client.create_comment(
                document_id=document_id,
                text=reply,
                parent_comment_id=parent_comment_id,
            )

        reply_chunks = _prepare_comment_chunks(reply)
        if not reply_chunks:
            raise OutlineClientError("Failed to prepare reply comment chunks")

        try:
            await self.outline_client.update_comment(placeholder_comment_id, reply_chunks[0])
        except OutlineClientError as exc:
            logger.warning(
                "Failed to update placeholder comment {} with final reply; posting a new reply comment instead: {}",
                placeholder_comment_id,
                exc,
            )
            return await self.outline_client.create_comment(
                document_id=document_id,
                text=reply,
                parent_comment_id=parent_comment_id,
            )

        for chunk in reply_chunks[1:]:
            await self.outline_client.create_comment(
                document_id=document_id,
                text=chunk,
                parent_comment_id=parent_comment_id,
            )
        return {"id": placeholder_comment_id}

    def _record_progress_comment_state(
        self,
        *,
        thread_workspace: ThreadWorkspace,
        request_comment_id: str,
        status_comment_id: str | None,
        status: str,
        summary: str,
        actions: list[str],
    ) -> None:
        thread_workspace.record_progress_comment(
            request_comment_id=request_comment_id,
            status_comment_id=status_comment_id,
            status=status,
            summary=summary,
            actions=actions,
            max_recent_entries=self.settings.tool_recent_runs,
            max_action_chars=self.settings.tool_run_summary_max_chars,
        )

    async def _mark_processing(self, comment_id: str) -> bool:
        if not self.settings.reaction_enabled:
            return False
        try:
            await self.outline_client.add_comment_reaction(comment_id, self.settings.reaction_processing_emoji)
            return True
        except OutlineClientError as exc:
            logger.warning("Failed to add processing reaction to comment {}: {}", comment_id, exc)
            return False

    async def _mark_done(self, comment_id: str) -> None:
        if not self.settings.reaction_enabled:
            return
        await self._safe_remove_reaction(comment_id, self.settings.reaction_processing_emoji)
        await self._safe_add_reaction(comment_id, self.settings.reaction_done_emoji)

    async def _clear_processing(self, comment_id: str) -> None:
        if not self.settings.reaction_enabled:
            return
        await self._safe_remove_reaction(comment_id, self.settings.reaction_processing_emoji)

    async def _safe_add_reaction(self, comment_id: str, emoji: str) -> None:
        try:
            await self.outline_client.add_comment_reaction(comment_id, emoji)
        except OutlineClientError as exc:
            logger.warning("Failed to add reaction {} to comment {}: {}", emoji, comment_id, exc)

    async def _safe_remove_reaction(self, comment_id: str, emoji: str) -> None:
        try:
            await self.outline_client.remove_comment_reaction(comment_id, emoji)
        except OutlineClientError as exc:
            logger.warning("Failed to remove reaction {} from comment {}: {}", emoji, comment_id, exc)
