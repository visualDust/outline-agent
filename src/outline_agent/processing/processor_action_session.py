from __future__ import annotations

from dataclasses import dataclass, field

from ..clients.outline_client import OutlineClient, OutlineDocument
from ..core.config import AppSettings
from ..runtime.tool_runtime import UploadedAttachment
from ..state.workspace import CollectionWorkspace, DocumentWorkspace, ThreadWorkspace
from .action_plan_results import describe_action_result_for_progress as _describe_action_result_for_progress
from .action_plan_structure import describe_action_step_for_progress as _describe_action_step_for_progress
from .processor_progress import (
    describe_round_retry_for_progress as _describe_round_retry_for_progress,
)
from .processor_progress import (
    describe_round_stop_for_progress as _describe_round_stop_for_progress,
)
from .processor_progress import (
    describe_tool_start_for_progress as _describe_tool_start_for_progress,
)
from .processor_progress import (
    format_tool_context as _format_tool_context,
)
from .processor_progress import (
    format_tool_preview as _format_tool_preview,
)
from .processor_progress import (
    progress_comment_headline as _progress_comment_headline,
)
from .processor_prompting import truncate as _truncate
from .processor_side_effects import sync_progress_comment as _sync_progress_comment
from .processor_types import (
    ActionPlanOutcome,
    ExecutedToolRound,
    ToolRoundSummary,
)


@dataclass(slots=True)
class ActionLoopSession:
    settings: AppSettings
    outline_client: OutlineClient
    workspace: CollectionWorkspace
    document_workspace: DocumentWorkspace
    thread_workspace: ThreadWorkspace
    comment_id: str
    document_id: str

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
    effective_document: OutlineDocument | None = None
    draft_creation_data: dict[str, object] | None = None
    draft_update_data: dict[str, object] | None = None
    round_summaries: list[ToolRoundSummary] = field(default_factory=list)
    round_history: list[str] = field(default_factory=list)
    round_observations: list[str] = field(default_factory=list)
    progress_actions: list[str] = field(default_factory=list)
    uploaded_attachments: list[UploadedAttachment] = field(default_factory=list)
    executed_rounds: list[ExecutedToolRound] = field(default_factory=list)
    mermaid_validation_failures: int = 0
    progress_comment_id: str | None = field(init=False)

    def __post_init__(self) -> None:
        self.progress_comment_id = self.thread_workspace.progress_comment_id_for(self.comment_id)

    def outcome(self) -> ActionPlanOutcome:
        assert self.effective_document is not None
        return ActionPlanOutcome(
            effective_document=self.effective_document,
            document_creation_status=self.document_creation_status,
            document_creation_preview=self.document_creation_preview,
            document_creation_context=self.document_creation_context,
            created_document=self.created_document,
            document_update_status=self.document_update_status,
            document_update_preview=self.document_update_preview,
            document_update_context=self.document_update_context,
            tool_execution_status=self.tool_execution_status,
            tool_execution_preview=self.tool_execution_preview,
            tool_execution_context=self.tool_execution_context,
            uploaded_attachments=self.uploaded_attachments,
        )

    def remember_progress_action(self, action: str) -> None:
        if not action.strip():
            return
        self.progress_actions.append(_truncate(action, self.settings.tool_run_summary_max_chars))
        del self.progress_actions[: -self.settings.progress_comment_recent_actions]

    async def sync_progress(self, status: str, headline: str) -> None:
        self.progress_comment_id = await _sync_progress_comment(
            settings=self.settings,
            outline_client=self.outline_client,
            thread_workspace=self.thread_workspace,
            request_comment_id=self.comment_id,
            document_id=self.document_id,
            status_comment_id=self.progress_comment_id,
            status=status,
            headline=headline,
            actions=self.progress_actions,
        )

    def refresh_tool_execution_summary(self) -> None:
        self.tool_execution_preview = _format_tool_preview(self.round_summaries)
        self.tool_execution_context = _format_tool_context(self.round_summaries)

    def terminal_failure_status(self) -> str | None:
        for status in (
            self.tool_execution_status,
            self.document_update_status,
            self.document_creation_status,
        ):
            if status in {"failed", "blocked", "stopped-max-rounds"}:
                return status
        return None

    def record_tool_run(
        self,
        *,
        status: str,
        summary: str,
        step_summaries: list[str],
    ) -> None:
        self.thread_workspace.record_tool_run(
            comment_id=self.comment_id,
            status=status,
            summary=summary,
            step_summaries=step_summaries,
            max_recent_runs=self.settings.tool_recent_runs,
            max_summary_chars=self.settings.tool_run_summary_max_chars,
        )

    async def start_round(self, *, round_index: int, action_description: str) -> None:
        self.remember_progress_action(action_description)
        await self.sync_progress(
            "running",
            _progress_comment_headline(
                "running",
                round_index=round_index,
                total_rounds=self.settings.tool_execution_max_rounds,
            ),
        )

    async def note_step_progress(
        self,
        *,
        round_index: int,
        stage: str,
        tool_name: str,
        resolved_args: dict[str, object],
        requires_confirmation: bool,
        result: object,
    ) -> None:
        if stage == "before_step":
            description = _describe_action_step_for_progress(tool_name, resolved_args)
            self.remember_progress_action(
                _describe_tool_start_for_progress(description, requires_confirmation=requires_confirmation)
            )
        elif result is not None:
            self.remember_progress_action(_describe_action_result_for_progress(tool_name, resolved_args, result))
        await self.sync_progress(
            "running",
            _progress_comment_headline(
                "running",
                round_index=round_index,
                total_rounds=self.settings.tool_execution_max_rounds,
            ),
        )

    async def stop_with_blocked_round(
        self,
        *,
        round_index: int,
        preview: str,
        context: str,
        progress_action: str,
        record_tool_run: bool,
    ) -> ActionPlanOutcome:
        self.round_summaries.append(
            ToolRoundSummary(
                round_index=round_index,
                status="blocked",
                preview=preview,
                context=context,
            )
        )
        if record_tool_run:
            self.record_tool_run(status="blocked", summary=preview, step_summaries=[])
        self.remember_progress_action(progress_action)
        await self.sync_progress("blocked", _progress_comment_headline("blocked"))
        self.tool_execution_status = "blocked"
        self.refresh_tool_execution_summary()
        return self.outcome()

    async def finish_local_actions(self) -> ActionPlanOutcome:
        terminal_status = self.terminal_failure_status()
        if self.tool_execution_status is None and self.round_summaries:
            self.tool_execution_status = "applied"
            self.refresh_tool_execution_summary()
            terminal_status = self.terminal_failure_status()
        if terminal_status is not None:
            self.remember_progress_action(
                "Stopped: no safe follow-up actions remained after the earlier failed action attempts."
            )
            await self.sync_progress(
                terminal_status,
                _progress_comment_headline(terminal_status),
            )
            return self.outcome()
        if self.progress_comment_id or self.progress_actions:
            self.remember_progress_action("Finished: all requested local actions are complete.")
            await self.sync_progress("applied", _progress_comment_headline("applied"))
        return self.outcome()

    async def note_retry_after_failure(self, *, round_index: int, status: str) -> None:
        self.remember_progress_action(_describe_round_retry_for_progress(round_index, status))
        await self.sync_progress("running", _progress_comment_headline("running"))

    async def stop_failed_round(self, *, round_index: int, status: str) -> ActionPlanOutcome:
        self.remember_progress_action(_describe_round_stop_for_progress(round_index, status))
        await self.sync_progress(status, _progress_comment_headline(status))
        return self.outcome()

    async def stop_max_rounds(self) -> ActionPlanOutcome:
        preview = (
            f"stopped after reaching the maximum local tool planning rounds ({self.settings.tool_execution_max_rounds})"
        )
        self.round_summaries.append(
            ToolRoundSummary(
                round_index=self.settings.tool_execution_max_rounds + 1,
                status="stopped-max-rounds",
                preview=preview,
                context=(
                    "- status: stopped-max-rounds\n"
                    "- reason: Reached the maximum local tool planning rounds "
                    f"({self.settings.tool_execution_max_rounds})."
                ),
            )
        )
        self.record_tool_run(
            status="stopped-max-rounds",
            summary=preview,
            step_summaries=[],
        )
        self.remember_progress_action(
            "Paused: reached the configured limit for local action rounds before stopping naturally."
        )
        await self.sync_progress(
            "stopped-max-rounds",
            _progress_comment_headline("stopped-max-rounds"),
        )
        self.tool_execution_status = "stopped-max-rounds"
        self.refresh_tool_execution_summary()
        return self.outcome()
