from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..core.config import AppSettings, PROJECT_ROOT
from ..core.logging import logger

MERMAID_VALIDATION_FAILED_PREFIX = "MERMAID_VALIDATION_FAILED:"
MERMAID_VALIDATION_UNAVAILABLE_PREFIX = "MERMAID_VALIDATION_UNAVAILABLE:"

_WARNING_EMITTED = False
_PROBE_CACHE: dict[tuple[bool, str, float], "MermaidValidatorProbe"] = {}

_MERMAID_FENCE_RE = re.compile(r"(?ms)^```mermaid[^\n]*\n(.*?)^```[ \t]*$")
_PARSE_LINE_RE = re.compile(r"(?i)(?:parse|lexical|syntax) error on line (\d+)")


@dataclass(frozen=True)
class MermaidFence:
    index: int
    source: str
    markdown_start_line: int


@dataclass(frozen=True)
class MermaidValidationError:
    block_index: int
    markdown_start_line: int
    mermaid_line: int | None
    message: str
    excerpt: str | None = None


@dataclass(frozen=True)
class MermaidValidatorProbe:
    available: bool
    backend: str | None
    command: tuple[str, ...] | None
    reason: str | None = None
    version: str | None = None
    puppeteer_config_path: str | None = None


@dataclass(frozen=True)
class MermaidValidationResult:
    status: Literal["skipped", "passed", "invalid", "unavailable"]
    available: bool
    checked_blocks: int
    errors: tuple[MermaidValidationError, ...]
    backend: str | None = None
    reason: str | None = None

    @property
    def has_mermaid(self) -> bool:
        return self.checked_blocks > 0 or bool(self.errors)


def clear_mermaid_validator_probe_cache() -> None:
    global _WARNING_EMITTED
    _PROBE_CACHE.clear()
    _WARNING_EMITTED = False


def extract_mermaid_fences(markdown_text: str) -> list[MermaidFence]:
    fences: list[MermaidFence] = []
    for index, match in enumerate(_MERMAID_FENCE_RE.finditer(markdown_text), start=1):
        source = match.group(1).strip()
        fences.append(
            MermaidFence(
                index=index,
                source=source,
                markdown_start_line=markdown_text.count("\n", 0, match.start()) + 1,
            )
        )
    return fences


def get_mermaid_validator_probe(
    settings: AppSettings,
    *,
    log_warning: bool = False,
    force_refresh: bool = False,
) -> MermaidValidatorProbe:
    cache_key = (
        settings.mermaid_validation_enabled,
        settings.mermaid_validation_mode,
        settings.mermaid_validation_timeout_seconds,
    )
    if not force_refresh and cache_key in _PROBE_CACHE:
        probe = _PROBE_CACHE[cache_key]
        if log_warning:
            _maybe_log_probe_warning(settings, probe)
        return probe

    if not settings.mermaid_validation_enabled or settings.mermaid_validation_mode == "off":
        probe = MermaidValidatorProbe(
            available=False,
            backend="mermaid-cli",
            command=None,
            reason="Mermaid validation is disabled by configuration.",
        )
        _PROBE_CACHE[cache_key] = probe
        return probe

    command = _resolve_mermaid_cli_command()
    puppeteer_config_path = _resolve_puppeteer_config_path()
    if command is None:
        probe = MermaidValidatorProbe(
            available=False,
            backend="mermaid-cli",
            command=None,
            reason="Mermaid CLI was not found in PATH or the project node_modules/.bin directory.",
            puppeteer_config_path=puppeteer_config_path,
        )
    else:
        try:
            sample_source = "graph TD\n  A[Probe] --> B[OK]\n"
            invocation = _invoke_mermaid_cli(
                source=sample_source,
                command=command,
                timeout_seconds=settings.mermaid_validation_timeout_seconds,
                puppeteer_config_path=puppeteer_config_path,
            )
            if invocation.returncode == 0:
                version = _read_mermaid_cli_version(command)
                probe = MermaidValidatorProbe(
                    available=True,
                    backend="mermaid-cli",
                    command=command,
                    version=version,
                    puppeteer_config_path=puppeteer_config_path,
                )
            else:
                message = _normalize_cli_output(invocation.stdout, invocation.stderr) or "unknown Mermaid CLI failure"
                probe = MermaidValidatorProbe(
                    available=False,
                    backend="mermaid-cli",
                    command=command,
                    reason=f"Mermaid CLI self-test failed: {message}",
                    puppeteer_config_path=puppeteer_config_path,
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            probe = MermaidValidatorProbe(
                available=False,
                backend="mermaid-cli",
                command=command,
                reason=f"Mermaid CLI self-test failed: {exc}",
                puppeteer_config_path=puppeteer_config_path,
            )

    _PROBE_CACHE[cache_key] = probe
    if log_warning:
        _maybe_log_probe_warning(settings, probe)
    return probe


def validate_mermaid_markdown(
    markdown_text: str,
    *,
    settings: AppSettings,
) -> MermaidValidationResult:
    if not settings.mermaid_validation_enabled or settings.mermaid_validation_mode == "off":
        return MermaidValidationResult(
            status="skipped",
            available=False,
            checked_blocks=0,
            errors=(),
            backend="mermaid-cli",
            reason="Mermaid validation disabled.",
        )

    fences = extract_mermaid_fences(markdown_text)
    if not fences:
        return MermaidValidationResult(
            status="skipped",
            available=False,
            checked_blocks=0,
            errors=(),
            backend="mermaid-cli",
            reason="No Mermaid code fences found.",
        )

    probe = get_mermaid_validator_probe(settings)
    if not probe.available or not probe.command:
        return MermaidValidationResult(
            status="unavailable",
            available=False,
            checked_blocks=len(fences),
            errors=(),
            backend=probe.backend,
            reason=probe.reason,
        )

    errors: list[MermaidValidationError] = []
    for fence in fences:
        try:
            invocation = _invoke_mermaid_cli(
                source=fence.source,
                command=probe.command,
                timeout_seconds=settings.mermaid_validation_timeout_seconds,
                puppeteer_config_path=probe.puppeteer_config_path,
            )
        except subprocess.TimeoutExpired as exc:
            return MermaidValidationResult(
                status="unavailable",
                available=False,
                checked_blocks=len(fences),
                errors=tuple(errors),
                backend=probe.backend,
                reason=f"Mermaid CLI timed out after {exc.timeout:g}s.",
            )
        except OSError as exc:
            return MermaidValidationResult(
                status="unavailable",
                available=False,
                checked_blocks=len(fences),
                errors=tuple(errors),
                backend=probe.backend,
                reason=f"Mermaid CLI invocation failed: {exc}",
            )

        if invocation.returncode == 0:
            continue

        combined = _normalize_cli_output(invocation.stdout, invocation.stderr)
        parse_line = _extract_mermaid_line_number(combined)
        if _looks_like_mermaid_parse_error(combined):
            excerpt = _excerpt_mermaid_source(fence.source, parse_line)
            errors.append(
                MermaidValidationError(
                    block_index=fence.index,
                    markdown_start_line=fence.markdown_start_line,
                    mermaid_line=parse_line,
                    message=_compact_error_message(combined),
                    excerpt=excerpt,
                )
            )
            continue

        return MermaidValidationResult(
            status="unavailable",
            available=False,
            checked_blocks=len(fences),
            errors=tuple(errors),
            backend=probe.backend,
            reason=(
                "Mermaid CLI failed unexpectedly while validating a diagram: "
                + (_compact_error_message(combined) or "unknown error")
            ),
        )

    if errors:
        return MermaidValidationResult(
            status="invalid",
            available=True,
            checked_blocks=len(fences),
            errors=tuple(errors),
            backend=probe.backend,
            reason="One or more Mermaid blocks failed validation.",
        )

    return MermaidValidationResult(
        status="passed",
        available=True,
        checked_blocks=len(fences),
        errors=(),
        backend=probe.backend,
    )


def build_mermaid_validation_failure(
    *,
    tool_name: str,
    document_text: str,
    settings: AppSettings,
    bypass_validation: bool = False,
) -> tuple[str, dict[str, object]] | None:
    if bypass_validation:
        logger.info(
            "Bypassing Mermaid validation for {} after retry exhaustion (exhausted_action={}).",
            tool_name,
            settings.mermaid_validation_exhausted_action,
        )
        return None
    result = validate_mermaid_markdown(document_text, settings=settings)
    if result.status in {"skipped", "passed"}:
        return None

    if result.status == "unavailable":
        if settings.mermaid_validation_mode == "required":
            summary = (
                f"{tool_name}: {MERMAID_VALIDATION_UNAVAILABLE_PREFIX} "
                f"Mermaid validation backend unavailable. {result.reason or ''}".strip()
            )
            return summary, {
                "mermaid_validation": {
                    "status": result.status,
                    "available": result.available,
                    "checked_blocks": result.checked_blocks,
                    "backend": result.backend,
                    "reason": result.reason,
                }
            }
        _maybe_log_validation_skip(tool_name=tool_name, result=result)
        return None

    summary = f"{tool_name}: {MERMAID_VALIDATION_FAILED_PREFIX} {format_mermaid_validation_errors(result)}"
    return summary, {
        "mermaid_validation": {
            "status": result.status,
            "available": result.available,
            "checked_blocks": result.checked_blocks,
            "backend": result.backend,
            "reason": result.reason,
            "errors": [
                {
                    "block_index": error.block_index,
                    "markdown_start_line": error.markdown_start_line,
                    "mermaid_line": error.mermaid_line,
                    "message": error.message,
                    "excerpt": error.excerpt,
                }
                for error in result.errors
            ],
        }
    }


def format_mermaid_validation_errors(result: MermaidValidationResult) -> str:
    if not result.errors:
        return result.reason or "Mermaid validation failed."
    details: list[str] = []
    for error in result.errors[:3]:
        location = f"block {error.block_index}"
        if error.markdown_start_line > 0:
            location += f", markdown line {error.markdown_start_line}"
        if error.mermaid_line is not None:
            location += f", mermaid line {error.mermaid_line}"
        detail = f"{location}: {error.message}"
        if error.excerpt:
            detail += f" | excerpt: {error.excerpt}"
        details.append(detail)
    if len(result.errors) > 3:
        details.append(f"... and {len(result.errors) - 3} more Mermaid block error(s)")
    return " ; ".join(details)


def _resolve_mermaid_cli_command() -> tuple[str, ...] | None:
    env_path = os.environ.get("OUTLINE_AGENT_MERMAID_CLI_PATH")
    candidates: list[str] = []
    if env_path:
        candidates.append(env_path)

    project_bin = PROJECT_ROOT / "node_modules" / ".bin" / ("mmdc.cmd" if os.name == "nt" else "mmdc")
    candidates.append(str(project_bin))
    cwd_bin = Path.cwd() / "node_modules" / ".bin" / ("mmdc.cmd" if os.name == "nt" else "mmdc")
    candidates.append(str(cwd_bin))

    which_result = shutil.which("mmdc")
    if which_result:
        candidates.append(which_result)

    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.exists() and os.access(path, os.X_OK):
            return (str(path),)
    return None


def _resolve_puppeteer_config_path() -> str | None:
    candidate = PROJECT_ROOT / "docker" / "puppeteer-mermaid.json"
    if candidate.exists():
        return str(candidate)
    return None


@dataclass(frozen=True)
class _MermaidCliInvocation:
    returncode: int
    stdout: str
    stderr: str


def _invoke_mermaid_cli(
    *,
    source: str,
    command: tuple[str, ...],
    timeout_seconds: float,
    puppeteer_config_path: str | None,
) -> _MermaidCliInvocation:
    with tempfile.TemporaryDirectory(prefix="outline-agent-mermaid-") as temp_dir:
        temp_root = Path(temp_dir)
        input_path = temp_root / "diagram.mmd"
        output_path = temp_root / "diagram.svg"
        input_path.write_text(source, encoding="utf-8")
        args = [*command, "-q", "-i", str(input_path), "-o", str(output_path)]
        if puppeteer_config_path:
            args[1:1] = ["-p", puppeteer_config_path]
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        return _MermaidCliInvocation(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


def _read_mermaid_cli_version(command: tuple[str, ...]) -> str | None:
    try:
        completed = subprocess.run(
            [*command, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = _normalize_cli_output(completed.stdout, completed.stderr)
    return text.strip() or None


def _normalize_cli_output(stdout: str | None, stderr: str | None) -> str:
    parts = [part.strip() for part in (stdout, stderr) if isinstance(part, str) and part.strip()]
    return "\n".join(parts)


def _looks_like_mermaid_parse_error(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in ("parse error", "lexical error", "syntax error", "expecting "))


def _extract_mermaid_line_number(text: str) -> int | None:
    match = _PARSE_LINE_RE.search(text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _compact_error_message(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "unknown Mermaid validation error"
    if lines[0].lower().startswith("error: "):
        return lines[0][7:].strip()
    return lines[0]


def _excerpt_mermaid_source(source: str, mermaid_line: int | None) -> str | None:
    lines = [line.rstrip() for line in source.splitlines()]
    if not lines:
        return None
    if mermaid_line is None or mermaid_line < 1 or mermaid_line > len(lines):
        excerpt = lines[0]
        return excerpt[:160] if excerpt else None
    excerpt = lines[mermaid_line - 1]
    return excerpt[:160] if excerpt else None


def _maybe_log_probe_warning(settings: AppSettings, probe: MermaidValidatorProbe) -> None:
    global _WARNING_EMITTED
    if _WARNING_EMITTED:
        return
    if settings.mermaid_validation_mode == "off" or not settings.mermaid_validation_enabled:
        return
    if probe.available:
        logger.info(
            "Mermaid validation ready: backend={}, version={}, command={}",
            probe.backend or "unknown",
            probe.version or "unknown",
            " ".join(probe.command or ()),
        )
        _WARNING_EMITTED = True
        return
    level = "error" if settings.mermaid_validation_mode == "required" else "warning"
    log_fn = getattr(logger, level)
    log_fn(
        "Mermaid validation unavailable (mode={}): {}. Document writes will {} Mermaid syntax checking.",
        settings.mermaid_validation_mode,
        probe.reason or "unknown reason",
        "fail without" if settings.mermaid_validation_mode == "required" else "skip",
    )
    _WARNING_EMITTED = True


def _maybe_log_validation_skip(*, tool_name: str, result: MermaidValidationResult) -> None:
    logger.debug(
        "Skipping Mermaid validation for {} because backend is unavailable: {}",
        tool_name,
        result.reason or "unknown reason",
    )
