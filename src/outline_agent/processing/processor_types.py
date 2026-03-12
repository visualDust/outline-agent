from __future__ import annotations

from dataclasses import dataclass, field

from ..clients.model_client import ModelInputImage
from ..clients.outline_client import OutlineCollection, OutlineComment, OutlineDocument
from ..managers.action_router_manager import ActionRoutingDecision
from ..models.webhook_models import CommentModel
from ..runtime.tool_runtime import UploadedAttachment
from ..state.workspace import CollectionWorkspace, DocumentWorkspace, ThreadWorkspace
from ..utils.attachment_context import AttachmentContextItem
from ..utils.rich_text import MentionRef


@dataclass
class ProcessingResult:
    action: str
    reason: str
    comment_id: str | None = None
    document_id: str | None = None
    collection_id: str | None = None
    collection_workspace: str | None = None
    document_workspace: str | None = None
    thread_workspace: str | None = None
    triggered_alias: str | None = None
    reply_preview: str | None = None
    action_route_preview: str | None = None
    document_creation_preview: str | None = None
    document_update_preview: str | None = None
    tool_execution_preview: str | None = None
    memory_action_preview: str | None = None
    same_document_comment_preview: str | None = None
    memory_update_preview: str | None = None
    document_memory_update_preview: str | None = None
    handoff_preview: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "action": self.action,
            "reason": self.reason,
            "comment_id": self.comment_id,
            "document_id": self.document_id,
            "collection_id": self.collection_id,
            "collection_workspace": self.collection_workspace,
            "document_workspace": self.document_workspace,
            "thread_workspace": self.thread_workspace,
            "triggered_alias": self.triggered_alias,
            "reply_preview": self.reply_preview,
            "action_route_preview": self.action_route_preview,
            "document_creation_preview": self.document_creation_preview,
            "document_update_preview": self.document_update_preview,
            "tool_execution_preview": self.tool_execution_preview,
            "memory_action_preview": self.memory_action_preview,
            "same_document_comment_preview": self.same_document_comment_preview,
            "memory_update_preview": self.memory_update_preview,
            "document_memory_update_preview": self.document_memory_update_preview,
            "handoff_preview": self.handoff_preview,
        }


@dataclass
class ToolRoundSummary:
    round_index: int
    status: str
    preview: str | None
    context: str | None


@dataclass
class ExecutedToolRound:
    round_index: int
    plan_fingerprint: tuple[tuple[object, ...], ...]
    status: str
    may_change_state: bool
    read_only: bool


@dataclass
class CrossThreadHandoff:
    mode: str
    preview: str
    prompt_section: str


@dataclass
class ArtifactRegistrationResult:
    status: str
    preview: str | None
    context: str | None
    effective_document: OutlineDocument


@dataclass
class PreparedRequest:
    semantic_key: str
    event: str
    comment: CommentModel
    document: OutlineDocument
    collection: OutlineCollection | None
    workspace: CollectionWorkspace
    document_workspace: DocumentWorkspace
    thread_workspace: ThreadWorkspace
    document_comments: list[OutlineComment]
    thread_comments: list[OutlineComment]
    comment_text: str
    comment_image_sources: list[str]
    comment_image_inputs: list[ModelInputImage]
    mentions: list[MentionRef]
    agent_user_id: str | None
    triggered_alias: str | None
    reply_trigger_pending: bool


@dataclass
class PreparedThreadContext:
    triggered_alias: str | None
    context_comments: list[OutlineComment]
    comment_context: str
    prompt_text: str
    action_route: ActionRoutingDecision | None
    handoff: CrossThreadHandoff | None
    same_document_comment_context: str | None
    same_document_comment_preview: str | None
    related_documents_context: str | None
    available_attachment_context: list[AttachmentContextItem]


@dataclass(slots=True)
class ActionPlanRequest:
    comment_id: str
    workspace: CollectionWorkspace
    document_workspace: DocumentWorkspace
    thread_workspace: ThreadWorkspace
    collection: OutlineCollection | None
    document: OutlineDocument
    user_comment: str
    comment_context: str
    related_documents_context: str | None
    available_attachment_context: list[AttachmentContextItem]
    current_comment_image_count: int
    input_images: list[ModelInputImage]


@dataclass
class ResolvedThreadTrigger:
    triggered_alias: str | None
    comments: list[OutlineComment]


@dataclass(slots=True)
class ActionPlanOutcome:
    effective_document: OutlineDocument
    document_creation_status: str | None = None
    document_creation_preview: str | None = None
    document_creation_context: str | None = None
    created_document: OutlineDocument | None = None
    document_update_status: str | None = None
    document_update_preview: str | None = None
    document_update_context: str | None = None
    tool_execution_status: str | None = None
    tool_execution_preview: str | None = None
    tool_execution_context: str | None = None
    uploaded_attachments: list[UploadedAttachment] = field(default_factory=list)


@dataclass
class PreparedActionOutcome:
    memory_action_status: str | None
    memory_action_preview: str | None
    memory_action_context: str | None
    document_creation_status: str | None
    document_creation_preview: str | None
    document_creation_context: str | None
    created_document: OutlineDocument | None
    document_update_status: str | None
    document_update_preview: str | None
    document_update_context: str | None
    tool_execution_status: str | None
    tool_execution_preview: str | None
    tool_execution_context: str | None
    effective_document: OutlineDocument
    uploaded_attachments: list[UploadedAttachment]


@dataclass
class ReplyPersistenceOutcome:
    memory_update_preview: str | None
    document_memory_update_preview: str | None
