from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from .bootstrap import validate_outline_runtime_identity
from .core.config import (
    APP_NAME,
    OUTLINE_AGENT_CONFIG_PATH_ENV,
    OUTLINE_AGENT_HOME_ENV,
    PROJECT_ROOT,
    AppSettings,
    clear_settings_cache,
    create_default_config,
    get_config_root,
    get_package_internal_prompt_dir,
    get_package_prompt_pack_dir,
    get_package_prompt_path,
    get_settings,
    get_user_config_path,
    get_user_config_root,
)
from .core.logging import configure_logging, logger
from .models.model_profiles import ModelProfileError, ModelProfileResolver


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=APP_NAME)
    subparsers = parser.add_subparsers(dest="command")

    start_parser = subparsers.add_parser("start", help="Start the Outline webhook service")
    start_parser.add_argument("--host", help="Bind host")
    start_parser.add_argument("--port", type=int, help="Bind port")
    start_parser.add_argument("--reload", action="store_true", help="Enable auto-reload")
    start_parser.add_argument("--config-path", help="Override config YAML path")
    start_parser.add_argument("--system-prompt-path", help="Override system prompt path")
    start_parser.add_argument("--prompt-pack-dir", help="Override prompt pack directory")
    start_parser.add_argument("--workspace-root", help="Override workspace root")
    start_parser.add_argument("--log-file-path", help="Override log file path")
    start_parser.add_argument("--log-level", help="Override log level")
    start_parser.add_argument(
        "--quiet-startup",
        action="store_true",
        help="Reduce startup config diagnostics",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    command = args.command or "start"
    if command != "start":
        parser.error(f"unknown command: {command}")
    return _run_start(args)


def _run_start(args: argparse.Namespace) -> int:
    previous_env = _apply_cli_overrides(args)
    clear_settings_cache()
    try:
        config_path = get_user_config_path()
        if not config_path.exists():
            create_default_config(config_path)
            print(
                f"[{APP_NAME}] created initial config at {config_path}; edit it and run again",
                file=sys.stderr,
            )
            return 0

        try:
            settings = get_settings()
        except Exception as exc:
            print(f"[{APP_NAME}] error: failed to load settings: {exc}", file=sys.stderr)
            return 2

        configure_logging(settings)
        if not args.quiet_startup:
            _print_startup_report(settings)

        try:
            profile = ModelProfileResolver(get_user_config_path()).resolve(settings.model_ref)
        except ModelProfileError as exc:
            logger.warning("Model configuration is not ready: {}", exc)
            print(f"[{APP_NAME}] warning: model configuration is not ready: {exc}", file=sys.stderr)
        except Exception as exc:
            logger.warning("Model configuration preflight failed: {}", exc)
            print(f"[{APP_NAME}] warning: model configuration preflight failed: {exc}", file=sys.stderr)
        else:
            logger.info(
                "Using model profile alias={} provider={} model={} base_url={}",
                profile.alias,
                profile.provider,
                profile.model,
                profile.base_url,
            )

        try:
            current_user = asyncio.run(validate_outline_runtime_identity(settings))
        except Exception as exc:
            logger.exception("Outline API identity validation failed at startup")
            print(f"[{APP_NAME}] error: outline API identity validation failed: {exc}", file=sys.stderr)
            return 2
        else:
            logger.info(
                "Validated Outline runtime identity at startup: {} ({})",
                current_user.id,
                current_user.name or "unknown",
            )

        try:
            uvicorn.run(
                "outline_agent.app:app",
                host=settings.host,
                port=settings.port,
                reload=bool(args.reload),
                log_level=settings.log_level.lower(),
            )
        except Exception as exc:
            logger.exception("Server startup failed")
            print(f"[{APP_NAME}] error: server startup failed: {exc}", file=sys.stderr)
            return 1
        return 0
    finally:
        _restore_cli_overrides(previous_env)
        clear_settings_cache()


def _apply_cli_overrides(args: argparse.Namespace) -> dict[str, str | None]:
    overrides = {
        OUTLINE_AGENT_CONFIG_PATH_ENV: args.config_path,
        "HOST": args.host,
        "PORT": str(args.port) if args.port is not None else None,
        "SYSTEM_PROMPT_PATH": args.system_prompt_path,
        "PROMPT_PACK_DIR": args.prompt_pack_dir,
        "WORKSPACE_ROOT": args.workspace_root,
        "LOG_FILE_PATH": args.log_file_path,
        "LOG_LEVEL": args.log_level,
    }
    previous_env: dict[str, str | None] = {}
    for key, value in overrides.items():
        previous_env[key] = os.environ.get(key)
        if value is not None:
            os.environ[key] = value
    return previous_env


def _restore_cli_overrides(previous_env: dict[str, str | None]) -> None:
    for key, value in previous_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _print_startup_report(settings: AppSettings) -> None:
    lines = [
        f"[{APP_NAME}] startup configuration",
        f"[{APP_NAME}] user config root: {get_user_config_root()}",
        f"[{APP_NAME}] active config root: {get_config_root()}",
        f"[{APP_NAME}] {OUTLINE_AGENT_HOME_ENV}: {os.environ.get(OUTLINE_AGENT_HOME_ENV, '<unset>')}",
        f"[{APP_NAME}] {OUTLINE_AGENT_CONFIG_PATH_ENV}: {os.environ.get(OUTLINE_AGENT_CONFIG_PATH_ENV, '<unset>')}",
        f"[{APP_NAME}] config yaml: {_describe_path(get_user_config_path())}",
        f"[{APP_NAME}] project root: {PROJECT_ROOT}",
        f"[{APP_NAME}] package prompt: {_describe_path(get_package_prompt_path())}",
        f"[{APP_NAME}] package prompt packs: {_describe_path(get_package_prompt_pack_dir())}",
        f"[{APP_NAME}] package internal prompts: {_describe_path(get_package_internal_prompt_dir())}",
        f"[{APP_NAME}] system prompt: {_describe_path(settings.system_prompt_path)}",
        f"[{APP_NAME}] prompt pack dir: {_describe_path(settings.prompt_pack_dir)}",
        f"[{APP_NAME}] internal prompt dir: {_describe_path(settings.internal_prompt_dir)}",
        f"[{APP_NAME}] workspace root: {settings.workspace_root}",
        f"[{APP_NAME}] webhook log dir: {settings.webhook_log_dir}",
        f"[{APP_NAME}] dedupe store: {settings.dedupe_store_path}",
        f"[{APP_NAME}] log file: {settings.log_file_path}",
        f"[{APP_NAME}] bind: {settings.host}:{settings.port}",
        f"[{APP_NAME}] trigger mode: {settings.trigger_mode}",
        f"[{APP_NAME}] dry run: {settings.dry_run}",
        f"[{APP_NAME}] tool execution rounds: {settings.tool_execution_max_rounds}",
        f"[{APP_NAME}] planner step budget: {settings.tool_execution_max_steps}",
        f"[{APP_NAME}] execution chunk size: {settings.tool_execution_chunk_size}",
    ]
    for line in lines:
        print(line, file=sys.stderr)
    for warning in _collect_startup_warnings(settings):
        print(f"[{APP_NAME}] warning: {warning}", file=sys.stderr)


def _collect_startup_warnings(settings: AppSettings) -> list[str]:
    warnings: list[str] = []
    if not get_user_config_path().exists():
        warnings.append(f"config yaml not found at {get_user_config_path()}")
    if not settings.system_prompt_path.exists():
        warnings.append(f"system prompt file not found at {settings.system_prompt_path}")
    if not settings.outline_api_base_url:
        warnings.append("OUTLINE_API_BASE_URL is not configured")
    if not settings.outline_api_key:
        warnings.append("OUTLINE_API_KEY is not configured")
    if not settings.outline_webhook_signing_secret:
        warnings.append("OUTLINE_WEBHOOK_SIGNING_SECRET is not configured")
    return warnings


def _describe_path(path: Path) -> str:
    status = "exists" if path.exists() else "missing"
    return f"{path} ({status})"
