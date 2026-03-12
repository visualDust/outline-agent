from __future__ import annotations

import asyncio
from pathlib import Path

from outline_agent.core.config import AppSettings
from outline_agent.runtime.tool_runtime import ToolExecutionPlan, ToolExecutionStep, ToolRuntime


class DummyOutlineClient:
    def __init__(self) -> None:
        self.uploads: list[dict[str, str]] = []
        self.downloads: list[dict[str, str]] = []

    async def upload_attachment(self, document_id: str, file_path: Path) -> dict:
        self.uploads.append({"document_id": document_id, "file_path": str(file_path)})
        return {
            "ok": True,
            "attachment": {
                "id": "attachment-1",
                "name": file_path.name,
                "url": "https://outline.example/api/attachments.redirect?id=attachment-1",
            },
        }

    async def download_attachment(self, url_or_path: str, file_path: Path) -> dict:
        self.downloads.append({"url_or_path": url_or_path, "file_path": str(file_path)})
        file_path.write_bytes(b"downloaded bytes")
        return {
            "ok": True,
            "url": url_or_path,
            "file_path": str(file_path),
            "size": len(b"downloaded bytes"),
        }


class ExplodingOutlineClient:
    async def upload_attachment(self, document_id: str, file_path: Path) -> dict:
        raise RuntimeError("boom")


def test_tool_runtime_writes_and_runs_script(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=False,
    )
    runtime = ToolRuntime(settings)
    work_dir = tmp_path / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    plan = ToolExecutionPlan(
        should_run=True,
        steps=[
            ToolExecutionStep(
                tool="write_file",
                path="hello.sh",
                content="#!/usr/bin/env bash\necho hello-from-tool\n",
            ),
            ToolExecutionStep(tool="run_shell", command="bash hello.sh"),
        ],
    )

    report = asyncio.run(runtime.execute_plan(work_dir, plan))

    assert report.status == "applied"
    assert (work_dir / "hello.sh").exists()
    assert report.preview is not None
    assert "write_file[hello.sh]" in report.preview
    assert "stdout=hello-from-tool" in report.preview


def test_tool_runtime_blocks_path_escape(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=False,
    )
    runtime = ToolRuntime(settings)
    work_dir = tmp_path / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    plan = ToolExecutionPlan(
        should_run=True,
        steps=[ToolExecutionStep(tool="write_file", path="../escape.txt", content="nope")],
    )

    report = asyncio.run(runtime.execute_plan(work_dir, plan))

    assert report.status == "failed"
    assert report.preview is not None
    assert "path escapes work dir" in report.preview
    assert not (tmp_path / "escape.txt").exists()


def test_tool_runtime_times_out_shell(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=False,
        tool_shell_timeout_seconds=0.1,
    )
    runtime = ToolRuntime(settings)
    work_dir = tmp_path / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    plan = ToolExecutionPlan(
        should_run=True,
        steps=[ToolExecutionStep(tool="run_shell", command="python -c 'import time; time.sleep(1)'")],
    )

    report = asyncio.run(runtime.execute_plan(work_dir, plan))

    assert report.status == "failed"
    assert report.preview is not None
    assert "timed out" in report.preview


def test_tool_runtime_invokes_progress_callback_around_each_step(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=False,
    )
    runtime = ToolRuntime(settings)
    work_dir = tmp_path / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    plan = ToolExecutionPlan(
        should_run=True,
        steps=[ToolExecutionStep(tool="write_file", path="hello.txt", content="hello")],
    )
    events: list[tuple[str, str, str | None]] = []

    async def on_progress(stage: str, step: ToolExecutionStep, result) -> None:
        summary = result.summary if result is not None else None
        events.append((stage, step.tool, summary))

    report = asyncio.run(runtime.execute_plan(work_dir, plan, on_progress=on_progress))

    assert report.status == "applied"
    assert events == [
        ("before_step", "write_file", None),
        ("after_step", "write_file", "write_file[hello.txt] -> 5 chars"),
    ]


def test_tool_runtime_uploads_attachment_to_outline_document(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=False,
    )
    outline_client = DummyOutlineClient()
    runtime = ToolRuntime(settings, outline_client=outline_client)
    work_dir = tmp_path / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    artifact = work_dir / "report.pdf"
    artifact.write_bytes(b"%PDF-1.7\nfake\n")
    plan = ToolExecutionPlan(
        should_run=True,
        steps=[ToolExecutionStep(tool="upload_attachment", path="report.pdf")],
    )

    report = asyncio.run(runtime.execute_plan(work_dir, plan, document_id="doc-1"))

    assert report.status == "applied"
    assert report.preview == "upload_attachment[report.pdf] -> attachment_id=attachment-1"
    assert report.step_results[0].attachment is not None
    assert report.step_results[0].attachment.url == "https://outline.example/api/attachments.redirect?id=attachment-1"
    assert report.step_results[0].attachment.file_hash is not None
    assert outline_client.uploads == [{"document_id": "doc-1", "file_path": str(artifact)}]


def test_tool_runtime_converts_unexpected_upload_exception_into_failed_report(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=False,
    )
    runtime = ToolRuntime(settings, outline_client=ExplodingOutlineClient())
    work_dir = tmp_path / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    artifact = work_dir / "report.pdf"
    artifact.write_bytes(b"%PDF-1.7\nfake\n")
    plan = ToolExecutionPlan(
        should_run=True,
        steps=[ToolExecutionStep(tool="upload_attachment", path="report.pdf")],
    )

    report = asyncio.run(runtime.execute_plan(work_dir, plan, document_id="doc-1"))

    assert report.status == "failed"
    assert report.preview == "upload_attachment: unexpected error: boom"


def test_tool_runtime_downloads_attachment_into_work_dir(tmp_path: Path) -> None:
    settings = AppSettings(
        outline_api_key="ol_api_test",
        outline_webhook_signing_secret="ol_whs_test",
        workspace_root=tmp_path / "agents",
        dedupe_store_path=tmp_path / "processed.json",
        dry_run=False,
    )
    outline_client = DummyOutlineClient()
    runtime = ToolRuntime(settings, outline_client=outline_client)
    work_dir = tmp_path / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    plan = ToolExecutionPlan(
        should_run=True,
        steps=[
            ToolExecutionStep(
                tool="download_attachment",
                path="downloads/report.pdf",
                source_url="/api/attachments.redirect?id=attachment-1",
            )
        ],
    )

    report = asyncio.run(runtime.execute_plan(work_dir, plan, document_id="doc-1"))

    assert report.status == "applied"
    assert (
        report.preview == "download_attachment[downloads/report.pdf] <- "
        "/api/attachments.redirect?id=attachment-1 -> 16 bytes"
    )
    assert (work_dir / "downloads" / "report.pdf").read_bytes() == b"downloaded bytes"
    assert outline_client.downloads == [
        {
            "url_or_path": "/api/attachments.redirect?id=attachment-1",
            "file_path": str(work_dir / "downloads" / "report.pdf"),
        }
    ]
