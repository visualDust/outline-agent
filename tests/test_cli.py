from __future__ import annotations

from pathlib import Path

from outline_agent import cli as cli_module
from outline_agent.core import config as config_module


async def _validated_user() -> object:
    class User:
        id = "user-1"
        name = "Agent"

    return User()


def test_start_command_prints_config_sources(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
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

    (user_root / "config.yaml").write_text(
        """
server:
  host: 0.0.0.0
  port: 9999
outline:
  api_base_url: https://outline.example.com/api
  api_key: ol_api_test
model_profiles:
  default: demo/gpt-test
  profiles:
    demo:
      provider: openai-responses
      base_url: https://example.com/v1
      api_key: secret
      models:
        - gpt-test
logging:
  level: INFO
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setenv("OUTLINE_AGENT_HOME", str(user_root))
    monkeypatch.setattr(config_module, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(config_module, "PACKAGE_PROMPT_ROOT", package_prompt_root)
    monkeypatch.setattr(cli_module, "PROJECT_ROOT", project_root)

    called: dict[str, object] = {}

    def fake_run(app: str, *, host: str, port: int, reload: bool, log_level: str) -> None:
        called.update(
            {
                "app": app,
                "host": host,
                "port": port,
                "reload": reload,
                "log_level": log_level,
            }
        )

    monkeypatch.setattr(cli_module, "validate_outline_runtime_identity", lambda settings: _validated_user())
    monkeypatch.setattr(cli_module.uvicorn, "run", fake_run)

    exit_code = cli_module.main(["start"])

    assert exit_code == 0
    assert called == {
        "app": "outline_agent.app:app",
        "host": "0.0.0.0",
        "port": 9999,
        "reload": False,
        "log_level": "info",
    }

    stderr = capsys.readouterr().err
    assert "config yaml:" in stderr
    assert str(user_root / "config.yaml") in stderr
    assert "bind: 0.0.0.0:9999" in stderr


def test_start_command_accepts_explicit_config_path(
    monkeypatch,
    tmp_path: Path,
) -> None:
    user_root = tmp_path / "user-home"
    project_root = tmp_path / "project"
    package_prompt_root = tmp_path / "package-prompts"
    custom_root = tmp_path / "dev-config"
    custom_config_path = custom_root / "config.yaml"

    user_root.mkdir(parents=True, exist_ok=True)
    project_root.mkdir(parents=True, exist_ok=True)
    custom_root.mkdir(parents=True, exist_ok=True)
    (package_prompt_root / "user/packs").mkdir(parents=True, exist_ok=True)
    (package_prompt_root / "internal").mkdir(parents=True, exist_ok=True)
    (package_prompt_root / "user/00_system.md").write_text("Package prompt.", encoding="utf-8")
    (package_prompt_root / "user/packs/outline_style.md").write_text("Outline style.", encoding="utf-8")
    (package_prompt_root / "internal/tool_planner_policy.md").write_text("Planner policy.", encoding="utf-8")
    custom_config_path.write_text(
        """
server:
  host: 0.0.0.0
  port: 9999
outline:
  api_base_url: https://outline.example.com/api
  api_key: ol_api_test
model_profiles:
  default: demo/gpt-test
  profiles:
    demo:
      provider: openai-responses
      base_url: https://example.com/v1
      api_key: secret
      models:
        - gpt-test
logging:
  file_path: .data/logs/app.log
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setenv("OUTLINE_AGENT_HOME", str(user_root))
    monkeypatch.setattr(config_module, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(config_module, "PACKAGE_PROMPT_ROOT", package_prompt_root)
    monkeypatch.setattr(cli_module, "PROJECT_ROOT", project_root)

    called: dict[str, object] = {}

    def fake_run(app: str, *, host: str, port: int, reload: bool, log_level: str) -> None:
        called.update(
            {
                "app": app,
                "host": host,
                "port": port,
                "reload": reload,
                "log_level": log_level,
            }
        )

    monkeypatch.setattr(cli_module, "validate_outline_runtime_identity", lambda settings: _validated_user())
    monkeypatch.setattr(cli_module.uvicorn, "run", fake_run)

    exit_code = cli_module.main(["start", "--config-path", str(custom_config_path)])

    assert exit_code == 0
    assert called["host"] == "0.0.0.0"
    assert called["port"] == 9999


def test_start_command_exits_when_outline_identity_validation_fails(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
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

    (user_root / "config.yaml").write_text(
        """
server:
  host: 0.0.0.0
  port: 9999
outline:
  api_base_url: https://outline.example.com/api
  api_key: ol_api_test
model_profiles:
  default: demo/gpt-test
  profiles:
    demo:
      provider: openai-responses
      base_url: https://example.com/v1
      api_key: secret
      models:
        - gpt-test
logging:
  level: INFO
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setenv("OUTLINE_AGENT_HOME", str(user_root))
    monkeypatch.setattr(config_module, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(config_module, "PACKAGE_PROMPT_ROOT", package_prompt_root)
    monkeypatch.setattr(cli_module, "PROJECT_ROOT", project_root)

    async def fail_validation(settings):
        raise RuntimeError("invalid api key")

    def fail_run(*args, **kwargs) -> None:
        raise AssertionError("uvicorn.run should not be called when identity validation fails")

    monkeypatch.setattr(cli_module, "validate_outline_runtime_identity", fail_validation)
    monkeypatch.setattr(cli_module.uvicorn, "run", fail_run)

    exit_code = cli_module.main(["start"])

    assert exit_code == 2
    assert "outline API identity validation failed: invalid api key" in capsys.readouterr().err


def test_start_command_bootstraps_missing_config_and_exits(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
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
    monkeypatch.setattr(cli_module, "PROJECT_ROOT", project_root)

    def fail_run(*args, **kwargs) -> None:
        raise AssertionError("uvicorn.run should not be called when bootstrapping config")

    monkeypatch.setattr(cli_module.uvicorn, "run", fail_run)

    exit_code = cli_module.main(["start"])

    created_config = user_root / "config.yaml"
    assert exit_code == 0
    assert created_config.exists()
    assert "server:" in created_config.read_text(encoding="utf-8")

    stderr = capsys.readouterr().err
    assert "created initial config" in stderr
    assert str(created_config) in stderr
