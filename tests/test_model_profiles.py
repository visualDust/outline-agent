from __future__ import annotations

from pathlib import Path

import pytest

from outline_agent.models.model_profiles import ModelProfileError, ModelProfileResolver


def test_resolve_default_model_profile(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
model_profiles:
  default: demo/gpt-test
  profiles:
    demo:
      provider: openai-responses
      base_url: https://example.com/v1
      api_key: secret
      models:
        - gpt-test
        - gpt-alt
""".strip(),
        encoding="utf-8",
    )

    profile = ModelProfileResolver(config_path).resolve()
    assert profile.alias == "demo"
    assert profile.provider == "openai-responses"
    assert profile.model == "gpt-test"


def test_resolve_rejects_unknown_model_name(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
model_profiles:
  profiles:
    demo:
      provider: anthropic
      base_url: https://example.com
      api_key: secret
      models:
        - claude-sonnet
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ModelProfileError):
        ModelProfileResolver(config_path).resolve("demo/not-listed")
