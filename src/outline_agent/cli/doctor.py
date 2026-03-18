from __future__ import annotations

import argparse
import asyncio
import json
import sys

from ..bootstrap import build_outline_client
from ..core.config import APP_NAME, clear_settings_cache, create_default_config, get_settings, get_user_config_path
from ..core.logging import configure_logging, logger
from ..doctor.workspace_sync import (
    apply_workspace_sync_repair_plan,
    build_workspace_sync_repair_plan,
    format_workspace_sync_repair_plan_text,
    format_workspace_sync_repair_run_text,
    format_workspace_sync_report_text,
    run_workspace_sync_diagnostics,
)
from . import _apply_cli_overrides, _restore_cli_overrides


def configure_doctor_parser(doctor_parser: argparse.ArgumentParser) -> None:
    doctor_subparsers = doctor_parser.add_subparsers(dest="doctor_command", required=True)

    workspace_sync_parser = doctor_subparsers.add_parser(
        "workspace-sync",
        help="Check local workspace state against Outline",
    )
    workspace_sync_parser.add_argument(
        "--depth",
        choices=("coarse", "deep"),
        default="coarse",
        help="Validation depth (default: coarse)",
    )
    workspace_sync_parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Emit machine-readable JSON output",
    )
    workspace_sync_parser.add_argument(
        "--concurrency",
        type=int,
        default=5,
        help="Maximum concurrent Outline API checks (default: 5)",
    )
    workspace_sync_parser.add_argument(
        "--fix",
        action="store_true",
        help="Archive safe local stale state after confirmation",
    )
    workspace_sync_parser.add_argument(
        "--yes",
        action="store_true",
        help="Apply --fix changes without interactive confirmation",
    )
    workspace_sync_parser.add_argument("--collection-id", help="Only inspect one local collection workspace")
    workspace_sync_parser.add_argument("--document-id", help="Only inspect one local document workspace")
    workspace_sync_parser.add_argument("--config-path", help="Override config YAML path")
    workspace_sync_parser.add_argument("--workspace-root", help="Override workspace root")
    workspace_sync_parser.add_argument("--log-file-path", help="Override log file path")
    workspace_sync_parser.add_argument("--log-level", help="Override log level")



def run_workspace_sync_command(args: argparse.Namespace) -> int:
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
        logger.info(
            "Doctor workspace-sync started: depth={}, concurrency={}, collection_id={}, document_id={}, fix={}",
            args.depth,
            args.concurrency,
            args.collection_id,
            args.document_id,
            args.fix,
        )

        try:
            outline_client = build_outline_client(settings)
            report = asyncio.run(
                run_workspace_sync_diagnostics(
                    settings=settings,
                    outline_client=outline_client,
                    depth=args.depth,
                    concurrency=args.concurrency,
                    collection_id=args.collection_id,
                    document_id=args.document_id,
                )
            )
        except Exception as exc:
            logger.exception("Doctor workspace-sync failed")
            print(f"[{APP_NAME}] error: doctor workspace-sync failed: {exc}", file=sys.stderr)
            return 2

        if not args.fix:
            if args.json_output:
                print(report.to_json())
            else:
                print(format_workspace_sync_report_text(report))
                if report.findings:
                    print("")
                    print("Tip: re-run with --fix to archive safe local stale workspaces.")
            return report.exit_code()

        include_inaccessible_repairs = False
        repair_plan = build_workspace_sync_repair_plan(
            settings=settings,
            report=report,
            include_inaccessible=include_inaccessible_repairs,
        )

        if not args.json_output:
            print(format_workspace_sync_report_text(report))
            print("")
            print(format_workspace_sync_repair_plan_text(repair_plan))

        if not repair_plan.actions:
            if args.json_output:
                payload = {
                    "report": report.to_dict(),
                    "repair_plan": repair_plan.to_dict(),
                }
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            return report.exit_code()

        if not args.yes:
            if not sys.stdin.isatty():
                print(
                    f"[{APP_NAME}] error: --fix requires confirmation; re-run with --yes in non-interactive mode",
                    file=sys.stderr,
                )
                return 2
            inaccessible_count = _count_inaccessible_findings(report)
            if inaccessible_count:
                include_inaccessible_repairs = _prompt_for_inaccessible_confirmation(inaccessible_count)
                repair_plan = build_workspace_sync_repair_plan(
                    settings=settings,
                    report=report,
                    include_inaccessible=include_inaccessible_repairs,
                )
                print("")
                print(format_workspace_sync_repair_plan_text(repair_plan))
                if not repair_plan.actions:
                    print("")
                    print("No changes applied.")
                    return report.exit_code()
            confirmed = _prompt_for_confirmation(len(repair_plan.actions))
            if not confirmed:
                if args.json_output:
                    payload = {
                        "report": report.to_dict(),
                        "repair_plan": repair_plan.to_dict(),
                        "repair_run": {"counts": {}, "results": [], "status": "cancelled"},
                    }
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    print("")
                    print("No changes applied.")
                return report.exit_code()

        try:
            repair_run = apply_workspace_sync_repair_plan(settings=settings, plan=repair_plan)
        except Exception as exc:
            logger.exception("Doctor workspace-sync repair phase failed")
            print(f"[{APP_NAME}] error: doctor workspace-sync repair failed: {exc}", file=sys.stderr)
            return 2

        if repair_run.has_failures:
            if args.json_output:
                payload = {
                    "report": report.to_dict(),
                    "repair_plan": repair_plan.to_dict(),
                    "repair_run": repair_run.to_dict(),
                }
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print("")
                print(format_workspace_sync_repair_run_text(repair_run))
            return 2

        try:
            final_report = asyncio.run(
                run_workspace_sync_diagnostics(
                    settings=settings,
                    outline_client=outline_client,
                    depth=args.depth,
                    concurrency=args.concurrency,
                    collection_id=args.collection_id,
                    document_id=args.document_id,
                )
            )
        except Exception as exc:
            logger.exception("Doctor workspace-sync post-fix verification failed")
            print(f"[{APP_NAME}] error: doctor workspace-sync post-fix verification failed: {exc}", file=sys.stderr)
            return 2

        if args.json_output:
            payload = {
                "report": report.to_dict(),
                "repair_plan": repair_plan.to_dict(),
                "repair_run": repair_run.to_dict(),
                "final_report": final_report.to_dict(),
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print("")
            print(format_workspace_sync_repair_run_text(repair_run))
            print("")
            print("Post-fix verification")
            print(format_workspace_sync_report_text(final_report))
        return final_report.exit_code()
    finally:
        _restore_cli_overrides(previous_env)
        clear_settings_cache()


def _prompt_for_confirmation(action_count: int) -> bool:
    response = input(f"Apply {action_count} safe local archival repairs? [y/N]: ").strip().lower()
    return response in {"y", "yes"}


def _prompt_for_inaccessible_confirmation(inaccessible_count: int) -> bool:
    response = input(
        f"Include {inaccessible_count} inaccessible (403) local workspaces in the repair plan? [y/N]: "
    ).strip().lower()
    return response in {"y", "yes"}


def _count_inaccessible_findings(report) -> int:
    return sum(1 for finding in report.findings if finding.kind.startswith("inaccessible_remote_"))
