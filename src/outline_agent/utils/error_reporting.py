from __future__ import annotations

from datetime import datetime, timezone


def summarize_exception(exc: BaseException, *, max_chars: int = 300) -> str:
    message = " ".join(str(exc).split())
    if not message:
        message = "(no details)"
    if len(message) > max_chars:
        message = message[: max_chars - 1].rstrip() + "…"
    return message


def generate_error_id(*, prefix: str = "err") -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{timestamp}"


def format_failure_comment(*, error_id: str, exc: BaseException) -> str:
    exc_name = exc.__class__.__name__
    exc_summary = summarize_exception(exc)
    return (
        "Sorry — I hit an internal error while processing this comment and stopped.\n\n"
        f"- error_id: `{error_id}`\n"
        f"- error_type: `{exc_name}`\n"
        f"- error: {exc_summary}\n\n"
        "Please try again later. If the issue persists, ask an admin to check the service logs."
    )
