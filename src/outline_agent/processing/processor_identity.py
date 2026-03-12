from __future__ import annotations

from ..clients.outline_client import OutlineClient, OutlineClientError, OutlineUser
from ..core.config import AppSettings
from ..core.logging import logger

_AUTH_ERROR_MARKERS = (
    "outline api error 401",
    "outline api error 403",
    "unauthorized",
    "forbidden",
    "api key",
    "auth.info",
    "token expired",
    "expired token",
    "invalid token",
)


def is_outline_auth_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _AUTH_ERROR_MARKERS)


def cache_runtime_identity(*, settings: AppSettings, current_user: OutlineUser) -> None:
    changed = (
        settings.runtime_outline_user_id != current_user.id or settings.runtime_outline_user_name != current_user.name
    )
    settings.runtime_outline_user_id = current_user.id
    settings.runtime_outline_user_name = current_user.name
    if changed:
        logger.info(
            "Resolved runtime Outline identity: {} ({})",
            current_user.id,
            current_user.name or "unknown",
        )


def invalidate_runtime_identity(*, settings: AppSettings, reason: str | None = None) -> None:
    had_cached_identity = bool(settings.runtime_outline_user_id or settings.runtime_outline_user_name)
    settings.runtime_outline_user_id = None
    settings.runtime_outline_user_name = None
    if had_cached_identity:
        if reason:
            logger.warning("Cleared cached runtime Outline identity: {}", reason)
        else:
            logger.warning("Cleared cached runtime Outline identity.")


async def resolve_agent_identity(
    *,
    settings: AppSettings,
    outline_client: OutlineClient,
) -> str | None:
    if settings.runtime_outline_user_id:
        return settings.runtime_outline_user_id

    try:
        current_user = await outline_client.current_user()
    except OutlineClientError as exc:
        if is_outline_auth_error(exc):
            invalidate_runtime_identity(settings=settings, reason=str(exc))
        logger.warning("Unable to resolve runtime Outline user identity: {}", exc)
        return None

    cache_runtime_identity(settings=settings, current_user=current_user)
    return current_user.id
