from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from outline_agent.core import config as config_module
from outline_agent.core.config import AppSettings


@pytest.fixture
def config_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    user_root = tmp_path / "user-home"
    project_root = tmp_path / "project"
    package_prompt_root = tmp_path / "package-prompts"

    user_root.mkdir(parents=True, exist_ok=True)
    project_root.mkdir(parents=True, exist_ok=True)
    (package_prompt_root / "packs").mkdir(parents=True, exist_ok=True)
    (package_prompt_root / "00_system.md").write_text("Package prompt.", encoding="utf-8")
    (package_prompt_root / "packs/outline_style.md").write_text("Outline style.", encoding="utf-8")

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

    assert settings.system_prompt_path == config_paths["package_prompt_root"] / "00_system.md"
    assert settings.system_prompt == "Package prompt."


def test_prompt_pack_defaults(config_paths: dict[str, Path]) -> None:
    settings = AppSettings()

    assert settings.prompt_pack_dir == config_paths["package_prompt_root"] / "packs"
    assert settings.system_prompt_packs == ["outline_style"]


def test_log_file_path_defaults_to_user_config_root(config_paths: dict[str, Path]) -> None:
    settings = AppSettings()

    assert settings.log_file_path == config_paths["user_root"] / "logs/outline-agent.log"


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


def test_project_env_file_is_ignored_when_user_yaml_is_missing(
    config_paths: dict[str, Path],
) -> None:
    (config_paths["project_root"] / ".env").write_text(
        "\n".join(
            [
                "HOST=0.0.0.0",
                "PORT=9999",
            ]
        ),
        encoding="utf-8",
    )

    settings = AppSettings()

    assert settings.host == "127.0.0.1"
    assert settings.port == 8787


def test_relative_project_paths_are_resolved_from_project_root(config_paths: dict[str, Path]) -> None:
    project_root = config_paths["project_root"]
    project_prompt = project_root / "prompts/00_system.md"
    project_prompt.parent.mkdir(parents=True, exist_ok=True)
    project_prompt.write_text("Project prompt.", encoding="utf-8")

    settings = AppSettings(
        system_prompt_path=Path("prompts/00_system.md"),
        prompt_pack_dir=Path("prompts/packs"),
    )

    assert settings.system_prompt_path == project_root / "prompts/00_system.md"
    assert settings.prompt_pack_dir == project_root / "prompts/packs"


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


def test_explicit_config_path_changes_relative_resolution(
    config_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = config_paths["project_root"]
    config_path = repo_root / "config.dev.yaml"
    config_path.write_text(
        "runtime:\n  workspace_root: .data/agents\nlogging:\n  file_path: .data/logs/app.log\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OUTLINE_AGENT_CONFIG_PATH", str(config_path))

    settings = AppSettings()

    assert settings.workspace_root == repo_root / ".data/agents"
    assert settings.log_file_path == repo_root / ".data/logs/app.log"


def test_grouped_config_maps_module_sections_to_settings(config_paths: dict[str, Path]) -> None:
    (config_paths["user_root"] / "config.yaml").write_text(
        """
outline:
  api_base_url: https://outline.example.com/api
  api_key: secret
  webhook_signing_secret: signing
model:
  ref: demo/gpt-test
logging:
  level: INFO
runtime:
  workspace_root: data/agents
features:
  tool_use: false
  progress_comments: false
model_profiles:
  default: demo/gpt-test
  profiles:
    demo:
      provider: openai-responses
      base_url: https://example.com/v1
      api_key: secret
      models:
        - gpt-test
""".strip(),
        encoding="utf-8",
    )

    settings = AppSettings()

    assert settings.outline_api_base_url == "https://outline.example.com/api"
    assert settings.outline_api_key == "secret"
    assert settings.outline_webhook_signing_secret == "signing"
    assert settings.model_ref == "demo/gpt-test"
    assert settings.log_level == "INFO"
    assert settings.workspace_root == config_paths["user_root"] / "data/agents"
    assert settings.tool_use_enabled is False
    assert settings.progress_comment_enabled is False


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


def test_model_group_maps_feature_specific_model_refs(config_paths: dict[str, Path]) -> None:
    (config_paths["user_root"] / "config.yaml").write_text(
        """
model:
  ref: demo/gpt-main
  memory_ref: demo/gpt-memory
  document_update_ref: demo/gpt-doc
  tool_ref: demo/gpt-tool
  thread_session_ref: demo/gpt-thread
""".strip(),
        encoding="utf-8",
    )

    settings = AppSettings()

    assert settings.model_ref == "demo/gpt-main"
    assert settings.memory_model_ref == "demo/gpt-memory"
    assert settings.document_update_model_ref == "demo/gpt-doc"
    assert settings.tool_model_ref == "demo/gpt-tool"
    assert settings.thread_session_model_ref == "demo/gpt-thread"


def test_system_prompt_field_overrides_file_loading(config_paths: dict[str, Path]) -> None:
    settings = AppSettings(system_prompt="  Custom system prompt.  ")

    assert settings.system_prompt == "Custom system prompt."


def test_missing_system_prompt_file_raises_helpful_error(config_paths: dict[str, Path]) -> None:
    with pytest.raises(ValidationError, match="Required text file does not exist"):
        AppSettings(system_prompt_path=Path("prompts/does-not-exist.md"))
