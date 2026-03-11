from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

import httpx

from ..models.model_profiles import ResolvedModelProfile


class ModelClientError(RuntimeError):
    """Raised when model invocation fails."""


@dataclass(frozen=True)
class ModelInputImage:
    data: bytes
    media_type: str

    def as_data_url(self) -> str:
        encoded = base64.b64encode(self.data).decode("ascii")
        return f"data:{self.media_type};base64,{encoded}"

    def as_base64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")


class ModelClient:
    def __init__(self, profile: ResolvedModelProfile, timeout: float = 60.0, max_output_tokens: int = 800):
        self.profile = profile
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens

    async def generate_reply(self, system_prompt: str, user_prompt: str) -> str:
        provider = self.profile.provider.lower()
        if provider == "openai-responses":
            return await self._call_openai_responses(system_prompt, user_prompt)
        if provider in {"openai", "openai-chat"}:
            return await self._call_openai_chat(system_prompt, user_prompt)
        if provider == "anthropic":
            return await self._call_anthropic(system_prompt, user_prompt)
        raise ModelClientError(f"Unsupported model provider: {self.profile.provider}")

    async def generate_reply_with_images(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        input_images: list[ModelInputImage],
    ) -> str:
        if not input_images:
            return await self.generate_reply(system_prompt, user_prompt)

        provider = self.profile.provider.lower()
        if provider == "openai-responses":
            return await self._call_openai_responses(system_prompt, user_prompt, input_images=input_images)
        if provider in {"openai", "openai-chat"}:
            return await self._call_openai_chat(system_prompt, user_prompt, input_images=input_images)
        if provider == "anthropic":
            return await self._call_anthropic(system_prompt, user_prompt, input_images=input_images)
        raise ModelClientError(f"Unsupported model provider: {self.profile.provider}")

    async def _call_openai_responses(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        input_images: list[ModelInputImage] | None = None,
    ) -> str:
        url = f"{self.profile.base_url}/responses"
        user_content: list[dict[str, Any]] = [{"type": "input_text", "text": user_prompt}]
        for image in input_images or []:
            user_content.append({"type": "input_image", "image_url": image.as_data_url()})
        payload = {
            "model": self.profile.model,
            "max_output_tokens": self.max_output_tokens,
            "input": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": system_prompt}],
                },
                {
                    "role": "user",
                    "content": user_content,
                },
            ],
        }
        headers = {
            "Authorization": f"Bearer {self.profile.api_key}",
            "Content-Type": "application/json",
        }
        data = await self._post_json(url, payload, headers)

        output_text = data.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text.strip()

        fragments: list[str] = []
        for item in data.get("output", []):
            if not isinstance(item, dict):
                continue
            for content in item.get("content", []):
                if not isinstance(content, dict):
                    continue
                text = content.get("text")
                if isinstance(text, str) and text.strip():
                    fragments.append(text.strip())
        if fragments:
            return "\n".join(fragments).strip()
        raise ModelClientError("OpenAI Responses API returned no assistant text")

    async def _call_openai_chat(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        input_images: list[ModelInputImage] | None = None,
    ) -> str:
        url = f"{self.profile.base_url}/chat/completions"
        user_content: str | list[dict[str, Any]]
        if input_images:
            user_content = [{"type": "text", "text": user_prompt}]
            for image in input_images:
                user_content.append({"type": "image_url", "image_url": {"url": image.as_data_url()}})
        else:
            user_content = user_prompt
        payload = {
            "model": self.profile.model,
            "max_tokens": self.max_output_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        }
        headers = {
            "Authorization": f"Bearer {self.profile.api_key}",
            "Content-Type": "application/json",
        }
        data = await self._post_json(url, payload, headers)
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()
        raise ModelClientError("OpenAI Chat API returned no assistant message")

    async def _call_anthropic(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        input_images: list[ModelInputImage] | None = None,
    ) -> str:
        url = f"{self.profile.base_url}/messages"
        message_content: str | list[dict[str, Any]]
        if input_images:
            message_content = [{"type": "text", "text": user_prompt}]
            for image in input_images:
                message_content.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": image.media_type,
                            "data": image.as_base64(),
                        },
                    }
                )
        else:
            message_content = user_prompt
        payload = {
            "model": self.profile.model,
            "system": system_prompt,
            "max_tokens": self.max_output_tokens,
            "messages": [{"role": "user", "content": message_content}],
        }
        headers = {
            "x-api-key": self.profile.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        data = await self._post_json(url, payload, headers)
        contents = data.get("content")
        fragments: list[str] = []
        if isinstance(contents, list):
            for item in contents:
                if not isinstance(item, dict):
                    continue
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    fragments.append(text.strip())
        if fragments:
            return "\n".join(fragments).strip()
        raise ModelClientError("Anthropic API returned no assistant text")

    async def _post_json(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise ModelClientError(f"Model request failed: {exc}") from exc

        if response.is_error:
            raise ModelClientError(f"Model API error {response.status_code}: {_extract_error_message(response)}")

        data = response.json()
        if not isinstance(data, dict):
            raise ModelClientError("Model API returned a non-object JSON response")
        return data


def _extract_error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text or response.reason_phrase

    if isinstance(payload, dict):
        for key in ("error", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, dict):
                nested = value.get("message")
                if isinstance(nested, str) and nested:
                    return nested
    return response.text or response.reason_phrase
