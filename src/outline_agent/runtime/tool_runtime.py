from __future__ import annotations

import asyncio
import hashlib
import os
import warnings
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from ..clients.outline_client import OutlineClient, OutlineClientError
from ..core.config import AppSettings
from ..core.logging import logger


class ToolExecutionStep(BaseModel):
    tool: Literal[
        "list_dir",
        "read_file",
        "write_file",
        "edit_file",
        "run_shell",
        "upload_attachment",
        "download_attachment",
    ]
    path: str | None = None
    source_url: str | None = None
    content: str | None = None
    append: bool = False
    old_text: str | None = None
    new_text: str | None = None
    command: str | None = None
    recursive: bool = False


class ToolExecutionPlan(BaseModel):
    should_run: bool = False
    reason: str | None = None
    steps: list[ToolExecutionStep] = Field(default_factory=list)


@dataclass(frozen=True)
class UploadedAttachment:
    path: str
    name: str
    url: str | None = None
    attachment_id: str | None = None
    file_hash: str | None = None


@dataclass(frozen=True)
class ToolStepResult:
    tool: str
    ok: bool
    summary: str
    target: str | None = None
    stdout: str | None = None
    stderr: str | None = None
    exit_code: int | None = None
    attachment: UploadedAttachment | None = None
    requires_confirmation: bool = False
    approval_status: str | None = None
    approval_mode: str | None = None
    approval_reason: str | None = None


@dataclass(frozen=True)
class ToolExecutionReport:
    status: str
    step_results: list[ToolStepResult]
    error: str | None = None

    @property
    def preview(self) -> str | None:
        if not self.step_results:
            return None
        return " ; ".join(result.summary for result in self.step_results if result.summary)


class ToolRuntimeError(RuntimeError):
    """Raised when a local tool action cannot be executed safely."""


def collect_uploaded_attachments(step_results: list[ToolStepResult]) -> list[UploadedAttachment]:
    attachments: list[UploadedAttachment] = []
    for result in step_results:
        if result.attachment is not None:
            attachments.append(result.attachment)
    return attachments


class ToolRuntime:
    def __init__(self, settings: AppSettings, outline_client: OutlineClient | None = None):
        self.settings = settings
        self.outline_client = outline_client

    async def execute_plan(
        self,
        work_dir: Path,
        plan: ToolExecutionPlan,
        document_id: str | None = None,
        on_progress: Callable[[str, ToolExecutionStep, ToolStepResult | None], Awaitable[None] | None] | None = None,
    ) -> ToolExecutionReport:
        step_results: list[ToolStepResult] = []
        for step in plan.steps[: self.settings.tool_execution_max_steps]:
            if on_progress is not None:
                callback_result = on_progress("before_step", step, None)
                if callback_result is not None:
                    await callback_result
            result = await self.execute_step(work_dir, step, document_id=document_id)
            step_results.append(result)
            if on_progress is not None:
                callback_result = on_progress("after_step", step, result)
                if callback_result is not None:
                    await callback_result
            if not result.ok:
                return ToolExecutionReport(
                    status="failed",
                    step_results=step_results,
                    error=result.summary,
                )
        return ToolExecutionReport(status="applied", step_results=step_results)

    async def execute_step(
        self,
        work_dir: Path,
        step: ToolExecutionStep,
        *,
        document_id: str | None = None,
    ) -> ToolStepResult:
        try:
            if step.tool == "list_dir":
                return self._list_dir(work_dir, step)
            if step.tool == "read_file":
                return self._read_file(work_dir, step)
            if step.tool == "write_file":
                return self._write_file(work_dir, step)
            if step.tool == "edit_file":
                return self._edit_file(work_dir, step)
            if step.tool == "run_shell":
                return await self._run_shell(work_dir, step)
            if step.tool == "upload_attachment":
                return await self._upload_attachment(work_dir, step, document_id=document_id)
            if step.tool == "download_attachment":
                return await self._download_attachment(work_dir, step)
        except (ToolRuntimeError, OutlineClientError) as exc:
            return ToolStepResult(tool=step.tool, ok=False, summary=f"{step.tool}: {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected local tool failure for step {}", step.tool)
            return ToolStepResult(tool=step.tool, ok=False, summary=f"{step.tool}: unexpected error: {exc}")

        return ToolStepResult(tool=step.tool, ok=False, summary=f"unsupported tool: {step.tool}")

    def _list_dir(self, work_dir: Path, step: ToolExecutionStep) -> ToolStepResult:
        target_dir = self._resolve_path(work_dir, step.path or ".")
        if not target_dir.exists():
            raise ToolRuntimeError(f"directory does not exist: {step.path or '.'}")
        if not target_dir.is_dir():
            raise ToolRuntimeError(f"path is not a directory: {step.path or '.'}")

        if step.recursive:
            entries = _walk_relative_paths(target_dir, self.settings.tool_list_dir_max_entries)
        else:
            entries = _list_relative_children(target_dir, self.settings.tool_list_dir_max_entries)
        rel_target = str(target_dir.relative_to(work_dir)) if target_dir != work_dir else "."
        preview = ", ".join(entries) if entries else "(empty)"
        return ToolStepResult(
            tool=step.tool,
            ok=True,
            target=rel_target,
            summary=f"list_dir[{rel_target}] -> {preview}",
        )

    def _read_file(self, work_dir: Path, step: ToolExecutionStep) -> ToolStepResult:
        target_file = self._resolve_path(work_dir, step.path)
        if not target_file.exists():
            raise ToolRuntimeError(f"file does not exist: {step.path}")
        if not target_file.is_file():
            raise ToolRuntimeError(f"path is not a file: {step.path}")

        content = target_file.read_text(encoding="utf-8")
        rel_path = str(target_file.relative_to(work_dir))
        preview = _truncate_text(content, self.settings.tool_shell_max_output_chars)
        return ToolStepResult(
            tool=step.tool,
            ok=True,
            target=rel_path,
            stdout=preview,
            summary=f"read_file[{rel_path}] -> {preview or '(empty file)'}",
        )

    def _write_file(self, work_dir: Path, step: ToolExecutionStep) -> ToolStepResult:
        target_file = self._resolve_path(work_dir, step.path)
        content = step.content or ""
        target_file.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if step.append else "w"
        with target_file.open(mode, encoding="utf-8") as handle:
            handle.write(content)
        rel_path = str(target_file.relative_to(work_dir))
        action = "append" if step.append else "write"
        return ToolStepResult(
            tool=step.tool,
            ok=True,
            target=rel_path,
            summary=f"{action}_file[{rel_path}] -> {len(content)} chars",
        )

    def _edit_file(self, work_dir: Path, step: ToolExecutionStep) -> ToolStepResult:
        target_file = self._resolve_path(work_dir, step.path)
        if not target_file.exists():
            raise ToolRuntimeError(f"file does not exist: {step.path}")
        if not target_file.is_file():
            raise ToolRuntimeError(f"path is not a file: {step.path}")
        if step.old_text is None or step.new_text is None:
            raise ToolRuntimeError("edit_file requires old_text and new_text")

        original = target_file.read_text(encoding="utf-8")
        if step.old_text not in original:
            raise ToolRuntimeError("old_text not found in target file")

        updated = original.replace(step.old_text, step.new_text, 1)
        target_file.write_text(updated, encoding="utf-8")
        rel_path = str(target_file.relative_to(work_dir))
        return ToolStepResult(
            tool=step.tool,
            ok=True,
            target=rel_path,
            summary=f"edit_file[{rel_path}] -> replaced 1 occurrence",
        )

    async def _run_shell(self, work_dir: Path, step: ToolExecutionStep) -> ToolStepResult:
        command = (step.command or "").strip()
        if not command:
            raise ToolRuntimeError("run_shell requires a non-empty command")

        loop = asyncio.get_running_loop()
        _ensure_subprocess_support(loop)

        process = await asyncio.create_subprocess_shell(
            command,
            cwd=str(work_dir),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=self.settings.tool_shell_timeout_seconds,
            )
        except TimeoutError:
            process.kill()
            await process.communicate()
            return ToolStepResult(
                tool=step.tool,
                ok=False,
                summary=(
                    f"run_shell[{_preview_text(command, 60)}] -> timed out after "
                    f"{self.settings.tool_shell_timeout_seconds:g}s"
                ),
            )

        stdout = _truncate_text(
            stdout_bytes.decode("utf-8", errors="replace"),
            self.settings.tool_shell_max_output_chars,
        )
        stderr = _truncate_text(
            stderr_bytes.decode("utf-8", errors="replace"),
            self.settings.tool_shell_max_output_chars,
        )
        exit_code = process.returncode
        status_text = f"exit {exit_code}"
        extras: list[str] = []
        if stdout:
            extras.append(f"stdout={stdout}")
        if stderr:
            extras.append(f"stderr={stderr}")
        suffix = f" ; {' ; '.join(extras)}" if extras else ""
        return ToolStepResult(
            tool=step.tool,
            ok=exit_code == 0,
            target=_preview_text(command, 80),
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            summary=f"run_shell[{_preview_text(command, 60)}] -> {status_text}{suffix}",
        )

    async def _upload_attachment(
        self,
        work_dir: Path,
        step: ToolExecutionStep,
        *,
        document_id: str | None,
    ) -> ToolStepResult:
        if not document_id:
            raise ToolRuntimeError("upload_attachment requires a target Outline document id")
        if self.outline_client is None:
            raise ToolRuntimeError("upload_attachment requires an Outline client")

        target_file = self._resolve_path(work_dir, step.path)
        if not target_file.exists():
            raise ToolRuntimeError(f"file does not exist: {step.path}")
        if not target_file.is_file():
            raise ToolRuntimeError(f"path is not a file: {step.path}")

        result = await self.outline_client.upload_attachment(document_id=document_id, file_path=target_file)
        attachment = result.get("attachment") if isinstance(result.get("attachment"), dict) else {}
        attachment_id = attachment.get("id") if isinstance(attachment.get("id"), str) else None
        attachment_name = attachment.get("name") if isinstance(attachment.get("name"), str) else target_file.name
        attachment_url = attachment.get("url") if isinstance(attachment.get("url"), str) else None
        rel_path = str(target_file.relative_to(work_dir))
        suffix = f" -> attachment_id={attachment_id}" if attachment_id else " -> uploaded"
        return ToolStepResult(
            tool=step.tool,
            ok=True,
            target=rel_path,
            summary=f"upload_attachment[{rel_path}]{suffix}",
            attachment=UploadedAttachment(
                path=rel_path,
                name=attachment_name,
                url=attachment_url,
                attachment_id=attachment_id,
                file_hash=_sha256_file(target_file),
            ),
        )

    async def _download_attachment(self, work_dir: Path, step: ToolExecutionStep) -> ToolStepResult:
        if self.outline_client is None:
            raise ToolRuntimeError("download_attachment requires an Outline client")
        source_url = (step.source_url or "").strip()
        if not source_url:
            raise ToolRuntimeError("download_attachment requires a source_url")

        target_file = self._resolve_path(work_dir, step.path)
        target_file.parent.mkdir(parents=True, exist_ok=True)
        result = await self.outline_client.download_attachment(source_url, target_file)
        rel_path = str(target_file.relative_to(work_dir))
        size = result.get("size") if isinstance(result.get("size"), int) else None
        size_suffix = f" -> {size} bytes" if size is not None else ""
        return ToolStepResult(
            tool=step.tool,
            ok=True,
            target=rel_path,
            summary=f"download_attachment[{rel_path}] <- {_preview_text(source_url, 80)}{size_suffix}",
        )

    def _resolve_path(self, work_dir: Path, raw_path: str | None) -> Path:
        candidate_text = (raw_path or "").strip()
        if not candidate_text:
            raise ToolRuntimeError("path is required")

        root = Path(os.path.normpath(str(work_dir)))
        candidate = Path(os.path.normpath(str(work_dir / candidate_text)))
        if candidate != root and root not in candidate.parents:
            raise ToolRuntimeError(f"path escapes work dir: {candidate_text}")
        return candidate


def describe_work_dir(work_dir: Path, max_entries: int) -> str:
    resolved = work_dir.resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    entries = _walk_relative_paths(resolved, max_entries)
    return "\n".join(f"- {item}" for item in entries) or "(empty work dir)"


def _ensure_subprocess_support(loop: asyncio.AbstractEventLoop) -> None:
    """Ensure the current event loop is ready for subprocess management.

    In some test/runner environments, the default child watcher may not be attached
    to the active loop, which can cause asyncio subprocess calls to hang.
    """

    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=".*get_child_watcher.*deprecated.*",
            category=DeprecationWarning,
        )
        try:
            watcher = asyncio.get_child_watcher()
        except NotImplementedError:
            return

    attach = getattr(watcher, "attach_loop", None)
    if not callable(attach):
        return

    try:
        attach(loop)
    except Exception:  # noqa: BLE001 - fallback to a fresh watcher when misconfigured
        watcher = asyncio.SafeChildWatcher()
        asyncio.set_child_watcher(watcher)
        watcher.attach_loop(loop)


def _walk_relative_paths(root: Path, limit: int) -> list[str]:
    entries: list[str] = []
    for path in sorted(root.rglob("*")):
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        value = relative.as_posix() + ("/" if path.is_dir() else "")
        entries.append(value)
        if len(entries) >= limit:
            break
    return entries


def _list_relative_children(root: Path, limit: int) -> list[str]:
    entries: list[str] = []
    for path in sorted(root.iterdir()):
        value = path.name + ("/" if path.is_dir() else "")
        entries.append(value)
        if len(entries) >= limit:
            break
    return entries


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _truncate_text(text: str, limit: int) -> str:
    compact = text.strip()
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 1)].rstrip() + "…"


def _preview_text(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 1)].rstrip() + "…"
