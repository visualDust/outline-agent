from __future__ import annotations

import json
from pathlib import Path

from outline_agent.clients.outline_models import OutlineCollection, OutlineDocument
from outline_agent.core.config import AppSettings
from outline_agent.core.prompt_registry import PromptPack
from outline_agent.models.webhook_models import WebhookEnvelope
from outline_agent.planning.tool_planner import UnifiedToolPlanner
from outline_agent.processing.processor_prompting import build_system_prompt, build_user_prompt
from outline_agent.processing.processor_types import CrossThreadHandoff
from outline_agent.state.workspace import CollectionWorkspaceManager
from outline_agent.tools.base import ToolSpec
from outline_agent.utils.attachment_context import AttachmentContextItem


def _load_comment_fixture():
    fixture_path = Path(__file__).parent / "fixtures" / "comments.create.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    payload["actorId"] = "user-1"
    payload["payload"]["model"]["createdById"] = "user-1"
    payload["payload"]["model"]["createdBy"] = {"id": "user-1", "name": "User"}
    payload["payload"]["model"]["data"] = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "Please summarize and then update the draft."}],
            }
        ],
    }
    return WebhookEnvelope.model_validate(payload).payload.model


def test_build_system_prompt_layers_base_packs_collection_prompt_and_workspace_memory(tmp_path: Path) -> None:
    manager = CollectionWorkspaceManager(tmp_path / "agents")
    workspace = manager.ensure(collection_id="collection-1", collection_name="Demo Collection")
    workspace.system_prompt_path.write_text("Collection-specific instructions.", encoding="utf-8")
    workspace.memory_path.write_text(
        "# MEMORY.md - Collection Working Memory\n\n"
        "## Collection Profile\n"
        "- Collection ID: collection-1\n"
        "- Collection Name: Demo Collection\n\n"
        "## Durable Facts\n"
        "- The collection tracks architecture work.\n\n"
        "## Decisions\n\n"
        "## Working Notes\n",
        encoding="utf-8",
    )

    prompt = build_system_prompt(
        system_prompt="Base system prompt.",
        workspace=workspace,
        prompt_packs=[PromptPack(name="outline_style", text="Style pack guidance.")],
        max_memory_chars=10_000,
    )

    assert "Base system prompt." in prompt
    assert "[Prompt pack: outline_style]" in prompt
    assert "Style pack guidance." in prompt
    assert "[Collection prompt]" in prompt
    assert "Collection-specific instructions." in prompt
    assert "Collection workspace context follows" in prompt
    assert "The collection tracks architecture work." in prompt


def test_build_user_prompt_includes_memory_runtime_and_outcome_sections(tmp_path: Path) -> None:
    manager = CollectionWorkspaceManager(tmp_path / "agents")
    workspace = manager.ensure(collection_id="collection-1", collection_name="Demo Collection")
    document_workspace = manager.ensure_document(
        workspace,
        document_id="doc-1",
        document_title="Kickoff",
    )
    thread_workspace = manager.ensure_thread(
        workspace,
        thread_id="thread-1",
        document_id="doc-1",
        document_title="Kickoff",
    )
    document_workspace.memory_path.write_text(
        "# MEMORY.md - Document Working Memory\n\n"
        "## Document Profile\n"
        "- Document ID: doc-1\n"
        "- Document Title: Kickoff\n\n"
        "## Summary\n"
        "Initial summary.\n\n"
        "## Open Questions\n"
        "- What should be implemented first?\n\n"
        "## Working Notes\n"
        "- Keep answers concise.\n",
        encoding="utf-8",
    )
    thread_workspace.record_turn(
        comment_id="comment-1",
        user_comment="First request",
        assistant_reply="First reply",
        document_id="doc-1",
        document_title="Kickoff",
        max_recent_turns=4,
        max_turn_chars=200,
    )

    prompt = build_user_prompt(
        comment=_load_comment_fixture(),
        document=OutlineDocument(
            id="doc-1",
            title="Kickoff",
            collection_id="collection-1",
            url="/doc/kickoff",
            text="# Kickoff\n\nCurrent document body.",
        ),
        collection=OutlineCollection(
            id="collection-1",
            name="Demo Collection",
            description=None,
            url="/collection/demo",
        ),
        workspace=workspace,
        document_workspace=document_workspace,
        thread_workspace=thread_workspace,
        prompt_text="Please summarize and then update the draft.",
        comment_context="Thread root and latest reply.",
        document_creation_context="- status: applied\n- created document id: created-doc-1",
        document_update_context="- status: applied\n- summary: Updated roadmap",
        tool_execution_context="- status: applied\n- step 1: write_file[report.txt]",
        memory_action_context="- status: applied\n- planned: add[facts]: keep replies concise",
        same_document_comment_context="Most relevant matching thread: thread-x",
        related_documents_context="- doc-related | Architecture Notes",
        handoff=CrossThreadHandoff(
            mode="resolved",
            preview="handoff",
            prompt_section="Most likely referenced thread: roadmap rollout",
        ),
        current_comment_image_count=1,
        reply_policy_text="Reply directly and keep it concise.",
        max_document_chars=5_000,
        max_document_memory_chars=2_000,
        max_prompt_chars=2_000,
    )

    assert "Persisted document memory:" in prompt
    assert "Initial summary." in prompt
    assert "Thread runtime state:" in prompt
    assert "interaction_count: 1" in prompt
    assert "Document creation outcome:" in prompt
    assert "Document update outcome:" in prompt
    assert "Tool execution outcome:" in prompt
    assert "Memory action outcome:" in prompt
    assert "Same-document comment lookup outcome:" in prompt
    assert "Related documents in this collection:" in prompt
    assert "Potential cross-thread handoff context:" in prompt
    assert "Current user comment also includes 1 embedded image." in prompt
    assert "Reply directly and keep it concise." in prompt
    assert "keep the comment reply short" in prompt
    assert "keep the comment reply very short" in prompt


def test_tool_planner_prompt_includes_rounds_inventory_and_attachment_context(tmp_path: Path) -> None:
    settings = AppSettings(
        workspace_root=tmp_path / "agents",
        tool_execution_max_rounds=3,
        tool_execution_max_steps=4,
        tool_execution_chunk_size=2,
    )
    manager = CollectionWorkspaceManager(settings.workspace_root)
    workspace = manager.ensure(collection_id="collection-1", collection_name="Demo Collection")
    document_workspace = manager.ensure_document(workspace, document_id="doc-1", document_title="Kickoff")
    thread_workspace = manager.ensure_thread(workspace, thread_id="thread-1", document_id="doc-1", document_title="Kickoff")
    (workspace.workspace_dir / "report.txt").write_text("artifact-generated-in-round-one\n", encoding="utf-8")

    planner = UnifiedToolPlanner(settings, model_client=object())
    prompt = planner._build_user_prompt(  # noqa: SLF001
        available_tools=[
            ToolSpec(name="write_file", description="Write a file", side_effect_level="write"),
            ToolSpec(name="read_file", description="Read a file", side_effect_level="read"),
        ],
        workspace=workspace,
        document_workspace=document_workspace,
        thread_workspace=thread_workspace,
        collection=OutlineCollection(id="collection-1", name="Demo Collection", description=None, url=None),
        document=OutlineDocument(
            id="doc-1",
            title="Kickoff",
            collection_id="collection-1",
            url="/doc/kickoff",
            text="# Kickoff\n\nDocument body.",
        ),
        user_comment="Inspect the generated artifact and summarize it.",
        comment_context="Latest comments here.",
        related_documents_context="- doc-2 | Architecture",
        current_round=2,
        prior_round_summaries=["round 1 (status=applied): write_file[report.txt]"],
        prior_round_observations=["round 1 (status=applied): read_file[report.txt] -> artifact-generated-in-round-one"],
        available_attachment_context=[
            AttachmentContextItem(
                source_url="/api/attachments.redirect?id=att-1",
                suggested_path="attachments/current/report.pdf",
                origin="current_comment",
                kind="attachment",
                label="report.pdf",
                comment_id="comment-1",
                author_name="User",
            )
        ],
        current_comment_image_count=1,
    )

    system_prompt = planner.build_system_prompt()

    assert "You plan bounded tool use for an Outline agent." in system_prompt
    assert "Additional internal policy:" in system_prompt
    assert "Current planning round: 2 of 3" in prompt
    assert "Action execution history in this turn:" in prompt
    assert "Structured observations from prior rounds:" in prompt
    assert "Current work dir inventory:" in prompt
    assert "report.txt" in prompt
    assert "Available attachment candidates for `download_attachment`" in prompt
    assert "source_url=/api/attachments.redirect?id=att-1" in prompt
    assert "Current Outline document ID: doc-1" in prompt
    assert "The latest user comment also includes 1 embedded image." in prompt
