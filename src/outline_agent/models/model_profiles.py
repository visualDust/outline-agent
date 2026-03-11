from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ModelProfileError(RuntimeError):
    """Raised when a model profile cannot be loaded or resolved."""


@dataclass(frozen=True)
class ResolvedModelProfile:
    alias: str
    provider: str
    base_url: str
    api_key: str
    model: str


class ModelProfileResolver:
    def __init__(self, config_path: Path):
        self.config_path = config_path

    def load(self) -> dict[str, Any]:
        if not self.config_path.exists():
            raise ModelProfileError(f"Model config not found: {self.config_path}")

        with self.config_path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}

        if not isinstance(data, dict):
            raise ModelProfileError("Model config must be a YAML mapping")
        nested = data.get("model_profiles")
        if nested is None:
            return data
        if not isinstance(nested, dict):
            raise ModelProfileError("Config field 'model_profiles' must be a YAML mapping")
        return nested

    def resolve(self, model_ref: str | None = None) -> ResolvedModelProfile:
        data = self.load()
        ref = model_ref or data.get("default")
        if not isinstance(ref, str) or not ref.strip():
            raise ModelProfileError("No model ref provided and no default configured")

        alias, slash, explicit_model = ref.partition("/")
        if not alias:
            raise ModelProfileError(f"Invalid model ref: {ref!r}")

        profiles = data.get("profiles", data.get("models"))
        if not isinstance(profiles, dict):
            raise ModelProfileError("Missing 'profiles' mapping in model config")

        raw_profile = profiles.get(alias)
        if not isinstance(raw_profile, dict):
            raise ModelProfileError(f"Unknown model alias: {alias}")

        provider = raw_profile.get("provider")
        base_url = raw_profile.get("base_url", raw_profile.get("baseUrl"))
        api_key = raw_profile.get("api_key", raw_profile.get("apiKey"))
        available_models = raw_profile.get("models", raw_profile.get("model"))

        if not isinstance(provider, str) or not provider:
            raise ModelProfileError(f"Profile {alias!r} is missing provider")
        if not isinstance(base_url, str) or not base_url:
            raise ModelProfileError(f"Profile {alias!r} is missing base_url")
        if not isinstance(api_key, str) or not api_key:
            raise ModelProfileError(f"Profile {alias!r} is missing api_key")

        model_name = self._resolve_model_name(explicit_model if slash else None, available_models, alias)
        return ResolvedModelProfile(
            alias=alias,
            provider=provider,
            base_url=base_url.rstrip("/"),
            api_key=api_key,
            model=model_name,
        )

    @staticmethod
    def _resolve_model_name(explicit_model: str | None, available_models: Any, alias: str) -> str:
        if isinstance(available_models, str):
            default_model = available_models
            allowed = {available_models}
        elif isinstance(available_models, list) and all(isinstance(item, str) for item in available_models):
            if not available_models:
                raise ModelProfileError(f"Profile {alias!r} has an empty model list")
            default_model = available_models[0]
            allowed = set(available_models)
        else:
            raise ModelProfileError(f"Profile {alias!r} has invalid model configuration")

        if explicit_model:
            if allowed and explicit_model not in allowed:
                raise ModelProfileError(
                    f"Model {explicit_model!r} is not listed under profile {alias!r}"
                )
            return explicit_model
        return default_model
