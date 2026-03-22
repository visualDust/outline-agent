from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from outline_agent.core import config as config_module
from outline_agent.core.config import AppSettings
from outline_agent.core.prompt_registry import PromptRegistry


@pytest.fixture
def config_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    user_root = tmp_path / "user-home"
    project_root = tmp_path / "project"
    package_prompt_root = tmp_path / "package-prompts"

    user_root.mkdir(parents=True, exist_ok=True)
    project_root.mkdir(parents=True, exist_ok=True)
    (package_prompt_root / "user/packs").mkdir(parents=True, exist_ok=True)
    (package_prompt_root / "internal").mkdir(parents=True, exist_ok=True)
    (package_prompt_root / "user/00_system.md").write_text("Package prompt.", encoding="utf-8")
    (package_prompt_root / "user/packs/outline_style.md").write_text("Outline style.", encoding="utf-8")
    (package_prompt_root / "internal/tool_planner_policy.md").write_text("Planner policy.", encoding="utf-8")

    monkeypatch.setenv("OUTLINE_AGENT_HOME", str(user_root))
    monkeypatch.setattr(config_module, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(config_module, "PACKAGE_PROMPT_ROOT", package_prompt_root)
    return {
        "user_root": user_root,
        "project_root": project_root,
        "package_prompt_root": package_prompt_root,
    }


def test_system_prompt_defaults_to_packaged_prompt_contents(config_paths: dict[str, Path]) -> None:
    settings = AppSettings()

    assert settings.system_prompt_path == config_paths["package_prompt_root"] / "user/00_system.md"
    assert settings.system_prompt == "Package prompt."


def test_prompt_pack_defaults(config_paths: dict[str, Path]) -> None:
    settings = AppSettings()

    assert settings.prompt_pack_dir == config_paths["package_prompt_root"] / "user/packs"
    assert settings.internal_prompt_dir == config_paths["package_prompt_root"] / "internal"
    assert settings.system_prompt_packs == ["outline_style"]

def test_user_yaml_applies_when_environment_is_unset(
    config_paths: dict[str, Path],
) -> None:
    (config_paths["user_root"] / "config.yaml").write_text(
        """
server:
  host: 0.0.0.0
  port: 9999
trigger:
  mention_aliases:
    - "@yaml-agent"
prompts:
  system_prompt_packs:
    - outline_style
    - custom_pack
""".strip(),
        encoding="utf-8",
    )

    settings = AppSettings()

    assert settings.host == "0.0.0.0"
    assert settings.port == 9999
    assert settings.mention_aliases == ["@yaml-agent"]
    assert settings.system_prompt_packs == ["outline_style", "custom_pack"]


def test_environment_variables_override_user_yaml(
    config_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (config_paths["user_root"] / "config.yaml").write_text(
        """
server:
  host: 0.0.0.0
  port: 9999
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("PORT", "8787")

    settings = AppSettings()

    assert settings.host == "127.0.0.1"
    assert settings.port == 8787

def test_relative_runtime_paths_are_resolved_from_user_config_root(config_paths: dict[str, Path]) -> None:
    settings = AppSettings(
        workspace_root=Path("runtime/agents"),
        webhook_log_dir=Path("runtime/webhooks"),
        dedupe_store_path=Path("runtime/processed.json"),
        log_file_path=Path("runtime/app.log"),
    )

    user_root = config_paths["user_root"]
    assert settings.workspace_root == user_root / "runtime/agents"
    assert settings.webhook_log_dir == user_root / "runtime/webhooks"
    assert settings.dedupe_store_path == user_root / "runtime/processed.json"
    assert settings.log_file_path == user_root / "runtime/app.log"

def test_outline_api_base_url_defaults_to_api_path_when_site_root_is_given(
    config_paths: dict[str, Path],
) -> None:
    (config_paths["user_root"] / "config.yaml").write_text(
        """
outline:
  api_base_url: https://outline.example.com/
""".strip(),
        encoding="utf-8",
    )

    settings = AppSettings()

    assert settings.outline_api_base_url == "https://outline.example.com/api"

def test_prompt_registry_prefers_project_user_optional_prompts_over_package_defaults(
    config_paths: dict[str, Path],
) -> None:
    project_reply_policy = config_paths["project_root"] / "prompts/user/reply_policy.md"
    project_reply_policy.parent.mkdir(parents=True, exist_ok=True)
    project_reply_policy.write_text("Project reply policy.", encoding="utf-8")
    (config_paths["package_prompt_root"] / "user/reply_policy.md").write_text(
        "Package reply policy.",
        encoding="utf-8",
    )

    registry = PromptRegistry.from_settings(AppSettings())

    assert registry.load_user_optional("reply_policy.md") == "Project reply policy."

def test_missing_system_prompt_file_raises_helpful_error(config_paths: dict[str, Path]) -> None:
    with pytest.raises(ValidationError, match="Required text file does not exist"):
        AppSettings(system_prompt_path=Path("prompts/user/does-not-exist.md"))


def test_gemini_api_key_can_come_from_google_api_key_env(
    config_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key-from-env")

    settings = AppSettings()

    assert settings.gemini_api_key == "google-key-from-env"


def test_gemini_base_url_can_be_configured_from_nested_web_search_yaml(config_paths: dict[str, Path]) -> None:
    (config_paths["user_root"] / "config.yaml").write_text(
        """
web_search:
  provider: gemini
  gemini:
    base_url: https://gemini-gateway.example.com
""".strip(),
        encoding="utf-8",
    )

    settings = AppSettings()

    assert settings.gemini_base_url == "https://gemini-gateway.example.com"


def test_web_search_provider_can_be_configured_from_yaml(config_paths: dict[str, Path]) -> None:
    (config_paths["user_root"] / "config.yaml").write_text(
        """
web_search:
  provider: openai
  openai:
    model: gpt-5
""".strip(),
        encoding="utf-8",
    )

    settings = AppSettings()

    assert settings.web_search_provider == "openai"
    assert settings.openai_web_search_model == "gpt-5"


def test_nested_web_search_gemini_config_can_be_configured_from_yaml(config_paths: dict[str, Path]) -> None:
    (config_paths["user_root"] / "config.yaml").write_text(
        """
web_search:
  provider: gemini
  gemini:
    base_url: https://gemini-nested.example.com
""".strip(),
        encoding="utf-8",
    )

    settings = AppSettings()

    assert settings.web_search_provider == "gemini"
    assert settings.gemini_base_url == "https://gemini-nested.example.com"


def test_openai_web_search_api_key_can_come_from_openai_api_key_env(
    config_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key-from-env")

    settings = AppSettings(web_search_provider="openai")

    assert settings.openai_web_search_api_key == "openai-key-from-env"
