from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from ..bootstrap import build_outline_client
from ..core.config import APP_NAME, clear_settings_cache, create_default_config, get_settings, get_user_config_path
from ..core.logging import configure_logging, logger
from . import _apply_cli_overrides, _restore_cli_overrides


def configure_auth_parser(auth_parser: argparse.ArgumentParser) -> None:
    auth_subparsers = auth_parser.add_subparsers(dest="auth_command", required=True)

    info_parser = auth_subparsers.add_parser(
        "info",
        help="Verify the active Outline config can authenticate and show the current identity",
    )
    info_parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Emit machine-readable JSON output",
    )
    info_parser.add_argument("--config-path", help="Override config YAML path")
    info_parser.add_argument("--log-file-path", help="Override log file path")
    info_parser.add_argument("--log-level", help="Override log level")



def run_auth_info_command(args: argparse.Namespace) -> int:
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
        logger.info("Auth info command started: config_path={}", config_path)

        try:
            auth_payload = asyncio.run(build_outline_client(settings).auth_info())
        except Exception as exc:
            logger.exception("Outline auth.info failed")
            print(f"[{APP_NAME}] error: outline auth.info failed: {exc}", file=sys.stderr)
            return 2

        if args.json_output:
            payload = {
                "config_path": str(config_path),
                "base_url": settings.outline_api_base_url,
                "auth": auth_payload,
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(
                _format_auth_info_text(
                    config_path=str(config_path),
                    base_url=settings.outline_api_base_url,
                    payload=auth_payload,
                )
            )
        return 0
    finally:
        _restore_cli_overrides(previous_env)
        clear_settings_cache()



def _format_auth_info_text(*, config_path: str, base_url: str | None, payload: dict[str, Any]) -> str:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    team = data.get("team") if isinstance(data.get("team"), dict) else {}

    lines = [
        "Outline auth info",
        f"- config path: {config_path}",
        f"- base url: {base_url or '<unset>'}",
        f"- user id: {_display_value(user.get('id'))}",
        f"- user name: {_display_value(user.get('name'))}",
        f"- user email: {_display_value(user.get('email'))}",
        f"- team id: {_display_value(team.get('id'))}",
        f"- team name: {_display_value(team.get('name'))}",
        f"- team url: {_display_value(team.get('url'))}",
    ]
    return "\n".join(lines)



def _display_value(value: Any) -> str:
    if isinstance(value, str) and value:
        return value
    return "<unknown>"
