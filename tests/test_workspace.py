from __future__ import annotations

from pathlib import Path

from outline_agent.state.workspace import CollectionWorkspaceManager


def test_collection_workspace_manager_bootstraps_only_system_prompt_until_memory_is_needed(tmp_path: Path) -> None:
    manager = CollectionWorkspaceManager(tmp_path / "agents")
    workspace = manager.ensure(
        collection_id="107b2669-e0ad-4abd-a66e-28305124edc8",
        collection_name="Outline Agent Dev Sandbox",
    )

    assert workspace.root_dir.exists()
    assert workspace.scratch_dir.exists()
    assert workspace.threads_dir.exists()
    assert workspace.system_prompt_path.exists()
    assert not workspace.memory_path.exists()

    prompt_context = workspace.load_prompt_context(max_chars=10_000)
    assert prompt_context == ""


def test_collection_workspace_manager_bootstraps_document_and_thread_files(tmp_path: Path) -> None:
    manager = CollectionWorkspaceManager(tmp_path / "agents")
    workspace = manager.ensure(
        collection_id="107b2669-e0ad-4abd-a66e-28305124edc8",
        collection_name="Outline Agent Dev Sandbox",
    )
    document_workspace = manager.ensure_document(
        workspace,
        document_id="doc-123",
        document_title="Outline Agent Kickoff",
    )

    thread_workspace = manager.ensure_thread(
        workspace,
        thread_id="root-user-comment",
        document_id="doc-123",
        document_title="Outline Agent Kickoff",
    )

    assert thread_workspace.root_dir.exists()
    assert document_workspace.root_dir.exists()
    assert document_workspace.memory_path.exists()
    assert document_workspace.state_path.exists()
    assert thread_workspace.state_path.exists()

    memory_text = document_workspace.memory_path.read_text(encoding="utf-8")
    assert "Document ID: doc-123" in memory_text
    assert "Document Title: Outline Agent Kickoff" in memory_text

    thread_workspace.record_turn(
        comment_id="comment-1",
        user_comment="Please summarize this document.",
        assistant_reply="Here is a short summary.",
        document_id="doc-123",
        document_title="Outline Agent Kickoff",
        max_recent_turns=4,
        max_turn_chars=200,
    )
    prompt_context = thread_workspace.load_prompt_context(max_chars=10_000)
    assert "state.json" in prompt_context
    assert "interaction_count: 1" in prompt_context
    assert "Please summarize this document." in prompt_context

    thread_workspace.record_tool_run(
        comment_id="comment-1",
        status="applied",
        summary="write_file[hello.sh] -> 20 chars ; run_shell[bash hello.sh] -> exit 0 ; stdout=hello",
        step_summaries=[
            "write_file[hello.sh] -> 20 chars",
            "run_shell[bash hello.sh] -> exit 0 ; stdout=hello",
        ],
        max_recent_runs=4,
        max_summary_chars=200,
    )
    prompt_context = thread_workspace.load_prompt_context(max_chars=10_000)
    assert "recent_tool_runs" in prompt_context
    assert "write_file[hello.sh]" in prompt_context

    thread_workspace.record_progress_comment(
        request_comment_id="comment-1",
        status_comment_id="status-comment-1",
        status="applied",
        summary="Done — local workspace actions finished.",
        actions=[
            "planned round 1: write_file[hello.sh] | run_shell[bash hello.sh]",
            "completed round 1: run_shell[bash hello.sh] -> exit 0 ; stdout=hello",
        ],
        max_recent_entries=4,
        max_action_chars=200,
    )
    prompt_context = thread_workspace.load_prompt_context(max_chars=10_000)
    assert "progress_comment_states" in prompt_context
    assert "recent_progress_events" in prompt_context
    assert "status-comment-1" in prompt_context
    assert "Done — local workspace actions finished." in prompt_context

    document_prompt_context = document_workspace.load_prompt_context(max_chars=10_000)
    assert "MEMORY.md" in document_prompt_context


def test_collection_workspace_manager_lists_active_and_archived_entities(tmp_path: Path) -> None:
    manager = CollectionWorkspaceManager(tmp_path / "agents")
    workspace = manager.ensure(
        collection_id="collection-1",
        collection_name="Demo",
    )
    active_document = manager.ensure_document(
        workspace,
        document_id="doc-active",
        document_title="Active Doc",
    )
    archived_document = manager.ensure_document(
        workspace,
        document_id="doc-archived",
        document_title="Archived Doc",
    )
    active_thread = manager.ensure_thread(
        workspace,
        thread_id="thread-active",
        document_id="doc-active",
        document_title="Active Doc",
    )
    archived_thread = manager.ensure_thread(
        workspace,
        thread_id="thread-archived",
        document_id="doc-active",
        document_title="Active Doc",
    )

    manager.archive_document(workspace, archived_document, reason="test")
    manager.archive_thread(workspace, archived_thread, reason="test")

    assert [item.document_id for item in manager.list_active_documents(workspace)] == ["doc-active"]
    assert [item.document_id for item in manager.list_archived_documents(workspace)] == ["doc-archived"]
    assert [item.thread_id for item in manager.list_active_threads(workspace)] == ["thread-active"]
    assert [item.thread_id for item in manager.list_archived_threads(workspace)] == ["thread-archived"]

    assert active_document.root_dir.exists()
    assert active_thread.root_dir.exists()
