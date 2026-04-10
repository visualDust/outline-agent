from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..clients.model_client import ModelClient, ModelClientError, ModelInputImage, ModelStreamEvent
from ..clients.outline_client import OutlineClient, OutlineClientError
from ..clients.outline_comments import OUTLINE_COMMENT_MAX_CHARS
from ..core.logging import logger

_STREAM_UPDATE_MIN_INTERVAL_SECONDS = 0.8
_STREAM_PREVIEW_MAX_CHARS = min(OUTLINE_COMMENT_MAX_CHARS, 3500)


@dataclass(slots=True)
class ReplyStreamCoordinator:
    model_client: ModelClient
    outline_client: OutlineClient
    document_id: str
    placeholder_comment_id: str | None

    thinking_fragments: list[str] = field(default_factory=list)
    answer_fragments: list[str] = field(default_factory=list)
    _last_rendered_text: str | None = None
    _last_update_at: float = field(default_factory=lambda: 0.0)

    async def generate_reply(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        input_images: list[ModelInputImage],
    ) -> str:
        async for event in self.model_client.stream_reply_events(
            system_prompt,
            user_prompt,
            input_images=input_images,
        ):
            await self._consume_event(event)

        final_answer = "".join(self.answer_fragments).strip()
        if final_answer:
            return final_answer
        raise ModelClientError("Reply stream completed without a final answer")

    async def _consume_event(self, event: ModelStreamEvent) -> None:
        if event.kind == "thinking_delta" and event.text:
            self.thinking_fragments.append(event.text)
            await self._sync_preview(force=False)
            return
        if event.kind == "answer_delta" and event.text:
            self.answer_fragments.append(event.text)
            await self._sync_preview(force=False)
            return
        if event.kind == "completed":
            await self._sync_preview(force=True)

    async def _sync_preview(self, *, force: bool) -> None:
        if not self.placeholder_comment_id:
            return
        now = time.monotonic()
        if not force and (now - self._last_update_at) < _STREAM_UPDATE_MIN_INTERVAL_SECONDS:
            return

        text = self._render_preview_text()
        if not text or text == self._last_rendered_text:
            return

        try:
            await self.outline_client.update_comment(self.placeholder_comment_id, text)
        except OutlineClientError as exc:
            logger.warning(
                "Failed to update streamed reply preview comment {} for document {}: {}",
                self.placeholder_comment_id,
                self.document_id,
                exc,
            )
            return

        self._last_rendered_text = text
        self._last_update_at = now

    def _render_preview_text(self) -> str:
        answer_text = "".join(self.answer_fragments).strip()
        if answer_text:
            return _truncate_preview(answer_text)

        thinking_text = "".join(self.thinking_fragments).strip()
        if thinking_text:
            return _truncate_preview(f"<thinking>\n{thinking_text}\n</thinking>")

        return ""


def _truncate_preview(text: str) -> str:
    if len(text) <= _STREAM_PREVIEW_MAX_CHARS:
        return text
    suffix = "\n\n…"
    budget = max(0, _STREAM_PREVIEW_MAX_CHARS - len(suffix))
    return text[:budget].rstrip() + suffix
