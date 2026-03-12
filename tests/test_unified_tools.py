from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from outline_agent.clients.model_client import ModelClientError
from outline_agent.clients.outline_models import OutlineCollection, OutlineDocument
from outline_agent.core.config import AppSettings
from outline_agent.planning.execution_loop import (
    UnifiedExecutionLoop,
    _hydrate_document_action_args,
    _hydrate_drafting_context_args,
    _sanitize_document_action_args,
    _validate_document_action_args,
)
from outline_agent.planning.tool_plan_schema import (
    ToolPlanValidationError,
    UnifiedToolPlan,
    UnifiedToolPlanStep,
    sanitize_unified_tool_plan,
)
from outline_agent.planning.tool_planner import UNIFIED_TOOL_PLANNER_SYSTEM_PROMPT
from outline_agent.processing.action_plan_structure import select_next_action_plan_chunk
from outline_agent.tools import (
    CreateDocumentTool,
    GetCurrentDocumentTool,
    ToolContext,
    ToolRegistry,
    build_default_extract_text_tools,
    build_workspace_tools,
)


class DummyOutlineClient:
    def __init__(self) -> None:
        self.downloads: list[dict[str, str]] = []
        self.created_documents: list[dict[str, str | bool | None]] = []

    async def download_attachment(self, url_or_path: str, file_path: Path) -> dict:
        self.downloads.append({"url_or_path": url_or_path, "file_path": str(file_path)})
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("Line one\nLine two\n", encoding="utf-8")
        return {
            "ok": True,
            "url": url_or_path,
            "file_path": str(file_path),
            "size": file_path.stat().st_size,
            "content_type": "text/plain",
        }

    async def create_document(
        self,
        *,
        title: str,
        text: str,
        collection_id: str,
        parent_document_id: str | None = None,
        publish: bool = True,
    ) -> OutlineDocument:
        created_id = f"created-{len(self.created_documents) + 1}"
        self.created_documents.append(
            {
                "title": title,
                "text": text,
                "collection_id": collection_id,
                "parent_document_id": parent_document_id,
                "publish": publish,
            }
        )
        return OutlineDocument(
            id=created_id,
            title=title,
            collection_id=collection_id,
            url=f"/doc/{created_id}",
            text=text,
        )


def _build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(GetCurrentDocumentTool())
    registry.register_many(build_workspace_tools())
    registry.register(CreateDocumentTool())
    registry.register_many(build_default_extract_text_tools())
    return registry


def _build_context(tmp_path: Path, outline_client: DummyOutlineClient | None = None) -> ToolContext:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=False,
    )
    work_dir = tmp_path / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    return ToolContext(
        settings=settings,
        outline_client=outline_client,
        work_dir=work_dir,
        collection=OutlineCollection(id="collection-1", name="Test", description=None, url=None),
        document=OutlineDocument(
            id="doc-1",
            title="Current Doc",
            collection_id="collection-1",
            url="/doc/doc-1",
            text="# Current\n\nHello world",
        ),
    )


def test_get_current_document_tool_returns_document_text(tmp_path: Path) -> None:
    registry = _build_registry()
    context = _build_context(tmp_path)

    result = asyncio.run(registry.execute("get_current_document", {}, context))

    assert result.ok is True
    assert result.data["document_id"] == "doc-1"
    assert result.data["text"] == "# Current\n\nHello world"


def test_default_tool_registry_exposes_workspace_and_shell_tools(tmp_path: Path) -> None:
    registry = _build_registry()

    tool_names = {spec.name for spec in registry.list_specs()}

    assert "download_attachment" in tool_names
    assert "extract_text_from_pdf" in tool_names
    assert "run_shell" in tool_names


def test_extract_text_from_pdf_reads_literal_strings(tmp_path: Path) -> None:
    registry = _build_registry()
    context = _build_context(tmp_path)
    pdf_path = context.work_dir / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n1 0 obj\n<< /Length 20 >>\nstream\nBT\n(Hello PDF) Tj\nET\nendstream\nendobj\n")

    result = asyncio.run(registry.execute("extract_text_from_pdf", {"path": "sample.pdf"}, context))

    assert result.ok is True
    assert result.data["format"] == "pdf"
    assert "Hello PDF" in result.data["text"]


def test_sanitize_unified_tool_plan_rejects_unknown_tool() -> None:
    plan = UnifiedToolPlan(
        should_act=True,
        steps=[UnifiedToolPlanStep(tool="missing_tool", args={})],
    )

    with pytest.raises(ToolPlanValidationError):
        sanitize_unified_tool_plan(plan, allowed_tools={"get_current_document"}, max_steps=4)


def test_unified_execution_loop_can_compose_download_extract_and_create_document(tmp_path: Path) -> None:
    outline_client = DummyOutlineClient()
    registry = _build_registry()
    context = _build_context(tmp_path, outline_client=outline_client)
    loop = UnifiedExecutionLoop(registry, max_steps=4)
    plan = UnifiedToolPlan(
        should_act=True,
        goal="Read the attachment and create a new document from its text.",
        final_response_strategy="brief_confirmation",
        steps=[
            UnifiedToolPlanStep(
                tool="download_attachment",
                args={
                    "attachment_url": "/api/attachments.redirect?id=attachment-1",
                    "path": "attachments/input.txt",
                },
            ),
            UnifiedToolPlanStep(
                tool="extract_text_from_txt",
                args={"path": "{{steps.1.data.path}}"},
            ),
            UnifiedToolPlanStep(
                tool="create_document",
                args={
                    "title": "Imported Attachment",
                    "text": "{{steps.2.data.text}}",
                },
            ),
        ],
    )

    summary = asyncio.run(loop.execute(plan, context))

    assert summary.status == "applied"
    assert [step.tool for step in summary.steps] == [
        "download_attachment",
        "extract_text_from_txt",
        "create_document",
    ]
    assert outline_client.downloads == [
        {
            "url_or_path": "/api/attachments.redirect?id=attachment-1",
            "file_path": str(context.work_dir / "attachments" / "input.txt"),
        }
    ]
    assert outline_client.created_documents == [
        {
            "title": "Imported Attachment",
            "text": "Line one\nLine two",
            "collection_id": "collection-1",
            "parent_document_id": None,
            "publish": True,
        }
    ]
    assert summary.preview is not None
    assert "Created document 'Imported Attachment'." in summary.preview


def test_hydrate_document_update_args_prefers_draft_over_broken_template_reference() -> None:
    step_context = [
        {
            "tool": "draft_document_update",
            "ok": True,
            "data": {"title": "Updated Title", "text": "Updated body"},
        }
    ]

    hydrated = _hydrate_document_action_args(
        "apply_document_update",
        {
            "title": "{{steps.2.data.title}}",
            "text": "{{steps.2.data.draft}}",
            "content": "{{steps.2.data.content}}",
        },
        step_context,
        {},
    )

    assert hydrated == {
        "title": "Updated Title",
        "text": "Updated body",
        "content": "Updated body",
    }


def test_sanitize_document_update_args_drops_unsupported_draft_id_reference() -> None:
    sanitized = _sanitize_document_action_args(
        "apply_document_update",
        {
            "draft_id": "{{steps.2.data.draft_id}}",
            "title": "{{steps.2.data.title}}",
            "text": "{{steps.2.data.text}}",
            "unexpected": "ignored",
        },
    )

    assert sanitized == {
        "title": "{{steps.2.data.title}}",
        "text": "{{steps.2.data.text}}",
    }


def test_sanitize_create_document_args_drops_unsupported_draft_id_reference() -> None:
    sanitized = _sanitize_document_action_args(
        "create_document",
        {
            "draft_id": "{{steps.1.data.draft_id}}",
            "title": "{{steps.1.data.title}}",
            "text": "{{steps.1.data.text}}",
            "publish": True,
            "unexpected": "ignored",
        },
    )

    assert sanitized == {
        "title": "{{steps.1.data.title}}",
        "text": "{{steps.1.data.text}}",
        "publish": True,
    }


def test_hydrate_document_create_args_prefers_draft_over_broken_template_reference() -> None:
    step_context = [
        {
            "tool": "draft_new_document",
            "ok": True,
            "data": {"title": "Draft Title", "text": "Draft body"},
        }
    ]

    hydrated = _hydrate_document_action_args(
        "create_document",
        {
            "title": "{{steps.2.data.title}}",
            "text": "{{steps.2.data.text}}",
            "content": "{{steps.2.data.content}}",
        },
        step_context,
        {},
    )

    assert hydrated == {
        "title": "Draft Title",
        "text": "Draft body",
        "content": "Draft body",
    }


def test_validate_document_update_args_blocks_apply_after_blocked_draft() -> None:
    step_context = [
        {
            "tool": "draft_document_update",
            "ok": True,
            "data": {
                "decision": "blocked",
                "reason": "Reliable extracted paper text is still unavailable.",
            },
        }
    ]

    error = _validate_document_action_args("apply_document_update", {}, step_context, {})

    assert error == (
        "apply_document_update blocked by draft_document_update decision=blocked: "
        "Reliable extracted paper text is still unavailable."
    )


def test_hydrate_drafting_context_args_collects_local_workspace_observations() -> None:
    step_context = [
        {
            "tool": "download_attachment",
            "ok": True,
            "data": {"path": "attachments/paper.pdf", "size": 4096, "content_type": "application/pdf"},
        },
        {
            "tool": "extract_text_from_pdf",
            "ok": True,
            "data": {
                "path": "attachments/paper.pdf",
                "text": "Method overview\\nThe paper uses a two-stage routing design.",
            },
        },
    ]

    hydrated = _hydrate_drafting_context_args(
        "draft_document_update",
        {"user_comment": "请把这篇论文总结写进文档"},
        step_context,
        {"prior_round_observations": ["round 1 (status=applied): download + extract succeeded"]},
    )

    assert "local_workspace_context" in hydrated
    assert "Prior round observations:" in hydrated["local_workspace_context"]
    assert "extract_text_from_pdf[attachments/paper.pdf] extracted text:" in hydrated["local_workspace_context"]
    assert "two-stage routing design" in hydrated["local_workspace_context"]


def test_tool_planner_prompt_prefers_shell_first_pdf_workflow() -> None:
    assert "shell-first local workflow" in UNIFIED_TOOL_PLANNER_SYSTEM_PROMPT
    assert "Prefer `run_shell` over `extract_text_from_pdf`" in UNIFIED_TOOL_PLANNER_SYSTEM_PROMPT


def test_tool_registry_surfaces_model_client_error_without_unexpected_wrapper(tmp_path: Path) -> None:
    class ModelFailingTool:
        @property
        def spec(self):
            from outline_agent.tools.base import ToolSpec

            return ToolSpec(name="draft_document_update", description="x", side_effect_level="read")

        async def run(self, args, context):
            del args, context
            raise ModelClientError("Model request failed (openai-responses/ReadTimeout) during POST https://x.test")

    registry = ToolRegistry()
    registry.register(ModelFailingTool())
    context = _build_context(tmp_path)

    result = asyncio.run(registry.execute("draft_document_update", {}, context))

    assert result.ok is False
    assert result.summary == (
        "draft_document_update: Model request failed (openai-responses/ReadTimeout) during POST https://x.test"
    )
    assert "unexpected error" not in result.summary


def test_select_next_action_plan_chunk_trims_to_small_execution_batch() -> None:
    proposal = UnifiedToolPlan(
        should_act=True,
        goal="Render a PDF and upload it.",
        steps=[
            UnifiedToolPlanStep(tool="get_current_document", args={}),
            UnifiedToolPlanStep(tool="write_file", args={"path": "document.md", "content": "hello"}),
            UnifiedToolPlanStep(tool="run_shell", args={"command": "pandoc document.md -o output.pdf"}),
            UnifiedToolPlanStep(tool="upload_attachment", args={"path": "output.pdf"}),
        ],
        final_response_strategy="brief_confirmation",
    )

    chunk = select_next_action_plan_chunk(proposal, max_chunk_steps=2)

    assert [step.tool for step in chunk.steps] == ["get_current_document", "write_file"]
    assert chunk.goal == proposal.goal
    assert chunk.final_response_strategy == proposal.final_response_strategy


def test_tool_planner_prompt_uses_weak_planner_next_chunk_guidance() -> None:
    assert "Plan only the next smallest executable chunk" in UNIFIED_TOOL_PLANNER_SYSTEM_PROMPT
    assert "Trust the outer loop to replan after each executed chunk" in UNIFIED_TOOL_PLANNER_SYSTEM_PROMPT


def test_hydrate_document_update_args_uses_prior_round_draft_when_current_round_has_only_apply() -> None:
    hydrated = _hydrate_document_action_args(
        "apply_document_update",
        {},
        [],
        {
            "prior_draft_update_data": {
                "title": "Updated Title",
                "text": "Updated body",
            }
        },
    )

    assert hydrated == {"title": "Updated Title", "text": "Updated body", "content": "Updated body"}
